"""weflow 消息源装配门面 — 客户端 + SSE 监听 + 历史拉取的唯一入口。

main 通过 `WeFlowSource` 创建消息源：创建客户端、启动实时监听、
关闭释放全部收敛在本类，main 不接触 weflow 具体类型。
只产出源无关数据，不触碰 DB（写库由应用层完成）。
"""

import logging
import time as time_module

from briefdesk.logger import fmt_dur
from briefdesk.masking import clean_display_name
from briefdesk.plugins.weflow.client import WeFlowClient, WeFlowNotReadyError
from briefdesk.plugins.weflow.config import WeFlowSettings
from briefdesk.plugins.weflow.poller import _session_kind, poll
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
    """已装配的 weflow 消息源，实现 SourceRuntime 协议。"""

    name = "weflow"

    def __init__(
        self,
        base_url: str | None = None,
        api_token: str | None = None,
    ):
        # 统一实例化一次源专属配置（reconnect 参数注入监听器）
        self._settings = WeFlowSettings()
        # 未显式传入时读取 weflow 专属配置（WEFLOW_API_BASE / WEFLOW_API_TOKEN）；
        # 密钥在「配置 → 客户端」边界解包为明文 str，客户端不感知 SecretStr
        if base_url is None or api_token is None:
            base_url = base_url or self._settings.api_base
            api_token = api_token or self._settings.api_token.get_secret_value()
        # 具体类型而非 SourceClient：poll/WeFlowSseClient 需要 WeFlowClient 能力；
        # 结构上仍满足 SourceClient 协议，可传给 pipeline/server
        self.client = WeFlowClient(
            base_url=base_url,
            api_token=api_token,
            wxid=self._settings.wxid,
            db_path=self._settings.db_path,
            db_keys=self._settings.db_keys_map,
            img_aes_key=self._settings.img_aes_key.get_secret_value(),
            img_xor_key=self._settings.img_xor_key.get_secret_value(),
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
        """从 weflow-server 重新拉取会话列表并返回（不写库），应用层负责落库。

        未就绪（503）返回空列表：不让单源未就绪炸掉应用层的批量刷新。
        """
        start = time_module.perf_counter()
        try:
            await self.client.ensure_ready()
            sessions = await self.client.fetch_sessions()
        except WeFlowNotReadyError:
            logger.info("weflow-server 未就绪，会话刷新跳过")
            return []
        if self.listener is not None:
            self.listener.invalidate_session_cache()
        # 会话类型判定复用 poller._session_kind：sessionType 权威 + 数字 type
        # 兜底，避免两条路径（轮询发现 / 手动刷新）判定口径漂移
        result = [
            SessionInfo(
                source=self.name,
                session_id=s["username"],
                name=clean_display_name(s.get("displayName")) or s["username"],
                is_group=_session_kind(s)[0],
                is_official=_session_kind(s)[1],
                last_active_at=s.get("lastTimestamp", 0),
            )
            # 服务端可能产出空 username 的脏会话（数据缺陷），无 id 的会话无意义
            for s in sessions
            if s.get("username")
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
        """停止监听并关闭客户端（幂等）。

        顺序：stop（取消连接/统计任务并启动收尾冲刷）→ aclose（等待缓冲
        残余消息冲刷与 in-flight 批处理完成）→ 关 HTTP 客户端。aclose 先于
        client.close()，消除关停竞态：in-flight 批内的图片回查/媒体下载
        不再撞上已关闭的客户端。
        """
        if self.listener is not None:
            self.listener.stop()
            # RealtimeListener 协议只约束同步 stop()；aclose 是本仓库监听器
            # 的扩展收尾钩子，经 getattr 防御调用以保持对协议鸭子实现兼容
            # （缺失时退回旧语义：冲刷任务在后台自行完成）
            drain = getattr(self.listener, "aclose", None)
            if drain is not None:
                await drain()
            self.listener = None
        await self.client.close()
        logger.info("[weflow] 已关闭")
