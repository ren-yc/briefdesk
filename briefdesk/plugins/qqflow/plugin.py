"""qqflow 消息源插件 — 把 QqFlowSource 装配为 SourcePlugin。

必填配置（QQFLOW_API_TOKEN / QQFLOW_QQ / QQFLOW_KEY）缺失任一 →
setup 抛 PluginDisabledError 自禁用（与旧工厂 builder 返回 None 语义一致）。

生命周期：setup 构造 runtime 并经 ctx.register_source 注册（HTTP 启动前）；
activate 无副作用（监听启动由应用层在服务器就绪后统一编排）；
teardown 关闭 runtime（幂等）。
"""

from briefdesk.plugin.base import PluginContext, PluginDisabledError, SourcePlugin
from briefdesk.sources_base import SourceRuntime


class QqFlowPlugin(SourcePlugin):
    """qqflow 源插件（显式实现 SourcePlugin；入口见模块底部 `plugin` 实例）。"""

    name = "qqflow"
    version = "1.0.1"
    dependencies: tuple[str, ...] = ()

    def __init__(self) -> None:
        self._runtime: SourceRuntime | None = None

    async def setup(self, ctx: PluginContext) -> None:
        # 延迟导入 + 模块属性访问：仅加载本插件依赖，且便于测试替换
        from briefdesk.plugins.qqflow import config as qqflow_config
        from briefdesk.plugins.qqflow import runtime as qqflow_runtime

        settings = qqflow_config.QqFlowSettings()
        # QQFLOW_DB_PATH 允许为空：上游 qqflow-server 在 db_path 为空时
        # 自动回退到平台默认位置（Windows: Documents\Tencent Files 等）
        required = (
            ("QQFLOW_API_TOKEN", settings.api_token.get_secret_value()),
            ("QQFLOW_QQ", settings.qq),
            ("QQFLOW_KEY", settings.key.get_secret_value()),
        )
        missing = [name for name, value in required if not value]
        if missing:
            raise PluginDisabledError(
                f"缺少必填配置 {', '.join(missing)}（在 .env 中配置后重启生效）"
            )
        runtime = qqflow_runtime.QqFlowSource()
        ctx.register_source(runtime)
        self._runtime = runtime

    async def activate(self, ctx: PluginContext) -> None: ...

    async def teardown(self) -> None:
        if self._runtime is not None:
            await self._runtime.close()


plugin = QqFlowPlugin()
