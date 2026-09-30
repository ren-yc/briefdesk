"""配置基类，提供统一的密钥环集成。

子类只需定义 KEYRING_FIELDS 字典和 model_config['env_prefix']。
"""

from contextvars import ContextVar
from typing import Any, ClassVar

from pydantic_settings import (
    BaseSettings,
    DotEnvSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from briefdesk import settings_env
from briefdesk.secrets_store import KeyringSource

#: 本次实例化是否显式传入了 `_env_file`（含显式 None）。
#:
#: 为什么用 ContextVar 而不是「路径哨兵 + 比对 dotenv_settings.env_file」：
#: `settings_customise_sources` 是 classmethod、拿不到实例状态；而 pydantic-settings
#: **先**构造 DotEnvSettingsSource（构造本身就会读文件）**再**调用该钩子——哨兵写法
#: 即便在钩子里把 source 丢弃，文件也已经被读过（哨兵指向含非法 UTF-8 的文件时直接
#: 抛 UnicodeDecodeError）；wheel 模式下哨兵指向 site-packages，等于去读
#: site-packages/.env，违反「不读任何隐式 .env」。
_ENV_FILE_EXPLICIT: ContextVar[bool] = ContextVar(
    "briefdesk_env_file_explicit", default=False
)


class KeyringSettingsBase(BaseSettings):
    """带密钥环支持的配置基类。

    子类使用方式：
    1. 定义类级别的 KEYRING_FIELDS 字典（字段名 → 环境变量名，用 ClassVar 注解）
    2. 在 model_config 中设置 env_prefix（如 "QQFLOW_"）；其余键
       （env_file/env_file_encoding/extra）由 pydantic 自动从基类合并，无需展开

    解析优先级（高→低）：init 参数 > 系统密钥环（仅密钥字段）> 环境变量 >
    项目根 .env（仅源码 / editable 模式）> 用户暂存文件 > 默认值。

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
        # 不指向任何真实路径：DotEnvSettingsSource 在构造期就会读文件，
        # 指向真实路径等于在决定要不要读之前先读一次（见 _ENV_FILE_EXPLICIT 注释）。
        "env_file": None,
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }

    def __init__(self, **values: Any) -> None:
        # `_env_file` 是 BaseSettings.__init__ 的显式形参；本类签名用 **values 接收时
        # 它是普通键——据此判断「调用方是否显式覆盖」，无需任何路径哨兵。
        token = _ENV_FILE_EXPLICIT.set("_env_file" in values)
        try:
            super().__init__(**values)
        finally:
            _ENV_FILE_EXPLICIT.reset(token)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """来源顺序（高→低）：init > 密钥环 > 环境变量 > 项目 .env > 暂存 > 文件密钥。"""
        keyring_fields = getattr(cls, "KEYRING_FIELDS", {})
        sources: list[PydanticBaseSettingsSource] = [init_settings]
        if keyring_fields:
            sources.append(KeyringSource(settings_cls, keyring_fields))
        sources.append(env_settings)
        if _ENV_FILE_EXPLICIT.get():
            # 显式 `_env_file`（含显式 None）：完全取代两层，保留多文件「后者优先」
            # 与 None = 不读 dotenv 的语义（不做任何 env_file 值比对）。
            sources.append(dotenv_settings)
        else:
            # 缺省：项目 .env 与暂存文件是**两个独立来源**，顺序取自
            # settings_env.dotenv_layers()（与 source_of / 启动快照同一份定义）。
            for _name, path in settings_env.dotenv_layers():
                sources.append(
                    DotEnvSettingsSource(
                        settings_cls, env_file=path, env_file_encoding="utf-8"
                    )
                )
        sources.append(file_secret_settings)
        return tuple(sources)
