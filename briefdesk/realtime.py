"""进程内实时事件分发（后端任务 -> 前端 SSE）。"""

import asyncio
import json

_subscribers: set[asyncio.Queue[str]] = set()
_subscribers_lock = asyncio.Lock()

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


async def subscribe() -> asyncio.Queue[str]:
    q: asyncio.Queue[str] = asyncio.Queue(maxsize=32)
    async with _subscribers_lock:
        _subscribers.add(q)
    return q


async def unsubscribe(q: asyncio.Queue[str]) -> None:
    async with _subscribers_lock:
        _subscribers.discard(q)


async def publish_items_updated(payload: dict | None = None) -> None:
    data = json.dumps(payload or {}, ensure_ascii=False)
    async with _subscribers_lock:
        subscribers = list(_subscribers)
    for q in subscribers:
        if q.full():
            continue
        q.put_nowait(data)
