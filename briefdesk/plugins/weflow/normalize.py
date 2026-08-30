"""消息规范化与预过滤 — weflow 版（weflow-server :5033）。

- normalize_sse / normalize_rest 均产出源无关的 InternalMessage 列表
  （普通消息 1 条；公众号文章卡片按 mmreader 拆为多条，见 parse_appmsg_xml）
- pre_filter_sse / pre_filter_rest 丢弃撤回、非文本、空/短内容与附件
  占位符；图片消息放行：SSE [图片] 交由 normalize_sse 回查 REST 获取
  媒体路径（上游 v0.3.0 推送携带 media 元数据 {type, fileName, md5}，
  type 非 image 时跳过无效回查、元数据缺失时保持回查），REST
  localType=3 且带 media.url 时直接放行（供 OCR）
- 文章卡片（localType=0x500000031）放行并解析：weflow-server 的 REST 消息
  content 为 [消息] 占位、rawContent 为原始 <msg><appmsg> XML（无需像
  weflow-legacy 那样回查 media=False）；SSE 事件无 rawContent，按内容形状
  识别、解析失败按原文单条放行
"""

import html
import logging
import re
from typing import TypedDict

from briefdesk.masking import clean_display_name
from briefdesk.plugins.weflow.client import WeFlowClient, WeFlowEvent, WeFlowMessage
from briefdesk.types import InternalMessage

logger = logging.getLogger(__name__)

# weflow-server mediaUrl 的前缀，用于提取相对路径
_MEDIA_URL_PREFIX = "/api/v1/media/"

# 微信 type-49 链接卡片（公众号文章推送 / 聊天内转发的文章卡片）：
# 0x500000031，rawContent 为 <msg><appmsg>… XML
_APPMSG_LOCAL_TYPE = 21474836529


def _extract_media_path(media_url: str) -> str | None:
    """从 weflow-server 的完整 mediaUrl 中提取相对路径（去掉查询串）。

    "http://127.0.0.1:5033/api/v1/media/xxx/images/abc.jpg?access_token=..."
    → "xxx/images/abc.jpg"
    """
    idx = media_url.find(_MEDIA_URL_PREFIX)
    if idx < 0:
        return None
    path = media_url[idx + len(_MEDIA_URL_PREFIX):]
    return path.split("?", 1)[0]


# ── 文章卡片 XML 解析 ──


class ParsedArticle(TypedDict):
    """appmsg XML 中单篇文章的提取结果。"""

    title: str
    summary: str
    url: str


def _cdata(block: str, tag: str) -> str:
    """提取块内首个 <tag> 的文本，兼容 CDATA 与纯文本两种写法。

    微信 appmsg XML 中同一字段可能以：
      <title><![CDATA[...]]></title>
    或：
      <title>纯文本</title>
    形式出现。返回值会做 HTML 实体反转义（如 &amp; → &）。
    """
    m = re.search(
        rf"<{tag}>\s*(?:<!\[CDATA\[(.*?)\]\]>|(.*?))\s*</{tag}>",
        block,
        re.DOTALL,
    )
    if not m:
        return ""
    value = m.group(1) if m.group(1) is not None else m.group(2)
    return html.unescape(value.strip())


def parse_appmsg_xml(content: str) -> list[ParsedArticle]:
    """解析微信 appmsg 文章卡片 XML → 文章列表。

    多图文（mmreader/category/item[] 逐篇提取）优先；无 item 时退化解析
    外层 appmsg 的 title/des/url（单图文卡片）。标题为空的条目跳过
    （视频/占位条目只有 text_title 等字段，不构成文章）。
    """
    articles: list[ParsedArticle] = []
    for item in re.findall(r"<item>(.*?)</item>", content, re.DOTALL):
        title = _cdata(item, "title") or _cdata(item, "title_v2")
        if not title:
            continue
        articles.append(
            ParsedArticle(
                title=title,
                summary=_cdata(item, "summary"),
                url=_cdata(item, "url"),
            )
        )

    if not articles:
        # 单图文退化路径：只在外层 appmsg（mmreader 之前）内找 title/url
        head = content.split("<mmreader", 1)[0]
        title = _cdata(head, "title")
        if title:
            articles.append(
                ParsedArticle(
                    title=title,
                    summary=_cdata(head, "des"),
                    url=_cdata(head, "url"),
                )
            )
    return articles


