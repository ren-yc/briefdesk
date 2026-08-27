"""WeFlow Legacy 消息源专属配置 — 从 .env 读取 WEFLOW_LEGACY_* 环境变量。

与 app 级配置(briefdesk/config.py)分离:只有启用 weflow-legacy 源时才被加载。
环境变量名改为 WEFLOW_LEGACY_API_BASE / WEFLOW_LEGACY_API_TOKEN，但为兼容
既有部署，缺失新前缀时回读旧前缀 WEFLOW_*（见 LegacyEnvSource）。
"""

from typing import Any

from pydantic import Field, SecretStr
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource

from briefdesk.secrets_store import KeyringSource
from briefdesk.settings_env import PROJECT_ROOT, get_settings_file

# 密钥解析链（keyring > 环境变量 > .env > 默认值），见 briefdesk/secrets_store.py
_KEYRING_FIELDS = {"api_token": "WEFLOW_LEGACY_API_TOKEN"}
# 密钥环旧键回退：迁移期兼容，主键未命中时回读旧 WEFLOW_API_TOKEN。
_KEYRING_FALLBACK = {"api_token": "WEFLOW_API_TOKEN"}

# 旧前缀回读映射：字段名 → 旧环境变量名（仅当新前缀缺失时回退）。
_LEGACY_ENV_FALLBACK = {
    "api_token": "WEFLOW_API_TOKEN",
    "api_base": "WEFLOW_API_BASE",
    "sse_reconnect_initial_ms": "WEFLOW_SSE_RECONNECT_INITIAL_MS",
    "sse_reconnect_max_ms": "WEFLOW_SSE_RECONNECT_MAX_MS",
    "sse_read_timeout_ms": "WEFLOW_SSE_READ_TIMEOUT_MS",
}


class LegacyEnvSource(PydanticBaseSettingsSource):
    """旧前缀回退源：新前缀键缺失时回读旧 WEFLOW_*。

    pydantic-settings 的 EnvSettingsSource 只识别新前缀(WEFLOW_LEGACY_)下的
    环境变量键，DotEnvSettingsSource 也只识别新前缀下的 .env 键；旧前缀的
    键会被判为 `is_complex` 而跳过。此处单独接管旧前缀的读取，覆盖两处：
    进程环境变量、已配置的 env 文件（跟随 `settings_customise_sources` 传入
    的 `env_file`，因此测试的 `_env_file=None` 覆盖同样生效）。
    本源被置于 env 源之后、dotenv 源之前，因此现存的 WEFLOW_* 只在对应的
    WEFLOW_LEGACY_* 未设置时兜底，不覆盖新值。
    """

    def __init__(
        self,
        settings_cls: type[BaseSettings],
        env_file: Any | None = None,
    ):
        self.env_file = env_file
        super().__init__(settings_cls)

    def _legacy_field_values(self) -> dict[str, str]:
        """读取旧前缀值：进程环境变量 > env 文件（后加载者优先）。"""
        import os

        from dotenv import dotenv_values

        merged: dict[str, str] = {}
        # env 文件（低优先级）：可传 None（测试隔离）或路径/路径列表。
        files = self.env_file
        if files is None:
            files = []
        elif isinstance(files, (str, os.PathLike)):
            files = [files]
        for path in files:
            try:
                data = dotenv_values(str(path), encoding="utf-8")
            except OSError:
                continue
            for legacy_key in _LEGACY_ENV_FALLBACK.values():
                value = data.get(legacy_key)
                if value is not None:
                    merged[legacy_key] = value
        # 进程环境变量（高优先级）：仅覆盖 os.environ 显式存在的键。
        for legacy_key in _LEGACY_ENV_FALLBACK.values():
            if legacy_key in os.environ:
                merged[legacy_key] = os.environ[legacy_key]
        return merged

    def _alias_for(self, field_name: str) -> str:
        """字段 → 输出键，与 KeyringSource._key_for_field 一致：用字段别名，
        别名缺省时回退字段名（本项目 Settings 字段 alias 均为 None）。"""
        field = self.settings_cls.model_fields.get(field_name)
        if field is None:
            return field_name
        return field.alias or field_name

    def get_field_value(
        self, field: FieldInfo, field_name: str
    ) -> tuple[Any, str, bool]:
        legacy_key = _LEGACY_ENV_FALLBACK.get(field_name)
        if legacy_key is None:
            return None, field_name, False
        value = self._legacy_field_values().get(legacy_key)
        if value is None:
            return None, field_name, False
        return value, self._alias_for(field_name), True

    def __call__(self) -> dict[str, Any]:
        values = self._legacy_field_values()
        return {
            self._alias_for(field_name): value
            for field_name, legacy_key in _LEGACY_ENV_FALLBACK.items()
            if (value := values.get(legacy_key)) is not None
        }


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
        """来源优先级：init 参数 > 系统密钥环 > 环境变量 > 旧前缀回退 > .env > 默认值。"""
        # 旧前缀回退源跟随 dotenv_settings 的 env_file（含 _env_file=None 覆盖），
        # 避免直接硬编码项目 .env 路径而绕过测试隔离与用户显式指定的环境文件。
        env_file = getattr(dotenv_settings, "env_file", None)
        return (
            init_settings,
            KeyringSource(settings_cls, _KEYRING_FIELDS, _KEYRING_FALLBACK),
            env_settings,
            LegacyEnvSource(settings_cls, env_file=env_file),
            dotenv_settings,
            file_secret_settings,
        )
