"""Web 插件扩展测试：/api/plugins 元数据、plugin-assets 静态资源、WebPlugin 装配。"""

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
            return [{"name": "weflow", "version": "1.0.0", "status": "loaded", "reason": ""}]
        srv.set_plugins_info_callback(fake)
        try:
            resp = self.client.get("/api/plugins")
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.json()["plugins"][0]["name"], "weflow")
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
