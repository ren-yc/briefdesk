"""AI 分类阶段插件（slot=classify）— 把一批消息分类，结果写入 batch.outcomes。

分类类别由 DB categories 表驱动（用户可增删改/启用停用）；无启用类别时
引擎抛错（正常路径由 pipeline 入口在切批前拦截，此处仅兜底竞态）。

类别分配器（allocator，就绪待校准组件）随本插件装配：setup 构造实例并触发
首次重建（锁外异步）；类别改名/新增/停用经事件触发重建（原型向量集与
类名强相关——改名后旧缓存仍是旧名口径）。分配主路径 allocate_with_vector
为「就绪 + 待校准」组件：本阶段不接入判定主链路（旧 sysb 链路全程保留），
消费方式 = 影子打分与留一校准。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from briefdesk.events import EVENT_CATEGORIES_CHANGED
from briefdesk.plugin.base import PluginContext, StagePlugin
from briefdesk.types import BatchContext, ClassifyOutcome


class ClassifyPlugin(StagePlugin):
    """分类阶段插件（显式实现 StagePlugin；入口见模块底部 `plugin` 实例）。"""

    name = "classify"
    version = "1.2.0"
    dependencies: tuple[str, ...] = ("ai_provider",)  # 分类依赖 AI 供应商
    conflicts: tuple[str, ...] = ()
    core = True  # 核心插件：始终装配，不受 PLUGINS 过滤
    slot = "classify"
    priority = 0

    def __init__(self) -> None:
        self._classify_batch: Callable[..., Awaitable[ClassifyOutcome]] | None = None
        self._ctx: PluginContext | None = None
        # 在飞的重建任务引用集（防 create_task 产物被 GC；见 _on_categories_changed）
        self._rebuild_tasks: set[asyncio.Task[None]] = set()

    async def setup(self, ctx: PluginContext) -> None:
        # 延迟导入：仅加载本插件依赖，且便于测试替换
        from briefdesk.plugins.classify import engine as classify_engine

        self._classify_batch = classify_engine.classify_batch
        self._ctx = ctx
        ctx.register_stage(self)
        # 分配器重建是网络调用（批量嵌入），setup 在服务启动前、锁外执行；
        # 失败不阻断装配（分配器是可选组件，冷启动回退 API 链路）。
        try:
            await rebuild_allocator()
        except Exception:
            import logging

            logging.getLogger(__name__).warning(
                "类别分配器首次重建失败（不阻断装配，待事件触发重试）",
                exc_info=True,
            )
        # 类别结构变更 → 原型缓存失配（改名/新增/删除），触发重建。
        # 同步处理器：发布方（类别路由）持存储锁时保持原子——rebuild 内部
        # 有网络调用，经 asyncio.create_task 转锁外执行。
        ctx.subscribe_event(
            EVENT_CATEGORIES_CHANGED, self._on_categories_changed
        )

    async def activate(self, ctx: PluginContext) -> None: ...

    async def teardown(self) -> None:
        self._ctx = None

    def _on_categories_changed(self, _payload: object) -> None:
        """类别结构变更 → 异步重建原型（节流合并须兑现：-1 等剩余间隔重试）。

        节流返回 -1 时若不补触发，重建请求会被静默吞掉（pending 标志只等
        下一次 rebuild 到来自行兑现，而事件源没有下一次）——改名后缓存保持
        旧类名口径直到重启。故此处等剩余间隔耗尽后重试一次；重试仍 -1
        （极端高频变更）则放弃（最新一次变更已由后续事件重试覆盖）。
        """
        import asyncio
        import logging

        async def _rebuild_with_retry() -> None:
            from briefdesk.plugins.classify import allocator as allocator_module

            logger = logging.getLogger(__name__)
            try:
                rc = await rebuild_allocator()
                if rc == -1:
                    remaining = max(
                        0.0,
                        allocator_module._REBUILD_MIN_INTERVAL
                        - (
                            __import__("time").monotonic()
                            - allocator_module.allocator._rebuilt_at
                        ),
                    )
                    await asyncio.sleep(remaining)
                    await rebuild_allocator(force=True)
            except Exception:
                logger.warning("类别变更后的分配器重建失败", exc_info=True)

        try:
            # 任务必须持引用：事件循环对 create_task 的产物只持弱引用，
            # 无外部引用时可能在完成前被 GC（重建静默丢失，缓存停在旧类名
            # 口径）。集合在任务结束时自行移除，兼作在飞任务的登记表。
            task = asyncio.get_running_loop().create_task(_rebuild_with_retry())
            self._rebuild_tasks.add(task)
            task.add_done_callback(self._rebuild_tasks.discard)
        except RuntimeError:
            pass  # 无运行中循环（测试/关闭序列）：跳过，下次触发重建

    async def run(self, batch: BatchContext, ctx: PluginContext) -> None:
        if self._classify_batch is not None:
            # vision_images 为 enrich 阶段暂存的归一化图片字节（vision 关闭时
            # 为空 dict）：引擎对无图消息自动维持纯文本请求
            batch.outcomes = await self._classify_batch(
                batch.messages, vision_images=batch.vision_images
            )
            # per-instance 判定观察记录转批上下文（观察型插件消费）：
            # 引擎在 outcome 内携带（与判定结果同成功口径），此处仅转接。
            if batch.outcomes is not None:
                batch.classify_checks = list(batch.outcomes.observations)


async def rebuild_allocator(*, force: bool = False) -> int:
    """从 items + recat_log 合成样本并重建分配器原型缓存（网络调用，锁外）。

    样本源：items 近窗口每类 Top-N（主源，实例分布）+ recat_log 纠错覆盖层
    （weight=2，计入新类、同源 content 由分配器去重胜出）。嵌入未启用时
    rebuild 内部直接返回 0（分配器置空、allocate 回退 API）。force 透传给
    rebuild 跳过重建节流（节流后的到期补触发用）。
    """
    from briefdesk import db as db_module
    from briefdesk.plugins.classify import allocator as allocator_module

    if not allocator_module.is_available():
        return 0
    samples = await db_module.get_category_sample_contents(
        allocator_module.PROTOTYPE_TOP_N,
        allocator_module.PROTOTYPE_WINDOW_DAYS,
    )
    overlay = await db_module.get_recat_overlay_samples(
        allocator_module.PROTOTYPE_WINDOW_DAYS
    )
    # recat 覆盖层并入（分配器按 content 去重，weight=2 胜出普通样本）
    samples.extend(overlay)
    return await allocator_module.allocator.rebuild(samples, force=force)


plugin = ClassifyPlugin()
