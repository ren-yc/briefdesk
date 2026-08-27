"""IGNORE_SELF 自消息识别测试：normalize 层 is_self 盖章 + qqflow SSE 回查。

weflow-legacy REST 按 isSend 判定（缺失 fail-open）；qqflow REST 按自身 UID
（u_<QQFLOW_QQ>）判定；qqflow SSE 事件无发送者标识，按消息回查 REST。
管道入口的过滤行为见 tests/test_pipeline.py 的 IgnoreSelfFilterTest。
"""

import unittest
from unittest.mock import AsyncMock, Mock, patch

from briefdesk.config import config
from briefdesk.plugins.qqflow.client import QqFlowClient
from briefdesk.plugins.qqflow.normalize import is_self_message
from briefdesk.plugins.qqflow.normalize import normalize_rest as qq_normalize_rest
from briefdesk.plugins.qqflow.poller import poll as qq_poll
from briefdesk.plugins.qqflow.sse import QqFlowSseClient
from briefdesk.plugins.weflow_legacy.normalize import _APPMSG_LOCAL_TYPE, normalize_rest
from briefdesk.plugins.weflow_legacy.poller import poll as we_poll
from briefdesk.types import SessionInfo

_MULTI_XML = """<msg>
    <appmsg appid="" sdkver="0">
        <mmreader>
            <category type="20" count="2">
                <item>
                    <title><![CDATA[标题A]]></title>
                    <url><![CDATA[http://mp.weixin.qq.com/s?a#rd]]></url>
                </item>
                <item>
                    <title><![CDATA[标题B]]></title>
                    <url><![CDATA[http://mp.weixin.qq.com/s?b#rd]]></url>
                </item>
            </category>
        </mmreader>
    </appmsg>
</msg>"""


class WeflowRestSelfDetectionTest(unittest.TestCase):
    """weflow-legacy REST：isSend=1 → is_self；0/缺失 → 非自己（fail-open）。"""

    def _msg(self, **over):
        base = {
            "serverId": "1001",
            "content": "测试消息内容",
            "createTime": 123456,
            "localType": 1,
            "senderUsername": "wxid_other",
        }
        base.update(over)
        return base

    def test_is_send_1_marks_self(self):
        msgs = normalize_rest(self._msg(isSend=1), "s1", "g", {}, {})
        self.assertEqual(len(msgs), 1)
        self.assertTrue(msgs[0].is_self)

    def test_is_send_0_not_self(self):
        msgs = normalize_rest(self._msg(isSend=0), "s1", "g", {}, {})
        self.assertEqual(len(msgs), 1)
        self.assertFalse(msgs[0].is_self)

    def test_missing_is_send_fails_open(self):
        msgs = normalize_rest(self._msg(), "s1", "g", {}, {})
        self.assertFalse(msgs[0].is_self)

    def test_article_card_splits_propagate_self(self):
        # 自己转发的文章卡片：拆条全部继承 is_self
        msgs = normalize_rest(
            self._msg(
                isSend=1, localType=_APPMSG_LOCAL_TYPE, content=_MULTI_XML
            ),
            "s1",
            "g",
            {},
            {},
        )
        self.assertEqual(len(msgs), 2)
        self.assertTrue(all(m.is_self for m in msgs))

    def test_article_card_not_self_kept(self):
        msgs = normalize_rest(
            self._msg(
                isSend=0, localType=_APPMSG_LOCAL_TYPE, content=_MULTI_XML
            ),
            "s1",
            "g",
            {},
            {},
        )
        self.assertEqual(len(msgs), 2)
        self.assertTrue(all(not m.is_self for m in msgs))


class QqflowRestSelfDetectionTest(unittest.TestCase):
    """qqflow REST：senderUsername == u_<QQ> → is_self；isSend 兜底。"""

    def _msg(self, **over):
        base = {
            "localId": 42,
            "serverId": "seq1",
            "localType": 0,
            "createTime": 123456,
            "isSend": 0,
            "senderUsername": "u_12345678",
            "content": "测试消息内容",
        }
        base.update(over)
        return base

    def test_self_uid_match_marks_self(self):
        m = qq_normalize_rest(self._msg(), "s1", "g", {}, self_uid="u_12345678")
        self.assertTrue(m.is_self)

    def test_other_uid_not_self(self):
        m = qq_normalize_rest(
            self._msg(senderUsername="u_9999999999"),
            "s1",
            "g",
            {},
            self_uid="u_12345678",
        )
        self.assertFalse(m.is_self)

    def test_empty_self_uid_fails_open(self):
        m = qq_normalize_rest(self._msg(), "s1", "g", {}, self_uid="")
        self.assertFalse(m.is_self)

    def test_is_send_future_proof(self):
        # 上游未来版本提供方向（isSend=1）时即使 UID 不匹配也判为自己
        m = qq_normalize_rest(
            self._msg(isSend=1, senderUsername="u_other"),
            "s1",
            "g",
            {},
            self_uid="u_12345678",
        )
        self.assertTrue(m.is_self)

    def test_self_uid_matches_qq_config(self):
        self.assertEqual(
            QqFlowClient(
                base_url="http://x", api_token="t", qq="12345678", key="k"
            ).self_uid,
            "u_12345678",
        )
        self.assertEqual(
            QqFlowClient(base_url="http://x", api_token="t", qq="", key="k").self_uid,
            "",
        )


