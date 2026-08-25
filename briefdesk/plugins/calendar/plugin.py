"""日历 Web 插件 — 提供 /api/calendar 视图路由与前端资源。

前端资源（ui/ui.js，经 /plugin-assets/calendar/ui.js 提供）由核心前端
加载器动态注入：日历入口（#calendar-btn）的可见性由插件自己控制。
"""

from pathlib import Path

from fastapi import APIRouter

from briefdesk.plugin.base import PluginContext, WebPlugin


class CalendarPlugin(WebPlugin):
    """日历视图插件（显式实现 WebPlugin；入口见模块底部 `plugin` 实例）。"""

    name = "calendar"
    version = "1.0.0"
    dependencies: tuple[str, ...] = ()

    def router(self) -> APIRouter:
        from briefdesk.plugins.calendar import router as calendar_router

        return calendar_router.router

    def asset_dir(self) -> Path | None:
        # 插件前端资源目录：核心挂载到 /plugin-assets/calendar/（浏览器直连）
        return Path(__file__).parent / "ui"

    async def setup(self, ctx: PluginContext) -> None:
        ctx.register_router(self.router())
        asset_dir = self.asset_dir()
        if asset_dir is not None:
            ctx.register_plugin_assets(self.name, str(asset_dir))

    async def activate(self, ctx: PluginContext) -> None: ...

    async def teardown(self) -> None: ...


plugin = CalendarPlugin()
