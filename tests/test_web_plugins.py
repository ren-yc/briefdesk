"""Web 插件扩展测试：/api/plugins 元数据、plugin-assets 静态资源、WebPlugin 装配。"""

import re
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar

from starlette.testclient import TestClient

import briefdesk.server as srv
from briefdesk.plugins.calendar.plugin import CalendarPlugin
from briefdesk.plugins.reminders.plugin import RemindersPlugin


class PluginsApiTest(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(srv.app, base_url="http://localhost")

    def tearDown(self):
        self.client.close()

    def test_no_callback_returns_empty(self):
        resp = self.client.get("/api/plugins")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"plugins": []})

    def test_callback_result_passthrough(self):
        def fake():
            return [{"name": "weflow-legacy", "version": "1.0.0", "status": "loaded", "reason": ""}]
        srv.set_plugins_info_callback(fake)
        try:
            resp = self.client.get("/api/plugins")
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.json()["plugins"][0]["name"], "weflow-legacy")
        finally:
            srv.set_plugins_info_callback(None)


class PluginAssetsTest(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(srv.app, base_url="http://localhost")

    def tearDown(self):
        self.client.close()

    def test_serves_registered_asset(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "hello.txt").write_text("hi", encoding="utf-8")
            srv.register_plugin_assets("demo", tmp)
            try:
                resp = self.client.get("/plugin-assets/demo/hello.txt")
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(resp.text, "hi")
            finally:
                srv._plugin_assets.pop("demo", None)

    def test_unknown_plugin_404(self):
        resp = self.client.get("/plugin-assets/nope/x.txt")
        self.assertEqual(resp.status_code, 404)
        # 404 必须是非 JSON（text/plain）：浏览器严格 MIME 检查会拒绝
        # application/json 作为 <link>/<script> 响应并打控制台告警
        self.assertTrue(resp.headers.get("content-type", "").startswith("text/plain"))

    def test_traversal_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "a.txt").write_text("hi", encoding="utf-8")
            srv.register_plugin_assets("demo", tmp)
            try:
                for bad in ("../a.txt", "..%2Fa.txt", "x/../../a.txt", "..\\a.txt"):
                    resp = self.client.get(f"/plugin-assets/demo/{bad}")
                    self.assertEqual(resp.status_code, 404, bad)
            finally:
                srv._plugin_assets.pop("demo", None)

    def test_missing_file_404(self):
        with tempfile.TemporaryDirectory() as tmp:
            srv.register_plugin_assets("demo", tmp)
            try:
                resp = self.client.get("/plugin-assets/demo/nope.txt")
                self.assertEqual(resp.status_code, 404)
                self.assertTrue(resp.headers.get("content-type", "").startswith("text/plain"))
            finally:
                srv._plugin_assets.pop("demo", None)


class WebPluginSetupTest(unittest.IsolatedAsyncioTestCase):
    async def test_calendar_registers_router(self):
        registered = []
        assets = []

        async def publish_event(event, payload):
            return None

        def subscribe_event(event, handler):
            return None

        from briefdesk.config import Settings
        from briefdesk.plugin.base import PluginContext

        ctx = PluginContext(
            config=Settings(plugins=["*"], plugins_disabled=[], plugins_required=[], plugin_path=""),
            publish_event=publish_event,
            subscribe_event=subscribe_event,
            register_source=lambda r: None,
            register_stage=lambda s: None,
            register_router=registered.append,
            register_plugin_assets=lambda name, d: assets.append((name, d)),
        )
        await CalendarPlugin().setup(ctx)
        await RemindersPlugin().setup(ctx)
        paths = sorted(
            {getattr(r, "path", "") for r in registered[0].routes}
            | {getattr(r, "path", "") for r in registered[1].routes}
        )
        self.assertIn("/api/calendar", paths)
        self.assertIn("/api/reminders/due", paths)
        self.assertIn("/api/items/{item_id}/reminder", paths)
        # 前端资源随插件注册：calendar / reminders 均注册 ui 目录
        self.assertEqual(
            assets,
            [
                ("calendar", str(CalendarPlugin().asset_dir())),
                ("reminders", str(RemindersPlugin().asset_dir())),
            ],
        )

    def test_web_plugins_expose_router_and_asset_dir(self):
        for cls in (CalendarPlugin, RemindersPlugin):
            plugin = cls()
            self.assertIsNotNone(plugin.router())
            self.assertIsNotNone(plugin.asset_dir())
            self.assertTrue(plugin.asset_dir().is_dir())
            self.assertTrue((plugin.asset_dir() / "ui.js").is_file())
            self.assertTrue((plugin.asset_dir() / "ui.css").is_file())


