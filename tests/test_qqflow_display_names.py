"""qqflow 发送者显示名处理测试。

覆盖 group-members 接口解析、normalize_rest 的 per-session 优先级，
以及 poller 内非好友群成员的显示名解析与错误语义。
"""

import time
import unittest
from unittest.mock import AsyncMock, patch

from briefdesk.plugins.qqflow.client import QqFlowClient, QqFlowNotReadyError
from briefdesk.plugins.qqflow.normalize import (
    normalize_rest,
    normalize_sse,
    pre_filter_rest,
    pre_filter_sse,
)
from briefdesk.plugins.qqflow.poller import poll
from briefdesk.plugins.qqflow.runtime import QqFlowSource
from briefdesk.types import SessionInfo


class FetchGroupMembersTest(unittest.IsolatedAsyncioTestCase):
    async def test_group_nickname_wins_and_candidates_cleaned(self):
        client = QqFlowClient(
            base_url="http://127.0.0.1:5032",
            api_token="t",
            qq="1",
            key="k",
        )
        payload = {
            "members": [
                {
                    "wxid": "u1",
                    "displayName": "消息昵称",
                    "nickname": "消息昵称",
                    "remark": "备注",
                    "groupNickname": "群名片",
                },
                {
                    "wxid": "u2",
                    "displayName": " 李四 ",
                    "nickname": "",
                    "remark": "",
                    "groupNickname": "u2",
                },
                {
                    "wxid": "u3",
                    "displayName": "\x01\x01",
                    "nickname": "\x02乙",
                    "remark": "",
                    "groupNickname": "",
                },
                {
                    "wxid": "u4",
                    "displayName": "\x01\x01",
                    "nickname": "   ",
                    "remark": "\t",
                    "groupNickname": "\x02",
                },
            ]
        }
        with patch.object(
            QqFlowClient, "_get", new=AsyncMock(return_value=payload)
        ) as mock:
            members = await client.fetch_group_members("10001")
        self.assertEqual(members, {"u1": "群名片", "u2": "李四", "u3": "乙"})
        self.assertNotIn("u4", members)
        mock.assert_awaited_once_with(
            "/api/v1/group-members",
            params={"chatroomId": "10001"},
            not_found_ok=True,
        )

    async def test_not_found_returns_empty_mapping(self):
        client = QqFlowClient(
            base_url="http://127.0.0.1:5032",
            api_token="t",
            qq="1",
            key="k",
        )
        with patch.object(QqFlowClient, "_get", new=AsyncMock(return_value=None)):
            members = await client.fetch_group_members("gone")
        self.assertEqual(members, {})

    async def test_other_errors_propagate(self):
        client = QqFlowClient(
            base_url="http://127.0.0.1:5032",
            api_token="t",
            qq="1",
            key="k",
        )
        with (
            patch.object(
                QqFlowClient, "_get", side_effect=RuntimeError("QqFlow API error: 500")
            ),
            self.assertRaisesRegex(RuntimeError, "500"),
        ):
            await client.fetch_group_members("10001")


class NormalizeRestDisplayNameTest(unittest.TestCase):
    def _msg(self, uid: str) -> dict:
        return {
            "localId": 1,
            "content": "hello world",
            "localType": 0,
            "createTime": 123,
            "senderUsername": uid,
        }

    def test_group_member_name_wins_over_contact(self):
        msg = normalize_rest(
            self._msg("u_a"),
            "10001",
            "项目群",
            {"u_a": "全局备注名"},
            {"u_a": "群名片"},
        )
        self.assertEqual(msg.sender_name, "群名片")
        self.assertEqual(msg.sender_id, "u_a")

    def test_dirty_group_member_falls_back_to_contact(self):
        msg = normalize_rest(
            self._msg("u_a"),
            "10001",
            "项目群",
            {"u_a": "全局备注名"},
            {"u_a": "\x01\x01"},
        )
        self.assertEqual(msg.sender_name, "全局备注名")

    def test_missing_names_fall_back_to_uid(self):
        msg = normalize_rest(self._msg("u_a"), "10001", "项目群", {}, {})
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
        group_members: dict[str, dict[str, str]] | None = None,
        messages: list[dict] | None = None,
    ):
        self._contacts = contacts
        self._sessions = sessions
        self._group_members = group_members or {}
        self._messages = messages or []

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
        return self._group_members.get(chatroom_id, {})


class _FailingGroupMembersClient(_FakeClient):
    async def fetch_group_members(self, chatroom_id: str) -> dict[str, str]:
        raise RuntimeError("group members down")


class _NotReadyGroupMembersClient(_FakeClient):
    async def fetch_group_members(self, chatroom_id: str) -> dict[str, str]:
        raise QqFlowNotReadyError("qqflow-server 尚未就绪（503）")


class PollerDisplayNameTest(unittest.IsolatedAsyncioTestCase):
    def _enabled(self) -> list[SessionInfo]:
        return [
            SessionInfo(
                source="qqflow", session_id="10001", name="项目群", is_group=True
            )
        ]

    def _message(self) -> dict:
        return {
            "localId": 1,
            "localType": 0,
            "createTime": int(time.time()),
            "senderUsername": "u_nonfriend",
            "content": "hello world",
        }

    async def test_group_member_name_resolves_non_friend_sender(self):
        client = _FakeClient(
            contacts={"u_friend": "朋友"},
            sessions=[{"username": "10001", "displayName": "项目群", "type": 2}],
            group_members={"10001": {"u_nonfriend": "群名片"}},
            messages=[self._message()],
        )

        async def no_processed(ids):
            return set()

        result = await poll(client, self._enabled(), no_processed)
        self.assertEqual(len(result.messages), 1)
        self.assertEqual(result.messages[0].sender_name, "群名片")
        self.assertEqual(result.messages[0].sender_id, "u_nonfriend")
        self.assertNotIn("u_nonfriend", {c.sender_id for c in result.contacts})

    async def test_group_members_failure_aborts_poll(self):
        client = _FailingGroupMembersClient(
            contacts={},
            sessions=[{"username": "10001", "displayName": "项目群", "type": 2}],
            group_members={},
            messages=[self._message()],
        )

        async def no_processed(ids):
            return set()

        with self.assertRaisesRegex(RuntimeError, "group members down"):
            await poll(client, self._enabled(), no_processed)

    async def test_group_members_503_skips_session(self):
        client = _NotReadyGroupMembersClient(
            contacts={},
            sessions=[{"username": "10001", "displayName": "项目群", "type": 2}],
            group_members={},
            messages=[self._message()],
        )

        async def no_processed(ids):
            return set()

        result = await poll(client, self._enabled(), no_processed)
        self.assertEqual(result.messages, [])


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
