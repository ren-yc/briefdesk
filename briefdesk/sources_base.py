"""消息源抽象（核心契约模块）— 客户端能力契约、监听器契约与已装配源单元。

`SourceClient` 是 pipeline / server 依赖的最小客户端契约：新消息源
实现本协议（name、connection_status、download_media、close）即可被
系统消费。`RealtimeListener` 是实时监听器的生命周期契约：插件包内的
监听器实现本协议，server 只依赖协议不依赖具体源。
`SourceRuntime` 是已装配消息源单元：main 通过它编排启动/关闭与轮询
同步——**新增消息源 = 实现 SourceRuntime 并以插件发布**（briefdesk/plugins/*，
entry point 组 briefdesk.plugins，启用走 PLUGINS/PLUGINS_DISABLED）。
轮询拉取等源特有的控制流留在各插件包内。
"""

import asyncio
import logging
import time as time_module
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any, Literal, Protocol

import httpx

from briefdesk.config import config
from briefdesk.logger import fmt_dur
from briefdesk.types import InternalMessage, PollResult, SessionInfo

logger = logging.getLogger(__name__)

# 连接状态取值(消息源内部维护,如实时连接建立/断开时更新)
type ConnectionStatus = Literal["online", "reconnecting", "offline"]

# 实时批回调(插件包内的实时监听器把攒批后的消息交给应用层处理)
type BatchHandler = Callable[[list[InternalMessage]], Coroutine[Any, Any, None]]

# 应用层提供的已处理查询端口:输入候选 msg_ids,返回其中已处理的子集
type ProcessedQuery = Callable[[list[str]], Coroutine[Any, Any, set[str]]]

async def with_connect_retry[T](
    request_fn: Callable[[], Awaitable[T]],
    *,
    attempts: int = 3,
    base_delay: float = 0.5,
) -> T:
    """对连接类失败（httpx.ConnectError / ConnectTimeout）做短退避重试。

    目的：覆盖上游（qqflow-server / WeFlow）TCP 暂未监听的启动竞态与
    运行期瞬断——SSE 有自带退避重连，REST 侧此前零重试，一次拒连即
    冒泡到 poll_cycle 写 lastError。重试只在连接阶段失败时发生：
    - 不重试 HTTP 状态错误（4xx/5xx 由调用方语义处理；
      qqflow 503 就绪门控走 QqFlowNotReadyError，语义保持不变）；
    - 不用于 SSE 流（stream_events 已有监听器级退避重连）；
    - 每次重试新建请求调用（request_fn 无参工厂），天然重放。
    耗尽后原样上抛最后一次异常（错误对象与堆栈保留，
    poll_cycle 的 lastError 行为不变）。
    """
    if attempts < 1:
        raise ValueError("attempts must be >= 1")
    delay = base_delay
    last_error: httpx.TransportError | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await request_fn()
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            last_error = e
            if attempt < attempts:
                logger.debug(
                    "连接失败（第 %d/%d 次），%.1fs 后重试: %s",
                    attempt,
                    attempts,
                    delay,
                    e,
                )
                await asyncio.sleep(delay)
                delay *= 2
    # 耗尽（attempts >= 1 保证循环至少执行一次）：上抛最后一次连接异常
    assert last_error is not None
    raise last_error


def make_sse_timeout(read_timeout_s: float) -> httpx.Timeout:
    """SSE 长连接超时：连接 10s、读超时可配置、写/连接池不限。

    ReadTimeout 是 httpx.RequestError 的子类：会被 stream_events 的既有
    except 捕获置 offline 并正常结束生成器，由监听器的退避重连循环接管
    ——半开连接自愈的关键路径。write=None/pool=None 对一次性单请求的
    SSE 专用客户端无争用面。
    """
    return httpx.Timeout(
        connect=10.0,
        read=read_timeout_s,
        write=None,
        pool=None,
    )


class BatchBuffer:
    """按数量或超时批量刷新消息的缓冲区。

    跨源共享（weflow-legacy/qqflow 监听器共用），勿在各插件包另立副本。
    """

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


