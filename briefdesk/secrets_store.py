"""系统密钥环存储 — 秘密解析链的第一层（keyring > 环境变量 > .env > 默认值）。

Windows 下 keyring 走凭据管理器（DPAPI 加密，随用户账号）；macOS=钥匙串；
Linux=Secret Service。密钥环不可用（无桌面会话 / 无 Secret Service）或显式
禁用（`BRIEFDESK_KEYRING=0`）时静默降级：get_secret 返回 None，解析链继续
走环境变量 / .env，应用照常启动——安全分层是渐进式兜底，不是硬故障。

密钥只写入系统密钥环（CLI `briefdesk secrets set`），绝不回写 .env 明文
文件；UI/CLI 也只能查询「是否配置」状态，不回传明文。

空条目（值 == ""）与未配置同语义（真值判定）：读取层不会让空串以「已配置」
身份压过环境变量/.env 的有效值。

`WEFLOW_DB_KEYS` 特有：配置语义上是一份完整 JSON，但存储层按
`DB_KEYS_SEGMENT_LIMIT` 自动切成动态 N 段（规范名 + `_2`/`_3`…）——拆段只在
本模块的 set_db_keys/get_db_keys 助手内发生，对配置输入与读取侧完全透明。
"""

import json
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
#
# 注意：WEFLOW_DB_KEYS 的存储段（WEFLOW_DB_KEYS_2 / _3 / …）**不在此白名单内**——
# 它们是密钥环存储层的物理分割（见 DB_KEYS_* 助手），对用户/CLI/UI 完全透明。
SECRET_NAMES = (
    "AI_API_KEY",
    "EMBED_API_KEY",
    "WEFLOW_API_TOKEN",
    "WEFLOW_IMG_AES_KEY",
    "WEFLOW_IMG_XOR_KEY",
    "WEFLOW_DB_KEYS",
    "WEFLOW_LEGACY_API_TOKEN",
    "QQFLOW_API_TOKEN",
    "QQFLOW_KEY",
    "RAG_API_KEY",
)

# ── WEFLOW_DB_KEYS 配置/存储语义分离 ────────────────────────────────────────
# 配置输入层始终把 WEFLOW_DB_KEYS 视为**一份完整 JSON**（{相对路径: 64位hex} 库
# 密钥映射）。但 Windows 凭据管理器单条条目有容量上限，而微信 4.x 实测 26 库
# 约 2347 字节放不下——拆段是**存储层的物理细节**，不会泄漏到配置输入/读取侧。
#
# 拆段布局（动态 N 段，段序固定）：
#   segment 0  → WEFLOW_DB_KEYS        （也是用户/CLI/UI 唯一可见的规范名）
#   segment n  → WEFLOW_DB_KEYS_{n+1}  （n≥1，故 n=1 → WEFLOW_DB_KEYS_2）
# 读取时按序拼接；对 JSON 解析不可行时回退「旧格式」——旧版把两段各存半份
# JSON 对象（各自 json.loads 后按 dict 合并）。两种格式都透明产出完整 JSON。
DB_KEYS_BASE = "WEFLOW_DB_KEYS"

# 单条 keyring 条目安全净载荷上限（字节）。Windows 凭据管理器 CRED_MAX 约
# 2560 字节，但条目还含服务名/用户名/注释等头尾开销，留足余量取 1100B，并做
# 字节级硬校验兜底；换容量更大的存储后端时可调大让段数自动收敛回 1。
DB_KEYS_SEGMENT_LIMIT: int = 1100


def _db_keys_segment_name(index: int) -> str:
    """第 index 段的 keyring 条目名：0 → WEFLOW_DB_KEYS，n≥1 → ..._n+1。"""
    if index < 0:
        raise ValueError(f"非法段索引: {index}")
    return DB_KEYS_BASE if index == 0 else f"{DB_KEYS_BASE}_{index + 1}"


