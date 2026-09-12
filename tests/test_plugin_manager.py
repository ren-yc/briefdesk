"""PluginManager 单元测试：发现、过滤、拓扑排序、生命周期与故障隔离。"""

import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from briefdesk.config import Settings
from briefdesk.plugin.base import (
    PLUGIN_GROUP,
    PluginContext,
    PluginDisabledError,
    PluginError,
)
from briefdesk.plugin.manager import PluginManager


class FakePlugin:
    """可编程假插件：记录生命周期调用，可注入失败/依赖/核心与互斥声明。"""

    def __init__(
        self,
        name: str,
        *,
        version: str = "1.0.0",
        dependencies: tuple[str, ...] = (),
        conflicts: tuple[str, ...] = (),
        core: bool = False,
        calls: list | None = None,
        setup_disabled: str | None = None,
        setup_error: Exception | None = None,
        activate_error: Exception | None = None,
    ) -> None:
        self.name = name
        self.version = version
        self.dependencies = dependencies
        self.conflicts = conflicts
        self.core = core
        self.calls = calls if calls is not None else []
        self.setup_disabled = setup_disabled
        self.setup_error = setup_error
        self.activate_error = activate_error

    async def setup(self, ctx: PluginContext) -> None:
        self.calls.append(("setup", self.name))
        if self.setup_disabled:
            raise PluginDisabledError(self.setup_disabled)
        if self.setup_error:
            raise self.setup_error

    async def activate(self, ctx: PluginContext) -> None:
        self.calls.append(("activate", self.name))
        if self.activate_error:
            raise self.activate_error

    async def teardown(self) -> None:
        self.calls.append(("teardown", self.name))


class _BrokenAssetDirPlugin(FakePlugin):
    """asset_dir() 抛异常的插件：has_frontend 应安全回退 False。"""

    def asset_dir(self):
        raise RuntimeError("boom")


class _SettingsPlugin(FakePlugin):
    def settings_schema(self):
        return [
            {
                "key": "EXAMPLE_LIMIT",
                "type": "number",
                "numberKind": "integer",
                "min": 1,
                "current": 3,
            }
        ]


class _EmptyEPS(list):
    """空 entry point 列表桩（manager 只调用 .select(group=...)）。"""

    def select(self, *, group: str | None = None, name: str | None = None):
        return [e for e in self if e.group == group]


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "plugins": [],
        "plugins_required": [],
        "plugin_path": "",
    }
    values.update(overrides)
    return Settings(**values)


def make_ctx(settings: Settings | None = None) -> PluginContext:
    async def publish_event(event: str, payload: Any) -> None:
        return None

    def subscribe_event(event: str, handler: Any) -> None:
        return None

    def register_source(runtime: Any) -> None:
        return None

    def register_stage(stage: Any) -> None:
        return None

    return PluginContext(
        config=settings if settings is not None else make_settings(),
        publish_event=publish_event,
        subscribe_event=subscribe_event,
        register_source=register_source,
        register_stage=register_stage,
    )


class _ManagerTestBase:
    """测试基类：隔离真实 entry point 环境，避免本机安装的插件干扰断言。"""

    @pytest.fixture(autouse=True)
    async def _autouse_setup(self):
        self._eps_patch = patch(
            "importlib.metadata.entry_points", return_value=_EmptyEPS([])
        )
        self._eps_patch.start()
        yield
        self._eps_patch.stop()


