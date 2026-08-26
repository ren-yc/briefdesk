"""RAG 问答插件 — 白名单群聊消息的检索式问答（读路径派生层）。

双能力插件（显式继承 StagePlugin + WebPlugin）：
- StagePlugin(slot=post_insert, priority=10)：批次消息索引（before_run 锁外
  预嵌入、run 锁内落库），priority 恒排 merge(priority=0) 之后；
- WebPlugin：/api/rag/* 路由与「问一问」前端资源（ui/）。

嵌入为硬依赖：ctx.ai 缺失或未启用嵌入（EMBED_* 未配置）时 setup 抛
PluginDisabledError 自禁用——同 ocr 缺可选依赖、qqflow 缺必填配置的自禁用
语义。raw_messages 仍是唯一事实源，rag 三表只是派生索引。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from pathlib import Path

from fastapi import APIRouter

from briefdesk.plugin.base import (
    PluginContext,
    PluginDisabledError,
    StagePlugin,
    WebPlugin,
)
from briefdesk.plugins.rag.engine import RagEngine, set_engine
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
        self._engine = engine
        set_engine(engine)
        ctx.register_stage(self)
        ctx.register_router(self.router())
        asset_dir = self.asset_dir()
        if asset_dir is not None:
            ctx.register_plugin_assets(self.name, str(asset_dir))

    async def activate(self, ctx: PluginContext) -> None:
        # 启动历史回填（fire-and-forget，逐轮有界；DB 已在 setup 前就绪）
        if self._engine is None:
            return
        self._engine.on_backfill_kick = self._spawn_backfill
        self._spawn_backfill()

    def _spawn_backfill(self) -> None:
        # 已有循环在跑则不重复拉起（循环自身会跑到自然结束）
        if self._backfill_task is not None and not self._backfill_task.done():
            return
        self._backfill_task = asyncio.create_task(
            self._backfill_loop(), name="rag-backfill"
        )

    async def _backfill_loop(self) -> None:
        try:
            while self._engine is not None:
                processed = await self._engine.backfill_step(int(time.time()))
                if processed <= 0:
                    return
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — 回填失败不影响主流程
            logger.exception("rag: 历史回填异常终止")

    async def teardown(self) -> None:
        if self._backfill_task is not None:
            self._backfill_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._backfill_task
            self._backfill_task = None
        self._engine = None
        set_engine(None)

    async def before_run(self, batch: BatchContext, ctx: PluginContext) -> None:
        # 锁外：批量嵌入（网络调用只允许发生在这里）
        if self._engine is not None:
            await self._engine.before_run(batch)

    async def run(self, batch: BatchContext, ctx: PluginContext) -> None:
        # 锁内：纯 SQLite 落库
        if self._engine is not None:
            await self._engine.run(batch)


plugin = RagPlugin()