def split_db_keys(json_text: str) -> list[str]:
    """把完整 DB_KEYS JSON 文本按字节上限切成 1..N 段（段序固定）。

    在字符边界切分，绝不把一个多字节 UTF-8 字符拆到两段（各段被独立存进
    keyring，必须是合法 UTF-8 文本）。单条总长不超过上限时直接返回整段。
    """
    if len(json_text.encode("utf-8")) <= DB_KEYS_SEGMENT_LIMIT:
        return [json_text]
    segments: list[str] = []
    current: list[str] = []
    current_bytes = 0
    for ch in json_text:
        char_bytes = len(ch.encode("utf-8"))
        if current and current_bytes + char_bytes > DB_KEYS_SEGMENT_LIMIT:
            segments.append("".join(current))
            current = []
            current_bytes = 0
        current.append(ch)
        current_bytes += char_bytes
    if current:
        segments.append("".join(current))
    return segments


def join_db_keys(segments: list[str]) -> str:
    """按序拼接各段为完整 DB_KEYS JSON 文本（仅拼接，不做 JSON 校验）。"""
    return "".join(segments)


def _read_db_keys_segments() -> list[str]:
    """按段序读出全部已配置段（从段 0 起，遇空即停）。"""
    segments: list[str] = []
    while True:
        value = get_secret(_db_keys_segment_name(len(segments)))
        if not value:
            break
        segments.append(value)
    return segments


def _delete_db_keys_segments() -> None:
    """清掉当前全部 DB_KEYS 段（写前重置 / 删除入口用，幂等）。"""
    index = 0
    while get_secret(_db_keys_segment_name(index)) is not None:
        delete_secret(_db_keys_segment_name(index))
        index += 1


def set_db_keys(json_text: str) -> None:
    """把完整 DB_KEYS JSON 写入密钥环；超限自动切段存储（存储细节，调用方无感）。

    写路径要求密钥环可用（否则抛 SecretsStoreError），行为与 set_secret 一致。
    """
    if not is_keyring_available():
        raise SecretsStoreError(
            "系统密钥环不可用（可用 BRIEFDESK_KEYRING=0 确认强制禁用）"
        )
    _delete_db_keys_segments()
    for index, segment in enumerate(split_db_keys(json_text)):
        set_secret(_db_keys_segment_name(index), segment)


def delete_db_keys() -> None:
    """删除全部 DB_KEYS 段（幂等）。"""
    _delete_db_keys_segments()


def get_db_keys() -> str | None:
    """读回完整 DB_KEYS JSON 文本，兼容新/旧两种存储格式。

    - 新格式：各段是连续 JSON 的字节切片 → 按序拼接后 json.loads 校验，返回拼接串。
    - 旧格式：各段是独立 JSON 对象（旧版各存半份映射）→ 拼接不可解析，则逐段
      json.loads 并按 dict 合并，返回合并后的规范化 JSON。
    均不可解析/无任何段 → 返回 None。
    """
    segments = _read_db_keys_segments()
    if not segments:
        return None
    joined = join_db_keys(segments)
    try:
        json.loads(joined)
    except json.JSONDecodeError:
        merged: dict[str, str] = {}
        for segment in segments:
            try:
                data = json.loads(segment)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                for key, value in data.items():
                    if isinstance(key, str) and isinstance(value, str):
                        merged[key] = value
        return (
            json.dumps(merged, ensure_ascii=False, separators=(",", ":"))
            if merged
            else None
        )
    return joined


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

    def _read_value(self, name: str) -> str | None:
        """按密钥名取值；`WEFLOW_DB_KEYS` 特判为存储层段合并后的完整 JSON。

        其它密钥沿用单条 get_secret。这样配置层只面对一份完整 `WEFLOW_DB_KEYS`，
        拆段/合并在存储层完成，`KeyringSource` 得以维持统一的解析链优先级。
        """
        if name == DB_KEYS_BASE:
            return get_db_keys()
        return get_secret(name)

    def get_field_value(
        self, field: FieldInfo, field_name: str
    ) -> tuple[Any, str, bool]:
        """返回 (值, 键名, 是否有效)；字段不在映射中或未配置/空条目返回无效。

        空条目与未配置同语义（真值判定，与 configured_names 口径一致）：
        `secrets set X ""` 产生的空串不得以「已配置」身份压过环境变量/.env
        里的有效值。
        """
        name = self._field_map.get(field_name)
        if name is None:
            return None, field_name, False
        value = self._read_value(name)
        if not value:
            return None, field_name, False
        return SecretStr(value), self._key_for_field(field_name), True

    def __call__(self) -> dict[str, Any]:
        return {
            self._key_for_field(field_name): SecretStr(value)
            for field_name, name in self._field_map.items()
            if (value := self._read_value(name))
        }
