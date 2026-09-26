"""前端备份 / 导出的基准窗口 409 提示回归（通过 Node vm 执行
ui_export_window_test.mjs）。

窗口内后端对 /api/backup、/api/export/items、/api/export/recat-samples 返回
409 + {"detail":{"code":"benchmark_running"}}，downloadExport 此前一律只提示
「导出失败，请重试」，用户会反复重试到窗口结束之后。
"""

import shutil
import subprocess
import unittest
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent / "ui_export_window_test.mjs"
_ROOT = Path(__file__).resolve().parents[1]
_NODE = shutil.which("node")


@unittest.skipUnless(_NODE, "Node.js not available; skipping frontend vm regression test")
class UiExportWindowTest(unittest.TestCase):
    def test_benchmark_busy_409_gets_dedicated_hint(self):
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


if __name__ == "__main__":
    unittest.main()
