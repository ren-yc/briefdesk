"""PluginManager 测试的 entry point 加载目标（_ 前缀：pytest 不收集）。"""

from briefdesk.plugin.base import PluginContext


class _EntryPointPlugin:
    name = "ep_a"
    version = "fixture"
    dependencies = ()

    async def setup(self, ctx: PluginContext) -> None: ...

    async def activate(self, ctx: PluginContext) -> None: ...

    async def teardown(self) -> None: ...


plugin_a = _EntryPointPlugin()