class TestSetupOrder(_ManagerTestBase):
    async def test_dependency_topological_order(self):
        calls: list = []
        manager = PluginManager(make_settings(plugins=["c", "b", "a"]))
        manager.register(FakePlugin("c", dependencies=("a", "b"), calls=calls))
        manager.register(FakePlugin("b", dependencies=("a",), calls=calls))
        manager.register(FakePlugin("a", calls=calls))
        await manager.setup_all(make_ctx())
        setups = [c for c in calls if c[0] == "setup"]
        assert setups == [("setup", "a"), ("setup", "b"), ("setup", "c")]

    async def test_activate_follows_load_order(self):
        calls: list = []
        manager = PluginManager(make_settings(plugins=["b", "a"]))
        manager.register(FakePlugin("b", dependencies=("a",), calls=calls))
        manager.register(FakePlugin("a", calls=calls))
        await manager.setup_all(make_ctx())
        await manager.activate_all(make_ctx())
        activates = [c for c in calls if c[0] == "activate"]
        assert activates == [("activate", "a"), ("activate", "b")]

    async def test_teardown_reverse_order_and_idempotent(self):
        calls: list = []
        manager = PluginManager(make_settings(plugins=["b", "a"]))
        manager.register(FakePlugin("b", dependencies=("a",), calls=calls))
        manager.register(FakePlugin("a", calls=calls))
        await manager.setup_all(make_ctx())
        await manager.teardown_all()
        teardowns = [c for c in calls if c[0] == "teardown"]
        assert teardowns == [("teardown", "b"), ("teardown", "a")]
        await manager.teardown_all()  # 幂等：第二次不重复调用
        assert len([c for c in calls if c[0] == "teardown"]) == 2

    async def test_core_plugins_activated_without_plugins_config(self):
        """核心插件恒装配：PLUGINS 为空也加载；可选插件不列出即不加载。"""
        calls: list = []
        manager = PluginManager(make_settings())
        manager.register(FakePlugin("core", core=True, calls=calls))
        manager.register(FakePlugin("opt", calls=calls))
        await manager.setup_all(make_ctx())
        assert manager.loaded == ["core"]
        assert manager.records()["opt"].status == "disabled"
        assert "未启用" in manager.records()["opt"].reason


class TestFilter(_ManagerTestBase):
    async def test_allowlist_filters_optional_plugins(self):
        calls: list = []
        manager = PluginManager(make_settings(plugins=["b"]))
        manager.register(FakePlugin("a", calls=calls))
        manager.register(FakePlugin("b", calls=calls))
        await manager.setup_all(make_ctx())
        assert manager.loaded == ["b"]

    async def test_unknown_plugins_name_only_warns(self):
        """PLUGINS 含未知名：仅 WARNING（启动继续），不影响已知插件。"""
        calls: list = []
        manager = PluginManager(make_settings(plugins=["ghost", "a"]))
        manager.register(FakePlugin("a", calls=calls))
        await manager.setup_all(make_ctx())
        assert manager.loaded == ["a"]

    async def test_star_is_unknown_not_wildcard(self):
        """无通配语义：PLUGINS 里的 "*" 按未知名处理——只 WARNING、不启用
        任何可选插件（旧 ["*"] 配置升级后即零源，语义须钉死防回潮）。"""
        calls: list = []
        manager = PluginManager(make_settings(plugins=["*"]))
        manager.register(FakePlugin("a", calls=calls))
        await manager.setup_all(make_ctx())
        assert manager.loaded == []
        assert manager.records()["a"].status == "disabled"

    async def test_optional_plugin_not_listed_is_disabled(self):
        """可选插件「禁用 = 不在 PLUGINS 中」：无独立的禁用名单配置。"""
        calls: list = []
        manager = PluginManager(make_settings())
        manager.register(FakePlugin("a", calls=calls))
        await manager.setup_all(make_ctx())
        rec = manager.records()["a"]
        assert rec.status == "disabled"
        assert "互斥" not in rec.reason


