"""PluginManager — 插件发现（entry points + PLUGIN_PATH）、装配与生命周期编排。

setup/activate/teardown 的顺序约束（对齐 main 启动顺序）、PLUGINS 过滤
规则与失败隔离语义详见 docs/architecture.md「插件框架」。
"""

import importlib.metadata
import importlib.util
import logging
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from briefdesk.config import Settings, config
from briefdesk.plugin.base import (
    PLUGIN_GROUP,
    Plugin,
    PluginContext,
    PluginDisabledError,
    PluginError,
)

logger = logging.getLogger(__name__)


@dataclass
class PluginRecord:
    """单个插件的发现/装配记录。"""

    name: str
    version: str
    plugin: Plugin | None  # 加载失败/校验不通过时无实例
    status: Literal["discovered", "loaded", "disabled", "failed"] = "discovered"
    reason: str = ""
    dependencies: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    core: bool = False

    def info(self) -> dict[str, str | bool]:
        """装配摘要：供 /api/plugins 与前端加载器使用。

        has_frontend：插件是否声明前端资源（asset_dir() 非 None）——前端
        加载器据此只对带前端的插件注入 ui.css/ui.js，避免无资源插件的
        404 请求触发浏览器严格 MIME 检查告警。
        """
        has_frontend = False
        if self.plugin is not None:
            asset_dir = getattr(self.plugin, "asset_dir", None)
            if callable(asset_dir):
                try:
                    has_frontend = asset_dir() is not None
                except Exception:  # noqa: BLE001 — 第三方插件实现不可控
                    has_frontend = False
        return {
            "name": self.name,
            "version": self.version,
            "status": self.status,
            "reason": self.reason,
            "has_frontend": has_frontend,
            "core": self.core,
        }


