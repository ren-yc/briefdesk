"""基准运行环境 — 临时数据库（补丁式隔离）+ AI 供应商装配。

在应用进程内运行基准时，**不能**切换 config.db_path 或关闭应用的主连接
（那会打断运行中的轮询/实时链路）。因此这里把 briefdesk.db 的 get_db /
get_embed_db 两个模块级入口临时替换为指向临时库的连接（引擎/管道函数均
在调用时刻经模块属性解析，替换即生效；与 tests 中 patch("briefdesk.db.get_db")
同机制），结束后恢复原函数并删除临时库。

注意：补丁是进程级的——运行期间其它协程的 DB 调用也会落到临时库（数据
随后丢弃）。单用户本地应用 + 前端"运行中"横幅约束下可接受；文档已注明
运行期间请勿触发同步。
"""

from __future__ import annotations

import asyncio
import shutil
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite

from briefdesk import ai_ports
from briefdesk.db import init_schema
from briefdesk.plugins.ai_provider.engine import Provider
from briefdesk.plugins.benchmark.schema import CategoryDef

# 临时库目录：插件包内 .tmp（gitignore），沙箱/受限环境可写；退出时删除。
_TMP_ROOT = Path(__file__).resolve().parent / ".tmp"


async def _replace_categories(conn: aiosqlite.Connection, defs: list[CategoryDef]) -> None:
    """把临时库类别替换为数据集声明的类别（清空后重建）。"""
    cursor = await conn.execute("DELETE FROM categories")
    await cursor.close()
    await conn.executemany(
        "INSERT INTO categories (name, prompt, color, enabled, created_at) "
        "VALUES (?, ?, ?, 1, datetime('now'))",
        [(d.name, d.prompt, d.color or "#2563EB") for d in defs],
    )
    await conn.commit()


async def _new_connection(path: str) -> aiosqlite.Connection:
    conn = await aiosqlite.connect(path)
    conn.row_factory = aiosqlite.Row
    await init_schema(conn)
    return conn


@asynccontextmanager
async def bench_environment(
    categories: list[CategoryDef] | None = None, *, register_ai: bool = True
) -> AsyncIterator[None]:
    """进入基准环境：临时库（补丁 get_db/get_embed_db）+ 注册真实 AI 供应商。

    退出时恢复 ai_ports / briefdesk.db 原值并删除临时目录；不动 config.db_path、
    不关闭应用已有的数据库连接。
    """
    import briefdesk.db as briefdesk_db

    old_ai = ai_ports.get_ai()
    old_get_db = briefdesk_db.get_db
    old_get_embed_db = briefdesk_db.get_embed_db
    _TMP_ROOT.mkdir(parents=True, exist_ok=True)
    db_path = str(_TMP_ROOT / f"bench-{uuid.uuid4().hex[:8]}.sqlite")
    main_conn = await _new_connection(db_path)
    embed_conn = await _new_connection(db_path)
    for pragma in ("PRAGMA journal_mode = WAL", "PRAGMA busy_timeout = 5000"):
        cursor = await embed_conn.execute(pragma)
        await cursor.close()
    try:
        if categories:
            await _replace_categories(main_conn, categories)

        async def _main_getter() -> aiosqlite.Connection:
            return main_conn

        async def _embed_getter() -> aiosqlite.Connection:
            return embed_conn

        briefdesk_db.get_db = _main_getter  # type: ignore[assignment]
        briefdesk_db.get_embed_db = _embed_getter  # type: ignore[assignment]
        if register_ai:
            ai_ports.set_ai(Provider())
        yield
    finally:
        ai_ports.set_ai(old_ai)
        briefdesk_db.get_db = old_get_db  # type: ignore[assignment]
        briefdesk_db.get_embed_db = old_get_embed_db  # type: ignore[assignment]
        await embed_conn.close()
        await main_conn.close()
        await asyncio.to_thread(shutil.rmtree, _TMP_ROOT, ignore_errors=True)
