"""前端写操作提示分流回归（通过 Node vm 执行 ui_write_guard_test.mjs）。

基准窗口内写路由一律 409；前端必须按 detail.code 分流成「基准运行中」的
info 提示，而不是普通失败或「同步已在后台进行中」。
"""

import shutil
import subprocess
import unittest
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent / "ui_write_guard_test.mjs"
_ROOT = Path(__file__).resolve().parents[1]
_NODE = shutil.which("node")


@unittest.skipUnless(_NODE, "Node.js not available; skipping frontend vm regression test")
class UiWriteGuardTest(unittest.TestCase):
    def test_benchmark_busy_write_hints(self):
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
