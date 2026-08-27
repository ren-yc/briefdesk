"""weflow-legacy 公众号文章卡片（appmsg XML）解析与拆条测试。

样本为上海发布真实推送的简化形态：多图文 mmreader 含 2 篇 item；
另覆盖单图文退化、title_v2 回退、解析失败兜底与预过滤放行。
"""

import unittest

from briefdesk.plugins.weflow_legacy.normalize import (
    _APPMSG_LOCAL_TYPE,
    normalize_rest,
    normalize_sse,
    parse_appmsg_xml,
    pre_filter_rest,
)
from briefdesk.plugins.weflow_legacy.poller import poll
from briefdesk.types import SessionInfo

_MULTI_XML = """<msg>
    <appmsg appid="" sdkver="0">
        <title><![CDATA[头条标题]]></title>
        <des><![CDATA[]]></des>
        <type>5</type>
        <url><![CDATA[http://mp.weixin.qq.com/s?__biz=TOP&idx=1#rd]]></url>
        <mmreader>
            <category type="20" count="2">
                <name><![CDATA[上海发布]]></name>
                <item>
                    <title><![CDATA[从上海书展出发，探索阅读嵌入沪苏浙皖城市日常的N种可能]]></title>
                    <url><![CDATA[http://mp.weixin.qq.com/s?__biz=MjM5NTA5NzYyMA==&mid=1&idx=1&sn=a#rd]]></url>
                    <summary><![CDATA[上海书展正点亮全城阅读热情，而沪苏浙皖的书香早已突破展馆边界。]]></summary>
                    <fileid>507978729</fileid>
                </item>
                <item>
                    <title><![CDATA[]]></title>
                    <title_v2><![CDATA[【交通】沪苏嘉城际铁路建设又有新进展]]></title_v2>
                    <url><![CDATA[http://mp.weixin.qq.com/s?__biz=MjM5NTA5NzYyMA==&mid=1&idx=2&sn=b#rd]]></url>
                    <summary><![CDATA[]]></summary>
                </item>
                <item>
                    <text_title><![CDATA[视频占位]]></text_title>
                    <play_url><![CDATA[xxx]]></play_url>
                </item>
            </category>
            <publisher>
                <username><![CDATA[gh_27278ac0a645]]></username>
                <nickname><![CDATA[上海发布]]></nickname>
            </publisher>
        </mmreader>
    </appmsg>
    <fromusername><![CDATA[gh_27278ac0a645]]></fromusername>
</msg>"""

_SINGLE_XML = """<msg>
    <appmsg appid="" sdkver="0">
        <title><![CDATA[单图文标题]]></title>
        <des><![CDATA[单图文摘要]]></des>
        <type>5</type>
        <url><![CDATA[http://mp.weixin.qq.com/s?single#rd]]></url>
        <thumburl><![CDATA[https://mmbiz.qpic.cn/x]]></thumburl>
    </appmsg>
</msg>"""

_PLAIN_SINGLE_XML = """<msg>
    <appmsg appid="" sdkver="0">
        <title>最后9天！LMCC报名即将截止（集训4天后开营）</title>
        <des>⚠️重点提醒2026 LMCC青少年组第一轮认证报名将于8月25日17:00截止</des>
        <type>5</type>
        <url>http://mp.weixin.qq.com/s?__biz=MzkxOTE4NjQ2OA==&amp;mid=2247572663&amp;idx=1#rd</url>
    </appmsg>
</msg>"""


class ParseAppmsgXmlTest(unittest.TestCase):
    def test_multi_item_parses_each_article(self):
        articles = parse_appmsg_xml(_MULTI_XML)
        # 第 3 个 item 只有 text_title（视频占位）→ 跳过
        self.assertEqual(len(articles), 2)
        self.assertEqual(
            articles[0]["title"], "从上海书展出发，探索阅读嵌入沪苏浙皖城市日常的N种可能"
        )
        self.assertIn("上海书展正点亮全城阅读热情", articles[0]["summary"])
        self.assertIn("mp.weixin.qq.com", articles[0]["url"])
        self.assertEqual(articles[1]["title"], "【交通】沪苏嘉城际铁路建设又有新进展")
        self.assertEqual(articles[1]["summary"], "")
        self.assertIn("idx=2", articles[1]["url"])

    def test_single_article_fallback(self):
        articles = parse_appmsg_xml(_SINGLE_XML)
        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0]["title"], "单图文标题")
        self.assertEqual(articles[0]["summary"], "单图文摘要")
        self.assertIn("single#rd", articles[0]["url"])

    def test_plain_text_single_article_fallback(self):
        articles = parse_appmsg_xml(_PLAIN_SINGLE_XML)
        self.assertEqual(len(articles), 1)
        self.assertEqual(
            articles[0]["title"],
            "最后9天！LMCC报名即将截止（集训4天后开营）",
        )
        self.assertIn("2026 LMCC", articles[0]["summary"])
        self.assertIn("&mid=2247572663", articles[0]["url"])
        self.assertNotIn("&amp;", articles[0]["url"])

    def test_unparseable_returns_empty(self):
        self.assertEqual(parse_appmsg_xml("<msg><appmsg><type>5</type></appmsg></msg>"), [])
        self.assertEqual(parse_appmsg_xml("不是 XML"), [])


