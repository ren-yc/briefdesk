"""管道阶段插件装配测试：setup 注册、服务端口、事件接线与注册表排序。"""

import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from briefdesk import ai_ports, stages
from briefdesk.config import Settings
from briefdesk.events import EVENT_ITEMS_DELETED
from briefdesk.plugin.base import PluginContext, PluginError
from briefdesk.plugin.manager import PluginManager
from briefdesk.plugins.ai_provider.plugin import AiProviderPlugin
from briefdesk.plugins.classify.plugin import ClassifyPlugin
from briefdesk.plugins.dedup.plugin import DedupPlugin
from briefdesk.plugins.merge.plugin import MergePlugin
from briefdesk.plugins.ocr.plugin import OcrPlugin


async def _noop_async(*args, **kwargs):
    return None


def _noop_sync(*args, **kwargs):
    return None


def _ctx(register_stage=None):
    subscribers: list = []

    ctx = PluginContext(
        config=Settings(
            plugins=[], plugins_required=[], plugin_path=""
        ),
        publish_event=_noop_async,
        subscribe_event=lambda event, handler: subscribers.append((event, handler)),
        register_source=_noop_sync,
        register_stage=register_stage or _noop_sync,
    )
    return ctx, subscribers


class _FakeStage:
    """注册表测试用最小阶段（带 priority）。"""

    def __init__(self, slot, priority):
        self.slot = slot
        self.priority = priority

    async def run(self, batch, ctx): ...


class StagesRegistryTest(unittest.TestCase):
    def setUp(self):
        stages.reset()

    def tearDown(self):
        stages.reset()

    def test_slot_ordering_by_priority(self):
        s1 = _FakeStage("enrich", 10)
        s0 = _FakeStage("enrich", 0)
        stages.register_stage(s1)
        stages.register_stage(s0)
        assert stages.get_stages("enrich") == [s0, s1]

    def test_duplicate_registration_not_duplicated(self):
        s = _FakeStage("classify", 0)
        stages.register_stage(s)
        stages.register_stage(s)
        assert len(stages.get_stages("classify")) == 1

    def test_get_stages_returns_snapshot(self):
        stages.register_stage(_FakeStage("classify", 0))
        snap = stages.get_stages("classify")
        snap.append(None)  # 修改快照不影响注册表
        assert len(stages.get_stages("classify")) == 1
        assert stages.get_stages("nope") == []

    def test_context_set_and_reset(self):
        ctx, _ = _ctx()
        assert stages.get_context() is None
        stages.set_context(ctx)
        assert stages.get_context() is ctx
        stages.reset()
        assert stages.get_context() is None


