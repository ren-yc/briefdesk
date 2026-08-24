"""PluginManager 单元测试：发现、过滤、拓扑排序、生命周期与故障隔离。"""

import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from briefdesk.config import Settings
from briefdesk.plugin.base import (
    PLUGIN_GROUP,
    PluginContext,
    PluginDisabledError,
    PluginError,
)
from briefdesk.plugin.manager import PluginManager


class FakePlugin:
    """可编程假插件：记录生命周期调用，可注入失败/依赖。"""

    def __init__(
        self,
        name: str,
        *,
        version: str = "1.0.0",
        dependencies: tuple[str, ...] = (),
        default_disabled: bool = False,
        calls: list | None = None,
        setup_disabled: str | None = None,
        setup_error: Exception | None = None,
        activate_error: Exception | None = None,
    ) -> None:
        self.name = name
        self.version = version
        self.dependencies = dependencies
        self.default_disabled = default_disabled
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


class _EmptyEPS(list):
    """空 entry point 列表桩（manager 只调用 .select(group=...)）。"""

    def select(self, *, group: str | None = None, name: str | None = None):
        return [e for e in self if e.group == group]


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "plugins": ["*"],
        "plugins_disabled": [],
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


class _ManagerTestBase(unittest.IsolatedAsyncioTestCase):
    """测试基类：隔离真实 entry point 环境，避免本机安装的插件干扰断言。"""

    async def asyncSetUp(self) -> None:
        self._eps_patch = patch(
            "importlib.metadata.entry_points", return_value=_EmptyEPS([])
        )
        self._eps_patch.start()

    async def asyncTearDown(self) -> None:
        self._eps_patch.stop()


class SetupOrderTest(_ManagerTestBase):
    async def test_dependency_topological_order(self):
        calls: list = []
        manager = PluginManager(make_settings())
        manager.register(FakePlugin("c", dependencies=("a", "b"), calls=calls))
        manager.register(FakePlugin("b", dependencies=("a",), calls=calls))
        manager.register(FakePlugin("a", calls=calls))
        await manager.setup_all(make_ctx())
        setups = [c for c in calls if c[0] == "setup"]
        self.assertEqual(
            setups, [("setup", "a"), ("setup", "b"), ("setup", "c")]
        )

    async def test_activate_follows_load_order(self):
        calls: list = []
        manager = PluginManager(make_settings())
        manager.register(FakePlugin("b", dependencies=("a",), calls=calls))
        manager.register(FakePlugin("a", calls=calls))
        await manager.setup_all(make_ctx())
        await manager.activate_all(make_ctx())
        activates = [c for c in calls if c[0] == "activate"]
        self.assertEqual(activates, [("activate", "a"), ("activate", "b")])

    async def test_teardown_reverse_order_and_idempotent(self):
        calls: list = []
        manager = PluginManager(make_settings())
        manager.register(FakePlugin("b", dependencies=("a",), calls=calls))
        manager.register(FakePlugin("a", calls=calls))
        await manager.setup_all(make_ctx())
        await manager.teardown_all()
        teardowns = [c for c in calls if c[0] == "teardown"]
        self.assertEqual(teardowns, [("teardown", "b"), ("teardown", "a")])
        await manager.teardown_all()  # 幂等：第二次不重复调用
        self.assertEqual(len([c for c in calls if c[0] == "teardown"]), 2)


