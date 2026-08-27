"""列表端点分页 helper（sources_base.fetch_all_pages）测试。

上游列表端点的 limit 默认值很小（100）且截断时**无任何错误提示**，
不翻页会静默丢数据（实测 weflow 真实账号 100/4533，截断外的发送者显示名
退化成 UID）。本文件锁住 helper 的终止条件与三道防御：

1. hasMore 为准（确定信号），缺失时回退「短页即末页」；
2. 跨页重复按 dedup_key 去重（翻页期间上游数据变动）；
3. 本页无新增 → 立即终止 + 告警（上游忽略 offset 时防死循环）。

全部使用虚构标识，不含真实凭据或聊天内容。
"""

import unittest

from briefdesk.sources_base import LIST_PAGE_SIZE, fetch_all_pages


def _rows(start: int, count: int) -> list[dict]:
    """构造 count 条虚构联系人（username 唯一）。"""
    return [
        {"username": f"u_{i:05d}", "displayName": f"联系人{i}"}
        for i in range(start, start + count)
    ]


class _Recorder:
    """记录每次请求参数的假 get。"""

    def __init__(self, pages: list[dict]):
        self._pages = pages
        self.calls: list[dict] = []

    async def __call__(self, path: str, params: dict) -> dict:
        self.calls.append(dict(params))
        idx = len(self.calls) - 1
        return self._pages[idx] if idx < len(self._pages) else {"contacts": []}

    @property
    def offsets(self) -> list[int]:
        return [c["offset"] for c in self.calls]


class TerminationTest(unittest.IsolatedAsyncioTestCase):
    """终止条件。"""

    async def test_single_short_page_stops_immediately(self):
        """首页不足 page_size：一次请求即结束。"""
        get = _Recorder([{"contacts": _rows(0, 5), "total": 5, "hasMore": False}])
        items = await fetch_all_pages(get, "/api/v1/contacts", key="contacts")
        self.assertEqual(len(items), 5)
        self.assertEqual(len(get.calls), 1)
        self.assertEqual(get.calls[0]["limit"], LIST_PAGE_SIZE)

    async def test_has_more_drives_pagination(self):
        """hasMore=True 继续翻页，False 停止。"""
        get = _Recorder(
            [
                {"contacts": _rows(0, 3), "hasMore": True},
                {"contacts": _rows(3, 3), "hasMore": True},
                {"contacts": _rows(6, 2), "hasMore": False},
            ]
        )
        items = await fetch_all_pages(
            get, "/api/v1/contacts", key="contacts", page_size=3
        )
        self.assertEqual(len(items), 8)
        self.assertEqual(get.offsets, [0, 3, 6])

    async def test_has_more_true_but_empty_page_stops(self):
        """上游 hasMore=True 却回空页（数据竞态）：空页即止，不无限循环。"""
        get = _Recorder(
            [
                {"contacts": _rows(0, 3), "hasMore": True},
                {"contacts": [], "hasMore": True},
            ]
        )
        items = await fetch_all_pages(
            get, "/api/v1/contacts", key="contacts", page_size=3
        )
        self.assertEqual(len(items), 3)
        self.assertEqual(len(get.calls), 2)

    async def test_missing_has_more_falls_back_to_short_page(self):
        """旧上游无 hasMore 字段：按「本页条数 < page_size」判末页。"""
        get = _Recorder(
            [
                {"contacts": _rows(0, 3)},  # 满页 → 继续
                {"contacts": _rows(3, 1)},  # 短页 → 末页
            ]
        )
        items = await fetch_all_pages(
            get, "/api/v1/contacts", key="contacts", page_size=3
        )
        self.assertEqual(len(items), 4)
        self.assertEqual(get.offsets, [0, 3])

    async def test_missing_key_stops(self):
        """响应缺列表键：视为无数据，不抛错。"""
        get = _Recorder([{"success": True}])
        items = await fetch_all_pages(get, "/api/v1/contacts", key="contacts")
        self.assertEqual(items, [])


