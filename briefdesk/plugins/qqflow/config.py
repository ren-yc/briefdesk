"""qqflow 消息源专属配置 — 从 .env 读取 QQFLOW_* 环境变量。

与 app 级配置(briefdesk/config.py)分离:只有启用 qqflow 源时才被加载。
QQFLOW_API_TOKEN / QQFLOW_QQ / QQFLOW_KEY 为必填项，缺失时由
QqFlowPlugin.setup 抛 PluginDisabledError 自禁用（见 briefdesk/plugins/qqflow/plugin.py）。
"""

from pydantic import Field
from pydantic_settings import BaseSettings


class QqFlowSettings(BaseSettings):
    api_base: str = "http://127.0.0.1:5032"  # env: QQFLOW_API_BASE
    api_token: str = ""  # env: QQFLOW_API_TOKEN（必填，缺失→禁用源）
    qq: str = ""  # env: QQFLOW_QQ（必填，引导注册 QQ 号，缺失→禁用源）
    key: str = ""  # env: QQFLOW_KEY（必填，引导注册密钥，缺失→禁用源）
    # env: QQFLOW_DB_PATH（可选，允许为空：上游服务端在 db_path 为空时
    # 自动回退平台默认位置，如 Windows: Documents\Tencent Files）
    db_path: str = ""
    sse_reconnect_initial_ms: int = Field(
        default=1000,
        ge=0,  # env: QQFLOW_SSE_RECONNECT_INITIAL_MS
    )
    sse_reconnect_max_ms: int = Field(
        default=60000,
        gt=0,  # env: QQFLOW_SSE_RECONNECT_MAX_MS
    )

    model_config = {
        "env_prefix": "QQFLOW_",  # api_base → QQFLOW_API_BASE
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        # 同一 .env 里还有 app 级与其它源的字段，忽略未知项
        "extra": "ignore",
    }