class TestStagePluginSetup:
    async def test_stage_plugins_register_with_correct_slots(self):
        registered = []
        ctx, _ = _ctx(register_stage=registered.append)
        fake_engine = Mock(remove_items=Mock())
        fake_engine.ensure_cache = AsyncMock()
        # OCR 依赖（rapidocr/onnxruntime）为可选：mock engine 模块使测试
        # 不依赖真实安装环境（未安装时 setup 抛 PluginDisabledError）
        fake_ocr_engine = SimpleNamespace(ocr_images_bytes=AsyncMock())
        with patch.dict(sys.modules, {"briefdesk.plugins.ocr.engine": fake_ocr_engine}), patch(
            "briefdesk.plugins.dedup.engine.DedupEngine", return_value=fake_engine
        ):
            await OcrPlugin().setup(ctx)
            await ClassifyPlugin().setup(ctx)
            await DedupPlugin().setup(ctx)
            await MergePlugin().setup(ctx)
        assert [s.slot for s in registered] == ["enrich", "classify", "dedup", "post_insert"]

    async def test_dedup_setup_warms_cache_and_registers_service(self):
        ctx, _ = _ctx()
        fake_engine = Mock(remove_items=Mock())
        fake_engine.ensure_cache = AsyncMock()
        with patch("briefdesk.plugins.dedup.engine.DedupEngine", return_value=fake_engine):
            plugin = DedupPlugin()
            await plugin.setup(ctx)
        fake_engine.ensure_cache.assert_awaited_once()
        assert ctx.dedup is fake_engine  # 服务端口注册

    async def test_dedup_teardown_clears_ctx_dedup_port(self):
        """teardown 幂等回收自己注册的 ctx.dedup 服务端口。"""
        ctx, _ = _ctx()
        fake_engine = Mock(remove_items=Mock())
        fake_engine.ensure_cache = AsyncMock()
        with patch("briefdesk.plugins.dedup.engine.DedupEngine", return_value=fake_engine):
            plugin = DedupPlugin()
            await plugin.setup(ctx)
        assert ctx.dedup is fake_engine
        await plugin.teardown()
        assert ctx.dedup is None

    async def test_dedup_setup_failure_window_leaves_no_ports(self):
        """可失败步骤（ensure_cache）先于全部注册——抛错时 ctx.dedup
        未注册、stage 未注册、事件未订阅。"""
        ctx, subscribers = _ctx()
        fake_engine = Mock(remove_items=Mock())
        fake_engine.ensure_cache = AsyncMock(side_effect=RuntimeError("预热失败"))
        with patch("briefdesk.plugins.dedup.engine.DedupEngine", return_value=fake_engine):
            plugin = DedupPlugin()
            with pytest.raises(RuntimeError):
                await plugin.setup(ctx)
        assert ctx.dedup is None
        assert subscribers == [], "失败窗口不得注册事件订阅"

    async def test_dedup_subscribes_items_deleted_and_clears_cache(self):
        ctx, subscribers = _ctx()
        fake_engine = Mock(remove_items=Mock())
        fake_engine.ensure_cache = AsyncMock()
        with patch("briefdesk.plugins.dedup.engine.DedupEngine", return_value=fake_engine):
            plugin = DedupPlugin()
            await plugin.setup(ctx)
        events = [e for e, _ in subscribers]
        assert EVENT_ITEMS_DELETED in events
        handler = dict(subscribers)[EVENT_ITEMS_DELETED]
        handler(["i1", "i2"])
        fake_engine.remove_items.assert_called_once_with(["i1", "i2"])
        # 引擎未就绪时删除事件安全跳过
        plugin._engine = None
        handler(["i3"])
        fake_engine.remove_items.assert_called_once()

    async def test_classify_run_passes_vision_images_to_engine(self):
        # vision 路由：classify 阶段把 enrich 暂存的图片字节随批传给引擎。
        # 直接向实例注入假引擎函数——engine 模块可能已被其他测试真实导入，
        # sys.modules patch 会被 package 属性查找绕过（与 OcrPlugin 假引擎
        # 不同，classify engine 是硬依赖）。
        from briefdesk.types import BatchContext

        fake_classify = AsyncMock(return_value=None)
        plugin = ClassifyPlugin()
        plugin._classify_batch = fake_classify
        vision = {("weflow-legacy", "m1"): [b"jpeg-bytes"]}
        batch = BatchContext(messages=[SimpleNamespace()], client=SimpleNamespace())
        batch.vision_images = vision
        await plugin.run(batch, _ctx()[0])
        fake_classify.assert_awaited_once_with(batch.messages, vision_images=vision)


class StagePluginMetaTest(unittest.TestCase):
    def test_merge_declares_dedup_dependency(self):
        # 拓扑序保证 dedup 先 setup → ctx.dedup 就绪后 merge 才可能运行
        assert MergePlugin.dependencies == ("dedup", "ai_provider")

    def test_ai_dependent_plugins_declare_ai_provider(self):
        # 分类/去重/合并依赖 AI 供应商：ai_provider 被禁用时它们随依赖未就绪
        # 自动降级，pipeline 骨架的"阶段缺失"守卫保证消息不被误标记
        assert ClassifyPlugin.dependencies == ("ai_provider",)
        assert DedupPlugin.dependencies == ("ai_provider",)

    def test_slot_priorities_are_zero(self):
        for cls in (OcrPlugin, ClassifyPlugin, DedupPlugin, MergePlugin):
            assert cls.priority == 0


