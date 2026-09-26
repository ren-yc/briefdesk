"""基准测试插件（WebPlugin + StagePlugin 双能力）— 路由 + 前端 + 处理时点记录。

- Web 插件：/api/benchmark/* 路由 + 前端（ui/）+ CLI 入口；
- 阶段插件（slot=post_insert，priority=1，在合并阶段之后、锁内运行）：
  管道处理期间经 recorder 采集 dedup/merge 阶段写入 BatchContext 的判定
  观察记录（真实处理时点的事实，含判重/合并命中的正向用例——网页按卡片
  最终状态导出观察不到命中），累积内存（记录开关默认关闭，经
  /api/benchmark/record 打开），导出为 cases/<feature>.fromweb.json；
- 用例：文件存储（不触碰数据库）——网页「导出当前列表为基准用例」把当前
  筛选的卡片逐功能覆盖导出到 cases/*.fromweb.json（classify/dedup/merge/
  title 四类用例，期望=卡片当前状态），前端无需手动用例管理；
- 运行：与生产同引擎同 AI 供应商（真实调用，耗时数分钟，后台任务执行）；
  运行期间经 db.db_redirect 把主/向量连接重定向到临时库，并经
  pipeline.set_processing_paused 暂停生产处理管道——实时消息延后到下一轮
  回填窗口处理，不丢失。窗口内变更路由与备份/导出被 server 中间件拒绝
  （见 server/window_guard.py），故界面写操作不会静默落进临时库；teardown
  会取消并限时等待运行中的基准任务，保证环境在 close_db 之前还原。
- CLI：python -m briefdesk.plugins.benchmark.cli。
"""

import asyncio
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter

from briefdesk.plugin.base import PluginContext, StagePlugin, WebPlugin
from briefdesk.settings_schema import build_settings_schema
from briefdesk.types import BatchContext

logger = logging.getLogger(__name__)

# 关闭期等待基准任务终结的上限（秒），与 main 收尾同一个量级
_TEARDOWN_TIMEOUT = 5.0


class BenchmarkPlugin(WebPlugin, StagePlugin):
    """基准测试插件（显式实现 WebPlugin + StagePlugin；入口见模块底部 `plugin` 实例）。

    依赖 ai_provider：基准必须真实调用 AI，AI 供应商不可用时随依赖降级禁用。
    """

    name = "benchmark"
    version = "1.0.2"
    dependencies: tuple[str, ...] = ("ai_provider",)
    conflicts: tuple[str, ...] = ()
    core = False  # 可选插件（实验性基准工具）：默认禁用，经 PLUGINS / 设置页开关启用
    slot = "post_insert"  # 阶段槽位：合并判定之后（batch.merge_checks 已填充）
    priority = 1  # 同槽 priority 升序：在 merge 阶段（priority=0）之后运行

    def settings_schema(self) -> list[dict[str, Any]]:
        from briefdesk.plugins.benchmark.config import BenchmarkSettings

        return build_settings_schema(
            BenchmarkSettings,
            plugin=self.name,
            labels={"drain_stall_seconds": "排空无进展阈值（秒）"},
            hints={
                "drain_stall_seconds": (
                    "在途批次排空时，进展信号（待处理批次数/在途批次数）"
                    "连续无变化达到本值才中止基准；不要低于单请求最坏耗时"
                    "（120s × 3 次尝试 = 360s）。每次运行基准时重新读取，"
                    "暂存后下次运行即生效，无需重启应用"
                )
            },
        )

    def router(self) -> APIRouter:
        from briefdesk.plugins.benchmark import router as benchmark_router

        return benchmark_router.router

    def asset_dir(self) -> Path | None:
        # 插件前端资源目录：核心挂载到 /plugin-assets/benchmark/（浏览器直连）
        return Path(__file__).parent / "ui"

    async def setup(self, ctx: PluginContext) -> None:
        ctx.register_router(self.router())
        asset_dir = self.asset_dir()
        if asset_dir is not None:
            ctx.register_plugin_assets(self.name, str(asset_dir))
        ctx.register_stage(self)  # 阶段插件：处理时点采集判定观察记录

    async def run(self, batch: BatchContext, ctx: PluginContext) -> None:
        """锁内（骨架持有 _storage_lock）：记录开关开启时采集本批判定记录。

        只做内存追加（无 AI/DB/文件 IO），不拖累存储锁。
        """
        from briefdesk.plugins.benchmark import recorder as bench_recorder

        if bench_recorder.is_enabled():
            bench_recorder.record_batch(batch)

    async def activate(self, ctx: PluginContext) -> None: ...

    async def teardown(self) -> None:
        """取消并限时等待运行中的基准任务。

        关闭序列是 teardown_all → close_db。基准运行期间单例指向临时库，
        close_db 关掉的是**临时**连接；若此时任务仍活着，它被取消后
        db_redirect 的 finally 会把单例还原为**从未关闭的生产连接**，其残留的
        aiosqlite 非 daemon worker 线程让解释器退出 join 挂死。在此先把任务
        收干净，bench_environment 的 finally（撤销公告、复位暂停标志与 AI 端口、
        删运行目录）就能赶在 close_db 之前跑完。
        """
        from briefdesk.plugins.benchmark import router as benchmark_router

        await _cancel_running_task(benchmark_router._running_task)


async def _cancel_running_task(task: asyncio.Task[None] | None) -> None:
    """取消并限时等待单个任务终结（关闭期收尾，best-effort，幂等）。

    本地实现而非复用 main._reap_task：全仓仅 __main__ 引用 main，插件反向
    依赖会破坏分层。已完成（含已取消）的任务原样返回；超时或任务自身异常
    都只记日志、不向上传播——关闭是 best-effort，残留任务交给进程收尾兜底。
    """
    if task is None or task.done():
        return
    task.cancel()
    try:
        # shield：wait_for 超时只取消包装层，任务保持 pending 留给兜底清理，
        # 避免二次 cancel 打断其内部清理流程
        await asyncio.wait_for(asyncio.shield(task), _TEARDOWN_TIMEOUT)
    except TimeoutError:
        logger.warning(
            "关闭等待超时：基准任务未在 %.0fs 内终结（留待兜底清理）",
            _TEARDOWN_TIMEOUT,
        )
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.exception("关闭期间基准任务异常")


plugin = BenchmarkPlugin()
