"""weflow-legacy 消息源插件 — 把 WeFlowLegacySource 装配为 SourcePlugin。

生命周期：setup 构造 runtime 并经 ctx.register_source 注册（HTTP 启动前）；
activate 无副作用（监听启动由应用层在服务器就绪后统一编排）；
teardown 关闭 runtime（幂等）。

本插件为旧版 WeFlow 消息源，改名腾出 `weflow` 标识给未来新源。必填
配置校验与 weflow/qqflow 统一（决策 ①=1B：零源降级启动后自禁用不再有
「唯一源中止启动」的顾虑，.env.example 亦标注 WEFLOW_LEGACY_API_TOKEN
必填）：缺失在装配期抛 PluginDisabledError 明示，配置后重启生效。
"""

import logging
from typing import Any

from briefdesk.plugin.base import PluginContext, SourcePlugin
from briefdesk.plugin.config_helpers import validate_required_config
from briefdesk.settings_schema import build_settings_schema
from briefdesk.sources_base import SourceRuntime

logger = logging.getLogger(__name__)


class WeFlowLegacyPlugin(SourcePlugin):
    """weflow-legacy 源插件（显式实现 SourcePlugin；入口见模块底部 `plugin` 实例）。"""

    name = "weflow-legacy"
    version = "1.0.0"
    dependencies: tuple[str, ...] = ()

    def __init__(self) -> None:
        self._runtime: SourceRuntime | None = None

    def settings_schema(self) -> list[dict[str, Any]]:
        from briefdesk.plugins.weflow_legacy.config import WeFlowLegacySettings

        return build_settings_schema(
            WeFlowLegacySettings,
            plugin=self.name,
            labels={
                "api_base": "WeFlow Legacy API 地址",
                "api_token": "WeFlow Legacy 访问令牌",
                "sse_reconnect_initial_ms": "SSE 初始重连间隔（毫秒）",
                "sse_reconnect_max_ms": "SSE 最大重连间隔（毫秒）",
                "sse_read_timeout_ms": "SSE 读取超时（毫秒）",
            },
            hints={
                "api_token": "密钥只保存到系统钥匙串，不会写入暂存文件",
            },
        )

    async def setup(self, ctx: PluginContext) -> None:
        # 延迟导入 + 模块属性访问：仅加载本插件依赖，且便于测试替换
        from briefdesk.plugins.weflow_legacy import config as wfl_config
        from briefdesk.plugins.weflow_legacy import runtime as wfl_runtime

        settings = wfl_config.WeFlowLegacySettings()
        # 必填校验与 weflow/qqflow 统一（决策 ①=1B：零源降级启动后，自禁用
        # 不再引发「唯一源中止启动」；.env.example 标注本项必填）
        validate_required_config(settings, {
            'api_token': 'WEFLOW_LEGACY_API_TOKEN',
        })
        runtime = wfl_runtime.WeFlowLegacySource()
        ctx.register_source(runtime)
        self._runtime = runtime

    async def activate(self, ctx: PluginContext) -> None: ...

    async def teardown(self) -> None:
        if self._runtime is not None:
            await self._runtime.close()


plugin = WeFlowLegacyPlugin()
