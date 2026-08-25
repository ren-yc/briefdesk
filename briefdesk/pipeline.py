"""消息处理管道骨架 — 入口过滤/落库 + 阶段链编排。

具体阶段（OCR 增强 / AI 分类 / 语义去重 / 同话题合并）由 StagePlugin
实现，经 PluginContext.register_stage 注册到 briefdesk.stages；本模块只做
编排，不 import 任何具体 AI/OCR 实现：

  盖章 → 过滤（自消息/启用会话/纯占位符图片（OCR 未启用）/已处理）→ raw 落库 → 切批
  → 并行：enrich + classify（锁外）
  → 串行（_storage_lock 内）：dedup（判重/入库/缓存）→ 跳过标记 → post_insert（合并）
  → 锁外：dedup after_run（向量落库）→ 计数 → 状态 → 实时通知
"""

import asyncio
import logging
import re
import time as time_module
from datetime import UTC, datetime

from briefdesk.config import config
from briefdesk.db import (
    RawMsgInput,
    are_messages_processed,
    bulk_insert_raw_messages,
    get_enabled_categories,
    get_enabled_sessions,
    mark_message_processed,
)
from briefdesk.db import storage_lock as _storage_lock
from briefdesk.logger import fmt_dur
from briefdesk.realtime import publish_items_updated, publish_sync_progress
from briefdesk.sources_base import SourceClient
from briefdesk.stages import get_context, get_stages
from briefdesk.status import (
    get_sync_progress,
    note_sync_batch_done,
    note_sync_batch_start,
    set_status,
)
from briefdesk.types import BatchContext, InternalMessage

logger = logging.getLogger(__name__)

# 纯附件占位符消息（整条内容为单个方括号片段）：[图片]/[image]/[语音]/[视频]…
# 与源侧 normalize 的占位符判定语义一致（qqflow 的通用方括号模式；weflow
# 的 [图片] 亦匹配）。OCR 未启用（enrich 槽位为空）时，这类带图消息无信息
# 价值，入口直接屏蔽（不落 raw/不进分类/不标记 processed）；图片+文字
# 混合消息（content 非占位符）不受影响，文字照常处理。
_PLACEHOLDER_ONLY_RE = re.compile(r"^\s*\[[^\]]+\]\s*$")


def _split_batches(
    messages: list[InternalMessage], batch_size: int
) -> list[list[InternalMessage]]:
    """按 batch_size 切批。"""
    return [messages[i : i + batch_size] for i in range(0, len(messages), batch_size)]


async def _mark_skipped(bctx: BatchContext, failed_set: set[int]) -> None:
    """未选中且非失败的消息标记 processed（闲聊跳过）；无分类结果时全批标记。

    被滤自消息不标记 processed（回填窗口内每轮重滤、关闭 IGNORE_SELF 可恢复）。
    """
    outcome = bctx.outcomes
    if outcome is None or not outcome.results:
        for i, msg in enumerate(bctx.messages):
            if i in failed_set:
                continue
            await mark_message_processed(msg.source, msg.msg_id)
            bctx.skipped += 1
        return
    selected = {
        r.msg_index for r in outcome.results if 0 <= r.msg_index < len(bctx.messages)
    }
    for i, msg in enumerate(bctx.messages):
        if i in selected or i in failed_set:
            continue
        await mark_message_processed(msg.source, msg.msg_id)
        bctx.skipped += 1


