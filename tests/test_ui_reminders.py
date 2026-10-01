"""reminders 插件前端回归测试（通过 Node vm 执行 ui_reminders_test.mjs）。

守两处曾漏掉的投递语义：页面隐藏且无桌面通知权限时不得清除服务端提醒
（清除会把提醒静默吞掉，回到前台经 visibilitychange 补查）；重设提醒会清掉
本地「已通知」标记，同卡可再次触发。
"""

import shutil
import subprocess
import unittest
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent / "ui_reminders_test.mjs"
_ROOT = Path(__file__).resolve().parents[1]
_NODE = shutil.which("node")


@unittest.skipUnless(_NODE, "Node.js not available; skipping frontend vm regression test")
class UiRemindersTest(unittest.TestCase):
    def test_delivery_gate_and_reminder_reset(self):
        result = subprocess.run(
            [_NODE, str(_SCRIPT)],
            cwd=_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
        self.assertEqual(
            result.returncode,
            0,
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )
