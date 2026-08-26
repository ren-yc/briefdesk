"""RAG 插件测试：setup 自禁用/注册、库层、索引、回填、检索、问答路由。"""

import unittest
from unittest.mock import AsyncMock, Mock

from briefdesk.config import Settings
from briefdesk.plugin.base import PluginContext, PluginDisabledError
from briefdesk.plugins.rag.plugin import RagPlugin
from briefdesk.types import BatchContext


def _noop_sync(*args, **kwargs):
    return None


async def _noop_async(*args, **kwargs):
    return None


def _ctx(provider=None):
    """构造带记录器的 PluginContext（镜像 test_stage_plugins._ctx 风格）。"""

    registered_stages, routers, assets = [], [], []
    ctx = PluginContext(
        config=Settings(
            plugins=["*"], plugins_disabled=[], plugins_required=[], plugin_path=""
        ),
        publish_event=_noop_async,
        subscribe_event=lambda event, handler: None,
        register_source=_noop_sync,
        register_stage=registered_stages.append,
        ai=provider,
        register_router=routers.append,
        register_plugin_assets=lambda name, path: assets.append((name, path)),
    )
    return ctx, registered_stages, routers, assets


def _embed_provider(enabled=True):
    provider = Mock()
    provider.is_embedding_enabled = Mock(return_value=enabled)
    return provider


class RagSetupTest(unittest.IsolatedAsyncioTestCase):
    async def test_setup_without_ai_self_disables(self):
        ctx, *_ = _ctx(None)
        with self.assertRaises(PluginDisabledError):
            await RagPlugin().setup(ctx)

    async def test_setup_without_embedding_self_disables(self):
        ctx, *_ = _ctx(_embed_provider(enabled=False))
        with self.assertRaises(PluginDisabledError):
            await RagPlugin().setup(ctx)

    async def test_setup_registers_stage_router_assets(self):
        ctx, stages_, routers, assets = _ctx(_embed_provider(True))
        plugin = RagPlugin()
        await plugin.setup(ctx)
        try:
            self.assertEqual(plugin.slot, "post_insert")
            self.assertEqual(plugin.priority, 10)  # 恒排 merge(priority=0) 之后
            self.assertEqual(stages_, [plugin])
            self.assertEqual(len(routers), 1)
            self.assertEqual(len(assets), 1)
            self.assertEqual(assets[0][0], "rag")
        finally:
            await plugin.teardown()

    async def test_teardown_clears_engine_singleton(self):
        from briefdesk.plugins.rag.engine import get_engine

        ctx, *_ = _ctx(_embed_provider(True))
        plugin = RagPlugin()
        await plugin.setup(ctx)
        try:
            self.assertIsNotNone(get_engine())
        finally:
            await plugin.teardown()
        self.assertIsNone(get_engine())

    async def test_hooks_noop_without_engine(self):
        # teardown 后钩子安全空转（引擎缺失不抛错）
        plugin = RagPlugin()
        ctx, *_ = _ctx(_embed_provider(True))
        await plugin.before_run(object(), ctx)  # type: ignore[arg-type]
        await plugin.run(object(), ctx)  # type: ignore[arg-type]


class RagMetaTest(unittest.TestCase):
    def test_declares_ai_provider_dependency(self):
        # ai_provider 被禁用时 rag 随依赖未就绪自动降级（拓扑序保证其先 setup）
        self.assertEqual(RagPlugin.dependencies, ("ai_provider",))


