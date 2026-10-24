"""同步服务 — 触发一次全源轮询同步（fire-and-forget）。

main 启动与 /api/sync 路由共用本服务：server 只保留路由调用，
应用编排收在本模块。
"""

import asyncio
import logging
import time as time_module
from collections.abc import Awaitable, Callable

from briefdesk.logger import fmt_dur
from briefdesk.realtime import publish_items_updated
from briefdesk.status import is_syncing, set_status

logger = logging.getLogger(__name__)

_sync_callback: Callable[[], Awaitable[None]] | None = None


def set_sync_callback(cb: Callable[[], Awaitable[None]]) -> None:
    global _sync_callback
    _sync_callback = cb


def trigger_sync(reason: str = "manual") -> asyncio.Task[None] | None:
    """触发一次同步（fire-and-forget），main 启动与 /api/sync 共用。

    已在同步中或未注册回调时返回 None，不触发。

        Args:
            reason: 触发来源标识（"startup" 启动同步 / "periodic" 周期同步 /
                "api" 经 /api/sync 手动触发），仅用于日志。默认值 "manual"
                是省略参数时的兜底，当前没有调用点显式传它——新增来源时按
                上面三种的命名风格补一个词，不要复用 "api" 掩盖真实来源
                （日志里分不清谁触发的同步）。

        Returns:
            asyncio.Task: 同步任务，调用方（main）可持有并在关闭时取消
    """
    cb = _sync_callback
    if cb is None or is_syncing():
        logger.info(
            "同步未触发（来源 %s）: %s",
            reason,
            "同步已在进行" if is_syncing() else "未注册同步回调",
        )
        return None
    set_status({"syncing": True})
    logger.info("同步触发（来源 %s）", reason)

    async def _run() -> None:
        start = time_module.perf_counter()
        try:
            await cb()
        except Exception:  # 同步任务自身异常不应静默
            logger.exception("同步任务失败")
        finally:
            set_status({"syncing": False})
            logger.info("同步完成 (%s)", fmt_dur(time_module.perf_counter() - start))
            # 同步结束（无论有无新消息）都推送事件，前端据此刷新状态，
            # 否则"同步中..."要等下一次轮询（最长 refreshIntervalSec）才恢复
            await publish_items_updated({"synced": True})

    return asyncio.create_task(_run())
