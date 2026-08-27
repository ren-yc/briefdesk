"""RAG 插件专属配置 — 从 .env / keyring 读取 RAG_* 环境变量。

与 app 级配置(briefdesk/config.py)分离：只有启用 rag 插件时才被加载。
嵌入模型复用 app 级 EMBED_API_BASE / EMBED_API_KEY / EMBED_MODEL /
EMBED_BATCH_SIZE（经 ai_provider 插件注册的端口使用）。

本模块承载两类字段：
- 检索行为参数（top_k / min_score / 回填与维护等，走 .env）；
- 问答模型通道（model / api_base / api_key）——分类/去重/合并走 app 级
  AI_MODEL（通常是微调模型），而 RAG 引用式问答可独立指向另一个模型（如
  未微调的通用版或云端模型）。三项全部留空 = 与主链路共用同一配置。
  这三项归本插件声明域（`RAG_` 前缀属 rag 插件），engine 在调用
  `ai_ports.rag_chat` 时作为 override 参数下传，ai_provider 只提供机制、
  不反向依赖本插件。

密钥型字段（api_key）经系统密钥环（keyring）读取，绝不落 .env 明文；
参考 qqflow / weflow 的同名机制。
"""

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource

from briefdesk.secrets_store import KeyringSource
from briefdesk.settings_env import PROJECT_ROOT, get_settings_file

# 密钥解析链（keyring > 环境变量 > .env > 默认值），见 briefdesk/secrets_store.py
_KEYRING_FIELDS = {
    "api_key": "RAG_API_KEY",
}


class RagSettings(BaseSettings):
    top_k: int = Field(default=12, ge=1)  # env: RAG_TOP_K 向量召回条数
    fts_limit: int = Field(default=12, ge=1)  # env: RAG_FTS_LIMIT 关键词召回条数
    max_evidence: int = Field(default=10, ge=1)  # env: RAG_MAX_EVIDENCE 注入 prompt 的证据上限
    evidence_chars: int = Field(default=600, ge=50)  # env: RAG_EVIDENCE_CHARS 单条证据注入 prompt 的字符上限（超出以「…」截断）
    min_score: float = Field(default=0.25, ge=0.0, le=1.0)  # env: RAG_MIN_SCORE 余弦拒答门限
    backfill_days: int = Field(default=7)  # env: RAG_BACKFILL_DAYS 回填窗口天；0=关 -1=全量
    backfill_batch: int = Field(default=256, ge=1)  # env: RAG_BACKFILL_BATCH 嵌入子批大小
    backfill_budget_per_cycle: int = Field(
        default=2000, ge=1
    )  # env: RAG_BACKFILL_BUDGET_PER_CYCLE 单轮回填总预算
    group_only: bool = Field(default=True)  # env: RAG_GROUP_ONLY 仅索引/检索群聊会话（启用会话恒为前提）
    maintenance_interval_seconds: int = Field(
        default=3600, ge=30
    )  # env: RAG_MAINTENANCE_INTERVAL_SECONDS 维护循环空闲间隔

    # ── 问答模型通道（留空 = 复用主链路 ai_*） ──
    model: str = ""  # env: RAG_MODEL（问答专用模型名）
    api_base: str = ""  # env: RAG_API_BASE（问答专用端点）
    # env: RAG_API_KEY（问答专用 API Key，走 keyring；SecretStr 自动掩码 repr/序列化）
    api_key: SecretStr = SecretStr("")

    model_config = {
        "env_prefix": "RAG_",  # top_k → RAG_TOP_K
        "env_file": [PROJECT_ROOT / ".env", get_settings_file()],
        "env_file_encoding": "utf-8",
        # 同一 .env 里还有 app 级与其它插件的字段，忽略未知项
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
