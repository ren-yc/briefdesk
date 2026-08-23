"""基准测试插件（P5 WebPlugin + P3 StagePlugin 双能力）— 路由 + 前端 + 处理时点记录。

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
  运行期间补丁 briefdesk.db.get_db 指向临时库，请勿同时触发同步。
- CLI：python -m briefdesk.plugins.benchmark.cli。
"""

from pathlib import Path

from fastapi import APIRouter

from briefdesk.plugin.base import PluginContext, StagePlugin, WebPlugin
from briefdesk.types import BatchContext


class BenchmarkPlugin(WebPlugin, StagePlugin):
    """基准测试插件（显式实现 WebPlugin + StagePlugin；入口见模块底部 `plugin` 实例）。

    依赖 ai_provider：基准必须真实调用 AI，AI 供应商不可用时随依赖降级禁用。
    """

    name = "benchmark"
    version = "1.0.2"
    dependencies: tuple[str, ...] = ("ai_provider",)
    slot = "post_insert"  # 阶段槽位：合并判定之后（batch.merge_checks 已填充）
    priority = 1  # 同槽 priority 升序：在 merge 阶段（priority=0）之后运行

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

    async def teardown(self) -> None: ...


plugin = BenchmarkPlugin()
