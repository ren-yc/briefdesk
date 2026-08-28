"""weflow 发送者显示名处理测试。

覆盖 normalize_rest 对上游 senderName 的采用与净化/退化兜底，
以及 poller 不再逐群请求 /api/v1/group-members。

上游 senderName 在 index 期由全局 contacts 算出（备注 > 昵称 > wxid），
无群名片：group_cards 字段在上游全仓从未被写入，两个 Store 构造点都是
Default::default()。实测最近活跃 5 群 208 名成员 groupNickname 非空 0 条，
974 条消息与 group-members 派生名 974 条同值。
"""

import time
import unittest
from unittest.mock import AsyncMock

from briefdesk.plugins.weflow.normalize import normalize_rest, normalize_sse
from briefdesk.plugins.weflow.poller import poll
from briefdesk.types import SessionInfo


class NormalizeRestDisplayNameTest(unittest.TestCase):
    def _msg(self, wxid: str, sender_name: str | None = None) -> dict:
        msg = {
            "serverId": "1001",
            "localId": 1,
            "localType": 1,
            "createTime": 123,
            "senderUsername": wxid,
            "content": "hello world",
        }
        if sender_name is not None:
            msg["senderName"] = sender_name
        return msg

    def test_upstream_sender_name_wins_over_contact(self):
        msgs = normalize_rest(
            self._msg("wxid_a", "上游名"), "s1", "项目群", {"wxid_a": "全局备注名"}
        )
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0].sender_name, "上游名")
        self.assertEqual(msgs[0].sender_id, "wxid_a")

    def test_dirty_sender_name_falls_back_to_contact(self):
        msgs = normalize_rest(
            self._msg("wxid_a", "\x01\x01"), "s1", "项目群", {"wxid_a": "全局备注名"}
        )
        self.assertEqual(msgs[0].sender_name, "全局备注名")

    def test_wxid_valued_sender_name_falls_back_to_contact(self):
        """上游名字链全退化时 senderName 即 wxid，应让位于 contacts。

        私聊/公众号对端可能不在上游 contacts 集合，poller 用会话显示名回填了
        contacts —— 那是唯一名字来源，不能被 wxid 压住。
        """
        msgs = normalize_rest(
            self._msg("wxid_a", "wxid_a"), "s1", "项目群", {"wxid_a": "会话显示名"}
        )
        self.assertEqual(msgs[0].sender_name, "会话显示名")

    def test_absent_sender_name_falls_back_to_contact(self):
        """旧上游无该字段（版本偏斜兜底）。"""
        msgs = normalize_rest(
            self._msg("wxid_a"), "s1", "项目群", {"wxid_a": "全局备注名"}
        )
        self.assertEqual(msgs[0].sender_name, "全局备注名")

    def test_missing_names_fall_back_to_wxid(self):
        msgs = normalize_rest(self._msg("wxid_a"), "s1", "项目群", {})
        self.assertEqual(msgs[0].sender_name, "wxid_a")


