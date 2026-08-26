"""server 路由层与安全中间件测试（monkeypatch 隔离 DB/AI）。"""

import asyncio
import time
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from starlette.testclient import TestClient

import briefdesk.server as srv
from briefdesk.events import EVENT_ITEMS_DELETED
from briefdesk.plugins.calendar import router as calendar_router
from briefdesk.plugins.reminders import router as reminders_router


def _client(**kwargs):
    """统一 TestClient 构造：模拟前端同源请求（浏览器对 fetch POST 总是携带 Origin）。

    中间件 CSRF 收口后，变更接口要求 Origin/Referer 至少存在其一；
    测试默认带上同源 Origin，需要验证「双缺」的用例自行另建裸 client。
    """
    return TestClient(
        srv.app, base_url="http://localhost", headers={"Origin": "http://localhost"}, **kwargs
    )


# Web 插件路由挂载（模拟 main 的装配；include_plugin_router 已幂等）
for _r in (calendar_router.router, reminders_router.router):
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
        self.client = _client()
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
        self.payload = b"ok"

    async def download_media(self, path: str) -> bytes:
        self.calls.append(path)
        return self.payload


class LocalSecurityGuardTest(unittest.TestCase):
    def setUp(self):
        self.client = _client()

    def tearDown(self):
        self.client.close()

    def test_rejects_foreign_host(self):
        resp = self.client.get("/api/status", headers={"Host": "evil.example"})
        self.assertEqual(resp.status_code, 400)

    def test_rejects_cross_origin_post(self):
        resp = self.client.post("/api/sync", headers={"Origin": "http://evil.example"})
        self.assertEqual(resp.status_code, 403)

    def test_rejects_post_without_origin_and_referer(self):
        # 审查修复 #3：Origin/Referer 双缺失不得静默放行变更接口（旧浏览器/
        # 隐私扩展剥离 Referer 后 CSRF 防护失效）——裸 client 不带默认头
        with TestClient(srv.app, base_url="http://localhost") as bare:
            resp = bare.post("/api/sync")
        self.assertEqual(resp.status_code, 403)

    def test_referer_only_same_origin_post_passes_guard(self):
        # 仅 Referer 同源也放行（Origin 缺失场景，如部分重定向链）
        with TestClient(srv.app, base_url="http://localhost") as bare:
            resp = bare.post(
                "/api/sync", headers={"Referer": "http://localhost/settings"}
            )
        # 未注册同步回调时该路由返回 409；只要未被中间件 403/400 即通过
        self.assertEqual(resp.status_code, 409)

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
        self.client = _client()

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
        self.client = _client()

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
        # is_verified 补查（路由层合并，核心 get_due_reminders 契约不变）
        fake_cursor = AsyncMock()
        fake_cursor.fetchall.return_value = [{"id": "i1", "is_verified": 1}]
        fake_db = AsyncMock()
        fake_db.execute.return_value = fake_cursor
        with (
            patch("briefdesk.plugins.reminders.router.get_due_reminders", new=fake),
            patch("briefdesk.plugins.reminders.router.get_db", new=AsyncMock(return_value=fake_db)),
        ):
            resp = self.client.get("/api/reminders/due")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(len(data["items"]), 1)
        self.assertEqual(data["items"][0]["id"], "i1")
        self.assertEqual(data["items"][0]["is_verified"], 1)


class VerifyApiTest(unittest.TestCase):
    """F3：/api/items/:id/verify 对不存在的卡片返回 404（不再静默成功）。"""

    def setUp(self):
        self.client = _client()

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
        self.client = _client()

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