class PluginManager:
    """插件注册中心：发现 → 过滤 → 拓扑排序 → setup → activate → teardown。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings if settings is not None else config
        self._records: dict[str, PluginRecord] = {}
        """全部获取到的插件记录（含失败/禁用）"""
        self._discovered = False
        self._load_order: list[str] = []  # setup 成功顺序，teardown 逆序
        self._initialized: set[str] = set()

    # ── 发现 ──

    def discover(self) -> None:
        """从 entry points 与 PLUGIN_PATH 发现插件（幂等，可重复调用）。"""
        if self._discovered:
            return  # 重复调用无副作用
        self._discovered = True
        for ep in importlib.metadata.entry_points().select(group=PLUGIN_GROUP):
            try:
                obj = ep.load()
            except Exception as e:  # noqa: BLE001 — 插件代码不可控，任何导入错误都隔离
                self._record_failure(ep.name, f"entry point 加载失败: {e}")
                continue
            self._accept(getattr(obj, "plugin", obj), origin=f"entry point {ep.name}")
        plugin_path = self._settings.plugin_path
        if plugin_path:
            self._discover_path(Path(plugin_path))

    def register(self, plugin: Plugin) -> None:
        """程序化注册（测试/免打包场景），与 discover 结果合流。"""
        self._accept(plugin, origin="register")

    # ── 装配 ──

    def enabled_names(self) -> list[str]:
        """核心插件恒入选、可选插件按 PLUGINS 显式列表过滤后的插件名（保持发现顺序）。

        PLUGINS 无通配语义：仅接受可选插件名，未知名打 WARNING；可选插件
        不在列表即禁用（"禁用 = 不列出"，无独立的禁用名单配置）。互斥对
        同时入选（手工改 .env 才可能）时按 PLUGINS 列表位置仲裁，先列者
        保留，后者降级 disabled（见 ``_arbitrate_conflicts``）。
        """
        allow = self._settings.plugins
        for name in allow:
            if name not in self._records:
                logger.warning("PLUGINS 含未知插件名: %s", name)
        explicit = set(allow)
        enabled = []
        for name, rec in self._records.items():
            if rec.core or name in explicit:
                enabled.append(name)
            elif rec.status not in ("disabled", "failed"):
                # 可选插件未列出即禁用：显式标记（/api/plugins 与设置页据此
                # 显示「未启用」而非笼统的「不可用」），幂等不覆盖既有原因
                self._mark(
                    name, "disabled", "未启用：在 PLUGINS 中列出或经「插件」面板开关即可启用"
                )
        return self._arbitrate_conflicts(enabled)

    def _arbitrate_conflicts(self, enabled: list[str]) -> list[str]:
        """互斥仲裁：互斥对同时入选时按 PLUGINS 列表位置先列者保留。

        PLUGINS 位置未知（核心插件恒入选、或对端未显式列出）时按发现
        顺序兜底。落选者降级 disabled 并注明原因；返回仲裁后的启用名单。
        """
        allow = self._settings.plugins
        order = {name: i for i, name in enumerate(allow)}
        rank = {name: i for i, name in enumerate(enabled)}  # 发现顺序兜底

        def _key(name: str) -> tuple[int, int, int]:
            rec = self._records[name]
            # 核心恒装配：互斥对中核心插件恒胜（第三方可选插件与核心互斥时
            # 可选侧让位，避免把核心插件仲裁掉），其次 PLUGINS 先列者
            return (0 if rec.core else 1, order.get(name, len(allow)), rank[name])

        # 同一落选者与多个 winner 冲突时全部保留（去重），/api/plugins 的
        # reason 才不丢对端
        losers: dict[str, list[str]] = {}
        for name in enabled:
            if name in losers:
                continue
            for other in self._records[name].conflicts:
                if other == name or other not in rank or other in losers:
                    continue
                loser, winner = (
                    (name, other) if _key(name) > _key(other) else (other, name)
                )
                winners = losers.setdefault(loser, [])
                if winner not in winners:
                    winners.append(winner)
        for name, winners in losers.items():
            self._mark(
                name, "disabled", f"与 {'、'.join(winners)} 互斥（PLUGINS 先列者保留）"
            )
        return [n for n in enabled if n not in losers]

    def setup_order(self) -> list[str]:
        """启用插件按依赖拓扑排序（Kahn，稳定：同级保持发现顺序）。

        依赖指向未知插件名的插件在此被降级 disabled（原因「未知依赖」），
        依赖环成员同样被降级 disabled。幂等：已降级的插件重复调用不再处理。
        """
        names = self.enabled_names()
        for name in names:
            rec = self._records[name]
            unknown = [d for d in rec.dependencies if d not in self._records]
            if unknown and rec.status not in ("disabled", "failed"):
                self._mark(name, "disabled", f"未知依赖: {', '.join(unknown)}")
        active = [n for n in names if self._records[n].status in ("discovered",)]
        indegree = {n: 0 for n in active}
        dependents: dict[str, list[str]] = {}
        for n in active:
            for dep in self._records[n].dependencies:
                if dep in indegree:
                    indegree[n] += 1
                    dependents.setdefault(dep, []).append(n)
        queue = [n for n in active if indegree[n] == 0]
        order: list[str] = []
        while queue:
            n = queue.pop(0)
            order.append(n)
            for m in dependents.get(n, []):
                indegree[m] -= 1
                if indegree[m] == 0:
                    queue.append(m)
        for n in active:
            if n not in order:
                self._mark(n, "disabled", "依赖环（或依赖已被禁用）")
        return order

    async def setup_all(self, ctx: PluginContext) -> None:
        """按拓扑序 setup；单插件失败隔离，required 失败致命。"""
        self.discover()
        self._load_order = []
        self._initialized.clear()
        for name in self.setup_order():
            rec = self._records[name]
            if rec.status not in ("discovered",):
                continue
            plugin = rec.plugin
            if plugin is None:
                self._mark(name, "failed", "无插件实例")
                self._fail_if_required(name)
                continue
            missing = [d for d in rec.dependencies if d not in self._initialized]
            if missing:
                self._mark(name, "disabled", f"依赖未就绪: {', '.join(missing)}")
                self._fail_if_required(name)
                continue
            try:
                await plugin.setup(ctx)
            except PluginDisabledError as e:
                self._mark(name, "disabled", str(e) or "插件自禁用")
                logger.warning("插件 %s 已禁用: %s", name, rec.reason)
                self._fail_if_required(name)
                continue
            except Exception as e:
                # 装配失败先 best-effort 回收副作用再标 failed（规范性
                # 契约：资源获取先于注册；任何注册行为必须可被自身 teardown
                # 幂等回收——内置 ai_provider/dedup 已满足，第三方插件同受
                # 此约束，见 docs/architecture.md 插件框架节）。
                await self._best_effort_teardown(name, plugin)
                self._mark(name, "failed", f"setup 失败: {e!r}")
                logger.exception("插件 %s setup 失败", name)
                self._fail_if_required(name)
                continue
            self._mark(name, "loaded")
            self._initialized.add(name)
            self._load_order.append(name)
            logger.info("插件已加载: %s %s", name, rec.version)

    async def activate_all(self, ctx: PluginContext) -> None:
        """按加载序 activate；失败插件降级 failed（required 致命）。"""
        for name in list(self._load_order):
            rec = self._records[name]
            plugin = rec.plugin
            if plugin is None:
                continue
            try:
                await plugin.activate(ctx)
            except Exception as e:
                # P2 修复：activate 失败同样 best-effort 回滚（该插件仍在
                # _load_order，关闭时 teardown_all 会按幂等契约再调一次）
                await self._best_effort_teardown(name, plugin)
                self._mark(name, "failed", f"activate 失败: {e!r}")
                logger.exception("插件 %s activate 失败", name)
                self._fail_if_required(name)

    async def teardown_all(self) -> None:
        """按 setup 逆序 teardown（幂等）；单插件失败不影响其余。"""
        for name in reversed(self._load_order):
            rec = self._records[name]
            plugin = rec.plugin
            if plugin is None:
                continue
            try:
                await plugin.teardown()
            except Exception:
                logger.exception("插件 %s teardown 失败", name)
        self._initialized.clear()
        self._load_order = []

    # ── 查询 ──

    def infos(self) -> list[dict[str, str | bool]]:
        """全部插件的发现/装配摘要（供 /api/plugins 与测试使用）。"""
        return [rec.info() for rec in self._records.values()]

    def plugin_meta(self) -> list[dict[str, Any]]:
        """全部有效插件的声明元数据（供设置页逐插件开关渲染）。"""
        self.discover()
        result: list[dict[str, Any]] = []
        for rec in self._records.values():
            if rec.plugin is None:
                continue  # 加载失败记录无声明可读，前端经 infos() 兜底展示
            result.append(
                {
                    "name": rec.name,
                    "version": rec.version,
                    "dependencies": list(rec.dependencies),
                    "conflicts": list(rec.conflicts),
                    "core": rec.core,
                }
            )
        return result

    def validate_selection(self, names: Iterable[str]) -> list[dict[str, str]]:
        """校验可选插件期望启用集合（纯检查，不改变任何插件状态）。

        供设置 API 在写入暂存前复检期望列表，issue 类型：unknown（未知名）、
        missing_dep（依赖既不在集合也不是核心插件）、conflict（互斥对同现）、
        cycle（依赖环）。返回空列表即合法。核心插件恒装配、无需出现在集合
        中，指向核心插件的依赖视为恒满足。
        """
        self.discover()
        selected = list(dict.fromkeys(names))
        selected_set = set(selected)
        core_names = {n for n, rec in self._records.items() if rec.core}
        issues: list[dict[str, str]] = []

        for name in selected:
            if name not in self._records:
                issues.append(
                    {"type": "unknown", "plugin": name, "detail": f"未知插件名: {name}"}
                )
        for name in selected:
            rec = self._records.get(name)
            if rec is None:
                continue
            for dep in rec.dependencies:
                if dep in selected_set or dep in core_names:
                    continue
                note = "插件不存在" if dep not in self._records else "未启用"
                issues.append(
                    {
                        "type": "missing_dep",
                        "plugin": name,
                        "detail": f"缺少依赖: {dep}（{note}）",
                    }
                )
        reported: set[frozenset[str]] = set()
        for name in selected:
            rec = self._records.get(name)
            if rec is None:
                continue
            for other in rec.conflicts:
                if other not in selected_set:
                    continue
                pair = frozenset((name, other))
                if len(pair) != 2 or pair in reported:
                    continue
                reported.add(pair)
                issues.append(
                    {
                        "type": "conflict",
                        "plugin": name,
                        "detail": f"与 {other} 互斥，两者不可同时启用",
                    }
                )
        issues.extend(self._cycle_issues(selected_set | core_names))
        return issues

    def _cycle_issues(self, nodes: set[str]) -> list[dict[str, str]]:
        """nodes（插件名）子图上的依赖环检测（Kahn；环成员各报一条）。"""
        active = {n for n in nodes if n in self._records}
        indegree = {n: 0 for n in active}
        dependents: dict[str, list[str]] = {}
        for n in active:
            for dep in self._records[n].dependencies:
                if dep in indegree:
                    indegree[n] += 1
                    dependents.setdefault(dep, []).append(n)
        queue = [n for n in active if indegree[n] == 0]
        ordered: set[str] = set()
        while queue:
            n = queue.pop(0)
            ordered.add(n)
            for m in dependents.get(n, []):
                indegree[m] -= 1
                if indegree[m] == 0:
                    queue.append(m)
        return [
            {"type": "cycle", "plugin": n, "detail": "依赖环成员，无法确定装配顺序"}
            for n in sorted(active - ordered)
        ]

    def settings_schema(self) -> list[dict[str, Any]]:
        """返回插件的设置描述（核心 + 全部已发现的可选插件）。

        不按启用状态筛选：可选插件禁用时用户仍需能预配置（启用前先填
        必填项），自禁用（缺必填配置）时亦然；UI 对未加载插件组折叠展示。
        """
        self.discover()
        result: list[dict[str, Any]] = []
        for name, rec in self._records.items():
            if rec.plugin is None:
                continue
            callback = getattr(rec.plugin, "settings_schema", None)
            if not callable(callback):
                continue
            try:
                fields = callback()
            except Exception:
                logger.exception("读取插件 %s 设置 schema 失败", name)
                continue
            for field in fields:
                if not isinstance(field, dict) or not isinstance(
                    field.get("key"), str
                ):
                    logger.warning("插件 %s 返回了无效设置 schema 字段", name)
                    continue
                item = dict(field)
                item["plugin"] = name
                item["pluginStatus"] = rec.status
                result.append(item)
        return result

    def records(self) -> dict[str, PluginRecord]:
        return dict(self._records)

    @property
    def loaded(self) -> list[str]:
        return list(self._load_order)

    # ── 内部 ──

    def _accept(self, obj: Any, *, origin: str) -> None:
        name = getattr(obj, "name", None)
        version = str(getattr(obj, "version", ""))

        # 检查obj是不是一个有效的插件实例
        if not isinstance(name, str) or not name:
            self._record_failure(origin, "缺少有效的 name")
            return
        if name in self._records:
            logger.error("插件名重复，跳过 %s（%s）", name, origin)
            return
        for attr in ("setup", "activate", "teardown"):
            if not callable(getattr(obj, attr, None)):
                self._record_failure(name, f"缺少生命周期方法 {attr}")
                return
        dependencies = getattr(obj, "dependencies", ())
        if not isinstance(dependencies, (tuple, list)) or not all(
            isinstance(d, str) for d in dependencies
        ):
            self._record_failure(name, "dependencies 必须为字符串元组/列表")
            return
        core = getattr(obj, "core", False)
        conflicts = getattr(obj, "conflicts", ())
        if not isinstance(core, bool):
            self._record_failure(name, "core 必须为布尔值")
            return
        if not isinstance(conflicts, (tuple, list)) or not all(
            isinstance(c, str) and c for c in conflicts
        ):
            self._record_failure(name, "conflicts 必须为非空字符串元组/列表")
            return
        if name in conflicts or len(set(conflicts)) != len(conflicts):
            self._record_failure(name, "conflicts 不得自指或重复")
            return
        if core and conflicts:
            self._record_failure(name, "核心插件恒装配，不得声明互斥")
            return

        self._records[name] = PluginRecord(
            name=name,
            version=version,
            plugin=cast(Plugin, obj),
            dependencies=tuple(dependencies),
            conflicts=tuple(conflicts),
            core=core,
        )

    def _discover_path(self, path: Path) -> None:
        if not path.is_dir():
            logger.warning("PLUGIN_PATH 不存在: %s", path)
            return
        for file in sorted(path.glob("*.py")):
            mod_name = (
                f"_briefdesk_dev_plugin_{file.stem}_"
                f"{abs(hash(str(file.resolve()))) % 10**8}"
            )
            spec = importlib.util.spec_from_file_location(mod_name, file)
            if spec is None:
                self._record_failure(file.name, "无法构造模块 spec")
                continue
            loader = spec.loader
            if loader is None:
                self._record_failure(file.name, "模块 spec 缺少 loader")
                continue
            module = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = module
            try:
                loader.exec_module(module)
            except (
                Exception  # noqa: BLE001 — 插件代码不可控，任何执行期错误都隔离
            ) as e:
                sys.modules.pop(mod_name, None)
                self._record_failure(file.name, f"加载失败: {e}")
                continue
            plugin = getattr(module, "plugin", None)
            if plugin is None:
                self._record_failure(file.name, "未暴露 plugin 实例")
                continue
            self._accept(plugin, origin=f"PLUGIN_PATH {file.name}")

    def _record_failure(self, name: str, reason: str) -> None:
        if name in self._records:
            logger.error("插件 %s 已有记录，忽略本次失败: %s", name, reason)
            return
        self._records[name] = PluginRecord(
            name=name, version="", plugin=None, status="failed", reason=reason
        )
        logger.error("插件 %s 不可用: %s", name, reason)

    def _mark(
        self,
        name: str,
        status: Literal["discovered", "loaded", "disabled", "failed"],
        reason: str = "",
    ) -> None:
        self._records[name].status = status
        self._records[name].reason = reason

    async def _best_effort_teardown(self, name: str, plugin: Plugin) -> None:
        """装配失败后回收半装配副作用（best-effort）。

        teardown 自身异常吞掉记 DEBUG：失败隔离原则下不得因清理失败掩盖
        原始装配错误；插件 teardown 契约本就要求幂等。
        """
        try:
            await plugin.teardown()
        except Exception as e:  # noqa: BLE001 — 清理失败不得掩盖原始错误
            logger.debug("插件 %s teardown 回收失败（忽略）: %r", name, e)

    def _fail_if_required(self, name: str) -> None:
        if name in self._settings.plugins_required:
            raise PluginError(f"必选插件 {name} 装配失败: {self._records[name].reason}")
