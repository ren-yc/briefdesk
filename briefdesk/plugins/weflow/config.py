"""weflow 消息源专属配置 — 从 .env / keyring 读取 WEFLOW_* 环境变量。

与 app 级配置(briefdesk/config.py)分离:只有启用 weflow 源时才被加载。

密钥型字段（api_token / img_aes_key / img_xor_key / db_keys）经系统密钥环
（keyring）读取，绝不落 .env 明文；非密钥字段（wxid / db_path / sse 参数）
走 .env。参考 qqflow（keyring SecretStr 密钥）与 weflow-legacy（SSE 参数层）。

`keys`（weflow-server.json 中单个 db 相对路径 → SQLCipher 64 位 hex 密钥
的映射对象）整体按 JSON 字符串存入密钥环条目，但 Windows 凭据管理器单条
上限约 1280 字节、26 库映射约 2347 字节放不下——故拆为两段存储：
  WEFLOW_DB_KEYS（前半）+ WEFLOW_DB_KEYS_2（后半），
`db_keys_map` property 合并两段反序列化为 dict[str, str]（形状不符返回
空 dict，由上层决定是否自禁用）。
"""

import json
import logging

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource

from briefdesk.secrets_store import KeyringSource
from briefdesk.settings_env import PROJECT_ROOT, get_settings_file

logger = logging.getLogger(__name__)

# 密钥解析链（keyring > 环境变量 > .env > 默认值），见 briefdesk/secrets_store.py
_KEYRING_FIELDS = {
    "api_token": "WEFLOW_API_TOKEN",
    "img_aes_key": "WEFLOW_IMG_AES_KEY",
    "img_xor_key": "WEFLOW_IMG_XOR_KEY",
    "db_keys": "WEFLOW_DB_KEYS",
    "db_keys_2": "WEFLOW_DB_KEYS_2",
}


class WeFlowSettings(BaseSettings):
    # ── 非密钥字段（.env） ──
    api_base: str = "http://127.0.0.1:5033"  # env: WEFLOW_API_BASE
    wxid: str = ""  # env: WEFLOW_WXID（微信 ID，参与请求/路径拼装）
    db_path: str = ""  # env: WEFLOW_DB_PATH（wechat 数据目录，含 wxid 的半隐私路径）

    # ── 密钥字段（keyring，SecretStr 自动掩码 repr/序列化） ──
    api_token: SecretStr = SecretStr("")  # env: WEFLOW_API_TOKEN
    img_aes_key: SecretStr = SecretStr("")  # env: WEFLOW_IMG_AES_KEY（图片 AES 解密密钥）
    img_xor_key: SecretStr = SecretStr("")  # env: WEFLOW_IMG_XOR_KEY（图片 XOR 解密密钥）
    # env: WEFLOW_DB_KEYS / WEFLOW_DB_KEYS_2（各一段 JSON 字符串：
    # {相对路径: 64位hex} 的库→密钥映射，合并后为完整 keys 对象）
    db_keys: SecretStr = SecretStr("")
    db_keys_2: SecretStr = SecretStr("")

    # ── SSE 参数（.env） ──
    sse_reconnect_initial_ms: int = Field(
        default=1000,
        ge=0,  # env: WEFLOW_SSE_RECONNECT_INITIAL_MS
    )
    sse_reconnect_max_ms: int = Field(
        default=60000,
        gt=0,  # env: WEFLOW_SSE_RECONNECT_MAX_MS
    )
    sse_read_timeout_ms: int = Field(
        default=300000,
        gt=0,  # env: WEFLOW_SSE_READ_TIMEOUT_MS
    )

    model_config = {
        "env_prefix": "WEFLOW_",  # api_base → WEFLOW_API_BASE
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

    @property
    def db_keys_map(self) -> dict[str, str]:
        """把 WEFLOW_DB_KEYS / WEFLOW_DB_KEYS_2 的 JSON 字符串合并解析为
        {相对路径: hex} 映射。

        未配置 / 非法 JSON / 形状不符（值非 64 位 hex）时返回空 dict，
        并记 WARNING——`keys` 是可选增强项，缺失不应阻断其它字段的读取，
        由上层决定是否据此自禁用。
        """
        result: dict[str, str] = {}
        for field_name, key_name in (("db_keys", "WEFLOW_DB_KEYS"), ("db_keys_2", "WEFLOW_DB_KEYS_2")):
            raw = getattr(self, field_name).get_secret_value()
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("%s 非法 JSON，按未配置处理", key_name)
                continue
            if not isinstance(data, dict):
                logger.warning("%s 应为 JSON 对象，按未配置处理", key_name)
                continue
            for key, value in data.items():
                if not isinstance(key, str) or not isinstance(value, str):
                    continue
                result[key] = value
        return result
