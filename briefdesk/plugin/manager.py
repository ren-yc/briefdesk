"""PluginManager — 插件发现（entry points + PLUGIN_PATH）、装配与生命周期编排。

装配顺序与 main 的启动顺序约束对齐（P1 先落地语义，P2 起接入 main）：
- setup：DB 就绪后、HTTP 服务启动前（去重缓存预热等重活放这里）；
- activate：HTTP 服务就绪后（注册路由、启动消息源监听等副作用放这里）；
- teardown：按 setup 成功顺序逆序执行（幂等）。

故障隔离：单插件 setup/activate 失败 → 该插件 failed + 日志，其余照常；
名字在 PLUGINS_REQUIRED 的插件失败 → 抛 PluginError（致命，启动中止）。
"""

import importlib.metadata
import importlib.util
import logging
import sys
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
        """按 PLUGINS / PLUGINS_DISABLED / 默认禁用名单过滤后的插件名（保持发现顺序）。

        默认禁用语义：声明 `default_disabled = True` 的插件（如实验性 benchmark）
        仅在 PLUGINS 中显式列名时启用——`PLUGINS=["*"]` 的"启用全部"不包含它，
        显式名称优先于通配；PLUGINS_DISABLED 仍为最高优先级，无论是否显式列名。
        """
        names = list(self._records)
        allow = self._settings.plugins
        explicit = set(allow)
        if "*" not in allow:
            for name in allow:
                if name not in self._records:
                    logger.warning("PLUGINS 含未知插件名: %s", name)
            names = [n for n in names if n in explicit]
        blocked = set(self._settings.plugins_disabled)
        enabled = []
        for name in names:
            if name in blocked:
                continue
            rec = self._records[name]
            if (
                getattr(rec.plugin, "default_disabled", False)
                and name not in explicit
            ):
                self._mark(
                    name, "disabled", "默认禁用：在 PLUGINS 中显式列出即可启用"
                )
                continue
            enabled.append(name)
        return enabled

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

        self._records[name] = PluginRecord(
            name=name,
            version=version,
            plugin=cast(Plugin, obj),
            dependencies=tuple(dependencies),
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

    def _fail_if_required(self, name: str) -> None:
        if name in self._settings.plugins_required:
            raise PluginError(f"必选插件 {name} 装配失败: {self._records[name].reason}")
