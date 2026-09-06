"""状态横幅前端回归测试（通过 Node vm 执行 ui_status_banner_test.mjs）。

守卫 #error-banner 的三类状态提示与其动作按钮：

1. 零源降级（/api/status.sources 为空）→ 警示横幅 +「去启用」直达
   「设置 → 插件」面板——新装/升级用户默认 PLUGINS=[] 时的主入口引导；
2. 有消息源时不显示零源横幅；lastError/lastWarning 与零源并存时更具体
   的报错优先（零源信息已见于状态文字，避免横幅叠报错）；
3. 「去设置」（lastWarning）与「重试同步」（lastError）既有分支不回归。

数据一律虚构（见 AGENTS.md）。
"""

import shutil
import subprocess
import unittest
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent / "ui_status_banner_test.mjs"
_ROOT = Path(__file__).resolve().parents[1]
_NODE = shutil.which("node")


@unittest.skipUnless(_NODE, "Node.js not available; skipping frontend vm regression test")
class UiStatusBannerTest(unittest.TestCase):
    # 签名级检查约定（见 pyproject [tool.mypy] 注释）：测试方法不加返回注解，
    # 函数体不做深检——_NODE 的 Optional 收窄交给 skipUnless 装饰器
    def test_zero_source_banner_and_priority(self):
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
