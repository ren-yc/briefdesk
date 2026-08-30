"""qqflow 发送者显示名处理测试。

覆盖 normalize_rest 对上游 senderName 的采用与净化/退化兜底，
以及 poller 内非好友群成员的显示名解析。
"""

import time
import unittest
from unittest.mock import AsyncMock

from briefdesk.plugins.qqflow.client import QqFlowClient, QqFlowNotReadyError
from briefdesk.plugins.qqflow.config import QqFlowSettings
from briefdesk.plugins.qqflow.normalize import (
    normalize_rest,
    normalize_sse,
    pre_filter_rest,
    pre_filter_sse,
)
from briefdesk.plugins.qqflow.poller import poll
from briefdesk.plugins.qqflow.runtime import QqFlowSource
from briefdesk.plugins.qqflow.sse import QqFlowSseClient
from briefdesk.types import SessionInfo


class NormalizeRestDisplayNameTest(unittest.TestCase):
    def _msg(self, uid: str, sender_name: str | None = None) -> dict:
        msg = {
            "localId": 1,
            "content": "hello world",
            "localType": 0,
            "createTime": 123,
            "senderUsername": uid,
        }
        if sender_name is not None:
            msg["senderName"] = sender_name
        return msg

    def test_upstream_sender_name_wins_over_contact(self):
        """群名片是 per-conversation 的，全局 contacts 表达不了 → 必须优先。"""
        msg = normalize_rest(
            self._msg("u_a", "群名片"),
            "10001",
            "项目群",
            {"u_a": "全局备注名"},
        )
        self.assertEqual(msg.sender_name, "群名片")
        self.assertEqual(msg.sender_id, "u_a")

    def test_dirty_sender_name_falls_back_to_contact(self):
        msg = normalize_rest(
            self._msg("u_a", "\x01\x01"),
            "10001",
            "项目群",
            {"u_a": "全局备注名"},
        )
        self.assertEqual(msg.sender_name, "全局备注名")

    def test_uid_valued_sender_name_falls_back_to_contact(self):
        """上游名字链全退化时 senderName 即 UID，应让位于 contacts。"""
        msg = normalize_rest(
            self._msg("u_a", "u_a"),
            "10001",
            "项目群",
            {"u_a": "全局备注名"},
        )
        self.assertEqual(msg.sender_name, "全局备注名")

    def test_absent_sender_name_falls_back_to_contact(self):
        """旧上游无该字段（版本偏斜兜底）。"""
        msg = normalize_rest(self._msg("u_a"), "10001", "项目群", {"u_a": "全局备注名"})
        self.assertEqual(msg.sender_name, "全局备注名")

    def test_missing_names_fall_back_to_uid(self):
        msg = normalize_rest(self._msg("u_a"), "10001", "项目群", {})
        self.assertEqual(msg.sender_name, "u_a")


class NormalizeSseDisplayNameTest(unittest.TestCase):
    def test_dirty_source_name_falls_back_to_unknown(self):
        msg = normalize_sse(
            {
                "event": "message.new",
                "rawid": "1",
                "sessionId": "u_peer",
                "sessionType": "private",
                "sourceName": "\x01\x01",
                "content": "hello world",
                "timestamp": 123,
            }
        )
        self.assertEqual(msg.sender_name, "未知")
        self.assertEqual(msg.group_name, "未知")

    def test_dirty_group_name_falls_back_to_session_id(self):
        msg = normalize_sse(
            {
                "event": "message.new",
                "rawid": "1",
                "sessionId": "10001",
                "sessionType": "group",
                "sourceName": "\x01李四",
                "groupName": "\x02\x02",
                "content": "hello world",
                "timestamp": 123,
            }
        )
        self.assertEqual(msg.sender_name, "李四")
        self.assertEqual(msg.group_name, "10001")


