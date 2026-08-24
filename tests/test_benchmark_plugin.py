"""benchmark 插件测试：装配、用例文件导出/列举/清空、四类用例推导、处理记录、运行编排（不调用 AI）。

用例存储为插件包内 cases/*.fromweb.json（文件态，不触碰数据库）；文件测试经
patch store.CASES_DIR 指向临时目录，导出读取 items 表经 patch("briefdesk.db.get_db")
指向内存连接（与基准环境补丁同机制）；运行测试用"零用例"路径（不触发任何 AI 调用）。
"""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, ClassVar
from unittest.mock import AsyncMock, Mock, patch

import aiosqlite

import briefdesk.db as briefdesk_db
from briefdesk.config import Settings, config
from briefdesk.db import init_schema, insert_item
from briefdesk.plugin.base import PluginContext
from briefdesk.plugin.manager import PluginManager
from briefdesk.plugins.benchmark import recorder as bench_recorder
from briefdesk.plugins.benchmark import router as bench_router
from briefdesk.plugins.benchmark import store as bench_store
from briefdesk.plugins.benchmark.plugin import BenchmarkPlugin
from briefdesk.plugins.benchmark.router import RecordBody, RunBody
from briefdesk.types import (
    BatchContext,
    DedupCandidate,
    DedupCheck,
    InternalMessage,
    MergeCard,
    MergeCheck,
    MergeTitleCheck,
)


async def _noop_async(event, payload):
    return None


class _EmptyEPS(list):
    """空 entry point 列表桩：隔离本机已安装插件的发现（manager 只调 .select）。"""

    def select(self, *, group=None, name=None):
        return [e for e in self if e.group == group]


def _bare_ctx() -> PluginContext:
    """最小装配上下文：所有注册端口 noop（默认即静默丢弃）。"""
    return PluginContext(
        config=Settings(
            plugins=["*"], plugins_disabled=[], plugins_required=[], plugin_path=""
        ),
        publish_event=_noop_async,
        subscribe_event=lambda e, h: None,
        register_source=lambda r: None,
        register_stage=lambda s: None,
    )


class _AiProviderStub:
    """ai_provider 依赖桩：benchmark 声明依赖它，装配测试需先注册后才能加载。"""

    name = "ai_provider"
    version = "0"
    dependencies: tuple[str, ...] = ()

    async def setup(self, ctx: PluginContext) -> None: ...

    async def activate(self, ctx: PluginContext) -> None: ...

    async def teardown(self) -> None: ...


class DefaultDisabledTest(unittest.IsolatedAsyncioTestCase):
    """默认禁用：默认配置（PLUGINS=["*"]）下 PluginManager 不装配 benchmark，
    显式列名才启用。"""

    async def asyncSetUp(self):
        self._eps_patch = patch(
            "importlib.metadata.entry_points", return_value=_EmptyEPS([])
        )
        self._eps_patch.start()
        self.addCleanup(self._eps_patch.stop)

    async def test_not_loaded_by_default(self):
        manager = PluginManager(
            Settings(
                plugins=["*"], plugins_disabled=[], plugins_required=[], plugin_path=""
            )
        )
        manager.register(BenchmarkPlugin())
        await manager.setup_all(_bare_ctx())
        self.assertEqual(manager.loaded, [])
        rec = manager.records()["benchmark"]
        self.assertEqual(rec.status, "disabled")
        self.assertIn("默认禁用", rec.reason)  # /api/plugins 可见原因

    async def test_loaded_when_explicit(self):
        manager = PluginManager(
            Settings(
                plugins=["*", "benchmark"],
                plugins_disabled=[],
                plugins_required=[],
                plugin_path="",
            )
        )
        manager.register(_AiProviderStub())  # benchmark 声明依赖 ai_provider
        manager.register(BenchmarkPlugin())
        await manager.setup_all(_bare_ctx())
        self.assertIn("benchmark", manager.loaded)
        self.assertEqual(manager.records()["benchmark"].status, "loaded")


class PluginSetupTest(unittest.IsolatedAsyncioTestCase):
    async def test_benchmark_plugin_registers_router_and_assets(self):
        registered = []
        assets = []
        stages = []

        async def publish_event(event, payload):
            return None

        def subscribe_event(event, handler):
            return None

        ctx = PluginContext(
            config=Settings(
                plugins=["*"], plugins_disabled=[], plugins_required=[], plugin_path=""
            ),
            publish_event=publish_event,
            subscribe_event=subscribe_event,
            register_source=lambda r: None,
            register_stage=stages.append,
            register_router=registered.append,
            register_plugin_assets=lambda name, d: assets.append((name, d)),
        )
        await BenchmarkPlugin().setup(ctx)
        self.assertEqual(len(registered), 1)
        paths = {getattr(r, "path", "") for r in registered[0].routes}
        self.assertIn("/api/benchmark/cases", paths)
        self.assertIn("/api/benchmark/import-current", paths)
        self.assertIn("/api/benchmark/run", paths)
        self.assertIn("/api/benchmark/report", paths)
        self.assertIn("/api/benchmark/record", paths)
        self.assertIn("/api/benchmark/export-recorded", paths)
        self.assertEqual(assets, [("benchmark", str(BenchmarkPlugin().asset_dir()))])
        self.assertEqual(BenchmarkPlugin.dependencies, ("ai_provider",))
        # 同时注册为阶段插件（slot=post_insert，priority=1，在 merge 阶段之后）
        self.assertEqual(len(stages), 1)
        self.assertEqual(stages[0].slot, "post_insert")
        self.assertEqual(stages[0].priority, 1)


