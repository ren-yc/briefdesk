"""PLUGIN_PATH 发现的测试夹具：暴露 plugin 实例。"""

from briefdesk.plugin.base import PluginContext


class Hello:
    name = "hello"
    version = "0.1"
    dependencies = ()

    async def setup(self, ctx: PluginContext) -> None: ...

    async def activate(self, ctx: PluginContext) -> None: ...

    async def teardown(self) -> None: ...


plugin = Hello()
