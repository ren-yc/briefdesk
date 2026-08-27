"""weflow-legacy 发送者显示名处理测试。

覆盖 REST 联系人候选清洗/回退、SSE/REST 归一化显示名回退、
poller 私聊会话显示名兜底与 runtime 会话名清洗。
"""

import time
import unittest
from unittest.mock import AsyncMock, patch

from briefdesk.plugins.weflow_legacy.client import (
    WeFlowLegacyClient,
    is_group_session,
    is_official_session,
    is_private_session,
)
from briefdesk.plugins.weflow_legacy.normalize import normalize_rest, normalize_sse
from briefdesk.plugins.weflow_legacy.poller import poll
from briefdesk.plugins.weflow_legacy.runtime import WeFlowLegacySource
from briefdesk.types import SessionInfo


class SessionKindTest(unittest.TestCase):
    """chatlab 会话类型判定：channel→公众号，group/private 直映射，未知兜底私聊。"""

    def test_chatlab_types(self):
        self.assertTrue(is_group_session({"type": "group"}))
        self.assertTrue(is_private_session({"type": "private"}))
        self.assertTrue(is_official_session({"type": "channel"}))

    def test_unknown_type_falls_back_to_private(self):
        # JSON 格式的 type=2（数字）等未知值兜底为私聊，避免误判为群聊
        self.assertFalse(is_group_session({"type": 2}))
        self.assertTrue(is_private_session({"type": 2}))
        self.assertFalse(is_official_session({"type": 2}))


class FetchContactsTest(unittest.IsolatedAsyncioTestCase):
    async def test_candidates_cleaned_before_fallback(self):
        """每级候选必须净化后再判空：脏 displayName 不能压掉干净的 nickname/remark。"""
        client = WeFlowLegacyClient(base_url="http://127.0.0.1:5031", api_token="t")
        payload = {
            "contacts": [
                {"username": "u1", "displayName": "\x01\x01甲"},
                {
                    "username": "u2",
                    "displayName": "\x01\x01",
                    "nickname": " 乙 ",
                    "remark": "",
                },
                {
                    "username": "u3",
                    "displayName": "",
                    "nickname": "\x02\x02",
                    "remark": "丙",
                },
                {
                    "username": "u4",
                    "displayName": "\x01\x01",
                    "nickname": "   ",
                    "remark": "\t",
                },
            ]
        }
        with patch.object(WeFlowLegacyClient, "_get", new=AsyncMock(return_value=payload)):
            contacts = await client.fetch_contacts()
        self.assertEqual(
            contacts,
            {"u1": "甲", "u2": "乙", "u3": "丙", "u4": "u4"},
        )