class RagDbTest(unittest.IsolatedAsyncioTestCase):
    """库层测试（内存库 + 核心 init_schema，不触碰应用数据库文件）。"""

    async def asyncSetUp(self):
        import aiosqlite

        from briefdesk.db import init_schema
        from briefdesk.plugins.rag.db import ensure_rag_schema

        self.db = await aiosqlite.connect(":memory:")
        self.db.row_factory = aiosqlite.Row
        await init_schema(self.db)
        await ensure_rag_schema(self.db)

    async def asyncTearDown(self):
        await self.db.close()

    @staticmethod
    def _row(msg_id="m1", content="周六6点开会有通知，别迟到", item_id="", ts=1700000000):
        from briefdesk.plugins.rag.db import ChunkRow

        return ChunkRow(
            source="weflow", msg_id=msg_id, session_id="s1", group_name="测试群",
            sender_name="小明", msg_time=ts, content=content, item_id=item_id,
        )

    async def _seed_raw(self, msg_id, ts=1700000000):
        cursor = await self.db.execute(
            "INSERT INTO raw_messages(source,msg_id,session_id,group_name,"
            "sender_id,sender_name,content,timestamp) VALUES(?,?,?,?,?,?,?,?)",
            ("weflow", msg_id, "s1", "测试群", "u1", "小明", "内容" + msg_id, ts),
        )
        await cursor.close()
        await self.db.commit()

    async def test_upsert_chunks_idempotent_and_update(self):
        from briefdesk.plugins.rag.db import upsert_chunks

        await upsert_chunks(self.db, [self._row()])
        await upsert_chunks(self.db, [self._row(item_id="i9")])  # 重处理覆盖
        cursor = await self.db.execute("SELECT COUNT(*) AS c, item_id FROM rag_chunks")
        try:
            row = await cursor.fetchone()
        finally:
            await cursor.close()
        self.assertEqual(row["c"], 1)
        self.assertEqual(row["item_id"], "i9")

    async def test_fts_trigram_long_query_hits(self):
        from briefdesk.plugins.rag.db import (
            ensure_fts,
            fts_search,
            sync_fts,
            upsert_chunks,
        )

        self.assertTrue(await ensure_fts(self.db))
        await upsert_chunks(self.db, [self._row()])  # 事实源先行（fts_search 读它）
        await sync_fts(self.db, [self._row()])
        hits = await fts_search(self.db, "有通知", limit=10)  # ≥3 字符走 FTS MATCH
        self.assertEqual([h.msg_id for h in hits], ["m1"])

    async def test_short_chinese_query_like_fallback(self):
        from briefdesk.plugins.rag.db import (
            ensure_fts,
            fts_search,
            sync_fts,
            upsert_chunks,
        )

        await ensure_fts(self.db)
        await upsert_chunks(self.db, [self._row()])  # 事实源先行
        await sync_fts(self.db, [self._row()])
        hits = await fts_search(self.db, "开会", limit=10)  # 2 字符：trigram 盲区 → LIKE
        self.assertEqual([h.msg_id for h in hits], ["m1"])
        self.assertEqual(await fts_search(self.db, "%", limit=10), [])  # 通配符不越权

    async def test_fts_session_filter(self):
        from briefdesk.plugins.rag.db import ensure_fts, fts_search, sync_fts, upsert_chunks

        await ensure_fts(self.db)
        other = self._row("m2")
        other.session_id = "s2"
        await upsert_chunks(self.db, [self._row(), other])
        await sync_fts(self.db, [self._row(), other])
        hits = await fts_search(self.db, "有通知", limit=10, session_id="s1")
        self.assertEqual([h.msg_id for h in hits], ["m1"])

    async def test_embeddings_model_semantics_and_corrupt_row(self):
        from briefdesk.plugins.rag.db import (
            load_embeddings,
            upsert_chunks,
            upsert_embeddings,
        )

        await upsert_chunks(self.db, [self._row(), self._row("m2")])
        await upsert_embeddings(
            self.db, [("weflow", "m1"), ("weflow", "m2")],
            [[0.1, 0.2], [0.3, 0.4]], "old-model", "t0",
        )
        chunks, vecs = await load_embeddings(self.db, "old-model")
        self.assertEqual([c.msg_id for c in chunks], ["m1", "m2"])
        self.assertEqual(vecs[0], [0.1, 0.2])
        # 模型变更后旧行自然失配（触发回填重嵌入的语义基础）
        fresh_chunks, _ = await load_embeddings(self.db, "new-model")
        self.assertEqual(fresh_chunks, [])
        await upsert_embeddings(self.db, [("weflow", "m1")], [[0.9]], "new-model", "t1")
        re_chunks, re_vecs = await load_embeddings(self.db, "new-model")
        self.assertEqual([c.msg_id for c in re_chunks], ["m1"])
        self.assertEqual(re_vecs, [[0.9]])
        # 脏 JSON 行：读时剔除并删行（回填反连接下一轮自动重嵌入）
        cursor = await self.db.execute(
            "UPDATE rag_chunk_embeddings SET embedding='not-json' WHERE msg_id='m1'"
        )
        await cursor.close()
        await self.db.commit()
        fixed_chunks, _ = await load_embeddings(self.db, "new-model")
        self.assertEqual(fixed_chunks, [])
        cursor = await self.db.execute(
            "SELECT COUNT(*) AS c FROM rag_chunk_embeddings WHERE msg_id='m1'"
        )
        try:
            row = await cursor.fetchone()
        finally:
            await cursor.close()
        self.assertEqual(row["c"], 0)

    async def test_gc_orphans_removes_missing_raw(self):
        from briefdesk.plugins.rag.db import gc_orphans, upsert_chunks

        await self._seed_raw("keep")
        await upsert_chunks(self.db, [self._row("keep"), self._row("orphan")])
        removed = await gc_orphans(self.db)
        self.assertGreaterEqual(removed, 1)
        cursor = await self.db.execute("SELECT msg_id FROM rag_chunks")
        try:
            ids = {r["msg_id"] for r in await cursor.fetchall()}
        finally:
            await cursor.close()
        self.assertEqual(ids, {"keep"})

    async def test_count_status_shape(self):
        from briefdesk.plugins.rag.db import count_status, ensure_fts, upsert_chunks

        await ensure_fts(self.db)
        await upsert_chunks(self.db, [self._row()])
        status = await count_status(self.db)
        self.assertEqual(status["rag_chunks"], 1)
        self.assertEqual(status["fts_tokenizer"], "trigram")