class MediaContentTypeTest(unittest.TestCase):
    """审查修复 #2：媒体代理 Content-Type 白名单，掐断同源文档渲染 XSS 面。

    最终类型必须是安全位图（png/jpeg/gif/webp/bmp）；SVG/HTML 等可承载
    脚本的类型一律降级 application/octet-stream + attachment 强制下载。
    """

    _PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"fake-png-body"

    def setUp(self):
        self.client = _client()
        self.fake = _FakeMediaClient()
        self.patcher = patch(
            "briefdesk.server.media.get_source_client", return_value=self.fake
        )
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.client.close()

    def _get(self, path):
        return self.client.get("/api/media/weflow/" + path)

    def test_extensionless_media_sniffed_as_png(self):
        # qqflow mediaId 无扩展名：魔数嗅探兜底仍应放行位图类型
        self.fake.payload = self._PNG_BYTES
        resp = self._get("media/3f2a9c8b7d6e5f4a1b2c3d4e5f6a7b8c")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers["content-type"], "image/png")
        self.assertNotIn("content-disposition", resp.headers)

    def test_svg_served_as_octet_stream_with_attachment(self):
        self.fake.payload = b"<svg xmlns='http://www.w3.org/2000/svg'/>"
        resp = self._get("chat@room/images/evil.svg")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers["content-type"], "application/octet-stream")
        self.assertTrue(
            resp.headers.get("content-disposition", "").startswith("attachment;")
        )

    def test_nonascii_basename_attachment_uses_rfc5987(self):
        # 中文基名：Content-Disposition 头值仅限 latin-1，裸非 ASCII 会令
        # 响应阶段直接 UnicodeEncodeError——必须走 RFC 5987 filename*=UTF-8''
        # 扩展并配 ASCII 回退（审查必修项回归）
        self.fake.payload = b"<svg xmlns='http://www.w3.org/2000/svg'/>"
        resp = self._get("chat@room/报告/会议纪要.html")
        self.assertEqual(resp.status_code, 200)
        cd = resp.headers["content-disposition"]
        self.assertTrue(cd.startswith("attachment;"))
        self.assertIn("filename*=UTF-8''", cd)
        self.assertNotIn("会", cd)  # 裸非 ASCII 不得进入头值

    def test_html_served_as_octet_stream_with_attachment(self):
        self.fake.payload = b"<html><body>phishing</body></html>"
        resp = self._get("chat@room/files/page.html")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers["content-type"], "application/octet-stream")
        self.assertTrue(
            resp.headers.get("content-disposition", "").startswith("attachment;")
        )

    def test_png_keeps_image_type_without_attachment(self):
        self.fake.payload = self._PNG_BYTES
        resp = self._get("chat@room/images/ok.png")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers["content-type"], "image/png")
        self.assertNotIn("content-disposition", resp.headers)

    def test_svg_payload_disguised_as_png_extension_downgrades(self):
        # 扩展名说 png、内容是 SVG 文本：嗅探不出位图 → 必须降级下载
        self.fake.payload = b"<svg xmlns='http://www.w3.org/2000/svg'/>"
        resp = self._get("chat@room/images/tricky.png")
        self.assertEqual(resp.headers["content-type"], "application/octet-stream")
        self.assertTrue(
            resp.headers.get("content-disposition", "").startswith("attachment;")
        )


class SpaFallbackExtensionTest(unittest.TestCase):
    """审查修复 #8：带扩展名的未知静态资源 404，不回退 SPA 首页。

    回退 index.html 会以 200 + text/html 应答 .js/.css 等资源请求，
    触发浏览器严格 MIME 检查告警；SPA 路由（无扩展名）仍回退首页。
    """

    def test_unknown_js_asset_returns_404(self):
        client = _client()
        try:
            resp = client.get("/nonexistent.js")
            self.assertEqual(resp.status_code, 404)
        finally:
            client.close()

    def test_unknown_nested_css_asset_returns_404(self):
        client = _client()
        try:
            resp = client.get("/nope/dir/style.css")
            self.assertEqual(resp.status_code, 404)
        finally:
            client.close()

    def test_extensionless_route_still_falls_back_to_index(self):
        client = _client()
        try:
            resp = client.get("/some-spa-route")
            self.assertEqual(resp.status_code, 200)
            self.assertIn("DOCTYPE", resp.text[:400].upper())
        finally:
            client.close()