class FetchGroupMembersTest(unittest.IsolatedAsyncioTestCase):
    async def test_group_nickname_wins_and_candidates_cleaned(self):
        client = WeFlowLegacyClient(base_url="http://127.0.0.1:5031", api_token="t")
        payload = {
            "members": [
                {
                    "wxid": "u1",
                    "displayName": "客户A",
                    "nickname": "阿甲",
                    "remark": "客户A",
                    "groupNickname": "甲方",
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
            WeFlowLegacyClient, "_get", new=AsyncMock(return_value=payload)
        ) as mock:
            members = await client.fetch_group_members("g1")
        self.assertEqual(members, {"u1": "甲方", "u2": "李四", "u3": "乙"})
        self.assertNotIn("u4", members)
        mock.assert_awaited_once_with(
            "/api/v1/group-members",
            params={"chatroomId": "g1"},
            not_found_ok=True,
        )

    async def test_not_found_returns_empty_mapping(self):
        client = WeFlowLegacyClient(base_url="http://127.0.0.1:5031", api_token="t")
        with patch.object(WeFlowLegacyClient, "_get", new=AsyncMock(return_value=None)):
            members = await client.fetch_group_members("gone")
        self.assertEqual(members, {})

    async def test_other_errors_propagate(self):
        client = WeFlowLegacyClient(base_url="http://127.0.0.1:5031", api_token="t")
        with (
            patch.object(
                WeFlowLegacyClient, "_get", side_effect=RuntimeError("WeFlow API error: 500")
            ),
            self.assertRaisesRegex(RuntimeError, "500"),
        ):
            await client.fetch_group_members("g1")


class NormalizeSseDisplayNameTest(unittest.IsolatedAsyncioTestCase):
    async def test_dirty_source_name_falls_back_to_unknown(self):
        msgs = await normalize_sse(
            {
                "event": "message.new",
                "rawid": "1",
                "sessionId": "wxid_x",
                "sessionType": "private",
                "sourceName": "\x01\x01",
                "content": "hello world",
                "timestamp": 123,
            }
        )
        msg = msgs[0]
        self.assertEqual(msg.sender_name, "未知")
        self.assertEqual(msg.group_name, "未知")

    async def test_dirty_group_name_falls_back_to_session_id(self):
        msgs = await normalize_sse(
            {
                "event": "message.new",
                "rawid": "1",
                "sessionId": "xxx@chatroom",
                "sessionType": "group",
                "sourceName": "\x01李四",
                "groupName": "\x02\x02",
                "content": "hello world",
                "timestamp": 123,
            }
        )
        msg = msgs[0]
        self.assertEqual(msg.sender_name, "李四")
        self.assertEqual(msg.group_name, "xxx@chatroom")


class NormalizeRestDisplayNameTest(unittest.TestCase):
    def _msg(self, wxid: str) -> dict:
        return {
            "serverId": "s1",
            "content": "hello world",
            "localType": 1,
            "createTime": 123,
            "senderUsername": wxid,
        }

    def test_clean_contact_name_used(self):
        msg = normalize_rest(self._msg("wxid_a"), "sess", "群", {"wxid_a": "张三"})[0]
        self.assertEqual(msg.sender_name, "张三")
        self.assertEqual(msg.sender_id, "wxid_a")

    def test_dirty_contact_value_falls_back_to_wxid(self):
        msg = normalize_rest(self._msg("wxid_a"), "sess", "群", {"wxid_a": "\x01\x01"})[0]
        self.assertEqual(msg.sender_name, "wxid_a")

    def test_missing_contact_falls_back_to_wxid(self):
        msg = normalize_rest(self._msg("wxid_a"), "sess", "群", {})[0]
        self.assertEqual(msg.sender_name, "wxid_a")

    def test_group_member_name_wins_over_contact(self):
        msg = normalize_rest(
            self._msg("wxid_a"),
            "sess",
            "群",
            {"wxid_a": "全局备注名"},
            {"wxid_a": "群名片"},
        )[0]
        self.assertEqual(msg.sender_name, "群名片")
        self.assertEqual(msg.sender_id, "wxid_a")

    def test_dirty_group_member_falls_back_to_contact(self):
        msg = normalize_rest(
            self._msg("wxid_a"),
            "sess",
            "群",
            {"wxid_a": "全局备注名"},
            {"wxid_a": "\x01\x01"},
        )[0]
        self.assertEqual(msg.sender_name, "全局备注名")

    def test_missing_sender_username_falls_back_to_unknown(self):
        msg = normalize_rest(self._msg(""), "sess", "群", {})[0]
        self.assertEqual(msg.sender_name, "未知")


class _FakeClient:
    name = "weflow-legacy"

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

    async def fetch_contacts(self) -> dict[str, str]:
        return self._contacts

    async def fetch_sessions(self) -> list[dict]:
        return self._sessions

    async def fetch_messages(self, *_args, **_kwargs) -> dict:
        return {"messages": self._messages, "hasMore": False}

    async def fetch_group_members(self, chatroom_id: str) -> dict[str, str]:
        return self._group_members.get(chatroom_id, {})


class _FailingContactsClient(_FakeClient):
    async def fetch_contacts(self) -> dict[str, str]:
        raise RuntimeError("contacts down")


class _FailingGroupMembersClient(_FakeClient):
    async def fetch_group_members(self, chatroom_id: str) -> dict[str, str]:
        raise RuntimeError("group members down")


class PollerDisplayNameTest(unittest.IsolatedAsyncioTestCase):
    async def test_private_session_name_backfills_contacts(self):
        client = _FakeClient(
            contacts={"friend1": "朋友"},
            sessions=[
                {
                    "id": "u_private",
                    "name": "\x01\x01私聊对象",
                    "type": "private",
                },
                {
                    "id": "g1",
                    "name": "\x02\x02",
                    "type": "group",
                },
            ],
        )

        async def no_processed(ids):
            return set()

        result = await poll(client, [], no_processed)
        contacts = {c.sender_id: c.display_name for c in result.contacts}
        self.assertEqual(contacts["friend1"], "朋友")
        self.assertEqual(contacts["u_private"], "私聊对象")
        self.assertNotIn("g1", contacts)

        sessions = {s.session_id: s.name for s in result.sessions}
        self.assertEqual(sessions["u_private"], "私聊对象")
        self.assertEqual(sessions["g1"], "g1")

    async def test_official_session_backfills_contacts_and_flags_official(self):
        """公众号会话：显示名兜底进 contacts，且会话产出 is_official=True。"""
        client = _FakeClient(
            contacts={},
            sessions=[{"id": "gh_abc", "name": "\x01\x01上海发布", "type": "channel"}],
        )

        async def no_processed(ids):
            return set()

        result = await poll(client, [], no_processed)
        contacts = {c.sender_id: c.display_name for c in result.contacts}
        self.assertEqual(contacts["gh_abc"], "上海发布")
        sessions = {s.session_id: s for s in result.sessions}
        self.assertTrue(sessions["gh_abc"].is_official)
        self.assertFalse(sessions["gh_abc"].is_group)

    async def test_contacts_failure_aborts_poll(self):
        """与 qqflow 对齐：contacts 拉取失败必须中止本轮，不能带 wxid 显示名入库。"""
        client = _FailingContactsClient(
            contacts={},
            sessions=[
                {
                    "id": "u_private",
                    "name": "私聊对象",
                    "type": "private",
                }
            ],
        )

        async def no_processed(ids):
            return set()

        with self.assertRaisesRegex(RuntimeError, "contacts down"):
            await poll(client, [], no_processed)

    async def test_group_member_name_resolves_non_friend_sender(self):
        """非好友群成员通过 group-members 解析，且不写入全局 contacts。"""
        client = _FakeClient(
            contacts={"friend1": "朋友"},
            sessions=[{"id": "g1", "name": "项目群", "type": "group"}],
            group_members={"g1": {"wxid_nonfriend": "甲方"}},
            messages=[
                {
                    "serverId": "s1",
                    "localType": 1,
                    "createTime": int(time.time()),
                    "senderUsername": "wxid_nonfriend",
                    "content": "hello world",
                }
            ],
        )
        enabled = [
            SessionInfo(source="weflow-legacy", session_id="g1", name="项目群", is_group=True)
        ]

        async def no_processed(ids):
            return set()

        result = await poll(client, enabled, no_processed)
        self.assertEqual(len(result.messages), 1)
        self.assertEqual(result.messages[0].sender_name, "甲方")
        self.assertEqual(result.messages[0].sender_id, "wxid_nonfriend")
        self.assertNotIn(
            "wxid_nonfriend",
            {c.sender_id for c in result.contacts},
        )

    async def test_group_members_failure_aborts_poll(self):
        client = _FailingGroupMembersClient(
            contacts={},
            sessions=[{"id": "g1", "name": "项目群", "type": "group"}],
            group_members={},
            messages=[
                {
                    "serverId": "s1",
                    "localType": 1,
                    "createTime": int(time.time()),
                    "senderUsername": "wxid_nonfriend",
                    "content": "hello world",
                }
            ],
        )
        enabled = [
            SessionInfo(source="weflow-legacy", session_id="g1", name="项目群", is_group=True)
        ]

        async def no_processed(ids):
            return set()

        with self.assertRaisesRegex(RuntimeError, "group members down"):
            await poll(client, enabled, no_processed)


class RuntimeRefreshSessionsTest(unittest.IsolatedAsyncioTestCase):
    async def test_dirty_session_display_name_falls_back_to_id(self):
        source = WeFlowLegacySource(base_url="http://127.0.0.1:5031", api_token="t")
        source.client.fetch_sessions = AsyncMock(
            return_value=[
                {
                    "id": "u1",
                    "name": "\x01\x01",
                    "type": "private",
                },
                {
                    "id": "g1",
                    "name": "项目群",
                    "type": "group",
                },
                {
                    "id": "gh_x",
                    "name": "上海发布",
                    "type": "channel",
                },
            ]
        )
        sessions = await source.refresh_sessions()
        names = {s.session_id: s.name for s in sessions}
        kinds = {s.session_id: (s.is_group, s.is_official) for s in sessions}
        self.assertEqual(names["u1"], "u1")
        self.assertEqual(names["g1"], "项目群")
        self.assertEqual(kinds["g1"], (True, False))
        self.assertEqual(kinds["gh_x"], (False, True))
        await source.close()


if __name__ == "__main__":
    unittest.main()
