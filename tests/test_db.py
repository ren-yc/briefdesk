"""DB schema 与查询辅助函数测试（内存数据库，不触碰应用数据库文件）。"""

import asyncio
import hashlib
import os
import shutil
import sqlite3
import tempfile
import unittest
import uuid
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import aiosqlite

from briefdesk.config import config
from briefdesk.db import (
    ItemInput,
    ItemRow,
    RawMsgInput,
    ReminderRow,
    SchemaMismatchError,
    SessionRow,
    _escape_like,
    apply_pending_restore,
    are_messages_processed,
    backup_db_to,
    bulk_insert_raw_messages,
    close_db,
    delete_category,
    delete_items,
    get_all_item_texts,
    get_all_sessions,
    get_category_counts,
    get_context_messages,
    get_db,
    get_due_reminders,
    get_group_count,
    get_item_texts_by_ids,
    get_items,
    get_items_by_subject,
    get_items_page,
    get_merge_candidates,
    get_oldest_unprocessed_by_session,
    get_recat_samples,
    get_session_last_polls,
    get_subject_count,
    init_schema,
    insert_category,
    insert_item,
    item_is_expired,
    load_embeddings,
    mark_message_processed,
    mark_messages_processed,
    merge_source_group,
    purge_expired_ignored,
    set_item_reminder,
    toggle_session,
    update_category,
    update_item_category,
    update_item_merged,
    update_item_verify,
    update_session_last_polls,
    upsert_embeddings,
    upsert_session,
    validate_restore_file,
    validate_schema,
)
from briefdesk.masking import normalize_subject
from briefdesk.plugins.calendar.db import get_calendar_items


class EscapeLikeTest(unittest.TestCase):
    def test_escapes_wildcards_and_backslash(self):
        self.assertEqual(_escape_like("100%_x\\y"), r"100\%\_x\\y")


class SchemaTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = await aiosqlite.connect(":memory:")
        self.db.row_factory = aiosqlite.Row
        await self.db.execute("PRAGMA foreign_keys = ON")
        await init_schema(self.db)

    async def asyncTearDown(self):
        await self.db.close()

    async def test_new_raw_messages_schema_has_sender_name(self):
        cursor = await self.db.execute("PRAGMA table_info(raw_messages)")
        columns = {row["name"] for row in await cursor.fetchall()}
        self.assertIn("sender_name", columns)

    async def test_sessions_schema_has_watermark_columns(self):
        # last_active / last_poll_ts 直接在 CREATE TABLE 中定义
        cursor = await self.db.execute("PRAGMA table_info(sessions)")
        columns = {row["name"] for row in await cursor.fetchall()}
        self.assertIn("last_active", columns)
        self.assertIn("last_poll_ts", columns)

    async def test_items_schema_has_verified_at(self):
        cursor = await self.db.execute("PRAGMA table_info(items)")
        columns = {row["name"] for row in await cursor.fetchall()}
        self.assertIn("verified_at", columns)

    async def test_items_schema_has_extra_times(self):
        # 多时间点结构化存储列在 CREATE TABLE 中直接定义
        cursor = await self.db.execute("PRAGMA table_info(items)")
        columns = {row["name"] for row in await cursor.fetchall()}
        self.assertIn("extra_times", columns)

    async def test_default_categories_seeded(self):
        cursor = await self.db.execute("SELECT COUNT(*) AS cnt FROM categories")
        row = await cursor.fetchone()
        self.assertGreaterEqual(row["cnt"], 5)

    async def test_validate_schema_passes_on_current_schema(self):
        # 当前 init_schema 建出的库应通过严格 schema 校验
        await validate_schema(self.db)

    async def test_validate_schema_passes_on_fresh_empty_db(self):
        fresh = await aiosqlite.connect(":memory:")
        try:
            await validate_schema(fresh)
        finally:
            await fresh.close()

    async def test_validate_schema_fails_on_missing_table(self):
        await self.db.execute("DROP TABLE contacts")
        await self.db.commit()
        with self.assertRaises(SchemaMismatchError):
            await validate_schema(self.db)

    async def test_validate_schema_fails_on_missing_column(self):
        # 模拟旧库：items 缺 extra_times 列
        await self.db.execute("DROP TABLE items")
        await self.db.execute(
            """CREATE TABLE items (
                id            TEXT PRIMARY KEY,
                category      TEXT NOT NULL,
                title         TEXT NOT NULL,
                key_info      TEXT,
                sender_name   TEXT,
                source_quote  TEXT NOT NULL,
                source_group  TEXT NOT NULL,
                subject       TEXT,
                source        TEXT NOT NULL DEFAULT '',
                source_msg_id TEXT NOT NULL,
                session_id    TEXT NOT NULL DEFAULT '',
                msg_time      INTEGER NOT NULL DEFAULT 0,
                is_verified   INTEGER DEFAULT 0,
                verified_at   TEXT,
                content_hash  TEXT,
                image_urls    TEXT NOT NULL DEFAULT '',
                article_url   TEXT NOT NULL DEFAULT '',
                start    TEXT,
                end      TEXT,
                remind_at     TEXT,
                created_at    TEXT NOT NULL
            )"""
        )
        await self.db.commit()
        with self.assertRaises(SchemaMismatchError):
            await validate_schema(self.db)

    async def test_validate_schema_fails_on_type_mismatch(self):
        # 模拟类型不匹配：msg_time 被建成 TEXT
        await self.db.execute("DROP TABLE items")
        await self.db.execute(
            """CREATE TABLE items (
                id            TEXT PRIMARY KEY,
                category      TEXT NOT NULL,
                title         TEXT NOT NULL,
                key_info      TEXT,
                sender_name   TEXT,
                source_quote  TEXT NOT NULL,
                source_group  TEXT NOT NULL,
                subject       TEXT,
                source        TEXT NOT NULL DEFAULT '',
                source_msg_id TEXT NOT NULL,
                session_id    TEXT NOT NULL DEFAULT '',
                msg_time      TEXT NOT NULL DEFAULT '',
                is_verified   INTEGER DEFAULT 0,
                verified_at   TEXT,
                content_hash  TEXT,
                image_urls    TEXT NOT NULL DEFAULT '',
                article_url   TEXT NOT NULL DEFAULT '',
                start    TEXT,
                end      TEXT,
                remind_at     TEXT,
                extra_times   TEXT NOT NULL DEFAULT '',
                created_at    TEXT NOT NULL
            )"""
        )
        await self.db.commit()
        with self.assertRaises(SchemaMismatchError):
            await validate_schema(self.db)



class ContextMessagesSenderNameTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = await aiosqlite.connect(":memory:")
        self.db.row_factory = aiosqlite.Row
        await self.db.execute("PRAGMA foreign_keys = ON")
        await init_schema(self.db)

    async def asyncTearDown(self):
        await self.db.close()

    async def _seed(
        self, is_group: int, raw_sender_name: str, contact_name: str
    ) -> None:
        await self.db.execute(
            "INSERT OR REPLACE INTO sessions "
            "(source, session_id, name, is_group, enabled) "
            "VALUES ('weflow-legacy', 'g1', '项目群', ?, 1)",
            (is_group,),
        )
        await self.db.execute(
            "INSERT OR REPLACE INTO contacts (source, sender_id, display_name) "
            "VALUES ('weflow-legacy', 'u1', ?)",
            (contact_name,),
        )
        await self.db.execute(
            "INSERT OR REPLACE INTO raw_messages "
            "(source, msg_id, session_id, group_name, sender_id, sender_name, "
            "content, timestamp) "
            "VALUES ('weflow-legacy', 'm1', 'g1', '项目群', 'u1', ?, 'hello', 100)",
            (raw_sender_name,),
        )
        await self.db.commit()

    async def _context_sender(self) -> str:
        with patch("briefdesk.db.get_db", new=AsyncMock(return_value=self.db)):
            rows = await get_context_messages("weflow-legacy", "g1", 100)
        self.assertEqual(len(rows), 1)
        return rows[0]["sender"]

    async def test_group_prefers_sender_name_snapshot_over_contact(self):
        await self._seed(is_group=1, raw_sender_name="群名片", contact_name="全局名")
        self.assertEqual(await self._context_sender(), "群名片")

    async def test_private_prefers_live_contact_name(self):
        await self._seed(is_group=0, raw_sender_name="旧快照", contact_name="新备注")
        self.assertEqual(await self._context_sender(), "新备注")

    async def test_group_legacy_uid_snapshot_falls_back_to_contact(self):
        await self._seed(is_group=1, raw_sender_name="u1", contact_name="全局名")
        self.assertEqual(await self._context_sender(), "全局名")

    async def test_group_empty_snapshot_falls_back_to_contact(self):
        await self._seed(is_group=1, raw_sender_name="", contact_name="全局名")
        self.assertEqual(await self._context_sender(), "全局名")

    async def _seed_image_context(self, source: str, placeholder: str) -> None:
        msg_id = f"m-img-{source}"
        await self.db.execute(
            "INSERT INTO raw_messages (source, msg_id, session_id, group_name, "
            "sender_id, sender_name, content, timestamp) "
            "VALUES (?, ?, 'g1', '项目群', 'u1', 'A', ?, 100)",
            (source, msg_id, placeholder),
        )
        await self.db.execute(
            "INSERT INTO items (id, category, title, source_quote, source_group, "
            "source, source_msg_id, msg_time, is_verified, created_at) "
            "VALUES (?, '社团招新', '戟川学社招新', "
            "'[OCR]\n[图片 1 OCR 结果]\n戟川学社\n招新啦！', "
            "'项目群', ?, ?, 100, 0, '2026-01-01T00:00:00+00:00')",
            (f"i-img-{source}", source, msg_id),
        )
        await self.db.commit()

    async def test_image_placeholder_resolves_to_ocr_quote(self):
        # 图片消息（raw 内容为占位符 [图片]）应回填 OCR 后的原文，并去掉 [OCR] 与 [图片 N OCR 结果] 标记行
        await self._seed_image_context("weflow-legacy", "[图片]")
        with patch("briefdesk.db.get_db", new=AsyncMock(return_value=self.db)):
            rows = await get_context_messages("weflow-legacy", "g1", 100)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["content"], "戟川学社\n招新啦！")

    async def test_qqflow_image_placeholder_resolves_to_ocr_quote(self):
        # qqflow 图片 raw 占位符为 [image]，应与 [图片] 一样回填 OCR 原文并去掉标记行
        await self._seed_image_context("qqflow", "[image]")
        with patch("briefdesk.db.get_db", new=AsyncMock(return_value=self.db)):
            rows = await get_context_messages("qqflow", "g1", 100)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["content"], "戟川学社\n招新啦！")