class TestConflict(_ManagerTestBase):
    """互斥仲裁：互斥对同现按 PLUGINS 先列者保留（手工改 .env 才可能同现）。"""

    async def test_conflict_first_in_plugins_wins(self):
        manager = PluginManager(make_settings(plugins=["a", "b"]))
        manager.register(FakePlugin("a", conflicts=("b",)))
        manager.register(FakePlugin("b", conflicts=("a",)))
        await manager.setup_all(make_ctx())
        assert manager.loaded == ["a"]
        rec = manager.records()["b"]
        assert rec.status == "disabled"
        assert "与 a 互斥" in rec.reason  # /api/plugins 可见原因

    async def test_conflict_reason_aggregates_all_winners(self):
        """同一落选者与多个 winner 冲突 → reason 汇总全部对端
        不再被最后一个 winner 覆盖）。"""
        manager = PluginManager(make_settings(plugins=["c", "b", "a"]))
        manager.register(FakePlugin("a", conflicts=("b", "c")))
        manager.register(FakePlugin("b", conflicts=("a",)))
        manager.register(FakePlugin("c", conflicts=("a",)))
        await manager.setup_all(make_ctx())
        assert manager.loaded == ["b", "c"]
        rec = manager.records()["a"]
        assert rec.status == "disabled"
        assert "与 b、c 互斥" in rec.reason

    async def test_conflict_arbitration_follows_plugins_order(self):
        manager = PluginManager(make_settings(plugins=["b", "a"]))
        manager.register(FakePlugin("a", conflicts=("b",)))
        manager.register(FakePlugin("b", conflicts=("a",)))
        await manager.setup_all(make_ctx())
        assert manager.loaded == ["b"]

    async def test_conflict_core_plugin_always_wins(self):
        """第三方可选插件与核心插件互斥：核心恒装配，可选侧让位。"""
        manager = PluginManager(make_settings(plugins=["opt"]))
        manager.register(FakePlugin("core", core=True))
        manager.register(FakePlugin("opt", conflicts=("core",)))
        await manager.setup_all(make_ctx())
        assert manager.loaded == ["core"]
        assert manager.records()["opt"].status == "disabled"
        assert "与 core 互斥" in manager.records()["opt"].reason

    async def test_asymmetric_conflict_declaration(self):
        """对称声明不是硬性要求：单侧声明同样参与仲裁。"""
        manager = PluginManager(make_settings(plugins=["x", "y"]))
        manager.register(FakePlugin("x", conflicts=("y",)))
        manager.register(FakePlugin("y"))
        await manager.setup_all(make_ctx())
        assert manager.loaded == ["x"]
        assert manager.records()["y"].status == "disabled"

    async def test_conflict_only_when_both_selected(self):
        """对端未入选（未启用）时不触发仲裁。"""
        calls: list = []
        manager = PluginManager(make_settings(plugins=["x"]))
        manager.register(FakePlugin("x", conflicts=("y",), calls=calls))
        manager.register(FakePlugin("y", calls=calls))
        await manager.setup_all(make_ctx())
        assert manager.loaded == ["x"]


class TestValidateSelection(_ManagerTestBase):
    """validate_selection：设置 API 写入前的期望启用集合纯校验。"""

    def _manager(self) -> PluginManager:
        manager = PluginManager(make_settings())
        manager.register(FakePlugin("src"))
        manager.register(FakePlugin("stage", dependencies=("src",)))
        manager.register(FakePlugin("bench", dependencies=("ai",)))
        manager.register(FakePlugin("ai", core=True))
        return manager

    def test_valid_selection_passes(self):
        assert self._manager().validate_selection(["src", "stage"]) == []

    def test_core_names_implicitly_satisfied(self):
        """依赖指向核心插件视为恒满足；核心插件本身无需列入集合。"""
        assert self._manager().validate_selection(["bench"]) == []

    def test_unknown_name_reported(self):
        issues = self._manager().validate_selection(["ghost"])
        assert [i["type"] for i in issues] == ["unknown"]
        assert "ghost" in issues[0]["detail"]

    def test_star_rejected_as_unknown(self):
        """无通配语义：旧 ["*"] 列表提交时按未知名报错（PUT 会 409），
        不会意外放行全部插件。"""
        issues = self._manager().validate_selection(["*"])
        assert [i["type"] for i in issues] == ["unknown"]
        assert "*" in issues[0]["detail"]

    def test_missing_dep_reported(self):
        issues = self._manager().validate_selection(["stage"])
        assert [i["type"] for i in issues] == ["missing_dep"]
        assert issues[0]["plugin"] == "stage"
        assert "src" in issues[0]["detail"]

    def test_conflict_reported_once_per_pair(self):
        manager = self._manager()
        manager.register(FakePlugin("x", conflicts=("y",)))
        manager.register(FakePlugin("y", conflicts=("x",)))
        issues = manager.validate_selection(["x", "y"])
        assert [i["type"] for i in issues] == ["conflict"]

    def test_cycle_reported_per_member(self):
        manager = self._manager()
        manager.register(FakePlugin("u", dependencies=("v",)))
        manager.register(FakePlugin("v", dependencies=("u",)))
        issues = manager.validate_selection(["u", "v"])
        assert {i["type"] for i in issues} == {"cycle"}
        assert {i["plugin"] for i in issues} == {"u", "v"}

    def test_duplicate_names_deduplicated(self):
        assert self._manager().validate_selection(["src", "src"]) == []

    def test_unknown_dep_of_selected_plugin_reported(self):
        """依赖指向不存在的插件：missing_dep（插件不存在），非 unknown。"""
        manager = self._manager()
        manager.register(FakePlugin("bad", dependencies=("ghost",)))
        issues = manager.validate_selection(["bad"])
        assert [i["type"] for i in issues] == ["missing_dep"]
        assert "不存在" in issues[0]["detail"]


