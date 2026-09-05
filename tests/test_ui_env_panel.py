"""启动配置面板前端逻辑回归测试（通过 Node vm 执行 ui_env_panel_test.mjs）。

守卫「设置 → 启动配置 / 插件」面板的八类回归：

1. ``_collectEnvChanges`` 布尔分支的相等性检查——此前缺失该检查，每次
   「暂存更改」都会把所有布尔项重写进暂存文件，「没有需要暂存的更改」
   永不触发；差异计数（脏高亮）上线后表现为按钮常驻虚高；
2. 分组渲染：未启用插件组默认折叠 + 组头徽章、行内徽章去重、布尔开关、
   hidden 项（PLUGINS）不渲染进启动配置面板、已配置（钥匙串）密钥的
   「替换/取消」入口（「取消」仅随钥匙串托管行渲染，非托管行的输入框
   是常驻配置入口）；
3. 「保存」按钮的全局合并计数（类别/会话 ops + 刷新间隔 + 暂存差异，
   与统一保存提交口径一致）与搜索过滤的组级显隐；
4. 脏检查差异派生：``_hasPendingChanges`` 与保存共用同一 diff 函数——
   改了又改回、行内动作后的残留不再误问「是否放弃」；类别新增/行内
   编辑表单打开中仍视为有未保存修改（保守项）；
5. 插件面板渲染：核心插件无开关（恒启用徽章）、可选插件开关与依赖提示、
   discovered 状态映射为「未装配」；
6. 插件开关草稿：依赖/互斥阻止并逐步提示，通过后仅更新本插件草稿态，
   _pluginChanges 把草稿 diff 成单个 PLUGINS JSON 值；
7. 行内动作（恢复默认/密钥写清）行级贴片：不整面重载 loadEnvConfig，
   插件开关草稿与其它行未暂存编辑得以保留；
8. 统一保存流：一次点击提交全部草稿（暂存 PUT + 类别 ops 同一次生效、
   关闭弹窗），皆无更改时静默关闭且不发任何提交请求；
9. 无可选插件启用时面板顶部空态引导（7b：消息采集不可用 → 启用至少一个
   消息源并重启生效）；
10. pluginsSource==="env" 时面板顶部警示（8a：PLUGINS 由环境变量控制、
    开关重启不生效；override/dotenv/default 来源不显示）。

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
