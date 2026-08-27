"""weflow 消息源插件 — 把 WeFlowSource 装配为 SourcePlugin。

生命周期：setup 构造 runtime 并经 ctx.register_source 注册（HTTP 启动前）；
activate 无副作用（监听启动由应用层在服务器就绪后统一编排）；
teardown 关闭 runtime（幂等）。

无必填配置校验（缺 WEFLOW_API_TOKEN 时插件仍注册，与旧工厂语义一致）：
缺失会在 setup 阶段打醒目 WARNING，首次同步时由上游调用报错定位——
不采用自禁用（weflow 是默认源，自禁用会触发"零源报错"中止启动）。
"""

import logging
from typing import Any

from briefdesk.plugin.base import PluginContext, SourcePlugin
from briefdesk.settings_schema import build_settings_schema
from briefdesk.sources_base import SourceRuntime

logger = logging.getLogger(__name__)


class WeFlowPlugin(SourcePlugin):
    """weflow 源插件（显式实现 SourcePlugin；入口见模块底部 `plugin` 实例）。"""

    name = "weflow"
    version = "1.0.0"
    dependencies: tuple[str, ...] = ()

    def __init__(self) -> None:
        self._runtime: SourceRuntime | None = None

    def settings_schema(self) -> list[dict[str, Any]]:
        from briefdesk.plugins.weflow.config import WeFlowSettings

        return build_settings_schema(
            WeFlowSettings,
            plugin=self.name,
            labels={
                "api_base": "WeFlow API 地址",
                "api_token": "WeFlow 访问令牌",
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
        from briefdesk.plugins.weflow import config as weflow_config
        from briefdesk.plugins.weflow import runtime as weflow_runtime

        settings = weflow_config.WeFlowSettings()
        if not settings.api_token.get_secret_value():
            logger.warning(
                "[weflow] WEFLOW_API_TOKEN 未配置：实时监听与历史回填将在调用期失败。"
                "请在 .env 中填入 WeFlow HTTP API 访问令牌后重启。"
            )
        runtime = weflow_runtime.WeFlowSource()
        ctx.register_source(runtime)
        self._runtime = runtime

    async def activate(self, ctx: PluginContext) -> None: ...

    async def teardown(self) -> None:
        if self._runtime is not None:
            await self._runtime.close()


plugin = WeFlowPlugin()
