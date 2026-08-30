"""AI 分类阶段插件（slot=classify）— 把一批消息分类，结果写入 batch.outcomes。

分类类别由 DB categories 表驱动（用户可增删改/启用停用）；无启用类别时
引擎抛错（正常路径由 pipeline 入口在切批前拦截，此处仅兜底竞态）。
"""

from collections.abc import Awaitable, Callable

from briefdesk.plugin.base import PluginContext, StagePlugin
from briefdesk.types import BatchContext, ClassifyOutcome


class ClassifyPlugin(StagePlugin):
    """分类阶段插件（显式实现 StagePlugin；入口见模块底部 `plugin` 实例）。"""

    name = "classify"
    version = "1.1.0"
    dependencies: tuple[str, ...] = ("ai_provider",)  # 分类依赖 AI 供应商
    slot = "classify"
    priority = 0

    def __init__(self) -> None:
        self._classify_batch: Callable[..., Awaitable[ClassifyOutcome]] | None = None

    async def setup(self, ctx: PluginContext) -> None:
        # 延迟导入：仅加载本插件依赖，且便于测试替换
        from briefdesk.plugins.classify import engine as classify_engine

        self._classify_batch = classify_engine.classify_batch
        ctx.register_stage(self)

    async def activate(self, ctx: PluginContext) -> None: ...

    async def teardown(self) -> None: ...

    async def run(self, batch: BatchContext, ctx: PluginContext) -> None:
        if self._classify_batch is not None:
            # vision_images 为 enrich 阶段暂存的归一化图片字节（vision 关闭时
            # 为空 dict）：引擎对无图消息自动维持纯文本请求
            batch.outcomes = await self._classify_batch(
                batch.messages, vision_images=batch.vision_images
            )


plugin = ClassifyPlugin()