def _msg(msg_id="m1", content="周六6点开会有通知", ts=1700000000):
    from briefdesk.types import InternalMessage

    return InternalMessage(
        msg_id=msg_id, content=content, sender_name="小明", sender_id="u1",
        session_id="s1", group_name="测试群", timestamp=ts, source="weflow",
    )


class RagIndexTest(unittest.IsolatedAsyncioTestCase):
    """索引路径：before_run 锁外预嵌入 / run 锁内落库（内存库 + fake 供应商）。"""

    async def asyncSetUp(self):
        import aiosqlite

        from briefdesk import ai_ports
        from briefdesk.db import init_schema
        from briefdesk.plugins.rag.config import RagSettings as RS
        from briefdesk.plugins.rag.engine import RagEngine

        self.db = await aiosqlite.connect(":memory:")
        self.db.row_factory = aiosqlite.Row
        await init_schema(self.db)

        self.provider = Mock()
        self.provider.is_embedding_enabled = Mock(return_value=True)
        self.provider.embed_model_name = Mock(return_value="test-model")
        # 按输入长度动态返回（数量守卫测试依赖严格一致）
        def _fake_embed(texts):
            return [[min(float(len(t)), 100.0), 0.0] for t in texts]

        self.provider.embed_texts = AsyncMock(side_effect=_fake_embed)
        ai_ports.set_ai(self.provider)

        async def _factory():
            return self.db

        self.engine = RagEngine(RS(), db_factory=_factory)

    async def asyncTearDown(self):
        from briefdesk import ai_ports

        ai_ports.set_ai(None)
        await self.engine.teardown()
        await self.db.close()

    def _batch(self, messages, inserted_pairs=()):
        from briefdesk.types import BatchContext, ClassifyResult, InsertedRow

        batch = BatchContext(messages=messages, client=Mock())
        for i, (msg, item_id) in enumerate(inserted_pairs):
            batch.inserted.append(
                InsertedRow(item_id=item_id, msg=msg,
                            result=ClassifyResult(msg_index=i), title="标题")
            )
        return batch

    async def test_index_writes_chunks_fts_embeddings_and_item_map(self):
        m1 = _msg("m1", "周六6点开会有通知")
        m2 = _msg("m2", "学术讲座在周五下午", ts=1700003600)
        batch = self._batch([m1, m2], [(m1, "i1")])

        await self.engine.before_run(batch)
        self.assertEqual(len(self.engine._pending), 2)  # 锁外预嵌入完成
        await self.engine.run(batch)
        self.assertEqual(self.engine._pending, {})  # 消费即清

        cursor = await self.db.execute(
            "SELECT msg_id, item_id FROM rag_chunks ORDER BY msg_id"
        )
        try:
            rows = [dict(r) for r in await cursor.fetchall()]
        finally:
            await cursor.close()
        self.assertEqual(rows, [
            {"msg_id": "m1", "item_id": "i1"},
            {"msg_id": "m2", "item_id": ""},
        ])
        cursor = await self.db.execute(
            "SELECT COUNT(*) AS c FROM rag_chunk_embeddings WHERE model='test-model'"
        )
        try:
            row = await cursor.fetchone()
        finally:
            await cursor.close()
        self.assertEqual(row["c"], 2)
        cursor = await self.db.execute("SELECT COUNT(*) AS c FROM rag_fts")
        try:
            row = await cursor.fetchone()
        finally:
            await cursor.close()
        self.assertEqual(row["c"], 2)

    async def test_embed_failure_still_indexes_content(self):
        # 嵌入失败只丢向量：内容照常入索引，缺向量由回填反连接补齐
        self.provider.embed_texts = AsyncMock(side_effect=RuntimeError("boom"))
        m1 = _msg("m1", "周六6点开会有通知")
        batch = self._batch([m1])
        await self.engine.before_run(batch)
        await self.engine.run(batch)
        cursor = await self.db.execute("SELECT COUNT(*) AS c FROM rag_chunks")
        try:
            row = await cursor.fetchone()
        finally:
            await cursor.close()
        self.assertEqual(row["c"], 1)
        cursor = await self.db.execute("SELECT COUNT(*) AS c FROM rag_chunk_embeddings")
        try:
            row = await cursor.fetchone()
        finally:
            await cursor.close()
        self.assertEqual(row["c"], 0)

    async def test_blank_content_not_indexed(self):
        m1 = _msg("m1", "   ")  # 空白内容
        m2 = _msg("m2", "正常消息")
        batch = self._batch([m1, m2])
        await self.engine.before_run(batch)
        await self.engine.run(batch)
        cursor = await self.db.execute("SELECT msg_id FROM rag_chunks")
        try:
            ids = {r["msg_id"] for r in await cursor.fetchall()}
        finally:
            await cursor.close()
        self.assertEqual(ids, {"m2"})


