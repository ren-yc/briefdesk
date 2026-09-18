"""前端按群视图与 listMode 迁移回归测试（通过 Node vm 执行 ui_group_by_chat_test.mjs）。

docs/architecture.md「前端」节把它列为行为守卫（按群模式强制全量渲染、
listMode 迁移与三态计数文本分流），此前未接入 pytest，只能靠手动跑 node。
"""

import shutil
import subprocess
import unittest
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent / "ui_group_by_chat_test.mjs"
_ROOT = Path(__file__).resolve().parents[1]
_NODE = shutil.which("node")


@unittest.skipUnless(_NODE, "Node.js not available; skipping frontend vm regression test")
class UiGroupByChatTest(unittest.TestCase):
    def test_group_by_chat_and_list_mode_migration(self):
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
