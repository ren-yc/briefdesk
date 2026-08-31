"""应用运行时状态 — 状态字典、消息源注册表与状态聚合查询。

业务层（pipeline/poll_cycle）与 HTTP 层（server）都只依赖本模块，
避免业务层反向依赖表现层；源客户端与监听器注册表也收在这里
（组合根 main 注入、server 查询）。

相对时间展示在前端计算：卡片行 relativeTime 与状态面板的 relativeSync
均由前端按 msg_time/lastSync 自行换算，本模块只下发原始时间数据
（items 的 msg_time/created_at、lastSync）。
"""

from datetime import UTC, datetime, timedelta
from typing import TypedDict

from briefdesk.announcements import get_announcements
from briefdesk.sources_base import RealtimeListener, SourceClient


class AppStatus(TypedDict):
    """共享应用状态，`_app_status` 的键类型。"""

    lastSync: str
    lastError: str
    syncing: bool
    lastWarning: str  # 非致命状态提示（如管道阶段缺失/无启用类别），前端状态面板展示


class PartialAppStatus(TypedDict, total=False):
    """仅用于 `set_status` 的部分更新。

    不能通过继承 AppStatus 实现：`total` 不作用于继承的键，且
    mypy/Pylance 均禁止子类把父类 Required 键改为 NotRequired，
    故此处独立声明（字段与 AppStatus 保持一致）。
    """

    lastSync: str
    lastError: str
    syncing: bool
    lastWarning: str


_app_status: AppStatus = {
    "lastSync": "",
    "lastError": "",
    "syncing": False,
    "lastWarning": "",
}

_listeners: dict[str, RealtimeListener] = {}
_source_clients: dict[str, SourceClient] = {}


# ── 消息同步进度（新增消息数）──
#
# 只统计"进入处理管道的新消息"（原始消息条数，非去重/合并后的卡片数），
# 覆盖增量查询与 SSE 推送两条路径——两者都汇入 pipeline 入口/出口埋点：
#   入口 note_sync_batch_start：计入本次突发累计（new_count）与进行中（pending）
#   出口 note_sync_batch_done  ：每批完成递减 pending、累加 processed
# 突发边界：pending 由 0 变非 0 开启新突发（重置 new/processed），归 0 时置
# done 标志，供前端展示"✓ 已同步 N 条新消息"后短暂淡出。
#
# 并发安全：单事件循环内本模块各计数函数为同步调用（内部无 await），
# 读-改-写天然原子、不会与其他协程交错；pipeline 在 await 边界之间只做
# 单一函数调用，无需额外加锁。


class SyncProgress(TypedDict):
    """消息同步进度快照（/api/status 与 sync_progress SSE 事件共用）。"""

    startedAt: str  # 本次突发开始时间（ISO）；无突发时为空串
    newCount: int  # 本次突发累计新增消息数（原始消息，含处理中）
    pendingCount: int  # 仍在处理中（含刚进入管道的实时消息）
    processedCount: int  # 已达终态（入库/判重/闲聊/失败）
    done: bool  # pending 归 0，突发收尾


_sync_progress: SyncProgress = {
    "startedAt": "",
    "newCount": 0,
    "pendingCount": 0,
    "processedCount": 0,
    "done": False,
}


def get_sync_progress() -> dict:
    """同步进度快照（返回拷贝，避免调用方改动内部状态）。"""
    return dict(_sync_progress)


def note_sync_batch_start(count: int) -> dict:
    """管道入口：新增 count 条消息进入处理（含处理中的实时消息）。

    上一突发完成后再次有消息到达时开启新突发：重置累计计数字段。
    新突发的 startedAt 保证严格递增，避免同一微秒内连续调用导致
    时间戳相等而触发用例 flaky。
    """
    if _sync_progress["pendingCount"] == 0:
        prev = _sync_progress["startedAt"]
        now = datetime.now(UTC)
        if prev:
            try:
                prev_dt = datetime.fromisoformat(prev)
                if now <= prev_dt:
                    now = prev_dt + timedelta(microseconds=1)
            except ValueError:
                if now.isoformat() == prev:
                    now = now + timedelta(microseconds=1)
        _sync_progress["startedAt"] = now.isoformat()
        _sync_progress["newCount"] = 0
        _sync_progress["processedCount"] = 0
        _sync_progress["done"] = False
    _sync_progress["newCount"] += count
    _sync_progress["pendingCount"] += count
    return dict(_sync_progress)


def note_sync_batch_done(count: int) -> dict:
    """管道出口：count 条消息完成处理（入库/判重/闲聊/失败均属终态）。

    pending 归 0 时置 done 标志，供前端展示"已同步"后自动淡出。
    """
    _sync_progress["pendingCount"] = max(
        0, _sync_progress["pendingCount"] - count
    )
    _sync_progress["processedCount"] += count
    if _sync_progress["pendingCount"] == 0:
        _sync_progress["done"] = True
    return dict(_sync_progress)


def reset_sync_progress() -> None:
    """清空同步进度（测试隔离用）。"""
    _sync_progress["startedAt"] = ""
    _sync_progress["newCount"] = 0
    _sync_progress["pendingCount"] = 0
    _sync_progress["processedCount"] = 0
    _sync_progress["done"] = False


def set_listener(name: str, listener: RealtimeListener) -> None:
    _listeners[name] = listener


def get_listener(name: str) -> RealtimeListener | None:
    return _listeners.get(name)


def register_source_client(name: str, client: SourceClient) -> None:
    _source_clients[name] = client


def get_source_client(name: str) -> SourceClient | None:
    return _source_clients.get(name)


def set_status(update: PartialAppStatus) -> None:
    _app_status.update(update)


def is_syncing() -> bool:
    return _app_status["syncing"]


def get_status_info() -> dict:
    last = _app_status["lastSync"]
    # 实时连接状态由各源客户端内部维护（如实时连接建立/断开时更新），
    # 这里按源名聚合透传，_app_status 不存连接状态

    return {
        "sources": {
            name: {"clientName": name, "status": client.connection_status}
            for name, client in _source_clients.items()
        },
        "lastSync": last,
        "lastError": _app_status["lastError"] or None,
        "lastWarning": _app_status["lastWarning"] or None,
        "syncing": _app_status["syncing"],
        "syncProgress": get_sync_progress(),
        "announcements": get_announcements(),
    }