class RagBackfillTest(unittest.IsolatedAsyncioTestCase):
    """历史回填：窗口/预算/续跑/模型切换重嵌入（内存库 + fake 供应商）。"""

    async def asyncSetUp(self):
        import aiosqlite

        from briefdesk import ai_ports
        from briefdesk.db import init_schema
        from briefdesk.plugins.rag.config import RagSettings as RS
        from briefdesk.plugins.rag.engine import RagEngine

        self.db = await aiosqlite.connect(":memory:")
        self.db.row_factory = aiosqlite.Row
        await init_schema(self.db)

        self.provider = Mock()
        self.provider.is_embedding_enabled = Mock(return_value=True)
        self.model = "model-a"
        self.provider.embed_model_name = Mock(side_effect=lambda: self.model)

        def _fake_embed(texts):
            return [[min(float(len(t)), 100.0), 0.0] for t in texts]

        self.provider.embed_texts = AsyncMock(side_effect=_fake_embed)
        ai_ports.set_ai(self.provider)

        async def _factory():
            return self.db

        self.now = 1_800_000_000
        self.day = 86400
        self.engine = RagEngine(RS(), db_factory=_factory)

    async def asyncTearDown(self):
        from briefdesk import ai_ports

        ai_ports.set_ai(None)
        await self.engine.teardown()
        await self.db.close()

    async def _seed(self, msg_id, age_days):
        cursor = await self.db.execute(
            "INSERT INTO raw_messages(source,msg_id,session_id,group_name,"
            "sender_id,sender_name,content,timestamp) VALUES(?,?,?,?,?,?,?,?)",
            ("weflow", msg_id, "s1", "测试群", "u1", "小明",
             "历史消息" + msg_id, self.now - int(age_days * self.day)),
        )
        await cursor.close()
        await self.db.commit()

    async def _seed_window_fixture(self):
        await self._seed("old", 8)      # 窗口外（7 天）
        await self._seed("d3", 3)
        await self._seed("d1", 1)
        await self._seed("h", 1 / 24)   # 一小时前

    async def _chunk_ids(self):
        cursor = await self.db.execute("SELECT msg_id FROM rag_chunks ORDER BY msg_id")
        try:
            ids = [r["msg_id"] for r in await cursor.fetchall()]
        finally:
            await cursor.close()
        return ids

    async def _embed_count_by_model(self):
        cursor = await self.db.execute(
            "SELECT model, COUNT(*) AS c FROM rag_chunk_embeddings GROUP BY model"
        )
        try:
            out = {r["model"]: r["c"] for r in await cursor.fetchall()}
        finally:
            await cursor.close()
        return out

    async def test_window_budget_and_resume(self):
        from briefdesk.plugins.rag.config import RagSettings as RS

        await self._seed_window_fixture()
        engine = self.engine
        engine.settings = RS(backfill_days=7, backfill_budget_per_cycle=2,
                             backfill_batch=64)
        self.assertEqual(await engine.backfill_step(self.now), 2)  # 预算截断
        ids = await self._chunk_ids()
        self.assertEqual(len(ids), 2)  # 最新优先：h、d1
        self.assertEqual(await engine.backfill_step(self.now), 1)  # 续跑 d3
        self.assertEqual(sorted(await self._chunk_ids()), ["d1", "d3", "h"])
        self.assertEqual(await engine.backfill_step(self.now), 0)  # 完成（old 在窗外）
        self.assertEqual(await engine.backfill_step(self.now), 0)  # 幂等

    async def test_full_and_off_modes(self):
        from briefdesk.plugins.rag.config import RagSettings as RS

        await self._seed_window_fixture()
        self.engine.settings = RS(backfill_days=0)
        self.assertEqual(await self.engine.backfill_step(self.now), 0)  # 关闭
        self.engine.settings = RS(backfill_days=-1)
        self.assertEqual(await self.engine.backfill_step(self.now), 4)  # 全量含 old
        self.assertEqual(sorted(await self._chunk_ids()),
                         ["d1", "d3", "h", "old"])

    async def test_model_switch_triggers_reembed(self):
        from briefdesk.plugins.rag.config import RagSettings as RS

        self.engine.settings = RS(backfill_days=7)
        await self._seed_window_fixture()
        while await self.engine.backfill_step(self.now):
            pass
        before = await self._embed_count_by_model()
        self.assertEqual(before.get("model-a"), 3)
        self.model = "model-b"  # 供应商换模型 → 反连接按失配重嵌入
        self.assertEqual(await self.engine.backfill_step(self.now), 3)
        after = await self._embed_count_by_model()
        self.assertNotIn("model-a", after)
        self.assertEqual(after.get("model-b"), 3)


