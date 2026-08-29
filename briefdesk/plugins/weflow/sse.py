"""SSE 客户端 — 通过 WeFlowClient 实时监听消息流（weflow-server :5033）。

只负责监听与攒批，不触碰 DB：启用会话过滤、已处理过滤与 raw 落库
均由应用层（pipeline 入口）统一完成。
"""

import asyncio
import logging
import math
import random
from collections import deque

from briefdesk.plugins.weflow.client import WeFlowClient, WeFlowEvent
from briefdesk.plugins.weflow.config import WeFlowSettings
from briefdesk.plugins.weflow.normalize import normalize_sse, pre_filter_sse
from briefdesk.sources_base import (
    BatchBuffer,
    BatchHandler,
    DrainableListenerMixin,
    RealtimeListener,
)

logger = logging.getLogger(__name__)

# 近期事件去重缓存上限（按 event+rawid，FIFO）：防断线重连/上游重复投递导致
# 同一事件重复进管道（weflow-server 文档明确建议接收端按 event+rawid 去重；
# 与 qqflow 监听器一致）。被去重挡下的消息未标记 processed，回填窗口内可恢复
_SEEN_LIMIT = 1024

# 周期统计上报间隔（秒）
_STATS_INTERVAL_SECONDS = 60


class WeFlowSseClient(DrainableListenerMixin, RealtimeListener[WeFlowClient]):
    """SSE 实时消息监听器，自动重连。实现 RealtimeListener 契约。"""

    def __init__(
        self,
        weflow: WeFlowClient,
        on_batch: BatchHandler,
        *,
        settings: WeFlowSettings,
    ):
        self._weflow = weflow
        self._on_batch = on_batch
        self._settings = settings
        self._running = False
        self._reconnect_attempt = 0
        self._batch_buffer = BatchBuffer(self._on_batch)
        self._task: asyncio.Task | None = None
        # 近期事件去重缓存（event, rawid)，FIFO 有界（见模块级 _SEEN_LIMIT）
        self._seen: set[tuple[str, str]] = set()
        self._seen_order: deque[tuple[str, str]] = deque()
        # 监听统计（周期/停止时上报）：事件总数、预过滤丢弃数、去重命中数
        self._stats_events = 0
        self._stats_filtered = 0
        self._stats_deduped = 0
        self._stats_task: asyncio.Task | None = None
        # stop() 启动的收尾冲刷任务（协议要求 stop 为同步方法，故后台执行；
        # DrainableListenerMixin 提供，此处显式置 None 以便类型检查）
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
        # 为同步方法，故经共享 mixin 以后台任务执行，aclose() 供 runtime 等待
        self._start_final_drain()

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
        ):
            return
        logger.info(
            "SSE 统计: 事件 %d, 预过滤丢弃 %d, 去重命中 %d",
            self._stats_events,
            self._stats_filtered,
            self._stats_deduped,
        )
        self._stats_events = 0
        self._stats_filtered = 0
        self._stats_deduped = 0

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

        async for event in self._weflow.stream_events():
            if not self._running:
                break
            self._reconnect_attempt = 0
            await self._handle_event(event)

    async def _handle_event(self, event: WeFlowEvent) -> None:
        # 控制事件不计入消息事件/预过滤统计（无消息静默语义）：
        # ready（连接基线，载荷 {"status":"ok"} 无 event 键）与 sync（水位基线/
        # 重基）都不是消息，计进统计会让「预过滤丢弃」虚增——上游无就绪门控后
        # 冷启动即连上，这两类帧在每次重连时都会出现。与 qqflow 监听器一致。
        #
        # ping 当前不可达，列在这里是为对称与前瞻：上游保活是
        # `KeepAlive::new().interval(25s).text("ping")`，线上字节为注释行
        # `:ping`，而 stream_events 只解析 `data: ` 行，故心跳永不成为事件。
        # 若上游哪天把保活改成 data 帧，缺这一项会让每 25s 一次的心跳同时
        # 虚增「事件」与「预过滤丢弃」（+144/小时），"无消息即静默"的统计
        # 行随之常亮——那才是这个统计最有用的性质。
        etype = event.get("event", "")
        if etype in ("ready", "sync", "ping") or not etype:
            return
        self._stats_events += 1
        if not pre_filter_sse(event):
            self._stats_filtered += 1
            return
        # 按 event+rawid 去重（上游文档建议；防断线重连重放/重复投递）
        key = (event.get("event", ""), event.get("rawid", ""))
        if key in self._seen:
            self._stats_deduped += 1
            return
        self._seen.add(key)
        self._seen_order.append(key)
        if len(self._seen_order) > _SEEN_LIMIT:
            self._seen.discard(self._seen_order.popleft())
        try:
            msgs = await normalize_sse(event, self._weflow)
        except Exception as e:  # noqa: BLE001 — 单条失败不应拖垮监听循环
            logger.warning(
                "SSE 事件 normalize 失败 (rawid=%s): %s", event.get("rawid"), e
            )
            return
        for msg in msgs:  # 文章卡片会拆为多条
            self._batch_buffer.add(msg)
