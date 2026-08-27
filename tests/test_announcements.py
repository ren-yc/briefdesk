"""公告（announcements）功能测试：注册表语义、SSE 发布、/api/status 携带与前端接线。

公告表达"当前持续存在的条件"（如嵌入服务未启用/不可用），与
status.lastWarning（管道成功产出即清空的瞬态提示）互补：由发现方置位、
条件解除方撤销，撤销前常驻。前端经 /api/status 的 announcements 字段
轮询兜底 + announcements_updated SSE 事件即时刷新。
"""

import unittest
from pathlib import Path
from unittest.mock import patch

from briefdesk.realtime import publish_announcements_updated, subscribe, unsubscribe
from briefdesk.status import get_status_info

_ROOT = Path(__file__).resolve().parents[1]


class AnnounceRegistryTest(unittest.IsolatedAsyncioTestCase):
    """announce/revoke 注册表语义。"""

    def setUp(self) -> None:
        from briefdesk import announcements

        self.announcements = announcements
        announcements.reset_announcements()
        self.addCleanup(announcements.reset_announcements)

    async def test_announce_sets_entry(self) -> None:
        changed = await self.announcements.announce(
            "embedding_unreachable", "warning", "嵌入服务不可用"
        )
        self.assertIs(changed, True)
        items = self.announcements.get_announcements()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["code"], "embedding_unreachable")
        self.assertEqual(items[0]["level"], "warning")
        self.assertEqual(items[0]["message"], "嵌入服务不可用")
        self.assertTrue(items[0]["since"])

    async def test_announce_same_content_is_noop(self) -> None:
        await self.announcements.announce("c", "warning", "m")
        self.assertIs(await self.announcements.announce("c", "warning", "m"), False)
        self.assertEqual(len(self.announcements.get_announcements()), 1)

    async def test_announce_updated_message_changes(self) -> None:
        await self.announcements.announce("c", "warning", "old")
        self.assertIs(await self.announcements.announce("c", "warning", "new"), True)
        self.assertEqual(self.announcements.get_announcements()[0]["message"], "new")

    async def test_revoke_missing_returns_false(self) -> None:
        self.assertIs(await self.announcements.revoke("absent"), False)

    async def test_revoke_removes_existing(self) -> None:
        await self.announcements.announce("c", "warning", "m")
        self.assertIs(await self.announcements.revoke("c"), True)
        self.assertEqual(self.announcements.get_announcements(), [])

    async def test_list_stable_order(self) -> None:
        """since 升序；同秒并列时按 code 兜底，保证 /api/status 输出稳定。"""
        await self.announcements.announce("b", "warning", "m2")
        await self.announcements.announce("a", "warning", "m1")
        self.assertEqual(
            [x["code"] for x in self.announcements.get_announcements()],
            ["a", "b"],
        )


class AnnouncePublishTest(unittest.IsolatedAsyncioTestCase):
    """仅状态变化时发布；快照随事件下发。"""

    def setUp(self) -> None:
        from briefdesk import announcements

        self.announcements = announcements
        announcements.reset_announcements()
        self.addCleanup(announcements.reset_announcements)
        self.published: list[dict] = []

        async def fake_publish(payload=None):
            self.published.append(payload or {})

        self._patcher = patch.object(
            announcements, "publish_announcements_updated", fake_publish
        )
        self._patcher.start()
        self.addCleanup(self._patcher.stop)

    async def test_publishes_only_on_change(self) -> None:
        await self.announcements.announce("c", "warning", "m")
        await self.announcements.announce("c", "warning", "m")
        self.assertEqual(len(self.published), 1)
        self.assertEqual(
            self.published[0]["announcements"],
            self.announcements.get_announcements(),
        )
        await self.announcements.revoke("c")
        self.assertEqual(len(self.published), 2)
        self.assertEqual(self.published[1]["announcements"], [])

    async def test_revoke_absent_does_not_publish(self) -> None:
        await self.announcements.revoke("absent")
        self.assertEqual(self.published, [])


class AnnounceRealtimeEventTest(unittest.IsolatedAsyncioTestCase):
    """realtime 侧具名事件：announcements_updated 可经 /api/stream 派发。"""

    async def test_publish_announcements_updated_event_name(self) -> None:
        q = await subscribe()
        try:
            await publish_announcements_updated({"announcements": []})
            name, _data = q.get_nowait()
            self.assertEqual(name, "announcements_updated")
        finally:
            await unsubscribe(q)


class StatusCarriesAnnouncementsTest(unittest.IsolatedAsyncioTestCase):
    """/api/status 聚合携带公告快照（前端轮询兜底数据源）。"""

    def setUp(self) -> None:
        from briefdesk import announcements

        announcements.reset_announcements()
        self.addCleanup(announcements.reset_announcements)

    async def test_get_status_info_includes_announcements(self) -> None:
        from briefdesk import announcements

        await announcements.announce("embedding_unreachable", "warning", "x")
        self.assertEqual(
            get_status_info()["announcements"],
            announcements.get_announcements(),
        )


class UiWiringTest(unittest.TestCase):
    """前端接线静态守卫：容器、渲染函数、SSE 事件与状态轮询四个接点。"""

    def test_ui_wires_announcements(self) -> None:
        html = (_ROOT / "ui" / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="announcements"', html)
        app = (_ROOT / "ui" / "app.js").read_text(encoding="utf-8")
        self.assertIn("function renderAnnouncements", app)
        self.assertIn('stream.addEventListener("announcements_updated"', app)
        self.assertIn(
            "renderAnnouncements(data.status && data.status.announcements)", app
        )


if __name__ == "__main__":
    unittest.main()