class ContextTargetInclusionTest(unittest.IsolatedAsyncioTestCase):
    """上下文查询在目标消息被窗口截断时仍必须返回目标行（回归：原文引用高亮落空）。

    现场：高活跃会话 ±1h 窗口 87 条消息，卡片「订阅ds不涨价」的目标消息排第 40 位，
    旧的「窗口内最早 30 条」会截掉它，前端按 msg_id 高亮落空。
    """

    async def asyncSetUp(self):
        self.db = await aiosqlite.connect(":memory:")
        self.db.row_factory = aiosqlite.Row
        await self.db.execute("PRAGMA foreign_keys = ON")
        await init_schema(self.db)

    async def asyncTearDown(self):
        await self.db.close()

    async def _seed_active_session(self, target_index: int = 39, anchor_index: int = 39) -> int:
        """铺 87 条消息（80s 间隔，全在 ±1h 窗口内），返回锚点（卡片 msg_time）时间戳。

        target_index 为目标消息（msg_id=m-target）在窗口内的序号（0 起）；
        anchor_index 为卡片 msg_time 对应的消息序号，两者不同即模拟锚点与
        目标消息时间戳不一致的兜底场景。
        """
        since = 1_000_000 - 3600
        anchor = since + anchor_index * 80
        rows = []
        for i in range(87):
            ts = since + i * 80
            msg_id = "m-target" if i == target_index else f"m{i:02d}"
            rows.append((msg_id, f"内容 {i:02d}", ts))
        await self.db.executemany(
            "INSERT INTO raw_messages (source, msg_id, session_id, group_name, "
            "sender_id, sender_name, content, timestamp) "
            "VALUES ('qqflow', ?, 'g1', '项目群', 'u1', 'A', ?, ?)",
            rows,
        )
        await self.db.commit()
        return anchor

    async def test_target_beyond_first_30_still_included(self):
        # 现场复刻：87 条消息、目标排第 40 位（窗口最早 30 条之外），仍必须返回目标行
        anchor = await self._seed_active_session(target_index=39, anchor_index=39)
        with patch("briefdesk.db.get_db", new=AsyncMock(return_value=self.db)):
            rows = await get_context_messages("qqflow", "g1", anchor, "m-target")
        ids = [r["msg_id"] for r in rows]
        self.assertIn("m-target", ids)
        self.assertLessEqual(len(rows), 30)
        # 双向取数：锚点前最近 15 条 + 锚点起（含目标）最近 15 条，整体时间升序
        self.assertEqual(len(rows), 30)
        self.assertEqual(ids[0], "m24")        # 锚点前最近 15 条的最早一条
        self.assertEqual(ids[14], "m38")       # 目标前最近一条
        self.assertEqual(ids[15], "m-target")  # 目标为后半段首条
        self.assertEqual(ids[29], "m53")
        times = [r["time"] for r in rows]
        self.assertEqual(times, sorted(times))

    async def test_target_msg_id_fallback_when_anchor_mismatched(self):
        # 锚点（卡片 msg_time）与目标消息时间戳不一致（目标在锚点之后 21 条）时，
        # msg_id 兜底补回目标行；未传 msg_id 则保持旧的窗口截断行为
        anchor = await self._seed_active_session(target_index=60, anchor_index=39)
        with patch("briefdesk.db.get_db", new=AsyncMock(return_value=self.db)):
            without = await get_context_messages("qqflow", "g1", anchor)
            with_target = await get_context_messages("qqflow", "g1", anchor, "m-target")
        self.assertNotIn("m-target", [r["msg_id"] for r in without])
        ids = [r["msg_id"] for r in with_target]
        self.assertIn("m-target", ids)
        self.assertLessEqual(len(with_target), 30)
        times = [r["time"] for r in with_target]
        self.assertEqual(times, sorted(times))


class GetCategoryCountsTest(unittest.IsolatedAsyncioTestCase):
    """get_category_counts 只统计 categories 表中仍存在的分类（删除类别的遗留卡片不计入）。"""

    async def asyncSetUp(self):
        self.db = await aiosqlite.connect(":memory:")
        self.db.row_factory = aiosqlite.Row
        await self.db.execute("PRAGMA foreign_keys = ON")
        await init_schema(self.db)

    async def asyncTearDown(self):
        await self.db.close()

    async def _insert_item(self, item_id: str, category: str) -> None:
        await self.db.execute(
            "INSERT INTO items (id, category, title, source_quote, source_group, "
            "source, source_msg_id, msg_time, is_verified, created_at) "
            "VALUES (?, ?, '标题', '引文', '项目群', 'weflow-legacy', ?, 100, 0, '2026-01-01T00:00:00+00:00')",
            (item_id, category, item_id),
        )
        await self.db.commit()

    async def test_legacy_category_items_not_counted(self):
        # 删除「学术」类别（保留卡片）后：学术卡片成为遗留项，侧边栏不应再计数
        await self.db.execute("DELETE FROM categories WHERE name = '学术'")
        await self.db.commit()
        await self._insert_item("i1", "活动通知")  # 存在且启用
        await self._insert_item("i2", "学术")  # 类别已删除的遗留卡片

        with patch("briefdesk.db.get_db", new=AsyncMock(return_value=self.db)):
            counts = await get_category_counts()
        keys = [c["key"] for c in counts]
        self.assertIn("活动通知", keys)
        self.assertNotIn("学术", keys)
        self.assertEqual(next(c for c in counts if c["key"] == "活动通知")["count"], 1)

    async def test_disabled_category_items_not_counted(self):
        # 停用类别的卡片同样不计入侧边栏
        await self.db.execute("UPDATE categories SET enabled = 0 WHERE name = '交易'")
        await self.db.commit()
        await self._insert_item("i1", "活动通知")
        await self._insert_item("i2", "交易")

        with patch("briefdesk.db.get_db", new=AsyncMock(return_value=self.db)):
            counts = await get_category_counts()
        keys = [c["key"] for c in counts]
        self.assertIn("活动通知", keys)
        self.assertNotIn("交易", keys)


class GetItemsSearchTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = await aiosqlite.connect(":memory:")
        self.db.row_factory = aiosqlite.Row
        await self.db.execute("PRAGMA foreign_keys = ON")
        await init_schema(self.db)
        await self.db.execute(
            "INSERT INTO items (id, category, title, source_quote, source_group, "
            "source, source_msg_id, msg_time, is_verified, created_at) "
            "VALUES ('i1', '学术', '机器学习讲座报名', 'quote', '项目群', "
            "'weflow-legacy', 'm1', 100, 0, '2026-01-01T00:00:00+00:00')"
        )
        await self.db.commit()

    async def asyncTearDown(self):
        await self.db.close()

    async def _items(self, q):
        with patch("briefdesk.db.get_db", new=AsyncMock(return_value=self.db)):
            return await get_items(q=q)

    async def test_whitespace_only_query_no_sql_error(self):
        # 纯空白 q 曾拼出 "AND ()" 触发 SQL 语法错误（500）；
        # 修复后与空 q 同语义：不附加搜索条件，返回全部
        self.assertEqual(len(await self._items("   ")), 1)

    async def test_none_and_empty_query_return_all(self):
        self.assertEqual(len(await self._items(None)), 1)
        self.assertEqual(len(await self._items("")), 1)

    async def test_term_match(self):
        self.assertEqual(len(await self._items("讲座")), 1)

    async def test_multi_term_or_match(self):
        self.assertEqual(len(await self._items("机器学习 讲座")), 1)

    async def test_no_match_returns_empty(self):
        self.assertEqual(await self._items("不存在的词"), [])


class ItemExpiryTest(unittest.TestCase):
    NOW = "2026-08-18 12:00:00"

    def test_past_deadline_is_expired(self):
        self.assertTrue(item_is_expired(None, "2026-08-18 11:59", "", self.NOW))

    def test_date_only_deadline_stays_active_through_that_day(self):
        self.assertFalse(item_is_expired(None, "2026-08-18", "", self.NOW))

    def test_future_extra_time_keeps_partially_expired_card(self):
        extra = '[{"type":"end","time":"2026-08-20","label":"后续任务"}]'
        self.assertFalse(item_is_expired(None, "2026-08-17", extra, self.NOW))

    def test_missing_or_invalid_primary_end_never_expires_card(self):
        self.assertFalse(item_is_expired(None, None, "", self.NOW))
        self.assertFalse(item_is_expired(None, "not-a-time", "", self.NOW))
        self.assertFalse(item_is_expired(None, "2026-08-17 00:00:00", "", self.NOW))

    def test_invalid_extra_time_type_does_not_keep_card_active(self):
        extra = '[{"type":"note","time":"2026-08-20","label":"脏数据"}]'
        self.assertTrue(item_is_expired(None, "2026-08-17", extra, self.NOW))


