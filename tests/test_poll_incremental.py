"""增量轮询测试（按会话水位完整版）。

覆盖：
- weflow：window_start_by_session 传入 → 各会话 start 参数透传、多页翻页至
  hasMore=False、窗口下界含边界（createTime == start 保留）、超窗口消息过滤；
  无窗口表/会话缺省 → 回退 BACKFILL_HOURS；-1 全量优先于窗口
- qqflow：window_start_by_session 传入 → start=窗口下界、早停与边界过滤
- poll_cycle._compute_session_windows：会话水位 / 未处理消息按会话钉窗 /
  回填起点 / overlap 组合计算（互不影响）
"""

import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import aiosqlite

from briefdesk.config import config
from briefdesk.db import (
    get_oldest_unprocessed_by_session,
    get_session_last_polls,
    init_schema,
    mark_message_processed,
    update_session_last_polls,
)
from briefdesk.plugins.qqflow.poller import poll as qq_poll
from briefdesk.plugins.weflow.poller import poll as we_poll
from briefdesk.poll_cycle import _compute_session_windows, run_poll_cycle
from briefdesk.types import SessionInfo

_DAY = 86400


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


class _WeFlowClient:
    """按条数 offset 切页的假客户端（500 条/页，hasMore 语义与真实 API 一致）。"""

    name = "weflow"

    def __init__(self, messages: list[dict]):
        self._messages = messages
        self.calls: list[tuple[str, int | None, int, bool]] = []

    async def fetch_contacts(self) -> dict[str, str]:
        return {"u": "用户"}

    async def fetch_sessions(self) -> list[dict]:
        return [{"id": "g1", "name": "项目群", "type": "group"}]

    async def fetch_messages(
        self, talker: str, start_ts: int | None, limit: int = 500, offset: int = 0,
        media: bool = False,
    ) -> dict:
        self.calls.append((talker, start_ts, offset, media))
        page = self._messages[offset : offset + limit]
        return {
            "messages": page,
            "hasMore": offset + len(page) < len(self._messages),
        }

    async def fetch_group_members(self, chatroom_id: str) -> dict[str, str]:
        return {"u": "用户"}


class _QqFlowClient:
    name = "qqflow"
    self_uid = ""  # IGNORE_SELF 自消息判定（fail-open，不启用）

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
        page = self._messages[offset : offset + limit]
        return {
            "messages": page,
            "hasMore": offset + len(page) < len(self._messages),
        }

    async def fetch_group_members(self, chatroom_id: str) -> dict[str, str]:
        return {"u": "用户"}


async def _no_processed(ids):
    return set()


def _enabled(source: str, *session_ids: str) -> list[SessionInfo]:
    return [
        SessionInfo(source=source, session_id=sid, name=sid, is_group=True)
        for sid in session_ids
    ]


class WeFlowIncrementalTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # 固定 BACKFILL_HOURS：增量窗口测试不依赖 .env 配置（本地 .env 可能为 -1 全量）
        self._hours = config.backfill_hours
        config.backfill_hours = 24

    def tearDown(self):
        config.backfill_hours = self._hours

    async def test_incremental_window_passes_start_and_pages_to_end(self):
        now = int(time.time())
        window = now - _DAY
        # 500 条窗口内 + 1 条恰好窗口下界 + 1 条超窗口 → 502 条共两页
        messages = [_weflow_msg(f"m{i}", now - i * 10) for i in range(500)]
        messages.append(_weflow_msg("edge", window))  # createTime == start：含边界保留
        messages.append(_weflow_msg("old", window - 1))  # 超窗口：过滤
        client = _WeFlowClient(messages)

        result = await we_poll(
            client,
            _enabled("weflow", "g1"),
            _no_processed,
            window_start_by_session={"g1": window},
        )

        # start 参数透传为该会话窗口下界；翻页直至 hasMore=False（无 500 条硬顶）
        self.assertEqual(client.calls[0][1], window)
        self.assertEqual([c[2] for c in client.calls], [0, 500])
        self.assertTrue(all(c[3] for c in client.calls))  # media=True 保持
        ids = {m.msg_id for m in result.messages}
        self.assertIn("edge", ids, "窗口下界含边界：createTime == start 应保留")
        self.assertNotIn("old", ids, "超窗口消息应被过滤")
        self.assertEqual(len(ids), 501)

    async def test_incremental_respects_processed(self):
        now = int(time.time())
        window = now - 3600
        messages = [_weflow_msg("m1", now - 10), _weflow_msg("m2", now - 20)]
        client = _WeFlowClient(messages)

        async def processed(ids):
            return {"m1"}

        result = await we_poll(
            client,
            _enabled("weflow", "g1"),
            processed,
            window_start_by_session={"g1": window},
        )

        self.assertEqual([m.msg_id for m in result.messages], ["m2"])

    async def test_session_missing_from_windows_falls_back_to_backfill(self):
        # 会话缺省（无水位/新启用）→ 回退 BACKFILL_HOURS 窗口（启用即回填）
        original = config.backfill_hours
        config.backfill_hours = 24
        try:
            now = int(time.time())
            client = _WeFlowClient([_weflow_msg("m1", now - 10)])

            await we_poll(
                client,
                _enabled("weflow", "g1"),
                _no_processed,
                window_start_by_session={"other_session": now - 3600},
            )

            start_ts = client.calls[0][1]
            self.assertIsNotNone(start_ts)
            self.assertAlmostEqual(start_ts, now - 24 * 3600, delta=5)
        finally:
            config.backfill_hours = original

    async def test_no_window_map_falls_back_to_backfill(self):
        original = config.backfill_hours
        config.backfill_hours = 24
        try:
            now = int(time.time())
            messages = [_weflow_msg("m1", now - 10)]
            client = _WeFlowClient(messages)

            await we_poll(client, _enabled("weflow", "g1"), _no_processed)

            start_ts = client.calls[0][1]
            self.assertIsNotNone(start_ts)
            self.assertAlmostEqual(start_ts, now - 24 * 3600, delta=5)
        finally:
            config.backfill_hours = original

    async def test_none_window_value_falls_back_to_backfill(self):
        # 会话窗口值为 None（无水位/重新启用）→ 回退 BACKFILL_HOURS（启用即回填）
        original = config.backfill_hours
        config.backfill_hours = 24
        try:
            now = int(time.time())
            client = _WeFlowClient([_weflow_msg("m1", now - 10)])

            await we_poll(
                client,
                _enabled("weflow", "g1"),
                _no_processed,
                window_start_by_session={"g1": None},
            )

            start_ts = client.calls[0][1]
            self.assertIsNotNone(start_ts)
            self.assertAlmostEqual(start_ts, now - 24 * 3600, delta=5)
        finally:
            config.backfill_hours = original

    async def test_pull_all_wins_over_window(self):
        original = config.backfill_hours
        config.backfill_hours = -1
        try:
            now = int(time.time())
            client = _WeFlowClient([_weflow_msg("m1", now - 10)])

            await we_poll(
                client,
                _enabled("weflow", "g1"),
                _no_processed,
                window_start_by_session={"g1": now - 3600},
            )

            self.assertIsNone(client.calls[0][1], "全量模式 start 不传")
        finally:
            config.backfill_hours = original


class QqFlowIncrementalTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # 固定 BACKFILL_HOURS：增量窗口测试不依赖 .env 配置（本地 .env 可能为 -1 全量）
        self._hours = config.backfill_hours
        config.backfill_hours = 24

    def tearDown(self):
        config.backfill_hours = self._hours

    async def test_incremental_window_passes_start_and_filters_old(self):
        now = int(time.time())
        window = now - _DAY
        messages = [
            _qqflow_msg(1, now),
            _qqflow_msg(2, window),  # 恰好窗口下界：保留
            _qqflow_msg(3, window - 1),  # 超窗口：早停过滤
        ]
        client = _QqFlowClient(messages)

        result = await qq_poll(
            client,
            _enabled("qqflow", "g1"),
            _no_processed,
            window_start_by_session={"g1": window},
        )

        self.assertEqual(client.calls[0][0], window)
        ids = {m.msg_id for m in result.messages}
        self.assertEqual(ids, {"1", "2"})

    async def test_no_watermark_falls_back_to_backfill_hours(self):
        original = config.backfill_hours
        config.backfill_hours = 24
        try:
            now = int(time.time())
            client = _QqFlowClient([_qqflow_msg(1, now - 10)])

            await qq_poll(client, _enabled("qqflow", "g1"), _no_processed)

            start = client.calls[0][0]
            self.assertIsNotNone(start)
            self.assertAlmostEqual(start, now - 24 * 3600, delta=5)
        finally:
            config.backfill_hours = original