class DrainableListenerMixin:
    """stop() 后冲刷批缓冲的收尾钩子（配合共享 BatchBuffer 使用）。

    RealtimeListener 协议的 stop 为同步方法：冲刷以受跟踪的后台任务执行，
    监听器实现方在 stop() 末尾调用 _start_final_drain()；aclose() 可等待其
    完成（SourceRuntime.close 经 getattr 探测调用）。幂等。
    """

    _batch_buffer: BatchBuffer
    _drain_task: asyncio.Task | None = None

    def _start_final_drain(self) -> None:
        if self._drain_task is None:
            self._drain_task = asyncio.create_task(self._final_drain())

    async def _final_drain(self) -> None:
        try:
            await self._batch_buffer.flush()
        except Exception:
            logger.exception("停止冲刷批缓冲失败（未标记 processed 的消息由回填恢复）")

    async def aclose(self) -> None:
        if self._drain_task is not None:
            await asyncio.gather(self._drain_task, return_exceptions=True)


class SourceError(Exception):
    """消息源侧错误基类。"""


class MediaError(SourceError):
    """媒体下载失败(网络错误或源侧返回非成功状态),保留原因为 cause。"""


class SourceClient(Protocol):
    """客户端能力契约 — 新消息源实现本协议即可被 pipeline/server 消费。"""

    name: str  # 源标识,用作状态键、日志前缀、DB 命名空间
    connection_status: ConnectionStatus

    async def download_media(self, path: str) -> bytes:
        """下载媒体文件原始字节(带鉴权)。path 为该源自身约定的媒体路径。

        Raises:
            MediaError: 网络错误或源侧返回非成功状态导致下载失败。
        """
        ...

    async def close(self) -> None:
        """释放底层资源。"""
        ...


class RealtimeListener[S: SourceClient](Protocol):
    """实时消息监听器契约 — 插件包内的监听器实现。

    仅约束生命周期与会话缓存刷新,事件解析等源特有细节留在实现内部。

    实现可另提供 `async aclose()`：等待关停冲刷（残余缓冲 + in-flight 批任务）收尾；SourceRuntime.close 经 getattr 探测调用，缺失则跳过（可选扩展钩子，不入协议方法集）。
    """

    def start(self) -> None:
        """启动后台监听任务(可重入,重复调用无副作用)。"""
        ...

    def stop(self) -> None:
        """停止监听并取消后台任务。"""
        ...

    def invalidate_session_cache(self) -> None:
        """强制刷新已启用会话缓存(如切换会话启用状态后调用)。"""
        ...


class SourceRuntime[S: SourceClient](Protocol):
    """已装配的消息源单元 — 客户端 + 实时监听 + 历史拉取。

    main 只依赖本协议编排启动/关闭；更换消息源只需替换插件实现，
    后续接线全部走协议方法。
    """

    name: str
    client: S
    # 监听器类型与客户端类型绑定(RealtimeListener 的泛型参数 S 未在
    # 成员中使用,仅表达"监听该客户端类型"的关联)
    listener: RealtimeListener[S] | None

    async def fetch_history(
        self,
        enabled_sessions: list[SessionInfo],
        is_processed: ProcessedQuery,
        *,
        window_start_by_session: dict[str, int | None] | None = None,
    ) -> PollResult:
        """拉取一次历史消息(源特有的回填逻辑,结果源无关)。

        enabled_sessions 由应用层从启用会话表查询后传入;
        is_processed 为应用层提供的已处理查询(源不直接访问 DB),
        用于在返回前剔除已处理消息。
        window_start_by_session 为应用层按会话计算的增量窗口下界
        (session_id → 秒级时间戳,含边界):本会话只拉 [下界, now]。
        值为 None 的会话按 BACKFILL_HOURS 回退(无水位/重新启用,启用即回填);
        整参 None 时所有会话按 BACKFILL_HOURS 回退(-1 = 拉取全部历史)。
        """
        ...

    async def refresh_sessions(self) -> list[SessionInfo]:
        """从消息源重新拉取会话列表并返回(不写库),应用层负责落库。

        含监听器缓存失效(如有);无会话概念的源返回空列表。
        """
        ...

    def start(self, on_batch: BatchHandler) -> None:
        """创建并启动实时监听;on_batch 由应用层提供(如 pipeline 处理)。"""
        ...

    async def close(self) -> None:
        """停止监听并释放客户端资源(幂等)。"""
        ...
