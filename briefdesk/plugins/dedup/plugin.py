"""语义去重阶段插件（slot=dedup）— 判重、入库与缓存维护。

- before_run（存储锁外）：行规划（标题/描述 + 批内预嵌入，每批最多一次
  嵌入 API 调用，避免在 _storage_lock 内 await 远程嵌入阻塞整条管道）；
- run（存储锁内，由骨架持有锁）：逐行判重 → 重复则标记 processed /
  否则入库 + 缓存追加；
- after_run（锁外）：批量持久化本批待落库向量（一次 DB 调用）。

setup：构造 DedupEngine、预热缓存（HTTP 服务启动前、源启动前），
把引擎注册为 ctx.dedup 服务端口（供 merge 阶段与事件清理同步缓存），
并订阅 items_deleted 事件（核心删除卡片后同步清缓存）。
"""

import logging
from typing import TYPE_CHECKING

from briefdesk.db import insert_item, mark_message_processed
from briefdesk.events import EVENT_ITEMS_DELETED
from briefdesk.plugin.base import PluginContext, StagePlugin
from briefdesk.plugins.dedup.engine import build_item_input
from briefdesk.types import (
    BatchContext,
    ClassifyResult,
    DedupCheck,
    InsertedRow,
    InternalMessage,
)

if TYPE_CHECKING:
    from briefdesk.plugins.dedup.engine import DedupEngine

logger = logging.getLogger(__name__)


class DedupPlugin(StagePlugin):
    """去重阶段插件（显式实现 StagePlugin；入口见模块底部 `plugin` 实例）。"""

    name = "dedup"
    version = "1.0.0"
    dependencies: tuple[str, ...] = ("ai_provider",)  # 判重依赖 AI 供应商
    slot = "dedup"
    priority = 0

    def __init__(self) -> None:
        self._engine: DedupEngine | None = None

    async def setup(self, ctx: PluginContext) -> None:
        # 延迟导入：仅加载本插件依赖，且便于测试替换
        from briefdesk.plugins.dedup import engine as dedup_engine

        engine = dedup_engine.DedupEngine()
        # 预热：全量加载历史条目 + 缺失向量嵌入（源监听启动前执行，
        # 避免首个批次在 _storage_lock 内触发全量嵌入阻塞整条管道）
        await engine.ensure_cache()
        self._engine = engine
        ctx.dedup = engine  # 服务端口：merge 阶段 / 其它插件经此同步缓存
        ctx.register_stage(self)
        ctx.subscribe_event(EVENT_ITEMS_DELETED, self._on_items_deleted)

    def _on_items_deleted(self, item_ids: list[str]) -> None:
        """核心删除卡片后同步清理缓存（同步处理器：发布方持锁时仍保持原子）。"""
        if self._engine is not None:
            self._engine.remove_items(item_ids)

    async def before_run(self, batch: BatchContext, ctx: PluginContext) -> None:
        """锁外：行规划（标题/描述）与批内预嵌入。"""
        if self._engine is None:
            return
        outcome = batch.outcomes
        if outcome is None or not outcome.results:
            return
        planned: list[tuple[InternalMessage, ClassifyResult, str]] = []
        for result in outcome.results:
            msg = (
                batch.messages[result.msg_index]
                if 0 <= result.msg_index < len(batch.messages)
                else None
            )
            if msg is None:
                continue
            title = result.summary or msg.content[:50]
            logger.info('[%s] "%s" — %s', result.category, title, msg.group_name)
            planned.append((msg, result, title))
        # 预嵌入文本用原文 msg.content（去重判定/入库/缓存统一口径，替代原 description）
        embs = await self._engine.preembed_batch(
            [(t, m.content) for m, _r, t in planned]
        )
        batch.rows = [
            (m, r, t, (embs[i] if embs is not None else None))
            for i, (m, r, t) in enumerate(planned)
        ]

    async def run(self, batch: BatchContext, ctx: PluginContext) -> None:
        """锁内（骨架持有 _storage_lock）：判重 → 入库/标记重复 → 缓存追加。"""
        if self._engine is None:
            return
        for msg, result, title, q_emb in batch.rows:
            dedup_result = await self._engine.check_dedup(
                title,
                msg.group_name,
                q_emb=q_emb,
                image_urls=msg.image_urls,
                source=msg.source,
                source_quote=msg.content,
            )
            # 判定观察记录：仅记录发生了实际比较的判定（命中候选或候选被判定
            # 为不同）；无候选（未比较）不记录——供观察型阶段插件（benchmark）
            # 在真实处理时点导出基准用例（如判重命中的 same=true 对）
            candidate = dedup_result.candidate
            if candidate is not None:
                batch.dedup_checks.append(
                    DedupCheck(
                        msg=msg,
                        title=title,
                        is_duplicate=dedup_result.is_duplicate,
                        candidate=candidate,
                    )
                )
            if dedup_result.is_duplicate:
                batch.dupes += 1
                await mark_message_processed(msg.source, msg.msg_id)
                continue
            item_id = await insert_item(
                build_item_input(msg, result, title)
            )
            await mark_message_processed(msg.source, msg.msg_id)
            # 内存同步追加（含预计算向量、图片集合、源名与原文）；向量落库由 after_run 批量完成
            self._engine.add_to_cache(
                item_id,
                title,
                embedding=q_emb,
                image_urls=msg.image_urls,
                source=msg.source,
                source_quote=msg.content,
            )
            batch.inserted.append(
                InsertedRow(
                    item_id=item_id,
                    msg=msg,
                    result=result,
                    title=title,
                )
            )

    async def after_run(self, batch: BatchContext, ctx: PluginContext) -> None:
        """锁外：批量持久化本批新增向量。"""
        if self._engine is not None:
            await self._engine.flush_pending_embeddings()

    async def activate(self, ctx: PluginContext) -> None: ...

    async def teardown(self) -> None:
        self._engine = None


plugin = DedupPlugin()
