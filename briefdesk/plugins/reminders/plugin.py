"""提醒 Web 插件（P5 起）— 提供卡片提醒设置与到期提醒路由。

前端资源（ui/ui.js + ui/ui.css，经 /plugin-assets/reminders/ 提供）由核心
加载器注入：卡片「提醒」按钮/菜单、设置面板自动提醒控件与到期轮询全部
随插件分发，核心只提供行内扩展钩子（registerItemRowExtension）。
"""

from pathlib import Path

from fastapi import APIRouter

from briefdesk.plugin.base import PluginContext, WebPlugin


class RemindersPlugin(WebPlugin):
    """提醒插件（显式实现 WebPlugin；入口见模块底部 `plugin` 实例）。"""

    name = "reminders"
    version = "1.0.0"
    dependencies: tuple[str, ...] = ()

    def router(self) -> APIRouter:
        from briefdesk.plugins.reminders import router as reminders_router

        return reminders_router.router

    def asset_dir(self) -> Path | None:
        # 插件前端资源目录：核心挂载到 /plugin-assets/reminders/（浏览器直连）
        return Path(__file__).parent / "ui"

    async def setup(self, ctx: PluginContext) -> None:
        ctx.register_router(self.router())
        asset_dir = self.asset_dir()
        if asset_dir is not None:
            ctx.register_plugin_assets(self.name, str(asset_dir))

    async def activate(self, ctx: PluginContext) -> None: ...

    async def teardown(self) -> None: ...


plugin = RemindersPlugin()