class ItemsPageTest(unittest.IsolatedAsyncioTestCase):
    """列表分页、总数与组数必须共享全部有效过滤条件。"""

    async def asyncSetUp(self):
        self.db = await aiosqlite.connect(":memory:")
        self.db.row_factory = aiosqlite.Row
        await self.db.execute("PRAGMA foreign_keys = ON")
        await init_schema(self.db)
        await self.db.executemany(
            "INSERT INTO sessions "
            "(source, session_id, name, is_group, enabled) VALUES (?, ?, ?, 1, ?)",
            [
                ("weflow-legacy", "active", "启用群", 1),
                ("weflow-legacy", "disabled", "停用群", 0),
            ],
        )
        await self.db.execute("UPDATE categories SET enabled = 0 WHERE name = '交易'")

        rows = []
        for i in range(204):
            rows.append(
                (
                    f"i{i:03d}",
                    "活动通知",
                    f"分页卡片 {i}",
                    "quote",
                    "群A" if i % 2 == 0 else "群B",
                    f"主体{i // 2}",
                    "weflow-legacy",
                    f"m{i:03d}",
                    "active",
                    i,
                    0,
                    None,
                    "2026-08-20",
                    "",
                    "2026-08-18T00:00:00+00:00",
                )
            )
        rows.extend(
            [
                (
                    "partial",
                    "活动通知",
                    "分页部分截止",
                    "quote",
                    "群C",
                    "部分截止",
                    "weflow-legacy",
                    "m-partial",
                    "active",
                    999,
                    0,
                    None,
                    "2026-08-17",
                    '[{"type":"end","time":"2026-08-20","label":"后续任务"}]',
                    "2026-08-18T00:00:00+00:00",
                ),
                (
                    "expired",
                    "活动通知",
                    "分页已截止",
                    "quote",
                    "群A",
                    "已截止",
                    "weflow-legacy",
                    "m-expired",
                    "active",
                    998,
                    0,
                    None,
                    "2026-08-17",
                    "",
                    "2026-08-18T00:00:00+00:00",
                ),
                (
                    "disabled-session",
                    "活动通知",
                    "分页停用会话",
                    "quote",
                    "停用群",
                    "停用会话",
                    "weflow-legacy",
                    "m-disabled-session",
                    "disabled",
                    997,
                    0,
                    None,
                    "2026-08-20",
                    "",
                    "2026-08-18T00:00:00+00:00",
                ),
                (
                    "disabled-category",
                    "交易",
                    "分页停用类别",
                    "quote",
                    "群A",
                    "停用类别",
                    "weflow-legacy",
                    "m-disabled-category",
                    "active",
                    996,
                    0,
                    None,
                    "2026-08-20",
                    "",
                    "2026-08-18T00:00:00+00:00",
                ),
            ]
        )
        await self.db.executemany(
            "INSERT INTO items "
            "(id, category, title, source_quote, source_group, subject, source, "
            "source_msg_id, session_id, msg_time, is_verified, start, end, "
            "extra_times, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        await self.db.commit()

    async def asyncTearDown(self):
        await self.db.close()

    async def _page(self, offset: int):
        with patch("briefdesk.db.get_db", new=AsyncMock(return_value=self.db)):
            return await get_items_page(
                q="分页",
                limit=100,
                offset=offset,
                hide_expired=True,
                now_local="2026-08-18 12:00:00",
            )

    async def test_three_pages_cover_all_matching_cards_without_duplicates(self):
        first = await self._page(0)
        second = await self._page(first["next_offset"])
        third = await self._page(second["next_offset"])

        self.assertEqual([len(first["items"]), len(second["items"]), len(third["items"])], [100, 100, 5])
        self.assertEqual(first["total_count"], 205)
        self.assertEqual(first["group_count"], 103)
        self.assertEqual(first["filter_now"], "2026-08-18 12:00:00")
        self.assertEqual(first["source_groups"], ["群A", "群B", "群C"])
        self.assertTrue(first["has_more"])
        self.assertTrue(second["has_more"])
        self.assertFalse(third["has_more"])
        self.assertEqual(third["next_offset"], 205)

        ids = [item["id"] for page in (first, second, third) for item in page["items"]]
        self.assertEqual(len(ids), 205)
        self.assertEqual(len(set(ids)), 205)
        self.assertIn("partial", ids)
        self.assertNotIn("expired", ids)
        self.assertNotIn("disabled-session", ids)
        self.assertNotIn("disabled-category", ids)

    async def test_source_group_and_time_filters_affect_page_and_counts(self):
        with patch("briefdesk.db.get_db", new=AsyncMock(return_value=self.db)):
            page = await get_items_page(
                q="分页",
                source_group="群A",
                min_msg_time=100,
                limit=100,
                hide_expired=True,
                now_local="2026-08-18 12:00:00",
            )
        self.assertEqual(page["total_count"], 52)
        self.assertEqual(page["group_count"], 52)
        self.assertEqual(len(page["items"]), 52)
        self.assertEqual(page["source_groups"], ["群A", "群B", "群C"])
        self.assertEqual(page["filter_now"], "2026-08-18 12:00:00")

    async def test_empty_page_still_returns_full_counts(self):
        page = await self._page(999)
        self.assertEqual(page["items"], [])
        self.assertEqual(page["total_count"], 205)
        self.assertEqual(page["group_count"], 103)
        self.assertTrue(page["filter_now"])


class ReminderAndCalendarTest(unittest.IsolatedAsyncioTestCase):
    """#3A/#13：到期提醒查询 + 已忽略卡片不提醒/不进日历但保留数据。"""

    async def asyncSetUp(self):
        self.db = await aiosqlite.connect(":memory:")
        self.db.row_factory = aiosqlite.Row
        await self.db.execute("PRAGMA foreign_keys = ON")
        await init_schema(self.db)

    async def asyncTearDown(self):
        await self.db.close()

    @staticmethod
    def _item(**overrides: Any) -> ItemInput:
        base: dict[str, Any] = {
            "category": "活动通知",
            "title": "测试卡片",
            "key_info": "k",
            "sender_name": "A",
            "source_quote": "quote",
            "source_group": "社团群",
            "subject": "摄影社",
            "source": "weflow-legacy",
            "source_msg_id": "m",
            "session_id": "s1",
            "msg_time": 100,
            "is_verified": 0,
            "content_hash": "h",
        }
        base.update(overrides)
        return cast(ItemInput, base)

    async def _insert(self, **overrides) -> str:
        # 默认给每条卡片唯一 source_msg_id：insert_item 以 (source, source_msg_id)
        # 为唯一键，重复会命中 INSERT OR IGNORE 冲突（真实 id 语义下指向同一行）
        if "source_msg_id" not in overrides:
            overrides["source_msg_id"] = f"m-{uuid.uuid4().hex[:12]}"
        with patch("briefdesk.db.get_db", new=AsyncMock(return_value=self.db)):
            return await insert_item(self._item(**overrides))

    async def _due(self, now_local: str) -> list[ReminderRow]:
        with patch("briefdesk.db.get_db", new=AsyncMock(return_value=self.db)):
            return await get_due_reminders(now_local)

    async def _calendar(self, date_from: str, date_to_excl: str) -> list[ItemRow]:
        # 日历查询随 calendar 插件分发：patch 插件模块内的 get_db 引用
        with patch("briefdesk.plugins.calendar.db.get_db", new=AsyncMock(return_value=self.db)):
            return await get_calendar_items(date_from, date_to_excl)

    async def _set_reminder(self, item_id: str, at: str | None) -> None:
        with patch("briefdesk.db.get_db", new=AsyncMock(return_value=self.db)):
            await set_item_reminder(item_id, at)

    async def _verify(self, item_id: str, verified: int) -> None:
        with patch("briefdesk.db.get_db", new=AsyncMock(return_value=self.db)):
            await update_item_verify(item_id, verified)

    async def test_due_reminders_only_past_unignored(self):
        past = await self._insert(title="已到期")
        future = await self._insert(title="未到期")
        ignored = await self._insert(title="已忽略但到期")
        await self._insert(title="无提醒")
        await self._set_reminder(past, "2000-01-01 00:00")
        await self._set_reminder(future, "2999-01-01 00:00")
        await self._set_reminder(ignored, "2000-01-01 00:00")
        await self._verify(ignored, -1)

        due = await self._due("2000-01-02 00:00")
        self.assertEqual([d["id"] for d in due], [past])

    async def test_undo_ignore_restores_due(self):
        item = await self._insert(title="到期后撤销忽略")
        await self._set_reminder(item, "2000-01-01 00:00")
        await self._verify(item, -1)
        self.assertEqual(await self._due("2000-01-02 00:00"), [])
        # 数据保留：行仍在 items 表
        cursor = await self.db.execute("SELECT COUNT(*) AS cnt FROM items WHERE id = ?", (item,))
        row = await cursor.fetchone()
        self.assertEqual(row["cnt"], 1)
        # 撤销忽略后重新进入到期结果
        await self._verify(item, 0)
        due = await self._due("2000-01-02 00:00")
        self.assertEqual([d["id"] for d in due], [item])

    async def test_ignored_excluded_from_calendar_data_kept(self):
        item = await self._insert(title="日历卡片", start="2026-08-15 10:00")
        await self._verify(item, -1)
        self.assertEqual(await self._calendar("2026-08-01", "2026-09-01"), [])
        cursor = await self.db.execute("SELECT is_verified FROM items WHERE id = ?", (item,))
        row = await cursor.fetchone()
        self.assertEqual(row["is_verified"], -1)  # 数据保留，仅标记忽略
        # 撤销忽略后回到日历区间查询
        await self._verify(item, 0)
        in_cal = await self._calendar("2026-08-01", "2026-09-01")
        self.assertEqual([d["id"] for d in in_cal], [item])

    async def test_update_item_verify_missing_returns_false(self):
        # F3：不存在的卡片应返回 False（server 层据此转 404），而非静默成功
        with patch("briefdesk.db.get_db", new=AsyncMock(return_value=self.db)):
            self.assertFalse(await update_item_verify("no-such-id", 1))

    async def test_update_item_verify_existing_returns_true(self):
        item = await self._insert(title="可验证卡片")
        with patch("briefdesk.db.get_db", new=AsyncMock(return_value=self.db)):
            self.assertTrue(await update_item_verify(item, 1))
        cursor = await self.db.execute("SELECT is_verified FROM items WHERE id = ?", (item,))
        row = await cursor.fetchone()
        self.assertEqual(row["is_verified"], 1)

    async def test_calendar_includes_extra_times(self):
        # 多时间点卡片：extra_times 中的截止日也把卡片带进对应月份的日历
        item = await self._insert(
            title="部门工作提醒",
            end="2026-07-31",
            extra_times='[{"type":"end","time":"2026-08-15","label":"视频"},'
            '{"type":"end","time":"2026-09-20","label":"海报"}]',
        )
        # 7 月（主 end）与 8 月、9 月（extra_times）都命中
        self.assertEqual(
            [d["id"] for d in await self._calendar("2026-07-01", "2026-08-01")], [item]
        )
        self.assertEqual(
            [d["id"] for d in await self._calendar("2026-08-01", "2026-09-01")], [item]
        )
        self.assertEqual(
            [d["id"] for d in await self._calendar("2026-09-01", "2026-10-01")], [item]
        )
        # 10 月无任何时间点 → 不命中
        self.assertEqual(await self._calendar("2026-10-01", "2026-11-01"), [])

    async def test_calendar_ignores_dirty_extra_times(self):
        # extra_times 脏 JSON 不影响日历主字段行为
        item = await self._insert(title="脏数据", end="2026-07-31", extra_times="not-json")
        self.assertEqual(
            [d["id"] for d in await self._calendar("2026-07-01", "2026-08-01")], [item]
        )


class SubjectTimelineNormalizationTest(unittest.IsolatedAsyncioTestCase):
    """#9B（修正版）：subject 写时归一化，时间线按归一化键跨写法聚合。"""

    async def asyncSetUp(self):
        self.db = await aiosqlite.connect(":memory:")
        self.db.row_factory = aiosqlite.Row
        await self.db.execute("PRAGMA foreign_keys = ON")
        await init_schema(self.db)

    async def asyncTearDown(self):
        await self.db.close()

    @staticmethod
    def _item(subject: str, msg_id: str, msg_time: int) -> ItemInput:
        return {
            "category": "活动通知",
            "title": "卡片",
            "key_info": "",
            "sender_name": "A",
            "source_quote": "q",
            "source_group": "社团群",
            "subject": normalize_subject(subject),  # 模拟 pipeline 写侧归一化
            "source": "weflow-legacy",
            "source_msg_id": msg_id,
            "session_id": "s1",
            "msg_time": msg_time,
            "is_verified": 0,
            "content_hash": "h",
        }

    async def _insert(self, subject: str, msg_id: str, msg_time: int) -> None:
        with patch("briefdesk.db.get_db", new=AsyncMock(return_value=self.db)):
            await insert_item(self._item(subject, msg_id, msg_time))

    async def _timeline(self, subject: str) -> list[str]:
        with patch("briefdesk.db.get_db", new=AsyncMock(return_value=self.db)):
            rows = await get_items_by_subject(subject, 100, 0)
            return sorted(r["source_msg_id"] for r in rows)

    async def test_cross_script_and_case_aggregation(self):
        await self._insert("摄影社", "m1", 1)
        await self._insert("摄影社 ", "m2", 2)  # 尾随空格 → 同一主体
        await self._insert("ＡＣＭ社", "m3", 3)  # 全角字母 → 同一主体
        await self._insert("acm社", "m4", 4)  # 大小写 → 同一主体
        await self._insert("摄影社招新", "m5", 5)  # 不剥后缀 → 非同一主体

        self.assertEqual(await self._timeline("摄影社"), ["m1", "m2"])
        self.assertEqual(await self._timeline("ACM社"), ["m3", "m4"])
        self.assertEqual(await self._timeline("摄影社招新"), ["m5"])

    async def test_subject_count_matches_timeline(self):
        await self._insert("摄影社", "m1", 1)
        await self._insert("摄影社 ", "m2", 2)
        await self._insert("摄影社招新", "m3", 3)
        with patch("briefdesk.db.get_db", new=AsyncMock(return_value=self.db)):
            self.assertEqual(await get_subject_count("摄影社"), 2)


class GetGroupCountTest(unittest.IsolatedAsyncioTestCase):
    """组数口径：有主体 (subject, category) 键数 + 无主体条数（与列表渲染块一致）。"""

    async def asyncSetUp(self):
        self.db = await aiosqlite.connect(":memory:")
        self.db.row_factory = aiosqlite.Row
        await self.db.execute("PRAGMA foreign_keys = ON")
        await init_schema(self.db)

    async def asyncTearDown(self):
        await self.db.close()

    @staticmethod
    def _item(subject, msg_id, category="活动通知", is_verified=0, title="标题") -> ItemInput:
        return {
            "category": category,
            "title": title,
            "key_info": "",
            "sender_name": "A",
            "source_quote": "q",
            "source_group": "社团群",
            "subject": subject,
            "source": "weflow-legacy",
            "source_msg_id": msg_id,
            "session_id": "s1",
            "msg_time": 1,
            "is_verified": is_verified,
            "content_hash": "h",
        }

    async def _insert(self, **overrides) -> None:
        with patch("briefdesk.db.get_db", new=AsyncMock(return_value=self.db)):
            await insert_item(self._item(**overrides))

    async def _count(self, **kwargs) -> int:
        with patch("briefdesk.db.get_db", new=AsyncMock(return_value=self.db)):
            return await get_group_count(**kwargs)

    async def test_block_semantics(self):
        # 无主体 2 条 + 单成员组 1 + 双成员组 1 + 四成员组 1（同名不同类 = 独立键）
        await self._insert(subject=None, msg_id="n1")
        await self._insert(subject=None, msg_id="n2")
        await self._insert(subject="摄影社", msg_id="a1")
        await self._insert(subject="编程社", msg_id="b1")
        await self._insert(subject="编程社", msg_id="b2")
        await self._insert(subject="摄影社", category="社团招新", msg_id="c1")
        await self._insert(subject="摄影社", category="社团招新", msg_id="c2")
        await self._insert(subject="摄影社", category="社团招新", msg_id="c3")
        await self._insert(subject="摄影社", category="社团招新", msg_id="c4")
        self.assertEqual(await self._count(), 2 + 3)  # 无主体2 + 键(摄影社/编程社/摄影社@招新)

    async def test_category_and_verified_filters(self):
        await self._insert(subject=None, msg_id="n1")
        await self._insert(subject="摄影社", msg_id="a1")
        await self._insert(subject="编程社", msg_id="b1")
        await self._insert(subject="编程社", msg_id="b2")
        await self._insert(subject="摄影社", category="社团招新", msg_id="c1")
        await self._insert(subject="摄影社", category="社团招新", msg_id="c2")
        await self._insert(subject="M社", msg_id="m1", is_verified=1)
        await self._insert(subject="I社", msg_id="i1", is_verified=-1)

        self.assertEqual(await self._count(category="活动通知"), 1 + 2)  # 无主体1 + 摄影社/编程社
        self.assertEqual(await self._count(verified="memo"), 1)
        self.assertEqual(await self._count(verified="ignored"), 1)

    async def test_disabled_category_excluded(self):
        await self.db.execute("UPDATE categories SET enabled = 0 WHERE name = '交易'")
        await self.db.commit()
        await self._insert(subject="旧物社", msg_id="o1", category="交易")
        await self._insert(subject="旧物社", msg_id="o2", category="交易")
        await self._insert(subject="新社", msg_id="x1")
        self.assertEqual(await self._count(), 1)

    async def test_search_filter(self):
        await self._insert(subject="A社", msg_id="a1", title="机器学习讲座")
        await self._insert(subject="A社", msg_id="a2", title="机器学习讲座")
        await self._insert(subject="B社", msg_id="b1", title="篮球赛报名")
        self.assertEqual(await self._count(q="讲座"), 1)


class UpsertSessionTest(unittest.IsolatedAsyncioTestCase):
    """F2：upsert_session 单语句 UPSERT 的插入/更新语义与并发原子性。"""

    async def asyncSetUp(self):
        self.db = await aiosqlite.connect(":memory:")
        self.db.row_factory = aiosqlite.Row
        await self.db.execute("PRAGMA foreign_keys = ON")
        await init_schema(self.db)

    async def asyncTearDown(self):
        await self.db.close()

    async def _upsert(self, *args, **kwargs) -> None:
        with patch("briefdesk.db.get_db", new=AsyncMock(return_value=self.db)):
            await upsert_session(*args, **kwargs)

    async def _all(self) -> list[SessionRow]:
        with patch("briefdesk.db.get_db", new=AsyncMock(return_value=self.db)):
            return await get_all_sessions()

    async def test_insert_then_update_keeps_enabled_and_watermark(self):
        await self._upsert("weflow-legacy", "s1", "群A", True)
        rows = await self._all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["enabled"], 0)  # 新会话默认停用
        self.assertIsNone(rows[0]["last_poll_ts"])

        # 模拟用户启用 + 水位推进后再次 upsert（刷新会话）：
        # enabled/last_poll_ts 必须保留，名称/类型等元数据更新
        with patch("briefdesk.db.get_db", new=AsyncMock(return_value=self.db)):
            await toggle_session("weflow-legacy", "s1")
            await update_session_last_polls("weflow-legacy", [("s1", 1000)])
        await self._upsert("weflow-legacy", "s1", "群A新名", True, is_official=True, last_active_at=2000)
        rows = await self._all()
        self.assertEqual(len(rows), 1)  # 不产生重复行
        self.assertEqual(rows[0]["name"], "群A新名")
        self.assertEqual(rows[0]["is_official"], 1)
        self.assertEqual(rows[0]["enabled"], 1)  # 用户启用状态保留
        self.assertEqual(rows[0]["last_poll_ts"], 1000)  # 水位保留
        self.assertEqual(rows[0]["last_active"], 2000)  # 新元数据写入

    async def test_concurrent_upserts_same_session_no_error(self):
        # 多源并发刷新同一会话：UPSERT 原子执行，不抛主键冲突、不产生重复行
        async def run():
            await self._upsert("weflow-legacy", "s1", f"群{id(self)}", True)

        import asyncio

        await asyncio.gather(run(), run(), run())
        rows = await self._all()
        self.assertEqual(len(rows), 1)