def _is_appmsg_content(content: str) -> bool:
    """内容形状是否为 appmsg 文章卡片 XML。"""
    stripped = content.lstrip()
    return stripped.startswith("<msg") and "<appmsg" in stripped


def _article_messages(
    *,
    msg_id_base: str,
    articles: list[ParsedArticle],
    sender_name: str,
    sender_id: str,
    session_id: str,
    group_name: str,
    timestamp: int,
    is_self: bool = False,
) -> list[InternalMessage]:
    """按文章拆条构造 InternalMessage（msg_id = {base}_{idx}，文档序 1 起）。

    content 只含标题与摘要（供 AI 分类与前端展示）；原文链接存 article_url。
    is_self 继承原始消息判定（自己转发的文章卡片同样需过滤）。
    """
    msgs: list[InternalMessage] = []
    for i, a in enumerate(articles, start=1):
        lines = [f"标题：{a['title']}"]
        if a["summary"]:
            lines.append(f"摘要：{a['summary']}")
        msgs.append(
            InternalMessage(
                msg_id=f"{msg_id_base}_{i}",
                content="\n".join(lines),
                sender_name=sender_name,
                sender_id=sender_id,
                session_id=session_id,
                group_name=group_name,
                timestamp=timestamp,
                article_url=a["url"],
                is_self=is_self,
            )
        )
    return msgs


# ── Normalize SSE → list[InternalMessage] ──


async def normalize_sse(
    event: WeFlowEvent, client: WeFlowClient | None = None
) -> list[InternalMessage]:
    """SSE 事件 → InternalMessage 列表（普通消息 1 条，文章卡片拆条）。

    文章卡片按内容形状识别（不依赖 sessionType，公众号推送与群聊转发
    同格式）；解析失败（无 title）时按原文单条放行，维持既有行为。
    is_self 恒 False：weflow-server 推送端据 isSend=1 主动跳过自己发送的
    消息，实时路径天然不含自消息，无需重复过滤。
    """
    is_group = event.get("sessionType") == "group"
    content = event.get("content", "")
    image_urls: list[str] = []

    # 显示名净化必须发生在空值回退之前：sourceName/groupName 可能携带
    # 控制字符/纯空白脏数据，净化后为空才回退到会话 id / "未知"。
    sender_name = clean_display_name(event.get("sourceName")) or "未知"
    group_name = (
        (clean_display_name(event.get("groupName")) or event["sessionId"])
        if is_group
        else sender_name
    )
    session_id = event.get("sessionId", "")
    timestamp = event.get("timestamp", 0)
    rawid = event.get("rawid", "")

    # [图片] 回查预检：推送携带的 media 元数据（v0.3.0 起）不含 url，字节
    # 仍需 REST media=1 导出回填，但 type 明确非 image（语音/视频/表情/
    # 文件）时回查必无图片 URL，跳过省一次本机 HTTP；元数据缺失/null 视为
    # 未知，保持原回查（边缘情形：SSE 解析失败但 REST 可导出）
    media_meta = event.get("media")
    if content.strip() == "[图片]" and client is not None:
        if media_meta is not None and media_meta.get("type") != "image":
            logger.debug(
                "SSE rawid=%s: media.type=%s 非图片，跳过图片回查",
                rawid,
                media_meta.get("type"),
            )
        else:
            path = await client.fetch_message_media(session_id, rawid, timestamp)
            if path:
                image_urls.append(path)
            logger.debug(
                "SSE rawid=%s: 图片回查%s",
                rawid,
                "成功" if image_urls else "无可用媒体路径",
            )

    # 回查未命中/媒体元数据非图片：纯占位符消息无信息价值，整条丢弃——
    # 与 REST 路径「无 media.url 的图片消息整条丢弃」及 qqflow pre_filter
    # 语义对齐；旧实现放行为纯文本 "[图片]"，会产生噪音卡片（审查回归）
    if content.strip() == "[图片]" and not image_urls:
        logger.debug("SSE rawid=%s: 图片回查未命中，丢弃纯占位符消息", rawid)
        return []

    # 文章卡片：拆条解析（公众号推送与群聊转发同格式）
    if _is_appmsg_content(content):
        articles = parse_appmsg_xml(content)
        if articles:
            msgs = _article_messages(
                msg_id_base=rawid,
                articles=articles,
                sender_name=sender_name,
                sender_id="",
                session_id=session_id,
                group_name=group_name,
                timestamp=timestamp,
            )
            logger.debug(
                "SSE rawid=%s: appmsg 拆条 %d 条", rawid, len(msgs)
            )
            return msgs

    normalized = InternalMessage(
        msg_id=rawid,
        content=content,
        sender_name=sender_name,
        sender_id="",
        session_id=session_id,
        group_name=group_name,
        timestamp=timestamp,
        image_urls=image_urls,
    )
    logger.debug(
        "SSE → msg_id=%s, sender=%s, len=%d, images=%d",
        normalized.msg_id,
        sender_name,
        len(normalized.content),
        len(image_urls),
    )
    return [normalized]


