"""提醒路由（reminders WebPlugin，P5 起从核心 server 迁出）。"""

import re
import time
from datetime import datetime

from fastapi import APIRouter, HTTPException

from briefdesk.db import get_due_reminders, set_item_reminder

router = APIRouter()

# 提醒时间入参契约：必须含时刻（可选秒与 UTC 偏移），拒绝仅日期/"T"/空格之外的写法
_REMIND_AT_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:?\d{2})?$"
)


@router.post("/api/items/{item_id}/reminder")
async def set_reminder(item_id: str, body: dict):
    """设置/清除卡片提醒。body.at 为本地时间 "YYYY-MM-DDTHH:MM" 或 null。

    必须含时刻（拒绝仅日期输入）；带时区的 aware datetime 先换算成服务器
    本地墙钟再存（remind_at 全链路为 naive 本地时间，与日历/前端解析一致）。
    """
    at = body.get("at")
    if at is None:
        remind_at: str | None = None
    elif isinstance(at, str):
        at_s = at.strip()
        if not _REMIND_AT_RE.match(at_s):
            raise HTTPException(400, "at must be YYYY-MM-DDTHH:MM or null")
        try:
            dt = datetime.fromisoformat(at_s)
        except ValueError:
            raise HTTPException(400, "at must be a valid datetime")
        if dt.tzinfo is not None:
            dt = dt.astimezone()  # aware → 服务器本地墙钟（naive 保留原样）
        remind_at = dt.strftime("%Y-%m-%d %H:%M")
    else:
        raise HTTPException(400, "at must be a string or null")
    if not await set_item_reminder(item_id, remind_at):
        raise HTTPException(404, "Item not found")
    return {"success": True, "remind_at": remind_at}


@router.get("/api/reminders/due")
async def reminders_due():
    """到期提醒：remind_at 不晚于本地现在的卡片（排除已忽略），供前端轮询。

    返回提醒所需最小字段；不清除 remind_at（清除走 POST /reminder null，
    保留多标签页"先清后通知"竞态语义）。
    """
    now_local = time.strftime("%Y-%m-%d %H:%M", time.localtime())
    return {"items": await get_due_reminders(now_local)}