class TestFailureIsolation(_ManagerTestBase):
    async def test_self_disabled_isolated(self):
        manager = PluginManager(make_settings(plugins=["a", "b"]))
        manager.register(FakePlugin("a", setup_disabled="缺少必填配置"))
        manager.register(FakePlugin("b"))
        await manager.setup_all(make_ctx())
        assert manager.loaded == ["b"]
        rec = manager.records()["a"]
        assert rec.status == "disabled"
        assert "缺少必填配置" in rec.reason

    async def test_setup_error_isolated(self):
        manager = PluginManager(make_settings(plugins=["a", "b"]))
        manager.register(FakePlugin("a", setup_error=RuntimeError("boom")))
        manager.register(FakePlugin("b"))
        await manager.setup_all(make_ctx())
        assert manager.loaded == ["b"]
        assert manager.records()["a"].status == "failed"

    async def test_setup_failure_runs_teardown_and_leaves_no_ports(self):
        """守卫：setup 中先注册端口再抛错 → 框架仍调用 teardown、插件
        标 failed、遵守契约的插件回收后 ctx.ai/ctx.dedup 无残留。"""

        class _RegisterThenFailPlugin(FakePlugin):
            def __init__(self, name, **kwargs):
                super().__init__(name, **kwargs)
                self._ctx = None

            async def setup(self, ctx):
                self.calls.append(("setup", self.name))
                self._ctx = ctx
                ctx.register_stage(self)
                ctx.ai = object()  # 模拟端口注册
                ctx.dedup = object()
                raise RuntimeError("注册后爆炸")

            async def teardown(self):
                self.calls.append(("teardown", self.name))
                # 契约合规的插件：teardown 幂等回收自己注册的端口
                if self._ctx is not None:
                    self._ctx.ai = None
                    self._ctx.dedup = None
                    self._ctx = None

        manager = PluginManager(make_settings(plugins=["a"]))
        plugin = _RegisterThenFailPlugin("a")
        manager.register(plugin)
        ctx = make_ctx()
        await manager.setup_all(ctx)
        assert ("teardown", "a") in plugin.calls, "框架须 best-effort teardown"
        assert manager.records()["a"].status == "failed"
        assert ctx.ai is None, "按契约回收后 ctx.ai 不得残留"
        assert ctx.dedup is None, "按契约回收后 ctx.dedup 不得残留"

    async def test_unknown_dependency_disables(self):
        manager = PluginManager(make_settings(plugins=["a"]))
        manager.register(FakePlugin("a", dependencies=("ghost",)))
        await manager.setup_all(make_ctx())
        rec = manager.records()["a"]
        assert rec.status == "disabled"
        assert "未知依赖" in rec.reason

    async def test_cycle_disables_both(self):
        manager = PluginManager(make_settings(plugins=["a", "b"]))
        manager.register(FakePlugin("a", dependencies=("b",)))
        manager.register(FakePlugin("b", dependencies=("a",)))
        await manager.setup_all(make_ctx())
        for name in ("a", "b"):
            rec = manager.records()[name]
            assert rec.status == "disabled"
            assert "依赖环" in rec.reason

    async def test_depends_on_failed_dependency_disables(self):
        manager = PluginManager(make_settings(plugins=["a", "b"]))
        manager.register(FakePlugin("a", setup_error=RuntimeError("boom")))
        manager.register(FakePlugin("b", dependencies=("a",)))
        await manager.setup_all(make_ctx())
        rec = manager.records()["b"]
        assert rec.status == "disabled"
        assert "依赖未就绪" in rec.reason

    async def test_depends_on_unlisted_optional_dependency_disables(self):
        """依赖指向未列入 PLUGINS 的可选插件：setup 期降级「依赖未就绪」。"""
        manager = PluginManager(make_settings(plugins=["b"]))
        manager.register(FakePlugin("a"))
        manager.register(FakePlugin("b", dependencies=("a",)))
        await manager.setup_all(make_ctx())
        rec = manager.records()["b"]
        assert rec.status == "disabled"
        assert "依赖未就绪" in rec.reason

    async def test_required_failure_raises(self):
        manager = PluginManager(make_settings(plugins=["a"], plugins_required=["a"]))
        manager.register(FakePlugin("a", setup_error=RuntimeError("boom")))
        with pytest.raises(PluginError):
            await manager.setup_all(make_ctx())

    async def test_required_self_disabled_raises(self):
        manager = PluginManager(make_settings(plugins=["a"], plugins_required=["a"]))
        manager.register(FakePlugin("a", setup_disabled="缺少配置"))
        with pytest.raises(PluginError):
            await manager.setup_all(make_ctx())

    async def test_activate_error_isolated(self):
        manager = PluginManager(make_settings(plugins=["a", "b"]))
        manager.register(FakePlugin("a", activate_error=RuntimeError("boom")))
        manager.register(FakePlugin("b"))
        await manager.setup_all(make_ctx())
        await manager.activate_all(make_ctx())
        rec = manager.records()["a"]
        assert rec.status == "failed"
        assert "activate 失败" in rec.reason


