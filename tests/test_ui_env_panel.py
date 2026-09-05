"""启动配置面板前端逻辑回归测试（通过 Node vm 执行 ui_env_panel_test.mjs）。

守卫「设置 → 启动配置」面板的三类回归：

1. ``_collectEnvChanges`` 布尔分支的相等性检查——此前缺失该检查，每次
   「暂存更改」都会把所有布尔项重写进暂存文件，「没有需要暂存的更改」
   永不触发；差异计数（脏高亮）上线后表现为按钮常驻虚高；
2. 分组渲染：未启用插件组默认折叠 + 组头徽章、行内徽章去重、布尔开关、
   已配置（钥匙串）密钥的「替换/取消」入口（「取消」仅随钥匙串托管行渲染，
   非托管行的输入框是常驻配置入口）；
3. 「暂存更改」按钮的脏计数联动与搜索过滤的组级显隐。

数据一律虚构（见 AGENTS.md）。
"""

import shutil
import subprocess
import unittest
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent / "ui_env_panel_test.mjs"
_ROOT = Path(__file__).resolve().parents[1]
_NODE = shutil.which("node")


@unittest.skipUnless(_NODE, "Node.js not available; skipping frontend vm regression test")
class UiEnvPanelTest(unittest.TestCase):
    # 签名级检查约定（见 pyproject [tool.mypy] 注释）：测试方法不加返回注解，
    # 函数体不做深检——_NODE 的 Optional 收窄交给 skipUnless 装饰器
    def test_env_panel_render_collect_and_filter(self):
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