class TopKSimilarStableOrderTest(unittest.TestCase):
    """并列相似度必须按原始下标序稳定输出（rag 检索的确定性依赖）。"""

    def test_ties_keep_original_index_order(self):
        from briefdesk.ai_ports import top_k_similar

        hits = top_k_similar(
            [1.0, 0.0], [[1.0, 0.0], [1.0, 0.0], [0.0, 1.0]], top_k=3, threshold=0.5
        )
        assert [i for i, _ in hits] == [0, 1]
        assert round(abs(hits[0][1] - hits[1][1]), 6) == 0

    def test_descending_order_unchanged(self):
        from briefdesk.ai_ports import top_k_similar

        hits = top_k_similar(
            [1.0, 0.0], [[0.9, 0.1], [0.5, 0.5], [1.0, 0.0]], top_k=3, threshold=0.0
        )
        assert [i for i, _ in hits] == [2, 0, 1]


class TestAiProviderPlugin:
    @pytest.fixture(autouse=True)
    async def _autouse_setup(self):
        ai_ports.set_ai(None)
        yield
        ai_ports.set_ai(None)
    async def test_setup_registers_ctx_and_ports(self):
        ctx, _ = _ctx()
        fake_provider = Mock()
        plugin = AiProviderPlugin()
        with patch(
            "briefdesk.plugins.ai_provider.engine.Provider", return_value=fake_provider
        ):
            await plugin.setup(ctx)
        try:
            assert ctx.ai is fake_provider
            assert ai_ports.get_ai() is fake_provider
        finally:
            await plugin.teardown()
        assert ai_ports.get_ai() is None

    async def test_setup_failure_window_leaves_no_ports(self):
        """可失败步骤（announce）在端口注册之前——announce 抛错时
        ctx.ai 未被设置；best-effort teardown 后 ai_ports 亦清空。"""
        ctx, _ = _ctx()
        fake_provider = Mock()
        plugin = AiProviderPlugin()
        with (
            patch(
                "briefdesk.plugins.ai_provider.engine.Provider",
                return_value=fake_provider,
            ),
            patch(
                "briefdesk.plugins.ai_provider.engine.announce_embedding_state",
                new=AsyncMock(side_effect=RuntimeError("announce failed")),
            ),pytest.raises(RuntimeError)
        ):
            await plugin.setup(ctx)
        assert ctx.ai is None, "announce 失败窗口 ctx.ai 不得被注册"
        await plugin.teardown()  # manager 装配失败路径的 best-effort 回收
        assert ai_ports.get_ai() is None

    async def test_port_functions_forward_to_provider(self):
        # 引擎经 ai_ports 端口函数调用：chat/嵌入转发到注册的供应商
        fake_provider = Mock()
        fake_provider.chat = AsyncMock(return_value="resp")
        fake_provider.embed_texts = AsyncMock(return_value=[[0.1]])
        fake_provider.is_embedding_enabled = Mock(return_value=True)
        fake_provider.embed_model_name = Mock(return_value="m")
        ai_ports.set_ai(fake_provider)
        assert await ai_ports.chat([{"role": "user", "content": "hi"}], temperature=0.1, max_tokens=8) == "resp"
        assert await ai_ports.embed_texts(["x"]) == [[0.1]]
        assert ai_ports.is_embedding_enabled()
        assert ai_ports.embed_model_name() == "m"
        fake_provider.chat.assert_awaited_once()

    async def test_unregistered_provider_raises_on_chat(self):
        ai_ports.set_ai(None)
        with pytest.raises(RuntimeError):
            await ai_ports.chat([], temperature=0.1, max_tokens=8)
        assert not ai_ports.is_embedding_enabled()  # 启用性检查安全返回 False


class _NoEntryPoints(list):
    """空 entry point 列表桩（manager 只调用 .select(group=...)）。"""

    def select(self, *, group=None, name=None):
        return []


def _mgr_settings(*, required=None):
    return Settings(
        plugins=["boom", "dep"],
        plugins_required=list(required or []),
        plugin_path="",
    )


