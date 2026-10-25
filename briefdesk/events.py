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
# purge_expired_ignored 直调 db 不经路由，历史上不发删除事件——忽略态过期
# 清理后，去重负缓存若仍记着这些 item 的 DIFFERENT 对子，属过期失效盲区
# （负向结论只影响判官调用次数不影响正确性，但失效纪律按「任一侧状态变更
# 即失效」执行）。新事件与 EVENT_ITEMS_DELETED 同载荷语义，订阅方同口径处理。
EVENT_ITEMS_PURGED = "items_purged"  # payload: list[str] item_ids（忽略态过期清理后发布）
# 类别结构变更信号（categories 增删/改名/停用时由类别路由发布，payload 恒
# None）：classify 分配器的原型缓存以类名/类集为键，订阅方据此触发重建。
# 发布与订阅两侧共用本常量——同名字符串各自定义会让改名静默断线。
EVENT_CATEGORIES_CHANGED = "categories_changed"  # payload: None（仅作信号）

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
