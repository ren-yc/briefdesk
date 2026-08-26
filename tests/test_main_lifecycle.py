"""main 启动/关停生命周期回归测试。

- _run 启动段清理保护（P1）：PLUGINS_REQUIRED 插件 setup 失败抛 PluginError
  中止启动时，teardown_all / close_db 仍必须执行——否则 aiosqlite 非 daemon
  worker 线程不关闭，解释器退出时 join 挂死，只能强杀进程。
- _reap_task 关停收尾四态：done 直返 / pending 取消等待 / 超时留 pending
  交兜底清理 / 任务自身异常吞并记日志。
"""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from briefdesk.plugin.base import PluginError


class MainRunCleanupOnSetupFailureTest(unittest.IsolatedAsyncioTestCase):
    async def test_required_plugin_failure_still_closes_db(self):
        from briefdesk import main as main_mod

        manager = MagicMock()
        manager.setup_all = AsyncMock(side_effect=PluginError("required boom"))
        manager.teardown_all = AsyncMock()
        close_db = AsyncMock()

        with (
            patch.object(main_mod, "PluginManager", return_value=manager),
            patch.object(
                main_mod, "apply_pending_restore", new=AsyncMock(return_value=False)
            ),
            patch.object(main_mod, "get_db", new=AsyncMock()),
            patch.object(main_mod.config, "ignored_expiry_hours", 0),
            patch.object(main_mod, "close_db", close_db),
            self.assertRaises(PluginError),
        ):
            await main_mod._run()

        manager.teardown_all.assert_awaited_once()
        close_db.assert_awaited_once()


class ReapTaskTest(unittest.IsolatedAsyncioTestCase):
    """_reap_task 直接单测（关闭期收尾契约，幂等且不向调用方传播异常）。"""

    async def test_done_task_returns_immediately(self):
        """已完成任务传入 → 函数直返，任务状态不变。"""
        from briefdesk.main import _reap_task

        task = asyncio.create_task(asyncio.sleep(0), name="done-task")
        await task  # 已完成（含结果已取出）
        self.assertTrue(task.done())
        await _reap_task(task)  # 直返，不取消、不抛错
        self.assertTrue(task.done())
        self.assertFalse(task.cancelled())

    async def test_pending_task_is_cancelled_and_awaited(self):
        """未完成任务先 cancel 再限时等待 → 返回后任务已终结（cancelled）。"""
        from briefdesk.main import _reap_task

        task = asyncio.create_task(asyncio.sleep(3600), name="pending-task")
        await _reap_task(task)
        self.assertTrue(task.done())
        self.assertTrue(task.cancelled())

    async def test_timeout_leaves_task_pending_for_fallback(self):
        """忽略第一次取消的任务超时未终结 → WARNING 落日志、任务保持 pending，
        留给 _cancel_pending_tasks 兜底；测试 finally 里再 cancel 收尾清理。"""

        async def stubborn():
            # 忽略第一次取消，模拟卡死的清理流程
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await asyncio.sleep(3600)

        from briefdesk.main import _reap_task

        task = asyncio.create_task(stubborn(), name="stubborn-task")
        await asyncio.sleep(0)  # 让任务先跑到首个 await 点，再进入收尾
        try:
            with self.assertLogs("briefdesk.main", level="WARNING") as captured:
                await _reap_task(task, timeout=0.05)
            self.assertTrue(
                any("关闭等待超时" in line for line in captured.output),
                captured.output,
            )
            self.assertFalse(task.done(), "超时路径不得二次 cancel 打断内部清理")
        finally:
            task.cancel()  # 收尾清理，避免污染事件循环
            with self.assertRaises(asyncio.CancelledError):
                await task

    async def test_task_exception_swallowed_and_logged(self):
        """任务在取消后自身抛 RuntimeError → 不向上传播、ERROR 记日志。"""
        from briefdesk.main import _reap_task

        async def failing_on_reap():
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                raise RuntimeError("任务内部清理爆炸")

        task = asyncio.create_task(failing_on_reap(), name="boom-task")
        await asyncio.sleep(0)  # 让任务先跑到首个 await 点，cancel 才会命中其内部清理
        with self.assertLogs("briefdesk.main", level="ERROR") as captured:
            await _reap_task(task, timeout=1.0)  # 不抛出
        self.assertTrue(any("boom-task" in line for line in captured.output))
        self.assertTrue(task.done())
        self.assertFalse(task.cancelled())


if __name__ == "__main__":
    unittest.main()
