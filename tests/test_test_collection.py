"""测试收集守卫 — 拦截「写了测试但 pytest 静默不收集」的失效模式。

失败模式：pytest 默认按名字前缀 `Test*` 收集测试类（unittest.TestCase
子类例外，按基类收集）。类名写成 `XxxTest` 后缀形态、或不继承
TestCase 又不以 Test 开头时，**收集 0 条且退出码为 0**——CI 全绿但
这些用例从未运行；断言漂移要等到改名那天才炸出（本仓库真实发生过：
scripts/recalibrate_similarity 的单测类长期未跑）。同理，名字合格但
定义了 `__init__` 的普通类也会被 pytest 警告后跳过收集。

守卫用 AST 静态扫描（不依赖 pytest 内部 API），基类解析跨 tests/ 全部
模块（共享 unittest 基座经 import 复用的模式必须可达，如
test_settings_env.py 定义、round2 文件导入的 StagedFileTestCase）：
- 类内定义了 test_* 方法 → 必须满足其一：类名 Test* 前缀；基类链可达
  unittest 的 TestCase/IsolatedAsyncioTestCase；被同文件内其他类作
  基类继承（共享基座模式，用例经可收集子类执行，如 test_db.py 的
  内存库基座）。
- Test* 前缀且非 unittest 子类的类不得定义 __init__。
"""

from __future__ import annotations

import ast
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent

# unittest 收集口径的基类名（末段比较：兼容 `unittest.TestCase` 与裸导入）
_UNITTEST_BASES = {"TestCase", "IsolatedAsyncioTestCase"}


def _base_last_names(node: ast.ClassDef) -> list[str]:
    out: list[str] = []
    for base in node.bases:
        if isinstance(base, ast.Name):
            out.append(base.id)
        elif isinstance(base, ast.Attribute):
            out.append(base.attr)
        elif isinstance(base, ast.Subscript):  # Generic 基（如 TestCase[T]）
            inner = base.value
            if isinstance(inner, ast.Name):
                out.append(inner.id)
            elif isinstance(inner, ast.Attribute):
                out.append(inner.attr)
    return out


def _load_registry() -> dict[str, ast.ClassDef]:
    """tests/ 全部模块的类名 → 节点（跨文件 import 的基座也能解析）。

    同名类以首个出现为准；tests/ 内类名冲突本就异常，若真冲突到误判
    收集性，改名即是修复方向，不做更重的 import 图解析。
    """
    registry: dict[str, ast.ClassDef] = {}
    for path in sorted(_TESTS_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name not in registry:
                registry[node.name] = node
    return registry


def _unittest_like(name: str, registry: dict[str, ast.ClassDef]) -> bool:
    """基类链（跨文件）可达 unittest TestCase 形态即算 unittest 收集口径。"""
    seen: set[str] = set()
    frontier = [name]
    while frontier:
        current = frontier.pop()
        node = registry.get(current)
        if node is None:
            continue
        for base in _base_last_names(node):
            if base in _UNITTEST_BASES:
                return True
            if base in registry and base not in seen:
                seen.add(base)
                frontier.append(base)
    return False


def _has_test_methods(node: ast.ClassDef) -> bool:
    return any(
        isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
        and item.name.startswith("test_")
        for item in node.body
    )


def _has_init(node: ast.ClassDef) -> bool:
    return any(
        isinstance(item, ast.FunctionDef) and item.name == "__init__"
        for item in node.body
    )


def _collectable_self(name: str, registry: dict[str, ast.ClassDef]) -> bool:
    """类自身可收集口径：Test* 前缀（名字匹配）或 unittest 基类链可达。"""
    return name.startswith("Test") or _unittest_like(name, registry)


def test_test_classes_are_collected_by_pytest() -> None:
    registry = _load_registry()
    # 基类名 → 子类名集合（共享基座豁免用，跨文件继承也算）
    based_on: dict[str, set[str]] = {}
    for node in registry.values():
        for base in _base_last_names(node):
            based_on.setdefault(base, set()).add(node.name)

    violations: list[str] = []
    for path in sorted(_TESTS_DIR.rglob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for cls in ast.walk(tree):
            if not isinstance(cls, ast.ClassDef):
                continue
            if not _has_test_methods(cls):
                continue
            is_unittest = _unittest_like(cls.name, registry)
            if cls.name.startswith("Test"):
                if not is_unittest and _has_init(cls):
                    violations.append(
                        f"{path.name}:{cls.lineno} class {cls.name} 定义了 "
                        "__init__，pytest 会跳过收集（改用夹具）"
                    )
                continue
            if is_unittest:
                continue
            children = based_on.get(cls.name, set())
            if any(_collectable_self(child, registry) for child in children):
                # 共享基座：自身不收集，但用例经同仓库内可收集子类执行
                continue
            violations.append(
                f"{path.name}:{cls.lineno} class {cls.name} 含 test_ 方法但不被"
                " pytest 收集（类名需 Test* 前缀或继承 unittest.TestCase）"
            )
    assert not violations, "存在静默不收集的测试类：\n" + "\n".join(violations)
