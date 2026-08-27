"""calendar 插件数据层与路由测试（内存 SQLite，沙箱安全）。

覆盖：
- 区间查询在「extra_times 干扰行超过旧 SQL LIMIT」时不再丢失区间内卡片
  （截断改为内存过滤之后应用）；
- 过滤后截断上限仍然生效；
- 路由对 from/to 的真实日期校验（如 2026-02-30 必须 400）。
"""

import unittest
from unittest.mock import AsyncMock, patch

import aiosqlite
from fastapi import HTTPException

from briefdesk.db import init_schema


async def _memory_db() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await init_schema(conn)
    return conn


async def _insert_item(
    conn: aiosqlite.Connection,
    item_id: str,
    *,
    end: str | None = None,
    start: str | None = None,
    extra_times: str = "",
) -> None:
    await conn.execute(
        """INSERT INTO items (
               id, category, title, source_quote, source_group,
               source, source_msg_id, msg_time, end, start, extra_times, created_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            item_id,
            "活动通知",
            "t",
            "quote",
            "测试群",
            "weflow-legacy",
            item_id,
            1893456000,
            end,
            start,
            extra_times,
            "2030-01-01 00:00:00",
        ),
    )
    await conn.commit()


RANGE_FROM = "2030-01-01"
RANGE_TO_EXCL = "2030-02-01"
TARGET_END = "2030-01-05 10:00"  # 落在区间内


class CalendarRangeTruncationTest(unittest.IsolatedAsyncioTestCase):
    """区间卡片不得被超量 extra_times 干扰行挤出结果集。"""

    async def test_range_cards_survive_extra_times_flood(self):
        from briefdesk.plugins.calendar import db as cal_db

        conn = await _memory_db()
        try:
            # 1001 条干扰行：extra_times 非空但时间点在区间外、start/end 为 NULL。
            # 旧实现按 COALESCE(start,end) 升序时它们排最前并吃满 LIMIT 1000，
            # 把真正的区间内卡片整体挤出。
            noise_json = '{"type": "end", "time": "2031-06-01", "label": "noise"}'
            for i in range(1001):
                await _insert_item(conn, "noise-" + str(i), extra_times=noise_json)
            target_ids = ["target-" + str(i) for i in range(3)]
            for tid in target_ids:
                await _insert_item(conn, tid, end=TARGET_END)

            async def fake_get_db():
                return conn

            with patch.object(cal_db, "get_db", fake_get_db):
                rows = await cal_db.get_calendar_items(RANGE_FROM, RANGE_TO_EXCL)

            got_ids = sorted(r["id"] for r in rows)
            self.assertEqual(got_ids, sorted(target_ids))
        finally:
            await conn.close()

    async def test_post_filter_cap_still_enforced(self):
        from briefdesk.plugins.calendar import db as cal_db

        conn = await _memory_db()
        try:
            # 全部落在区间内：过滤后仍须按上限截断到 1000 条。
            for i in range(1005):
                await _insert_item(conn, "in-range-" + str(i), end=TARGET_END)

            async def fake_get_db():
                return conn

            with patch.object(cal_db, "get_db", fake_get_db):
                rows = await cal_db.get_calendar_items(RANGE_FROM, RANGE_TO_EXCL)

            self.assertEqual(len(rows), 1000)
        finally:
            await conn.close()


class CalendarRouteDateValidationTest(unittest.IsolatedAsyncioTestCase):
    """/api/calendar 的 from/to 必须是真实存在的日历日期。"""

    async def test_impossible_from_date_rejected(self):
        from briefdesk.plugins.calendar import router as cal_router

        with patch.object(
            cal_router, "get_calendar_items", new=AsyncMock(return_value=[])
        ):
            with self.assertRaises(HTTPException) as ctx:
                await cal_router.calendar(date_from="2026-02-30", date_to="2026-03-01")
            self.assertEqual(ctx.exception.status_code, 400)

    async def test_bad_format_rejected_and_valid_passes(self):
        from briefdesk.plugins.calendar import router as cal_router

        fetch = AsyncMock(return_value=[{"id": "x"}])
        with patch.object(cal_router, "get_calendar_items", new=fetch):
            with self.assertRaises(HTTPException) as ctx:
                await cal_router.calendar(date_from="2030/01/01", date_to="2030-02-01")
            self.assertEqual(ctx.exception.status_code, 400)

            result = await cal_router.calendar(
                date_from="2030-01-01", date_to="2030-01-31"
            )
            self.assertEqual(result, {"items": [{"id": "x"}]})
            fetch.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
