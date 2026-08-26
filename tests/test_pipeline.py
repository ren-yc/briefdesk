"""pipeline 骨架与阶段插件测试：不触发真实 AI，DB 用内存库/桩。"""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import aiosqlite

from briefdesk import stages
from briefdesk.config import config
from briefdesk.db import init_schema
from briefdesk.pipeline import _split_batches, process_all_batches
from briefdesk.plugin.base import PluginContext
from briefdesk.plugins.dedup.engine import build_item_input
from briefdesk.plugins.dedup.plugin import DedupPlugin
from briefdesk.plugins.merge.engine import (
    _merge_image_urls,
    _merge_key_info,
    _merge_quote,
    _merge_time_points,
    _parse_extra_json,
)
from briefdesk.plugins.merge.plugin import MergePlugin
from briefdesk.plugins.ocr.plugin import OcrPlugin
from briefdesk.sources_base import MediaError
from briefdesk.types import (
    BatchContext,
    ClassifyOutcome,
    ClassifyResult,
    InsertedRow,
    InternalMessage,
)


async def _noop_async(*args, **kwargs):
    return None


def _noop_sync(*args, **kwargs):
    return None


class _Stage:
    """最小 StagePlugin 假实现（结构化类型，无需继承）。"""

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
    """包装 classify 函数（messages -> ClassifyOutcome）为 classify 槽位阶段。"""

    async def run(batch, ctx):
        batch.outcomes = await fn(batch.messages)

    return _Stage("classify", run)


def _install_dedup_stage(engine):
    """注册真实 DedupPlugin（可注入假引擎；None = 引擎未就绪路径）。"""
    plugin = DedupPlugin()
    plugin._engine = engine
    stages.register_stage(plugin)
    return plugin


def _install_merge_stage():
    plugin = MergePlugin()
    stages.register_stage(plugin)
    return plugin


def _dedup_engine_mock():
    """行为与 DedupEngine 端口一致的最小假引擎。"""
    return Mock(
        preembed_batch=AsyncMock(return_value=None),
        check_dedup=AsyncMock(return_value=SimpleNamespace(is_duplicate=False)),
        add_to_cache=Mock(),
        flush_pending_embeddings=AsyncMock(),
    )


class _StageTestBase(unittest.IsolatedAsyncioTestCase):
    """阶段测试基座：注册表隔离 + 装配期上下文。"""

    async def asyncSetUp(self):
        stages.reset()
        self.ctx = PluginContext(
            config=config,
            publish_event=_noop_async,
            subscribe_event=_noop_sync,
            register_source=_noop_sync,
            register_stage=stages.register_stage,
        )
        stages.set_context(self.ctx)

    async def asyncTearDown(self):
        stages.reset()


def _pipeline_msg(mid, content="c", session_id="s", ts=1, is_self=False):
    return InternalMessage(
        msg_id=mid,
        content=content,
        sender_name="A",
        sender_id="u",
        session_id=session_id,
        group_name="g",
        timestamp=ts,
        source="weflow",
        is_self=is_self,
    )


def _pipeline_client(name="weflow"):
    c = Mock()
    c.name = name
    c.download_media = AsyncMock(return_value=b"x")
    return c


class BuildItemInputTest(unittest.TestCase):
    def _msg(self):
        return InternalMessage(
            msg_id="m1",
            content="原始内容",
            sender_name="Alice",
            sender_id="uid1",
            session_id="s1",
            group_name="group",
            timestamp=123456,
            source="weflow",
            image_urls=["a.jpg"],
        )

    def test_content_hash_stable_and_nonempty(self):
        result = ClassifyResult(
            msg_index=0, category="学术", summary="标题", quote="内容"
        )
        item = build_item_input(self._msg(), result, "标题")
        self.assertTrue(item["content_hash"])
        self.assertEqual(len(item["content_hash"]), 16)
        self.assertEqual(
            item["content_hash"],
            build_item_input(self._msg(), result, "标题")["content_hash"],
        )

    def test_image_urls_serialized(self):
        result = ClassifyResult(msg_index=0, category="学术")
        item = build_item_input(self._msg(), result, "标题")
        self.assertEqual(item["image_urls"], '["a.jpg"]')

    def test_sender_fields_preserved(self):
        result = ClassifyResult(msg_index=0, category="学术")
        item = build_item_input(self._msg(), result, "标题")
        self.assertEqual(item["sender_name"], "Alice")
        self.assertEqual(item["source_msg_id"], "m1")

    def test_extra_times_serialized(self):
        # 多时间点以 JSON 存储（单条消息含多个截止日，如工作提醒）
        result = ClassifyResult(
            msg_index=0, category="活动通知", end="2026-07-31",
            extra_times=[
                {"type": "end", "time": "2026-08-15", "label": "部门宣传视频"},
                {"type": "end", "time": "2026-08-20", "label": "部门文字宣传稿"},
            ],
        )
        item = build_item_input(self._msg(), result, "标题")
        import json as _json
        self.assertEqual(_json.loads(item["extra_times"]), [
            {"type": "end", "time": "2026-08-15", "label": "部门宣传视频"},
            {"type": "end", "time": "2026-08-20", "label": "部门文字宣传稿"},
        ])
        # 无多时间点时存空串
        item2 = build_item_input(self._msg(), ClassifyResult(msg_index=0, category="学术"), "t")
        self.assertEqual(item2["extra_times"], "")

    def test_subject_normalized_at_write(self):
        # subject 写时归一化（NFKC+小写+空白折叠），展示与时间线匹配共用
        result = ClassifyResult(msg_index=0, category="学术", subject="ＡＣＭ社 ")
        item = build_item_input(self._msg(), result, "标题")
        self.assertEqual(item["subject"], "acm社")

    def test_subject_none_stays_none(self):
        result = ClassifyResult(msg_index=0, category="学术", subject="")
        item = build_item_input(self._msg(), result, "标题")
        self.assertIsNone(item["subject"])

    def test_source_quote_uses_full_content_not_ai_quote(self):
        # 原文引用展示完整原文（含 [OCR] 前缀的识别文本），而非 AI 摘要式 quote
        result = ClassifyResult(msg_index=0, category="学术", quote="截断的摘要")
        item = build_item_input(self._msg(), result, "标题")
        self.assertEqual(item["source_quote"], "原始内容")


