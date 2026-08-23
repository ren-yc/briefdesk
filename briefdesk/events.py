"""内部事件总线 — 核心组件与插件间的进程内 topic 解耦通道。

与 realtime.py 的关系：realtime 是「前端实时推送」专用总线（/api/stream
SSE，每订阅者一条队列）；本总线是通用事件通道（如 items_deleted →
去重插件清缓存），核心只发布 topic，订阅者经 PluginContext 接入。
事件语义是「通知」而非「请求」：处理器异常只记日志，不向发布方传播。
"""

import inspect
import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

# ── 核心 → 插件通用事件主题 ──
# 核心只发布 topic，订阅者（插件）在 setup 阶段经 PluginContext 接入。
EVENT_ITEMS_DELETED = "items_deleted"  # payload: list[str] item_ids（核心删除卡片后发布，去重缓存等订阅清理）

# 处理器可为同步或异步函数；同步函数返回 awaitable 时同样会被 await
EventHandler = Callable[[Any], Any]


class EventBus:
    """进程内 topic 事件总线：同步处理器直接调用，异步处理器顺序 await。"""

    def __init__(self) -> None:
        self._handlers: dict[str, list[EventHandler]] = {}

    def subscribe(self, event: str, handler: EventHandler) -> None:
        """注册事件处理器；同一 handler 重复注册不叠加。"""
        handlers = self._handlers.setdefault(event, [])
        if handler not in handlers:
            handlers.append(handler)

    def unsubscribe(self, event: str, handler: EventHandler) -> None:
        """注销事件处理器；处理器不存在时为 no-op。"""
        handlers = self._handlers.get(event)
        if handlers and handler in handlers:
            handlers.remove(handler)
            if not handlers:
                self._handlers.pop(event, None)

    async def publish(self, event: str, payload: Any = None) -> None:
        """发布事件：顺序执行当前订阅者快照，单个处理器异常不中断其余。"""
        for handler in list(self._handlers.get(event, ())):
            try:
                if inspect.iscoroutinefunction(handler):
                    await handler(payload)
                    continue
                result = handler(payload)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.exception("事件处理器异常（event=%s）", event)


# 模块级单例（与 realtime/db 同风格）：main 构造 PluginContext
# 时注入；测试可自建 EventBus 隔离验证。
event_bus = EventBus()
