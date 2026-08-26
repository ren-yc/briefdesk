"""SSE 客户端 — 通过 QqFlowClient 实时监听消息流（参照 weflow sse.py）。

只负责监听与攒批，不触碰 DB：启用会话过滤、已处理过滤与 raw 落库
均由应用层（pipeline 入口）统一完成。
"""

import asyncio
import logging
import math
import random
import time as time_module
from collections import deque

from briefdesk.config import config
from briefdesk.logger import fmt_dur
from briefdesk.plugins.qqflow.client import QqFlowClient, QqFlowEvent
from briefdesk.plugins.qqflow.config import QqFlowSettings
from briefdesk.plugins.qqflow.normalize import (
    is_self_message,
    normalize_sse,
    pre_filter_sse,
)
from briefdesk.sources_base import BatchHandler, RealtimeListener
from briefdesk.types import InternalMessage

logger = logging.getLogger(__name__)

# 监听器内近期事件去重缓存上限（按 event + rawid），
# 防断线重连/基线重放导致重复触发 AI 分类（省 token）
_SEEN_LIMIT = 1024

# 周期统计上报间隔（秒）
_STATS_INTERVAL_SECONDS = 60


class BatchBuffer:
    """按数量或超时批量刷新消息的缓冲区。"""

    def __init__(self, on_flush: BatchHandler):
        self._buffer: list[InternalMessage] = []
        self._on_flush = on_flush
        self._timer: asyncio.Task | None = None
        self._started_at: float | None = None
        # in-flight 批处理任务跟踪：stop 后等待其收尾，避免关停竞态丢批
        self._inflight: set[asyncio.Task] = set()

    def add(self, msg: InternalMessage) -> None:
        if not self._buffer:
            self._started_at = time_module.perf_counter()
        self._buffer.append(msg)
        if len(self._buffer) >= config.realtime_batch_max_count:
            self._schedule_flush()
        elif self._timer is None:
            self._timer = asyncio.create_task(self._delayed_flush())

    async def _delayed_flush(self) -> None:
        await asyncio.sleep(config.realtime_batch_timeout_ms / 1000)
        self._schedule_flush()

    def _schedule_flush(self) -> None:
        if self._timer:
            self._timer.cancel()
            self._timer = None
        if self._buffer:
            batch = self._buffer[:]
            self._buffer.clear()
            self._log_flush(batch)
            task = asyncio.create_task(self._safe_flush(batch))
            self._inflight.add(task)
            task.add_done_callback(self._inflight.discard)

    async def _safe_flush(self, batch: list[InternalMessage]) -> None:
        """fire-and-forget 安全包装：批处理异常记日志，不产生
        "Task exception was never retrieved"（未标记 processed 的消息
        由回填窗口内重试恢复，不永久丢失）。"""
        try:
            await self._on_flush(batch)
        except Exception:
            logger.exception("批处理失败（%d 条，下轮回填重试）", len(batch))

    async def flush(self) -> None:
        """冲刷缓冲区残余消息并等待 in-flight 批处理收尾（stop 后由 drain 任务调用）。"""
        if self._timer:
            self._timer.cancel()
            self._timer = None
        if self._buffer:
            batch = self._buffer[:]
            self._buffer.clear()
            self._log_flush(batch)
            await self._on_flush(batch)
        pending = [t for t in self._inflight if not t.done()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    def _log_flush(self, batch: list[InternalMessage]) -> None:
        wait = time_module.perf_counter() - (self._started_at or time_module.perf_counter())
        self._started_at = None
        logger.debug("批刷新: %d 条 (攒批 %s)", len(batch), fmt_dur(wait))


class QqFlowSseClient(RealtimeListener[QqFlowClient]):
    """SSE 实时消息监听器，自动重连。实现 RealtimeListener 契约。"""

    def __init__(
        self,
        qqflow: QqFlowClient,
        on_batch: BatchHandler,
        *,
        settings: QqFlowSettings,
    ):
        self._qqflow = qqflow
        self._on_batch = on_batch
        self._settings = settings
        self._running = False
        self._reconnect_attempt = 0
        self._batch_buffer = BatchBuffer(self._on_batch)
        self._task: asyncio.Task | None = None
        self._seen: set[tuple[str, str]] = set()
        self._seen_order: deque[tuple[str, str]] = deque()
        # 监听统计（周期/停止时上报）：事件总数、预过滤丢弃数、去重命中数、
        # IGNORE_SELF 回查判定的自消息数；sync/ping 控制事件不进统计
        # （保持「无消息静默」语义）
        self._stats_events = 0
        self._stats_filtered = 0
        self._stats_deduped = 0
        self._stats_self = 0
        self._stats_task: asyncio.Task | None = None
        # stop() 启动的收尾冲刷任务（协议要求 stop 为同步方法，故后台执行）
        self._drain_task: asyncio.Task | None = None

    def invalidate_session_cache(self) -> None:
        # 启用会话过滤已收敛到 pipeline 入口（每批实时查询），无缓存可失效；
        # 保留协议成员以兼容 server 刷新端点与 runtime.refresh_sessions 的调用。
        pass

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._connect_loop())
        self._stats_task = asyncio.create_task(self._stats_loop())

    def stop(self) -> None:
        self._running = False
        self._log_stats()
        if self._stats_task:
            self._stats_task.cancel()
        if self._task:
            self._task.cancel()
        # 冲刷残余缓冲并等待 in-flight 批收尾；RealtimeListener 协议的 stop
        # 为同步方法，故以后台任务执行，aclose() 供 runtime 在关客户端前等待
        if self._drain_task is None:
            self._drain_task = asyncio.create_task(self._final_drain())

    async def _final_drain(self) -> None:
        try:
            await self._batch_buffer.flush()
        except Exception:
            logger.exception("停止冲刷批缓冲失败（未标记 processed 的消息由回填恢复）")

    async def aclose(self) -> None:
        """等待 stop() 启动的收尾冲刷完成（幂等；runtime 关客户端前调用可消除竞态）。"""
        if self._drain_task is not None:
            await asyncio.gather(self._drain_task, return_exceptions=True)

    async def _stats_loop(self) -> None:
        """周期上报监听统计（INFO，默认 60s 一次，无事件时静默）。"""
        while self._running:
            await asyncio.sleep(_STATS_INTERVAL_SECONDS)
            self._log_stats()

    def _log_stats(self) -> None:
        if (
            self._stats_events == 0
            and self._stats_filtered == 0
            and self._stats_deduped == 0
            and self._stats_self == 0
        ):
            return
        logger.info(
            "SSE 统计: 事件 %d, 预过滤丢弃 %d, 去重命中 %d, 自消息 %d",
            self._stats_events,
            self._stats_filtered,
            self._stats_deduped,
            self._stats_self,
        )
        self._stats_events = 0
        self._stats_filtered = 0
        self._stats_deduped = 0
        self._stats_self = 0

    async def _connect_loop(self) -> None:
        while self._running:
            try:
                await self._listen()
            except asyncio.CancelledError:
                break

            if not self._running:
                break

            delay = min(
                self._settings.sse_reconnect_initial_ms * (2**self._reconnect_attempt),
                self._settings.sse_reconnect_max_ms,
            )
            jitter = delay * (0.75 + 0.5 * random.random())
            self._reconnect_attempt += 1
            logger.info(
                "SSE 连接中断，%.0fms 后重连 (第 %d 次)",
                math.ceil(jitter),
                self._reconnect_attempt,
            )
            await asyncio.sleep(jitter / 1000)

    async def _listen(self) -> None:
        logger.info(f"连接中... (第 {self._reconnect_attempt + 1} 次)")

        async for event in self._qqflow.stream_events():
            if not self._running:
                break
            self._reconnect_attempt = 0
            await self._handle_event(event)

    async def _handle_event(self, event: QqFlowEvent) -> None:
        # 控制事件不计入消息事件/预过滤统计（无消息静默语义），亦无需计数：
        # sync（水位对齐）/ping（KeepAlive）直接跳过，否则每 15s 心跳会
        # 让「无消息静默」统计失效
        etype = event.get("event", "")
        if etype in ("sync", "ping"):
            return
        self._stats_events += 1
        if not pre_filter_sse(event):
            self._stats_filtered += 1
            return
        # 断线重连/基线重放可能重复投递同一事件，按 event + rawid 去重
        key = (event.get("event", ""), event.get("rawid", ""))
        if key in self._seen:
            self._stats_deduped += 1
            return
        self._seen.add(key)
        self._seen_order.append(key)
        if len(self._seen_order) > _SEEN_LIMIT:
            self._seen.discard(self._seen_order.popleft())
        try:
            msg = normalize_sse(event)
        except Exception as e:  # noqa: BLE001 — 单条失败不应拖垮监听循环
            logger.warning(
                "SSE 事件 normalize 失败 (rawid=%s): %s", event.get("rawid"), e
            )
            return
        # IGNORE_SELF：SSE 事件无发送者标识，按消息回查 REST 判定（仅开启时，
        # 每消息 +1 次本机 HTTP；回查失败/未命中 fail-open 放行不拖垮监听）
        if config.ignore_self and not msg.is_self:
            try:
                raw = await self._qqflow.lookup_message(
                    msg.session_id, msg.msg_id, msg.timestamp
                )
            except Exception as e:  # noqa: BLE001 — 回查失败不拖垮监听循环
                logger.warning(
                    "SSE rawid=%s: 自消息回查失败，按非自己放行: %s", msg.msg_id, e
                )
            else:
                if raw is not None:
                    msg.is_self = is_self_message(raw, self._qqflow.self_uid)
                else:
                    logger.debug(
                        "SSE rawid=%s: 自消息回查未命中，按非自己放行", msg.msg_id
                    )
        if msg.is_self:
            # 自己发送：监听器层直接丢弃（不标记 processed，关闭 IGNORE_SELF
            # 后经回填/重放可恢复），避免管道逐条 INFO 刷屏
            self._stats_self += 1
            logger.debug("SSE rawid=%s: 自己发送的消息，丢弃", msg.msg_id)
            return
        self._batch_buffer.add(msg)