class StoreTest(unittest.IsolatedAsyncioTestCase):
    """用例文件存储：cases/*.fromweb.json 导出/列举/清空（临时目录）。"""

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cases_dir = Path(self._tmp.name)
        patcher = patch.object(bench_store, "CASES_DIR", self.cases_dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_export_list_delete(self):
        case = {
            "id": "bench-abc",
            "note": "测试",
            "messages": [{"msg_id": "m1", "content": "活动"}],
            "expected": [{"index": 0, "category": "活动通知"}],
        }
        path = await bench_store.export_fromweb("classify", [case])
        self.assertEqual(path, self.cases_dir / "classify.fromweb.json")
        self.assertTrue(path.exists())
        cases = await bench_store.list_fromweb("classify")
        self.assertEqual(len(cases), 1)
        self.assertEqual(cases[0]["id"], "bench-abc")
        self.assertEqual(cases[0]["feature"], "classify")
        self.assertEqual(await bench_store.list_fromweb("dedup"), [])
        # 文件为 DatasetFile 顶层结构（与手写数据集同构）
        raw = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(raw["feature"], "classify")
        self.assertEqual(len(raw["cases"]), 1)

        # 覆盖导出（同功能文件被替换，不追加）
        other = {**case, "id": "bench-xyz"}
        await bench_store.export_fromweb("classify", [other])
        cases = await bench_store.list_fromweb("classify")
        self.assertEqual([c["id"] for c in cases], ["bench-xyz"])

        deleted = await bench_store.delete_all_fromweb("classify")
        self.assertEqual(deleted, 1)
        self.assertFalse(path.exists())
        self.assertEqual(await bench_store.list_fromweb(), [])

    async def test_export_invalid_case_rejected(self):
        from briefdesk.plugins.benchmark.schema import DatasetError

        with self.assertRaises(DatasetError):
            await bench_store.export_fromweb("classify", [{"messages": []}])
        self.assertFalse((self.cases_dir / "classify.fromweb.json").exists())

    async def test_export_empty_rejected(self):
        from briefdesk.plugins.benchmark.schema import DatasetError

        with self.assertRaises(DatasetError):
            await bench_store.export_fromweb("classify", [])

    async def test_delete_all_by_feature(self):
        # 各功能造一个合法用例
        await bench_store.export_fromweb(
            "classify",
            [{"id": "c-classify", "messages": [{"msg_id": "m", "content": "活动"}],
              "expected": [{"index": 0, "category": "活动通知"}]}],
        )
        await bench_store.export_fromweb(
            "dedup",
            [{"id": "c-dedup", "items": [{"msg_id": "a", "content": "x"}],
              "query": {"msg_id": "b", "content": "y"},
              "expected": {"same": False}}],
        )
        await bench_store.export_fromweb(
            "title",
            [{"id": "c-title", "message": {"msg_id": "t", "content": "z"},
              "expected": {"keywords": ["摄影社"]}}],
        )
        deleted = await bench_store.delete_all_fromweb("classify")
        self.assertEqual(deleted, 1)
        remaining = await bench_store.list_fromweb()
        self.assertEqual({c["feature"] for c in remaining}, {"dedup", "title"})
        self.assertEqual(await bench_store.delete_all_fromweb(), 2)

    async def test_list_invalid_file_skipped(self):
        path = self.cases_dir / "classify.fromweb.json"
        path.write_text('{"no": "cases"}', encoding="utf-8")
        self.assertEqual(await bench_store.list_fromweb("classify"), [])
        path.write_text("not json", encoding="utf-8")
        self.assertEqual(await bench_store.list_fromweb(), [])

    async def test_parse_cases_with_errors_skips_bad(self):
        good = {"id": "g1", "messages": [{"msg_id": "m", "content": "x"}],
                "expected": [{"index": 0, "category": "活动通知"}]}
        bad = {"id": "b1", "messages": "not-a-list"}
        ok, errors = bench_store.parse_cases_with_errors("classify", [good, bad])
        self.assertEqual(len(ok), 1)
        self.assertEqual(len(errors), 1)
        self.assertIn("cases[1]", errors[0])

    async def test_load_web_cases(self):
        from briefdesk.plugins.benchmark import engine as bench_engine
        from briefdesk.plugins.benchmark.schema import ClassifyCase

        await bench_store.export_fromweb(
            "classify",
            [{"id": "g1", "messages": [{"msg_id": "m", "content": "x"}],
              "expected": [{"index": 0, "category": "活动通知"}]}],
        )
        cases = await bench_engine.load_web_cases("classify")
        self.assertEqual(len(cases), 1)
        self.assertIsInstance(cases[0], ClassifyCase)
        self.assertEqual(await bench_engine.load_web_cases("dedup"), [])


class RecorderTest(unittest.IsolatedAsyncioTestCase):
    """处理记录器：判定观察记录 → 用例累积/导出（纯内存 + 临时目录）。"""

    async def asyncSetUp(self):
        bench_recorder.reset()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        store_patcher = patch.object(bench_store, "CASES_DIR", Path(self._tmp.name))
        store_patcher.start()
        self.addCleanup(store_patcher.stop)

    async def asyncTearDown(self):
        bench_recorder.reset()

    def _dedup_check(self, same: bool) -> DedupCheck:
        cand = DedupCandidate(
            item_id="i1", title="标题A", source_quote="内容A",
            source="weflow", image_urls=["a.jpg"],
        )
        msg = InternalMessage(
            msg_id="m2", content="内容B", sender_name="张三", sender_id="u2",
            session_id="s1", group_name="群1", timestamp=1000, source="weflow",
        )
        return DedupCheck(msg=msg, title="标题B", is_duplicate=same, candidate=cand)

    def _merge_check(self, same: bool, key_info: str = "价格, 运费") -> MergeCheck:
        head = MergeCard(
            title="团购", desc="团购 价格 运费", key_info="价格", subject="主体X",
            sender_name="张三", session_id="s1", group_name="群1", msg_time=1000,
            source="weflow", msg_id="m1", source_quote="团购",
        )
        tail = MergeCard(
            title="补充", desc="补充 45元", key_info="45元", subject="主体X",
            sender_name="张三", session_id="s1", group_name="群1", msg_time=1200,
            source="weflow", msg_id="m2", source_quote="补充",
        )
        check = MergeCheck(same=same, head=head, tail=tail)
        if same:
            check.title = MergeTitleCheck(
                old_title="团购", key_info=key_info, quote="团购\n补充"
            )
        return check

    def test_record_batch_builds_cases(self):
        batch = BatchContext(messages=[], client=Mock())
        batch.dedup_checks = [self._dedup_check(True), self._dedup_check(False)]
        batch.merge_checks = [
            self._merge_check(True),
            self._merge_check(False, key_info=""),  # 无 key_info → 不产 title 用例
        ]
        bench_recorder.record_batch(batch)
        st = bench_recorder.stats()
        self.assertEqual(st["counts"]["dedup"], 2)
        self.assertEqual(st["counts"]["merge"], 2)
        self.assertEqual(st["counts"]["title"], 1)
        self.assertEqual(st["counts"]["classify"], 0)
        # 命中/未命中的期望与判定结论一致
        dedup = bench_recorder._cases["dedup"]
        self.assertEqual(sorted(c["expected"]["same"] for c in dedup), [False, True])
        self.assertEqual(dedup[0]["query"]["msg_id"], "m2")
        self.assertEqual(dedup[0]["items"][0]["msg_id"], "i1")
        merge = bench_recorder._cases["merge"]
        self.assertEqual(sorted(c["expected"]["merge"] for c in merge), [False, True])
        # title 用例：期望 = 合并后 key_info 关键词，old_title = 重拟前标题
        title = bench_recorder._cases["title"]
        self.assertEqual(title[0]["expected"]["keywords"], ["价格", "运费"])
        self.assertEqual(title[0]["old_title"], "团购")
        self.assertEqual(title[0]["message"]["content"], "团购\n补充")

    def test_dedup_no_candidate_skipped(self):
        msg = InternalMessage(
            msg_id="m", content="c", sender_name="A", sender_id="u",
            session_id="s", group_name="g", timestamp=1, source="weflow",
        )
        batch = BatchContext(messages=[], client=Mock())
        batch.dedup_checks = [
            DedupCheck(msg=msg, title="t", is_duplicate=False, candidate=None)
        ]
        bench_recorder.record_batch(batch)
        self.assertEqual(bench_recorder.stats()["total"], 0)

    def test_cap_drops_new_records(self):
        with patch.object(bench_recorder, "_MAX_CASES_PER_FEATURE", 2):
            for _ in range(3):
                batch = BatchContext(messages=[], client=Mock())
                batch.dedup_checks = [self._dedup_check(True)]
                bench_recorder.record_batch(batch)
        self.assertEqual(bench_recorder.stats()["counts"]["dedup"], 2)

    async def test_stage_run_records_only_when_enabled(self):
        plugin = BenchmarkPlugin()
        b = BatchContext(messages=[], client=Mock())
        b.dedup_checks = [self._dedup_check(True)]
        ctx = PluginContext(
            config=Settings(
                plugins=["*"], plugins_disabled=[], plugins_required=[], plugin_path=""
            ),
            publish_event=_noop_async,
            subscribe_event=lambda e, h: None,
            register_source=lambda r: None,
            register_stage=lambda s: None,
        )
        await plugin.run(b, ctx)
        self.assertEqual(bench_recorder.stats()["total"], 0)
        bench_recorder.set_enabled(True)
        await plugin.run(b, ctx)
        self.assertEqual(bench_recorder.stats()["total"], 1)

    async def test_export_recorded_writes_and_clears(self):
        batch = BatchContext(messages=[], client=Mock())
        batch.dedup_checks = [self._dedup_check(True)]
        bench_recorder.record_batch(batch)
        result = await bench_recorder.export_recorded()
        self.assertEqual(result["counts"]["dedup"], 1)
        self.assertEqual(result["total"], 1)
        self.assertEqual(set(result["paths"]), {"dedup"})
        self.assertTrue(Path(result["paths"]["dedup"]).exists())
        # 导出后清空累积器
        self.assertEqual(bench_recorder.stats()["total"], 0)
        cases = await bench_store.list_fromweb("dedup")
        self.assertEqual(len(cases), 1)
        self.assertTrue(cases[0]["expected"]["same"])

    async def test_export_empty_keeps_existing_files(self):
        await bench_store.export_fromweb(
            "dedup",
            [{"id": "keep", "items": [{"msg_id": "a", "content": "x"}],
              "query": {"msg_id": "b", "content": "y"},
              "expected": {"same": False}}],
        )
        result = await bench_recorder.export_recorded()
        self.assertEqual(result["paths"], {})
        self.assertEqual(result["total"], 0)
        cases = await bench_store.list_fromweb("dedup")
        self.assertEqual([c["id"] for c in cases], ["keep"])


class RecordEndpointsTest(unittest.IsolatedAsyncioTestCase):
    """记录端点：开关/状态/清空/导出（临时目录，不触碰应用库）。"""

    async def asyncSetUp(self):
        bench_recorder.reset()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        store_patcher = patch.object(bench_store, "CASES_DIR", Path(self._tmp.name))
        store_patcher.start()
        self.addCleanup(store_patcher.stop)

    async def asyncTearDown(self):
        bench_recorder.reset()

    def _batch_with_dedup(self) -> BatchContext:
        cand = DedupCandidate(item_id="i1", title="标题A", source_quote="内容A")
        msg = InternalMessage(
            msg_id="m2", content="内容B", sender_name="张三", sender_id="u2",
            session_id="s1", group_name="群1", timestamp=1000, source="weflow",
        )
        batch = BatchContext(messages=[], client=Mock())
        batch.dedup_checks = [
            DedupCheck(msg=msg, title="标题B", is_duplicate=True, candidate=cand)
        ]
        return batch

    async def test_record_toggle_status_and_clear(self):
        st = await bench_router.record_status()
        self.assertFalse(st["enabled"])
        st = await bench_router.set_record(RecordBody(enabled=True))
        self.assertTrue(st["enabled"])
        self.assertEqual(st["total"], 0)
        bench_recorder.record_batch(self._batch_with_dedup())
        st = await bench_router.record_status()
        self.assertEqual(st["counts"]["dedup"], 1)
        res = await bench_router.clear_record()
        self.assertEqual(res["cleared"], 1)
        self.assertEqual((await bench_router.record_status())["total"], 0)

    async def test_export_recorded_endpoint(self):
        bench_recorder.record_batch(self._batch_with_dedup())
        result = await bench_router.export_recorded()
        self.assertEqual(result["total"], 1)
        self.assertIn("dedup", result["paths"])
        self.assertEqual((await bench_router.record_status())["total"], 0)


class BuildCasesTest(unittest.TestCase):
    """设置导出：items 行 → 四类用例（纯函数）。"""

    def _row(self, item_id: str, category: str, content: str, **extra: Any) -> dict:
        row: dict[str, Any] = {
            "id": item_id,
            "category": category,
            "title": f"标题-{item_id}",
            "source_quote": content,
            "source": "bench",
            "source_msg_id": f"msg-{item_id}",
            "session_id": "s1",
            "source_group": "测试群",
            "msg_time": 1000,
            "start": "2026-04-20 19:00",
            "end": "",
            "extra_times": "",
            "image_urls": "",
            "article_url": "",
            "sender_name": "张三",
            "key_info": "k",
            "subject": "主体",
        }
        row.update(extra)
        return row

    def test_basic_batch(self):
        rows = [
            self._row("i1", "活动通知", "活动内容一"),
            self._row("i2", "交易", "交易内容二"),
            self._row("i3", "", "无类别应跳过"),
        ]
        cases = bench_store.build_classify_cases_from_rows(rows)
        self.assertEqual(len(cases), 1)
        case = cases[0]
        self.assertEqual(len(case["messages"]), 2)
        self.assertEqual(len(case["expected"]), 2)
        self.assertEqual(case["expected"][0]["index"], 0)
        self.assertEqual(case["expected"][0]["category"], "活动通知")
        self.assertEqual(case["expected"][0]["start"], "2026-04-20 19:00")
        self.assertEqual(case["expected"][1]["index"], 1)
        self.assertEqual(case["expected"][1]["category"], "交易")
        self.assertEqual(case["messages"][0]["content"], "活动内容一")
        self.assertIn("由设置导出", case["note"])

    def test_batch_split_over_cap(self):
        rows = [self._row(f"i{i}", "活动通知", f"内容{i}") for i in range(105)]
        cases = bench_store.build_classify_cases_from_rows(rows)
        self.assertEqual(len(cases), 2)
        self.assertEqual(len(cases[0]["messages"]), 100)
        self.assertEqual(len(cases[1]["messages"]), 5)
        # 第二用例的期望 index 重新从 0 计
        self.assertEqual(cases[1]["expected"][0]["index"], 0)

    def test_empty_rows(self):
        self.assertEqual(bench_store.build_classify_cases_from_rows([]), [])

    def test_classify_ignored_as_noise(self):
        # 已忽略卡片 = AI 误分类的闲聊（人工标记应丢弃）：进入 messages、
        # 不写期望（模型不应输出分类结果）；无类别/有类别均一样
        rows = [
            self._row("i1", "活动通知", "活动内容一"),
            self._row("i2", "活动通知", "闲聊内容", is_verified=-1),
            self._row("i3", "", "无类别闲聊", is_verified=-1),
        ]
        cases = bench_store.build_classify_cases_from_rows(rows)
        self.assertEqual(len(cases), 1)
        case = cases[0]
        self.assertEqual(len(case["messages"]), 3)
        self.assertEqual(
            [m["msg_id"] for m in case["messages"]], ["msg-i1", "msg-i2", "msg-i3"]
        )
        self.assertEqual(len(case["expected"]), 1)
        self.assertEqual(case["expected"][0]["index"], 0)
        self.assertEqual(case["expected"][0]["category"], "活动通知")
        self.assertIn("含 2 条噪声（已忽略）", case["note"])

    def test_ignored_excluded_from_title_dedup_merge(self):
        # 噪声（已忽略）不作 title/dedup/merge 期望：仅有效卡片参与配对
        rows = [
            self._row("i1", "活动通知", "内容A", content_hash="ha", msg_time=1000,
                      session_id="s1", subject="主体X", sender_name="张三"),
            self._row("i2", "活动通知", "内容B", content_hash="ha", msg_time=1100,
                      session_id="s1", subject="主体X", sender_name="张三",
                      is_verified=-1),
            self._row("i3", "活动通知", "内容C", content_hash="hb", msg_time=1200,
                      session_id="s1", subject="主体X", sender_name="张三"),
        ]
        # title：i1 与 i3（均有默认 key_info="k" → 关键词用例）；i2 排除
        titles = bench_store.build_title_cases_from_rows(rows)
        self.assertEqual(len(titles), 2)
        self.assertNotIn("msg-i2", [t["message"]["msg_id"] for t in titles])
        # dedup：同 hash 共存对只有 i1/i3 之一（i2 被排除）→ 无 same=true 对
        dedups = bench_store.build_dedup_cases_from_rows(rows)
        self.assertEqual(dedups, [])
        # merge：同会话同主体相邻对只有 i1/i3（时间窗内）→ i2 被排除
        merges = bench_store.build_merge_cases_from_rows(rows)
        self.assertEqual(len(merges), 1)
        self.assertEqual(merges[0]["head"]["msg_id"], "msg-i1")
        self.assertEqual(merges[0]["tail"]["msg_id"], "msg-i3")

    def test_title_cases(self):
        rows = [
            self._row("i1", "活动通知", "内容一", key_info="十佳歌手, 报名"),
            self._row("i2", "交易", "内容二", key_info=""),  # 无 key_info → 期望标题
            self._row("i3", "", "无类别跳过", key_info="x"),
        ]
        cases = bench_store.build_title_cases_from_rows(rows)
        self.assertEqual(len(cases), 2)
        kw_case = next(c for c in cases if "keywords" in c["expected"])
        self.assertEqual(kw_case["expected"]["keywords"], ["十佳歌手", "报名"])
        self.assertEqual(kw_case["old_title"], "标题-i1")
        self.assertEqual(kw_case["key_info"], "十佳歌手, 报名")
        title_case = next(c for c in cases if "title" in c["expected"])
        self.assertEqual(title_case["expected"]["title"], "标题-i2")

    def test_dedup_cases(self):
        rows = [
            self._row("a", "活动通知", "内容A", content_hash="ha", msg_time=1000),
            self._row("b", "活动通知", "内容B", content_hash="ha", msg_time=1100),
            self._row("c", "交易", "内容C", content_hash="hc", msg_time=1200),
            self._row("d", "", "无类别跳过", content_hash="hd", msg_time=1300),
        ]
        cases = bench_store.build_dedup_cases_from_rows(rows)
        trues = [c for c in cases if c["expected"]["same"]]
        falses = [c for c in cases if not c["expected"]["same"]]
        # 同 content_hash 共存对 → same=true（首条为 items，其余为 query）
        self.assertEqual(len(trues), 1)
        self.assertEqual(trues[0]["items"][0]["msg_id"], "msg-a")
        self.assertEqual(trues[0]["query"]["msg_id"], "msg-b")
        # 时间相邻且类别不同（b-活动 与 c-交易）→ same=false；同类别对跳过
        self.assertEqual(len(falses), 1)
        self.assertEqual(falses[0]["items"][0]["msg_id"], "msg-b")
        self.assertEqual(falses[0]["query"]["msg_id"], "msg-c")

    def test_dedup_same_hash_pair_not_conflicting(self):
        # 同 hash 但类别不同：true 对导出，false 对跳过（不产生矛盾期望）
        rows = [
            self._row("a", "活动通知", "内容A", content_hash="hx", msg_time=1000),
            self._row("b", "交易", "内容B", content_hash="hx", msg_time=1100),
        ]
        cases = bench_store.build_dedup_cases_from_rows(rows)
        self.assertEqual(len(cases), 1)
        self.assertTrue(cases[0]["expected"]["same"])

    def test_merge_cases(self):
        rows = [
            # 同会话同类别同主体同发送者、窗内 → merge=true
            self._row("h1", "活动通知", "片段一", session_id="s1", subject="主体X",
                      sender_name="张三", msg_time=1000, content_hash="x1"),
            self._row("t1", "活动通知", "片段二", session_id="s1", subject="主体X",
                      sender_name="张三", msg_time=1500, content_hash="x2"),
            # 同会话同类别、主体均非空且不同 → merge=false
            self._row("h2", "交易", "甲物", session_id="s1", subject="甲",
                      sender_name="李四", msg_time=1000, content_hash="x3"),
            self._row("t2", "交易", "乙物", session_id="s1", subject="乙",
                      sender_name="李四", msg_time=1500, content_hash="x4"),
            # 同 content_hash 对（去重场景，非合并场景）→ 跳过
            self._row("h3", "学术", "同hash一", session_id="s2", subject="丙",
                      sender_name="王五", msg_time=1000, content_hash="x5"),
            self._row("t3", "学术", "同hash二", session_id="s2", subject="丙",
                      sender_name="王五", msg_time=1500, content_hash="x5"),
            # 主体缺失的歧义对 → 跳过
            self._row("h4", "实习", "无主体一", session_id="s3", subject="",
                      sender_name="赵六", msg_time=1000, content_hash="x7"),
            self._row("t4", "实习", "无主体二", session_id="s3", subject="",
                      sender_name="赵六", msg_time=1500, content_hash="x8"),
            # 时间窗外（>10 分钟）→ 跳过
            self._row("h5", "活动通知", "窗外一", session_id="s4", subject="丁",
                      sender_name="钱七", msg_time=1000, content_hash="x9"),
            self._row("t5", "活动通知", "窗外二", session_id="s4", subject="丁",
                      sender_name="钱七", msg_time=1000 + 601, content_hash="x10"),
            # 同主体不同发送者（一买一卖歧义）→ 跳过
            self._row("h6", "交易", "买", session_id="s5", subject="书",
                      sender_name="甲A", msg_time=1000, content_hash="x11"),
            self._row("t6", "交易", "卖", session_id="s5", subject="书",
                      sender_name="乙B", msg_time=1200, content_hash="x12"),
        ]
        with patch.object(config, "merge_window_minutes", 10):
            cases = bench_store.build_merge_cases_from_rows(rows)
        trues = [c for c in cases if c["expected"]["merge"]]
        falses = [c for c in cases if not c["expected"]["merge"]]
        self.assertEqual(len(trues), 1)
        self.assertEqual(trues[0]["head"]["msg_id"], "msg-h1")
        self.assertEqual(trues[0]["tail"]["msg_id"], "msg-t1")
        self.assertEqual(len(falses), 1)
        self.assertEqual(falses[0]["head"]["msg_id"], "msg-h2")
        self.assertEqual(falses[0]["tail"]["msg_id"], "msg-t2")


class ImportCurrentTest(unittest.IsolatedAsyncioTestCase):
    """POST /api/benchmark/import-current：导出四类 cases/*.fromweb.json（内存连接）。"""

    # 夹具覆盖四类用例的全部推导路径（详见各断言）：
    # r1 活动通知；r2 无类别（跳过）；r3/r4 同 content_hash 共存（dedup=true）；
    # r5/r6 同会话同主体同发送者相邻片段（merge=true，r6 无 key_info）；
    # r7/r8 同会话不同主体相邻卡片（merge=false）；
    # r9 已忽略（is_verified=-1，AI 误分类的闲聊）→ classify 噪声样本，
    #    title/dedup/merge 全部排除。
    _FIXTURE: ClassVar[list[dict[str, Any]]] = [
        {"category": "活动通知", "title": "十佳歌手大赛报名",
         "key_info": "十佳歌手, 报名", "sender_name": "张三",
         "source_quote": "十佳歌手大赛报名啦", "source_group": "群1", "subject": "主体A",
         "source": "bench", "source_msg_id": "m1", "session_id": "s1", "msg_time": 1000,
         "is_verified": 0, "content_hash": "h1"},
        {"category": "", "title": "无类别",
         "key_info": "", "sender_name": "B", "source_quote": "闲聊",
         "source_group": "群1", "subject": "", "source": "bench",
         "source_msg_id": "m2", "session_id": "s1", "msg_time": 1100,
         "is_verified": 0, "content_hash": "h2"},
        {"category": "交易", "title": "出二手自行车",
         "key_info": "自行车, 二手", "sender_name": "李四",
         "source_quote": "出二手自行车八成新", "source_group": "群1", "subject": "主体B",
         "source": "bench", "source_msg_id": "m3", "session_id": "s1", "msg_time": 1200,
         "is_verified": 0, "content_hash": "h3"},
        {"category": "交易", "title": "出二手自行车",
         "key_info": "自行车, 二手", "sender_name": "李四",
         "source_quote": "出二手自行车八成新", "source_group": "群1", "subject": "主体B",
         "source": "bench", "source_msg_id": "m4", "session_id": "s1", "msg_time": 1250,
         "is_verified": 0, "content_hash": "h3"},
        {"category": "活动通知", "title": "十佳歌手大赛报名",
         "key_info": "十佳歌手, 报名", "sender_name": "王五",
         "source_quote": "十佳歌手大赛开始报名", "source_group": "群2",
         "subject": "校园十佳歌手大赛", "source": "bench",
         "source_msg_id": "m5", "session_id": "s2", "msg_time": 2000,
         "is_verified": 0, "content_hash": "h5"},
        {"category": "活动通知", "title": "十佳歌手报名截止",
         "key_info": "", "sender_name": "王五",
         "source_quote": "补充：报名截止4月15日", "source_group": "群2",
         "subject": "校园十佳歌手大赛", "source": "bench",
         "source_msg_id": "m6", "session_id": "s2", "msg_time": 2300,
         "is_verified": 0, "content_hash": "h6"},
        {"category": "交易", "title": "出二手自行车",
         "key_info": "自行车", "sender_name": "赵六",
         "source_quote": "出自行车150元", "source_group": "群3", "subject": "自行车",
         "source": "bench", "source_msg_id": "m7", "session_id": "s3", "msg_time": 3000,
         "is_verified": 0, "content_hash": "h7"},
        {"category": "交易", "title": "收考研数学书",
         "key_info": "考研数学", "sender_name": "钱七",
         "source_quote": "收考研数学复习全书", "source_group": "群3", "subject": "考研书",
         "source": "bench", "source_msg_id": "m8", "session_id": "s3", "msg_time": 3500,
         "is_verified": 0, "content_hash": "h8"},
        {"category": "活动通知", "title": "闲聊误分类",
         "key_info": "", "sender_name": "C", "source_quote": "今天天气不错",
         "source_group": "群1", "subject": "", "source": "bench",
         "source_msg_id": "m9", "session_id": "s1", "msg_time": 1050,
         "is_verified": -1, "content_hash": "h9"},
    ]

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cases_dir = Path(self._tmp.name)
        store_patcher = patch.object(bench_store, "CASES_DIR", self.cases_dir)
        store_patcher.start()
        self.addCleanup(store_patcher.stop)
        self.conn = await aiosqlite.connect(":memory:")
        self.conn.row_factory = aiosqlite.Row
        await init_schema(self.conn)
        self.patcher = patch.object(briefdesk_db, "get_db", new=AsyncMock(return_value=self.conn))
        self.patcher.start()
        for row in self._FIXTURE:
            await insert_item(row)

    async def asyncTearDown(self):
        self.patcher.stop()
        await self.conn.close()

    async def test_import_current_exports_four_features(self):
        # 直接调用 handler：Query 默认值需显式传 None（FastAPI 层会注入真实值）
        # 合并配对窗固定 10 分钟（m5→m6 差 300s、m7→m8 差 500s，均在窗内）
        with patch.object(config, "merge_window_minutes", 10):
            result = await bench_router.import_current_list(
                category=None, verified="unverified", q=None, source_group=None,
                min_msg_time=None, hide_expired=False, filter_now=None,
            )
        self.assertEqual(
            result["counts"], {"classify": 1, "dedup": 4, "merge": 2, "title": 7}
        )
        self.assertEqual(result["total"], 14)
        self.assertEqual(result["cards"], 8)  # 有效卡片（忽略行不计入）
        self.assertEqual(result["messages"], 8)  # classify 消息数（7 正样本 + 1 噪声）
        self.assertEqual(result["noise"], 1)  # 已忽略卡片 → classify 噪声样本
        self.assertEqual(result["skipped_no_category"], 1)
        # 四个文件全部导出且计数一致
        self.assertEqual(set(result["paths"]), {"classify", "dedup", "merge", "title"})
        for f in ("classify", "dedup", "merge", "title"):
            path = self.cases_dir / f"{f}.fromweb.json"
            self.assertEqual(Path(result["paths"][f]), path)
            raw = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(raw["feature"], f)
            self.assertEqual(len(raw["cases"]), result["counts"][f])
        # 抽查各功能期望推导（items 分页为 msg_time DESC，首条消息是 m8 交易卡）
        cls = json.loads((self.cases_dir / "classify.fromweb.json").read_text(encoding="utf-8"))
        self.assertEqual(len(cls["cases"][0]["messages"]), 8)
        cats = sorted(e["category"] for e in cls["cases"][0]["expected"])
        self.assertEqual(cats, ["交易"] * 4 + ["活动通知"] * 3)
        # 已忽略卡片（m9 闲聊）进入 messages 但无期望（噪声样本，模型不应分类）
        noise_msgs = [
            m for m in cls["cases"][0]["messages"] if m["msg_id"] == "m9"
        ]
        self.assertEqual(len(noise_msgs), 1)
        expected_indexes = [e["index"] for e in cls["cases"][0]["expected"]]
        noise_index = cls["cases"][0]["messages"].index(noise_msgs[0])
        self.assertNotIn(noise_index, expected_indexes)
        self.assertIn("含 1 条噪声（已忽略）", cls["cases"][0]["note"])
        # title/dedup/merge 排除噪声行：m9 不出现
        tt2 = json.loads((self.cases_dir / "title.fromweb.json").read_text(encoding="utf-8"))
        self.assertNotIn(
            "m9", [c["message"]["msg_id"] for c in tt2["cases"]]
        )
        dd2 = json.loads((self.cases_dir / "dedup.fromweb.json").read_text(encoding="utf-8"))
        self.assertNotIn(
            "m9",
            [m["msg_id"] for c in dd2["cases"] for m in c["items"] + [c["query"]]],
        )
        mg2 = json.loads((self.cases_dir / "merge.fromweb.json").read_text(encoding="utf-8"))
        self.assertNotIn(
            "m9",
            [m["msg_id"] for c in mg2["cases"] for m in (c["head"], c["tail"])],
        )
        dd = json.loads((self.cases_dir / "dedup.fromweb.json").read_text(encoding="utf-8"))
        self.assertEqual(
            sorted(c["expected"]["same"] for c in dd["cases"]), [False, False, False, True]
        )
        mg = json.loads((self.cases_dir / "merge.fromweb.json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(c["expected"]["merge"] for c in mg["cases"]), [False, True])
        tt = json.loads((self.cases_dir / "title.fromweb.json").read_text(encoding="utf-8"))
        title_only = [c["expected"] for c in tt["cases"] if "title" in c["expected"]]
        self.assertEqual(title_only, [{"title": "十佳歌手报名截止"}])  # r6 无 key_info
        all_kw = [tuple(c["expected"]["keywords"]) for c in tt["cases"] if "keywords" in c["expected"]]
        self.assertEqual(len(all_kw), 6)
        self.assertIn(("自行车", "二手"), all_kw)
        self.assertIn(("十佳歌手", "报名"), all_kw)
        self.assertIn(("考研数学",), all_kw)

    async def test_import_current_no_cases_skips_export(self):
        # 预置一份用例文件；空结果导出不得覆盖它
        await bench_store.export_fromweb(
            "classify",
            [{"id": "keep", "messages": [{"msg_id": "m", "content": "活动"}],
              "expected": [{"index": 0, "category": "活动通知"}]}],
        )
        result = await bench_router.import_current_list(
            category=None, verified="unverified", q="绝无匹配的关键词",
            source_group=None, min_msg_time=None, hide_expired=False, filter_now=None,
        )
        self.assertEqual(
            result["counts"], {"classify": 0, "dedup": 0, "merge": 0, "title": 0}
        )
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["paths"], {})
        cases = await bench_store.list_fromweb("classify")
        self.assertEqual([c["id"] for c in cases], ["keep"])


class RunFlowTest(unittest.IsolatedAsyncioTestCase):
    """运行编排：零用例路径不触发 AI、状态/报告端点可用。"""

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        store_patcher = patch.object(
            bench_store, "CASES_DIR", Path(self._tmp.name)
        )
        store_patcher.start()
        self.addCleanup(store_patcher.stop)
        self.conn = await aiosqlite.connect(":memory:")
        self.conn.row_factory = aiosqlite.Row
        await init_schema(self.conn)
        self.patcher = patch.object(briefdesk_db, "get_db", new=AsyncMock(return_value=self.conn))
        self.patcher.start()
        bench_router._running_task = None
        bench_router._last_result = None

    async def asyncTearDown(self):
        import contextlib

        if bench_router._running_task is not None and not bench_router._running_task.done():
            bench_router._running_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await bench_router._running_task
        bench_router._running_task = None
        bench_router._last_result = None
        self.patcher.stop()
        await self.conn.close()

    async def test_start_run_with_no_cases_completes(self):
        resp = await bench_router.start_run(None)
        self.assertTrue(resp["started"])
        # 等待后台任务完成（零用例 → 无 AI 调用，立即完成）
        for _ in range(50):
            state = await bench_router.run_status()
            if not state["running"]:
                break
            await asyncio.sleep(0.02)
        self.assertFalse(state["running"])
        self.assertIn("run_id", state)
        self.assertEqual(state["summary"], {})
        self.assertIsInstance(state["elapsed_sec"], float)
        report = await bench_router.latest_json_report()
        payload = json.loads(report.body)
        self.assertEqual(payload["features"], {})
        self.assertIn("run_id", payload)
        self.assertIsInstance(payload["elapsed_sec"], float)
        # 未知功能拒绝
        from fastapi import HTTPException

        with self.assertRaises(HTTPException):
            await bench_router.start_run(RunBody(features=["nope"]))


class CasesEndpointTest(unittest.IsolatedAsyncioTestCase):
    """用例端点参数校验（?feature= 未知值拒绝）。"""

    async def test_unknown_feature_rejected(self):
        from fastapi import HTTPException

        with self.assertRaises(HTTPException):
            await bench_router.list_cases(feature="nope")
        with self.assertRaises(HTTPException):
            await bench_router.remove_all_cases(feature="nope")


if __name__ == "__main__":
    unittest.main()
