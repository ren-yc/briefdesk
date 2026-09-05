"""配置基类，提供统一的密钥环集成。

子类只需定义 KEYRING_FIELDS 字典和 model_config['env_prefix']。
"""

from typing import ClassVar

from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from briefdesk.secrets_store import KeyringSource
from briefdesk.settings_env import PROJECT_ROOT, get_settings_file


class KeyringSettingsBase(BaseSettings):
    """带密钥环支持的配置基类。

    子类使用方式：
    1. 定义类级别的 KEYRING_FIELDS 字典（字段名 → 环境变量名，用 ClassVar 注解）
    2. 在 model_config 中设置 env_prefix（如 "QQFLOW_"）；其余键
       （env_file/env_file_encoding/extra）由 pydantic 自动从基类合并，无需展开

    密钥环字段会自动从系统密钥环读取，优先级：
    init参数 > 密钥环 > 环境变量 > .env > 默认值

    示例：
        class QqFlowSettings(KeyringSettingsBase):
            KEYRING_FIELDS: ClassVar[dict[str, str]] = {
                "api_token": "QQFLOW_API_TOKEN",
                "key": "QQFLOW_KEY",
            }

            api_token: SecretStr = SecretStr("")
            key: SecretStr = SecretStr("")

            model_config: ClassVar[SettingsConfigDict] = {"env_prefix": "QQFLOW_"}
    """

    # ClassVar 注解声明这是类级配置而非字段（RUF012；经中间基类继承时
    # ruff 对 pydantic 模型的豁免不再传导，子类需显式注解）
    model_config: ClassVar[SettingsConfigDict] = {
        "env_file": [PROJECT_ROOT / ".env", get_settings_file()],
        "env_file_encoding": "utf-8",
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
        keyring_fields = getattr(cls, "KEYRING_FIELDS", {})
        sources = [init_settings]
        if keyring_fields:
            sources.append(KeyringSource(settings_cls, keyring_fields))
        sources.extend([env_settings, dotenv_settings, file_secret_settings])
        return tuple(sources)
