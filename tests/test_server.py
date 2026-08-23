"""server 路由层与安全中间件测试（monkeypatch 隔离 DB/AI）。"""

import time
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from starlette.testclient import TestClient

import briefdesk.server as srv
from briefdesk.plugins.calendar import router as calendar_router
from briefdesk.plugins.reminders import router as reminders_router

# Web 插件路由挂载（模拟 main 的装配；幂等守卫防重复 include）
for _r in (calendar_router.router, reminders_router.router):
    if not any(getattr(r, "path", "") in {getattr(a, "path", "") for a in srv.app.routes} for r in _r.routes):
        srv.include_plugin_router(_r)


class SafeMediaPathTest(unittest.TestCase):
    def test_valid(self):
        self.assertTrue(srv._is_safe_media_path("chat@room/images/abc.jpg"))
        self.assertTrue(srv._is_safe_media_path("a..b/c.jpg"))

    def test_traversal_rejected(self):
        for p in (
            "",
            "/abs/path",
            "../api/v1/contacts",
            "a/../../b",
            "a//b",
            "a/./b",
            "a\\b",
            "a%2eb",
            "a\x00b",
        ):
            self.assertFalse(srv._is_safe_media_path(p), p)


class MediaProxyRouteTest(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(srv.app, base_url="http://localhost")
        self.fake = _FakeMediaClient()
        self.patcher = patch("briefdesk.server.media.get_source_client", return_value=self.fake)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.client.close()

    def test_blocked_paths_do_not_call_download(self):
        for path in (
            "/api/media/weflow/..%2F..%2F..%2Fapi%2Fv1%2Fcontacts",
            "/api/media/weflow/%2e%2e/%2e%2e/api/v1/contacts",
            "/api/media/weflow/..%252F..%252F..%252Fapi%252Fv1%252Fcontacts",
        ):
            resp = self.client.get(path)
            self.assertEqual(resp.status_code, 404, path)
        self.assertEqual(self.fake.calls, [])

    def test_valid_path_forwards(self):
        resp = self.client.get("/api/media/weflow/chat@room/images/abc.jpg")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.fake.calls, ["chat@room/images/abc.jpg"])


class _FakeMediaClient:
    def __init__(self):
        self.calls = []

    async def download_media(self, path: str) -> bytes:
        self.calls.append(path)
        return b"ok"


class LocalSecurityGuardTest(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(srv.app, base_url="http://localhost")

    def tearDown(self):
        self.client.close()

    def test_rejects_foreign_host(self):
        resp = self.client.get("/api/status", headers={"Host": "evil.example"})
        self.assertEqual(resp.status_code, 400)

    def test_rejects_cross_origin_post(self):
        resp = self.client.post("/api/sync", headers={"Origin": "http://evil.example"})
        self.assertEqual(resp.status_code, 403)

    def test_allows_same_origin_post(self):
        resp = self.client.post("/api/sync", headers={"Origin": "http://localhost"})
        # 未注册同步回调时该路由返回 409；只要未被中间件 403 即通过
        self.assertEqual(resp.status_code, 409)

    def test_security_headers_present(self):
        resp = self.client.get("/api/status")
        self.assertTrue(resp.headers.get("content-security-policy"))
        self.assertEqual(resp.headers.get("x-content-type-options"), "nosniff")

    def test_unknown_api_is_json_404(self):
        resp = self.client.get("/api/definitely-not-exists")
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.headers.get("content-type"), "application/json")