class FilterTest(_ManagerTestBase):
    async def test_allowlist_filters(self):
        calls: list = []
        manager = PluginManager(make_settings(plugins=["b"]))
        manager.register(FakePlugin("a", calls=calls))
        manager.register(FakePlugin("b", calls=calls))
        await manager.setup_all(make_ctx())
        self.assertEqual(manager.loaded, ["b"])

    async def test_disabled_list_filters(self):
        calls: list = []
        manager = PluginManager(
            make_settings(plugins=["*"], plugins_disabled=["a"])
        )
        manager.register(FakePlugin("a", calls=calls))
        manager.register(FakePlugin("b", calls=calls))
        await manager.setup_all(make_ctx())
        self.assertEqual(manager.loaded, ["b"])

    async def test_default_disabled_excluded_unless_explicit(self):
        """声明 default_disabled 的插件：PLUGINS=["*"] 默认不加载，显式列名才启用。"""
        calls: list = []
        manager = PluginManager(make_settings())
        manager.register(FakePlugin("bench", default_disabled=True, calls=calls))
        manager.register(FakePlugin("b", calls=calls))
        await manager.setup_all(make_ctx())
        self.assertEqual(manager.loaded, ["b"])
        rec = manager.records()["bench"]
        self.assertEqual(rec.status, "disabled")
        self.assertIn("默认禁用", rec.reason)  # /api/plugins 可见原因

    async def test_default_disabled_explicit_with_wildcard(self):
        calls: list = []
        manager = PluginManager(make_settings(plugins=["*", "bench"]))
        manager.register(FakePlugin("bench", default_disabled=True, calls=calls))
        manager.register(FakePlugin("b", calls=calls))
        await manager.setup_all(make_ctx())
        self.assertEqual(manager.loaded, ["bench", "b"])
        self.assertEqual(manager.records()["bench"].status, "loaded")

    async def test_default_disabled_explicit_allowlist(self):
        calls: list = []
        manager = PluginManager(make_settings(plugins=["bench"]))
        manager.register(FakePlugin("bench", default_disabled=True, calls=calls))
        await manager.setup_all(make_ctx())
        self.assertEqual(manager.loaded, ["bench"])

    async def test_explicit_disable_overrides_default_disabled(self):
        """PLUGINS_DISABLED 优先级最高：即便显式列名也被禁用。"""
        calls: list = []
        manager = PluginManager(
            make_settings(plugins=["*", "bench"], plugins_disabled=["bench"])
        )
        manager.register(FakePlugin("bench", default_disabled=True, calls=calls))
        manager.register(FakePlugin("b", calls=calls))
        await manager.setup_all(make_ctx())
        self.assertEqual(manager.loaded, ["b"])


class FailureIsolationTest(_ManagerTestBase):
    async def test_self_disabled_isolated(self):
        manager = PluginManager(make_settings())
        manager.register(FakePlugin("a", setup_disabled="缺少必填配置"))
        manager.register(FakePlugin("b"))
        await manager.setup_all(make_ctx())
        self.assertEqual(manager.loaded, ["b"])
        rec = manager.records()["a"]
        self.assertEqual(rec.status, "disabled")
        self.assertIn("缺少必填配置", rec.reason)

    async def test_setup_error_isolated(self):
        manager = PluginManager(make_settings())
        manager.register(FakePlugin("a", setup_error=RuntimeError("boom")))
        manager.register(FakePlugin("b"))
        await manager.setup_all(make_ctx())
        self.assertEqual(manager.loaded, ["b"])
        self.assertEqual(manager.records()["a"].status, "failed")

    async def test_unknown_dependency_disables(self):
        manager = PluginManager(make_settings())
        manager.register(FakePlugin("a", dependencies=("ghost",)))
        await manager.setup_all(make_ctx())
        rec = manager.records()["a"]
        self.assertEqual(rec.status, "disabled")
        self.assertIn("未知依赖", rec.reason)

    async def test_cycle_disables_both(self):
        manager = PluginManager(make_settings())
        manager.register(FakePlugin("a", dependencies=("b",)))
        manager.register(FakePlugin("b", dependencies=("a",)))
        await manager.setup_all(make_ctx())
        for name in ("a", "b"):
            rec = manager.records()[name]
            self.assertEqual(rec.status, "disabled")
            self.assertIn("依赖环", rec.reason)

    async def test_depends_on_failed_dependency_disables(self):
        manager = PluginManager(make_settings())
        manager.register(FakePlugin("a", setup_error=RuntimeError("boom")))
        manager.register(FakePlugin("b", dependencies=("a",)))
        await manager.setup_all(make_ctx())
        rec = manager.records()["b"]
        self.assertEqual(rec.status, "disabled")
        self.assertIn("依赖未就绪", rec.reason)

    async def test_required_failure_raises(self):
        manager = PluginManager(make_settings(plugins_required=["a"]))
        manager.register(FakePlugin("a", setup_error=RuntimeError("boom")))
        with self.assertRaises(PluginError):
            await manager.setup_all(make_ctx())

    async def test_required_self_disabled_raises(self):
        manager = PluginManager(make_settings(plugins_required=["a"]))
        manager.register(FakePlugin("a", setup_disabled="缺少配置"))
        with self.assertRaises(PluginError):
            await manager.setup_all(make_ctx())

    async def test_activate_error_isolated(self):
        manager = PluginManager(make_settings())
        manager.register(FakePlugin("a", activate_error=RuntimeError("boom")))
        manager.register(FakePlugin("b"))
        await manager.setup_all(make_ctx())
        await manager.activate_all(make_ctx())
        rec = manager.records()["a"]
        self.assertEqual(rec.status, "failed")
        self.assertIn("activate 失败", rec.reason)