class DefenseTest(unittest.IsolatedAsyncioTestCase):
    """三道防御。"""

    async def test_cross_page_duplicates_deduped(self):
        """翻页期间上游数据变动 → 跨页重复项只保留先到者。"""
        get = _Recorder(
            [
                {"contacts": _rows(0, 3), "hasMore": True},
                # 第 2 页与第 1 页有 2 条重叠（offset 漂移）
                {"contacts": _rows(1, 3), "hasMore": False},
            ]
        )
        items = await fetch_all_pages(
            get, "/api/v1/contacts", key="contacts", page_size=3
        )
        names = [i["username"] for i in items]
        self.assertEqual(len(names), len(set(names)), f"无重复: {names}")
        self.assertEqual(len(items), 4)

    async def test_upstream_ignoring_offset_terminates_with_warning(self):
        """上游忽略 offset（版本过旧）→ 整页重复，立即终止并告警。

        这是关键防御：此时 hasMore 恒 True、页也是满的，前两个终止条件
        都不成立，没有这道防御会永远重复取第一页。
        """
        same_page = {"contacts": _rows(0, 3), "hasMore": True}
        get = _Recorder([same_page] * 10)
        with self.assertLogs("briefdesk.sources_base", level="WARNING") as logs:
            items = await fetch_all_pages(
                get, "/api/v1/contacts", key="contacts", page_size=3
            )
        self.assertEqual(len(items), 3, "只保留第一页")
        self.assertEqual(len(get.calls), 2, "第二页发现全重复即止")
        self.assertTrue(
            any("可能不支持 offset" in m for m in logs.output),
            f"应告警上游不支持 offset: {logs.output}",
        )

    async def test_version_included_in_warning(self):
        """告警带上游版本号，便于定位「对端二进制过旧」。"""
        same_page = {"contacts": _rows(0, 2), "hasMore": True}
        get = _Recorder([same_page] * 5)
        with self.assertLogs("briefdesk.sources_base", level="WARNING") as logs:
            await fetch_all_pages(
                get,
                "/api/v1/contacts",
                key="contacts",
                page_size=2,
                upstream_version="0.2.0",
            )
        self.assertTrue(any("0.2.0" in m for m in logs.output), logs.output)

    async def test_items_without_dedup_key_all_kept(self):
        """列表项缺 dedup_key：不去重、全部保留（不静默丢数据）。"""
        get = _Recorder(
            [
                {"contacts": [{"displayName": "甲"}, {"displayName": "乙"}],
                 "hasMore": False},
            ]
        )
        items = await fetch_all_pages(
            get, "/api/v1/contacts", key="contacts", page_size=2
        )
        self.assertEqual(len(items), 2)


class ParamsTest(unittest.IsolatedAsyncioTestCase):
    """请求参数。"""

    async def test_extra_params_forwarded_on_every_page(self):
        """keyword 等附加参数每页都要带上。"""
        get = _Recorder(
            [
                {"contacts": _rows(0, 2), "hasMore": True},
                {"contacts": _rows(2, 1), "hasMore": False},
            ]
        )
        await fetch_all_pages(
            get,
            "/api/v1/contacts",
            key="contacts",
            page_size=2,
            extra_params={"keyword": "测试"},
        )
        self.assertEqual(len(get.calls), 2)
        for c in get.calls:
            self.assertEqual(c["keyword"], "测试")


class SourceClientWiringTest(unittest.IsolatedAsyncioTestCase):
    """三个源的 contacts 取法符合各自上游能力。"""

    def test_weflow_and_qqflow_use_the_shared_helper(self):
        """weflow / qqflow 的 fetch_contacts 必须走 helper，不得裸调 _get。

        守卫用意：三家 fetch_sessions 都传了大 limit，而 fetch_contacts 曾有
        两家漏传（weflow-legacy / qqflow），靠注释和人工记忆守不住。
        """
        import inspect

        from briefdesk.plugins.qqflow.client import QqFlowClient
        from briefdesk.plugins.weflow.client import WeFlowClient

        for cls in (WeFlowClient, QqFlowClient):
            src = inspect.getsource(cls.fetch_contacts)
            self.assertIn(
                "fetch_all_pages",
                src,
                f"{cls.__name__}.fetch_contacts 应走共享分页 helper",
            )

    def test_weflow_legacy_passes_max_limit(self):
        """weflow-legacy 上游（WeFlow 安装版）无 offset，只能传大 limit。"""
        import inspect

        from briefdesk.plugins.weflow_legacy.client import WeFlowLegacyClient

        src = inspect.getsource(WeFlowLegacyClient.fetch_contacts)
        self.assertIn("LIST_MAX_LIMIT", src)
        self.assertNotIn("fetch_all_pages", src, "该上游不支持 offset，不能翻页")


if __name__ == "__main__":
    unittest.main()