async def process_all_batches(
    messages: list[InternalMessage],
    client: SourceClient,
    batch_size: int | None = None,
    *,
    origin: str = "unknown",
) -> bool:
    """对所有新消息执行完整管道：并行分类，按完成顺序入库并实时通知前端。

    返回 bool：True = 消息已处理或无需处理（调用方可推进会话水位）；
    False = 存在未落 raw 的保留消息（无启用类别/阶段插件缺失早退），
    调用方必须跳过本轮水位推进，否则下轮窗口会永久跳过这些消息。

        Args:
            messages: 待处理消息
            client: 消息源客户端，用于下载图片字节供 OCR
            batch_size: 切批大小（一次 AI 分类调用处理的消息数）。None 时回退
                config.realtime_batch_max_count，保持原有行为。
            origin: 处理路径标识（"realtime" / "backfill"），仅用于日志
    """
    if not messages:
        return True

    ctx = get_context()
    if ctx is None:
        raise RuntimeError(
            "管道阶段上下文未设置：main 启动时须调用 stages.set_context()"
        )

    # 源身份由管道统一盖章（单一权威点）：后续分类/去重/入库
    # 都以 msg.source 作源级命名空间
    source = client.name
    for m in messages:
        m.source = source

    # ── 入口统一过滤 + raw 落库（替代源内实现；源不触碰 DB）──
    # 顺序：自己发送（IGNORE_SELF）→ 纯占位符图片（OCR 未启用）→ 启用会话
    # → 已处理 → raw 批量落库，均无锁。空启用集 → 全滤（保持原监听器语义）；
    # INSERT OR IGNORE 幂等。
    self_filtered = 0
    if config.ignore_self:
        self_filtered = sum(1 for m in messages if m.is_self)
        if self_filtered:
            messages = [m for m in messages if not m.is_self]
            logger.info("[pipeline] %s 过滤自己发送: %d 条", origin, self_filtered)

    # OCR 未启用（enrich 槽位为空）时纯占位符图片消息无信息价值：不落 raw、
    # 不进分类、不标记 processed——OCR 重新启用后回填窗口内自动重拉重处理
    # （与 IGNORE_SELF 过滤同语义，可逆）。图片+文字混合消息（content 非
    # 占位符）不受影响：文字仍有信息价值，照常处理。
    enrich_stages = get_stages("enrich")
    images_filtered = 0
    if not enrich_stages:
        images_filtered = sum(
            1
            for m in messages
            if m.image_urls and _PLACEHOLDER_ONLY_RE.match(m.content)
        )
        if images_filtered:
            messages = [
                m
                for m in messages
                if not (m.image_urls and _PLACEHOLDER_ONLY_RE.match(m.content))
            ]
            logger.info(
                "[pipeline] %s 屏蔽纯占位符图片消息（OCR 未启用）: %d 条",
                origin,
                images_filtered,
            )

    # 过滤后计数：日志中的「处理 N 条」即真正进入分类的消息数（自消息已剔除）
    logger.info("[pipeline] %s 处理: %d 条 (源 %s)", origin, len(messages), source)
    enabled_rows = await get_enabled_sessions(source)
    enabled_ids = {r["session_id"] for r in enabled_rows}
    enabled_filtered = len(messages)
    messages = [m for m in messages if m.session_id in enabled_ids]
    enabled_filtered -= len(messages)
    processed_filtered = 0
    if messages:
        # 无启用类别 → 整批保留：不标记 processed（回填窗口内下轮自动重试），
        # 同时跳过已处理过滤与 raw 落库（将来成功分类时再落）。
        if not await get_enabled_categories():
            logger.warning("没有启用的类别，本轮消息全部跳过（保留待回填）")
            set_status(
                {
                    "lastWarning": "没有启用的类别，新消息将被保留待回填"
                    "（请在设置-信息分类中启用至少一个分类）"
                }
            )
            return False
        processed = await are_messages_processed(source, [m.msg_id for m in messages])
        if processed:
            processed_filtered = len(messages)
            messages = [m for m in messages if m.msg_id not in processed]
            processed_filtered -= len(messages)
        if messages:
            await bulk_insert_raw_messages(
                [
                    RawMsgInput(
                        source=source,
                        msg_id=m.msg_id,
                        session_id=m.session_id,
                        group_name=m.group_name,
                        sender_id=m.sender_id,
                        sender_name=m.sender_name,
                        content=m.content,
                        timestamp=m.timestamp,
                        article_url=m.article_url or "",
                    )
                    for m in messages
                ]
            )
            logger.debug("raw 落库: %d 条", len(messages))
    if not messages:
        logger.debug(
            "入口过滤后无消息: 自消息过滤 %d, 图片屏蔽 %d, 启用会话过滤 %d, 已处理过滤 %d",
            self_filtered,
            images_filtered,
            enabled_filtered,
            processed_filtered,
        )
        return True

    if batch_size is None:
        batch_size = config.realtime_batch_max_count
    batches = _split_batches(messages, batch_size)

    enrich_stages = get_stages("enrich")
    classify_stages = get_stages("classify")
    dedup_stages = get_stages("dedup")
    merge_stages = get_stages("post_insert")
    if not classify_stages or not dedup_stages:
        # 分类/去重阶段缺失（插件被禁用/未安装）时整批保留：不标记 processed，
        # 否则会被当作闲聊跳过而永久丢失；补齐插件后回填窗口内自动重试
        logger.warning(
            "阶段插件不完整（classify=%d, dedup=%d），本轮消息跳过（保留待回填）",
            len(classify_stages),
            len(dedup_stages),
        )
        set_status(
            {
                "lastWarning": "分类/去重阶段未启用，新消息将被保留待回填"
                "（请检查 PLUGINS 配置是否启用了 classify/dedup/ai_provider）"
            }
        )
        return False

    # 同步进度：入口计数进入处理的消息（含处理中的实时消息），每批完成后
    # 递减；事件携带快照驱动前端状态指示器的同步进度展示。早退路径
    # （无类别/阶段缺失）不计数——消息未在推进，避免"永不完成"。
    note_sync_batch_start(len(messages))
    await publish_sync_progress(get_sync_progress())

    async def _run_front(
        batch_index: int, batch: list[InternalMessage]
    ) -> tuple[int, BatchContext]:
        """锁外阶段：enrich（OCR 等）+ classify。"""
        bctx = BatchContext(messages=batch, client=client)
        for stage in enrich_stages:
            await stage.run(bctx, ctx)
        for stage in classify_stages:
            await stage.run(bctx, ctx)
        return batch_index, bctx

    logger.info(f"并行分类 {len(batches)} 批 (共 {len(messages)} 条)...")
    classify_start = time_module.time()
    classify_tasks = [
        asyncio.create_task(_run_front(i, batch)) for i, batch in enumerate(batches)
    ]
    total_inserted = 0
    total_dupes = 0
    total_skipped = 0
    total_merged = 0
    total_failed = 0
    completed_in_batch = 0

    try:
        for completed in asyncio.as_completed(classify_tasks):
            bi, bctx = await completed
            batch_start = time_module.perf_counter()
            outcome = bctx.outcomes
            failed_set = set(outcome.failed) if outcome else set()
            # 锁外预计算：dedup 阶段的行规划 + 批内预嵌入（每批最多一次嵌入调用，
            # 避免在 _storage_lock 内 await 远程嵌入导致管道整体阻塞）
            for stage in dedup_stages:
                before = getattr(stage, "before_run", None)
                if before is not None:
                    await before(bctx, ctx)
            # 存储锁内：判重/入库/缓存 → 跳过标记 → 同话题合并（原子段）
            async with _storage_lock:
                for stage in dedup_stages:
                    await stage.run(bctx, ctx)
                await _mark_skipped(bctx, failed_set)
                for stage in merge_stages:
                    await stage.run(bctx, ctx)
            # 锁外批量持久化本批新增向量（一次 DB 调用）
            for stage in dedup_stages:
                after = getattr(stage, "after_run", None)
                if after is not None:
                    await after(bctx, ctx)
            total_inserted += len(bctx.inserted)
            total_dupes += bctx.dupes
            total_skipped += bctx.skipped
            total_merged += bctx.merged
            total_failed += len(failed_set)
            completed_in_batch += len(bctx.messages)

            # 同步进度：本批完成处理（入库/判重/闲聊/失败均达终态），递减
            # pending；归零时快照带 done 标志，前端据此展示"已同步"后淡出
            note_sync_batch_done(len(bctx.messages))
            await publish_sync_progress(get_sync_progress())

            logger.debug(
                "批 #%d: %d 条 → 入库 %d, 重复 %d, 跳过 %d, 合并 %d, 失败 %d (%s)",
                bi,
                len(bctx.messages),
                len(bctx.inserted),
                bctx.dupes,
                bctx.skipped,
                bctx.merged,
                len(failed_set),
                fmt_dur(time_module.perf_counter() - batch_start),
            )

            # 合并会改动/删除已发布卡片，即使本批插入被吸收也要通知前端重拉
            if bctx.inserted or bctx.merged > 0:
                await publish_items_updated(
                    {
                        "batchIndex": bi,
                        "inserted": len(bctx.inserted),
                        "duplicates": bctx.dupes,
                        "skipped": bctx.skipped,
                        "merged": bctx.merged,
                        "failed": len(failed_set),
                    }
                )
    finally:
        for t in classify_tasks:
            if not t.done():
                t.cancel()
        # 异常/取消路径兜底：清掉尚未完成批次的 pending，避免进度指示器卡死
        # （正常完成时 completed_in_batch == len(messages），此分支不触发）
        remaining = len(messages) - completed_in_batch
        if remaining > 0:
            note_sync_batch_done(remaining)
            await publish_sync_progress(get_sync_progress())

    logger.info(f"分类完成 ({int((time_module.time() - classify_start) * 1000)}ms)")
    logger.info(
        f"总计: {total_inserted} 入库, {total_dupes} 重复, {total_skipped} 闲聊, "
        f"{total_merged} 合并, {total_failed} 失败待回填"
    )

    if total_inserted + total_dupes + total_skipped > 0:
        # 有实际处理产出（入库/去重/闲聊跳过均算消息已处理）才刷新
        # "上次同步"并清错误；仅当全部消息失败（三者全为 0）时视为零产出，
        # 保持旧状态，避免前端误报同步成功（旧实现中分类失败会抛错中止）。
        set_status(
            {
                "lastSync": datetime.now(UTC).isoformat(),
                "lastError": "",
                "lastWarning": "",  # 管道正常产出，清除阶段缺失等提示
            }
        )
    else:
        logger.warning(
            f"本轮零产出：{total_failed} 条失败待回填，不刷新 lastSync"
        )
    # 正常路径（消息已落 raw）：可推进水位；失败待回填的消息仍能被
    # "最早未处理消息钉窗"机制在后续轮次找回
    return True
