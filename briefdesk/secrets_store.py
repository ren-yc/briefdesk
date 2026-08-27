"""系统密钥环存储 — 秘密解析链的第一层（keyring > 环境变量 > .env > 默认值）。

Windows 下 keyring 走凭据管理器（DPAPI 加密，随用户账号）；macOS=钥匙串；
Linux=Secret Service。密钥环不可用（无桌面会话 / 无 Secret Service）或显式
禁用（`BRIEFDESK_KEYRING=0`）时静默降级：get_secret 返回 None，解析链继续
走环境变量 / .env，应用照常启动——安全分层是渐进式兜底，不是硬故障。

密钥只写入系统密钥环（CLI `briefdesk secrets set`），绝不回写 .env 明文
文件；UI/CLI 也只能查询「是否配置」状态，不回传明文。
"""

import logging
import os
from typing import Any

from pydantic import SecretStr
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource

logger = logging.getLogger(__name__)

# 密钥环服务名（keyring 按 (service, username) 分条目）
SERVICE_NAME = "briefdesk"

# 可管理的秘密白名单（env 风格命名，与 .env / CLI 参数对齐；
# CLI 与 UI 只允许操作这些键，拒绝任意 key 防误写）
SECRET_NAMES = (
    "AI_API_KEY",
    "EMBED_API_KEY",
    "WEFLOW_API_TOKEN",
    "WEFLOW_IMG_AES_KEY",
    "WEFLOW_IMG_XOR_KEY",
    "WEFLOW_DB_KEYS",
    "WEFLOW_DB_KEYS_2",
    "WEFLOW_LEGACY_API_TOKEN",
    "QQFLOW_API_TOKEN",
    "QQFLOW_KEY",
    "RAG_API_KEY",
)


class SecretsStoreError(RuntimeError):
    """密钥环读写失败（写路径向 CLI/UI 显式报错；读路径保持静默降级）。"""


def is_keyring_available() -> bool:
    """密钥环是否可用：`BRIEFDESK_KEYRING=0` 强制禁用；可用性失败视为不可用。"""
    if os.environ.get("BRIEFDESK_KEYRING", "").strip().lower() in ("0", "false", "no"):
        return False
    try:
        import keyring  # 延迟导入：未安装时整体降级

        keyring.get_keyring()
        return True
    except Exception:  # noqa: BLE001 — 后端缺失/未启动一律视为不可用
        return False


def get_secret(name: str) -> str | None:
    """读取密钥环中的秘密；不可用/未设置/异常均返回 None（永不阻断启动）。"""
    if not is_keyring_available():
        return None
    try:
        import keyring

        return keyring.get_password(SERVICE_NAME, name)
    except Exception:  # 读失败只影响该层，后续层继续
        logger.debug("密钥环读取失败（%s），回退后续配置层", name, exc_info=True)
        return None


def set_secret(name: str, value: str) -> None:
    """写入密钥环；不可用或写入失败抛 SecretsStoreError（写路径必须显式报错）。"""
    if not is_keyring_available():
        raise SecretsStoreError(
            "系统密钥环不可用（可用 BRIEFDESK_KEYRING=0 确认强制禁用）"
        )
    try:
        import keyring

        keyring.set_password(SERVICE_NAME, name, value)
    except Exception as exc:  # 向上统一为可读错误
        raise SecretsStoreError(f"写入系统密钥环失败: {exc}") from exc


def delete_secret(name: str) -> None:
    """删除密钥环中的秘密；未设置/不可用视为成功（幂等）。"""
    if not is_keyring_available():
        return
    try:
        import keyring

        keyring.delete_password(SERVICE_NAME, name)
    except keyring.errors.PasswordDeleteError:
        pass
    except Exception as exc:
        raise SecretsStoreError(f"删除系统密钥环条目失败: {exc}") from exc


def configured_names() -> list[str]:
    """已配置（条目存在且非空）的秘密名列表；仅用于状态展示，不回传值。"""
    return [name for name in SECRET_NAMES if get_secret(name)]


class KeyringSource(PydanticBaseSettingsSource):
    """pydantic-settings 自定义源：按字段映射从系统密钥环读取秘密。

    优先级由 settings_customise_sources 的返回顺序决定（本方案位于环境
    变量之前）：keyring > 环境变量 > .env > 默认值。
    """

    def __init__(self, settings_cls: type[BaseSettings], field_map: dict[str, str]):
        super().__init__(settings_cls)
        self._field_map = field_map

    def _key_for_field(self, field_name: str) -> str:
        """输出键与 EnvSettingsSource 保持一致：用字段别名。

        否则同一字段会出现「字段名键 + 别名键」两个键（keyring 层与 env 层
        各贡献一个），传给 pydantic 时别名键总是胜出，keyring 层永远被环境
        变量覆盖，与自定义源的先后顺序无关。
        """
        field = self.settings_cls.model_fields.get(field_name)
        if field is None:
            return field_name
        return field.alias or field_name

    def get_field_value(
        self, field: FieldInfo, field_name: str
    ) -> tuple[Any, str, bool]:
        """返回 (值, 键名, 是否有效)；字段不在映射中或未配置返回无效。"""
        name = self._field_map.get(field_name)
        if name is None:
            return None, field_name, False
        value = get_secret(name)
        if value is None:
            return None, field_name, False
        return SecretStr(value), self._key_for_field(field_name), True

    def __call__(self) -> dict[str, Any]:
        return {
            self._key_for_field(field_name): SecretStr(value)
            for field_name, name in self._field_map.items()
            if (value := get_secret(name)) is not None
        }
