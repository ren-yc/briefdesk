"""管道阶段注册表与装配期上下文。

StagePlugin 经 PluginContext.register_stage 注册（main 把端口接到本模块），
pipeline 骨架运行时按槽位读取；装配期 PluginContext 由 main 在 setup_all
前经 set_context 注入，阶段 run(batch, ctx) 由此获得服务端口（如 ctx.dedup）。

槽位固定顺序：enrich → classify → dedup → post_insert；同槽按 priority 升序。
模块级单例（与 realtime/db 同风格）；测试用 reset() 隔离。
"""

from briefdesk.plugin.base import PluginContext, StagePlugin

_stages: dict[str, list[StagePlugin]] = {}
_context: PluginContext | None = None


def register_stage(stage: StagePlugin) -> None:
    """注册管道阶段（同实例重复注册不叠加）。"""
    slots = _stages.setdefault(stage.slot, [])
    if stage not in slots:
        slots.append(stage)
    slots.sort(key=lambda s: s.priority)


def get_stages(slot: str) -> list[StagePlugin]:
    """按槽位取阶段列表（priority 升序，快照）。"""
    return list(_stages.get(slot, []))


def set_context(ctx: PluginContext | None) -> None:
    """注入装配期上下文（main 在 setup_all 前调用）。"""
    global _context
    _context = ctx


def get_context() -> PluginContext | None:
    return _context


def reset() -> None:
    """清空注册表与上下文（仅测试使用）。"""
    _stages.clear()
    set_context(None)
