"""同步进度（新增消息数）功能测试：状态计数、实时事件与管道埋点。

覆盖：
- status.SyncProgress：突发起始/累加/收尾/复位与快照拷贝
- realtime：items_updated 与 sync_progress 两类 SSE 事件分别派发
- pipeline：正常处理计入并收尾、早退路径不计数
- get_status_info：状态聚合携带 syncProgress
"""

import json
import unittest
from unittest.mock import AsyncMock, patch

import aiosqlite

from briefdesk import stages
from briefdesk.config import config
from briefdesk.db import init_schema
from briefdesk.pipeline import process_all_batches
from briefdesk.plugin.base import PluginContext
from briefdesk.plugins.dedup.plugin import DedupPlugin
from briefdesk.realtime import (
    get_dropped_count,
    publish_items_updated,
    publish_sync_progress,
    subscribe,
    unsubscribe,
)
from briefdesk.status import (
    get_status_info,
    get_sync_progress,
    note_sync_batch_done,
    note_sync_batch_start,
    reset_sync_progress,
)
from briefdesk.types import (
    ClassifyOutcome,
    ClassifyResult,
    DedupResult,
)
from tests._helpers import _pipeline_client, _pipeline_msg

# ── 复用的测试小工具（pipeline 消息/客户端构造已收敛至 tests/_helpers）──


async def _noop_async(*args, **kwargs):
    return None


def _noop_sync(*args, **kwargs):
    return None


class _Stage:
    def __init__(self, slot, run, priority=0, before=None, after=None):
        self.slot = slot
        self.priority = priority
        self._run_fn = run
        self._before = before
        self._after = after

    async def run(self, batch, ctx):
        await self._run_fn(batch, ctx)

    async def before_run(self, batch, ctx):
        if self._before is not None:
            await self._before(batch, ctx)

    async def after_run(self, batch, ctx):
        if self._after is not None:
            await self._after(batch, ctx)


def _classify_stage(fn):
    async def run(batch, ctx):
        batch.outcomes = await fn(batch.messages)

    return _Stage("classify", run)


def _outcome_fn(results, failed):
    async def classify(messages):
        return ClassifyOutcome(list(results), list(failed))

    return classify


def _install_dedup_stage(engine):
    plugin = DedupPlugin()
    plugin._engine = engine
    stages.register_stage(plugin)
    return plugin


def _install_merge_stage():
    from briefdesk.plugins.merge.plugin import MergePlugin

    stages.register_stage(MergePlugin())


def _dedup_engine_mock():
    from unittest.mock import Mock

    return Mock(
        preembed_batch=AsyncMock(return_value=None),
        # 用真实契约类型而非 SimpleNamespace：假引擎缺字段应当在此暴露
        check_dedup=AsyncMock(return_value=DedupResult(is_duplicate=False)),
        add_to_cache=Mock(),
        flush_pending_embeddings=AsyncMock(),
    )


# ── status.SyncProgress 状态机 ──


class SyncProgressStateTest(unittest.TestCase):
    def setUp(self):
        reset_sync_progress()

    def tearDown(self):
        reset_sync_progress()

    def test_start_begins_burst(self):
        sp = note_sync_batch_start(3)
        self.assertEqual(
            sp,
            {
                "startedAt": sp["startedAt"],
                "newCount": 3,
                "pendingCount": 3,
                "processedCount": 0,
                "done": False,
            },
        )
        self.assertTrue(sp["startedAt"])

    def test_accumulates_while_pending(self):
        note_sync_batch_start(2)
        sp = note_sync_batch_start(3)
        self.assertEqual(sp["newCount"], 5)
        self.assertEqual(sp["pendingCount"], 5)
        self.assertEqual(sp["processedCount"], 0)
        self.assertFalse(sp["done"])

    def test_done_decrements_and_flags_when_zero(self):
        note_sync_batch_start(3)
        sp = note_sync_batch_done(1)
        self.assertEqual((sp["pendingCount"], sp["processedCount"]), (2, 1))
        self.assertFalse(sp["done"])
        sp = note_sync_batch_done(2)
        self.assertEqual((sp["pendingCount"], sp["processedCount"]), (0, 3))
        self.assertTrue(sp["done"])

    def test_new_burst_after_done_resets_counts(self):
        note_sync_batch_start(3)
        note_sync_batch_done(3)
        first = get_sync_progress()["startedAt"]
        sp = note_sync_batch_start(1)
        self.assertEqual(sp["newCount"], 1)
        self.assertEqual(sp["processedCount"], 0)
        self.assertFalse(sp["done"])
        self.assertNotEqual(sp["startedAt"], first, "新突发更新开始时间")

    def test_done_clamps_negative_pending(self):
        note_sync_batch_done(99)
        sp = get_sync_progress()
        self.assertEqual(sp["pendingCount"], 0)

    def test_snapshot_is_copy(self):
        note_sync_batch_start(2)
        snap = get_sync_progress()
        snap["newCount"] = 999
        self.assertEqual(get_sync_progress()["newCount"], 2)

    def test_status_info_carries_sync_progress(self):
        note_sync_batch_start(5)
        status = get_status_info()
        sp = status["syncProgress"]
        self.assertEqual(sp["newCount"], 5)
        self.assertEqual(sp["pendingCount"], 5)


# ── realtime：两类事件分别派发 ──