class NormalizeSseMediaTest(unittest.TestCase):
    """SSE 图片消息的 image_urls 提取（mediaId 判据，与 REST 同规则）。"""

    def _event(self, content: str = "[image]", **extra: object) -> dict:
        return {
            "event": "message.new",
            "rawid": "1",
            "sessionId": "10001",
            "sessionType": "group",
            "sourceName": "张三",
            "content": content,
            "timestamp": 123,
            **extra,
        }

    def test_image_with_media_id_sets_image_urls(self):
        msg = normalize_sse(
            self._event(mediaId="9f2a1c2d3e4f5a6b7c8d9e0f1a2b3c4d")
        )
        self.assertEqual(msg.image_urls, ["9f2a1c2d3e4f5a6b7c8d9e0f1a2b3c4d"])

    def test_image_without_media_id_leaves_image_urls_empty(self):
        # 上游推送的 media 为无路径视图，媒体是否可取只看 mediaId；
        # 缺失时无字节可取，image_urls 保持空（消息经 pre_filter 丢弃）
        msg = normalize_sse(self._event(media={"uuid": "R020-x"}))
        self.assertEqual(msg.image_urls, [])

    def test_text_with_media_id_ignored(self):
        # mediaId 仅对图片占位符生效；带真实文本的消息不受影响
        msg = normalize_sse(self._event(content="hello world", mediaId="abc"))
        self.assertEqual(msg.image_urls, [])


class PrefilterSenderTest(unittest.TestCase):
    """空发送者消息（含纯 UID/显示名内容的系统事件）应在入口被过滤。"""

    def test_sse_filters_empty_sender_with_uid_content(self):
        event = {
            "event": "message.new",
            "rawid": "1",
            "sessionId": "10001",
            "sessionType": "group",
            "sourceName": "",
            "content": "u_2XCtJBaCE1zEEUqL2h67Ng",
            "timestamp": 123,
        }
        self.assertFalse(pre_filter_sse(event))

    def test_sse_filters_uid_content_with_sender_name(self):
        event = {
            "event": "message.new",
            "rawid": "1",
            "sessionId": "10001",
            "sessionType": "group",
            "sourceName": "张三",
            "content": "u_2XCtJBaCE1zEEUqL2h67Ng",
            "timestamp": 123,
        }
        self.assertFalse(pre_filter_sse(event))

    def test_sse_filters_normal_text_with_empty_sender(self):
        event = {
            "event": "message.new",
            "rawid": "1",
            "sessionId": "10001",
            "sessionType": "group",
            "sourceName": "",
            "content": "hello world",
            "timestamp": 123,
        }
        self.assertFalse(pre_filter_sse(event))

    def test_sse_filters_control_only_sender_with_normal_text(self):
        event = {
            "event": "message.new",
            "rawid": "1",
            "sessionId": "10001",
            "sessionType": "group",
            "sourceName": "\x01\x01",
            "content": "hello world",
            "timestamp": 123,
        }
        self.assertFalse(pre_filter_sse(event))

    def test_sse_filters_image_with_empty_sender(self):
        event = {
            "event": "message.new",
            "rawid": "1",
            "sessionId": "10001",
            "sessionType": "group",
            "sourceName": "",
            "content": "[image]",
            "mediaId": "9f2a1c2d3e4f5a6b7c8d9e0f1a2b3c4d",
            "timestamp": 123,
        }
        self.assertFalse(pre_filter_sse(event))

    def test_sse_keeps_normal_text_with_sender(self):
        event = {
            "event": "message.new",
            "rawid": "1",
            "sessionId": "10001",
            "sessionType": "group",
            "sourceName": "张三",
            "content": "hello world",
            "timestamp": 123,
        }
        self.assertTrue(pre_filter_sse(event))

    def test_rest_filters_empty_sender_with_uid_content(self):
        msg = {
            "localId": 1,
            "localType": 0,
            "createTime": 123,
            "senderUsername": "",
            "content": "u_2XCtJBaCE1zEEUqL2h67Ng",
        }
        self.assertFalse(pre_filter_rest(msg))

    def test_rest_filters_uid_content_with_sender(self):
        msg = {
            "localId": 1,
            "localType": 0,
            "createTime": 123,
            "senderUsername": "u_2XCtJBaCE1zEEUqL2h67Ng",
            "content": "u_2XCtJBaCE1zEEUqL2h67Ng",
        }
        self.assertFalse(pre_filter_rest(msg))

    def test_rest_filters_revoke_uid_content_with_sender(self):
        # 撤回内容在 qqflow 中表现为“有发送者 + 纯 UID 内容”
        msg = {
            "localId": 1,
            "localType": 0,
            "createTime": 123,
            "senderUsername": "u_Z1uF3dwITNvHz1Er6mddKQ",
            "content": "u_Z1uF3dwITNvHz1Er6mddKQ",
        }
        self.assertFalse(pre_filter_rest(msg))

    def test_rest_filters_normal_text_with_empty_sender(self):
        msg = {
            "localId": 1,
            "localType": 0,
            "createTime": 123,
            "senderUsername": "",
            "content": "hello world",
        }
        self.assertFalse(pre_filter_rest(msg))

    def test_rest_filters_image_with_empty_sender(self):
        msg = {
            "localId": 1,
            "localType": 3,
            "createTime": 123,
            "senderUsername": "",
            "content": "[image]",
            "mediaId": "abc123",
        }
        self.assertFalse(pre_filter_rest(msg))

    def test_rest_keeps_normal_text_with_sender(self):
        msg = {
            "localId": 1,
            "localType": 0,
            "createTime": 123,
            "senderUsername": "u_a",
            "content": "hello world",
        }
        self.assertTrue(pre_filter_rest(msg))