class RagRetrieveTest(unittest.IsolatedAsyncioTestCase):
    """混合检索：RRF 融合、会话过滤、拒答门（文本键控向量，完全可控）。"""

    VECTORS = {
        "周六6点开会有通知": [1.0, 0.0],
        "学术讲座在周五下午": [0.0, 1.0],
        "二手自行车出售": [0.7, 0.7],
    }

    async def asyncSetUp(self):
        import aiosqlite

        from briefdesk import ai_ports
        from briefdesk.db import init_schema
        from briefdesk.plugins.rag.config import RagSettings as RS
        from briefdesk.plugins.rag.engine import RagEngine

        self.db = await aiosqlite.connect(":memory:")
        self.db.row_factory = aiosqlite.Row
        await init_schema(self.db)

        provider = Mock()
        provider.is_embedding_enabled = Mock(return_value=True)
        provider.embed_model_name = Mock(return_value="m")

        def fake_embed(texts):
            return [list(self.VECTORS.get(t, [0.0, 0.0])) for t in texts]

        provider.embed_texts = AsyncMock(side_effect=fake_embed)
        ai_ports.set_ai(provider)

        async def _factory():
            return self.db

        self.engine = RagEngine(RS(), db_factory=_factory)
        # 索引三条消息（含两个会话）
        for msg_id, content, session in (
            ("m1", "周六6点开会有通知", "s1"),
            ("m2", "学术讲座在周五下午", "s1"),
            ("m3", "二手自行车出售", "s2"),
        ):
            batch = BatchContext(
                messages=[self._msg(msg_id, content, session)], client=Mock()
            )
            await self.engine.before_run(batch)
            await self.engine.run(batch)

    async def asyncTearDown(self):
        from briefdesk import ai_ports

        ai_ports.set_ai(None)
        await self.engine.teardown()
        await self.db.close()

    @staticmethod
    def _msg(msg_id, content, session="s1"):
        from briefdesk.types import InternalMessage

        return InternalMessage(
            msg_id=msg_id, content=content, sender_name="小明", sender_id="u1",
            session_id=session, group_name="测试群", timestamp=1700000000,
            source="weflow",
        )

    @staticmethod
    def _batch(messages):
        from briefdesk.types import BatchContext

        return BatchContext(messages=messages, client=Mock())

    async def test_vector_top1(self):
        hits = await self.engine.retrieve("周六6点开会有通知")
        assert hits is not None
        self.assertEqual(hits[0].chunk.msg_id, "m1")
        self.assertAlmostEqual(hits[0].cos, 1.0, places=5)

    async def test_empty_question_returns_none(self):
        self.assertIsNone(await self.engine.retrieve("   "))

    async def test_refusal_on_zero_similarity_without_fts(self):
        # 未知查询 → 零向量（全零余弦）且无关键词命中 → 诚实拒答
        self.assertIsNone(await self.engine.retrieve("完全不相关的问题"))

    async def test_fts_rescues_low_cosine(self):
        # 「开会有通知」是 m1 内容子串（≥3 字符走 FTS），但向量映射为零：
        # 无向量命中、余弦 0 —— 关键词硬证据必须放行
        hits = await self.engine.retrieve("开会有通知")
        assert hits is not None
        self.assertEqual([h.chunk.msg_id for h in hits], ["m1"])
        self.assertTrue(hits[0].has_fts)

    async def test_session_filter_scopes_both_legs(self):
        hits = await self.engine.retrieve("周六6点开会有通知", session_id="s2")
        assert hits is not None
        self.assertEqual([h.chunk.msg_id for h in hits], ["m3"])


