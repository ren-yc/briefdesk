"""RAG 插件专属配置 — 从 .env 读取 RAG_* 环境变量。

与 app 级配置(briefdesk/config.py)分离：只有启用 rag 插件时才被加载。
嵌入模型复用 app 级 EMBED_API_BASE / EMBED_API_KEY / EMBED_MODEL /
EMBED_BATCH_SIZE（经 ai_provider 插件注册的端口使用），本模块只承载
检索行为参数。
"""

from pydantic import Field
from pydantic_settings import BaseSettings

from briefdesk.settings_env import PROJECT_ROOT, get_settings_file


class RagSettings(BaseSettings):
    top_k: int = Field(default=12, ge=1)  # env: RAG_TOP_K 向量召回条数
    fts_limit: int = Field(default=12, ge=1)  # env: RAG_FTS_LIMIT 关键词召回条数
    max_evidence: int = Field(default=10, ge=1)  # env: RAG_MAX_EVIDENCE 注入 prompt 的证据上限
    min_score: float = Field(default=0.25, ge=0.0, le=1.0)  # env: RAG_MIN_SCORE 余弦拒答门限
    backfill_days: int = Field(default=7)  # env: RAG_BACKFILL_DAYS 回填窗口天；0=关 -1=全量
    backfill_batch: int = Field(default=256, ge=1)  # env: RAG_BACKFILL_BATCH 嵌入子批大小
    backfill_budget_per_cycle: int = Field(
        default=2000, ge=1
    )  # env: RAG_BACKFILL_BUDGET_PER_CYCLE 单轮回填总预算

    model_config = {
        "env_prefix": "RAG_",  # top_k → RAG_TOP_K
        "env_file": [PROJECT_ROOT / ".env", get_settings_file()],
        "env_file_encoding": "utf-8",
        # 同一 .env 里还有 app 级与其它插件的字段，忽略未知项
        "extra": "ignore",
    }