class GetItemTextsByIdsTest(unittest.IsolatedAsyncioTestCase):
    """【复核 P2-18】按 id 取卡片文本（unverify 回加去重缓存的数据源）。"""

    async def asyncSetUp(self):
        self.db = await aiosqlite.connect(":memory:")
        self.db.row_factory = aiosqlite.Row
        await init_schema(self.db)
        for i, title in (("i1", "标题一"), ("i2", "标题二")):
            await self.db.execute(
                "INSERT INTO items (id, category, title, source_quote, source_group, "
                "source, source_msg_id, msg_time, is_verified, created_at) "
                "VALUES (?, '活动通知', ?, '引文', '项目群', 'weflow-legacy', ?, 100, 0, "
                "'2026-01-01T00:00:00+00:00')",
                (i, title, i),
            )
        await self.db.commit()

    async def asyncTearDown(self):
        await self.db.close()

    async def test_returns_rows_in_shape_of_warmup(self):
        with patch("briefdesk.db.get_db", new=AsyncMock(return_value=self.db)):
            rows = await get_item_texts_by_ids(["i1", "missing"])
        self.assertEqual([r["id"] for r in rows], ["i1"], "缺失 id 静默跳过")
        self.assertEqual(rows[0]["title"], "标题一")
        self.assertEqual(rows[0]["source"], "weflow-legacy")
        self.assertEqual(rows[0]["source_quote"], "引文")

    async def test_empty_input_is_noop(self):
        with patch("briefdesk.db.get_db", new=AsyncMock(return_value=self.db)):
            self.assertEqual(await get_item_texts_by_ids([]), [])


class SessionWatermarkTest(unittest.IsolatedAsyncioTestCase):
    """会话水位（增量轮询）读写与未处理消息按会话查询。"""

    async def asyncSetUp(self):
        self.db = await aiosqlite.connect(":memory:")
        self.db.row_factory = aiosqlite.Row
        await self.db.execute("PRAGMA foreign_keys = ON")
        await init_schema(self.db)
        self._db_patch = patch("briefdesk.db.get_db", new=AsyncMock(return_value=self.db))
        self._db_patch.start()
        for sid in ("g1", "g2"):
            await self.db.execute(
                "INSERT INTO sessions (source, session_id, name, is_group, enabled) "
                "VALUES ('weflow-legacy', ?, ?, 1, 0)",
                (sid, sid),
            )
        await self.db.commit()

    async def asyncTearDown(self):
        self._db_patch.stop()
        await self.db.close()

    async def test_get_session_last_polls_none_then_value(self):
        self.assertEqual(
            await get_session_last_polls("weflow-legacy", ["g1", "g2"]),
            {"g1": None, "g2": None},
        )
        await update_session_last_polls("weflow-legacy", [("g1", 100), ("g2", 200)])
        self.assertEqual(
            await get_session_last_polls("weflow-legacy", ["g1", "g2"]),
            {"g1": 100, "g2": 200},
        )
        # 多源隔离：不存在的源/会话不在结果中
        self.assertEqual(await get_session_last_polls("qqflow", ["g1"]), {})

    async def test_get_oldest_unprocessed_by_session(self):
        self.assertEqual(await get_oldest_unprocessed_by_session("weflow-legacy"), {})
        await self.db.execute(
            "INSERT INTO raw_messages (source, msg_id, session_id, group_name, "
            "sender_id, sender_name, content, timestamp) "
            "VALUES ('weflow-legacy', 'f1', 'g1', '群', 'u', 'n', 'x', 100), "
            "('weflow-legacy', 'f2', 'g1', '群', 'u', 'n', 'x', 300), "
            "('weflow-legacy', 'f3', 'g2', '群', 'u', 'n', 'x', 200), "
            "('qqflow', 'q1', 'g1', '群', 'u', 'n', 'x', 50)"
        )
        await self.db.commit()
        # 按会话分组取最早未处理（源隔离，不含 qqflow 的 50）
        self.assertEqual(
            await get_oldest_unprocessed_by_session("weflow-legacy"), {"g1": 100, "g2": 200}
        )
        # 标记 g1 全部已处理 → g1 不再出现
        await mark_message_processed("weflow-legacy", "f1")
        await mark_message_processed("weflow-legacy", "f2")
        self.assertEqual(await get_oldest_unprocessed_by_session("weflow-legacy"), {"g2": 200})

    async def test_mark_messages_processed_bulk(self):
        await self.db.execute(
            "INSERT INTO raw_messages (source, msg_id, session_id, group_name, "
            "sender_id, sender_name, content, timestamp) "
            "VALUES ('weflow-legacy', 'f1', 'g1', '群', 'u', 'n', 'x', 100), "
            "('weflow-legacy', 'f2', 'g1', '群', 'u', 'n', 'x', 300)"
        )
        await self.db.commit()
        # 批量标记（含重复项：INSERT OR IGNORE 幂等）；空表 no-op 不抛
        await mark_messages_processed(
            [("weflow-legacy", "f1"), ("weflow-legacy", "f2"), ("weflow-legacy", "f2")]
        )
        await mark_messages_processed([])
        self.assertEqual(
            await get_oldest_unprocessed_by_session("weflow-legacy"), {}
        )

    async def test_toggle_enable_clears_watermark(self):
        await update_session_last_polls("weflow-legacy", [("g1", 100)])
        # 启用 → 水位清空（NULL = 待回填）
        row = await toggle_session("weflow-legacy", "g1")
        self.assertEqual(row["enabled"], 1)
        self.assertEqual(
            await get_session_last_polls("weflow-legacy", ["g1"]), {"g1": None}
        )
        # 停用 → 不动水位
        await update_session_last_polls("weflow-legacy", [("g1", 100)])
        row = await toggle_session("weflow-legacy", "g1")
        self.assertEqual(row["enabled"], 0)
        self.assertEqual(
            await get_session_last_polls("weflow-legacy", ["g1"]), {"g1": 100}
        )




