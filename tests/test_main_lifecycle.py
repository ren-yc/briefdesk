"""main._run 启动段清理保护回归（P1）。

PLUGINS_REQUIRED 插件 setup 失败抛 PluginError 中止启动时，
teardown_all / close_db 仍必须执行——否则 aiosqlite 非 daemon worker
线程不关闭，解释器退出时 join 挂死，只能强杀进程。
"""

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


if __name__ == "__main__":
    unittest.main()
