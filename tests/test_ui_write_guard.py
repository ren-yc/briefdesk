"""写操作失败提示回归（通过 Node vm 执行 ui_write_guard_test.mjs）。

应用层不再有「基准运行中」的写闸门：写失败一律按调用点原文案与 error 级别提示。
"""

import shutil
import subprocess
import unittest
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent / "ui_write_guard_test.mjs"
_ROOT = Path(__file__).resolve().parents[1]
_NODE = shutil.which("node")


@unittest.skipUnless(_NODE, "Node.js not available; skipping frontend vm regression test")
class UiWriteErrorTest(unittest.TestCase):
    def test_write_error_keeps_call_site_message(self):
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


if __name__ == "__main__":
    unittest.main()