class MergeHelpersTest(unittest.IsolatedAsyncioTestCase):
    """会话合并 DB 辅助函数：候选查询（窗口/类别/会话/核实态/排除）与合并回写。"""

    async def asyncSetUp(self):
        self.db = await aiosqlite.connect(":memory:")
        self.db.row_factory = aiosqlite.Row
        await self.db.execute("PRAGMA foreign_keys = ON")
        await init_schema(self.db)
        self._db_patch = patch("briefdesk.db.get_db", new=AsyncMock(return_value=self.db))
        self._db_patch.start()

    async def asyncTearDown(self):
        self._db_patch.stop()
        await self.db.close()

    async def _insert(
        self,
        id,
        *,
        ts=100,
        session="s1",
        category="交易",
        source="weflow-legacy",
        is_verified=0,
        title="t",
        quote="q",
    ):
        await self.db.execute(
            "INSERT INTO items (id, category, title, source_quote, source_group, "
            "source, source_msg_id, session_id, msg_time, is_verified, created_at) "
            "VALUES (?, ?, ?, ?, 'g', ?, ?, ?, ?, ?, '2026-08-01')",
            (id, category, title, quote, source, id, session, ts, is_verified),
        )
        await self.db.commit()

    async def test_candidates_window_category_session_verified_filters(self):
        await self._insert("a1", ts=100)  # 命中
        await self._insert("a2", ts=150)  # 命中
        await self._insert("b1", ts=120, category="学术")  # 类别不符
        await self._insert("c1", ts=130, session="s2")  # 会话不符
        await self._insert("d1", ts=120, is_verified=1)  # 已核实不参与
        await self._insert("e1", ts=1000)  # 窗口外
        cands = await get_merge_candidates("weflow-legacy", "s1", "交易", 200, 300, [], 10)
        self.assertEqual([c["id"] for c in cands], ["a1", "a2"])  # msg_time 升序
        cands = await get_merge_candidates("weflow-legacy", "s1", "交易", 200, 300, ["a1"], 10)
        self.assertEqual([c["id"] for c in cands], ["a2"])  # exclude_ids 排除
        cands = await get_merge_candidates("qqflow", "s1", "交易", 200, 300, [], 10)
        self.assertEqual(cands, [])  # 源隔离
        cands = await get_merge_candidates(
            "weflow-legacy", "s1", "交易", 200, 300, [], 2
        )
        self.assertEqual([c["id"] for c in cands], ["a1", "a2"])  # limit 生效
        cands = await get_merge_candidates(
            "weflow-legacy", "s1", "交易", 200, 300, [], 1
        )
        self.assertEqual([c["id"] for c in cands], ["a1"])

    async def test_update_item_merged_rewrites_fields_and_hash(self):
        await self._insert("m1", ts=100, title="旧标题", quote="旧")
        await update_item_merged(
            "m1",
            title="新标题",
            key_info="k1, k2",
            source_quote="合并引文",
            subject="社团",
            start="2026-10-11",
            end="2026-10-12",
            msg_time=90,
            image_urls='["a.jpg"]',
            extra_times='[{"type":"end","time":"2026-08-15","label":"视频"}]',
        )
        cursor = await self.db.execute("SELECT * FROM items WHERE id = 'm1'")
        row = await cursor.fetchone()
        self.assertEqual(row["title"], "新标题")
        self.assertEqual(row["key_info"], "k1, k2")
        self.assertEqual(row["source_quote"], "合并引文")
        self.assertEqual(row["subject"], "社团")
        self.assertEqual(row["start"], "2026-10-11")
        self.assertEqual(row["end"], "2026-10-12")
        self.assertEqual(row["msg_time"], 90)
        self.assertEqual(row["image_urls"], '["a.jpg"]')
        self.assertEqual(row["extra_times"], '[{"type":"end","time":"2026-08-15","label":"视频"}]')
        self.assertEqual(row["content_hash"], hashlib.sha256(
            "合并引文".encode()
        ).hexdigest()[:16])
        self.assertEqual(row["is_verified"], 0)  # 元数据不受影响

    async def test_delete_items_keep_raw_option(self):
        await self._insert("k1", ts=100)
        await self.db.execute(
            "INSERT INTO raw_messages (source, msg_id, session_id, group_name, "
            "sender_id, sender_name, content, timestamp) "
            "VALUES ('weflow-legacy', 'k1', 's1', 'g', 'u', 'n', '原文', 100)"
        )
        await self.db.commit()
        # keep_raw_messages=True（会话合并吸收片段卡用）：保留原文行
        await delete_items(["k1"], keep_raw_messages=True)
        cursor = await self.db.execute("SELECT COUNT(*) AS cnt FROM raw_messages")
        self.assertEqual((await cursor.fetchone())["cnt"], 1)
        cursor = await self.db.execute("SELECT COUNT(*) AS cnt FROM items")
        self.assertEqual((await cursor.fetchone())["cnt"], 0)
        # 默认（用户批量删除等）：原文行随之删除
        await self._insert("k2", ts=110)
        await self.db.execute(
            "INSERT INTO raw_messages (source, msg_id, session_id, group_name, "
            "sender_id, sender_name, content, timestamp) "
            "VALUES ('weflow-legacy', 'k2', 's1', 'g', 'u', 'n', '原文2', 110)"
        )
        await self.db.commit()
        await delete_items(["k2"])
        cursor = await self.db.execute("SELECT COUNT(*) AS cnt FROM raw_messages")
        self.assertEqual((await cursor.fetchone())["cnt"], 1)  # 只剩 k1 的原文


class BulkRawInsertTest(unittest.IsolatedAsyncioTestCase):
    """H1 回归：大批量 raw 落库不得触发 SQLite 变量上限。"""

    async def asyncSetUp(self):
        self.db = await aiosqlite.connect(":memory:")
        self.db.row_factory = aiosqlite.Row
        await init_schema(self.db)

    async def asyncTearDown(self):
        await self.db.close()

    def _msg(self, i: int) -> RawMsgInput:
        return {
            "source": "weflow-legacy",
            "msg_id": f"m{i}",
            "session_id": "s1",
            "group_name": "群",
            "sender_id": "u",
            "sender_name": "n",
            "content": f"内容 {i}",
            "timestamp": 100 + i,
            "article_url": "",
        }

    async def test_large_batch_exceeds_single_statement_limit(self):
        # 5000 行 × 9 参数 = 45000 参数，超过旧实现的单语句上限（32766），
        # 旧实现会抛 "too many SQL variables"；分块后应全部落库。
        msgs = [self._msg(i) for i in range(5000)]
        with patch("briefdesk.db.get_db", new=AsyncMock(return_value=self.db)):
            await bulk_insert_raw_messages(msgs)
        cursor = await self.db.execute("SELECT COUNT(*) AS cnt FROM raw_messages")
        self.assertEqual((await cursor.fetchone())["cnt"], 5000)

    async def test_duplicate_ids_idempotent(self):
        msgs = [self._msg(1), self._msg(1), self._msg(2)]
        with patch("briefdesk.db.get_db", new=AsyncMock(return_value=self.db)):
            await bulk_insert_raw_messages(msgs)
        cursor = await self.db.execute("SELECT COUNT(*) AS cnt FROM raw_messages")
        self.assertEqual((await cursor.fetchone())["cnt"], 2)


class InsertItemConflictTest(unittest.IsolatedAsyncioTestCase):
    """H2 回归：insert_item 唯一键冲突时返回已存在行的真实 id（非幽灵 id）。"""

    async def asyncSetUp(self):
        self.db = await aiosqlite.connect(":memory:")
        self.db.row_factory = aiosqlite.Row
        await init_schema(self.db)

    async def asyncTearDown(self):
        await self.db.close()

    def _item(self, source_msg_id: str = "m") -> dict:
        return {
            "category": "活动通知",
            "title": "测试",
            "key_info": "k",
            "sender_name": "A",
            "source_quote": "quote",
            "source_group": "群",
            "subject": "主体",
            "source": "weflow-legacy",
            "source_msg_id": source_msg_id,
            "session_id": "s1",
            "msg_time": 100,
            "is_verified": 0,
            "content_hash": "h",
        }

    async def test_conflict_returns_existing_real_id(self):
        with patch("briefdesk.db.get_db", new=AsyncMock(return_value=self.db)):
            first = await insert_item(self._item())
            second = await insert_item(self._item())  # 同 (source, source_msg_id)
        # 两次返回同一 id，且该 id 真实存在于 items 表
        self.assertEqual(first, second)
        cursor = await self.db.execute("SELECT COUNT(*) AS cnt FROM items")
        self.assertEqual((await cursor.fetchone())["cnt"], 1)
        cursor = await self.db.execute("SELECT id FROM items WHERE source='weflow-legacy' AND source_msg_id='m'")
        row = await cursor.fetchone()
        self.assertEqual(second, row["id"])

    async def test_distinct_msg_ids_get_distinct_ids(self):
        with patch("briefdesk.db.get_db", new=AsyncMock(return_value=self.db)):
            a = await insert_item(self._item("m1"))
            b = await insert_item(self._item("m2"))
        self.assertNotEqual(a, b)


class RecatLogTest(unittest.IsolatedAsyncioTestCase):
    """分类修正样本积累：update_item_category 记录 before/after，可导出。"""

    async def asyncSetUp(self):
        self.db = await aiosqlite.connect(":memory:")
        self.db.row_factory = aiosqlite.Row
        await init_schema(self.db)

    async def asyncTearDown(self):
        await self.db.close()

    def _item(self) -> dict:
        return {
            "category": "活动通知",
            "title": "测试",
            "key_info": "k",
            "sender_name": "A",
            "source_quote": "原文内容（已脱敏）",
            "source_group": "群",
            "subject": "主体",
            "source": "weflow-legacy",
            "source_msg_id": "m1",
            "session_id": "s1",
            "msg_time": 100,
            "is_verified": 0,
            "content_hash": "h",
        }

    async def test_category_change_logged(self):
        with patch("briefdesk.db.get_db", new=AsyncMock(return_value=self.db)):
            item_id = await insert_item(self._item())
            await update_item_category(item_id, "学术")
        cursor = await self.db.execute("SELECT * FROM recat_log")
        rows = await cursor.fetchall()
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["item_id"], item_id)
        self.assertEqual(r["category_before"], "活动通知")
        self.assertEqual(r["category_after"], "学术")
        self.assertEqual(r["content"], "原文内容（已脱敏）")

    async def test_same_category_not_logged(self):
        with patch("briefdesk.db.get_db", new=AsyncMock(return_value=self.db)):
            item_id = await insert_item(self._item())
            await update_item_category(item_id, "活动通知")  # 相同类别
        cursor = await self.db.execute("SELECT COUNT(*) AS cnt FROM recat_log")
        self.assertEqual((await cursor.fetchone())["cnt"], 0)

    async def test_get_recat_samples_only_real_changes(self):
        with patch("briefdesk.db.get_db", new=AsyncMock(return_value=self.db)):
            a = await insert_item(self._item())
            b = await insert_item({**self._item(), "source_msg_id": "m2"})
            await update_item_category(a, "学术")      # 记日志
            await update_item_category(b, "活动通知")  # 未变不记
            samples = await get_recat_samples()
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0]["category_after"], "学术")