class TestPluginManagerRollback:
    """P2 修复：setup/activate 非 PluginDisabledError 失败时 best-effort 调一次
    plugin.teardown() 回收半装配副作用再标 failed；依赖方照常 disabled 降级、
    REQUIRED 名单失败仍致命（PluginError 中止装配）。"""

    class _Boom:
        """setup/activate 可注入失败的假插件（记录生命周期事件序）。"""

        def __init__(self, fail_in="setup"):
            self.name = "boom"
            self.version = "0.0.1"
            self.dependencies = ()
            self.fail_in = fail_in
            self.events = []

        async def setup(self, ctx):
            self.events.append("setup")
            if self.fail_in == "setup":
                raise RuntimeError("setup 半途爆炸")

        async def activate(self, ctx):
            self.events.append("activate")
            if self.fail_in == "activate":
                raise RuntimeError("activate 爆炸")

        async def teardown(self):
            self.events.append("teardown")

    class _Dep:
        name = "dep"
        version = "0.0.1"
        dependencies = ("boom",)

        async def setup(self, ctx): ...
        async def activate(self, ctx): ...
        async def teardown(self): ...

    @pytest.fixture(autouse=True)
    def _autouse_setup(self):
        self._eps_patcher = patch(
            "importlib.metadata.entry_points", return_value=_NoEntryPoints([])
        )
        self._eps_patcher.start()
        yield
        self._eps_patcher.stop()
    async def test_setup_failure_triggers_teardown_and_dependent_disabled(self):
        boom = self._Boom(fail_in="setup")
        manager = PluginManager(_mgr_settings())
        manager.register(boom)
        manager.register(self._Dep())
        ctx, _ = _ctx()
        await manager.setup_all(ctx)
        # 半装配副作用被回收：teardown 恰好一次（在标 failed 之前）
        assert boom.events == ["setup", "teardown"]
        assert manager.records()["boom"].status == "failed"
        # 后续依赖插件仍降级 disabled，不被波及为 fatal
        rec = manager.records()["dep"]
        assert rec.status == "disabled"
        assert "依赖未就绪" in rec.reason

    async def test_required_setup_failure_fatal_after_teardown(self):
        boom = self._Boom(fail_in="setup")
        manager = PluginManager(_mgr_settings(required=["boom"]))
        manager.register(boom)
        ctx, _ = _ctx()
        with pytest.raises(PluginError):
            await manager.setup_all(ctx)
        # REQUIRED 致命路径同样先回收副作用
        assert "teardown" in boom.events

    async def test_activate_failure_best_effort_teardown(self):
        boom = self._Boom(fail_in="activate")
        manager = PluginManager(_mgr_settings())
        manager.register(boom)
        ctx, _ = _ctx()
        await manager.setup_all(ctx)
        await manager.activate_all(ctx)
        assert boom.events == ["setup", "activate", "teardown"]
        rec = manager.records()["boom"]
        assert rec.status == "failed"
        assert "activate 失败" in rec.reason
        # 幂等叠加契约：该插件仍在 _load_order，关闭期 teardown_all 会按幂等
        # 契约再调一次 → teardown 总调用次数 == 2（best-effort 回收 + 收尾）
        await manager.teardown_all()
        assert boom.events.count("teardown") == 2


class TestPluginDisabledNoTeardown:
    """PluginDisabledError 自禁用发生在获取资源之前，不走 teardown 回收。"""

    @pytest.fixture(autouse=True)
    def _autouse_setup(self):
        self._eps_patcher = patch(
            "importlib.metadata.entry_points", return_value=_NoEntryPoints([])
        )
        self._eps_patcher.start()
        yield
        self._eps_patcher.stop()
    async def test_self_disable_skips_rollback_teardown(self):
        calls = []

        class P:
            name = "p"
            version = "0.1"
            dependencies = ()

            async def setup(self, ctx):
                calls.append("setup")
                from briefdesk.plugin.base import PluginDisabledError

                raise PluginDisabledError("缺少必填配置")

            async def activate(self, ctx): ...
            async def teardown(self):
                calls.append("teardown")

        settings = Settings(
            plugins=["p"], plugins_required=[], plugin_path=""
        )
        manager = PluginManager(settings)
        manager.register(P())
        ctx, _ = _ctx()
        await manager.setup_all(ctx)
        assert calls == ["setup"]  # 无 teardown 回收
        assert manager.records()["p"].status == "disabled"