class IsSelfMessagePredicateTest(unittest.TestCase):
    """is_self_message 谓词（normalize_rest 与 SSE 回查共用）。"""

    def _msg(self, **over):
        base = {
            "localId": 1,
            "serverId": "s",
            "localType": 0,
            "createTime": 1,
            "isSend": 0,
            "senderUsername": "u_123",
            "content": "内容",
        }
        base.update(over)
        return base

    def test_uid_match(self):
        self.assertTrue(is_self_message(self._msg(), "u_123"))
        self.assertFalse(is_self_message(self._msg(senderUsername="u_0"), "u_123"))

    def test_is_send_short_circuits(self):
        self.assertTrue(is_self_message(self._msg(isSend=1), ""))
        self.assertFalse(is_self_message(self._msg(), ""))  # 无 self_uid → 不误杀


class QqflowSseLookbackTest(unittest.IsolatedAsyncioTestCase):
    """qqflow SSE 实时路径：IGNORE_SELF 开启时按消息回查 REST 判定。"""

    async def _handle(self, lookback_result, lookback_error=None):
        captured: list = []

        async def on_batch(batch):
            captured.append(batch)

        client = Mock()
        client.name = "qqflow"
        client.self_uid = "u_12345"
        if lookback_error is not None:
            client.lookup_message = AsyncMock(side_effect=lookback_error)
        else:
            client.lookup_message = AsyncMock(return_value=lookback_result)
        listener = QqFlowSseClient(client, on_batch, settings=Mock())
        event = {
            "event": "message.new",
            "sessionId": "g1",
            "sessionType": "group",
            "groupName": "群",
            "rawid": "42",
            "sourceName": "自己",
            "content": "测试消息内容",
            "timestamp": 1000,
        }
        # 攒批参数调大：消息留在缓冲区内，便于断言（不触发异步 flush）
        with patch.object(config, "ignore_self", True), patch.object(
            config, "realtime_batch_max_count", 10
        ):
            await listener._handle_event(event)
        return listener, client, captured

    async def test_self_message_marked_by_lookback(self):
        listener, client, _ = await self._handle(
            {
                "localId": 42,
                "serverId": "seq",
                "localType": 0,
                "createTime": 1000,
                "isSend": 0,
                "senderUsername": "u_12345",
                "content": "测试消息内容",
            }
        )
        client.lookup_message.assert_awaited_once_with("g1", "42", 1000)
        # 自消息在监听器层直接丢弃（不进攒批缓冲），独立计入统计
        self.assertEqual(listener._batch_buffer._buffer, [])
        self.assertEqual(listener._stats_self, 1)

    async def test_lookback_miss_fails_open(self):
        listener, client, _ = await self._handle(None)
        client.lookup_message.assert_awaited_once()
        buf = listener._batch_buffer._buffer
        self.assertEqual(len(buf), 1)
        self.assertFalse(buf[0].is_self, "回查未命中 → 按非自己放行")

    async def test_lookback_error_fails_open(self):
        listener, client, _ = await self._handle(None, lookback_error=RuntimeError("503"))
        client.lookup_message.assert_awaited_once()
        buf = listener._batch_buffer._buffer
        self.assertEqual(len(buf), 1)
        self.assertFalse(buf[0].is_self, "回查异常 → 按非自己放行，不拖垮监听")

    async def test_disabled_skips_lookback(self):
        captured: list = []

        async def on_batch(batch):
            captured.append(batch)

        client = Mock()
        client.name = "qqflow"
        client.lookup_message = AsyncMock()
        listener = QqFlowSseClient(client, on_batch, settings=Mock())
        event = {
            "event": "message.new",
            "sessionId": "g1",
            "sessionType": "group",
            "groupName": "群",
            "rawid": "42",
            "sourceName": "自己",
            "content": "测试消息内容",
            "timestamp": 1000,
        }
        with patch.object(config, "ignore_self", False), patch.object(
            config, "realtime_batch_max_count", 10
        ):
            await listener._handle_event(event)
        client.lookup_message.assert_not_called()
        self.assertFalse(listener._batch_buffer._buffer[0].is_self)