class CalendarAssetsTest(unittest.TestCase):
    """日历插件前端资源经 /plugin-assets/calendar/ 由核心提供。"""

    def test_calendar_ui_js_served(self):
        cal = CalendarPlugin()
        srv.register_plugin_assets("calendar", str(cal.asset_dir()))
        try:
            client = TestClient(srv.app, base_url="http://localhost")
            try:
                resp = client.get("/plugin-assets/calendar/ui.js")
                self.assertEqual(resp.status_code, 200)
                self.assertIn("window.briefdeskPlugins", resp.text)
                self.assertIn('"calendar"', resp.text)
            finally:
                client.close()
        finally:
            srv._plugin_assets.pop("calendar", None)

    def test_calendar_ui_css_served(self):
        cal = CalendarPlugin()
        srv.register_plugin_assets("calendar", str(cal.asset_dir()))
        try:
            client = TestClient(srv.app, base_url="http://localhost")
            try:
                resp = client.get("/plugin-assets/calendar/ui.css")
                self.assertEqual(resp.status_code, 200)
                self.assertIn(".cal-chip", resp.text)
            finally:
                client.close()
        finally:
            srv._plugin_assets.pop("calendar", None)


class RemindersAssetsTest(unittest.TestCase):
    """提醒插件前端资源经 /plugin-assets/reminders/ 由核心提供。"""

    def test_reminders_ui_js_served(self):
        rem = RemindersPlugin()
        srv.register_plugin_assets("reminders", str(rem.asset_dir()))
        try:
            client = TestClient(srv.app, base_url="http://localhost")
            try:
                resp = client.get("/plugin-assets/reminders/ui.js")
                self.assertEqual(resp.status_code, 200)
                self.assertIn("window.briefdeskPlugins", resp.text)
                self.assertIn('"reminders"', resp.text)
            finally:
                client.close()
        finally:
            srv._plugin_assets.pop("reminders", None)

    def test_reminders_ui_css_served(self):
        rem = RemindersPlugin()
        srv.register_plugin_assets("reminders", str(rem.asset_dir()))
        try:
            client = TestClient(srv.app, base_url="http://localhost")
            try:
                resp = client.get("/plugin-assets/reminders/ui.css")
                self.assertEqual(resp.status_code, 200)
                self.assertIn(".card-remind-menu", resp.text)
            finally:
                client.close()
        finally:
            srv._plugin_assets.pop("reminders", None)


class CoreFrontendBoundaryTest(unittest.TestCase):
    """前端边界守卫：插件前端全部随插件包分发，核心 ui/ 零插件残留。"""

    CORE_FILES: ClassVar[dict[str, list[str]]] = {
        "index.html": [
            "calendar-btn", "calendar-view", "calendar-detail-modal", "cal-day-modal",
            "auto-remind",
        ],
        "app.js": [
            "renderCalendar", "calChipLabel", "calDaySet", "enterCalendarMode",
            "cal-detail-row", "calendar-detail-modal", "cal-day-modal",
            "renderRemindMenu", "setReminderApi", "checkDueReminders", "btn-remind",
        ],
        "style.css": [
            ".cal-chip", "calChipIn", "#calendar-view", "body.calendar-mode",
            ".card-remind-menu", ".remind-input",
        ],
    }

    def test_core_ui_has_no_plugin_frontend(self):
        ui_dir = Path(__file__).resolve().parents[1] / "ui"
        for fname, markers in self.CORE_FILES.items():
            text = (ui_dir / fname).read_text(encoding="utf-8")
            for marker in markers:
                self.assertNotIn(marker, text, f"{fname} 不应再包含 {marker}（插件前端已随插件分发）")