class TestMergeAfterRunReembed:
    """【复核 P2-20】merge after_run 对存活卡补嵌并带向量重新登记。

    合并改写文本后 DB 侧向量已删，run 的 add_to_cache 不带向量——长驻
    进程中存活卡就此退出余弦候选集直到重启；after_run 在锁外补嵌修复。
    """

    async def test_after_run_reembeds_and_reregisters_with_vector(self):
        from types import SimpleNamespace

        from briefdesk.plugins.dedup.engine import DedupEngine
        from briefdesk.plugins.merge.plugin import MergePlugin
        from briefdesk.types import BatchContext

        engine = DedupEngine()
        engine._embed_cache_ok = True  # 预热已完成态：补嵌向量可登记并落库
        engine.add_to_cache("i1", "旧标题", source="weflow-legacy", source_quote="旧文")
        batch = BatchContext(messages=[], client=Mock())
        batch.reembed_queue.append(
            ("i1", "合并后标题", "合并后原文", None, "weflow-legacy")
        )
        vec = [0.1, 0.2, 0.3]
        with patch(
            "briefdesk.ai_ports.embed_texts", AsyncMock(return_value=[vec])
        ), patch(
            # P3-11 门控：after_run 仅在嵌入启用时补嵌
            "briefdesk.ai_ports.is_embedding_enabled", return_value=True
        ), patch(
            # after_run 补嵌前复查存在性（P1-3）；本测试 i1 仍存在
            "briefdesk.db.get_existing_item_ids",
            AsyncMock(return_value={"i1"}),
        ):
            await MergePlugin().after_run(batch, SimpleNamespace(dedup=engine))
        assert engine._cache[0].title == "合并后标题"
        assert engine._cache[0].source_quote == "合并后原文"
        assert engine._cache[0].embedding == vec

    async def test_after_run_noop_without_queue_or_service(self):
        from types import SimpleNamespace

        from briefdesk.plugins.merge.plugin import MergePlugin
        from briefdesk.types import BatchContext

        batch = BatchContext(messages=[], client=Mock())
        # 无队列 / 无 dedup 服务（插件禁用）均静默跳过
        await MergePlugin().after_run(batch, SimpleNamespace(dedup=None))
        await MergePlugin().after_run(batch, SimpleNamespace(dedup=None))

    async def test_after_run_skips_deleted_items(self):
        """复核 P1-3：锁外补嵌前按 item_id 复查存在性，已删除的卡不得
        add_to_cache（否则复活幽灵缓存条目，相似消息被误判重静默丢失）。"""
        from types import SimpleNamespace

        from briefdesk.plugins.dedup.engine import DedupEngine
        from briefdesk.plugins.merge.plugin import MergePlugin
        from briefdesk.types import BatchContext

        engine = DedupEngine()
        engine._embed_cache_ok = True
        # 预置一条已存在缓存（i1），reembed_queue 里放已删除的 i2 与存在的 i1
        engine.add_to_cache("i1", "旧标题", source="weflow-legacy", source_quote="旧文")
        batch = BatchContext(messages=[], client=Mock())
        batch.reembed_queue.append(
            ("i2", "已删除卡", "已删除原文", None, "weflow-legacy")
        )
        vec = [0.1, 0.2, 0.3]
        with patch(
            "briefdesk.ai_ports.embed_texts", AsyncMock(return_value=[vec])
        ), patch(
            # after_run 内部延迟导入 get_existing_item_ids
            "briefdesk.db.get_existing_item_ids",
            AsyncMock(return_value=set()),  # i2 已不存在
        ):
            await MergePlugin().after_run(batch, SimpleNamespace(dedup=engine))
        # i2 被过滤：不 add_to_cache，不产生幽灵条目
        cached_ids = {c.id for c in engine._cache}
        assert "i2" not in cached_ids, "已删除卡不得复活进缓存"
        assert "i1" in cached_ids, "既有缓存条目不受影响"


class TestAiProviderPortsGuard:
    """未 setup 直接调用端口 → 显式 RuntimeError（非
    AssertionError，-O 下 assert 被剥离时同样可靠）。"""

    @pytest.fixture(autouse=True)
    def _autouse_setup(self):
        ai_ports.set_ai(None)
        yield
        ai_ports.set_ai(None)
    async def test_unsetup_port_calls_raise_runtime_error(self):
        plugin = AiProviderPlugin()
        with pytest.raises(RuntimeError):
            await plugin.chat(
                [{"role": "user", "content": "x"}], temperature=0.1, max_tokens=1
            )
        with pytest.raises(RuntimeError):
            await plugin.rag_chat(
                [{"role": "user", "content": "x"}], temperature=0.1, max_tokens=1
            )
        with pytest.raises(RuntimeError):
            await plugin.embed_texts(["x"])
        with pytest.raises(RuntimeError):
            plugin.embed_model_name()
