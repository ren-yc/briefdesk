"""weflow 消息源专属配置 — 从 .env / keyring 读取 WEFLOW_* 环境变量。

与 app 级配置(briefdesk/config.py)分离:只有启用 weflow 源时才被加载。

密钥型字段（api_token / img_aes_key / img_xor_key / db_keys）经系统密钥环
（keyring）读取，绝不落 .env 明文；非密钥字段（wxid / db_path / sse 参数）
走 .env。参考 qqflow（keyring SecretStr 密钥）与 weflow-legacy（SSE 参数层）。

`WEFLOW_DB_KEYS`（weflow-server.json 中单个 db 相对路径 → SQLCipher 64 位 hex
密钥的映射对象，JSON 字符串）在**配置语义上是一份完整 JSON**。Windows 凭据
管理器单条上限约 1280 字节、26 库映射约 2347 字节放不下，故存储层
(briefdesk/secrets_store.py) 自动切成动态多段——拆段对配置侧透明，字段仍只
是一个 `db_keys: SecretStr`，`db_keys_map` property 直接解析合并后的完整 JSON。
"""

import json
import logging
from typing import ClassVar

from pydantic import Field, SecretStr
from pydantic_settings import SettingsConfigDict

from briefdesk.settings_base import KeyringSettingsBase

logger = logging.getLogger(__name__)


class WeFlowSettings(KeyringSettingsBase):
    """WeFlow 消息源配置，密钥字段支持系统密钥环。"""

    # 密钥解析链（keyring > 环境变量 > .env > 默认值），见 briefdesk/secrets_store.py。
    # WEFLOW_DB_KEYS 由 KeyringSource 特判读取合并后的完整 JSON（存储层切段，
    # 配置层只见一份）。
    KEYRING_FIELDS: ClassVar[dict[str, str]] = {
        "api_token": "WEFLOW_API_TOKEN",
        "img_aes_key": "WEFLOW_IMG_AES_KEY",
        "img_xor_key": "WEFLOW_IMG_XOR_KEY",
        "db_keys": "WEFLOW_DB_KEYS",
    }
    # ── 非密钥字段（.env） ──
    api_base: str = "http://127.0.0.1:5033"  # env: WEFLOW_API_BASE
    wxid: str = ""  # env: WEFLOW_WXID（微信 ID，参与请求/路径拼装）
    db_path: str = ""  # env: WEFLOW_DB_PATH（wechat 数据目录，含 wxid 的半隐私路径）

    # ── 密钥字段（keyring，SecretStr 自动掩码 repr/序列化） ──
    api_token: SecretStr = SecretStr("")  # env: WEFLOW_API_TOKEN
    img_aes_key: SecretStr = SecretStr("")  # env: WEFLOW_IMG_AES_KEY（图片 AES 解密密钥）
    img_xor_key: SecretStr = SecretStr("")  # env: WEFLOW_IMG_XOR_KEY（图片 XOR 解密密钥）
    # env: WEFLOW_DB_KEYS（完整 JSON：{相对路径: 64位hex} 库→密钥映射；超长时由
    # 存储层自动切段，配置侧只填/读这份完整映射）
    db_keys: SecretStr = SecretStr("")

    # ── SSE 参数（.env） ──
    sse_reconnect_initial_ms: int = Field(
        default=1000,
        gt=0,  # env: WEFLOW_SSE_RECONNECT_INITIAL_MS（复核 P3-17：0 会退化为热重连风暴）
    )
    sse_reconnect_max_ms: int = Field(
        default=60000,
        gt=0,  # env: WEFLOW_SSE_RECONNECT_MAX_MS
    )
    # weflow-server 每 25s 发一个 ping 注释帧保活（weflow-server-api.md），
    # 60s ≈ 2.4 个周期，与 qqflow 同口径。原先的 300000 是从 weflow-legacy
    # 抄来的——那个源上游确实无心跳，只能靠 5 分钟兜住半开连接；这里有心跳
    # 可用，5 分钟等于白等 4 分半才发现连接已死。
    sse_read_timeout_ms: int = Field(
        default=60000,
        gt=0,  # env: WEFLOW_SSE_READ_TIMEOUT_MS
    )

    # env_file/env_file_encoding/extra 由 KeyringSettingsBase 自动合并，无需展开；
    # ClassVar 注解声明类级配置而非字段（RUF012）
    model_config: ClassVar[SettingsConfigDict] = {"env_prefix": "WEFLOW_"}  # api_base → WEFLOW_API_BASE

    @property
    def db_keys_map(self) -> dict[str, str]:
        """把 `WEFLOW_DB_KEYS` 的完整 JSON 字符串解析为 {相对路径: hex} 映射。

        keyring 写入超长时会自动切段，但解析链（KeyringSource 特判）始终把
        `db_keys` 字段还原为一份完整 JSON，故这里无需再合并——直接解析。非法
        JSON / 非 JSON 对象记 WARNING 并返回空 dict，由上层决定是否据以自禁用；
        键或值非字符串的条目静默跳过；不校验 hex 形状，值按原样保留。`keys` 是
        可选增强项，缺失不应阻断其它字段读取。
        """
        raw = self.db_keys.get_secret_value()
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("WEFLOW_DB_KEYS 非法 JSON，按未配置处理")
            return {}
        if not isinstance(data, dict):
            logger.warning("WEFLOW_DB_KEYS 应为 JSON 对象，按未配置处理")
            return {}
        return {
            key: value
            for key, value in data.items()
            if isinstance(key, str) and isinstance(value, str)
        }
