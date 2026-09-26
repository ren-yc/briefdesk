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
from briefdesk.db import db_redirect, get_db
from briefdesk.plugins.ai_provider.engine import Provider
from briefdesk.plugins.benchmark.schema import CategoryDef
from briefdesk.status import get_sync_progress

logger = logging.getLogger(__name__)

# 临时库根目录：插件包内 .tmp（gitignore），沙箱/受限环境可写；每次运行在其
# 下建唯一子目录，退出只删子目录。
_TMP_ROOT = Path(__file__).resolve().parent / ".tmp"

_DRAIN_POLL_INTERVAL = 0.05


def _drain_progress() -> tuple[int, int]:
    """排空进展信号（批粒度）：(待处理批次数, 在途批次数)。

    pendingCount 覆盖已计数的批次；activeBatches 覆盖「已过暂停检查、尚未
    计数」的窗口批次——只看前者会把该窗口批次误判为已排空。
    """
    from briefdesk.pipeline import get_active_batches

    return (get_sync_progress().get("pendingCount", 0), get_active_batches())


async def _wait_pipelines_drained(stall_seconds: float) -> bool:
    """等待在途批次排空：暂停只拦新批，已在分类阶段的批次仍会进入存储相——
    不排空就重定向会把它们的卡片写进临时库，并在生产去重缓存留下指向临时库
    的幽灵条目（后续相似消息被误吸收）。

    判据是**无进展超时**而非总时长上限。进展信号都是批粒度的，只在批边界
    变化，而单批内部的 AI 调用可能比任何固定总超时都慢（分类最坏
    120s × 3 次尝试 = 360s，其后还有「时间提取 ∥ 标题概括」并行段）：用总
    超时会误中止正常推进的慢批，用无进展阈值则只要批次还在推进就一直等，
    真正卡死的场景仍被兜住。

    返回是否在无进展超时前排空。阈值取值与未覆盖场景见
    benchmark/config.BenchmarkSettings.drain_stall_seconds。
    """
    loop = asyncio.get_running_loop()
    last: tuple[int, int] | None = None
    last_change = loop.time()
    while True:
        cur = _drain_progress()
        if cur == (0, 0):
            return True
        now = loop.time()
        if cur != last:
            last, last_change = cur, now
        elif now - last_change >= stall_seconds:
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


async def prepare_scratch(categories: list[CategoryDef] | None = None) -> None:
    """子进程侧的基准库准备：在 `config.db_path` 上建库 + 按数据集声明替换类别。

    与 `bench_environment` 的分工：环境缝是**父进程**为了「不打断自己的轮询/
    实时链路」才发明的隔离手段，子进程不需要——它可以直接把 `config.db_path`
    指到自己的 scratch 文件上。所以这里只做建库与换类别，不做暂停/排空/公告/
    重定向，也不碰 AI 端口（由调用方按运行参数注入）。

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


@asynccontextmanager
async def bench_environment(
    categories: list[CategoryDef] | None = None, *, register_ai: bool = True
) -> AsyncIterator[None]:
    """进入基准环境：暂停生产管道 + 临时库（db_redirect 重定向）+ AI。

    退出恢复顺序见 finally 内注释；不动 config.db_path、不关闭应用已有的数据库连接。
    """
    from briefdesk import pipeline
    from briefdesk.plugins.benchmark.config import BenchmarkSettings

    # 每次进入都重新实例化：设置页改动后无需重启即可生效
    stall_seconds = BenchmarkSettings().drain_stall_seconds
    old_ai = ai_ports.get_ai()
    run_dir = _TMP_ROOT / f"bench-{uuid.uuid4().hex[:8]}"
    db_path = str(run_dir / "bench.sqlite")
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
        # 重定向前先暂停生产管道：暂停期间 process_all_batches 直接返回，
        # 实时消息不入库也不标 processed，延后到下轮回填自然恢复。
        # 置于 try 内保证任何后续失败都走 finally 的复位与子目录清理。
        pipeline.set_processing_paused(True)
        # 「准备中」公告必须位于等待之前：这一段最长可达无进展阈值（默认
        # 720s），期间用户完全看不到反馈。措辞**不得**声称拒绝写操作——
        # 排空阶段 DB 仍是生产库，写操作自洽且安全，会被写闸门挡住的只有
        # 之后的**重定向窗口期**。
        try:
            await announcements.announce(
                "benchmark_preparing",
                "warning",
                "基准准备中：消息处理已暂停，正在等待在途批次排空",
            )
        except Exception:  # 公告失败不阻断基准运行
            logger.debug("基准公告发布失败", exc_info=True)
        # 等待在途批次排空，见 _wait_pipelines_drained。
        # 必须先于 db_redirect：在途批次仍持生产连接，未排空即重定向会让
        # 半程批次的后续写落到临时基准库。
        if not await _wait_pipelines_drained(stall_seconds):
            # 直接中止：带警告继续会在途批次的后续写落进临时基准库，并在生产
            # 去重缓存留下幽灵条目（去重缓存是进程级内存态，切库不会清）。
            # 位于 try 内，finally 照常复位；路由层捕获异常写入 _last_result。
            raise RuntimeError(
                f"benchmark: 等待在途批次排空时连续 {stall_seconds}s 无进展，"
                "已中止本次基准以免在途批次写入临时库/污染生产去重缓存；"
                "可调大 BENCHMARK_DRAIN_STALL_SECONDS 后重试"
            )
        try:
            await announcements.announce(
                "benchmark_running",
                "warning",
                "基准运行中：界面写操作、备份与导出暂不可用；消息处理已暂停，"
                "结束后如未开启周期同步，请点一次同步补齐",
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
            # 两条公告都在此撤销（幂等）：排空中止路径只发布过「准备中」，
            # 正常路径两条都有
            for code in ("benchmark_running", "benchmark_preparing"):
                try:
                    await announcements.revoke(code)
                except Exception:  # 撤销失败不影响环境还原
                    logger.debug("基准公告撤销失败", exc_info=True)
        finally:
            # 必须在外层 finally：revoke 是本段唯一的可取消点，而
            # CancelledError 是 BaseException 子类，上面的 except Exception
            # 拦不住（插件 teardown 会取消运行中的基准任务，兜底清理还会补发
            # 取消）。漏掉下面两行，管道会永久停在暂停态、AI 端口也仍指向
            # 基准供应商。
            # 两条复位保持相邻且不得移出本 finally：它们之间没有 await，事件
            # 循环内原子——分开或上移会新开「管道已放行而 AI 端口未还原」的
            # 窗口，届时新批次会用基准供应商处理。
            pipeline.set_processing_paused(False)
            ai_ports.set_ai(old_ai)
        # 运行目录清理留在保护段之外：它不是环境还原的必要条件，取消路径上
        # 不值得为它多付一次线程跳转与磁盘删除（遗留目录在插件包 .tmp/ 内，
        # 已 gitignore）。因此取消恰落在 revoke 上时本次目录会留存——接受的
        # 残留；第二次取消落在这里同样是接受项。
        await asyncio.to_thread(shutil.rmtree, run_dir, ignore_errors=True)