class PluginFrontendCoreHelperTest(unittest.TestCase):
    """反向边界守卫：插件前端复用的核心助手必须在 app.js 里以 `function` 声明存在。

    插件前端以同源 classic script 注入、与 app.js 共享全局作用域，因此可直接调用
    app.js 的顶层 `function`（顶层 `const` 箭头不挂 window，不可用）。这层耦合没有
    模块系统兜底：app.js 里改名或改成 const 箭头，插件只会在用户点到那个控件时才
    炸，且核心测试全绿。故此处按名字对账。
    """

    def test_helpers_used_by_plugins_are_declared_in_app_js(self):
        root = Path(__file__).resolve().parents[1]
        app_js = (root / "ui" / "app.js").read_text(encoding="utf-8")
        plugin_js = sorted((root / "briefdesk" / "plugins").glob("*/ui/ui.js"))
        self.assertTrue(plugin_js, "未找到任何插件前端，守卫失效")

        # 核心提供给插件的助手全集（新增复用时同步此表）
        exported = [
            "esc", "escAttr", "showToast", "reqJson", "getJson", "postJson", "putJson",
            "deleteJson", "postVerify", "lsGet", "lsSet", "lsGetJson", "lsSetJson",
            "makeItemQuery", "catColor", "renderItemRow", "registerPluginView",
            "registerItemRowExtension",
        ]
        for name in exported:
            if f"function {name}(" in app_js:
                continue
            # catColor 是顶层 let（Map），非函数：只要求存在同名顶层声明
            self.assertRegex(
                app_js,
                rf"(?m)^(?:let|var|function)\s+{name}\b",
                f"app.js 应保留顶层声明 {name}（插件前端依赖）",
            )

        # 插件实际用到的名字必须落在上表内，且在 app.js 里真的有声明
        used = set()
        for path in plugin_js:
            text = path.read_text(encoding="utf-8")
            for name in exported:
                if re.search(rf"\b{name}\s*\(", text):
                    used.add(name)
        self.assertIn("lsSet", used, "calendar/reminders 应经 lsSet 写 localStorage")
        for name in sorted(used):
            self.assertRegex(
                app_js,
                rf"(?m)^(?:let|var|function)\s+{name}\b",
                f"插件前端调用了 {name}，但 app.js 顶层没有该声明",
            )

    def test_plugin_frontends_do_not_touch_localstorage_directly(self):
        """插件不得裸用 localStorage：异常会中断事件处理函数（见 lsGet/lsSet 注释）。"""
        root = Path(__file__).resolve().parents[1]
        for path in sorted((root / "briefdesk" / "plugins").glob("*/ui/ui.js")):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn(
                "localStorage.",
                text,
                f"{path.relative_to(root)} 应改用核心 lsGet/lsSet/lsGetJson/lsSetJson",
            )


class IncludeRouterIdempotentTest(unittest.TestCase):
    """审查修复 #10：include_plugin_router 对同一 router 重复调用幂等。"""

    def test_double_include_inserts_routes_once(self):
        from fastapi import APIRouter

        router = APIRouter()

        @router.get("/api/idempotency-probe")
        async def probe():
            return {}

        srv.include_plugin_router(router)

        def count():
            return sum(
                1
                for r in srv.app.routes
                if getattr(r, "path", "") == "/api/idempotency-probe"
            )

        try:
            self.assertEqual(count(), 1)
            srv.include_plugin_router(router)  # 第二次必须跳过
            self.assertEqual(count(), 1, "重复 include 不应插入重复路由")
        finally:
            # 清理探针路由，避免污染其它测试的路由匹配
            srv.app.routes[:] = [
                r
                for r in srv.app.routes
                if getattr(r, "path", "") != "/api/idempotency-probe"
            ]