class RegistrationTest(unittest.TestCase):
    def test_duplicate_name_skipped(self):
        manager = PluginManager(make_settings())
        manager.register(FakePlugin("a"))
        manager.register(FakePlugin("a", version="2.0.0"))
        self.assertEqual(len(manager.records()), 1)
        self.assertEqual(manager.records()["a"].version, "1.0.0")

    def test_object_without_name_rejected(self):
        manager = PluginManager(make_settings())
        manager.register(object())  # type: ignore[arg-type]
        rec = manager.records()["register"]
        self.assertEqual(rec.status, "failed")
        self.assertIn("name", rec.reason)

    def test_missing_lifecycle_method_rejected(self):
        manager = PluginManager(make_settings())

        class Partial:
            name = "partial"
            version = "0"
            dependencies = ()

            async def setup(self, ctx: PluginContext) -> None: ...

        manager.register(Partial())  # type: ignore[arg-type]
        rec = manager.records()["partial"]
        self.assertEqual(rec.status, "failed")
        self.assertIn("activate", rec.reason)

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
        self.assertIs(by_name["backend"]["has_frontend"], False)
        self.assertIs(by_name["frontend"]["has_frontend"], True)
        self.assertIs(by_name["broken"]["has_frontend"], False)


class DiscoveryTest(_ManagerTestBase):
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
        self.assertIn("ep_a", manager.records())
        self.assertEqual(manager.records()["ep_a"].version, "fixture")

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
        self.assertEqual(rec.status, "failed")
        self.assertIn("加载失败", rec.reason)

    async def test_plugin_path_discovery(self):
        # 夹具目录随仓库提交：沙箱环境不允许写系统临时目录，
        # 测试夹具直接使用 tests/plugin_path_fixtures/ 下的真实文件
        fixture_dir = str(Path(__file__).parent / "plugin_path_fixtures")
        manager = PluginManager(make_settings(plugin_path=fixture_dir))
        manager.discover()
        self.assertIn("hello", manager.records())
        self.assertEqual(manager.records()["hello"].version, "0.1")

    async def test_plugin_path_missing_plugin_instance_recorded(self):
        fixture_dir = str(Path(__file__).parent / "plugin_path_fixtures")
        manager = PluginManager(make_settings(plugin_path=fixture_dir))
        manager.discover()
        rec = manager.records()["empty.py"]
        self.assertEqual(rec.status, "failed")
        self.assertIn("plugin", rec.reason)

    async def test_discover_idempotent(self):
        manager = PluginManager(make_settings())
        manager.register(FakePlugin("a"))
        manager.discover()
        manager.discover()
        self.assertEqual(len(manager.records()), 1)
