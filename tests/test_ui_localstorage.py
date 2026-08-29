"""前端 localStorage 单源助手回归测试（通过 Node vm 执行 ui_localstorage_test.mjs）。

修复背景：24 个 localStorage 调用点里只有一半包了 try。隐私模式/配额耗尽/站点
存储被策略禁用时 getItem/setItem 都会抛异常，未包的调用点一旦抛出会中断整个事件
处理函数，把“持久化失败”升级成“功能不响应”——主题芯片不再调 applyTheme()、折叠
开关不重渲染、隐藏已截止开关连按钮态都不更新。现读写统一经 lsGet/lsSet/lsGetJson/
lsSetJson，异常在助手内吞掉并退回 fallback；本测试用 vm 加载真实 ui/app.js，注入
“必抛的 localStorage”守卫该行为。
"""

import shutil
import subprocess
import unittest
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent / "ui_localstorage_test.mjs"
_ROOT = Path(__file__).resolve().parents[1]
_NODE = shutil.which("node")


@unittest.skipUnless(_NODE, "Node.js not available; skipping frontend vm regression test")
class UiLocalStorageHelperTest(unittest.TestCase):
    def test_storage_failures_never_propagate(self):
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
