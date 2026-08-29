"""进程内实时事件分发（后端任务 -> 前端 SSE）。

订阅队列项为 (事件名, data JSON 字符串) 二元组：api_stream 依事件名生成
`event: <name>` 的 SSE 帧，从而支持 items_updated（列表刷新）、
sync_progress（同步进度事件）与 announcements_updated（公告增删）三类事件。
"""

import asyncio
import json
import logging

logger = logging.getLogger(__name__)

_subscribers: set[asyncio.Queue[tuple[str, str]]] = set()
_subscribers_lock = asyncio.Lock()

# 队列满导致的累计丢弃事件数（诊断只读：客户端漏事件时先查这里）
_dropped_count = 0

# 丢弃告警的降噪闸门：首次丢弃打 WARNING（默认 LOG_LEVEL=INFO 下可见），
# 之后同一进程内只打 DEBUG。丢弃是真实的前端状态不同步，必须至少可见一次；
# 但慢客户端会连续触发，每条都 WARNING 会刷屏——首条足以引向
# get_dropped_count() 这个累计口径。
_drop_warned = False

# 服务关闭事件：置位后所有 /api/stream 流主动结束。
# 若流不结束，uvicorn 优雅退出会无限等待这些常驻 ASGI 任务
# （timeout_graceful_shutdown 默认 None）。
_shutdown_event: asyncio.Event | None = None


def get_shutdown_event() -> asyncio.Event:
    """返回全局关闭事件（懒初始化，仅在有 /api/stream 连接时创建）。"""
    global _shutdown_event
    if _shutdown_event is None:
        _shutdown_event = asyncio.Event()
    return _shutdown_event


def signal_shutdown() -> None:
    """置位关闭事件，通知所有 /api/stream 流结束。"""
    if _shutdown_event is not None:
        _shutdown_event.set()


def get_dropped_count() -> int:
    """返回累计丢弃的实时事件数（订阅队列满时丢弃，只读诊断口径）。"""
    return _dropped_count


async def subscribe() -> asyncio.Queue[tuple[str, str]]:
    q: asyncio.Queue[tuple[str, str]] = asyncio.Queue(maxsize=32)
    async with _subscribers_lock:
        _subscribers.add(q)
    return q


async def unsubscribe(q: asyncio.Queue[tuple[str, str]]) -> None:
    async with _subscribers_lock:
        _subscribers.discard(q)


async def _publish(name: str, payload: dict | None = None) -> None:
    global _dropped_count, _drop_warned
    data = json.dumps(payload or {}, ensure_ascii=False)
    async with _subscribers_lock:
        subscribers = list(_subscribers)
    for q in subscribers:
        if q.full():
            # 慢客户端积压超限：丢弃并计数（前端靠轮询兜底收敛），
            # 不静默——否则客户端状态不同步无从排查。DEBUG 在默认
            # LOG_LEVEL=INFO 下恰恰是静默的，故首条走 WARNING（见 _drop_warned）。
            _dropped_count += 1
            if not _drop_warned:
                _drop_warned = True
                logger.warning(
                    "SSE 订阅队列已满，开始丢弃事件（前端可能状态不同步，"
                    "靠轮询兜底收敛；累计数见 get_dropped_count()）"
                )
            else:
                logger.debug(
                    "SSE 订阅队列已满，丢弃事件（累计丢弃 %d）", _dropped_count
                )
            continue
        q.put_nowait((name, data))


async def publish_items_updated(payload: dict | None = None) -> None:
    """新入库卡片/同步完成等列表变化事件（原名与签名保持，兼容现有调用）。"""
    await _publish("items_updated", payload)


async def publish_sync_progress(payload: dict | None = None) -> None:
    """同步进度事件：携带 SyncProgress 快照，驱动前端状态胶囊的进度展示。"""
    await _publish("sync_progress", payload)


async def publish_announcements_updated(payload: dict | None = None) -> None:
    """公告增删事件：携带公告快照，驱动前端公告条即时刷新。"""
    await _publish("announcements_updated", payload)
