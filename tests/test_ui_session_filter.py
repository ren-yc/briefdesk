"""会话筛选回归测试（通过 Node vm 执行 ui_session_filter_test.mjs）。

设置「群聊筛选」与首次使用向导 step2 是同一套筛选规则（类型多选 + 消息源多选 +
名称搜索 + 时间窗口，四者 AND 叠加），历史上两侧各抄了一份实现，改一处漏一处。
现两侧都由 createSessionFilter 产出实例；本测试用 vm 加载真实 ui/app.js，
守卫 sessionRowMatches 的四维语义与工厂实例的过滤/三态/档位规范化行为。
"""

import shutil
import subprocess
import unittest
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent / "ui_session_filter_test.mjs"
_ROOT = Path(__file__).resolve().parents[1]
_NODE = shutil.which("node")


@unittest.skipUnless(_NODE, "Node.js not available; skipping frontend vm regression test")
class UiSessionFilterTest(unittest.TestCase):
    def test_session_filter_factory_behavior(self):
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