class NormalizeSseImageLookupTest(unittest.IsolatedAsyncioTestCase):
    """[图片] 的 REST 回查预检（上游 v0.3.0 推送携带 media 元数据）三态保守语义。

    - media.type == "image" → 回查（元数据无 url，字节需 REST 导出回填）
    - media.type != "image" → 跳过回查（必无图片 URL，省一次本机 HTTP）
    - 元数据缺失/null → 保持回查（边缘情形：SSE 解析失败但 REST 可导出）
    """

    def _event(self, **extra: object) -> dict:
        return {
            "event": "message.new",
            "sessionId": "wxid_test_0001",
            "sessionType": "private",
            "sourceName": "张三",
            "rawid": "1001",
            "content": "[图片]",
            "timestamp": 1700000000,
            **extra,
        }

    async def test_image_type_triggers_lookup(self):
        client = _FakeClient(contacts={}, messages=[])
        client.fetch_message_media = AsyncMock(  # type: ignore[method-assign]
            return_value="wxid_test_0001/images/abc.jpg"
        )
        msgs = await normalize_sse(
            self._event(media={"type": "image", "fileName": "abc.jpg", "md5": "a" * 32}),
            client,
        )
        client.fetch_message_media.assert_awaited_once()  # type: ignore[attr-defined]
        self.assertEqual(msgs[0].image_urls, ["wxid_test_0001/images/abc.jpg"])

    async def test_non_image_type_skips_lookup(self):
        client = _FakeClient(contacts={}, messages=[])
        client.fetch_message_media = AsyncMock()  # type: ignore[method-assign]
        msgs = await normalize_sse(
            self._event(media={"type": "voice", "fileName": "v.silk", "md5": "b" * 32}),
            client,
        )
        client.fetch_message_media.assert_not_awaited()  # type: ignore[attr-defined]
        self.assertEqual(msgs[0].image_urls, [])

    async def test_absent_media_keeps_lookup(self):
        client = _FakeClient(contacts={}, messages=[])
        client.fetch_message_media = AsyncMock(return_value=None)  # type: ignore[method-assign]
        await normalize_sse(self._event(), client)
        client.fetch_message_media.assert_awaited_once()  # type: ignore[attr-defined]


class _FakeClient:
    name = "weflow"

    def __init__(self, contacts: dict[str, str], messages: list[dict]):
        self._contacts = contacts
        self._messages = messages
        self.group_members_calls: list[str] = []

    async def ensure_ready(self) -> None:
        pass

    async def fetch_contacts(self) -> dict[str, str]:
        return self._contacts

    async def fetch_sessions(self) -> list[dict]:
        return [
            {
                "username": "g1@chatroom",
                "displayName": "项目群",
                "sessionType": "group",
                "lastTimestamp": 0,
            }
        ]

    async def fetch_messages(self, *_args, **_kwargs) -> dict:
        return {"messages": self._messages, "hasMore": False}

    async def fetch_group_members(self, chatroom_id: str) -> dict[str, str]:
        """已废弃的接口：poller 不应再调用（调用即记录，供断言）。"""
        self.group_members_calls.append(chatroom_id)
        return {"wxid_nonfriend": "群成员派生名"}


class PollerDisplayNameTest(unittest.IsolatedAsyncioTestCase):
    def _enabled(self) -> list[SessionInfo]:
        return [
            SessionInfo(
                source="weflow",
                session_id="g1@chatroom",
                name="项目群",
                is_group=True,
            )
        ]

    def _message(self, sender_name: str = "上游名") -> dict:
        return {
            "serverId": "1001",
            "localId": 1,
            "localType": 1,
            "createTime": int(time.time()),
            "senderUsername": "wxid_nonfriend",
            "senderName": sender_name,
            "content": "hello world",
        }

    async def _poll(self, client) -> object:
        async def no_processed(ids):
            return set()

        return await poll(client, self._enabled(), no_processed)

    async def test_sender_name_resolves_non_friend_sender(self):
        """非好友（不在 contacts）的群成员靠消息自带 senderName 解析。"""
        client = _FakeClient(
            contacts={"wxid_friend": "朋友"}, messages=[self._message()]
        )
        result = await self._poll(client)
        self.assertEqual(len(result.messages), 1)
        self.assertEqual(result.messages[0].sender_name, "上游名")
        self.assertEqual(result.messages[0].sender_id, "wxid_nonfriend")

    async def test_group_members_endpoint_not_called(self):
        """group-members 的两级候选都与 senderName 同源，不得再逐群请求。"""
        client = _FakeClient(contacts={}, messages=[self._message()])
        result = await self._poll(client)
        self.assertEqual(len(result.messages), 1)
        self.assertEqual(client.group_members_calls, [])
        # 未被请求 ⇒ 派生名不可能出现在结果里
        self.assertEqual(result.messages[0].sender_name, "上游名")


if __name__ == "__main__":
    unittest.main()
