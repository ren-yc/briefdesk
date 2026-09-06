"""公众号文章卡片（appmsg XML）解析与拆条 — weflow / weflow-legacy 共享。

两个源的文章卡片 XML 形态完全一致（微信公众号推送与群聊转发同格式），
解析与拆条逻辑逐字相同，收敛到此单一权威点；任何改动两源同步生效。

消费方：weflow/normalize.py 与 weflow_legacy/normalize.py（各自 re-export）、
weflow/poller.py 与 weflow_legacy/poller.py（经 normalize 间接导入）。
"""

import html
import re
from typing import TypedDict

from briefdesk.types import InternalMessage


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
