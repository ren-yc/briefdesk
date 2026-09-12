"""sync.trigger_sync 直接测试。

覆盖：未注册回调拒绝、同步中互斥、回调异常隔离与状态复位、正常路径
finally 清理与完成事件。此前仅经 main patch / HTTP 403 间接覆盖。
"""

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from briefdesk import sync as sync_module
from briefdesk.status import is_syncing, set_status


class TriggerSyncTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # 状态模块级单例：每个用例前置复位，避免相互污染
        set_status({"syncing": False})
        self.addCleanup(set_status, {"syncing": False})
        sync_module.set_sync_callback(None)
        self.addCleanup(sync_module.set_sync_callback, None)

    async def test_no_callback_rejected_and_not_syncing(self):
        """未注册回调 → 返回 None、不置 syncing。"""
        self.assertIsNone(sync_module.trigger_sync(reason="test"))
        self.assertFalse(is_syncing())

    async def test_concurrent_trigger_rejected_by_mutex(self):
        """同步进行中再次触发 → 互斥拒绝（返回 None），首粒任务不受影响。"""
        release = asyncio.Event()
        started = asyncio.Event()

        async def slow_cb():
            started.set()
            await release.wait()

        sync_module.set_sync_callback(slow_cb)
        first = sync_module.trigger_sync(reason="test")
        self.assertIsNotNone(first)
        await started.wait()
        self.assertTrue(is_syncing())
        # 第一轮仍在跑 → 第二次触发被拒
        self.assertIsNone(sync_module.trigger_sync(reason="test"))
        release.set()
        await first
        self.assertFalse(is_syncing(), "首粒任务结束后 syncing 复位")

    async def test_callback_exception_isolated_and_status_reset(self):
        """回调抛异常 → 不外泄（日志兜底）、syncing 复位、完成事件照发。"""
        sync_module.set_sync_callback(AsyncMock(side_effect=RuntimeError("boom")))
        calls: list[dict] = []

        def recording_set_status(patch_dict):
            calls.append(patch_dict)

        with (
            patch.object(sync_module, "set_status", recording_set_status),
            patch.object(
                sync_module, "publish_items_updated", new=AsyncMock()
            ) as publish,
            self.assertLogs("briefdesk.sync", level="ERROR") as captured,
        ):
            task = sync_module.trigger_sync(reason="test")
            self.assertIsNotNone(task)
            await task  # 异常被 _run 吞掉，任务正常结束
        self.assertTrue(
            any("同步任务失败" in m for m in captured.output), captured.output
        )
        self.assertEqual(calls, [{"syncing": True}, {"syncing": False}])
        publish.assert_awaited_once_with({"synced": True})

    async def test_normal_path_finally_cleanup_and_publish(self):
        """正常路径：finally 复位 syncing 并推送完成事件。"""
        sync_module.set_sync_callback(AsyncMock())
        calls: list[dict] = []

        def recording_set_status(patch_dict):
            calls.append(patch_dict)

        with (
            patch.object(sync_module, "set_status", recording_set_status),
            patch.object(
                sync_module, "publish_items_updated", new=AsyncMock()
            ) as publish,
        ):
            task = sync_module.trigger_sync(reason="test")
            self.assertIsNotNone(task)
            await task
        self.assertEqual(calls, [{"syncing": True}, {"syncing": False}])
        publish.assert_awaited_once_with({"synced": True})


if __name__ == "__main__":
    unittest.main()
