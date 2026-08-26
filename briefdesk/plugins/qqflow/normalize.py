"""Message normalization and pre-filtering — qqflow-server 版（参照 weflow normalize.py）。

qqflow-server v1 差异：
- 媒体经 mediaId + GET /api/v1/media/{id} 获取：图片消息（content 为
  [image] / localType=3）在 mediaId 可获取时放行，image_urls 挂 mediaId 供
  OCR 与前端代理展示；语音/视频（[voice]/[video]/localType 4/5）无下游
  消费方，维持过滤
- SSE 事件自带 media 对象（无路径元数据视图，上游不下发 localPath）与
  mediaId 字段（仅当索引注册了可读取的本地缓存时携带，与 REST 同规则）
  → normalize_sse 为纯同步函数（weflow 的 async 是媒体回查的产物），
  image_urls 直接取事件 mediaId，无推导/回查需求
- msg_id 统一用 rowid：SSE rawid（字符串）与 REST localId（数字转 str）同值
"""

import logging
import re

from briefdesk.masking import clean_display_name
from briefdesk.plugins.qqflow.client import QqFlowEvent, QqFlowMessage
from briefdesk.types import InternalMessage

logger = logging.getLogger(__name__)

# 整条消息为单个方括号片段即视为附件占位符（[图片]/[image]/[语音]/[视频]...），
# 覆盖中英文且不依赖词表；带真实文本的消息（"xxx [图片]"）不受影响
_PLACEHOLDER_RE = re.compile(r"^\s*\[[^\]]+\]\s*$")

# 图片消息的 SSE 占位符（上游 parser/types.rs:46，媒体消息 display 用）
_IMAGE_PLACEHOLDER = "[image]"

# QQ 入群/名片等系统事件可能被上游编码为“纯 UID 文本 + 空发送者”，
# 这类消息没有信息价值，入口直接过滤（仅匹配整条消息就是 UID）。
_QQ_UID_ONLY_RE = re.compile(r"^u_[A-Za-z0-9_-]{16,64}$")

# QQ 图片/文件卡片的富媒体 XML 属性对（m_fileName + m_resid 成对出现）。
# 上游解析失败时以原始 XML 残片形式进入 content（实测形如
# `]" m_fileName="<uuid>" m_resid="<base64>" tSum="n" flag="3"><item ...>`），
# 无文本信息、无 mediaId 可 OCR，入口直接过滤。正常聊天文本不会携带
# 该属性对，规则零误伤；localType=3 的图片消息 content 为 [image] 不受影响。
_QQ_RICH_XML_RE = re.compile(r'm_fileName\s*=\s*"[^"]+"\s+m_resid\s*=\s*"[^"]+"')


def is_self_message(msg: QqFlowMessage, self_uid: str) -> bool:
    """判定消息是否本账号自己发送（IGNORE_SELF 识别谓词）。

    主判据为发送者 UID 等于自身账号 UID（QQ NT UID 约定：u_<QQ号>）；
    isSend 来自上游 40013 列（部分 QQ 版本缺列或值非 1/2 时恒 0），
    作为方向信息的优先兜底。self_uid 为空 → 仅按 isSend 兜底（不误杀）。
    """
    if bool(msg.get("isSend")):
        return True
    return bool(self_uid) and (msg.get("senderUsername") or "") == self_uid


# ── Normalize SSE → InternalMessage ──


def normalize_sse(event: QqFlowEvent) -> InternalMessage:
    """SSE 事件 → InternalMessage（纯同步：无媒体回查、无 contacts 回查）。"""
    is_group = event.get("sessionType") == "group"
    # 显示名净化必须发生在空值回退之前：sourceName/groupName 可能携带
    # 控制字符/纯空白脏数据，净化后为空才回退到会话 id / "未知"。
    sender_name = clean_display_name(event.get("sourceName")) or "未知"
    image_urls: list[str] = []
    # 图片消息：事件自带的 mediaId（上游仅在该媒体可读取时携带，与 REST 同
    # 规则），挂 image_urls 供 OCR 与前端代理展示；缺失即无字节可取
    if event.get("content", "").strip() == _IMAGE_PLACEHOLDER:
        media_id = event.get("mediaId")
        if media_id:
            image_urls.append(media_id)
    normalized = InternalMessage(
        msg_id=event["rawid"],
        content=event.get("content", ""),
        sender_name=sender_name,
        sender_id="",  # SSE 事件无 UID 字段
        session_id=event.get("sessionId", ""),
        group_name=(
            (clean_display_name(event.get("groupName")) or event["sessionId"])
            if is_group
            else sender_name
        ),
        timestamp=event.get("timestamp", 0),
        image_urls=image_urls,
    )
    logger.debug(
        "SSE → msg_id=%s, sender=%s, len=%d, images=%d",
        normalized.msg_id,
        sender_name,
        len(normalized.content),
        len(image_urls),
    )
    return normalized


