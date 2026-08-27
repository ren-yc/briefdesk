"""应用级公告 — 持续性条件的顶部横幅通知（如嵌入服务未启用/不可用）。

与 status.lastWarning（管道成功产出即清空的瞬态提示）互补：公告表达
"当前持续存在的条件"，由发现方置位、条件解除方撤销，撤销前常驻。
下发两条路径：/api/status 的 announcements 字段（前端轮询兜底）与
announcements_updated SSE 事件（增删时即时推送）；仅状态变化时发布，
失败重试不刷屏。单条公告可被前端关闭（仅当次会话，刷新后条件仍在则重现）。

并发模型与 status.py 同约定：单事件循环内同步读写注册表（无锁），
await 仅用于发布 SSE；模块级注册表即进程内唯一实例。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from briefdesk.realtime import publish_announcements_updated

logger = logging.getLogger(__name__)

# code → {code, level, message, since}（值均为 str，便于直接 JSON 下发）
_ANNOUNCEMENTS: dict[str, dict[str, str]] = {}


async def announce(code: str, level: str, message: str) -> bool:
    """置位/更新公告。新增或 message/level 变化时返回 True 并发布事件。"""
    old = _ANNOUNCEMENTS.get(code)
    if old is not None and old["message"] == message and old["level"] == level:
        return False
    _ANNOUNCEMENTS[code] = {
        "code": code,
        "level": level,
        "message": message,
        "since": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    logger.info("公告置位 [%s] %s", code, message)
    await publish_announcements_updated(get_announcements_payload())
    return True


async def revoke(code: str) -> bool:
    """撤销公告。实际存在时返回 True 并发布事件；不存在为幂等 no-op。"""
    if code not in _ANNOUNCEMENTS:
        return False
    del _ANNOUNCEMENTS[code]
    logger.info("公告撤销 [%s]", code)
    await publish_announcements_updated(get_announcements_payload())
    return True


def get_announcements() -> list[dict[str, str]]:
    """公告快照列表（since 升序；同秒并列按 code 稳定排序）。"""
    return sorted(
        (dict(item) for item in _ANNOUNCEMENTS.values()),
        key=lambda item: (item["since"], item["code"]),
    )


def get_announcements_payload() -> dict:
    """SSE 事件负载：携带当前公告快照。"""
    return {"announcements": get_announcements()}


def reset_announcements() -> None:
    """清空注册表（测试隔离用；不发事件）。"""
    _ANNOUNCEMENTS.clear()
