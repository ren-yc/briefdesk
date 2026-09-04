"""weflow 消息源插件 — 把 WeFlowSource 装配为 SourcePlugin（weflow-server :5033）。

必填配置（WEFLOW_API_TOKEN / WEFLOW_WXID / WEFLOW_DB_KEYS）缺失任一 →
setup 抛 PluginDisabledError 自禁用（与 qqflow 插件语义一致；无密钥无法
解密微信库，注册必然失败，自禁用比调用期报错更早暴露配置问题）。

生命周期：setup 构造 runtime 并经 ctx.register_source 注册（HTTP 启动前）；
activate 无副作用（监听启动由应用层在服务器就绪后统一编排）；
teardown 关闭 runtime（幂等）。
"""

from typing import Any

from briefdesk.plugin.base import PluginContext, PluginDisabledError, SourcePlugin
from briefdesk.plugin.config_helpers import validate_required_config
from briefdesk.settings_schema import build_settings_schema
from briefdesk.sources_base import SourceRuntime


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
                "api_base": "weflow API 地址",
                "api_token": "weflow 访问令牌",
                "wxid": "微信账号 wxid",
                "db_path": "wechat 数据目录",
                "img_aes_key": "图片 AES 解密密钥",
                "img_xor_key": "图片 XOR 解密密钥",
                "db_keys": "库密钥映射（JSON 前半）",
                "db_keys_2": "库密钥映射（JSON 后半）",
                "sse_reconnect_initial_ms": "SSE 初始重连间隔（毫秒）",
                "sse_reconnect_max_ms": "SSE 最大重连间隔（毫秒）",
                "sse_read_timeout_ms": "SSE 读取超时（毫秒）",
            },
            hints={
                "api_token": "密钥只保存到系统钥匙串，不会写入暂存文件",
                "img_aes_key": "密钥只保存到系统钥匙串，不会写入暂存文件",
                "img_xor_key": "密钥只保存到系统钥匙串，不会写入暂存文件",
                "db_keys": "密钥只保存到系统钥匙串，不会写入暂存文件；两段各存约一半库映射",
                "db_keys_2": "密钥只保存到系统钥匙串，不会写入暂存文件；两段各存约一半库映射",
            },
        )

    async def setup(self, ctx: PluginContext) -> None:
        # 延迟导入 + 模块属性访问：仅加载本插件依赖，且便于测试替换
        from briefdesk.plugins.weflow import config as wf_config
        from briefdesk.plugins.weflow import runtime as wf_runtime

        settings = wf_config.WeFlowSettings()
        validate_required_config(settings, {
            'api_token': 'WEFLOW_API_TOKEN',
            'wxid': 'WEFLOW_WXID',
        })
        if not settings.db_keys_map:
            raise PluginDisabledError(
                "缺少必填配置 WEFLOW_DB_KEYS(+WEFLOW_DB_KEYS_2)"
                "（在 .env / 系统密钥环中配置后重启生效）"
            )
        runtime = wf_runtime.WeFlowSource()
        ctx.register_source(runtime)
        self._runtime = runtime

    async def activate(self, ctx: PluginContext) -> None: ...

    async def teardown(self) -> None:
        if self._runtime is not None:
            await self._runtime.close()


plugin = WeFlowPlugin()