def pre_filter_sse(event: QqFlowEvent) -> bool:
    """SSE 事件预过滤：仅放行 message.new。

    message.revoke（撤回）/ sync（基线水位，无消息载荷，pipeline 幂等已兜底）/
    ping（KeepAlive）一律拒绝；发送者为空/缺失、空/短内容、QQ 富媒体 XML 残片
    （m_fileName/m_resid）与占位符消息拒绝。
    """
    if event.get("event") != "message.new":
        logger.debug(
            "丢弃 SSE rawid=%s: 事件类型 %s",
            event.get("rawid"),
            event.get("event"),
        )
        return False
    # 任意发送者为空的消息均丢弃：上游可能把入群/名片等系统事件编码成
    # “无发送者 + 内容为显示名”的形式，这类消息没有可展示/可分类的信息价值。
    if not clean_display_name(event.get("sourceName")):
        logger.debug(
            "丢弃 SSE rawid=%s: 发送者为空",
            event.get("rawid"),
        )
        return False
    c: str = event.get("content", "")
    if not c:
        logger.debug("丢弃 SSE rawid=%s: 空内容", event.get("rawid"))
        return False
    # 入群/名片/撤回等系统事件：上游可能以“纯 UID 内容”呈现，
    # 没有可展示/可分类的信息价值，入口直接丢弃。
    if _QQ_UID_ONLY_RE.match(c.strip()):
        logger.debug(
            "丢弃 SSE rawid=%s: 纯 UID 内容",
            event.get("rawid"),
        )
        return False
    # QQ 富媒体 XML 残片（m_fileName/m_resid 属性对）：图片/文件卡片解析失败
    # 的原始 XML 尾部，无文本信息、无媒体可 OCR，入口直接丢弃。
    if _QQ_RICH_XML_RE.search(c):
        logger.debug(
            "丢弃 SSE rawid=%s: QQ 富媒体 XML 残片（m_fileName/m_resid）",
            event.get("rawid"),
        )
        return False
    # 图片消息：事件携带 mediaId（上游仅当索引注册了可读取的本地缓存时提供，
    # 与 REST messages.mediaId 同一规则，出现即保证可取）时放行，交由
    # normalize_sse 挂 image_urls 供 OCR；无 mediaId → /api/v1/media/{id} 必
    # 404，且占位符无信息价值，维持过滤。[voice]/[video] 不匹配精确占位符，
    # 照旧拒绝
    if c.strip() == _IMAGE_PLACEHOLDER:
        if event.get("mediaId"):
            return True
        logger.debug(
            "丢弃 SSE rawid=%s: 图片无 mediaId（服务端未注册可读取的本地缓存）",
            event.get("rawid"),
        )
        return False
    if len(c.strip()) < 5:
        logger.debug("丢弃 SSE rawid=%s: 内容过短", event.get("rawid"))
        return False
    if _PLACEHOLDER_RE.match(c.strip()):
        logger.debug("丢弃 SSE rawid=%s: 附件占位符", event.get("rawid"))
        return False
    return True


# ── Normalize REST → InternalMessage ──


