"""WeFlow 消息源装配门面 — 客户端 + SSE 监听 + 历史拉取的唯一入口。

main 通过 `WeFlowSource` 创建消息源：创建客户端、启动实时监听、
关闭释放全部收敛在本类，main 不接触 weflow 具体类型。
只产出源无关数据，不触碰 DB（写库由应用层完成）。
"""

import logging
import time as time_module

from briefdesk.logger import fmt_dur
from briefdesk.masking import clean_display_name
from briefdesk.plugins.weflow.client import WeFlowClient, is_official_session
from briefdesk.plugins.weflow.config import WeFlowSettings
from briefdesk.plugins.weflow.poller import poll
from briefdesk.plugins.weflow.sse import WeFlowSseClient
from briefdesk.sources_base import (
    BatchHandler,
    ProcessedQuery,
    RealtimeListener,
    SourceRuntime,
)
from briefdesk.types import PollResult, SessionInfo

logger = logging.getLogger(__name__)


class WeFlowSource(SourceRuntime[WeFlowClient]):
    """已装配的 WeFlow 消息源，实现 SourceRuntime 协议。"""

    name = "weflow"

    def __init__(
        self,
        base_url: str | None = None,
        api_token: str | None = None,
    ):
        # 统一实例化一次源专属配置（reconnect 参数注入监听器）
        self._settings = WeFlowSettings()
        # 未显式传入时读取 weflow 专属配置（WEFLOW_API_BASE / WEFLOW_API_TOKEN）
        if base_url is None or api_token is None:
            base_url = base_url or self._settings.api_base
            api_token = api_token or self._settings.api_token
        # 具体类型而非 SourceClient：poll/WeFlowSseClient 需要 WeFlowClient 能力；
        # 结构上仍满足 SourceClient 协议，可传给 pipeline/server
        self.client = WeFlowClient(
            base_url=base_url,
            api_token=api_token,
        )
        self.listener: RealtimeListener | None = None

    async def fetch_history(
        self,
        enabled_sessions: list[SessionInfo],
        is_processed: ProcessedQuery,
        *,
        window_start_by_session: dict[str, int | None] | None = None,
    ) -> PollResult:
        """REST 历史回填（按会话增量窗口；无水位会话按 BACKFILL_HOURS；-1 = 全量）。"""
        return await poll(
            self.client,
            enabled_sessions,
            is_processed,
            window_start_by_session=window_start_by_session,
        )

    async def refresh_sessions(self) -> list[SessionInfo]:
        """从 WeFlow 重新拉取会话列表并返回（不写库），应用层负责落库。"""
        start = time_module.perf_counter()
        sessions = await self.client.fetch_sessions()
        if self.listener is not None:
            self.listener.invalidate_session_cache()
        result = [
            SessionInfo(
                source=self.name,
                session_id=s["id"],
                name=clean_display_name(s.get("name")) or s["id"],
                is_group=(s.get("type") == "group"),
                is_official=is_official_session(s),
                last_active_at=s.get("lastMessageAt", 0),
            )
            for s in sessions
        ]
        logger.info(
            "[weflow] 会话刷新完成: %d 个会话 (%s)",
            len(result),
            fmt_dur(time_module.perf_counter() - start),
        )
        return result

    def start(self, on_batch: BatchHandler) -> None:
        """创建并启动 SSE 实时监听。"""
        logger.info("[weflow] 启动 SSE 实时监听")
        self.listener = WeFlowSseClient(self.client, on_batch, settings=self._settings)
        self.listener.start()

    async def close(self) -> None:
        """停止监听并关闭客户端（幂等）。"""
        if self.listener is not None:
            self.listener.stop()
            self.listener = None
        await self.client.close()
        logger.info("[weflow] 已关闭")