class TestRegistration(unittest.TestCase):
    def test_duplicate_name_skipped(self):
        manager = PluginManager(make_settings())
        manager.register(FakePlugin("a"))
        manager.register(FakePlugin("a", version="2.0.0"))
        assert len(manager.records()) == 1
        assert manager.records()["a"].version == "1.0.0"

    def test_object_without_name_rejected(self):
        manager = PluginManager(make_settings())
        manager.register(object())  # type: ignore[arg-type]
        rec = manager.records()["register"]
        assert rec.status == "failed"
        assert "name" in rec.reason

    def test_missing_lifecycle_method_rejected(self):
        manager = PluginManager(make_settings())

        class Partial:
            name = "partial"
            version = "0"
            dependencies = ()

            async def setup(self, ctx: PluginContext) -> None: ...

        manager.register(Partial())  # type: ignore[arg-type]
        rec = manager.records()["partial"]
        assert rec.status == "failed"
        assert "activate" in rec.reason

    def test_infos_include_has_frontend(self):
        # 前端加载器据此只对有前端资源的插件注入 ui.css/ui.js
        class WithFrontend(FakePlugin):
            def asset_dir(self):
                return Path("somewhere")

        manager = PluginManager(make_settings())
        manager.register(FakePlugin("backend"))       # 无 asset_dir → False
        manager.register(WithFrontend("frontend"))    # asset_dir 非 None → True
        manager.register(_BrokenAssetDirPlugin("broken"))  # asset_dir 抛错 → False
        by_name = {i["name"]: i for i in manager.infos()}
        assert by_name["backend"]["has_frontend"] is False
        assert by_name["frontend"]["has_frontend"] is True
        assert by_name["broken"]["has_frontend"] is False

    def test_infos_include_core_flag(self):
        manager = PluginManager(make_settings())
        manager.register(FakePlugin("core", core=True))
        manager.register(FakePlugin("opt"))
        by_name = {i["name"]: i for i in manager.infos()}
        assert by_name["core"]["core"] is True
        assert by_name["opt"]["core"] is False

    def test_core_plugin_with_conflicts_rejected(self):
        """核心插件恒装配，互斥无法仲裁：声明 conflicts 即接受失败。"""
        manager = PluginManager(make_settings())
        manager.register(FakePlugin("cc", core=True, conflicts=("other",)))
        rec = manager.records()["cc"]
        assert rec.status == "failed"
        assert "互斥" in rec.reason

    def test_conflicts_self_reference_rejected(self):
        manager = PluginManager(make_settings())
        manager.register(FakePlugin("bad", conflicts=("bad",)))
        rec = manager.records()["bad"]
        assert rec.status == "failed"
        assert "自指或重复" in rec.reason

    def test_conflicts_non_string_rejected(self):
        manager = PluginManager(make_settings())
        manager.register(FakePlugin("bad", conflicts=(1,)))  # type: ignore[list-item]
        rec = manager.records()["bad"]
        assert rec.status == "failed"
        assert "字符串" in rec.reason