class BackupRestoreTest(unittest.IsolatedAsyncioTestCase):
    """在线备份 + 恢复校验 + 启动替换（临时文件，不触碰应用库）。"""

    async def asyncSetUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.old_db_path = config.db_path
        self.main_path = os.path.join(self.tmpdir, "main.sqlite")
        self.bak_path = os.path.join(self.tmpdir, "bak.sqlite")

    async def asyncTearDown(self):
        config.db_path = self.old_db_path
        await asyncio.to_thread(shutil.rmtree, self.tmpdir, ignore_errors=True)

    async def _build_db(self, path: str, marker: str) -> None:
        conn = await aiosqlite.connect(path)
        try:
            conn.row_factory = aiosqlite.Row
            await init_schema(conn)
            with patch("briefdesk.db.get_db", new=AsyncMock(return_value=conn)):
                await insert_item({
                    "category": "活动通知", "title": marker,
                    "key_info": "k", "sender_name": "A", "source_quote": marker,
                    "source_group": "群", "subject": "主体", "source": "weflow-legacy",
                    "source_msg_id": marker, "session_id": "s1", "msg_time": 1,
                    "is_verified": 0, "content_hash": "h",
                })
            await conn.commit()
        finally:
            await conn.close()

    async def test_backup_and_validate(self):
        await self._build_db(self.main_path, "主库")
        src = await aiosqlite.connect(self.main_path)
        try:
            src.row_factory = aiosqlite.Row
            with patch("briefdesk.db.get_db", new=AsyncMock(return_value=src)):
                await backup_db_to(self.bak_path)
        finally:
            await src.close()
        self.assertIsNone(await validate_restore_file(self.bak_path))
        conn = await aiosqlite.connect(self.bak_path)
        try:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute("SELECT COUNT(*) AS cnt FROM items")
            self.assertEqual((await cursor.fetchone())["cnt"], 1)
        finally:
            await conn.close()

    async def test_apply_pending_restore_replaces_main(self):
        await self._build_db(self.main_path, "主库A")
        await self._build_db(self.bak_path, "备份B")

        async def _titles(path: str) -> list[str]:
            conn = await aiosqlite.connect(path)
            try:
                conn.row_factory = aiosqlite.Row
                cursor = await conn.execute("SELECT title FROM items")
                return [r["title"] for r in await cursor.fetchall()]
            finally:
                await conn.close()

        self.assertEqual(await _titles(self.main_path), ["主库A"])
        shutil.copyfile(self.bak_path, self.main_path + ".restore-pending")
        config.db_path = self.main_path
        self.assertTrue(await apply_pending_restore())
        self.assertFalse(os.path.exists(self.main_path + ".restore-pending"))
        self.assertEqual(await _titles(self.main_path), ["备份B"])

    async def test_invalid_pending_ignored(self):
        await self._build_db(self.main_path, "主库A")

        def _write_pending() -> None:
            with open(self.main_path + ".restore-pending", "w", encoding="utf-8") as f:
                f.write("not a sqlite file")

        await asyncio.to_thread(_write_pending)
        config.db_path = self.main_path
        self.assertFalse(await apply_pending_restore())
        self.assertFalse(os.path.exists(self.main_path + ".restore-pending"))
        conn = await aiosqlite.connect(self.main_path)
        try:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute("SELECT COUNT(*) AS cnt FROM items")
            self.assertEqual((await cursor.fetchone())["cnt"], 1)
        finally:
            await conn.close()


class RestoreEmptyDbRejectedTest(unittest.IsolatedAsyncioTestCase):
    """空库（无应用表）不可通过恢复校验，防止空库覆盖正式数据。"""

    async def test_empty_db_rejected(self):
        with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
            path = f.name
        try:
            conn = await aiosqlite.connect(path)
            await conn.close()  # 全新空库：integrity_check=ok 但无应用表
            err = await validate_restore_file(path)
            self.assertIsNotNone(err)
            self.assertIn("不含应用数据表", err)
        finally:
            await asyncio.to_thread(os.unlink, path)

    async def test_partial_db_with_items_table_rejected(self):
        # 只有一张非期望表（如用户自建表）的库同样拒绝——必须含应用期望表
        with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
            path = f.name
        try:
            conn = await aiosqlite.connect(path)
            await conn.execute("CREATE TABLE my_own (id INTEGER)")
            await conn.commit()
            await conn.close()
            err = await validate_restore_file(path)
            self.assertIsNotNone(err)
        finally:
            await asyncio.to_thread(os.unlink, path)


class GetItemTextsTest(unittest.IsolatedAsyncioTestCase):
    """get_all_item_texts 返回真实原文列（source_quote，不拼接其它字段）。"""

    async def asyncSetUp(self):
        self.db = await aiosqlite.connect(":memory:")
        self.db.row_factory = aiosqlite.Row
        await init_schema(self.db)
        self._db_patch = patch("briefdesk.db.get_db", new=AsyncMock(return_value=self.db))
        self._db_patch.start()

    async def asyncTearDown(self):
        self._db_patch.stop()
        await self.db.close()

    async def test_source_quote_is_real_column_not_concatenated(self):
        await insert_item({
            "category": "活动通知", "title": "标题X",
            "key_info": "k", "sender_name": "A", "source_quote": "原文Z",
            "source_group": "群", "subject": "主体", "source": "weflow-legacy",
            "source_msg_id": "m1", "session_id": "s1", "msg_time": 1,
            "is_verified": 0, "content_hash": "h",
        })
        rows = await get_all_item_texts()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "标题X")
        # source_quote 为真实原文列（不拼接 title 等字段）
        self.assertEqual(rows[0]["source_quote"], "原文Z")


class MergeVectorCleanupTest(unittest.IsolatedAsyncioTestCase):
    """合并回写（update_item_merged）后旧向量被删除，防重启语义漂移。"""

    async def asyncSetUp(self):
        # 主连接与 embed 连接需共享同一数据库（:memory: 每连接独立，删除不可见）
        fd, path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        self.path = path
        self.db = await aiosqlite.connect(path)
        self.db.row_factory = aiosqlite.Row
        self.embed_db = await aiosqlite.connect(path)
        self.embed_db.row_factory = aiosqlite.Row
        await init_schema(self.db)
        await init_schema(self.embed_db)
        self._db_patch = patch("briefdesk.db.get_db", new=AsyncMock(return_value=self.db))
        self._db_patch.start()
        self._embed_patch = patch(
            "briefdesk.db.get_embed_db", new=AsyncMock(return_value=self.embed_db)
        )
        self._embed_patch.start()

    async def asyncTearDown(self):
        self._db_patch.stop()
        self._embed_patch.stop()
        await self.db.close()
        await self.embed_db.close()
        await asyncio.to_thread(os.unlink, self.path)

    async def test_merged_item_embedding_removed(self):
        item_id = await insert_item({
            "category": "活动通知", "title": "旧标题",
            "key_info": "k", "sender_name": "A", "source_quote": "旧原文",
            "source_group": "群", "subject": "主体", "source": "weflow-legacy",
            "source_msg_id": "m1", "session_id": "s1", "msg_time": 1,
            "is_verified": 0, "content_hash": "h",
        })
        await upsert_embeddings([(item_id, "test-model", [0.1, 0.2])])
        self.assertIn(item_id, await load_embeddings("test-model"))

        await update_item_merged(
            item_id,
            title="新标题", key_info="k",
            source_quote="新原文", subject="主体", start="", end="",
            msg_time=1, image_urls="",
        )
        # 合并改写了文本 → 旧向量删除，重启后按新文本重算
        self.assertNotIn(item_id, await load_embeddings("test-model"))


class SourceGroupsSplitTest(unittest.IsolatedAsyncioTestCase):
    """多来源合并卡片的 source_group 在来源筛选下拉中按 ", " 拆分展示。"""

    async def asyncSetUp(self):
        self.db = await aiosqlite.connect(":memory:")
        self.db.row_factory = aiosqlite.Row
        await init_schema(self.db)

    async def asyncTearDown(self):
        await self.db.close()

    async def _insert(self, item_id: str, source_group: str) -> None:
        await self.db.execute(
            "INSERT INTO items (id, category, title, source_quote, source_group, "
            "source, source_msg_id, msg_time, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (item_id, "活动通知", "标题", "原文", source_group, "weflow-legacy", item_id, 100,
             "2026-08-18T00:00:00+00:00"),
        )
        await self.db.commit()

    async def test_source_groups_split_and_dedup(self):
        await self._insert("a", "群A")
        await self._insert("b", "群A, 群B")   # 多来源合并
        await self._insert("c", "群B, 群C")
        # 来源下拉选项仅在搜索模式（q 非空）下随分页返回
        with patch("briefdesk.db.get_db", new=AsyncMock(return_value=self.db)):
            page = await get_items_page(category=None, verified="unverified", q="标题")
        self.assertEqual(page["source_groups"], ["群A", "群B", "群C"])

    async def test_source_group_filter_matches_merged(self):
        await self._insert("a", "群A")
        await self._insert("b", "群A, 群B")   # 选中"群A"应命中合并卡片
        await self._insert("c", "群B")
        with patch("briefdesk.db.get_db", new=AsyncMock(return_value=self.db)):
            page = await get_items_page(
                category=None, verified="unverified", q=None, source_group="群A"
            )
        self.assertEqual(sorted(r["id"] for r in page["items"]), ["a", "b"])