class RealtimePublishEventTest(unittest.IsolatedAsyncioTestCase):
    async def _subscribe_one(self):
        q = await subscribe()
        return q

    async def test_items_updated_event_name(self):
        q = await self._subscribe_one()
        try:
            await publish_items_updated({"x": 1})
            name, data = await q.get()
            self.assertEqual(name, "items_updated")
            self.assertEqual(json.loads(data), {"x": 1})
        finally:
            await unsubscribe(q)

    async def test_sync_progress_event_name_and_payload(self):
        q = await self._subscribe_one()
        try:
            await publish_sync_progress(
                {"newCount": 7, "pendingCount": 3, "processedCount": 4, "done": False}
            )
            name, data = await q.get()
            self.assertEqual(name, "sync_progress")
            payload = json.loads(data)
            self.assertEqual(payload["newCount"], 7)
            self.assertEqual(payload["pendingCount"], 3)
        finally:
            await unsubscribe(q)

    async def test_unsubscribed_queue_gets_nothing(self):
        q = await self._subscribe_one()
        await unsubscribe(q)
        await publish_sync_progress({"newCount": 1, "pendingCount": 1})
        self.assertTrue(q.empty())


class RealtimeDropCounterTest(unittest.IsolatedAsyncioTestCase):
    """审查修复 #9：SSE 订阅队列满丢弃事件必须可观测（累计计数器）。"""

    async def test_full_queue_drop_increments_counter(self):
        before = get_dropped_count()
        q = await subscribe()
        try:
            for i in range(32):  # 队列容量 maxsize=32
                q.put_nowait("e" + str(i))
            await publish_items_updated({"k": 1})  # 满 → 丢弃
            await publish_items_updated({"k": 2})  # 满 → 丢弃
            self.assertEqual(q.qsize(), 32)
            self.assertEqual(get_dropped_count() - before, 2)
        finally:
            await unsubscribe(q)

    async def test_non_full_queue_does_not_increment(self):
        before = get_dropped_count()
        q = await subscribe()
        try:
            await publish_items_updated({"k": 1})
            self.assertEqual(q.qsize(), 1)
            self.assertEqual(get_dropped_count(), before)
        finally:
            await unsubscribe(q)


# ── pipeline 埋点：正常处理计数、早退路径不计数 ──


class PipelineSyncProgressCountsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        stages.reset()
        reset_sync_progress()
        self.ctx = PluginContext(
            config=config,
            publish_event=_noop_async,
            subscribe_event=_noop_sync,
            register_source=_noop_sync,
            register_stage=stages.register_stage,
        )
        stages.set_context(self.ctx)
        self.db = await aiosqlite.connect(":memory:")
        self.db.row_factory = aiosqlite.Row
        await self.db.execute("PRAGMA foreign_keys = ON")
        await init_schema(self.db)

    async def asyncTearDown(self):
        reset_sync_progress()
        await self.db.close()
        stages.reset()

    async def _process(self, messages, categories, install_stages=None):
        install_stages = install_stages or (lambda: None)
        install_stages()
        with patch(
            "briefdesk.db.get_db", new=AsyncMock(return_value=self.db)
        ), patch(
            "briefdesk.pipeline.get_enabled_sessions",
            new=AsyncMock(return_value=[{"session_id": "s"}]),
        ), patch(
            "briefdesk.pipeline.get_enabled_categories",
            new=AsyncMock(return_value=categories),
        ), patch(
            "briefdesk.pipeline.are_messages_processed",
            new=AsyncMock(return_value=set()),
        ), patch(
            "briefdesk.pipeline.bulk_insert_raw_messages", new=AsyncMock()
        ), patch(
            "briefdesk.plugins.merge.plugin.get_merge_candidates",
            new=AsyncMock(return_value=[]),
        ), patch(
            "briefdesk.pipeline.publish_items_updated", new=AsyncMock()
        ), patch(
            "briefdesk.plugins.dedup.plugin.insert_item",
            new=AsyncMock(return_value="fake-id"),
        ), patch(
            "briefdesk.plugins.dedup.plugin.mark_message_processed",
            new=AsyncMock(),
        ), patch(
            "briefdesk.pipeline.mark_messages_processed", new=AsyncMock()
        ):
            return await process_all_batches(
                messages, _pipeline_client(), batch_size=10, origin="test"
            )

    def _normal_stages(self, count):
        def install():
            _install_dedup_stage(_dedup_engine_mock())
            _install_merge_stage()
            results = [ClassifyResult(msg_index=0, category="x", summary="t", quote="q")]
            stages.register_stage(
                _classify_stage(_outcome_fn(results, list(range(1, count))))
            )

        return install

    async def test_normal_path_counts_all_and_done(self):
        ok = await self._process(
            [_pipeline_msg("m1"), _pipeline_msg("m2"), _pipeline_msg("m3")],
            [{"name": "x"}],
            install_stages=self._normal_stages(3),
        )
        self.assertTrue(ok)
        sp = get_sync_progress()
        self.assertEqual(sp["newCount"], 3)
        self.assertEqual(sp["pendingCount"], 0)
        self.assertEqual(sp["processedCount"], 3)
        self.assertTrue(sp["done"])
        self.assertTrue(sp["startedAt"])

    async def test_early_return_does_not_count(self):
        # 无启用类别 → 早退：消息未在推进，不计数（避免"永不完成"）
        ok = await self._process([_pipeline_msg("m1")], [])
        self.assertFalse(ok)
        sp = get_sync_progress()
        self.assertEqual(sp["newCount"], 0)
        self.assertEqual(sp["pendingCount"], 0)

    async def test_missing_stages_does_not_count(self):
        ok = await self._process([_pipeline_msg("m1")], [{"name": "x"}])
        self.assertFalse(ok)
        sp = get_sync_progress()
        self.assertEqual(sp["newCount"], 0)


if __name__ == "__main__":
    unittest.main()
