"""日历视图路由（calendar WebPlugin）。"""

import re
from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException, Query

from briefdesk.plugins.calendar.db import get_calendar_items

router = APIRouter()

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _parse_date_param(value: str, name: str) -> str:
    """校验 YYYY-MM-DD 参数，非法抛 400。"""
    value = (value or "").strip()
    if not _DATE_RE.match(value):
        raise HTTPException(400, f"{name} must be YYYY-MM-DD")
    return value


@router.get("/api/calendar")
async def calendar(
    date_from: str = Query(..., alias="from"),
    date_to: str = Query(..., alias="to"),
):
    """日历视图数据：区间内带开始/截止时间的卡片（排除已忽略）。"""
    from_d = _parse_date_param(date_from, "from")
    to_d = _parse_date_param(date_to, "to")
    try:
        to_excl = (datetime.fromisoformat(to_d) + timedelta(days=1)).strftime(
            "%Y-%m-%d"
        )
    except ValueError:
        raise HTTPException(400, "to must be a valid date")
    if from_d > to_d:
        raise HTTPException(400, "from must be <= to")
    items = await get_calendar_items(from_d, to_excl)
    return {"items": items}
