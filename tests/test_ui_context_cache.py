"""前端上下文缓存串高亮回归测试（通过 Node vm 执行 ui_context_cache_test.mjs）。

修复背景：fetchContext 曾把“已带 ctx-target 高亮的 HTML”按
source|session|msg_time 缓存，同会话同秒的多张卡会命中同一份 HTML，
导致 A 卡的原文在 B 卡的上下文中被标绿。现在缓存原始消息列表、按卡片
source_msg_id 现渲染；本测试用 vm 加载真实 ui/app.js 守卫该行为。
"""

import shutil
import subprocess
import unittest
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent / "ui_context_cache_test.mjs"
_ROOT = Path(__file__).resolve().parents[1]
_NODE = shutil.which("node")


@unittest.skipUnless(_NODE, "Node.js not available; skipping frontend vm regression test")
class UiContextCacheRegressionTest(unittest.TestCase):
    def test_same_timestamp_cards_highlight_their_own_message(self):
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