class _FakeClient:
    name = "qqflow"
    self_uid = ""  # IGNORE_SELF 自消息判定（测试不启用，空串 fail-open）

    def __init__(
        self,
        contacts: dict[str, str],
        sessions: list[dict],
        messages: list[dict] | None = None,
    ):
        self._contacts = contacts
        self._sessions = sessions
        self._messages = messages or []
        self.group_members_calls: list[str] = []

    async def ensure_ready(self) -> None:
        pass

    async def fetch_contacts(self) -> dict[str, str]:
        return self._contacts

    async def fetch_sessions(self) -> list[dict]:
        return self._sessions

    async def fetch_messages(self, *args, **_kwargs) -> dict:
        return {
            "success": True,
            "talker": args[0] if args else "",
            "count": len(self._messages),
            "hasMore": False,
            "messages": self._messages,
        }

    async def fetch_group_members(self, chatroom_id: str) -> dict[str, str]:
        """已废弃的接口：poller 不应再调用（调用即记录，供断言）。"""
        self.group_members_calls.append(chatroom_id)
        return {}


class _FailingMessagesClient(_FakeClient):
    async def fetch_messages(self, *_args, **_kwargs) -> dict:
        raise RuntimeError("messages down")


class _NotReadyMessagesClient(_FakeClient):
    async def fetch_messages(self, *_args, **_kwargs) -> dict:
        raise QqFlowNotReadyError("qqflow-server 尚未就绪（503）")


class PollerDisplayNameTest(unittest.IsolatedAsyncioTestCase):
    def _enabled(self) -> list[SessionInfo]:
        return [
            SessionInfo(
                source="qqflow", session_id="10001", name="项目群", is_group=True
            )
        ]

    def _message(self, sender_name: str = "群名片") -> dict:
        return {
            "localId": 1,
            "localType": 0,
            "createTime": int(time.time()),
            "senderUsername": "u_nonfriend",
            "senderName": sender_name,
            "content": "hello world",
        }

    async def test_sender_name_resolves_non_friend_sender(self):
        """非好友（不在 contacts）的群成员靠消息自带 senderName 解析。"""
        client = _FakeClient(
            contacts={"u_friend": "朋友"},
            sessions=[{"username": "10001", "displayName": "项目群", "type": 2}],
            messages=[self._message()],
        )

        async def no_processed(ids):
            return set()

        result = await poll(client, self._enabled(), no_processed)
        self.assertEqual(len(result.messages), 1)
        self.assertEqual(result.messages[0].sender_name, "群名片")
        self.assertEqual(result.messages[0].sender_id, "u_nonfriend")
        self.assertNotIn("u_nonfriend", {c.sender_id for c in result.contacts})

    async def test_group_members_endpoint_not_called(self):
        """/api/v1/group-members 与 senderName 同链，poller 不得再逐群请求。"""
        client = _FakeClient(
            contacts={},
            sessions=[{"username": "10001", "displayName": "项目群", "type": 2}],
            messages=[self._message()],
        )

        async def no_processed(ids):
            return set()

        result = await poll(client, self._enabled(), no_processed)
        self.assertEqual(len(result.messages), 1)
        self.assertEqual(client.group_members_calls, [])

    async def test_messages_failure_isolated_to_session(self):
        """【复核 P2-5】单会话拉取失败不再中止整轮：该会话记入
        failed_sessions/session_errors（消息不入库），不再整轮 raise。"""
        client = _FailingMessagesClient(
            contacts={},
            sessions=[{"username": "10001", "displayName": "项目群", "type": 2}],
            messages=[self._message()],
        )

        async def no_processed(ids):
            return set()

        result = await poll(client, self._enabled(), no_processed)
        self.assertEqual(result.messages, [])
        self.assertEqual(result.failed_sessions, {"10001"})
        self.assertIn("messages down", result.session_errors["项目群"])

    async def test_messages_503_skips_session(self):
        client = _NotReadyMessagesClient(
            contacts={},
            sessions=[{"username": "10001", "displayName": "项目群", "type": 2}],
            messages=[self._message()],
        )

        async def no_processed(ids):
            return set()

        result = await poll(client, self._enabled(), no_processed)
        self.assertEqual(result.messages, [])
        # 503 会话不推进水位（防永久漏拉）
        self.assertEqual(result.failed_sessions, {"10001"})


