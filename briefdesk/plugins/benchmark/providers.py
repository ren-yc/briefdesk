"""基准运行环境准备 — 子进程侧的 scratch 库与类别注入。

基准运行**不再**与应用共享进程：runner 在自己的进程里把 `config.db_path` 指向自己
的 scratch 文件，本模块只负责「建库 + 按数据集声明替换类别」。

这里此前还有一条 `bench_environment`：进程内运行时的隔离缝——暂停生产管道、排空
在途批次、经 `db.db_redirect` 把主/向量连接重定向到临时库，并用公告把窗口期告诉
前端。那条路径已随进程级隔离落地整体删除：父子只经文件交换，父进程不再重定向，
也不再有需要「挂起」的内存派生状态。
"""

from __future__ import annotations

import logging

import aiosqlite

from briefdesk.db import get_db
from briefdesk.plugins.benchmark.schema import CategoryDef

logger = logging.getLogger(__name__)


async def _replace_categories(
    conn: aiosqlite.Connection, defs: list[CategoryDef]
) -> None:
    """把 scratch 库的类别替换为数据集声明的类别（清空后重建）。"""
    cursor = await conn.execute("DELETE FROM categories")
    await cursor.close()
    await conn.executemany(
        "INSERT INTO categories (name, prompt, color, enabled, created_at) "
        "VALUES (?, ?, ?, 1, datetime('now'))",
        [(d.name, d.prompt, d.color or "#2563EB") for d in defs],
    )
    await conn.commit()


async def prepare_scratch(categories: list[CategoryDef] | None = None) -> None:
    """子进程侧的基准库准备：在 `config.db_path` 上建库 + 按数据集声明替换类别。

    建表必须走 `db.get_db()` 这条公开路径：它内部经 `_init_connection` →
    `init_schema` 幂等补建全部表，并在末尾 `_seed_default_categories` 种入默认
    类别。自带建表 SQL 会让 categories 表为空，classify 随后以「没有启用的类别」
    直接抛错。

    返回前不关闭连接：它落在模块级单例上，**调用方必须在退出前 `close_db()`**
    ——aiosqlite 的 worker 线程不是 daemon 线程，漏关会让解释器在退出阶段挂死。
    """
    conn = await get_db()
    if categories:
        await _replace_categories(conn, categories)
