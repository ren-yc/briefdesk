"""日历视图数据访问（calendar WebPlugin 专属）。

日历查询同时覆盖 extra_times（主字段命中 OR extra_times 非空取回后按区间
过滤）；extra_times 是 JSON 文本无法走索引，本地库规模下按「非空」取回后
内存过滤可接受。核心 db.py 不含日历专属查询。
"""

import json
from typing import cast

from briefdesk.db import ItemRow, get_db


def _extra_times_in_range(raw: str, date_from: str, date_to_excl: str) -> bool:
    """extra_times JSON 中是否有时间点落在 [date_from, date_to_excl)。

    时间字符串固定 "YYYY-MM-DD[ HH:MM]"，前缀字符串比较即时间序；
    JSON 解析失败视为无（不抛错，日历是增值视图）。
    """
    if not raw:
        return False
    try:
        entries = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return False
    if not isinstance(entries, list):
        return False
    for e in entries:
        if isinstance(e, dict):
            t = e.get("time")
            if isinstance(t, str) and date_from <= t < date_to_excl:
                return True
    return False


async def get_calendar_items(date_from: str, date_to_excl: str) -> list[ItemRow]:
    """日历视图查询：start / end / extra_times 任一落在
    [date_from, date_to_excl) 的卡片。

    时间存 "YYYY-MM-DD HH:MM" 文本，按前缀字符串比较即日期序；
    extra_times 是 JSON 文本无法走索引，按「主字段命中 OR 非空」取回后
    由 _extra_times_in_range 过滤（本地库规模可接受）。
    排除已忽略（is_verified >= 0），保留未处理 + 备忘录。
    """
    db = await get_db()
    cursor = await db.execute(
        """SELECT * FROM items
           WHERE is_verified >= 0
             AND ((start >= ? AND start < ?)
                OR (end >= ? AND end < ?)
                OR extra_times != '')
           ORDER BY COALESCE(start, end)
           LIMIT 1000""",
        (date_from, date_to_excl, date_from, date_to_excl),
    )
    try:
        rows = await cursor.fetchall()
    finally:
        await cursor.close()
    out: list[ItemRow] = []
    for row in rows:
        r = dict(row)
        st, dl = r.get("start") or "", r.get("end") or ""
        if (st and date_from <= st < date_to_excl) or (
            dl and date_from <= dl < date_to_excl
        ) or _extra_times_in_range(r.get("extra_times") or "", date_from, date_to_excl):
            out.append(cast(ItemRow, r))
    return out