class TestSettingsSchema(_ManagerTestBase):
    async def test_selected_plugin_schema_is_returned(self):
        manager = PluginManager(make_settings(plugins=["example"]))
        manager.register(_SettingsPlugin("example"))
        await manager.setup_all(make_ctx())
        schema = manager.settings_schema()
        assert schema[0]["key"] == "EXAMPLE_LIMIT"
        assert schema[0]["plugin"] == "example"
        assert schema[0]["pluginStatus"] == "loaded"

    async def test_self_disabled_plugin_schema_remains_configurable(self):
        manager = PluginManager(make_settings())
        manager.register(
            _SettingsPlugin("example", setup_disabled="missing configuration")
        )
        await manager.setup_all(make_ctx())
        schema = manager.settings_schema()
        assert schema[0]["pluginStatus"] == "disabled"

    async def test_unlisted_optional_plugin_schema_remains_configurable(self):
        """可选插件未列入 PLUGINS：设置页仍展示其配置（启用前可预配置）。"""
        manager = PluginManager(make_settings())  # PLUGINS 为空
        manager.register(_SettingsPlugin("example"))
        await manager.setup_all(make_ctx())
        schema = manager.settings_schema()
        assert [f["plugin"] for f in schema] == ["example"]
        assert schema[0]["pluginStatus"] == "disabled"


class TestDiscovery(_ManagerTestBase):
    async def test_entry_point_discovery(self):
        from importlib.metadata import EntryPoint

        class _EPS(list):
            def select(self, *, group: str | None = None, name: str | None = None):
                return [e for e in self if e.group == group]

        eps = _EPS(
            [
                EntryPoint(
                    name="ep_a",
                    value="tests._plugin_fixtures:plugin_a",
                    group=PLUGIN_GROUP,
                )
            ]
        )
        manager = PluginManager(make_settings())
        with patch("importlib.metadata.entry_points", return_value=eps):
            manager.discover()
        assert "ep_a" in manager.records()
        assert manager.records()["ep_a"].version == "fixture"

    async def test_entry_point_load_failure_recorded(self):
        from importlib.metadata import EntryPoint

        class _EPS(list):
            def select(self, *, group: str | None = None, name: str | None = None):
                return [e for e in self if e.group == group]

        eps = _EPS(
            [
                EntryPoint(
                    name="broken",
                    value="tests._no_such_module:nope",
                    group=PLUGIN_GROUP,
                )
            ]
        )
        manager = PluginManager(make_settings())
        with patch("importlib.metadata.entry_points", return_value=eps):
            manager.discover()
        rec = manager.records()["broken"]
        assert rec.status == "failed"
        assert "加载失败" in rec.reason

    async def test_plugin_path_discovery(self):
        # 夹具目录随仓库提交：沙箱环境不允许写系统临时目录，
        # 测试夹具直接使用 tests/plugin_path_fixtures/ 下的真实文件
        fixture_dir = str(Path(__file__).parent / "plugin_path_fixtures")
        manager = PluginManager(make_settings(plugin_path=fixture_dir))
        manager.discover()
        assert "hello" in manager.records()
        assert manager.records()["hello"].version == "0.1"

    async def test_plugin_path_missing_plugin_instance_recorded(self):
        fixture_dir = str(Path(__file__).parent / "plugin_path_fixtures")
        manager = PluginManager(make_settings(plugin_path=fixture_dir))
        manager.discover()
        rec = manager.records()["empty.py"]
        assert rec.status == "failed"
        assert "plugin" in rec.reason

    async def test_discover_idempotent(self):
        manager = PluginManager(make_settings())
        manager.register(FakePlugin("a"))
        manager.discover()
        manager.discover()
        assert len(manager.records()) == 1