class MergeSourceGroupTest(unittest.IsolatedAsyncioTestCase):
    """merge_source_group：逗号分隔、精确匹配去重（C3：群名互为子串不误判）。"""

    async def asyncSetUp(self):
        self.db = await aiosqlite.connect(":memory:")
        self.db.row_factory = aiosqlite.Row
        await init_schema(self.db)

    async def asyncTearDown(self):
        await self.db.close()

    async def _insert(self, item_id: str, source_group: str) -> None:
        await self.db.execute(
            "INSERT INTO items (id, category, title, source_quote, source_group, "
            "source, source_msg_id, msg_time, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (item_id, "活动通知", "标题", "原文", source_group, "weflow-legacy", item_id, 100,
             "2026-08-18T00:00:00+00:00"),
        )
        await self.db.commit()

    async def _get_group(self, item_id: str) -> str:
        cursor = await self.db.execute(
            "SELECT source_group FROM items WHERE id = ?", (item_id,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        return row["source_group"]

    async def test_substring_names_are_not_treated_as_present(self):
        """C3：群名互为子串（"我们四个" vs "我们四个2"）→ 追加而非跳过。"""
        await self._insert("a", "我们四个")
        with patch("briefdesk.db.get_db", new=AsyncMock(return_value=self.db)):
            await merge_source_group("a", "我们四个2")
        self.assertEqual(await self._get_group("a"), "我们四个, 我们四个2")

    async def test_exact_duplicate_is_skipped(self):
        """已存在的群名（精确匹配）不重复追加。"""
        await self._insert("a", "群A, 群B")
        with patch("briefdesk.db.get_db", new=AsyncMock(return_value=self.db)):
            await merge_source_group("a", "群B")
        self.assertEqual(await self._get_group("a"), "群A, 群B")

    async def test_first_group_writes_without_separator(self):
        """空 source_group 首写：不产生多余分隔符。"""
        await self._insert("a", "")
        with patch("briefdesk.db.get_db", new=AsyncMock(return_value=self.db)):
            await merge_source_group("a", "群A")
        self.assertEqual(await self._get_group("a"), "群A")

    async def test_missing_item_is_noop(self):
        """目标卡不存在 → 静默跳过。"""
        with patch("briefdesk.db.get_db", new=AsyncMock(return_value=self.db)):
            await merge_source_group("nope", "群A")  # 不抛错


class EmbeddingsDbTest(unittest.IsolatedAsyncioTestCase):
    """向量持久化：独立连接读写、并发读取不干扰落库、close_db 重建（临时文件库）。

    回归目标：向量落库的 COMMIT 不再被主连接上的活动语句打断
    （cannot commit transaction - SQL statements in progress）。
    """

    async def asyncSetUp(self):
        import briefdesk.db as db_module

        self.tmpdir = tempfile.mkdtemp()
        self.old_db_path = config.db_path
        config.db_path = os.path.join(self.tmpdir, "embed.sqlite")
        # 复位模块级单例，确保真实走临时库
        self._old_db = db_module._db
        self._old_embed_db = db_module._embed_db
        db_module._db = None
        db_module._embed_db = None

    async def asyncTearDown(self):
        import briefdesk.db as db_module

        await close_db()
        db_module._db = self._old_db
        db_module._embed_db = self._old_embed_db
        config.db_path = self.old_db_path
        await asyncio.to_thread(shutil.rmtree, self.tmpdir, ignore_errors=True)

    async def test_upsert_load_roundtrip(self):
        await get_db()  # 主连接初始化 schema
        await upsert_embeddings([("a", "m1", [0.1, 0.2]), ("b", "m1", [0.3])])
        loaded = await load_embeddings("m1")
        self.assertEqual(set(loaded), {"a", "b"})
        self.assertEqual(loaded["a"], [0.1, 0.2])
        # 模型过滤
        self.assertEqual(await load_embeddings("m2"), {})
        # REPLACE 按 item_id 覆盖
        await upsert_embeddings([("a", "m1", [9.9])])
        self.assertEqual((await load_embeddings("m1"))["a"], [9.9])

    async def test_concurrent_read_does_not_break_upsert(self):
        await get_db()
        await upsert_embeddings([(f"id{i}", "m", [float(i)]) for i in range(50)])
        db = await get_db()

        # 主连接长读：逐行让出事件循环，模拟 pipeline async-for 查询进行中
        async def reader() -> list[str]:
            cursor = await db.execute(
                "SELECT item_id FROM item_embeddings ORDER BY item_id"
            )
            try:
                out: list[str] = []
                while True:
                    row = await cursor.fetchone()
                    if row is None:
                        break
                    out.append(row["item_id"])
                    await asyncio.sleep(0)
                return out
            finally:
                await cursor.close()

        async def writer() -> None:
            await asyncio.sleep(0)  # 保证与读取交错
            await upsert_embeddings(
                [(f"new{i}", "m", [float(i)]) for i in range(100)]
            )

        ids, _ = await asyncio.gather(reader(), writer())
        # WAL 快照：读取可能看到部分/全部新行，重点是不抛错且最终落库完整
        self.assertTrue(50 <= len(ids) <= 150, len(ids))
        self.assertEqual(len(await load_embeddings("m")), 150)

    async def test_close_db_recreates_embed_connection(self):
        await get_db()
        await upsert_embeddings([("a", "m", [1.0])])
        await close_db()
        # 关闭后按需重建新连接，数据仍在（同一库文件）
        self.assertEqual((await load_embeddings("m"))["a"], [1.0])

    async def test_close_db_closes_main_even_if_embed_close_fails(self):
        """【核验 H3】_embed_db.close 抛错不得阻断 _db.close：残留的非 daemon
        worker 线程会让解释器退出挂死（与关闭路径要防的故障同源），且两个
        全局引用都必须置 None，保证后续按需重建不悬挂旧连接。"""
        import briefdesk.db as db_module

        await get_db()  # 主连接
        await upsert_embeddings([("a", "m", [1.0])])  # 向量连接

        embed_db = db_module._embed_db
        main_db = db_module._db
        self.assertIsNotNone(embed_db)
        self.assertIsNotNone(main_db)

        with (
            patch.object(
                embed_db, "close", side_effect=RuntimeError("模拟 close 失败")
            ),
            patch.object(main_db, "close") as main_close_mock,
        ):
            await close_db()

        main_close_mock.assert_awaited_once()
        self.assertIsNone(db_module._embed_db)
        self.assertIsNone(db_module._db)


# ── 审查修复回归测试（内存库，不触碰应用数据库文件）──


class _InMemoryDbTest(unittest.IsolatedAsyncioTestCase):
    """公共基座：内存库 + get_db/get_embed_db 打桩到该连接。"""

    async def asyncSetUp(self):
        self.db = await aiosqlite.connect(":memory:")
        self.db.row_factory = aiosqlite.Row
        await init_schema(self.db)

    async def asyncTearDown(self):
        await self.db.close()

    def _patch_db(self):
        return (
            patch("briefdesk.db.get_db", new=AsyncMock(return_value=self.db)),
            patch("briefdesk.db.get_embed_db", new=AsyncMock(return_value=self.db)),
        )

    @staticmethod
    def _item(category: str, source_msg_id: str = "m1") -> dict:
        return {
            "category": category,
            "title": "测试卡片",
            "key_info": "k",
            "sender_name": "发送者",
            "source_quote": "quote",
            "source_group": "群A",
            "subject": "主体",
            "source": "weflow-legacy",
            "source_msg_id": source_msg_id,
            "session_id": "s1",
            "msg_time": 100,
            "is_verified": 0,
            "content_hash": "h",
        }


class CategoryRenameSyncTest(_InMemoryDbTest):
    """审查修复 #1 回归保护：类别改名必须在同一事务内同步 items.category。"""

    async def test_rename_updates_items_category_in_same_transaction(self):
        p1, p2 = self._patch_db()
        with p1, p2:
            cat = await insert_category("旧类", "提示词", "#111111")
            await insert_item(self._item("旧类"))
            updated = await update_category(cat["id"], name="新类")
        self.assertIsNotNone(updated)
        self.assertEqual(updated["name"], "新类")
        cur = await self.db.execute(
            "SELECT COUNT(*) AS cnt FROM items WHERE category = '新类'"
        )
        self.assertEqual((await cur.fetchone())["cnt"], 1)
        cur = await self.db.execute(
            "SELECT COUNT(*) AS cnt FROM items WHERE category = '旧类'"
        )
        self.assertEqual((await cur.fetchone())["cnt"], 0)


class DeleteCategoryPurgeCascadeTest(_InMemoryDbTest):
    """审查修复 #1 回归保护：purge 级联删三表且 processed_messages 保留。"""

    async def test_purge_deletes_items_raw_embeddings_keeps_processed(self):
        p1, p2 = self._patch_db()
        with p1, p2:
            cat = await insert_category("审查级联类", "提示词", "#222222")
            item_id = await insert_item(self._item("审查级联类"))
            await bulk_insert_raw_messages(
                [
                    {
                        "source": "weflow-legacy",
                        "msg_id": "m1",
                        "session_id": "s1",
                        "group_name": "群A",
                        "sender_id": "u1",
                        "sender_name": "发送者",
                        "content": "原文",
                        "timestamp": 100,
                    }
                ]
            )
            await mark_message_processed("weflow-legacy", "m1")
            await upsert_embeddings([(item_id, "embed-model", [0.1, 0.2])])
            row, deleted_ids = await delete_category(cat["id"], purge_items=True)
        self.assertIsNotNone(row)
        self.assertEqual(deleted_ids, [item_id])
        for table in ("items", "raw_messages", "item_embeddings"):
            cur = await self.db.execute(f"SELECT COUNT(*) AS cnt FROM {table}")
            self.assertEqual((await cur.fetchone())["cnt"], 0, table)
        cur = await self.db.execute("SELECT COUNT(*) AS cnt FROM processed_messages")
        self.assertEqual((await cur.fetchone())["cnt"], 1)


class DeleteItemsRollbackTest(_InMemoryDbTest):
    """审查修复 #1a：多步写异常路径必须 rollback，不留悬挂事务。

    悬挂事务会被下一个不相干写操作的 commit 收尾提交（部分写入提前可见），
    因此异常路径 rollback 后连接必须回到无事务状态。

    注：本机 SQLite >=3.32 变量上限默认 250000，无法靠超长 id 列表触发
    "too many SQL variables"，故用故障注入让多步写的某一步抛错。

    覆盖 delete_items 之外的三条多步写链路：purge_expired_ignored、
    update_category（改名 + items 同步）、delete_category（purge 级联）。
    """

    async def test_failed_multi_step_delete_leaves_no_open_transaction(self):
        import briefdesk.db as db_mod

        orig_cursor = db_mod._cursor

        def failing_cursor(db, sql, params=()):
            if sql.startswith("DELETE FROM items WHERE id IN"):
                raise sqlite3.OperationalError("injected: final delete failed")
            return orig_cursor(db, sql, params)

        # 先放两张可命中的卡与原文，前两步 DELETE 真实生效、事务已开
        with patch("briefdesk.db.get_db", new=AsyncMock(return_value=self.db)):
            item_id = await insert_item(self._item("活动通知"))
            await bulk_insert_raw_messages(
                [
                    {
                        "source": "weflow-legacy",
                        "msg_id": "m1",
                        "session_id": "s1",
                        "group_name": "群A",
                        "sender_id": "u1",
                        "sender_name": "发送者",
                        "content": "原文",
                        "timestamp": 100,
                    }
                ]
            )
            with (
                patch.object(db_mod, "_cursor", new=failing_cursor),
                self.assertRaises(sqlite3.OperationalError),
            ):
                await delete_items([item_id])
        self.assertFalse(
            self.db.in_transaction, "多步写失败后必须 rollback，不得残留悬挂事务"
        )
        # 回滚后数据完好：items/raw_messages 行仍在，连接可用
        cur = await self.db.execute("SELECT COUNT(*) AS cnt FROM items")
        self.assertEqual((await cur.fetchone())["cnt"], 1)
        cur = await self.db.execute("SELECT COUNT(*) AS cnt FROM raw_messages")
        self.assertEqual((await cur.fetchone())["cnt"], 1)

    # ── 故障注入扩展：db.execute 第 N 步抛 RuntimeError ──

    def _fail_execute_on(self, needle: str):
        """返回 patcher：SQL 含 needle 的 db.execute 调用注入 RuntimeError。

        与 _cursor 注入互补——update_category 等函数的中间步不走 _cursor。
        """
        orig_execute = self.db.execute

        async def failing_execute(sql, params=()):
            if needle in sql:
                raise RuntimeError(f"injected: {needle} failed")
            return await orig_execute(sql, params)

        return patch.object(self.db, "execute", new=failing_execute)

    async def test_purge_expired_ignored_failure_rolls_back(self):
        """purge 第 2 步（删 raw_messages）失败 → 异常上抛，三表均未部分写入。"""
        p1, p2 = self._patch_db()
        with p1, p2:
            item_id = await insert_item(self._item("活动通知"))
            await upsert_embeddings([(item_id, "embed-model", [0.1, 0.2])])
            await bulk_insert_raw_messages(
                [
                    {
                        "source": "weflow-legacy",
                        "msg_id": "m1",
                        "session_id": "s1",
                        "group_name": "群A",
                        "sender_id": "u1",
                        "sender_name": "发送者",
                        "content": "原文",
                        "timestamp": 100,
                    }
                ]
            )
            # 目标行置为已忽略且过期（verified_at 远早于 cutoff）
            await self.db.execute(
                "UPDATE items SET is_verified = -1, verified_at = ? WHERE id = ?",
                ("2000-01-01T00:00:00+00:00", item_id),
            )
            await self.db.commit()
            with (
                self._fail_execute_on("DELETE FROM raw_messages"),
                self.assertRaises(RuntimeError),
            ):
                await purge_expired_ignored(24)
        self.assertFalse(
            self.db.in_transaction, "purge 失败后必须 rollback，不得残留悬挂事务"
        )
        for table in ("items", "raw_messages", "item_embeddings"):
            cur = await self.db.execute(f"SELECT COUNT(*) AS cnt FROM {table}")
            self.assertEqual((await cur.fetchone())["cnt"], 1, table)

    async def test_update_category_failure_rolls_back_rename_sync(self):
        """改名第 2 步（items 同步）失败 → 异常上抛，类别名与卡片均保持旧值。"""
        p1, p2 = self._patch_db()
        with p1, p2:
            cat = await insert_category("旧类", "提示词", "#111111")
            await insert_item(self._item("旧类"))
            with (
                self._fail_execute_on("UPDATE items SET category"),
                self.assertRaises(RuntimeError),
            ):
                await update_category(cat["id"], name="新类")
        self.assertFalse(
            self.db.in_transaction, "改名失败后必须 rollback，不得残留悬挂事务"
        )
        cur = await self.db.execute("SELECT name FROM categories WHERE id = ?", (cat["id"],))
        self.assertEqual((await cur.fetchone())["name"], "旧类")
        cur = await self.db.execute(
            "SELECT COUNT(*) AS cnt FROM items WHERE category = '旧类'"
        )
        self.assertEqual((await cur.fetchone())["cnt"], 1)

    async def test_delete_category_failure_rolls_back_cascade(self):
        """级联删除在 items 删除步失败 → 异常上抛，categories 行仍在、items 未删。"""
        p1, p2 = self._patch_db()
        with p1, p2:
            cat = await insert_category("级联回滚类", "提示词", "#333333")
            await insert_item(self._item("级联回滚类"))
            await bulk_insert_raw_messages(
                [
                    {
                        "source": "weflow-legacy",
                        "msg_id": "m1",
                        "session_id": "s1",
                        "group_name": "群A",
                        "sender_id": "u1",
                        "sender_name": "发送者",
                        "content": "原文",
                        "timestamp": 100,
                    }
                ]
            )
            with (
                self._fail_execute_on("DELETE FROM items WHERE id IN"),
                self.assertRaises(RuntimeError),
            ):
                await delete_category(cat["id"], purge_items=True)
        self.assertFalse(
            self.db.in_transaction, "级联删除失败后必须 rollback，不得残留悬挂事务"
        )
        cur = await self.db.execute(
            "SELECT COUNT(*) AS cnt FROM categories WHERE id = ?", (cat["id"],)
        )
        self.assertEqual((await cur.fetchone())["cnt"], 1, "类别行必须仍在")
        for table in ("items", "raw_messages"):
            cur = await self.db.execute(f"SELECT COUNT(*) AS cnt FROM {table}")
            self.assertEqual((await cur.fetchone())["cnt"], 1, table)


class AreMessagesProcessedChunkTest(unittest.IsolatedAsyncioTestCase):
    """审查修复 #6：are_messages_processed 按 900 条分块防 SQLite 变量上限。

    本机 SQLite 3.51 变量上限为 32766（>=3.32），1100 个 id 单次查询不会崩，
    崩溃路径的 RED 无法在本机复现；故直接对分块行为做参数化断言。
    """

    async def test_chunks_queries_to_900_ids_max(self):
        calls: list[tuple] = []

        async def fake_fetchall(db, sql, params=()):
            calls.append(params)
            return [{"msg_id": p} for p in params[1:]]

        ids = [f"m{i}" for i in range(1100)]
        with patch("briefdesk.db.get_db", new=AsyncMock()), patch(
            "briefdesk.db._fetchall", new=fake_fetchall
        ):
            got = await are_messages_processed("weflow-legacy", ids)

        self.assertEqual(got, set(ids))
        self.assertEqual(len(calls), 2)  # ceil(1100/900) = 2 次
        for params in calls:
            self.assertLessEqual(len(params) - 1, 900)

    async def test_small_batch_single_query_unchanged(self):
        calls: list[tuple] = []

        async def fake_fetchall(db, sql, params=()):
            calls.append(params)
            return [{"msg_id": p} for p in params[1:]]

        ids = ["a", "b"]
        with patch("briefdesk.db.get_db", new=AsyncMock()), patch(
            "briefdesk.db._fetchall", new=fake_fetchall
        ):
            got = await are_messages_processed("weflow-legacy", ids)

        self.assertEqual(got, {"a", "b"})
        self.assertEqual(len(calls), 1)


class AreMessagesProcessedLargeSetTest(_InMemoryDbTest):
    """审查修复 #6 端到端：1100 个 id 全部已处理 → 返回全集（首跑即绿的保护性测试）。"""

    async def test_1100_processed_ids_all_found(self):
        with patch("briefdesk.db.get_db", new=AsyncMock(return_value=self.db)):
            for i in range(1100):
                await mark_message_processed("weflow-legacy", f"m{i}")
            got = await are_messages_processed("weflow-legacy", [f"m{i}" for i in range(1100)])
        self.assertEqual(len(got), 1100)


class SetItemReminderClearMutexTest(_InMemoryDbTest):
    """审查修复 #7：清除提醒仅在已有提醒时命中——rowcount 才能当多标签页互斥判据。"""

    async def test_clear_when_no_reminder_returns_false(self):
        with patch("briefdesk.db.get_db", new=AsyncMock(return_value=self.db)):
            item_id = await insert_item(self._item("活动通知"))
            changed = await set_item_reminder(item_id, None)
        self.assertFalse(changed, "无提醒时清除不应命中（否则多标签页互斥失效）")

    async def test_clear_existing_then_second_clear_false(self):
        with patch("briefdesk.db.get_db", new=AsyncMock(return_value=self.db)):
            item_id = await insert_item(self._item("活动通知"))
            self.assertTrue(await set_item_reminder(item_id, "2026-08-15 10:00"))
            first_clear = await set_item_reminder(item_id, None)
            second_clear = await set_item_reminder(item_id, None)
        self.assertTrue(first_clear)
        self.assertFalse(second_clear, "第二次清除不得命中（已被第一个标签页清掉）")


class DefaultCategoriesUpgradeTest(_InMemoryDbTest):
    """默认分类 5→13 升级：出厂启用态 + 存量库 user_version 一次性回填。

    契约：新装库播种 13 类但仅原五类启用；存量库经回填补齐缺失默认类
    （新增八类以停用态入库），绝不改写已有行；迁移只跑一次，此后删除被尊重。
    """

    _ORIGINAL_FIVE = ("活动通知", "社团招新", "学术", "交易", "实习")
    _NEW_EIGHT = ("失物招领", "求助互助", "组队拼团", "兼职家教",
                  "免费福利", "房屋租售", "志愿公益", "奖助申报")

    async def _load_enabled_map(self) -> dict:
        cursor = await self.db.execute("SELECT name, enabled FROM categories")
        return {r["name"]: r["enabled"] for r in await cursor.fetchall()}

    async def test_fresh_seed_enables_only_original_five(self):
        rows = await self._load_enabled_map()
        self.assertEqual(len(rows), 13)
        enabled = {n for n, e in rows.items() if e}
        self.assertEqual(enabled, set(self._ORIGINAL_FIVE))

    async def test_backfill_adds_missing_disabled_respects_existing(self):
        # 模拟升级前旧库：删掉新增八类；用户曾手动禁用"交易"；重置迁移标记
        await self.db.execute("DELETE FROM categories WHERE enabled = 0")
        await self.db.execute("UPDATE categories SET enabled = 0 WHERE name = '交易'")
        await self.db.execute("PRAGMA user_version = 0")
        await self.db.commit()
        await init_schema(self.db)  # 触发一次性回填
        rows = await self._load_enabled_map()
        self.assertEqual(len(rows), 13)
        self.assertEqual(rows["交易"], 0)   # 已有行绝不被回填改写
        for n in self._NEW_EIGHT:
            self.assertEqual(rows[n], 0, n)  # 补入项按出厂态停用
        for n in ("活动通知", "社团招新", "学术", "实习"):
            self.assertEqual(rows[n], 1, n)

    async def test_backfill_runs_once_and_respects_deletion(self):
        await self.db.execute("DELETE FROM categories WHERE name = '失物招领'")
        await self.db.commit()
        await init_schema(self.db)  # user_version 已置位 → 不再回填
        cursor = await self.db.execute(
            "SELECT COUNT(*) AS cnt FROM categories WHERE name = '失物招领'"
        )
        row = await cursor.fetchone()
        self.assertEqual(row["cnt"], 0, "迁移完成后用户的删除必须被尊重")

    async def test_activity_notice_prompt_migrates_once_respecting_edits(self):
        # C3：活动通知口径 v1→v2——旧版原文才更新、已编辑行不动、只跑一次
        from briefdesk.db import (
            _ACTIVITY_NOTICE_NEW_PROMPT,
            _ACTIVITY_NOTICE_OLD_PROMPT,
        )

        async def _notice_prompt() -> str:
            cursor = await self.db.execute(
                "SELECT prompt FROM categories WHERE name = '活动通知'"
            )
            row = await cursor.fetchone()
            return row["prompt"]

        await self.db.execute("PRAGMA user_version = 1")
        await self.db.execute(
            "UPDATE categories SET prompt = ? WHERE name = '活动通知'",
            (_ACTIVITY_NOTICE_OLD_PROMPT,),
        )
        await self.db.commit()
        await init_schema(self.db)
        self.assertEqual(await _notice_prompt(), _ACTIVITY_NOTICE_NEW_PROMPT)

        # 只跑一次：此后（含用户改回旧文案）不再被覆盖
        await self.db.execute(
            "UPDATE categories SET prompt = ? WHERE name = '活动通知'",
            (_ACTIVITY_NOTICE_OLD_PROMPT,),
        )
        await self.db.commit()
        await init_schema(self.db)
        self.assertEqual(await _notice_prompt(), _ACTIVITY_NOTICE_OLD_PROMPT)


if __name__ == "__main__":
    unittest.main()