class ControlEventStatsTest(unittest.IsolatedAsyncioTestCase):
    """控制事件（ready / sync）不进管道、也不计入监听统计。"""

    async def test_control_events_skipped_without_inflating_stats(self):
        """上游无就绪门控后每次重连都会带 ready（+可能 sync）基线帧。

        它们不是消息：既不该进管道，也不该计入「事件」或「预过滤丢弃」——
        否则「无消息静默」统计失效（与 weflow 监听器一致）。
        ready 帧的载荷实测为 {"status":"ok"}，不含 event 键。
        """
        batches: list[list] = []

        async def on_batch(msgs: list) -> None:
            batches.append(msgs)

        client = QqFlowClient(
            "http://127.0.0.1:5032", "test-token", qq="12345678"
        )
        # IGNORE_SELF 自消息回查桩：避免测试发起真实 HTTP（fail-open 语义）
        client.lookup_message = AsyncMock(return_value=None)  # type: ignore[method-assign]
        listener = QqFlowSseClient(
            client, on_batch, settings=QqFlowSettings(sse_read_timeout_ms=1000)
        )
        await listener._handle_event({"status": "ok"})  # type: ignore[arg-type]
        await listener._handle_event({"event": "sync", "lastRowidGroup": 1})  # type: ignore[arg-type]
        await listener._handle_event({"event": "ready", "status": "ok"})  # type: ignore[arg-type]
        self.assertEqual(listener._stats_events, 0)
        self.assertEqual(listener._stats_filtered, 0)
        self.assertEqual(batches, [])

        # 真实消息仍计入事件统计（证明跳过逻辑没有误伤 message.new）
        await listener._handle_event(  # type: ignore[arg-type]
            {
                "event": "message.new",
                "sessionId": "10001",
                "sessionType": "group",
                "groupName": "测试群",
                "rawid": "1001",
                "sourceName": "张三",
                "content": "这是一条足够长的测试消息内容",
                "timestamp": 1700000000,
            }
        )
        self.assertEqual(listener._stats_events, 1)


class RuntimeRefreshSessionsTest(unittest.IsolatedAsyncioTestCase):
    async def test_dirty_session_display_name_falls_back_to_username(self):
        source = QqFlowSource(base_url="http://127.0.0.1:5032", api_token="t")
        source.client.ensure_ready = AsyncMock()
        source.client.fetch_sessions = AsyncMock(
            return_value=[
                {"username": "u1", "displayName": "\x01\x01", "type": 1},
                {"username": "g1", "displayName": "项目群", "type": 2},
            ]
        )
        sessions = await source.refresh_sessions()
        names = {s.session_id: s.name for s in sessions}
        self.assertEqual(names["u1"], "u1")
        self.assertEqual(names["g1"], "项目群")
        await source.close()


if __name__ == "__main__":
    unittest.main()
