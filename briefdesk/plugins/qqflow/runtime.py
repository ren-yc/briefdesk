"""qqflow 消息源装配门面 — 客户端 + SSE 监听 + 历史拉取的唯一入口。

main 通过 `QqFlowSource` 创建消息源：创建客户端、启动实时监听、
关闭释放全部收敛在本类，main 不接触 qqflow 具体类型。
只产出源无关数据，不触碰 DB（写库由应用层完成）。
"""

import logging
import time as time_module

from briefdesk.logger import fmt_dur
from briefdesk.masking import clean_display_name
from briefdesk.plugins.qqflow.client import (
    QqFlowClient,
    QqFlowNotReadyError,
)
from briefdesk.plugins.qqflow.config import QqFlowSettings
from briefdesk.plugins.qqflow.poller import poll
from briefdesk.plugins.qqflow.sse import QqFlowSseClient
from briefdesk.sources_base import (
    BatchHandler,
    ProcessedQuery,
    RealtimeListener,
    SourceRuntime,
)
from briefdesk.types import PollResult, SessionInfo

logger = logging.getLogger(__name__)


class QqFlowSource(SourceRuntime[QqFlowClient]):
    """已装配的 qqflow 消息源，实现 SourceRuntime 协议。"""

    name = "qqflow"

    def __init__(
        self,
        base_url: str | None = None,
        api_token: str | None = None,
    ):
        # 统一实例化一次源专属配置（reconnect 参数注入监听器）
        self._settings = QqFlowSettings()
        # 未显式传入时读取 qqflow 专属配置（QQFLOW_API_BASE / QQFLOW_API_TOKEN）；
        # 密钥在「配置 → 客户端」边界解包为明文 str，客户端不感知 SecretStr
        if base_url is None or api_token is None:
            base_url = base_url or self._settings.api_base
            api_token = api_token or self._settings.api_token.get_secret_value()
        if not api_token:
            logger.warning(
                "QQFLOW_API_TOKEN 为空：请从 qqflow-server 的 token 文件读取 "
                "(Windows: %LOCALAPPDATA%\\qqflow-server\\token.txt)"
            )
        # 具体类型而非 SourceClient：poll/QqFlowSseClient 需要 QqFlowClient 能力；
        # 结构上仍满足 SourceClient 协议，可传给 pipeline/server
        self.client = QqFlowClient(
            base_url=base_url,
            api_token=api_token,
            qq=self._settings.qq,
            key=self._settings.key.get_secret_value(),
            db_path=self._settings.db_path,
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
        """从 qqflow-server 重新拉取会话列表并返回（不写库），应用层负责落库。

        索引期（503）返回空列表：不让单源未就绪炸掉应用层的批量刷新。
        """
        start = time_module.perf_counter()
        try:
            await self.client.ensure_ready()
            sessions = await self.client.fetch_sessions()
        except QqFlowNotReadyError:
            logger.info("qqflow-server 未就绪，会话刷新跳过")
            return []
        if self.listener is not None:
            self.listener.invalidate_session_cache()
        result = [
            SessionInfo(
                source=self.name,
                session_id=s["username"],
                name=clean_display_name(s.get("displayName")) or s["username"],
                is_group=s.get("type") == 2,
                last_active_at=s.get("lastTimestamp", 0),
            )
            # 服务端可能产出空 username 的脏会话（数据缺陷），无 id 的会话无意义
            for s in sessions
            if s.get("username")
        ]
        logger.info(
            "[qqflow] 会话刷新完成: %d 个会话 (%s)",
            len(result),
            fmt_dur(time_module.perf_counter() - start),
        )
        return result

    def start(self, on_batch: BatchHandler) -> None:
        """创建并启动 SSE 实时监听。"""
        logger.info("[qqflow] 启动 SSE 实时监听")
        self.listener = QqFlowSseClient(self.client, on_batch, settings=self._settings)
        self.listener.start()

    async def close(self) -> None:
        """停止监听并关闭客户端（幂等）。"""
        if self.listener is not None:
            self.listener.stop()
            self.listener = None
        await self.client.close()
        logger.info("[qqflow] 已关闭")