class PreFilterRestTest(unittest.TestCase):
    def test_appmsg_card_allowed(self):
        self.assertTrue(pre_filter_rest({"localType": _APPMSG_LOCAL_TYPE, "content": _MULTI_XML}))

    def test_other_local_types_still_dropped(self):
        self.assertFalse(pre_filter_rest({"localType": 49, "content": "hello world"}))
        self.assertTrue(pre_filter_rest({"localType": 1, "content": "hello world"}))
        self.assertFalse(pre_filter_rest({"localType": 1, "content": "hi"}))


class NormalizeRestAppmsgTest(unittest.TestCase):
    def _msg(self) -> dict:
        return {
            "serverId": "8728588931173115719",
            "content": _MULTI_XML,
            "localType": _APPMSG_LOCAL_TYPE,
            "createTime": 1786785950,
            "senderUsername": "gh_27278ac0a645",
        }

    def test_splits_into_numbered_messages(self):
        msgs = normalize_rest(self._msg(), "gh_27278ac0a645", "上海发布", {})
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[0].msg_id, "8728588931173115719_1")
        self.assertEqual(msgs[1].msg_id, "8728588931173115719_2")
        self.assertTrue(msgs[0].content.startswith("标题："))
        self.assertIn("摘要：", msgs[0].content)
        self.assertNotIn("摘要：", msgs[1].content)  # 空摘要不占行
        self.assertIn("mp.weixin.qq.com", msgs[0].article_url)
        self.assertEqual(msgs[0].group_name, "上海发布")

    def test_plain_text_single_article_normalizes(self):
        msg = self._msg()
        msg["content"] = _PLAIN_SINGLE_XML
        msgs = normalize_rest(msg, "g", "G", {})
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0].msg_id, "8728588931173115719_1")
        self.assertIn("LMCC", msgs[0].content)
        self.assertIn("&mid=2247572663", msgs[0].article_url)
        self.assertNotIn("&amp;", msgs[0].article_url)

    def test_unparseable_returns_empty(self):
        msg = self._msg()
        msg["content"] = "<msg><appmsg><type>5</type></appmsg></msg>"
        self.assertEqual(normalize_rest(msg, "g", "G", {}), [])

    def test_plain_text_still_single_message(self):
        msg = self._msg()
        msg["localType"] = 1
        msg["content"] = "hello world"
        msgs = normalize_rest(msg, "g", "G", {})
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0].msg_id, "8728588931173115719")
        self.assertEqual(msgs[0].article_url, "")


class NormalizeSseAppmsgTest(unittest.IsolatedAsyncioTestCase):
    async def test_sse_appmsg_splits_by_content_shape(self):
        msgs = await normalize_sse(
            {
                "event": "message.new",
                "rawid": "r1",
                "sessionId": "gh_27278ac0a645",
                "sourceName": "上海发布",
                "content": _MULTI_XML,
                "timestamp": 1786785950,
            }
        )
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[0].msg_id, "r1_1")
        self.assertEqual(msgs[1].msg_id, "r1_2")
        self.assertEqual(msgs[0].group_name, "上海发布")

    async def test_sse_unparseable_appmsg_keeps_raw_single(self):
        msgs = await normalize_sse(
            {
                "event": "message.new",
                "rawid": "r1",
                "sessionId": "g",
                "sourceName": "张三",
                "content": "<msg><appmsg><type>5</type></appmsg></msg>",
                "timestamp": 1,
            }
        )
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0].msg_id, "r1")
        self.assertIn("<appmsg>", msgs[0].content)  # 原样放行兜底

    async def test_sse_plain_text_still_single(self):
        msgs = await normalize_sse(
            {
                "event": "message.new",
                "rawid": "r1",
                "sessionId": "g",
                "sourceName": "张三",
                "content": "你好世界",
                "timestamp": 1,
            }
        )
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0].msg_id, "r1")


class _PlaceholderClient:
    """模拟 media=True 回填：文章消息 content 为占位符，回查才返回 XML。"""

    name = "weflow-legacy"

    def __init__(self, placeholder: dict, raw_xml: dict):
        self._placeholder = placeholder
        self._raw = raw_xml
        self.lookups: list[tuple[str, str, int]] = []

    async def fetch_contacts(self) -> dict[str, str]:
        return {}

    async def fetch_sessions(self) -> list[dict]:
        return [{"id": "gh_x", "name": "上海发布", "type": "channel"}]

    async def fetch_messages(self, *_args, **_kwargs) -> dict:
        return {"messages": [self._placeholder], "hasMore": False}

    async def fetch_group_members(self, _chatroom_id: str) -> dict[str, str]:
        return {}

    async def fetch_message_raw(self, talker: str, rawid: str, ts: int):
        self.lookups.append((talker, rawid, ts))
        return self._raw