class HasMoreTest(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(srv.app, base_url="http://localhost")

    def tearDown(self):
        self.client.close()

    def test_exact_page_reports_has_more(self):
        captured = {}

        async def fake_get_items_page(
            category=None,
            verified="unverified",
            q=None,
            limit=100,
            offset=0,
            **kwargs,
        ):
            captured["limit"] = limit
            captured.update(kwargs)
            return {
                "items": [
                    {
                        "id": str(i),
                        "msg_time": 1,
                        "created_at": "2026-01-01T00:00:00+00:00",
                    }
                    for i in range(limit)
                ],
                "total_count": 3,
                "group_count": 2,
                "source_groups": ["测试群"],
                "has_more": True,
                "next_offset": limit,
                "filter_now": "2026-08-18 12:00:00",
            }

        async def fake_counts():
            return []

        async def fake_zero(*args, **kwargs):
            return 0

        async def fake_colors():
            return []

        async def fake_disabled():
            return []

        with patch.multiple(
            "briefdesk.server.routes_items",
            get_items_page=fake_get_items_page,
            get_category_counts=fake_counts,
            get_all_category_count=fake_zero,
            get_ignored_count=fake_zero,
            get_memo_count=fake_zero,
            get_enabled_category_colors=fake_colors,
            get_disabled_category_names=fake_disabled,
            get_status_info=lambda: {
                "sources": {},
                "lastSync": "",
                "lastError": None,
                "syncing": False,
            },
        ):
            resp = self.client.get(
                "/api/items?limit=2&sourceGroup=group-a&minMsgTime=123&hideExpired=true&filterNow=2026-08-18%2012:00:00"
            )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(captured["limit"], 2)
        self.assertEqual(len(data["items"]), 2)
        self.assertTrue(data["hasMore"])
        self.assertEqual(data["filterNow"], "2026-08-18 12:00:00")
        self.assertEqual(data["totalCount"], 3)
        self.assertEqual(data["groupCount"], 2)
        self.assertEqual(data["nextOffset"], 2)
        self.assertEqual(captured["source_group"], "group-a")
        self.assertEqual(captured["min_msg_time"], 123)
        self.assertTrue(captured["hide_expired"])
        self.assertEqual(captured["now_local"], "2026-08-18 12:00:00")

    def test_invalid_filter_now_is_rejected(self):
        for value in ("not-a-time", "2026-8-18 1:02:03", "2026-02-30 12:00:00"):
            with self.subTest(value=value):
                resp = self.client.get(
                    "/api/items?hideExpired=true&filterNow=" + value
                )
                self.assertEqual(resp.status_code, 400)


class ParseFlagTest(unittest.TestCase):
    def test_accepts_bool_and_int(self):
        self.assertIs(srv._parse_flag(True, field="x", default=False), True)
        self.assertIs(srv._parse_flag(0, field="x", default=True), False)

    def test_rejects_strings(self):
        for value in ("false", "true", "1", "0"):
            with self.assertRaises(HTTPException):
                srv._parse_flag(value, field="x", default=False)

    def test_rejects_other_types(self):
        for value in (2, 1.0, [], {}):
            with self.assertRaises(HTTPException):
                srv._parse_flag(value, field="x", default=False)


class ReminderApiTest(unittest.TestCase):
    """提醒端点（reminders Web 插件）：#10 aware→本地换算、参数校验，以及 #3A 到期查询路由。"""

    def setUp(self):
        self.client = TestClient(srv.app, base_url="http://localhost")

    def tearDown(self):
        self.client.close()

    def test_set_naive_reminder_passthrough(self):
        mock = AsyncMock(return_value=True)
        with patch("briefdesk.plugins.reminders.router.set_item_reminder", new=mock):
            resp = self.client.post(
                "/api/items/i1/reminder", json={"at": "2026-08-15T10:00"}
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["remind_at"], "2026-08-15 10:00")
        mock.assert_awaited_once_with("i1", "2026-08-15 10:00")

    def test_set_aware_reminder_converts_to_server_local(self):
        given = "2026-08-15T10:00:00+08:00"
        expected = time.strftime(
            "%Y-%m-%d %H:%M", time.localtime(datetime.fromisoformat(given).timestamp())
        )
        mock = AsyncMock(return_value=True)
        with patch("briefdesk.plugins.reminders.router.set_item_reminder", new=mock):
            resp = self.client.post("/api/items/i1/reminder", json={"at": given})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["remind_at"], expected)
        mock.assert_awaited_once_with("i1", expected)

    def test_clear_reminder_passes_null(self):
        mock = AsyncMock(return_value=True)
        with patch("briefdesk.plugins.reminders.router.set_item_reminder", new=mock):
            resp = self.client.post("/api/items/i1/reminder", json={"at": None})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["remind_at"], None)
        mock.assert_awaited_once_with("i1", None)

    def test_rejects_malformed_time_before_db_call(self):
        mock = AsyncMock()
        with patch("briefdesk.plugins.reminders.router.set_item_reminder", new=mock):
            for bad in ("2026-13-45T99:99", "not-a-date", "2026-08-15"):
                resp = self.client.post("/api/items/i1/reminder", json={"at": bad})
                self.assertEqual(resp.status_code, 400, bad)
            resp = self.client.post("/api/items/i1/reminder", json={"at": 123})
            self.assertEqual(resp.status_code, 400)
        mock.assert_not_awaited()

    def test_missing_item_returns_404(self):
        mock = AsyncMock(return_value=False)
        with patch("briefdesk.plugins.reminders.router.set_item_reminder", new=mock):
            resp = self.client.post("/api/items/nope/reminder", json={"at": None})
        self.assertEqual(resp.status_code, 404)

    def test_due_endpoint_returns_items(self):
        fake = AsyncMock(
            return_value=[
                {"id": "i1", "title": "到期卡片", "category": "活动通知", "remind_at": "2000-01-01 00:00"}
            ]
        )
        with patch("briefdesk.plugins.reminders.router.get_due_reminders", new=fake):
            resp = self.client.get("/api/reminders/due")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(len(data["items"]), 1)
        self.assertEqual(data["items"][0]["id"], "i1")