class SessionWindowComputationTest(unittest.IsolatedAsyncioTestCase):
    """_compute_session_windows：会话水位 / 未处理消息按会话钉窗 / 回填起点。"""

    async def asyncSetUp(self):
        self.db = await aiosqlite.connect(":memory:")
        self.db.row_factory = aiosqlite.Row
        await init_schema(self.db)
        self._db_patch = patch("briefdesk.db.get_db", new=AsyncMock(return_value=self.db))
        self._db_patch.start()
        self._overlap = config.poll_overlap_seconds
        self._hours = config.backfill_hours
        config.poll_overlap_seconds = 300
        config.backfill_hours = 24
        # 建两个启用会话
        for sid in ("g1", "g2"):
            await self.db.execute(
                "INSERT INTO sessions (source, session_id, name, is_group, enabled) "
                "VALUES ('weflow', ?, ?, 1, 1)",
                (sid, sid),
            )
        await self.db.commit()

    async def asyncTearDown(self):
        self._db_patch.stop()
        config.poll_overlap_seconds = self._overlap
        config.backfill_hours = self._hours
        await self.db.close()

    async def _seed_raw(self, session_id: str, msg_id: str, ts: int) -> None:
        await self.db.execute(
            "INSERT INTO raw_messages (source, msg_id, session_id, group_name, "
            "sender_id, sender_name, content, timestamp) "
            "VALUES ('weflow', ?, ?, '群', 'u', 'n', 'x', ?)",
            (msg_id, session_id, ts),
        )
        await self.db.commit()

    def _enabled(self) -> list[SessionInfo]:
        return _enabled("weflow", "g1", "g2")

    async def test_no_state_all_sessions_backfill(self):
        # 无水位会话 → 值 None（源按 BACKFILL_HOURS 回填）
        windows = await _compute_session_windows("weflow", self._enabled())
        self.assertEqual(windows, {"g1": None, "g2": None})

    async def test_watermark_minus_overlap_per_session(self):
        now = int(time.time())
        await update_session_last_polls("weflow", [("g1", now - _DAY), ("g2", now - 7200)])
        windows = await _compute_session_windows("weflow", self._enabled())
        self.assertEqual(windows["g1"], now - _DAY - 300)
        self.assertEqual(windows["g2"], now - 7200 - 300)

    async def test_unprocessed_pins_only_its_own_session(self):
        # g1 有更旧的未处理消息 → 仅 g1 窗口被钉住；g2 水位不受影响
        now = int(time.time())
        await update_session_last_polls("weflow", [("g1", now - 3600), ("g2", now - 3600)])
        await self._seed_raw("g1", "f1", now - 7200)
        windows = await _compute_session_windows("weflow", self._enabled())
        self.assertEqual(windows["g1"], now - 7200 - 300, "有未处理消息的会话以其最久远未处理消息为下界")
        self.assertEqual(windows["g2"], now - 3600 - 300, "完整处理后的会话水位不受影响")

    async def test_unprocessed_resolved_stops_pinning(self):
        now = int(time.time())
        await update_session_last_polls("weflow", [("g1", now - 3600)])
        await self._seed_raw("g1", "f1", now - 7200)
        await mark_message_processed("weflow", "f1")
        windows = await _compute_session_windows("weflow", self._enabled())
        self.assertEqual(windows["g1"], now - 3600 - 300)

    async def test_pull_all_returns_none(self):
        config.backfill_hours = -1
        self.assertIsNone(await _compute_session_windows("weflow", self._enabled()))

    async def test_empty_enabled_returns_empty_dict(self):
        self.assertEqual(await _compute_session_windows("weflow", []), {})

    async def test_db_functions_roundtrip(self):
        now = int(time.time())
        self.assertEqual(
            await get_session_last_polls("weflow", ["g1", "g2"]), {"g1": None, "g2": None}
        )
        await update_session_last_polls("weflow", [("g1", now)])
        self.assertEqual(await get_session_last_polls("weflow", ["g1", "g2"]), {"g1": now, "g2": None})
        await self._seed_raw("g1", "f1", now - 100)
        self.assertEqual(await get_oldest_unprocessed_by_session("weflow"), {"g1": now - 100})


class PollCycleWatermarkTest(unittest.IsolatedAsyncioTestCase):
    """run_poll_cycle 仅在管道正常完成时推进会话水位；
    管道早退（无启用类别/阶段缺失）时跳过推进，防消息永久丢失。"""

    def _source(self) -> Mock:
        source = Mock()
        source.name = "weflow"
        source.client = Mock()
        source.fetch_history = AsyncMock(
            return_value=SimpleNamespace(
                contacts=[], sessions=[], messages=[_weflow_msg("m1", 100)]
            )
        )
        return source

    async def _run(self, pipeline_ok: bool):
        source = self._source()
        with patch(
            "briefdesk.poll_cycle.get_enabled_sessions",
            new=AsyncMock(
                return_value=[
                    {
                        "source": "weflow",
                        "session_id": "g1",
                        "name": "g",
                        "is_group": 1,
                        "is_official": 0,
                    }
                ]
            ),
        ), patch(
            "briefdesk.poll_cycle._compute_session_windows",
            new=AsyncMock(return_value={"g1": 0}),
        ), patch(
            "briefdesk.poll_cycle.process_all_batches",
            new=AsyncMock(return_value=pipeline_ok),
        ), patch(
            "briefdesk.poll_cycle.update_session_last_polls",
            new=AsyncMock(),
        ) as upd:
            await run_poll_cycle(source)
        return upd

    async def test_early_return_skips_watermark(self):
        upd = await self._run(pipeline_ok=False)
        upd.assert_not_awaited()  # 早退：不推进水位

    async def test_success_advances_watermark(self):
        upd = await self._run(pipeline_ok=True)
        upd.assert_awaited_once()  # 正常完成：推进水位
