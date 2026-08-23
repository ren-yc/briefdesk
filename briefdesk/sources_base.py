"""消息源抽象（核心契约模块，P2 起无 sources 包）— 客户端能力契约、监听器契约与已装配源单元。

`SourceClient` 是 pipeline / server 依赖的最小客户端契约：新消息源
实现本协议（name、connection_status、download_media、close）即可被
系统消费。`RealtimeListener` 是实时监听器的生命周期契约：插件包内的
监听器实现本协议，server 只依赖协议不依赖具体源。
`SourceRuntime` 是已装配消息源单元：main 通过它编排启动/关闭与轮询
同步——**新增消息源 = 实现 SourceRuntime 并以插件发布**（briefdesk/plugins/*，
entry point 组 briefdesk.plugins，启用走 PLUGINS/PLUGINS_DISABLED）。
轮询拉取等源特有的控制流留在各插件包内。
"""

from collections.abc import Callable, Coroutine
from typing import Any, Literal, Protocol

from briefdesk.types import InternalMessage, PollResult, SessionInfo

# 连接状态取值(消息源内部维护,如实时连接建立/断开时更新)
type ConnectionStatus = Literal["online", "reconnecting", "offline"]

# 实时批回调(插件包内的实时监听器把攒批后的消息交给应用层处理)
type BatchHandler = Callable[[list[InternalMessage]], Coroutine[Any, Any, None]]

# 应用层提供的已处理查询端口:输入候选 msg_ids,返回其中已处理的子集
type ProcessedQuery = Callable[[list[str]], Coroutine[Any, Any, set[str]]]


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
