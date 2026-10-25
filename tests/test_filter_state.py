"""filter_state 可选表与助手的 DB 级测试（内存库，每用例自建自关）。

覆盖：幂等补建（不进 EXPECTED_SCHEMA）、uncertain 递增与 adapter_version
建行锚定、翻案清行、计数盘点。

close 纪律说明：aiosqlite 连接的 worker 线程非 daemon，解释器退出时
threading._shutdown 会等它——连接未 close（或 close 未送达）整个进程挂死。
本文件每用例自建连接、try/finally 自关，不依赖共享夹具的 teardown 时序
（tests/test_db.py 的用例级 close 同款理由）。
"""

from unittest.mock import AsyncMock, patch

import aiosqlite

from briefdesk.db import (
    clear_filter_state,
    count_filter_state,
    get_filter_state,
    init_schema,
    note_filter_uncertain,
    validate_schema,
)


async def _fresh_db():
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await init_schema(db)
    return db


def _patched(db):
    return (
        patch("briefdesk.db.get_db", new=AsyncMock(return_value=db)),
        patch("briefdesk.db.get_embed_db", new=AsyncMock(return_value=db)),
    )


class TestFilterStateSchema:
    async def test_optional_table_not_in_expected_schema(self):
        db = await _fresh_db()
        try:
            # 可选表不进 EXPECTED_SCHEMA：validate_schema 不因它 FATAL（严格
            # 纪律——新表一律可选表模式，违者旧库启动即崩）
            await validate_schema(db)
        finally:
            await db.close()

    async def test_init_schema_idempotent(self):
        db = await _fresh_db()
        try:
            # 二次 init 不炸（CREATE IF NOT EXISTS 幂等）
            await init_schema(db)
            cur = await db.execute("SELECT COUNT(*) AS cnt FROM filter_state")
            assert (await cur.fetchone())["cnt"] == 0
        finally:
            await db.close()


class TestFilterStateHelpers:
    async def test_note_inserts_then_increments(self):
        db = await _fresh_db()
        try:
            p1, p2 = _patched(db)
            with p1, p2:
                assert await note_filter_uncertain("weflow", "m1", "adapter-a") == 1
                assert await note_filter_uncertain("weflow", "m1") == 2
                state = await get_filter_state("weflow", "m1")
            assert state["uncertain_count"] == 2
            # adapter_version 只在首次建行时落：第二次调用传空不覆写
            assert state["adapter_version"] == "adapter-a"
        finally:
            await db.close()

    async def test_source_scoping(self):
        db = await _fresh_db()
        try:
            p1, p2 = _patched(db)
            with p1, p2:
                await note_filter_uncertain("weflow", "m1")
                await note_filter_uncertain("qqflow", "m1")
                s1 = await get_filter_state("weflow", "m1")
                s2 = await get_filter_state("qqflow", "m1")
                cnt = await count_filter_state()
            assert s1["uncertain_count"] == 1
            assert s2["uncertain_count"] == 1
            assert cnt == 2
        finally:
            await db.close()

    async def test_clear_removes_rows(self):
        db = await _fresh_db()
        try:
            p1, p2 = _patched(db)
            with p1, p2:
                await note_filter_uncertain("weflow", "m1")
                await note_filter_uncertain("weflow", "m2")
                removed = await clear_filter_state("weflow", ["m1", "m2", "ghost"])
                assert await get_filter_state("weflow", "m1") is None
                cnt = await count_filter_state()
            assert removed == 2
            assert cnt == 0
        finally:
            await db.close()

    async def test_clear_empty_is_noop(self):
        db = await _fresh_db()
        try:
            p1, p2 = _patched(db)
            with p1, p2:
                assert await clear_filter_state("weflow", []) == 0
        finally:
            await db.close()

    async def test_get_unknown_returns_none(self):
        db = await _fresh_db()
        try:
            p1, p2 = _patched(db)
            with p1, p2:
                assert await get_filter_state("weflow", "ghost") is None
        finally:
            await db.close()