class VerifyApiTest(unittest.TestCase):
    """F3：/api/items/:id/verify 对不存在的卡片返回 404（不再静默成功）。"""

    def setUp(self):
        self.client = TestClient(srv.app, base_url="http://localhost")

    def tearDown(self):
        self.client.close()

    def _patch_verify(self, mock):
        return patch.multiple(
            "briefdesk.server.routes_items",
            update_item_verify=mock,
            get_category_counts=AsyncMock(return_value=[]),
            get_all_category_count=AsyncMock(return_value=0),
            get_ignored_count=AsyncMock(return_value=0),
            get_memo_count=AsyncMock(return_value=0),
        )

    def test_verify_existing_item_ok(self):
        mock = AsyncMock(return_value=True)
        with self._patch_verify(mock):
            resp = self.client.post("/api/items/i1/verify", json={"verified": 1})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["success"])
        mock.assert_awaited_once_with("i1", 1)

    def test_verify_missing_item_returns_404(self):
        mock = AsyncMock(return_value=False)
        with self._patch_verify(mock):
            resp = self.client.post("/api/items/nope/verify", json={"verified": 1})
        self.assertEqual(resp.status_code, 404)
        mock.assert_awaited_once_with("nope", 1)

    def test_rejects_invalid_verified_value(self):
        mock = AsyncMock()
        with self._patch_verify(mock):
            resp = self.client.post("/api/items/i1/verify", json={"verified": 2})
        self.assertEqual(resp.status_code, 400)
        mock.assert_not_awaited()


class GroupCountFieldTest(unittest.TestCase):
    """/api/items 响应携带当前视图的 groupCount。"""

    def setUp(self):
        self.client = TestClient(srv.app, base_url="http://localhost")

    def tearDown(self):
        self.client.close()

    def test_group_count_in_response_with_view_params(self):
        captured = {}

        async def fake_page(
            category=None, verified="unverified", q=None, limit=100, offset=0, **kwargs
        ):
            captured["category"] = category
            captured["verified"] = verified
            captured["q"] = q
            return {
                "items": [],
                "total_count": 9,
                "group_count": 7,
                "source_groups": [],
                "has_more": False,
                "next_offset": 0,
                "filter_now": None,
            }

        async def fake_zero():
            return 0

        async def fake_list():
            return []

        with patch.multiple(
            "briefdesk.server.routes_items",
            get_items_page=fake_page,
            get_category_counts=fake_list,
            get_all_category_count=fake_zero,
            get_ignored_count=fake_zero,
            get_memo_count=fake_zero,
            get_enabled_category_colors=fake_list,
            get_disabled_category_names=fake_list,
            get_status_info=lambda: {
                "sources": {},
                "lastSync": "",
                "lastError": None,
                "syncing": False,
            },
        ):
            resp = self.client.get("/api/items?category=%E5%AD%A6%E6%9C%AF&verified=memo&q=%E8%AE%B2%E5%BA%A7")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["totalCount"], 9)
        self.assertEqual(data["groupCount"], 7)
        self.assertEqual(captured, {"category": "学术", "verified": "memo", "q": "讲座"})


if __name__ == "__main__":
    unittest.main()
