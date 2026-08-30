"""CSV 导出公式注入防护回归（安全审计 #9）。

导出单元格源自群聊消息（攻击者可控），以 = + - @ \t \r 开头的文本被
Excel/WPS 打开时会当作公式执行（命令执行/外联/数据外带）。导出层必须
为其加 "'" 前缀降级为纯文本；数字列不受影响。
"""

import csv
import io
import unittest
from unittest.mock import AsyncMock, patch

from starlette.testclient import TestClient

import briefdesk.server as srv
from briefdesk.server import routes_items


def _page(items: list[dict]) -> dict:
    """get_items_page 契约形状（对齐 tests/test_server.py 同类 fake）。"""
    return {
        "items": items,
        "total_count": len(items),
        "group_count": len(items),
        "source_groups": [],
        "next_offset": 0,
        "has_more": False,
        "filter_now": None,
    }


class ExportItemsCsvInjectionTest(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(srv.app, base_url="http://localhost")

    def _rows(self, text: str) -> list[dict]:
        reader = csv.reader(io.StringIO(text))
        rows = list(reader)
        return [dict(zip(rows[0], r)) for r in rows[1:]]

    def test_formula_prefix_cells_are_neutralized(self):
        raw = {
            "title": "=cmd|'/C calc'!A0",
            "key_info": "+SUM(A1)",
            "sender_name": "-张三",
            "source_group": "@HYPERLINK(\"http://evil\",\"x\")",
            "source_quote": "=1+1",
        }
        item = {
            "id": "i1",
            "category": "活动通知",
            **raw,
            "subject": "subj",
            "source": "weflow-legacy",
            "source_msg_id": "m1",
            "session_id": "s1",
            "msg_time": "2026-01-01 10:00:00",
            "start": "",
            "end": "",
            "extra_times": "",
            "article_url": "",
            "is_verified": 0,
        }
        with patch.object(
            routes_items, "get_items_page", new=AsyncMock(return_value=_page([item]))
        ):
            resp = self.client.get("/api/export/items")
        self.assertEqual(resp.status_code, 200)
        data = self._rows(resp.text)[0]
        for col in ("title", "key_info", "sender_name", "source_group", "source_quote"):
            self.assertEqual(data[col], "'" + raw[col], col)
        # 数字列不受前缀影响
        self.assertEqual(data["is_verified"], "0")

    def test_plain_text_cells_untouched(self):
        item = {
            "id": "i1",
            "category": "活动通知",
            "title": "摄影社招新",
            "key_info": "周四 18:00",
            "sender_name": "李四",
            "source_group": "社团群",
            "subject": "",
            "source": "weflow-legacy",
            "source_msg_id": "m1",
            "session_id": "s1",
            "msg_time": "2026-01-01 10:00:00",
            "start": "",
            "end": "",
            "extra_times": "",
            "article_url": "",
            "source_quote": "正常引用文本",
            "is_verified": 1,
        }
        with patch.object(
            routes_items, "get_items_page", new=AsyncMock(return_value=_page([item]))
        ):
            resp = self.client.get("/api/export/items")
        data = self._rows(resp.text)[0]
        self.assertEqual(data["title"], "摄影社招新")
        self.assertEqual(data["source_quote"], "正常引用文本")

    def test_newline_prefix_cell_neutralized(self):
        # \n 前缀与 \r 同层防御（OWASP CSV 注入清单）
        item = {
            "id": "i1",
            "category": "活动通知",
            "title": "\n=1+1",
            "key_info": "",
            "sender_name": "李四",
            "source_group": "社团群",
            "subject": "",
            "source": "weflow-legacy",
            "source_msg_id": "m1",
            "session_id": "s1",
            "msg_time": "2026-01-01 10:00:00",
            "start": "",
            "end": "",
            "extra_times": "",
            "article_url": "",
            "source_quote": "正常文本",
            "is_verified": 0,
        }
        with patch.object(
            routes_items, "get_items_page", new=AsyncMock(return_value=_page([item]))
        ):
            resp = self.client.get("/api/export/items")
        self.assertEqual(resp.status_code, 200)
        data = self._rows(resp.text)[0]
        self.assertEqual(data["title"], "'\n=1+1")


class ExportRecatSamplesCsvInjectionTest(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(srv.app, base_url="http://localhost")

    def test_content_cell_neutralized(self):
        sample = {
            "item_id": "i1",
            "source": "weflow-legacy",
            "source_msg_id": "m1",
            "category_before": "学术",
            "category_after": "活动通知",
            "content": "=cmd|'/C calc'!A0",
            "created_at": "2026-01-01T00:00:00",
        }
        with patch.object(
            routes_items,
            "get_recat_samples",
            new=AsyncMock(return_value=[sample]),
        ):
            resp = self.client.get("/api/export/recat-samples?format=csv")
        self.assertEqual(resp.status_code, 200)
        reader = csv.reader(io.StringIO(resp.text))
        rows = list(reader)
        data = dict(zip(rows[0], rows[1]))
        self.assertEqual(data["content"], "'=cmd|'/C calc'!A0")


if __name__ == "__main__":
    unittest.main()
