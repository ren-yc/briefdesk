"""依赖方向守卫：核心不得静态 import 插件实现（briefdesk.plugins.*）。

插件实现层（briefdesk/plugins/*，P2 起）可自由依赖核心与 briefdesk/plugin/*；
反之则禁止，否则「本体只保留最核心功能」的边界会被悄悄打破。
briefdesk/plugins/ 目录 P2 才出现，本守卫从 P1 起就生效：
核心侧任何模块 import briefdesk.plugins.* 即失败。
"""

import ast
import unittest
from pathlib import Path

_CIR = Path(__file__).resolve().parents[1] / "briefdesk"


def _imported_plugin_modules(tree: ast.AST) -> list[str]:
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "briefdesk.plugins" or alias.name.startswith(
                    "briefdesk.plugins."
                ):
                    names.append(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module and (
            node.module == "briefdesk.plugins"
            or node.module.startswith("briefdesk.plugins.")
        ):
            names.append(node.module)
    return names


class CoreImportGuardTest(unittest.TestCase):
    def test_core_never_imports_plugins(self):
        violations: list[tuple[str, list[str]]] = []
        for path in sorted(_CIR.rglob("*.py")):
            rel = path.relative_to(_CIR)
            if rel.parts[0] == "plugins":
                continue  # 实现层内部互引是允许的
            tree = ast.parse(path.read_text(encoding="utf-8"))
            names = _imported_plugin_modules(tree)
            if names:
                violations.append((str(rel), names))
        self.assertEqual(
            violations,
            [],
            f"核心模块静态依赖了插件实现: {violations}",
        )
