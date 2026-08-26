"""基准运行环境 — 管道门闸 + 临时数据库（补丁式隔离）+ AI 供应商装配。

在应用进程内运行基准时，**不能**切换 config.db_path 或关闭应用的主连接
（那会打断运行中的轮询/实时链路）。因此这里把 briefdesk.db 的 get_db /
get_embed_db 两个模块级入口临时替换为指向临时库的连接（引擎/管道函数均
在调用时刻经模块属性解析，替换即生效；与 tests 中 patch("briefdesk.db.get_db")
同机制），结束后恢复原函数并删除临时库。

补丁是进程级的——运行期间其它协程的 DB 调用也会落到临时库。为杜绝生产
数据误入临时库，进入环境即经 `pipeline.set_processing_paused(True)` 暂停
生产处理管道：实时消息在基准期间延后到下一轮回填窗口处理（不丢失，水位
不受影响）。

临时库落在本次运行专属的 uuid 子目录（`_TMP_ROOT/bench-<hex>/`），退出只
删除该子目录——共享的 .tmp 根目录内其它内容（如并行 CLI 运行的目录）不受
影响。
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

# 临时库根目录：插件包内 .tmp（gitignore），沙箱/受限环境可写；每次运行在其
# 下建唯一子目录，退出只删子目录。
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
    """进入基准环境：暂停生产管道 + 临时库（补丁 get_db/get_embed_db）+ AI。

    退出恢复顺序见 finally 内注释；不动 config.db_path、不关闭应用已有的数据库连接。
    """
    import briefdesk.db as briefdesk_db
    from briefdesk import pipeline

    old_ai = ai_ports.get_ai()
    old_get_db = briefdesk_db.get_db
    old_get_embed_db = briefdesk_db.get_embed_db
    run_dir = _TMP_ROOT / f"bench-{uuid.uuid4().hex[:8]}"
    db_path = str(run_dir / "bench.sqlite")
    # 连接变量先置 None：创建/初始化失败时 finally 才能区分「无需关闭」与
    # 「需关闭」，避免半程状态泄漏连接与本次子目录（审查 A3）
    main_conn: aiosqlite.Connection | None = None
    embed_conn: aiosqlite.Connection | None = None
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
        main_conn = await _new_connection(db_path)
        embed_conn = await _new_connection(db_path)
        for pragma in ("PRAGMA journal_mode = WAL", "PRAGMA busy_timeout = 5000"):
            cursor = await embed_conn.execute(pragma)
            await cursor.close()
        # 打补丁前先暂停生产管道：暂停期间 process_all_batches 直接返回，
        # 实时消息不入库也不标 processed，延后到下轮回填自然恢复。
        # 置于 try 内保证任何后续失败都走 finally 的复位与子目录清理。
        pipeline.set_processing_paused(True)
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
        # 顺序约束：先复位管道标志，再还原 DB 入口补丁（两者之间无 await 点，
        # 事件循环内原子，不存在"管道已放行而补丁未还原"的窗口）。
        pipeline.set_processing_paused(False)
        briefdesk_db.get_db = old_get_db  # type: ignore[assignment]
        briefdesk_db.get_embed_db = old_get_embed_db  # type: ignore[assignment]
        ai_ports.set_ai(old_ai)
        if embed_conn is not None:
            await embed_conn.close()
        if main_conn is not None:
            await main_conn.close()
        await asyncio.to_thread(shutil.rmtree, run_dir, ignore_errors=True)