class CsvExportFormulaInjectionTest(unittest.TestCase):
    """审查修复 #4：CSV 导出对公式前缀（=/+/-/@/Tab）单元格前置单引号转义。"""

    def test_items_export_escapes_formula_prefixes(self):
        client = _client()
        row = {
            "id": "=cmd|calc!A0",
            "category": "@风险",
            "title": "+汇总",
            "key_info": "-负号开头",
            "sender_name": "\t制表符开头",
            "source_group": "正常群",
            "subject": "主体",
            "source": "weflow",
            "source_msg_id": "m1",
            "session_id": "s1",
            "msg_time": 100,
            "start": None,
            "end": None,
            "extra_times": "",
            "article_url": "",
            "source_quote": "=hyperlink(evil)",
            "is_verified": 0,
        }

        async def fake_page(**kwargs):
            return {
                "items": [row],
                "total_count": 1,
                "group_count": 1,
                "source_groups": [],
                "has_more": False,
                "next_offset": 0,
                "filter_now": None,
            }

        with patch.multiple(
            "briefdesk.server.routes_items",
            get_items_page=fake_page,
            get_category_counts=AsyncMock(return_value=[]),
            get_all_category_count=AsyncMock(return_value=0),
            get_ignored_count=AsyncMock(return_value=0),
            get_memo_count=AsyncMock(return_value=0),
        ):
            resp = client.get("/api/export/items")
        client.close()
        self.assertEqual(resp.status_code, 200)
        text = resp.text
        # 公式前缀单元格必须被 ' 前缀转义（防 Excel/LibreOffice 当公式执行）
        escaped_cells = [
            "'=cmd|calc!A0",
            "'@风险",
            "'+汇总",
            "'-负号开头",
            "'=hyperlink(evil)",
        ]
        for escaped in escaped_cells:
            self.assertIn(escaped, text, "缺少转义形式: " + escaped)
        # 未转义形式不得出现在任何单元格起始（行首或逗号后）
        norm = text.replace("\r\n", "\n")
        for raw in ("=cmd|calc!A0", "@风险", "+汇总", "-负号开头", "=hyperlink(evil)"):
            self.assertNotIn("\n" + raw, norm, "存在未转义的公式单元格: " + raw)
            self.assertNotIn("," + raw, norm, "存在未转义的公式单元格: " + raw)
        self.assertNotIn(",\t制表符", norm)
        self.assertIn("'\t制表符开头", text)
        # 普通文本不转义
        self.assertIn("正常群", text)
        self.assertNotIn("'正常群", text)

    def test_recat_csv_export_escapes_formula_content(self):
        client = _client()
        sample = {
            "item_id": "i1",
            "source": "weflow",
            "source_msg_id": "m1",
            "category_before": "学术",
            "category_after": "活动通知",
            "content": "=SUM(A1:A2)",
            "created_at": "2026-01-01T00:00:00+00:00",
        }
        with patch(
            "briefdesk.server.routes_items.get_recat_samples",
            new=AsyncMock(return_value=[sample]),
        ):
            resp = client.get("/api/export/recat-samples?format=csv")
        client.close()
        self.assertEqual(resp.status_code, 200)
        self.assertIn("'=SUM(A1:A2)", resp.text)
        self.assertNotIn(",=SUM", resp.text.replace("\r\n", "\n"))


class IgnoreClearsDedupCacheEventTest(unittest.TestCase):
    """审查修复 #5：忽略卡片后发布 items_deleted，使去重缓存与预热口径一致。

    去重缓存预热只载入 is_verified >= 0 的条目；忽略后若不清内存缓存，
    被忽略卡仍参与判重，相似新消息被永久跳过不显示（直到重启）。
    """

    def test_batch_ignore_publishes_items_deleted(self):
        client = _client()
        publish = AsyncMock()
        bus = SimpleNamespace(publish=publish)
        with patch.multiple(
            "briefdesk.server.routes_items",
            event_bus=bus,
            update_items_verify=AsyncMock(return_value=2),
            storage_lock=asyncio.Lock(),
        ):
            resp = client.post(
                "/api/items/batch", json={"ids": ["i1", "i2"], "action": "ignore"}
            )
        client.close()
        self.assertEqual(resp.status_code, 200)
        publish.assert_awaited_once_with(EVENT_ITEMS_DELETED, ["i1", "i2"])

    def test_batch_memo_does_not_publish(self):
        client = _client()
        publish = AsyncMock()
        bus = SimpleNamespace(publish=publish)
        with patch.multiple(
            "briefdesk.server.routes_items",
            event_bus=bus,
            update_items_verify=AsyncMock(return_value=1),
            storage_lock=asyncio.Lock(),
        ):
            resp = client.post(
                "/api/items/batch", json={"ids": ["i1"], "action": "memo"}
            )
        client.close()
        self.assertEqual(resp.status_code, 200)
        publish.assert_not_awaited()

    def test_single_verify_ignore_publishes_event(self):
        client = _client()
        publish = AsyncMock()
        bus = SimpleNamespace(publish=publish)
        with patch.multiple(
            "briefdesk.server.routes_items",
            event_bus=bus,
            update_item_verify=AsyncMock(return_value=True),
            get_category_counts=AsyncMock(return_value=[]),
            get_all_category_count=AsyncMock(return_value=0),
            get_ignored_count=AsyncMock(return_value=0),
            get_memo_count=AsyncMock(return_value=0),
        ):
            resp = client.post("/api/items/i1/verify", json={"verified": -1})
        client.close()
        self.assertEqual(resp.status_code, 200)
        publish.assert_awaited_once_with(EVENT_ITEMS_DELETED, ["i1"])

    def test_single_verify_memo_does_not_publish(self):
        client = _client()
        publish = AsyncMock()
        bus = SimpleNamespace(publish=publish)
        with patch.multiple(
            "briefdesk.server.routes_items",
            event_bus=bus,
            update_item_verify=AsyncMock(return_value=True),
            get_category_counts=AsyncMock(return_value=[]),
            get_all_category_count=AsyncMock(return_value=0),
            get_ignored_count=AsyncMock(return_value=0),
            get_memo_count=AsyncMock(return_value=0),
        ):
            resp = client.post("/api/items/i1/verify", json={"verified": 1})
        client.close()
        self.assertEqual(resp.status_code, 200)
        publish.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
