"""前端过期请求失败回归测试（通过 Node vm 执行 ui_fetch_stale_error_test.mjs）。

fetchData 的 catch 此前没有 fetchSeq 守卫：快速切换查询时旧请求晚失败会把
状态胶囊改成「连接失败」并覆盖已渲染的新数据提示。
"""

import shutil
import subprocess
import unittest
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent / "ui_fetch_stale_error_test.mjs"
_ROOT = Path(__file__).resolve().parents[1]
_NODE = shutil.which("node")


@unittest.skipUnless(_NODE, "Node.js not available; skipping frontend vm regression test")
class UiFetchStaleErrorTest(unittest.TestCase):
    def test_stale_failure_does_not_override_latest_state(self):
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
