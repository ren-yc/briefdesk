"""基准运行期间列表区照常可用（通过 Node vm 执行 ui_benchmark_paused_test.mjs）。

运行只发 benchmark_paused 公告（消息处理暂停）；列表区不因该公告改变，必须继续渲染
真实卡片，否则运行期间界面等于瘫痪。
"""

import shutil
import subprocess
import unittest
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent / "ui_benchmark_paused_test.mjs"
_ROOT = Path(__file__).resolve().parents[1]
_NODE = shutil.which("node")


@unittest.skipUnless(_NODE, "Node.js not available; skipping frontend vm regression test")
class UiBenchmarkPausedTest(unittest.TestCase):
    def test_list_stays_usable_under_new_announcement_code(self):
        result = subprocess.run(
            [_NODE, str(_SCRIPT)],
            check=False,
            cwd=_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(
            result.returncode,
            0,
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )


if __name__ == "__main__":
    unittest.main()
