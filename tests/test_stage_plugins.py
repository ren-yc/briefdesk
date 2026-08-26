"""管道阶段插件装配测试：setup 注册、服务端口、事件接线与注册表排序。"""

import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from briefdesk import ai_ports, stages
from briefdesk.config import Settings
from briefdesk.events import EVENT_ITEMS_DELETED
from briefdesk.plugin.base import PluginContext
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
            plugins=["*"], plugins_disabled=[], plugins_required=[], plugin_path=""
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
        self.assertEqual(stages.get_stages("enrich"), [s0, s1])

    def test_duplicate_registration_not_duplicated(self):
        s = _FakeStage("classify", 0)
        stages.register_stage(s)
        stages.register_stage(s)
        self.assertEqual(len(stages.get_stages("classify")), 1)

    def test_get_stages_returns_snapshot(self):
        stages.register_stage(_FakeStage("classify", 0))
        snap = stages.get_stages("classify")
        snap.append(None)  # 修改快照不影响注册表
        self.assertEqual(len(stages.get_stages("classify")), 1)
        self.assertEqual(stages.get_stages("nope"), [])

    def test_context_set_and_reset(self):
        ctx, _ = _ctx()
        self.assertIsNone(stages.get_context())
        stages.set_context(ctx)
        self.assertIs(stages.get_context(), ctx)
        stages.reset()
        self.assertIsNone(stages.get_context())


class StagePluginSetupTest(unittest.IsolatedAsyncioTestCase):
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
        self.assertEqual(
            [s.slot for s in registered],
            ["enrich", "classify", "dedup", "post_insert"],
        )

    async def test_dedup_setup_warms_cache_and_registers_service(self):
        ctx, _ = _ctx()
        fake_engine = Mock(remove_items=Mock())
        fake_engine.ensure_cache = AsyncMock()
        with patch("briefdesk.plugins.dedup.engine.DedupEngine", return_value=fake_engine):
            plugin = DedupPlugin()
            await plugin.setup(ctx)
        fake_engine.ensure_cache.assert_awaited_once()
        self.assertIs(ctx.dedup, fake_engine)  # 服务端口注册

    async def test_dedup_subscribes_items_deleted_and_clears_cache(self):
        ctx, subscribers = _ctx()
        fake_engine = Mock(remove_items=Mock())
        fake_engine.ensure_cache = AsyncMock()
        with patch("briefdesk.plugins.dedup.engine.DedupEngine", return_value=fake_engine):
            plugin = DedupPlugin()
            await plugin.setup(ctx)
        events = [e for e, _ in subscribers]
        self.assertIn(EVENT_ITEMS_DELETED, events)
        handler = dict(subscribers)[EVENT_ITEMS_DELETED]
        handler(["i1", "i2"])
        fake_engine.remove_items.assert_called_once_with(["i1", "i2"])
        # 引擎未就绪时删除事件安全跳过
        plugin._engine = None
        handler(["i3"])
        fake_engine.remove_items.assert_called_once()


class StagePluginMetaTest(unittest.TestCase):
    def test_merge_declares_dedup_dependency(self):
        # 拓扑序保证 dedup 先 setup → ctx.dedup 就绪后 merge 才可能运行
        self.assertEqual(MergePlugin.dependencies, ("dedup", "ai_provider"))

    def test_ai_dependent_plugins_declare_ai_provider(self):
        # 分类/去重/合并依赖 AI 供应商：ai_provider 被禁用时它们随依赖未就绪
        # 自动降级，pipeline 骨架的"阶段缺失"守卫保证消息不被误标记
        self.assertEqual(ClassifyPlugin.dependencies, ("ai_provider",))
        self.assertEqual(DedupPlugin.dependencies, ("ai_provider",))

    def test_slot_priorities_are_zero(self):
        for cls in (OcrPlugin, ClassifyPlugin, DedupPlugin, MergePlugin):
            self.assertEqual(cls.priority, 0)


class TopKSimilarStableOrderTest(unittest.TestCase):
    """并列相似度必须按原始下标序稳定输出（rag 检索的确定性依赖）。"""

    def test_ties_keep_original_index_order(self):
        from briefdesk.ai_ports import top_k_similar

        hits = top_k_similar(
            [1.0, 0.0], [[1.0, 0.0], [1.0, 0.0], [0.0, 1.0]], top_k=3, threshold=0.5
        )
        self.assertEqual([i for i, _ in hits], [0, 1])
        self.assertAlmostEqual(hits[0][1], hits[1][1], places=6)

    def test_descending_order_unchanged(self):
        from briefdesk.ai_ports import top_k_similar

        hits = top_k_similar(
            [1.0, 0.0], [[0.9, 0.1], [0.5, 0.5], [1.0, 0.0]], top_k=3, threshold=0.0
        )
        self.assertEqual([i for i, _ in hits], [2, 0, 1])


class AiProviderPluginTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        ai_ports.set_ai(None)

    async def asyncTearDown(self):
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
            self.assertIs(ctx.ai, fake_provider)
            self.assertIs(ai_ports.get_ai(), fake_provider)
        finally:
            await plugin.teardown()
        self.assertIsNone(ai_ports.get_ai())

    async def test_port_functions_forward_to_provider(self):
        # 引擎经 ai_ports 端口函数调用：chat/嵌入转发到注册的供应商
        fake_provider = Mock()
        fake_provider.chat = AsyncMock(return_value="resp")
        fake_provider.embed_texts = AsyncMock(return_value=[[0.1]])
        fake_provider.is_embedding_enabled = Mock(return_value=True)
        fake_provider.embed_model_name = Mock(return_value="m")
        ai_ports.set_ai(fake_provider)
        self.assertEqual(await ai_ports.chat([{"role": "user", "content": "hi"}], temperature=0.1, max_tokens=8), "resp")
        self.assertEqual(await ai_ports.embed_texts(["x"]), [[0.1]])
        self.assertTrue(ai_ports.is_embedding_enabled())
        self.assertEqual(ai_ports.embed_model_name(), "m")
        fake_provider.chat.assert_awaited_once()

    async def test_unregistered_provider_raises_on_chat(self):
        ai_ports.set_ai(None)
        with self.assertRaises(RuntimeError):
            await ai_ports.chat([], temperature=0.1, max_tokens=8)
        self.assertFalse(ai_ports.is_embedding_enabled())  # 启用性检查安全返回 False