class PollerPlaceholderLookbackTest(unittest.IsolatedAsyncioTestCase):
    async def test_placeholder_looked_back_and_parsed(self):
        """media=True 占位符（[视频号]…）→ fetch_message_raw 回查 XML → 拆条。"""
        import time

        placeholder = {
            "serverId": "s1",
            "localType": _APPMSG_LOCAL_TYPE,
            "createTime": int(time.time()),
            "senderUsername": "gh_x",
            "content": "[视频号] 从上海书展出发，探索阅读嵌入沪苏浙皖城市日常的N种可能",
        }
        raw = {**placeholder, "content": _MULTI_XML}
        client = _PlaceholderClient(placeholder, raw)
        enabled = [
            SessionInfo(
                source="weflow-legacy", session_id="gh_x", name="上海发布",
                is_group=False, is_official=True,
            )
        ]

        async def no_processed(ids):
            return set()

        result = await poll(client, enabled, no_processed)
        self.assertEqual(len(client.lookups), 1, "占位符应触发一次回查")
        self.assertEqual(len(result.messages), 2, "回查后按 XML 拆成 2 条")
        self.assertEqual(result.messages[0].msg_id, "s1_1")
        self.assertIn("mp.weixin.qq.com", result.messages[0].article_url)

    async def test_real_xml_skips_lookback(self):
        """content 已是 XML 时不触发回查（避免多余请求）。"""
        import time

        msg = {
            "serverId": "s1",
            "localType": _APPMSG_LOCAL_TYPE,
            "createTime": int(time.time()),
            "senderUsername": "gh_x",
            "content": _MULTI_XML,
        }
        client = _PlaceholderClient(msg, msg)
        enabled = [
            SessionInfo(
                source="weflow-legacy", session_id="gh_x", name="上海发布",
                is_group=False, is_official=True,
            )
        ]

        async def no_processed(ids):
            return set()

        result = await poll(client, enabled, no_processed)
        self.assertEqual(len(client.lookups), 0, "XML 内容不应回查")
        self.assertEqual(len(result.messages), 2)

    @staticmethod
    def _processed_querier(full_set):
        async def query(ids):
            return {i for i in ids if i in full_set}
        return query

    def _enabled(self):
        return [
            SessionInfo(
                source="weflow-legacy", session_id="gh_x", name="上海发布",
                is_group=False, is_official=True,
            )
        ]

    async def test_processed_placeholder_article_skipped_after_lookback(self):
        """占位符文章拆条全部已处理：回查 XML 后按拆条粒度判定已处理，不再产出。"""
        import time

        placeholder = {
            "serverId": "s1",
            "localType": _APPMSG_LOCAL_TYPE,
            "createTime": int(time.time()),
            "senderUsername": "gh_x",
            "content": "[视频号] 从上海书展出发",
        }
        raw = {**placeholder, "content": _MULTI_XML}
        client = _PlaceholderClient(placeholder, raw)
        result = await poll(
            client, self._enabled(), self._processed_querier({"s1_1", "s1_2"})
        )
        self.assertEqual(len(client.lookups), 1, "占位符仍需回查以确定拆条数")
        self.assertEqual(len(result.messages), 0, "拆条全部已处理 → 不产出")

    async def test_processed_xml_article_skipped_without_lookback(self):
        """XML 文章拆条全部已处理：批量查询即命中，无需回查。"""
        import time

        msg = {
            "serverId": "s1",
            "localType": _APPMSG_LOCAL_TYPE,
            "createTime": int(time.time()),
            "senderUsername": "gh_x",
            "content": _MULTI_XML,
        }
        client = _PlaceholderClient(msg, msg)
        result = await poll(
            client, self._enabled(), self._processed_querier({"s1_1", "s1_2"})
        )
        self.assertEqual(len(client.lookups), 0)
        self.assertEqual(len(result.messages), 0)

    async def test_partially_processed_article_kept_as_candidate(self):
        """仅部分拆条已处理：整条保留为候选（pipeline 入口按拆条过滤已处理部分）。"""
        import time

        msg = {
            "serverId": "s1",
            "localType": _APPMSG_LOCAL_TYPE,
            "createTime": int(time.time()),
            "senderUsername": "gh_x",
            "content": _MULTI_XML,
        }
        client = _PlaceholderClient(msg, msg)
        result = await poll(
            client, self._enabled(), self._processed_querier({"s1_1"})
        )
        self.assertEqual(len(client.lookups), 0)
        self.assertEqual(len(result.messages), 2, "部分处理仍产出全部拆条，由 pipeline 过滤")


if __name__ == "__main__":
    unittest.main()