class WeflowPollerSelfDropTest(unittest.IsolatedAsyncioTestCase):
    """weflow-legacy 回填：IGNORE_SELF 开启时 isSend=1 消息在 poller 预滤，不进管道。"""

    class _Client:
        name = "weflow-legacy"

        def __init__(self, messages):
            self._messages = messages

        async def fetch_contacts(self):
            return {}

        async def fetch_sessions(self):
            return [{"id": "g1", "name": "群", "type": "group"}]

        async def fetch_messages(
            self, talker, start_ts, limit=500, offset=0, media=False
        ):
            return {"messages": self._messages, "hasMore": False}

        async def fetch_group_members(self, chatroom_id):
            return {}

    def _enabled(self):
        return [
            SessionInfo(source="weflow-legacy", session_id="g1", name="群", is_group=True)
        ]

    @staticmethod
    async def _no_processed(ids):
        return set()

    def _msgs(self, now):
        return [
            {
                "serverId": "1",
                "localType": 1,
                "createTime": now,
                "senderUsername": "wxid_self",
                "content": "自己发送的消息内容",
                "isSend": 1,
            },
            {
                "serverId": "2",
                "localType": 1,
                "createTime": now,
                "senderUsername": "wxid_other",
                "content": "别人发送的消息内容",
                "isSend": 0,
            },
        ]

    async def test_ignore_self_on_drops_self_in_poller(self):
        import time

        with patch.object(config, "ignore_self", True):
            result = await we_poll(
                self._Client(self._msgs(int(time.time()))),
                self._enabled(),
                self._no_processed,
            )
        self.assertEqual([m.msg_id for m in result.messages], ["2"], "自消息不进管道")

    async def test_ignore_self_off_keeps_self_flagged(self):
        import time

        with patch.object(config, "ignore_self", False):
            result = await we_poll(
                self._Client(self._msgs(int(time.time()))),
                self._enabled(),
                self._no_processed,
            )
        self.assertEqual([m.msg_id for m in result.messages], ["1", "2"])
        self.assertTrue(result.messages[0].is_self)
        self.assertFalse(result.messages[1].is_self)


class QqflowPollerSelfDropTest(unittest.IsolatedAsyncioTestCase):
    """qqflow 回填：IGNORE_SELF 开启时自身 UID 消息在 poller 预滤。"""

    class _Client:
        name = "qqflow"
        self_uid = "u_12345"

        def __init__(self, messages):
            self._messages = messages

        async def ensure_ready(self):
            pass

        async def fetch_contacts(self):
            return {}

        async def fetch_sessions(self):
            return [{"username": "g1", "displayName": "群", "type": 2}]

        async def fetch_messages(self, talker, start=None, limit=500, offset=0):
            return {"messages": self._messages, "hasMore": False}

    def _enabled(self):
        return [
            SessionInfo(source="qqflow", session_id="g1", name="群", is_group=True)
        ]

    @staticmethod
    async def _no_processed(ids):
        return set()

    def _msgs(self, now):
        return [
            {
                "localId": 1,
                "localType": 0,
                "createTime": now,
                "senderUsername": "u_12345",
                "content": "自己发送的消息内容",
                "isSend": 0,
            },
            {
                "localId": 2,
                "localType": 0,
                "createTime": now,
                "senderUsername": "u_99999",
                "content": "别人发送的消息内容",
                "isSend": 0,
            },
        ]

    async def test_ignore_self_on_drops_self_in_poller(self):
        import time

        with patch.object(config, "ignore_self", True):
            result = await qq_poll(
                self._Client(self._msgs(int(time.time()))),
                self._enabled(),
                self._no_processed,
            )
        self.assertEqual([m.msg_id for m in result.messages], ["2"], "自消息不进管道")

    async def test_ignore_self_off_keeps_self_flagged(self):
        import time

        with patch.object(config, "ignore_self", False):
            result = await qq_poll(
                self._Client(self._msgs(int(time.time()))),
                self._enabled(),
                self._no_processed,
            )
        self.assertEqual([m.msg_id for m in result.messages], ["1", "2"])
        self.assertTrue(result.messages[0].is_self)
        self.assertFalse(result.messages[1].is_self)


class QqflowLookupMessageTest(unittest.IsolatedAsyncioTestCase):
    """QqFlowClient.lookup_message：按 localId + 时间窗口匹配。"""

    def _client(self):
        return QqFlowClient(
            base_url="http://127.0.0.1:5032",
            api_token="t",
            qq="12345",
            key="k",
        )

    async def test_matches_local_id_within_window(self):
        client = self._client()
        client.fetch_messages = AsyncMock(
            return_value={
                "messages": [
                    {"localId": 42, "createTime": 1000, "senderUsername": "u_12345"},
                    {"localId": 43, "createTime": 1010, "senderUsername": "u_1"},
                ]
            }
        )
        m = await client.lookup_message("g1", "42", 1005)
        self.assertIsNotNone(m)
        self.assertEqual(m["localId"], 42)
        client.fetch_messages.assert_awaited_once_with("g1", start=885, limit=200)

    async def test_out_of_window_not_matched(self):
        client = self._client()
        client.fetch_messages = AsyncMock(
            return_value={
                "messages": [{"localId": 42, "createTime": 1000}],
            }
        )
        m = await client.lookup_message("g1", "42", 5000)
        self.assertIsNone(m)


if __name__ == "__main__":
    unittest.main()