# WeChat attachment placeholder patterns from weflow-server
_ATTACHMENT_RE = re.compile(
    r"^\[(图片|文件|视频|语音|链接|小程序|聊天记录|位置|名片|动画表情|红包|转账|音乐|笔记|直播|文件消息)\]"
    r"|^\s*\[[^\]]+\]\s*$"
)


def pre_filter_sse(event: WeFlowEvent) -> bool:
    if event.get("event") == "message.revoke":
        logger.debug("丢弃 SSE rawid=%s: 撤回消息", event.get("rawid"))
        return False
    if event.get("event") != "message.new":
        logger.debug("丢弃 SSE rawid=%s: 事件类型 %s", event.get("rawid"), event.get("event"))
        return False
    if not event.get("rawid"):
        # 无 rawid 无法生成 msg_id/标记 processed，放行只会在去重缓存
        # ("message.new","") 与回填之间反复投递——就地丢弃（审查 A5）
        logger.debug("丢弃 SSE: message.new 缺 rawid")
        return False
    c: str = event.get("content", "")
    if not c or c == "[消息]":
        logger.debug("丢弃 SSE rawid=%s: 空内容/[消息]", event.get("rawid"))
        return False
    # 图片消息放行，交由 normalize_sse 回查 REST 获取媒体路径做 OCR
    if c.strip() == "[图片]":
        return True
    if len(c.strip()) < 5:
        logger.debug("丢弃 SSE rawid=%s: 内容过短", event.get("rawid"))
        return False
    if _ATTACHMENT_RE.match(c.strip()):
        logger.debug("丢弃 SSE rawid=%s: 附件占位符", event.get("rawid"))
        return False
    return True


# ── Normalize REST → list[InternalMessage] ──


