"""WeFlow Legacy 消息源专属配置 — 从 .env 读取 WEFLOW_LEGACY_* 环境变量。

与 app 级配置(briefdesk/config.py)分离:只有启用 weflow-legacy 源时才被加载。
环境变量名为 WEFLOW_LEGACY_API_BASE / WEFLOW_LEGACY_API_TOKEN。
"""

from typing import ClassVar

from pydantic import Field, SecretStr
from pydantic_settings import SettingsConfigDict

from briefdesk.settings_base import KeyringSettingsBase


class WeFlowLegacySettings(KeyringSettingsBase):
    """WeFlow Legacy 消息源配置，密钥字段支持系统密钥环。"""

    # 密钥解析链（keyring > 环境变量 > .env > 默认值），见 briefdesk/secrets_store.py
    KEYRING_FIELDS: ClassVar[dict[str, str]] = {"api_token": "WEFLOW_LEGACY_API_TOKEN"}

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

    # env_file/env_file_encoding/extra 由 KeyringSettingsBase 自动合并，无需展开；
    # ClassVar 注解声明类级配置而非字段（RUF012）
    model_config: ClassVar[SettingsConfigDict] = {"env_prefix": "WEFLOW_LEGACY_"}  # api_base → WEFLOW_LEGACY_API_BASE