def _chat_response(text):
    from types import SimpleNamespace

    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))]
    )


class RagAskTest(unittest.IsolatedAsyncioTestCase):
    """问答路径：引用抽取、无标注回退、拒答不调 AI、失败上抛。"""

    VECTORS = RagRetrieveTest.VECTORS

    async def asyncSetUp(self):
        import aiosqlite

        from briefdesk import ai_ports
        from briefdesk.db import init_schema
        from briefdesk.plugins.rag.config import RagSettings as RS
        from briefdesk.plugins.rag.engine import RagEngine

        self.db = await aiosqlite.connect(":memory:")
        self.db.row_factory = aiosqlite.Row
        await init_schema(self.db)

        provider = Mock()
        provider.is_embedding_enabled = Mock(return_value=True)
        provider.embed_model_name = Mock(return_value="m")

        def fake_embed(texts):
            return [list(self.VECTORS.get(t, [0.0, 0.0])) for t in texts]

        provider.embed_texts = AsyncMock(side_effect=fake_embed)
        self.provider = provider
        self.provider.chat = AsyncMock(
            return_value=_chat_response("活动在周六6点举行 [1]。")
        )
        ai_ports.set_ai(provider)

        async def _factory():
            return self.db

        self.engine = RagEngine(RS(), db_factory=_factory)
        for msg_id, content, session in (
            ("m1", "周六6点开会有通知", "s1"),
            ("m2", "学术讲座在周五下午", "s1"),
        ):
            batch = BatchContext(
                messages=[RagRetrieveTest._msg(msg_id, content, session)], client=Mock()
            )
            await self.engine.before_run(batch)
            await self.engine.run(batch)

    async def asyncTearDown(self):
        from briefdesk import ai_ports

        ai_ports.set_ai(None)
        await self.engine.teardown()
        await self.db.close()

    async def test_answer_with_citation_subset(self):
        result = await self.engine.ask("周六6点开会有通知")
        self.assertFalse(result.refused)
        self.assertEqual([c["msg_id"] for c in result.citations], ["m1"])
        self.assertEqual(result.citations[0]["n"], 1)

    async def test_citation_fallback_when_unmarked(self):
        self.provider.chat = AsyncMock(return_value=_chat_response("活动在周六6点。"))
        result = await self.engine.ask("周六6点开会有通知")
        # 模型没标 [n]：回退全部证据，保持可核查
        self.assertEqual([c["msg_id"] for c in result.citations], ["m1"])

    async def test_refusal_skips_chat(self):
        result = await self.engine.ask("完全不相关的问题")
        self.assertTrue(result.refused)
        self.assertEqual(result.citations, [])
        self.provider.chat.assert_not_awaited()

    async def test_chat_failure_propagates(self):
        self.provider.chat = AsyncMock(side_effect=RuntimeError("ai down"))
        with self.assertRaises(RuntimeError):
            await self.engine.ask("周六6点开会有通知")


