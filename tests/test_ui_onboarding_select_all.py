"""首次使用向导 step2 全选功能回归测试（通过 Node vm 执行 ui_onboarding_select_all_test.mjs）。

验证 renderOnboardSessions 会生成“全选”复选框，且会话行保留 data-session-id
供保存逻辑使用；避免以后把全选行误当作可保存的会话行。
"""

import shutil
import subprocess
import unittest
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent / "ui_onboarding_select_all_test.mjs"
_ROOT = Path(__file__).resolve().parents[1]
_NODE = shutil.which("node")


@unittest.skipUnless(_NODE, "Node.js not available; skipping frontend vm regression test")
class UiOnboardingSelectAllRegressionTest(unittest.TestCase):
    def test_onboarding_renders_select_all_row(self):
        result = subprocess.run(
            [_NODE, str(_SCRIPT)],
            cwd=_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(
            result.returncode,
            0,
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )
