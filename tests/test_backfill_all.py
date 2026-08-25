"""BACKFILL_HOURS=-1 全量拉取测试。

覆盖 weflow / qqflow 两个源在 -1 配置下的行为：
- 不做年龄截止（极旧消息保留）
- weflow：start 不传（服务端不限时间），按 offset 翻页直至 hasMore=False
- qqflow：start 不传（服务端不限时间），仅由 hasMore 驱动翻页
- 配置校验：-1 合法，-2 非法
"""

import time
import unittest

from pydantic import ValidationError

from briefdesk.config import Settings, config
from briefdesk.plugins.qqflow.poller import poll as qq_poll
from briefdesk.plugins.weflow.poller import poll as we_poll
from briefdesk.types import SessionInfo

# 2018-01-01 的消息时间戳：正常 24h 窗口下必然被年龄截止过滤
_OLD_TS = 1514764800


def _weflow_msg(msg_id: str, ts: int) -> dict:
    return {
        "serverId": msg_id,
        "localType": 1,
        "createTime": ts,
        "senderUsername": "u",
        "content": "hello world",
    }


def _qqflow_msg(msg_id: int, ts: int) -> dict:
    return {
        "localId": msg_id,
        "localType": 0,
        "createTime": ts,
        "senderUsername": "u",
        "content": "hello world",
    }


class _WeFlowPagedClient:
    """按条数 offset 切页的假客户端（500 条/页，与真实 API 的 offset 语义一致）。"""

    name = "weflow"

    def __init__(self, messages: list[dict]):
        self._messages = messages
        self.calls: list[tuple[str, int | None, int]] = []

    async def fetch_contacts(self) -> dict[str, str]:
        return {"u": "用户"}

    async def fetch_sessions(self) -> list[dict]:
        # chatlab 格式会话（type: group/private/channel 权威）
        return [{"id": "g1", "name": "项目群", "type": "group"}]

    async def fetch_messages(
        self, talker: str, start_ts: int | None, limit: int = 500, offset: int = 0,
        media: bool = False,
    ) -> dict:
        self.calls.append((talker, start_ts, offset))
        page = self._messages[offset : offset + 500]
        return {
            "messages": page,
            "hasMore": offset + len(page) < len(self._messages),
        }

    async def fetch_group_members(self, chatroom_id: str) -> dict[str, str]:
        return {"u": "用户"}


class _QqPagedClient:
    """按条数 offset 切页的假客户端（qqflow-server 的 offset 语义）。"""

    name = "qqflow"
    self_uid = ""  # IGNORE_SELF 自消息判定（测试不启用，空串 fail-open）

    def __init__(self, messages: list[dict]):
        self._messages = messages
        self.calls: list[tuple[int | None, int]] = []

    async def ensure_ready(self) -> None:
        pass

    async def fetch_contacts(self) -> dict[str, str]:
        return {"u": "用户"}

    async def fetch_sessions(self) -> list[dict]:
        return [{"username": "g1", "displayName": "项目群", "type": 2}]

    async def fetch_messages(
        self, talker: str, start: int | None = None, limit: int = 500, offset: int = 0
    ) -> dict:
        self.calls.append((start, offset))
        page = self._messages[offset : offset + 500]
        return {
            "messages": page,
            "hasMore": offset + len(page) < len(self._messages),
        }

    async def fetch_group_members(self, chatroom_id: str) -> dict[str, str]:
        return {"u": "用户"}


async def _no_processed(ids):
    return set()


def _enabled(source: str) -> list[SessionInfo]:
    return [
        SessionInfo(source=source, session_id="g1", name="项目群", is_group=True)
    ]


class WeFlowBackfillAllTest(unittest.IsolatedAsyncioTestCase):
    async def test_pull_all_pages_and_keeps_old_messages(self):
        original = config.backfill_hours
        config.backfill_hours = -1
        try:
            messages = [_weflow_msg(f"m{i}", int(time.time())) for i in range(500)]
            messages.append(_weflow_msg("old", _OLD_TS))  # 501 条 → 两页
            client = _WeFlowPagedClient(messages)

            result = await we_poll(client, _enabled("weflow"), _no_processed)

            self.assertEqual(len(result.messages), 501)
            ids = {m.msg_id for m in result.messages}
            self.assertIn("old", ids)  # 无年龄截止：极旧消息保留
            # start 一律不传（None，服务端不限时间），offset 按 0/500 翻页
            self.assertIsNone(client.calls[0][1])
            self.assertEqual([c[2] for c in client.calls], [0, 500])
        finally:
            config.backfill_hours = original


class QqFlowBackfillAllTest(unittest.IsolatedAsyncioTestCase):
    async def test_pull_all_omits_start_and_pages_to_end(self):
        original = config.backfill_hours
        config.backfill_hours = -1
        try:
            messages = [_qqflow_msg(i, int(time.time())) for i in range(500)]
            messages.append(_qqflow_msg(9999, _OLD_TS))  # 501 条 → 两页
            client = _QqPagedClient(messages)

            result = await qq_poll(client, _enabled("qqflow"), _no_processed)

            self.assertEqual(len(result.messages), 501)
            ids = {m.msg_id for m in result.messages}
            self.assertIn("9999", ids)  # 无年龄截止：极旧消息保留
            # start 一律不传（None），仅 hasMore 驱动翻页
            self.assertTrue(all(s is None for s, _ in client.calls))
            self.assertEqual([o for _, o in client.calls], [0, 500])
        finally:
            config.backfill_hours = original


class BackfillConfigValidationTest(unittest.TestCase):
    def test_minus_one_allowed(self):
        self.assertEqual(Settings(backfill_hours=-1).backfill_hours, -1)

    def test_below_minus_one_rejected(self):
        with self.assertRaises(ValidationError):
            Settings(backfill_hours=-2)