def normalize_rest(
    msg: WeFlowMessage,
    session_id: str,
    group_name: str,
    contacts: dict[str, str] | None = None,
) -> list[InternalMessage]:
    """REST 消息 → InternalMessage 列表（普通消息 1 条，文章卡片拆条）。

    显示名取上游 senderName（备注 > 昵称 > wxid），退化为 wxid 时让位
    contacts —— 私聊/公众号对端可能不在上游 contacts 集合，poller 用会话
    显示名回填了 contacts，那才是唯一名字来源。

    文章卡片解析失败（无 title）时返回空列表：REST 路径下非常规消息不进
    管道（丢弃语义）。图片消息（localType=3 且带 media.url）提取相对路径
    进 image_urls 供 OCR。
    """
    wxid = msg.get("senderUsername") or ""
    # IGNORE_SELF 判定：微信 DB 的 isSend 原义 0=收到 / 1=自己发送
    # （上游 SSE 推送端也据此不推自消息）。字段缺失 → 视为非自己（fail-open）。
    is_self = bool(msg.get("isSend"))
    upstream_name = clean_display_name(msg.get("senderName"))
    if upstream_name == wxid:
        upstream_name = ""
    # 取值后再清洗一道（幂等，防御调用方传入未清洗映射）：逐级净化后为空
    # 才回退 wxid，避免空显示名进入 raw_messages/items。
    display_name = (
        upstream_name
        or clean_display_name((contacts or {}).get(wxid))
        or wxid
        or "未知"
    )
    image_urls: list[str] = []
    timestamp = msg.get("createTime", 0)
    server_id = str(msg.get("serverId", ""))

    # media=1 导出时消息对象带 media.url（完整 URL，含查询串）
    media = msg.get("media")
    if media is not None and media.get("type") == "image" and media.get("url"):
        path = _extract_media_path(str(media["url"]))
        if path:
            image_urls.append(path)
        else:
            logger.debug("REST msg_id=%s: media.url 无法提取路径，跳过图片", server_id)

    # 文章卡片：weflow-server 的 content 为 [消息] 占位、rawContent 为原始
    # XML（media=1 也保留），直接解析 rawContent 拆条（无需回查）
    if msg.get("localType") == _APPMSG_LOCAL_TYPE:
        raw = msg.get("rawContent") or ""
        if not _is_appmsg_content(raw):
            raw = msg.get("content", "")
        articles = parse_appmsg_xml(raw)
        if articles:
            msgs = _article_messages(
                msg_id_base=server_id,
                articles=articles,
                sender_name=display_name,
                sender_id=wxid,
                session_id=session_id,
                group_name=group_name,
                timestamp=timestamp,
                is_self=is_self,
            )
            logger.debug(
                "REST msg_id=%s: appmsg 拆条 %d 条", server_id, len(msgs)
            )
            return msgs
        # 解析失败：占位符消息不放行（丢弃语义），与 weflow-legacy 一致
        logger.debug("REST msg_id=%s: appmsg 解析失败，丢弃", server_id)
        return []

    normalized = InternalMessage(
        msg_id=server_id,
        content=msg.get("content", ""),
        sender_name=display_name,
        sender_id=wxid,
        session_id=session_id,
        group_name=group_name,
        timestamp=timestamp,
        image_urls=image_urls,
        is_self=is_self,
    )
    logger.debug(
        "REST → msg_id=%s, sender=%s, len=%d, images=%d",
        normalized.msg_id,
        display_name,
        len(normalized.content),
        len(image_urls),
    )
    return [normalized]


# ── Pre-Filter ──


def pre_filter_rest(msg: WeFlowMessage) -> bool:
    local_type = msg.get("localType")
    # 文章卡片（公众号推送/转发文章）放行，交给 normalize_rest 拆条
    if local_type == _APPMSG_LOCAL_TYPE:
        return True
    # 图片消息（localType=3）有 media.url 时放行，交给 OCR
    if local_type == 3:
        media = msg.get("media")
        if media is not None and media.get("type") == "image" and media.get("url"):
            return True
        logger.debug(
            "丢弃 REST msg_id=%s: 图片但无 media.url",
            msg.get("serverId"),
        )
        return False
    if local_type != 1:
        logger.debug(
            "丢弃 REST msg_id=%s: localType=%s（非文本/非文章卡片/非带媒体图片）",
            msg.get("serverId"),
            local_type,
        )
        return False
    content: str = msg.get("content", "")
    if not content or content == "[消息]" or content.strip() == "":
        logger.debug("丢弃 REST msg_id=%s: 空内容/[消息]", msg.get("serverId"))
        return False
    if len(content.strip()) < 5:
        logger.debug("丢弃 REST msg_id=%s: 内容过短", msg.get("serverId"))
        return False
    return True
