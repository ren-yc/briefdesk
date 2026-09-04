"""应用配置 — pydantic-settings 从 .env 读取，含默认值。"""

from pathlib import Path
from typing import ClassVar

from pydantic import Field, SecretStr

from briefdesk.settings_base import KeyringSettingsBase

# 项目根目录（briefdesk/config.py 上溯两级）：.env 与默认 DB 路径均以此为基准，
# 保证从任意工作目录启动（python main.py / python -m briefdesk / briefdesk）读到同一份配置，
# 避免 console script 在其它目录运行时静默丢失 .env 或把数据库建到错误位置。
PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(KeyringSettingsBase):
    """应用配置，从 .env 读取，密钥字段支持系统密钥环。"""

    # 密钥解析链（keyring > 环境变量 > .env > 默认值）：
    # 系统密钥环由 CLI `briefdesk secrets set` 写入，见 briefdesk/secrets_store.py
    KEYRING_FIELDS: ClassVar[dict[str, str]] = {
        "ai_api_key": "AI_API_KEY",
        "embed_api_key": "EMBED_API_KEY",
    }
    plugins: list[str] = Field(default=["*"], alias="PLUGINS")
    """启用的插件名列表（JSON 数组；"*" = 全部发现的插件）。
    消息源启用的唯一开关：weflow-legacy/qqflow 等源插件由本开关控制。"""

    plugins_disabled: list[str] = Field(default=[], alias="PLUGINS_DISABLED")
    """明确禁用的插件名列表（JSON 数组），优先于 PLUGINS。"""

    plugins_required: list[str] = Field(default=[], alias="PLUGINS_REQUIRED")
    """必选插件名列表（JSON 数组）：其 setup/activate 失败视为致命
    （抛 PluginError 中止启动），其余插件失败仅禁用并继续。"""

    plugin_path: str = Field(default="", alias="PLUGIN_PATH")
    """开发期插件目录：目录下每个 *.py 文件暴露 `plugin` 实例即被
    PluginManager 加载（免打包）；留空 = 不扫描。"""

    ai_api_key: SecretStr = Field(default=SecretStr(""), alias="AI_API_KEY")
    ai_api_base: str = Field(default="https://api.deepseek.com", alias="AI_API_BASE")
    ai_model: str = Field(default="deepseek-v4-flash", alias="AI_MODEL")
    ai_max_concurrency: int = Field(default=4, alias="AI_MAX_CONCURRENCY", ge=0)
    """AI 请求最大并发（chat 与嵌入共用），0 = 不限制。默认 4：不设限时大回填
    会一次性放行全部批次形成请求风暴（429 → 整批 failed → 钉窗重拉放大）。"""

    ai_disable_thinking: bool = Field(default=False, alias="AI_DISABLE_THINKING")
    """设为 true 时，AI 请求会附带 reasoning_effort="none"，
    用于关闭 Qwen3 / Qwen3.5 等模型的思考模式。"""

    ai_vision_enabled: bool = Field(default=False, alias="AI_VISION_ENABLED")
    """主模型支持图片输入（视觉模型）时开启：含图消息将 OCR 文本连同图片
    一并送入 AI_MODEL 分类（vision 路由）；关闭时维持纯文本 OCR 路径（现状）。
    图片字节由 enrich（ocr 插件）下载缩放后随批传递——需启用 ocr 插件；
    请求级失败自动降级纯文本重试并置公告提示。"""

    ai_vision_max_images: int = Field(
        default=4, alias="AI_VISION_MAX_IMAGES", ge=1, le=20
    )
    """单条消息随分类请求附图的上限：多图消息超出部分只发 OCR 文本；
    单次请求另有图片总量预算兜底（classify 引擎内 _MAX_IMAGES_PER_REQUEST）。"""

    # RAG 问答专用模型通道（RAG_MODEL/RAG_API_BASE/RAG_API_KEY）不在此声明：
    # `RAG_` 前缀归 rag 插件所有，三项由 briefdesk/plugins/rag/config.py 承载，
    # 经 ai_ports.rag_chat 的 override 参数下传（见该模块 docstring）。

    max_classify_tokens: int = Field(default=8192, alias="MAX_CLASSIFY_TOKENS", gt=0)

    backfill_hours: int = Field(default=24, alias="BACKFILL_HOURS", ge=-1)
    """自动回填窗口（小时）；-1 = 拉取全部历史（见 .env.example 警告）。"""

    ignored_expiry_hours: int = Field(default=0, alias="IGNORED_EXPIRY_HOURS", ge=0)
    """已忽略条目过期小时数，0 = 禁用清理。"""

    ignore_self: bool = Field(default=True, alias="IGNORE_SELF")
    """过滤本账号自己发送的消息（所有消息入口：SSE 实时 + REST 回填）。
    weflow-legacy REST 按 isSend 判定、SSE 上游已不推送自消息；qqflow REST 按
    自身 UID（u_<QQFLOW_QQ>）判定、SSE 事件无发送者标识需按消息回查 REST。"""

    realtime_batch_max_count: int = Field(
        default=1, alias="REALTIME_BATCH_MAX_COUNT", gt=0
    )
    realtime_batch_timeout_ms: int = Field(
        default=180000, alias="REALTIME_BATCH_TIMEOUT_MS", gt=0
    )
    backfill_batch_max_count: int = Field(
        default=10, alias="BACKFILL_BATCH_MAX_COUNT", gt=0
    )

    poll_overlap_seconds: int = Field(default=300, alias="POLL_OVERLAP_SECONDS", ge=0)
    """增量轮询窗口与上次水位之间的重叠秒数：吸收边界秒、时钟偏差与
    翻页期间上游插入导致的 offset 漂移；重叠部分由 processed_messages
    去重，无 AI 开销。"""

    poll_interval_seconds: int = Field(default=0, alias="POLL_INTERVAL_SECONDS", ge=0)
    """周期同步间隔（秒），0 = 禁用（默认，保持纯手动/启动期同步）。
    SSE 断连/监听死亡窗口的消息补齐兜底：>0 时按该周期自动触发与
    /api/sync 同路径的同步（进行中互斥）。"""

    dedup_similarity_threshold: float = Field(
        default=0.3, alias="DEDUP_SIMILARITY_THRESHOLD", ge=0, le=1
    )

    # 嵌入向量去重（可选）：EMBED_API_BASE 留空则整体禁用，回退到字符重叠预过滤
    embed_api_base: str = Field(default="", alias="EMBED_API_BASE")
    """嵌入向量去重（可选）：EMBED_API_BASE 留空则整体禁用，回退到字符重叠预过滤。"""
    embed_model: str = Field(default="", alias="EMBED_MODEL")
    embed_api_key: SecretStr = Field(default=SecretStr(""), alias="EMBED_API_KEY")
    embed_batch_size: int = Field(default=20, alias="EMBED_BATCH_SIZE", gt=0)
    dedup_embed_threshold: float = Field(
        default=0.80, alias="DEDUP_EMBED_THRESHOLD", ge=0, le=1
    )
    dedup_embed_top_k: int = Field(default=3, alias="DEDUP_EMBED_TOP_K", gt=0)
    dedup_strong_threshold: float = Field(
        default=0.99, alias="DEDUP_STRONG_THRESHOLD", ge=0, le=1
    )
    """同文本短路阈值：候选相似度 ≥ 此值时视为同文本，AI 判 SAME 即直接判重，
    不参与多数票（防被高相似但不同话题的干扰候选稀释成平票而漏判）。"""
    dedup_embed_fallback_threshold: float = Field(
        default=0.65, alias="DEDUP_EMBED_FALLBACK_THRESHOLD", ge=0, le=1
    )
    """低置信复核阈值：余弦候选相似度落在 [fallback, DEDUP_EMBED_THRESHOLD)
    区间且无正常候选时，全员判 SAME 才判重（弱候选复核通道）——覆盖中段
    相似度、低于门禁但确实重复的情形。"""

    merge_window_minutes: int = Field(default=10, alias="MERGE_WINDOW_MINUTES", ge=0)
    """会话内同话题片段合并（同一话题多条消息→一张卡）：窗口内、同会话同类别
    的相邻卡片经 AI 判官判定合并；0 = 禁用合并。"""
    merge_max_candidates: int = Field(default=3, alias="MERGE_MAX_CANDIDATES", gt=0)

    db_path: str = Field(default=str(PROJECT_ROOT / "briefdesk.sqlite"), alias="DB_PATH")
    """SQLite 文件路径；默认落在项目根目录（用户显式配置的相对路径仍按 cwd 解析）。"""
    server_port: int = Field(default=3000, alias="SERVER_PORT", ge=1, le=65535)
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    """日志级别（DEBUG / INFO / WARNING / ERROR / CRITICAL），由 logger.py 读取。"""

    model_config = {
        **KeyringSettingsBase.model_config,
        "populate_by_name": True,
    }


config = Settings()