class SplitBatchesTest(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(_split_batches([], 2), [])

    def test_exact(self):
        self.assertEqual([len(b) for b in _split_batches([1, 2, 3, 4], 2)], [2, 2])

    def test_remainder(self):
        self.assertEqual([len(b) for b in _split_batches([1, 2, 3], 2)], [2, 1])


class OcrEnrichTest(unittest.IsolatedAsyncioTestCase):
    """OCR 阶段（OcrPlugin.run）的异常隔离：单条 OCR 失败不拖垮整批。"""

    def _msg(self, msg_id="m1"):
        return InternalMessage(
            msg_id=msg_id,
            content="原文内容",
            sender_name="Alice",
            sender_id="uid1",
            session_id="s1",
            group_name="group",
            timestamp=123456,
            source="weflow",
            image_urls=["a.jpg"],
        )

    def _client(self, media: bytes = b"img"):
        client = Mock()
        client.download_media = AsyncMock(return_value=media)
        return client

    async def _run(self, msg, client, ocr_mock):
        plugin = OcrPlugin()
        plugin._ocr_image_bytes = ocr_mock
        bctx = BatchContext(messages=[msg], client=client)
        ctx = PluginContext(
            config=config,
            publish_event=_noop_async,
            subscribe_event=_noop_sync,
            register_source=_noop_sync,
            register_stage=_noop_sync,
        )
        await plugin.run(bctx, ctx)

    async def test_ocr_failure_keeps_original_content(self):
        # OCR 引擎异常只跳过该条，不抛出、不改变原文
        msg = self._msg()
        await self._run(
            msg,
            self._client(),
            AsyncMock(side_effect=RuntimeError("engine broken")),
        )
        self.assertEqual(msg.content, "原文内容")

    async def test_media_error_keeps_original_content(self):
        # 图片下载失败（MediaError）同样只跳过 OCR
        msg = self._msg()
        client = Mock()
        client.download_media = AsyncMock(side_effect=MediaError("404"))
        await self._run(msg, client, AsyncMock(return_value="识别文字"))
        self.assertEqual(msg.content, "原文内容")

    async def test_ocr_success_replaces_content_with_prefix(self):
        # 以源码为准：识别文本以 [OCR] 前缀替换（而非追加）content
        msg = self._msg()
        await self._run(msg, self._client(), AsyncMock(return_value="识别文字"))
        self.assertEqual(msg.content, "[OCR]\n识别文字")

    async def test_no_images_skips_download(self):
        msg = self._msg()
        msg.image_urls = []
        client = self._client()
        await self._run(msg, client, AsyncMock(return_value="识别文字"))
        client.download_media.assert_not_called()


class StoreBatchFailedTest(_StageTestBase):
    """骨架 + dedup 阶段：failed index 的消息不标记 processed（本轮抛弃、下轮回填）。"""

    async def _store(self, results, failed):
        batch = [_pipeline_msg("m0"), _pipeline_msg("m1"), _pipeline_msg("m2")]
        processed: list[str] = []

        async def fake_mark(source, msg_id):
            processed.append(msg_id)

        _install_dedup_stage(_dedup_engine_mock())
        _install_merge_stage()
        stages.register_stage(_classify_stage(_outcome_fn(results, failed)))

        with patch(
            "briefdesk.plugins.dedup.plugin.mark_message_processed",
            new=AsyncMock(side_effect=fake_mark),
        ), patch(
            "briefdesk.pipeline.mark_message_processed",
            new=AsyncMock(side_effect=fake_mark),  # 骨架 _mark_skipped 走 pipeline 名
        ), patch(
            "briefdesk.plugins.dedup.plugin.insert_item",
            new=AsyncMock(return_value="new-id"),
        ), patch(
            "briefdesk.plugins.merge.plugin.get_merge_candidates",
            new=AsyncMock(return_value=[]),
        ), patch(
            "briefdesk.pipeline.get_enabled_sessions",
            new=AsyncMock(return_value=[{"session_id": "s"}]),
        ), patch(
            "briefdesk.pipeline.get_enabled_categories",
            new=AsyncMock(return_value=[{"name": "x"}]),
        ), patch(
            "briefdesk.pipeline.are_messages_processed", new=AsyncMock(return_value=set())
        ), patch(
            "briefdesk.pipeline.bulk_insert_raw_messages", new=AsyncMock()
        ), patch("briefdesk.pipeline.publish_items_updated", new=AsyncMock()):
            await process_all_batches(
                batch, _pipeline_client(), batch_size=10, origin="test"
            )
        return processed

    async def test_failed_not_marked_with_partial_results(self):
        r = ClassifyResult(msg_index=0, category="活动通知", summary="s", quote="q")
        processed = await self._store([r], [1])
        self.assertEqual(processed, ["m0", "m2"])  # m1 未标记 processed

    async def test_failed_not_marked_when_results_empty(self):
        processed = await self._store([], [1])
        self.assertEqual(processed, ["m0", "m2"])


class MissingStageGuardTest(_StageTestBase):
    """分类/去重阶段缺失（插件被禁用）时整批保留：不标记 processed（防永久丢失）。"""

    async def _run(self, install_stages):
        processed: list[str] = []

        async def fake_mark(source, msg_id):
            processed.append(msg_id)

        install_stages()
        with patch(
            "briefdesk.pipeline.get_enabled_sessions",
            new=AsyncMock(return_value=[{"session_id": "s"}]),
        ), patch(
            "briefdesk.pipeline.get_enabled_categories",
            new=AsyncMock(return_value=[{"name": "x"}]),
        ), patch(
            "briefdesk.pipeline.are_messages_processed", new=AsyncMock(return_value=set())
        ), patch(
            "briefdesk.pipeline.bulk_insert_raw_messages", new=AsyncMock()
        ), patch(
            "briefdesk.pipeline.mark_message_processed",
            new=AsyncMock(side_effect=fake_mark),
        ), patch("briefdesk.pipeline.publish_items_updated", new=AsyncMock()):
            await process_all_batches(
                [_pipeline_msg("m1")], _pipeline_client(), batch_size=10, origin="test"
            )
        return processed

    async def test_no_classify_stage_keeps_batch(self):
        def install():
            _install_dedup_stage(_dedup_engine_mock())
            _install_merge_stage()

        processed = await self._run(install)
        self.assertEqual(processed, [])  # 未标记：回填窗口内自动重试

    async def test_no_dedup_stage_keeps_batch(self):
        def install():
            stages.register_stage(
                _classify_stage(
                    _outcome_fn(
                        [ClassifyResult(msg_index=0, category="x", summary="t")], []
                    )
                )
            )

        processed = await self._run(install)
        self.assertEqual(processed, [])


class ProcessAllBatchesReturnTest(_StageTestBase):
    """process_all_batches 返回标志——早退（未落 raw）返回 False，
    调用方（poll_cycle）据此跳过水位推进，防消息永久丢失。"""

    async def _run(self, categories, install_stages=None):
        install_stages = install_stages or (lambda: None)
        install_stages()
        # 正常路径 merge 阶段会查询 items（get_merge_candidates）：注入可用内存库，
        # 避免触碰应用库/被前序测试关闭的 get_db 单例
        db = await aiosqlite.connect(":memory:")
        db.row_factory = aiosqlite.Row
        await init_schema(db)
        try:
            with patch(
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
                "briefdesk.pipeline.publish_items_updated", new=AsyncMock()
            ), patch(
                "briefdesk.pipeline.mark_message_processed", new=AsyncMock()
            ), patch(
                "briefdesk.plugins.dedup.plugin.insert_item",
                new=AsyncMock(return_value="fake-id"),
            ), patch(
                "briefdesk.plugins.dedup.plugin.mark_message_processed", new=AsyncMock()
            ), patch(
                "briefdesk.db.get_db", new=AsyncMock(return_value=db)
            ):
                return await process_all_batches(
                    [_pipeline_msg("m1")],
                    _pipeline_client(),
                    batch_size=10,
                    origin="test",
                )
        finally:
            await db.close()

    async def test_no_categories_returns_false(self):
        # 无启用类别 → 早退（不落 raw、不标 processed）→ 返回 False
        ok = await self._run(categories=[])
        self.assertFalse(ok)

    async def test_missing_stages_returns_false(self):
        # 阶段插件缺失 → 早退 → 返回 False
        ok = await self._run(categories=[{"name": "x"}])
        self.assertFalse(ok)

    async def test_normal_path_returns_true(self):
        # 正常处理（dedup 判非重复入库）→ 返回 True（可推进水位）
        def install():
            _install_dedup_stage(_dedup_engine_mock())
            _install_merge_stage()
            stages.register_stage(
                _classify_stage(
                    _outcome_fn(
                        [ClassifyResult(msg_index=0, category="x", summary="t")], []
                    )
                )
            )

        ok = await self._run(categories=[{"name": "x"}], install_stages=install)
        self.assertTrue(ok)


def _outcome_fn(results, failed):
    async def classify(messages):
        return ClassifyOutcome(list(results), list(failed))

    return classify


class ZeroOutputStatusTest(_StageTestBase):
    """零产出（全部失败）不刷新 lastSync/lastError，避免前端误报同步成功。"""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.db = await aiosqlite.connect(":memory:")
        self.db.row_factory = aiosqlite.Row
        await self.db.execute("PRAGMA foreign_keys = ON")
        await init_schema(self.db)

    async def asyncTearDown(self):
        await super().asyncTearDown()
        await self.db.close()

    async def _run(self, classify):
        status_calls = []
        _install_dedup_stage(_dedup_engine_mock())
        _install_merge_stage()
        stages.register_stage(_classify_stage(classify))
        with patch("briefdesk.db.get_db", new=AsyncMock(side_effect=lambda: self.db)), patch(
            "briefdesk.pipeline.get_enabled_sessions",
            new=AsyncMock(return_value=[{"session_id": "s1"}]),
        ), patch(
            "briefdesk.pipeline.get_enabled_categories",
            new=AsyncMock(return_value=[{"name": "x"}]),
        ), patch(
            "briefdesk.pipeline.are_messages_processed", new=AsyncMock(return_value=set())
        ), patch(
            "briefdesk.plugins.merge.plugin.get_merge_candidates",
            new=AsyncMock(return_value=[]),
        ), patch(
            "briefdesk.pipeline.set_status", side_effect=lambda d: status_calls.append(d)
        ), patch("briefdesk.pipeline.publish_items_updated", new=AsyncMock()):
            await process_all_batches(
                [
                    _pipeline_msg("m1", session_id="s1", ts=1),
                    _pipeline_msg("m2", session_id="s1", ts=2),
                ],
                _pipeline_client(),
                origin="test",
            )
        return status_calls

    async def test_zero_output_keeps_status(self):
        async def classify_all_failed(messages):
            return ClassifyOutcome([], list(range(len(messages))))

        status_calls = await self._run(classify_all_failed)
        self.assertEqual(status_calls, [])  # 不刷新 lastSync、不清 lastError

    async def test_all_skipped_refreshes_status(self):
        # 全部识别为闲聊（results/failed 均空）属正常成功：应刷新 lastSync，
        # 不误报"零产出"
        async def classify_all_skipped(messages):
            return ClassifyOutcome([], [])

        status_calls = await self._run(classify_all_skipped)
        self.assertEqual(len(status_calls), 1)
        self.assertTrue(status_calls[0]["lastSync"])
        self.assertEqual(status_calls[0]["lastError"], "")

    async def test_partial_output_refreshes_status(self):
        async def classify_one_ok(messages):
            return ClassifyOutcome(
                [ClassifyResult(msg_index=0, category="活动通知", summary="t", quote="q")],
                [1],
            )

        status_calls = await self._run(classify_one_ok)
        self.assertEqual(len(status_calls), 1)
        self.assertTrue(status_calls[0]["lastSync"])
        self.assertEqual(status_calls[0]["lastError"], "")


class IgnoreSelfFilterTest(_StageTestBase):
    """IGNORE_SELF 入口过滤：is_self 消息在管道入口被丢弃，不进分类也不落 raw。"""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.db = await aiosqlite.connect(":memory:")
        self.db.row_factory = aiosqlite.Row
        await self.db.execute("PRAGMA foreign_keys = ON")
        await init_schema(self.db)

    async def asyncTearDown(self):
        await super().asyncTearDown()
        await self.db.close()

    async def _run(self, messages, classify, raw_rows):
        _install_dedup_stage(None)  # 引擎未就绪路径：结果为空时全程无副作用
        stages.register_stage(_classify_stage(classify))
        with patch("briefdesk.db.get_db", new=AsyncMock(side_effect=lambda: self.db)), patch(
            "briefdesk.pipeline.get_enabled_sessions",
            new=AsyncMock(return_value=[{"session_id": "s1"}]),
        ), patch(
            "briefdesk.pipeline.get_enabled_categories",
            new=AsyncMock(return_value=[{"name": "x"}]),
        ), patch(
            "briefdesk.pipeline.are_messages_processed", new=AsyncMock(return_value=set())
        ), patch(
            "briefdesk.pipeline.bulk_insert_raw_messages",
            new=AsyncMock(
                side_effect=lambda rows: raw_rows.extend(r["msg_id"] for r in rows)
            ),
        ), patch("briefdesk.pipeline.set_status"), patch(
            "briefdesk.pipeline.publish_items_updated", new=AsyncMock()
        ):
            await process_all_batches(messages, _pipeline_client(), origin="test")

    async def test_ignore_self_on_drops_self_messages(self):
        seen = []

        async def classify(messages):
            seen.append([m.msg_id for m in messages])
            return ClassifyOutcome([], [])

        raw_rows = []
        with patch.object(config, "ignore_self", True), patch.object(
            config, "realtime_batch_max_count", 10
        ):
            await self._run(
                [
                    _pipeline_msg("m1", session_id="s1", is_self=True),
                    _pipeline_msg("m2", session_id="s1"),
                ],
                classify,
                raw_rows,
            )
        self.assertEqual(seen, [["m2"]], "自消息不进分类")
        self.assertEqual(raw_rows, ["m2"], "自消息不落 raw")

    async def test_ignore_self_off_keeps_all(self):
        seen = []

        async def classify(messages):
            seen.append([m.msg_id for m in messages])
            return ClassifyOutcome([], [])

        raw_rows = []
        with patch.object(config, "ignore_self", False), patch.object(
            config, "realtime_batch_max_count", 10
        ):
            await self._run(
                [
                    _pipeline_msg("m1", session_id="s1", is_self=True),
                    _pipeline_msg("m2", session_id="s1"),
                ],
                classify,
                raw_rows,
            )
        self.assertEqual(seen, [["m1", "m2"]])
        self.assertEqual(raw_rows, ["m1", "m2"])

    async def test_all_self_short_circuits(self):
        classify = AsyncMock()
        raw_rows = []
        with patch.object(config, "ignore_self", True):
            await self._run(
                [_pipeline_msg("m1", session_id="s1", is_self=True)],
                classify,
                raw_rows,
            )
        classify.assert_not_called()
        self.assertEqual(raw_rows, [])


class ImageFilterWhenNoEnrichTest(_StageTestBase):
    """OCR 未启用（enrich 槽位为空）时，纯占位符图片消息在入口被屏蔽：
    不落 raw、不进分类、不标记 processed；图片+文字混合消息（content 非
    占位符）与启用 OCR 时（enrich 非空）不受影响。"""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.db = await aiosqlite.connect(":memory:")
        self.db.row_factory = aiosqlite.Row
        await self.db.execute("PRAGMA foreign_keys = ON")
        await init_schema(self.db)

    async def asyncTearDown(self):
        await super().asyncTearDown()
        await self.db.close()

    async def _run(self, messages, classify, raw_rows, install_enrich=False):
        if install_enrich:
            async def _noop_stage_run(batch, ctx):
                return None

            stages.register_stage(_Stage("enrich", _noop_stage_run))
        _install_dedup_stage(None)  # 引擎未就绪路径：结果为空时全程无副作用
        stages.register_stage(_classify_stage(classify))
        with patch("briefdesk.db.get_db", new=AsyncMock(side_effect=lambda: self.db)), patch(
            "briefdesk.pipeline.get_enabled_sessions",
            new=AsyncMock(return_value=[{"session_id": "s1"}]),
        ), patch(
            "briefdesk.pipeline.get_enabled_categories",
            new=AsyncMock(return_value=[{"name": "x"}]),
        ), patch(
            "briefdesk.pipeline.are_messages_processed", new=AsyncMock(return_value=set())
        ), patch(
            "briefdesk.pipeline.bulk_insert_raw_messages",
            new=AsyncMock(
                side_effect=lambda rows: raw_rows.extend(r["msg_id"] for r in rows)
            ),
        ), patch("briefdesk.pipeline.set_status"), patch(
            "briefdesk.pipeline.publish_items_updated", new=AsyncMock()
        ):
            await process_all_batches(messages, _pipeline_client(), origin="test")

    @staticmethod
    def _img_msg(mid, content="[图片]", **kw):
        m = _pipeline_msg(mid, content=content, session_id="s1", **kw)
        m.image_urls = ["a.jpg"]
        return m

    async def test_placeholder_image_filtered_without_enrich(self):
        seen = []

        async def classify(messages):
            seen.append([m.msg_id for m in messages])
            return ClassifyOutcome([], [])

        raw_rows = []
        await self._run(
            [self._img_msg("m1"), _pipeline_msg("m2", session_id="s1")],
            classify,
            raw_rows,
        )
        self.assertEqual(seen, [["m2"]], "纯占位符图片消息不进分类")
        self.assertEqual(raw_rows, ["m2"], "纯占位符图片消息不落 raw")

    async def test_mixed_image_text_kept_without_enrich(self):
        seen = []

        async def classify(messages):
            seen.append([m.msg_id for m in messages])
            return ClassifyOutcome([], [])

        raw_rows = []
        with patch.object(config, "realtime_batch_max_count", 10):
            await self._run(
                [self._img_msg("m1", content="123"), _pipeline_msg("m2", session_id="s1")],
                classify,
                raw_rows,
            )
        self.assertEqual(seen, [["m1", "m2"]], "图片+文字混合消息照常处理")
        self.assertEqual(raw_rows, ["m1", "m2"])

    async def test_multi_placeholder_image_filtered_without_enrich(self):
        # "[图片][图片]" 多片段占位拼接同样视为纯占位符图片消息（正则重复形）
        seen = []

        async def classify(messages):
            seen.append([m.msg_id for m in messages])
            return ClassifyOutcome([], [])

        raw_rows = []
        await self._run(
            [
                self._img_msg("m1", content="[图片][图片]"),
                _pipeline_msg("m2", session_id="s1"),
            ],
            classify,
            raw_rows,
        )
        self.assertEqual(seen, [["m2"]], "多片段占位图片消息不进分类")
        self.assertEqual(raw_rows, ["m2"], "多片段占位图片消息不落 raw")

    async def test_mixed_placeholder_text_kept_without_enrich(self):
        # "[图片] 说明文字" 混合消息不受屏蔽（文字仍有信息价值）
        seen = []

        async def classify(messages):
            seen.append([m.msg_id for m in messages])
            return ClassifyOutcome([], [])

        raw_rows = []
        with patch.object(config, "realtime_batch_max_count", 10):
            await self._run(
                [
                    self._img_msg("m1", content="[图片] 这是说明"),
                    _pipeline_msg("m2", session_id="s1"),
                ],
                classify,
                raw_rows,
            )
        self.assertEqual(seen, [["m1", "m2"]], "占位符+文字混合消息照常处理")
        self.assertEqual(raw_rows, ["m1", "m2"])

    async def test_placeholder_image_kept_with_enrich(self):
        # OCR 启用（enrich 槽位非空）时纯占位符图片消息不屏蔽：交由 OCR 阶段识别
        seen = []

        async def classify(messages):
            seen.append([m.msg_id for m in messages])
            return ClassifyOutcome([], [])

        raw_rows = []
        await self._run(
            [self._img_msg("m1")], classify, raw_rows, install_enrich=True
        )
        self.assertEqual(seen, [["m1"]])
        self.assertEqual(raw_rows, ["m1"])

    async def test_all_images_filtered_short_circuits(self):
        classify = AsyncMock()
        raw_rows = []
        await self._run([self._img_msg("m1")], classify, raw_rows)
        classify.assert_not_called()
        self.assertEqual(raw_rows, [])


class MergeFieldHelpersTest(unittest.TestCase):
    """合并字段拼接纯函数：quote 按行去重、key 去重、图片并集。"""

    def test_merge_quote_dedupes_lines(self):
        q = _merge_quote(["塔卡沙团购", "45元\n塔卡沙团购", "", "面交"])
        self.assertEqual(q, "塔卡沙团购\n45元\n面交")

    def test_merge_key_info_case_insensitive_dedupe(self):
        k = _merge_key_info(["45, 运费AA", "运费aa, 面交"])
        self.assertEqual(k, "45, 运费AA, 面交")

    def test_merge_image_urls_union(self):
        u = _merge_image_urls(['["a.jpg", "b.jpg"]', '["b.jpg", "c.jpg"]', ""])
        self.assertEqual(u, '["a.jpg", "b.jpg", "c.jpg"]')
        self.assertEqual(_merge_image_urls(["", ""]), "")

    def test_merge_time_points_earliest_primary_others_structured(self):
        # 两个截止日：主值取最早，第二个以结构化条目进 extra_times（防丢失）
        start, end, rest = _merge_time_points(
            [("end", "2026-08-25"), ("end", "2026-08-24")], []
        )
        self.assertEqual((start, end), ("", "2026-08-24"))
        self.assertEqual(rest, [{"type": "end", "time": "2026-08-25", "label": ""}])
        # 两个开始时间同理
        start, end, rest = _merge_time_points(
            [("start", "2026-08-26 19:00"), ("start", "2026-08-25 14:00")], []
        )
        self.assertEqual((start, end), ("2026-08-25 14:00", ""))
        self.assertEqual(rest, [{"type": "start", "time": "2026-08-26 19:00", "label": ""}])

    def test_merge_time_points_dedupe_and_mixed_format_order(self):
        # date-only 视为当日 00:00：字典序即时间序；重复 (type,time) 去重
        start, end, rest = _merge_time_points(
            [("end", "2026-08-25"), ("end", "2026-08-24 23:00")],
            [{"type": "end", "time": "2026-08-25", "label": "部门宣传视频"}],
        )
        self.assertEqual(end, "2026-08-24 23:00")
        self.assertEqual(rest, [{"type": "end", "time": "2026-08-25", "label": "部门宣传视频"}])
        # 全空
        start, end, rest = _merge_time_points([("start", ""), ("end", "")], [])
        self.assertEqual((start, end, rest), ("", "", []))

    def test_merge_time_points_combines_extras_from_both_cards(self):
        # 两卡各自带 extra_times：合并后全部保留（跨卡去重，label 保留首现）
        start, end, rest = _merge_time_points(
            [("end", "2026-07-31")],
            [
                {"type": "end", "time": "2026-08-15", "label": "部门宣传视频"},
                {"type": "end", "time": "2026-08-17", "label": "部门宣传海报"},
            ],
        )
        self.assertEqual((start, end), ("", "2026-07-31"))
        self.assertEqual(rest, [
            {"type": "end", "time": "2026-08-15", "label": "部门宣传视频"},
            {"type": "end", "time": "2026-08-17", "label": "部门宣传海报"},
        ])

    def test_parse_extra_json_tolerates_raw_and_dirty(self):
        raw = '[{"type":"end","time":"2026-08-15","label":"视频"}]'
        self.assertEqual(_parse_extra_json(raw), [
            {"type": "end", "time": "2026-08-15", "label": "视频"},
        ])
        self.assertEqual(_parse_extra_json([{"type": "start", "time": "2026-08-01", "label": None}]), [
            {"type": "start", "time": "2026-08-01", "label": ""},
        ])
        self.assertEqual(_parse_extra_json("not json"), [])
        self.assertEqual(_parse_extra_json(None), [])
        self.assertEqual(_parse_extra_json([{"type": "bad", "time": "x"}]), [])


class ConversationMergeStageTest(unittest.IsolatedAsyncioTestCase):
    """merge 阶段插件（内存 DB + mock 判官）：
    片段折入最早头卡、无关话题保留、提醒卡不参与、窗口/开关语义、缓存同步。
    """

    async def asyncSetUp(self):
        self.db = await aiosqlite.connect(":memory:")
        self.db.row_factory = aiosqlite.Row
        await self.db.execute("PRAGMA foreign_keys = ON")
        await init_schema(self.db)
        self._db_patch = patch("briefdesk.db.get_db", new=AsyncMock(return_value=self.db))
        self._db_patch.start()
        self.dedup_calls: list = []
        self.dedup = Mock(
            remove_items=lambda ids: self.dedup_calls.append(("remove", list(ids))),
            add_to_cache=lambda *a, **k: self.dedup_calls.append(("add", a[0])),
        )
        self.ctx = PluginContext(
            config=config,
            publish_event=_noop_async,
            subscribe_event=_noop_sync,
            register_source=_noop_sync,
            register_stage=_noop_sync,
            dedup=self.dedup,
        )

    async def asyncTearDown(self):
        self._db_patch.stop()
        await self.db.close()

    @staticmethod
    def _msg(mid, ts, content):
        return InternalMessage(
            msg_id=mid,
            content=content,
            sender_name="A",
            sender_id="u",
            session_id="s1",
            group_name="g",
            timestamp=ts,
            source="weflow",
        )

    async def _seed_cand(
        self,
        id,
        ts,
        *,
        title,
        key_info=None,
        quote=None,
        remind_at=None,
        end=None,
        start=None,
        extra_times=None,
        category="交易",
    ):
        await self.db.execute(
            "INSERT INTO items (id, category, title, key_info, source_quote, "
            "source_group, subject, source, source_msg_id, session_id, msg_time, "
            "is_verified, remind_at, end, start, extra_times, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'g', NULL, 'weflow', ?, 's1', ?, 0, ?, ?, ?, ?, '2026-08-01')",
            (id, category, title, key_info, quote or "", id, ts, remind_at, end, start, extra_times or ""),
        )
        # 候选卡的原文行（验证合并删除时保留：片段仍属对话上下文）
        await self.db.execute(
            "INSERT INTO raw_messages (source, msg_id, session_id, group_name, "
            "sender_id, sender_name, content, timestamp) "
            "VALUES ('weflow', ?, 's1', 'g', 'u', 'A', ?, ?)",
            (id, quote or "", ts),
        )
        await self.db.commit()

    async def _insert_new(self, msg, result, title):
        """真实插入新卡（与旧流程 insert_item 一致），返回 item_id。"""
        item_id = f"new-{msg.msg_id}"
        await self.db.execute(
            "INSERT INTO items (id, category, title, key_info, "
            "source_quote, source_group, subject, source, source_msg_id, session_id, "
            "msg_time, is_verified, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'g', NULL, 'weflow', ?, 's1', ?, 0, '2026-08-01')",
            (
                item_id,
                result.category,
                title,
                result.key_info or "",
                result.quote or msg.content,
                msg.msg_id,
                msg.timestamp,
            ),
        )
        await self.db.commit()
        return item_id

    async def _store(self, msg, result, judge_result, patch_judge=None, new_title=None):
        judge_mock = patch_judge or AsyncMock(return_value=judge_result)
        plugin = MergePlugin()
        title = result.summary or msg.content[:50]
        item_id = await self._insert_new(msg, result, title)
        bctx = BatchContext(
            messages=[msg],
            client=Mock(),
            inserted=[
                InsertedRow(
                    item_id=item_id,
                    msg=msg,
                    result=result,
                    title=title,
                )
            ],
        )
        with patch("briefdesk.plugins.merge.plugin.judge_merge", new=judge_mock), patch(
            "briefdesk.plugins.merge.plugin.summarize_title",
            new=AsyncMock(return_value=new_title),
        ):
            await plugin.run(bctx, self.ctx)
        return bctx.merged, judge_mock

    async def test_folds_new_card_into_earlier_head(self):
        await self._seed_cand(
            "c1", 100, title="塔卡沙a6方格40页团购", quote="塔卡沙a6方格40页团购"
        )
        msg = self._msg("m2", 180, "5本小红书现拍，45，按照你买的数量算钱")
        result = ClassifyResult(
            msg_index=0, category="交易", summary="5本小红书现拍",
            key_info="45, 按数量算钱", quote="5本小红书现拍，45，按照你买的数量算钱",
        )
        merged, _ = await self._store(
            msg, result, True, new_title="塔卡沙a6方格40页团购（5本45元）"
        )
        self.assertEqual(merged, 1)
        cursor = await self.db.execute("SELECT * FROM items")
        rows = await cursor.fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], "c1")  # 存活 = 最早头卡
        self.assertEqual(rows[0]["title"], "塔卡沙a6方格40页团购（5本45元）")  # 重拟标题
        self.assertIn("45", rows[0]["key_info"])
        self.assertIn("5本小红书现拍", rows[0]["source_quote"])
        self.assertEqual(rows[0]["msg_time"], 100)
        cursor = await self.db.execute("SELECT COUNT(*) AS cnt FROM raw_messages")
        self.assertEqual((await cursor.fetchone())["cnt"], 1)  # 片段 raw 行保留
        # 去重缓存经 ctx.dedup 同步：删两张、按合并文本重加存活卡
        self.assertIn(("remove", ["c1", "new-m2"]), self.dedup_calls)
        add_calls = [c for c in self.dedup_calls if c[0] == "add"]
        self.assertEqual(add_calls[0][1], "c1")

    async def test_folds_earlier_cand_into_newer_head(self):
        # 乱序：候选比新卡晚 → 新卡成为头卡，候选被吸收
        await self._seed_cand(
            "c1", 500, title="运费aa", quote="运费aa", key_info="运费AA"
        )
        msg = self._msg("m1", 180, "塔卡沙a6方格40页团购")
        result = ClassifyResult(
            msg_index=0, category="交易", summary="塔卡沙a6方格40页团购",
            key_info="", quote="塔卡沙a6方格40页团购",
        )
        merged, _ = await self._store(msg, result, True, new_title="塔卡沙团购（含运费）")
        self.assertEqual(merged, 1)
        cursor = await self.db.execute("SELECT * FROM items")
        rows = await cursor.fetchall()
        self.assertEqual(len(rows), 1)
        self.assertNotEqual(rows[0]["id"], "c1")  # 存活 = 新卡
        self.assertEqual(rows[0]["title"], "塔卡沙团购（含运费）")  # 重拟标题
        self.assertIn("运费AA", rows[0]["key_info"])
        self.assertIn("运费aa", rows[0]["source_quote"])
        self.assertEqual(rows[0]["msg_time"], 180)

    async def test_judge_false_keeps_both(self):
        await self._seed_cand("c1", 100, title="塔卡沙a6方格40页团购")
        msg = self._msg("m2", 180, "5本小红书现拍，45")
        result = ClassifyResult(
            msg_index=0, category="交易", summary="5本小红书现拍",
            key_info="45", quote="5本小红书现拍，45",
        )
        merged, _ = await self._store(msg, result, False)
        self.assertEqual(merged, 0)
        cursor = await self.db.execute("SELECT COUNT(*) AS cnt FROM items")
        self.assertEqual((await cursor.fetchone())["cnt"], 2)

    async def test_cand_with_reminder_skipped(self):
        await self._seed_cand("c1", 100, title="塔卡沙a6方格40页团购", remind_at="2026-08-02 09:00")
        msg = self._msg("m2", 180, "5本小红书现拍，45")
        result = ClassifyResult(
            msg_index=0, category="交易", summary="5本小红书现拍",
            key_info="45", quote="5本小红书现拍，45",
        )
        merged, judge_mock = await self._store(msg, result, True)
        self.assertEqual(merged, 0)
        judge_mock.assert_not_called()  # 有提醒的卡不参与合并

    async def test_out_of_window_not_merged(self):
        await self._seed_cand("c1", 100, title="塔卡沙a6方格40页团购")
        msg = self._msg("m2", 100 + 3600 * 2, "5本小红书现拍，45")
        result = ClassifyResult(
            msg_index=0, category="交易", summary="5本小红书现拍",
            key_info="45", quote="5本小红书现拍，45",
        )
        merged, judge_mock = await self._store(msg, result, True)
        self.assertEqual(merged, 0)
        judge_mock.assert_not_called()  # 窗口外无候选

    async def test_merge_keeps_second_end_in_extra_times(self):
        # 回归：多片段携带不同截止日（如「下周一前私信我 / 下周二前私信王亦群」），
        # 合并后主 end 取最早，第二个截止日保留在结构化 extra_times，不丢失
        await self._seed_cand(
            "c1",
            100,
            title="出王亦群《论位育中学》一套",
            quote="出王亦群《论位育中学》一套",
            end="2026-08-25",
        )
        msg = self._msg("m2", 180, "谁想要的在下周一之前私信我")
        result = ClassifyResult(
            msg_index=0, category="交易", summary="下周一前私信我",
            key_info="2026-08-24 前私信", quote="谁想要的在下周一之前私信我",
            end="2026-08-24",
        )
        merged, _ = await self._store(msg, result, True)
        self.assertEqual(merged, 1)
        cursor = await self.db.execute("SELECT * FROM items")
        row = await cursor.fetchone()
        self.assertEqual(row["end"], "2026-08-24")  # 主值取最早
        import json as _json
        extras = _json.loads(row["extra_times"])
        self.assertEqual(extras, [
            {"type": "end", "time": "2026-08-25", "label": ""},
        ])
        self.assertIn("2026-08-24 前私信", row["key_info"])

    async def test_merge_preserves_labeled_extra_times(self):
        # 候选卡带多个带标签的时间点（工作提醒场景），合并后全部保留且标签不丢
        await self._seed_cand(
            "c1",
            100,
            title="部门工作提醒",
            quote="部门工作提醒",
            end="2026-07-31",
            category="活动通知",
            extra_times='[{"type":"end","time":"2026-08-15","label":"部门宣传视频"},'
                "{\"type\":\"end\",\"time\":\"2026-08-17\",\"label\":\"部门宣传海报\"}]",
        )
        msg = self._msg("m2", 180, "部门文字宣传稿截止8月20日")
        result = ClassifyResult(
            msg_index=0, category="活动通知", summary="部门文字宣传稿截止",
            key_info="200字内, 填收集表", quote="部门文字宣传稿截止8月20日",
            end="2026-08-20",
        )
        merged, _ = await self._store(msg, result, True)
        self.assertEqual(merged, 1)
        cursor = await self.db.execute("SELECT * FROM items")
        row = await cursor.fetchone()
        self.assertEqual(row["end"], "2026-07-31")  # 最早为主值
        import json as _json
        extras = _json.loads(row["extra_times"])
        self.assertEqual(extras, [
            {"type": "end", "time": "2026-08-15", "label": "部门宣传视频"},
            {"type": "end", "time": "2026-08-17", "label": "部门宣传海报"},
            {"type": "end", "time": "2026-08-20", "label": ""},
        ])

    async def test_title_regeneration_failure_falls_back_to_head_title(self):
        # 重拟标题失败（None）时保守回退原标题（头句），不丢标题
        await self._seed_cand("c1", 100, title="塔卡沙a6方格40页团购")
        msg = self._msg("m2", 180, "5本小红书现拍，45")
        result = ClassifyResult(
            msg_index=0, category="交易", summary="5本小红书现拍",
            key_info="45", quote="5本小红书现拍，45",
        )
        merged, _ = await self._store(msg, result, True, new_title=None)
        self.assertEqual(merged, 1)
        cursor = await self.db.execute("SELECT * FROM items")
        row = await cursor.fetchone()
        self.assertEqual(row["title"], "塔卡沙a6方格40页团购")

    async def test_merge_disabled_when_window_zero(self):
        await self._seed_cand("c1", 100, title="塔卡沙a6方格40页团购")
        msg = self._msg("m2", 180, "5本小红书现拍，45")
        result = ClassifyResult(
            msg_index=0, category="交易", summary="5本小红书现拍",
            key_info="45", quote="5本小红书现拍，45",
        )
        with patch.object(config, "merge_window_minutes", 0):
            merged, judge_mock = await self._store(msg, result, True)
        self.assertEqual(merged, 0)
        judge_mock.assert_not_called()


