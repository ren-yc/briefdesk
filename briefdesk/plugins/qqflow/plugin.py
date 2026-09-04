"""qqflow 消息源插件 — 把 QqFlowSource 装配为 SourcePlugin。

必填配置（QQFLOW_API_TOKEN / QQFLOW_QQ / QQFLOW_KEY）缺失任一 →
setup 抛 PluginDisabledError 自禁用（与旧工厂 builder 返回 None 语义一致）。

生命周期：setup 构造 runtime 并经 ctx.register_source 注册（HTTP 启动前）；
activate 无副作用（监听启动由应用层在服务器就绪后统一编排）；
teardown 关闭 runtime（幂等）。
"""

from typing import Any

from briefdesk.plugin.base import PluginContext, PluginDisabledError, SourcePlugin
from briefdesk.plugin.config_helpers import validate_required_config
from briefdesk.settings_schema import build_settings_schema
from briefdesk.sources_base import SourceRuntime


class QqFlowPlugin(SourcePlugin):
    """qqflow 源插件（显式实现 SourcePlugin；入口见模块底部 `plugin` 实例）。"""

    name = "qqflow"
    version = "1.0.1"
    dependencies: tuple[str, ...] = ()

    def __init__(self) -> None:
        self._runtime: SourceRuntime | None = None

    def settings_schema(self) -> list[dict[str, Any]]:
        from briefdesk.plugins.qqflow.config import QqFlowSettings

        return build_settings_schema(
            QqFlowSettings,
            plugin=self.name,
            labels={
                "api_base": "qqflow API 地址",
                "api_token": "qqflow 访问令牌",
                "qq": "QQ 账号",
                "key": "qqflow 引导密钥",
                "db_path": "qqflow 数据库路径",
                "sse_reconnect_initial_ms": "SSE 初始重连间隔（毫秒）",
                "sse_reconnect_max_ms": "SSE 最大重连间隔（毫秒）",
                "sse_read_timeout_ms": "SSE 读取超时（毫秒）",
            },
            hints={
                "api_token": "密钥只保存到系统钥匙串，不会写入暂存文件",
                "key": "密钥只保存到系统钥匙串，不会写入暂存文件",
            },
        )

    async def setup(self, ctx: PluginContext) -> None:
        # 延迟导入 + 模块属性访问：仅加载本插件依赖，且便于测试替换
        from briefdesk.plugins.qqflow import config as qqflow_config
        from briefdesk.plugins.qqflow import runtime as qqflow_runtime

        settings = qqflow_config.QqFlowSettings()
        # QQFLOW_DB_PATH 允许为空：上游 qqflow-server 在 db_path 为空时
        # 自动回退到平台默认位置（Windows: Documents\Tencent Files 等）
        validate_required_config(settings, {
            'api_token': 'QQFLOW_API_TOKEN',
            'qq': 'QQFLOW_QQ',
            'key': 'QQFLOW_KEY',
        })
        runtime = qqflow_runtime.QqFlowSource()
        ctx.register_source(runtime)
        self._runtime = runtime

    async def activate(self, ctx: PluginContext) -> None: ...

    async def teardown(self) -> None:
        if self._runtime is not None:
            await self._runtime.close()


plugin = QqFlowPlugin()
