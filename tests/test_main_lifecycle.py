"""main 启动/关停生命周期回归测试。

- _run 启动段清理保护（P1）：PLUGINS_REQUIRED 插件 setup 失败抛 PluginError
  中止启动时，teardown_all / close_db 仍必须执行——否则 aiosqlite 非 daemon
  worker 线程不关闭，解释器退出时 join 挂死，只能强杀进程。
- _reap_task 关停收尾四态：done 直返 / pending 取消等待 / 超时留 pending
  交兜底清理 / 任务自身异常吞并记日志。
"""

import asyncio
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from briefdesk.plugin.base import PluginError


class PeriodicSyncLoopTest(unittest.IsolatedAsyncioTestCase):
    """【复核 P1-4】POLL_INTERVAL_SECONDS > 0 时周期触发 trigger_sync（与
    /api/sync 同路径；互斥由其返回 None 保证，不叠加触发）。"""

    async def test_loop_triggers_sync_periodically_and_exits_on_cancel(self):
        from briefdesk import main as main_mod

        reasons: list[str] = []

        def fake_sync(reason: str = "manual"):
            reasons.append(reason)  # 返回 None = 同步已在进行（互斥），循环不 await

        with (
            patch.object(main_mod.config, "poll_interval_seconds", 0.01),
            patch.object(main_mod, "trigger_sync", side_effect=fake_sync),
        ):
            task = asyncio.create_task(main_mod._periodic_sync_loop())
            await asyncio.sleep(0.05)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertGreaterEqual(len(reasons), 1)
        self.assertEqual(reasons[0], "periodic")


class ZeroSourceDegradedStartupTest(unittest.IsolatedAsyncioTestCase):
    """【复核 4a】零源不再中止启动：降级运行（UI 可用、采集不可用），
    清理路径照常执行——三源统一「缺配置自禁用」语义的前提（决策 ①=1B）。"""

    async def test_zero_sources_starts_degraded_and_cleans_up(self):
        from briefdesk import main as main_mod
        from briefdesk import stages
        from briefdesk.server.callbacks import set_refresh_sessions_callback
        from briefdesk.server.web_plugins import (
            set_plugins_info_callback,
            set_settings_schema_callback,
        )
        from briefdesk.sync import set_sync_callback

        # 全局回调/stages 上下文会被 _run 覆盖，测试后复位为「未注册」态
        # （None）防污染其它用例——test_server 依赖 409/503 的未注册语义
        self.addCleanup(stages.reset)
        self.addCleanup(set_plugins_info_callback, None)
        self.addCleanup(set_settings_schema_callback, None)
        self.addCleanup(set_sync_callback, None)
        self.addCleanup(set_refresh_sessions_callback, None)

        manager = MagicMock()
        manager.setup_all = AsyncMock()
        manager.activate_all = AsyncMock()
        manager.teardown_all = AsyncMock()
        close_db = AsyncMock()
        server = MagicMock()
        server.started = True
        server.serve = AsyncMock()

        with (
            patch.object(main_mod, "PluginManager", return_value=manager),
            patch.object(
                main_mod, "apply_pending_restore", new=AsyncMock(return_value=False)
            ),
            patch.object(main_mod, "get_db", new=AsyncMock()),
            patch.object(main_mod.config, "ignored_expiry_hours", 0),
            patch.object(main_mod, "close_db", close_db),
            patch.object(main_mod.uvicorn, "Config", MagicMock()),
            patch.object(main_mod.uvicorn, "Server", MagicMock(return_value=server)),
            patch.object(main_mod, "_install_signal_handlers"),
            patch.object(main_mod, "trigger_sync", return_value=None),
        ):
            await main_mod._run()  # 零源不抛：降级启动走完全生命周期

        manager.teardown_all.assert_awaited_once()
        close_db.assert_awaited_once()


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


class StartupInterruptCleanupTest(unittest.TestCase):
    """启动窗口期 Ctrl+C（审查回归）：信号 handler 在 server.started 之后才
    安装，此前的 Ctrl+C 以 KeyboardInterrupt 从 run_until_complete 逃逸——
    旧实现直接退出，_run 的 finally（teardown/close_db 唯一清理点）不执行，
    aiosqlite 非 daemon worker 线程令解释器退出挂死。现由 main() 取消
    main_task 并驱动事件循环把清理跑完。"""

    def test_startup_interrupt_runs_finally_cleanup(self):
        from briefdesk import main as main_mod

        cleaned: list[bool] = []

        async def fake_run() -> None:
            try:
                loop = asyncio.get_running_loop()

                def inject_interrupt() -> None:
                    # 模拟启动窗口期 Ctrl+C：异常从回调逃逸出 run_forever，
                    # 等价于主线程收到 SIGINT（真线程 interrupt_main 在
                    # Windows proactor 上无法唤醒阻塞中的 selector，不可用）
                    raise KeyboardInterrupt

                loop.call_later(0.05, inject_interrupt)
                await asyncio.sleep(3600)
            finally:
                cleaned.append(True)

        with (
            patch.object(main_mod, "_run", fake_run),
            patch.object(sys, "argv", ["briefdesk"]),
        ):
            main_mod.main()  # 必须返回且不向上抛 KeyboardInterrupt

        self.assertEqual(cleaned, [True], "清理 finally 必须已执行")


if __name__ == "__main__":
    unittest.main()