def normalize_rest(
    msg: QqFlowMessage,
    session_id: str,
    group_name: str,
    contacts: dict[str, str] | None = None,
    group_members: dict[str, str] | None = None,
    self_uid: str = "",
) -> InternalMessage:
    uid = msg.get("senderUsername") or ""
    # IGNORE_SELF 判定：自身 UID 匹配（QQ NT UID 约定 u_<QQ号>），
    # isSend 为上游未来版本方向兜底；self_uid 为空时 fail-open
    is_self = is_self_message(msg, self_uid)
    # 群成员名（本群群名片等 per-session 名字）优先于全局联系人名；
    # 逐级净化后为空才回退 UID，避免空显示名进入 raw_messages/items。
    display_name = (
        clean_display_name((group_members or {}).get(uid))
        or clean_display_name((contacts or {}).get(uid))
        or uid
        or "未知"
    )
    image_urls: list[str] = []
    # 图片消息：mediaId 存在 ⟺ 上游索引注册了可解析的本地缓存文件
    # （with_fetchable_media_id 保证不 404），挂 image_urls 供 OCR 与前端展示
    media_id = msg.get("mediaId")
    if msg.get("localType") == 3 and media_id:
        image_urls.append(media_id)
    normalized = InternalMessage(
        # localId 为 rowid 数字，与 SSE rawid 同值 → 跨路径去重一致
        msg_id=str(msg["localId"]),
        content=msg.get("content", ""),
        sender_name=display_name,
        sender_id=uid,
        session_id=session_id,
        group_name=group_name,
        timestamp=msg.get("createTime", 0),
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
    return normalized


# ── Pre-Filter ──


def pre_filter_rest(msg: QqFlowMessage) -> bool:
    """REST 消息预过滤。

    拒绝：撤回（6）/ 系统消息（7）；发送者为空/缺失；空/短内容；附件占位符
    （4/5 语音视频无下游消费方，3 图片无 mediaId 时无媒体可 OCR）；QQ 富媒体
    XML 残片（m_fileName/m_resid 属性对，图片/文件卡片解析失败的原始 XML）。
    图片消息（localType=3）带 mediaId（上游保证可获取）时放行，交由
    normalize_rest 提取并 OCR。
    localType=1（"其他"）不直接拒绝——其 content 为解析后文本，可能含
    有效信息，由内容规则（长度 + 占位符）决定去留，保召回。
    """
    local_type = msg.get("localType")
    if local_type in (6, 7):
        logger.debug(
            "丢弃 REST msg_id=%s: localType=%s（撤回/系统消息）",
            msg.get("localId"),
            local_type,
        )
        return False
    # 任意发送者为空的消息均丢弃：上游可能把入群/名片等系统事件编码成
    # “无发送者 + 内容为显示名”的形式，这类消息没有可展示/可分类的信息价值。
    if not clean_display_name(msg.get("senderUsername")):
        logger.debug(
            "丢弃 REST msg_id=%s: 发送者为空 (localType=%s)",
            msg.get("localId"),
            local_type,
        )
        return False
    # 入群/名片/撤回等系统事件：上游可能以“纯 UID 内容”呈现，
    # 没有可展示/可分类的信息价值，入口直接丢弃。
    c: str = msg.get("content") or ""
    if _QQ_UID_ONLY_RE.match(c.strip()):
        logger.debug(
            "丢弃 REST msg_id=%s: 纯 UID 内容 (localType=%s)",
            msg.get("localId"),
            local_type,
        )
        return False
    # QQ 富媒体 XML 残片（m_fileName/m_resid 属性对）：图片/文件卡片解析失败
    # 的原始 XML 尾部，无文本信息、无媒体可 OCR，入口直接丢弃。
    if _QQ_RICH_XML_RE.search(c):
        logger.debug(
            "丢弃 REST msg_id=%s: QQ 富媒体 XML 残片（m_fileName/m_resid）",
            msg.get("localId"),
        )
        return False
    # 图片消息：mediaId 存在 ⟺ 上游索引注册了可解析的本地缓存文件
    if local_type == 3 and msg.get("mediaId"):
        return True
    if not c.strip() or len(c.strip()) < 5:
        logger.debug(
            "丢弃 REST msg_id=%s: 空/过短内容 (localType=%s)",
            msg.get("localId"),
            local_type,
        )
        return False
    if _PLACEHOLDER_RE.match(c.strip()):
        logger.debug(
            "丢弃 REST msg_id=%s: 附件占位符 (localType=%s)",
            msg.get("localId"),
            local_type,
        )
        return False
    return True
