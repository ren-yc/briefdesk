"""WeFlow Legacy 消息源专属配置 — 从 .env 读取 WEFLOW_LEGACY_* 环境变量。

与 app 级配置(briefdesk/config.py)分离:只有启用 weflow-legacy 源时才被加载。
环境变量名为 WEFLOW_LEGACY_API_BASE / WEFLOW_LEGACY_API_TOKEN。
"""

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource

from briefdesk.secrets_store import KeyringSource
from briefdesk.settings_env import PROJECT_ROOT, get_settings_file

# 密钥解析链（keyring > 环境变量 > .env > 默认值），见 briefdesk/secrets_store.py
_KEYRING_FIELDS = {"api_token": "WEFLOW_LEGACY_API_TOKEN"}


class WeFlowLegacySettings(BaseSettings):
    api_base: str = "http://127.0.0.1:5031"
    api_token: SecretStr = SecretStr("")
    sse_reconnect_initial_ms: int = Field(
        default=1000,
        ge=0,  # env: WEFLOW_LEGACY_SSE_RECONNECT_INITIAL_MS
    )
    sse_reconnect_max_ms: int = Field(
        default=60000,
        gt=0,  # env: WEFLOW_LEGACY_SSE_RECONNECT_MAX_MS
    )
    # SSE 读超时（毫秒）：该时长内未收到任何数据即判定连接失效、断开重连。
    # WeFlow Legacy 无心跳机制，默认 5 分钟；半开连接（对端假死/断网无 FIN）
    # 下若无读超时，SSE 读循环会永久阻塞、监听静默死亡（审查报告【2·P1】）
    sse_read_timeout_ms: int = Field(
        default=300000,
        gt=0,  # env: WEFLOW_LEGACY_SSE_READ_TIMEOUT_MS
    )

    model_config = {
        "env_prefix": "WEFLOW_LEGACY_",  # api_base → WEFLOW_LEGACY_API_BASE
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