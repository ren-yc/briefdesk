"""轮询周期编排 — 拉取消息 → 流水线处理 → 更新轮询时间。

与 main.py 分离：main 只负责运行时生命周期（启动/优雅关闭），
本模块负责一次轮询周期的业务编排。
只依赖 SourceRuntime 协议，不绑定具体消息源。
"""

import asyncio
import logging
import time as time_module
from datetime import UTC, datetime

from briefdesk.config import config
from briefdesk.db import (
    are_messages_processed,
    get_enabled_sessions,
    get_oldest_unprocessed_by_session,
    get_session_last_polls,
    update_session_last_polls,
    upsert_contact,
    upsert_session,
)
from briefdesk.logger import fmt_dur
from briefdesk.pipeline import process_all_batches
from briefdesk.sources_base import SourceRuntime
from briefdesk.status import set_status
from briefdesk.types import SessionInfo

logger = logging.getLogger(__name__)

# ── Poll Cycle ──

_polling = False
_poll_lock = asyncio.Lock()


async def run_poll_cycle(source: SourceRuntime) -> None:
    """执行一轮完整轮询：历史拉取 → 批量流水线处理 → 记录轮询时间。

    供手动 /api/sync 与首轮回填共用，轮询互斥由 _poll_lock 保证。
    """
    global _polling
    async with _poll_lock:
        if _polling:
            logger.info("轮询跳过（%s）: 上一轮仍在进行", source.name)
            return
        _polling = True

    # 水位基准：本轮 cycle 的开始时刻（不是结束时刻）。处理期间到达的新消息
    # createTime 必然晚于该值，下轮窗口 [水位 - overlap, now] 必能覆盖；
    # 若提交结束时刻，处理耗时超过 overlap 时会漏掉处理期间到达的消息。
    cycle_start = datetime.now(UTC)
    perf_start = time_module.perf_counter()
    logger.info("[%s] 轮询周期开始", source.name)
    try:
        enabled_rows = await get_enabled_sessions(source.name)
        enabled = [
            SessionInfo(
                source=r["source"],
                session_id=r["session_id"],
                name=r["name"],
                is_group=bool(r["is_group"]),
                is_official=bool(r.get("is_official", 0)),
            )
            for r in enabled_rows
        ]
        windows = await _compute_session_windows(source.name, enabled)
        result = await source.fetch_history(
            enabled,
            lambda ids: are_messages_processed(source.name, ids),
            window_start_by_session=windows,
        )
        # 源只产出数据，写库统一在此完成（顺序与重构前一致：contacts 先、sessions 后）
        for c in result.contacts:
            await upsert_contact(c.source, c.sender_id, c.display_name)
        for s in result.sessions:
            await upsert_session(
                s.source,
                s.session_id,
                s.name,
                s.is_group,
                s.is_official,
                last_active_at=s.last_active_at or None,
            )
        ok = await process_all_batches(
            result.messages,
            source.client,
            batch_size=config.backfill_batch_max_count,
            origin="backfill",
        )
        # 只有处理完成才推进水位：管道早退或源侧拉取失败（result.failed_sessions，
        # 契约详见 types.PollResult）的会话消息未落 raw_messages，推进即永久漏拉
        if ok:
            advanced = [
                s for s in enabled if s.session_id not in result.failed_sessions
            ]
            if advanced:
                await update_session_last_polls(
                    source.name,
                    [
                        (s.session_id, int(cycle_start.timestamp()))
                        for s in advanced
                    ],
                )
            elif enabled:
                logger.info(
                    "[%s] 本轮 %d 个启用会话均未成功拉取，水位不推进",
                    source.name,
                    len(enabled),
                )
        logger.info(
            "[%s] 轮询周期完成: %d 新消息, %d 会话, %d 联系人 (%s)",
            source.name,
            len(result.messages),
            len(result.sessions),
            len(result.contacts),
            fmt_dur(time_module.perf_counter() - perf_start),
        )
    except Exception as e:  # noqa: BLE001
        logger.error(
            "[%s] 轮询周期失败: %s (%s)",
            source.name,
            e,
            fmt_dur(time_module.perf_counter() - perf_start),
        )
        set_status({"lastError": str(e)})
    finally:
        _polling = False


async def _compute_session_windows(
    source: str, enabled: list[SessionInfo]
) -> dict[str, int | None] | None:
    """按会话计算本轮增量窗口下界（session_id → 秒级时间戳，含边界）。

    每个启用会话独立计算：
    - 无水位（sessions.last_poll_ts 为 NULL，即新启用/重新启用）→ 值 None，
      由源按 BACKFILL_HOURS 回退（启用即回填一次；其历史未处理消息在下轮
      由水位钉窗机制重试）；
    - 有水位 → min(水位, 该会话最早未处理消息时间) - POLL_OVERLAP_SECONDS。
    仅"有未处理消息"的会话被其最久远未处理消息钉住窗口，其余会话水位
    不受影响；停用会话不参与（其未处理消息在重新启用回填时自然重试）。
    BACKFILL_HOURS=-1（全量）返回 None，由源自行处理全量拉取。
    """
    if config.backfill_hours == -1:
        return None
    if not enabled:
        return {}
    watermarks = await get_session_last_polls(
        source, [s.session_id for s in enabled]
    )
    oldest_pending = await get_oldest_unprocessed_by_session(source)
    windows: dict[str, int | None] = {}
    for s in enabled:
        w = watermarks.get(s.session_id)
        if w is None:
            windows[s.session_id] = None  # 无水位：源按 BACKFILL_HOURS 回填
            continue
        base = w
        pending = oldest_pending.get(s.session_id)
        if pending is not None and pending < base:
            base = pending
        windows[s.session_id] = base - config.poll_overlap_seconds
    logger.debug(
        "[%s] 按会话窗口: %s (overlap %ds)",
        source,
        {
            sid: (
                "回填"
                if w is None
                else datetime.fromtimestamp(w, tz=UTC).isoformat()
            )
            for sid, w in windows.items()
        },
        config.poll_overlap_seconds,
    )
    return windows