class ProcessingPausedGateTest(unittest.IsolatedAsyncioTestCase):
    """benchmark 门闸（契约 E）：set_processing_paused(True) 后
    process_all_batches 直接返回 False，不触 DB/AI，批次保留待回填；
    恢复后行为复原。"""

    def tearDown(self):
        try:
            from briefdesk.pipeline import set_processing_paused
        except ImportError:
            return
        set_processing_paused(False)

    async def test_paused_returns_false_and_stays_paused(self):
        # 局部导入：实现前该符号不存在，仅本用例 RED 而非整文件收集失败
        from briefdesk.pipeline import set_processing_paused

        set_processing_paused(True)
        # 空批在未暂停时本应返回 True；暂停门闸优先，且状态不被消费（连续两次均 False）
        ok1 = await process_all_batches([], _pipeline_client(), origin="test")
        ok2 = await process_all_batches([], _pipeline_client(), origin="test")
        self.assertFalse(ok1)
        self.assertFalse(ok2, "暂停中不应被空批调用自动恢复")

    async def test_resume_restores_true_for_empty_batch(self):
        from briefdesk.pipeline import set_processing_paused

        set_processing_paused(False)
        ok = await process_all_batches([], _pipeline_client(), origin="test")
        self.assertTrue(ok, "恢复后空批应回到默认快速路径")


if __name__ == "__main__":
    unittest.main()
