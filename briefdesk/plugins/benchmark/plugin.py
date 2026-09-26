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
- 运行：与生产同引擎同 AI 供应商（真实调用，耗时数分钟），交给独立子进程；
  父进程只负责生命周期与读盘，界面侧不受影响。运行期间经
  pipeline.set_processing_paused 暂停生产处理管道——实时消息延后到下一轮
  回填窗口处理，不丢失；teardown 会取消并限时等待运行收尾，保证父进程侧的
  暂停标志与公告都复位。
- CLI：python -m briefdesk.plugins.benchmark.cli。
"""

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter

from briefdesk.plugin.base import PluginContext, StagePlugin, WebPlugin
from briefdesk.settings_schema import build_settings_schema
from briefdesk.types import BatchContext

logger = logging.getLogger(__name__)

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
            labels={
                "pause_pipeline": "运行期间暂停消息处理",
                "keep_runs": "保留的运行目录数",
                "run_timeout_seconds": "总时长上限（秒，0 = 不限）",
                "run_stall_seconds": "无进展阈值（秒）",
            },
            hints={
                "keep_runs": (
                    "超出后按启动时间从旧到新删除运行目录，"
                    "但**最新一个已完成**的运行永不删除（报告页依赖它）"
                ),
                "run_stall_seconds": (
                    "子进程运行期以 progress.jsonl 的行数增长判进展（用例粒度），"
                    "并要求 ≥ 单个用例最坏耗时（分类 120s × 3 次重试 = 360s）；"
                    "父进程还会按 --progress-every 线性放大本值"
                ),
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

    async def activate(self, ctx: PluginContext) -> None:
        """启动兜底：清掉上次异常退出留下的残目录，并做一次轮转。

        轮转的主路径是「回收子进程后触发一次」——只挂在这里的话，长驻进程里
        运行目录会无限增长；这里的调用只保证重启后能收一次尾。
        """
        from briefdesk.plugins.benchmark import supervisor

        await supervisor.gc_orphans()

    async def teardown(self) -> None:
        """取消并限时等待运行中的基准。

        关闭序列是 teardown_all → close_db。运行活过 close_db 时，它的收尾会去动
        已经关闭的连接（父进程侧还有暂停标志与公告要复位）；子进程也一样，收干净
        之后它才不会再往 run_dir 里写。
        """
        from briefdesk.plugins.benchmark import supervisor

        await supervisor.stop_all()



plugin = BenchmarkPlugin()
