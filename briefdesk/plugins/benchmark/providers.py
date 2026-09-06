"""基准运行环境 — 管道门闸 + 临时数据库（db_redirect 官方缝）+ AI 供应商装配。

在应用进程内运行基准时，**不能**切换 config.db_path 或关闭应用的主连接
（那会打断运行中的轮询/实时链路）。因此进入基准环境后经 `db.db_redirect`
官方缝把主/向量连接重定向到临时库（窗口内所有经 get_db()/get_embed_db()
的调用都落到临时库，应用已有连接不关闭、退出后原样继续使用）。

重定向是进程级的——运行期间其它协程的 DB 调用也会落到临时库。为杜绝生产
数据误入临时库，进入环境即经 `pipeline.set_processing_paused(True)` 暂停
生产处理管道并等待在途批次排空：实时消息在基准期间延后到下一轮回填窗口
处理（不丢失，水位不受影响）。

临时库落在本次运行专属的 uuid 子目录（`_TMP_ROOT/bench-<hex>/`），退出只
删除该子目录——共享的 .tmp 根目录内其它内容（如并行 CLI 运行的目录）不受
影响。
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite

from briefdesk import ai_ports, announcements
from briefdesk.db import db_redirect
from briefdesk.plugins.ai_provider.engine import Provider
from briefdesk.plugins.benchmark.schema import CategoryDef
from briefdesk.status import get_sync_progress

logger = logging.getLogger(__name__)

# 临时库根目录：插件包内 .tmp（gitignore），沙箱/受限环境可写；每次运行在其
# 下建唯一子目录，退出只删子目录。
_TMP_ROOT = Path(__file__).resolve().parent / ".tmp"

_DRAIN_POLL_INTERVAL = 0.05


async def _wait_pipelines_drained(timeout_s: float = 120.0) -> bool:
    """等待在途批次排空（复核 P2-22）：暂停只拦新批，已在分类阶段的批次仍会
    进入存储相——不排空就重定向会把它们的卡片写进临时库，并在生产去重缓存
    留下指向临时库的幽灵条目（后续相似消息被误吸收）。

    以 sync 进度的 pendingCount 归零为排空信号（暂停批在管道入口直接返回，
    不进入计数）；返回是否在超时前排空。
    """
    deadline = asyncio.get_running_loop().time() + timeout_s
    while True:
        if get_sync_progress().get("pendingCount") == 0:
            return True
        if asyncio.get_running_loop().time() >= deadline:
            return False
        await asyncio.sleep(_DRAIN_POLL_INTERVAL)


async def _replace_categories(
    conn: aiosqlite.Connection, defs: list[CategoryDef]
) -> None:
    """把临时库类别替换为数据集声明的类别（清空后重建）。"""
    cursor = await conn.execute("DELETE FROM categories")
    await cursor.close()
    await conn.executemany(
        "INSERT INTO categories (name, prompt, color, enabled, created_at) "
        "VALUES (?, ?, ?, 1, datetime('now'))",
        [(d.name, d.prompt, d.color or "#2563EB") for d in defs],
    )
    await conn.commit()


@asynccontextmanager
async def bench_environment(
    categories: list[CategoryDef] | None = None, *, register_ai: bool = True
) -> AsyncIterator[None]:
    """进入基准环境：暂停生产管道 + 临时库（db_redirect 重定向）+ AI。

    退出恢复顺序见 finally 内注释；不动 config.db_path、不关闭应用已有的数据库连接。
    """
    from briefdesk import pipeline

    old_ai = ai_ports.get_ai()
    run_dir = _TMP_ROOT / f"bench-{uuid.uuid4().hex[:8]}"
    db_path = str(run_dir / "bench.sqlite")
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
        # 重定向前先暂停生产管道：暂停期间 process_all_batches 直接返回，
        # 实时消息不入库也不标 processed，延后到下轮回填自然恢复。
        # 置于 try 内保证任何后续失败都走 finally 的复位与子目录清理。
        pipeline.set_processing_paused(True)
        # 等待在途批次排空（复核 P2-22），见 _wait_pipelines_drained。
        # 必须先于 db_redirect：在途批次仍持生产连接，未排空即重定向会让
        # 半程批次的后续写落到临时基准库。
        if not await _wait_pipelines_drained():
            logger.warning(
                "benchmark: 等待在途批次排空超时（120s），基准结果可能污染生产缓存"
            )
        try:
            await announcements.announce(
                "benchmark_running",
                "warning",
                "基准运行中：生产管道已暂停，UI 写操作（备忘/忽略/改分类/"
                "提醒等）将落在临时基准库并在运行结束后丢弃，请勿在此期间"
                "操作界面。",
            )
        except Exception:  # 公告失败不阻断基准运行
            logger.debug("基准公告发布失败", exc_info=True)
        async with db_redirect(db_path) as (main_conn, _embed_conn):
            if categories:
                await _replace_categories(main_conn, categories)
            if register_ai:
                ai_ports.set_ai(Provider())
            yield
    finally:
        # 顺序约束：db_redirect 退出时已同步还原单例并关闭临时连接（先于本
        # finally），此处再复位管道标志——不存在"管道已放行而 DB 未还原"
        # 的窗口；AI 端口复位与标志复位之间无 await 点，事件循环内原子。
        try:
            await announcements.revoke("benchmark_running")
        except Exception:  # 撤销失败不影响环境还原
            logger.debug("基准公告撤销失败", exc_info=True)
        pipeline.set_processing_paused(False)
        ai_ports.set_ai(old_ai)
        await asyncio.to_thread(shutil.rmtree, run_dir, ignore_errors=True)