class RagRouterTest(unittest.IsolatedAsyncioTestCase):
    """路由组：ask 形状/校验/503、status 键、reindex 202（TestClient）。"""

    def setUp(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from briefdesk.plugins.rag import router as rag_router_module

        self.router_module = rag_router_module
        app = FastAPI()
        app.include_router(rag_router_module.router)
        self.client = TestClient(app)

    def _fake_engine(self):
        from unittest.mock import Mock

        from briefdesk.plugins.rag.config import RagSettings as RS
        from briefdesk.plugins.rag.engine import AskResult

        engine = Mock()
        engine.settings = RS()
        engine.request_backfill = Mock(return_value=True)
        engine.ask = AsyncMock(
            return_value=AskResult(
                refused=False,
                answer="周六6点 [1]。",
                citations=[{
                    "n": 1, "msg_id": "m1", "source": "weflow",
                    "session_id": "s1",
                    "sender_name": "小明", "time": 1700000000,
                    "group_name": "测试群", "snippet": "周六6点开会",
                    "item_id": "",
                }],
            )
        )
        return engine

    async def test_ask_roundtrip_and_shapes(self):
        from unittest.mock import patch

        engine = self._fake_engine()
        with patch.object(self.router_module, "get_engine", return_value=engine):
            resp = self.client.post(
                "/api/rag/ask",
                json={"question": "周六几点开会？"},
            )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertFalse(body["refused"])
        self.assertEqual(body["answer"], "周六6点 [1]。")
        self.assertEqual(body["citations"][0]["msg_id"], "m1")

    def test_ask_validation_and_unready(self):
        from unittest.mock import patch

        with patch.object(self.router_module, "get_engine", return_value=None):
            resp = self.client.post("/api/rag/ask", json={"question": "周六几点？"})
            self.assertEqual(resp.status_code, 503)
        engine = self._fake_engine()
        with patch.object(self.router_module, "get_engine", return_value=engine):
            resp = self.client.post("/api/rag/ask", json={"question": "短"})
            self.assertEqual(resp.status_code, 422)  # min_length=2

    async def test_status_and_reindex(self):
        import aiosqlite
        from unittest.mock import AsyncMock, patch

        from briefdesk.db import init_schema
        from briefdesk.plugins.rag.db import ensure_rag_schema

        db = await aiosqlite.connect(":memory:")
        try:
            db.row_factory = aiosqlite.Row
            await init_schema(db)
            await ensure_rag_schema(db)
            engine = self._fake_engine()
            with (
                patch.object(self.router_module, "get_engine", return_value=engine),
                patch.object(self.router_module, "get_db", new=AsyncMock(return_value=db)),
            ):
                status = self.client.get("/api/rag/status")
                self.assertEqual(status.status_code, 200)
                for key in ("chunks", "embedded", "fts_tokenizer", "model", "backfill_days"):
                    self.assertIn(key, status.json())
                reindex = self.client.post("/api/rag/reindex")
                self.assertEqual(reindex.status_code, 202)
                body = reindex.json()
                self.assertIn("removed", body)
                self.assertTrue(body["kicked"])
                engine.request_backfill.assert_called_once()
        finally:
            await db.close()
