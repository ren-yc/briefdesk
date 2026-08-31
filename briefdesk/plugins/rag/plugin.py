"""RAG 问答插件 — 白名单群聊消息的检索式问答（读路径派生层）。

双能力插件（显式继承 StagePlugin + WebPlugin）：
- StagePlugin(slot=post_insert, priority=10)：批次消息索引（before_run 锁外
  预嵌入、run 锁内落库），priority 恒排 merge(priority=0) 之后；
- WebPlugin：/api/rag/* 路由与「问一问」前端资源（ui/）。

嵌入为硬依赖：ctx.ai 缺失或未启用嵌入（EMBED_* 未配置）时 setup 抛
PluginDisabledError 自禁用——同 ocr 缺可选依赖、qqflow 缺必填配置的自禁用
语义。raw_messages 仍是唯一事实源，rag 四表（chunks/embeddings/fts/skipped，另有 rag_meta）只是派生索引。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter

from briefdesk.events import EVENT_ITEMS_DELETED
from briefdesk.plugin.base import (
    PluginContext,
    PluginDisabledError,
    StagePlugin,
    WebPlugin,
)
from briefdesk.plugins.rag.engine import (
    RagEngine,
    embed_fail_backoff,
    set_engine,
)
from briefdesk.settings_schema import build_settings_schema
from briefdesk.types import BatchContext

logger = logging.getLogger(__name__)


class RagPlugin(StagePlugin, WebPlugin):
    """检索问答插件（入口见模块底部 plugin 实例）。"""

    name = "rag"
    version = "1.0.0"
    dependencies: tuple[str, ...] = ("ai_provider",)
    slot = "post_insert"
    priority = 10

    def __init__(self) -> None:
        self._engine: RagEngine | None = None
        self._backfill_task: asyncio.Task[None] | None = None
        self._gc_task: asyncio.Task[None] | None = None
        # 维护循环的唤醒事件：reindex/降级自愈在循环休眠期踢一脚时立即
        # 执行回填，而不是干等一个维护间隔（默认 1h）后才生效
        self._kick_event = asyncio.Event()

    def settings_schema(self) -> list[dict[str, Any]]:
        from briefdesk.plugins.rag.config import RagSettings

        return build_settings_schema(
            RagSettings,
            plugin=self.name,
            labels={
                "top_k": "向量召回条数",
                "fts_limit": "关键词召回条数",
                "max_evidence": "最大证据条数",
                "min_score": "最低相关度",
                "backfill_days": "历史回填天数",
                "backfill_batch": "回填嵌入批量",
                "backfill_budget_per_cycle": "单轮回填预算",
                "group_only": "仅限群聊",
                "maintenance_interval_seconds": "维护间隔（秒）",
                "model": "问答专用模型",
                "api_base": "问答专用 API 地址",
                "api_key": "问答专用 API Key",
            },
            hints={
                "backfill_days": "0 = 关闭回填；-1 = 全量回填",
                "model": "留空 = 复用主链路 AI_MODEL（分类/去重用的微调模型）",
                "api_base": "留空 = 复用主链路 AI_API_BASE",
                "api_key": "留空 = 复用主链路 AI_API_KEY；密钥只保存到系统钥匙串，不会写入暂存文件",
            },
        )

    def router(self) -> APIRouter:
        from briefdesk.plugins.rag import router as rag_router

        return rag_router.router

    def asset_dir(self) -> Path | None:
        # 插件前端资源目录：核心挂载到 /plugin-assets/rag/（浏览器直连）
        return Path(__file__).parent / "ui"

    async def setup(self, ctx: PluginContext) -> None:
        if ctx.ai is None or not ctx.ai.is_embedding_enabled():
            raise PluginDisabledError(
                "rag 需要嵌入模型：请配置 EMBED_API_BASE / EMBED_API_KEY"
            )
        from briefdesk.plugins.rag.config import RagSettings

        engine = RagEngine(RagSettings())
        try:
            self._engine = engine
            set_engine(engine)
            ctx.register_stage(self)
            ctx.subscribe_event(EVENT_ITEMS_DELETED, self._on_items_deleted)
            ctx.register_router(self.router())
            asset_dir = self.asset_dir()
            if asset_dir is not None:
                ctx.register_plugin_assets(self.name, str(asset_dir))
        except Exception:
            # 半初始化残留清理（单例/引擎引用），避免路由拿到未装配引擎
            await engine.teardown()
            self._engine = None
            set_engine(None)
            raise

    async def activate(self, ctx: PluginContext) -> None:
        # 启动历史回填（fire-and-forget，逐轮有界；DB 已在 setup 前就绪）
        if self._engine is None:
            return
        self._engine.on_backfill_kick = self._spawn_backfill
        self._spawn_backfill()

    def _spawn_backfill(self) -> None:
        # 已有循环在跑则不重复拉起（循环自身会跑到自然结束），但以事件
        # 唤醒休眠期：reindex/自愈的语义是「立即执行回填」而非「下个间隔」
        if self._backfill_task is not None and not self._backfill_task.done():
            self._kick_event.set()
            return
        self._kick_event.clear()
        self._backfill_task = asyncio.create_task(
            self._maintenance_loop(), name="rag-maintenance"
        )

    async def _maintenance_loop(self) -> None:
        """常驻维护：排空回填 → GC 对账 → 缓存整表预热 → 按间隔休眠。

        嵌入失败按指数退避（60s 起、600s 封顶），恢复后立即续跑。
        """

        backoff_step = 0
        while self._engine is not None:
            processed = 0
            failed = False
            try:
                processed = await self._engine.backfill_step(int(time.time()))
                failed = self._engine.last_cycle_embed_failed
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("rag: 回填轮异常，退避续跑")
                failed = True
            if failed:
                wait = embed_fail_backoff(backoff_step)
                backoff_step += 1
                logger.warning("rag: 回填嵌入失败，%.0fs 后重试", wait)
                await asyncio.sleep(wait)
                continue
            backoff_step = 0
            if processed > 0:
                continue  # 仍有存量，立即继续排空
            try:
                await self._engine.maintenance_gc()
                await self._engine.warm_vectors(force_full=True)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("rag: GC/预热异常，下周期重试")
            # 休眠可被 reindex/降级自愈踢醒（_spawn_backfill set 事件），
            # 立即回到回填排空，而非等满一个维护间隔
            try:
                await asyncio.wait_for(
                    self._kick_event.wait(),
                    timeout=self._engine.settings.maintenance_interval_seconds,
                )
            except TimeoutError:
                pass
            self._kick_event.clear()

    async def teardown(self) -> None:
        if self._backfill_task is not None:
            self._backfill_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._backfill_task
            self._backfill_task = None
        if self._gc_task is not None:
            self._gc_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._gc_task
            self._gc_task = None
        if self._engine is not None:
            await self._engine.teardown()
        self._engine = None
        set_engine(None)

    def _on_items_deleted(self, item_ids: list[str]) -> None:
        """同步 handler（发布方持锁）：卡片删除后即时触发一次孤儿对账。

        delete_items 会级联删 raw_messages → rag_chunks/FTS/向量随之孤儿化，
        gc_orphans 对账即清。事件驱动让删除秒级生效（此前最长滞留一个维护
        周期，已删内容仍可被 /api/rag/ask 引用——复核 P2-24）。
        """
        if self._gc_task is not None and not self._gc_task.done():
            return  # 已有待跑/在跑的 GC，本轮对账足以覆盖新删除
        self._gc_task = asyncio.get_running_loop().create_task(
            self._run_gc(), name="rag-delete-gc"
        )

    async def _run_gc(self) -> None:
        # handler 在存储锁内被调用：先让出执行权待发布方释放锁，
        # maintenance_gc 自取存储锁（复核 P2-23）
        await asyncio.sleep(0)
        if self._engine is None:
            return
        try:
            await self._engine.maintenance_gc()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("rag: 删除触发 GC 异常，待维护周期对账兜底")

    async def before_run(self, batch: BatchContext, ctx: PluginContext) -> None:
        # 锁外：批量嵌入（网络调用只允许发生在这里）
        if self._engine is not None:
            await self._engine.before_run(batch)

    async def run(self, batch: BatchContext, ctx: PluginContext) -> None:
        # 锁内：纯 SQLite 落库
        if self._engine is not None:
            await self._engine.run(batch)


plugin = RagPlugin()
