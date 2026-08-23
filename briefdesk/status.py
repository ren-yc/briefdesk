"""应用运行时状态 — 状态字典、消息源注册表与状态聚合查询。

从 server.py 独立出来:业务层(pipeline/poll_cycle)与 HTTP 层(server)
都只依赖本模块,避免业务层反向依赖表现层。源客户端与监听器注册表
也收在这里(组合根 main 注入、server 查询),server 不再兼任
"应用状态持有者"。

相对时间展示已整体移到前端（P5）：卡片行 relativeTime 与状态面板的
relativeSync 均由前端按 msg_time/lastSync 自行计算，本模块只下发原始
时间数据（items 的 msg_time/created_at、lastSync）。
"""

from typing import TypedDict

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
    }
