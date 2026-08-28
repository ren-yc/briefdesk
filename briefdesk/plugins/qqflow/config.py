"""qqflow 消息源专属配置 — 从 .env 读取 QQFLOW_* 环境变量。

与 app 级配置(briefdesk/config.py)分离:只有启用 qqflow 源时才被加载。
QQFLOW_API_TOKEN / QQFLOW_QQ / QQFLOW_KEY 为必填项，缺失时由
QqFlowPlugin.setup 抛 PluginDisabledError 自禁用（见 briefdesk/plugins/qqflow/plugin.py）。
"""

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource

from briefdesk.secrets_store import KeyringSource
from briefdesk.settings_env import PROJECT_ROOT, get_settings_file

# 密钥解析链（keyring > 环境变量 > .env > 默认值），见 briefdesk/secrets_store.py
_KEYRING_FIELDS = {
    "api_token": "QQFLOW_API_TOKEN",
    "key": "QQFLOW_KEY",
}


class QqFlowSettings(BaseSettings):
    api_base: str = "http://127.0.0.1:5032"  # env: QQFLOW_API_BASE
    api_token: SecretStr = SecretStr("")  # env: QQFLOW_API_TOKEN（必填，缺失→禁用源）
    qq: str = ""  # env: QQFLOW_QQ（必填，引导注册 QQ 号，缺失→禁用源）
    key: SecretStr = SecretStr("")  # env: QQFLOW_KEY（必填，引导注册密钥，缺失→禁用源）
    # env: QQFLOW_DB_PATH（可选，允许为空：上游服务端在 db_path 为空时
    # 自动回退平台默认位置，如 Windows: Documents\Tencent Files）
    db_path: str = ""
    sse_reconnect_initial_ms: int = Field(
        default=1000,
        ge=0,  # env: QQFLOW_SSE_RECONNECT_INITIAL_MS
    )
    sse_reconnect_max_ms: int = Field(
        default=60000,
        gt=0,  # env: QQFLOW_SSE_RECONNECT_MAX_MS
    )
    # SSE 读超时（毫秒）：上游每 25 秒发送 KeepAlive ping，60s（≈2.4 个心跳
    # 周期）内未收到任何数据即判定连接失效、断开重连——防网络半开导致
    # 实时监听永久静默死亡（审查报告【2·P1】），兼作半开连接的自愈检测时限
    sse_read_timeout_ms: int = Field(
        default=60000,
        gt=0,  # env: QQFLOW_SSE_READ_TIMEOUT_MS
    )

    model_config = {
        "env_prefix": "QQFLOW_",  # api_base → QQFLOW_API_BASE
        "env_file": [PROJECT_ROOT / ".env", get_settings_file()],
        "env_file_encoding": "utf-8",
        # 同一 .env 里还有 app 级与其它源的字段，忽略未知项
        "extra": "ignore",
    }

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """来源优先级：init 参数 > 系统密钥环 > 环境变量 > .env > 默认值。"""
        return (
            init_settings,
            KeyringSource(settings_cls, _KEYRING_FIELDS),
            env_settings,
            dotenv_settings,
            file_secret_settings,
        )
