"""共享回调注册（server 子包）：会话刷新回调由 main 注入、路由读取。"""

from collections.abc import Awaitable, Callable

_refresh_sessions_callback: Callable[[], Awaitable[None]] | None = None


def set_refresh_sessions_callback(cb: Callable[[], Awaitable[None]]) -> None:
    """注入会话刷新回调（main 装配）。"""
    global _refresh_sessions_callback
    _refresh_sessions_callback = cb
