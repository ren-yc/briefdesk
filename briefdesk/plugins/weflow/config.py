"""WeFlow 消息源专属配置 — 从 .env 读取 WEFLOW_* 环境变量。

与 app 级配置(briefdesk/config.py)分离:只有启用 weflow 源时才被加载。
环境变量名保持 WEFLOW_API_BASE / WEFLOW_API_TOKEN 不变,.env 无需修改。
"""

from pydantic import Field
from pydantic_settings import BaseSettings


class WeFlowSettings(BaseSettings):
    api_base: str = "http://127.0.0.1:5031"
    api_token: str = ""
    sse_reconnect_initial_ms: int = Field(
        default=1000,
        ge=0,  # env: WEFLOW_SSE_RECONNECT_INITIAL_MS
    )
    sse_reconnect_max_ms: int = Field(
        default=60000,
        gt=0,  # env: WEFLOW_SSE_RECONNECT_MAX_MS
    )

    model_config = {
        "env_prefix": "WEFLOW_",  # api_base → WEFLOW_API_BASE
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        # 同一 .env 里还有 app 级与其它源的字段，忽略未知项
        "extra": "ignore",
    }
