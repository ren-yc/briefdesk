"""sdist 清单静态守卫：MANIFEST.in 的规则必须**有效**，期望集必须与登记表同源。

为什么只做静态断言：sdist 的真成员集合要构建出来才知道，那一步交给
`scripts/sdist_check.py`（构建 + 双向相等）。这里拦的是另一类失败——规则被删、
被改成空转（对当前树零命中，只在每次构建刷 "no previously-included ... found
matching" 噪音）、或期望集与 `scripts/runtime_manifest.py` 脱钩。
"""

from scripts.runtime_manifest import (
    EXCLUDED,
    REPO,
    REQUIRED,
    expected_data_members,
    icon_manifest_entries,
)

_MANIFEST = REPO / "MANIFEST.in"

#: sdist 必须显式剪掉的目录：distutils 默认会把 tests/ 顶层 test*.py 收进 sdist
_PRUNED = ("tests",)

#: 必须显式钉住的标准文件（setuptools 默认会带，但写下来防默认规则变化）
_INCLUDED = ("pyproject.toml", "README.md", "LICENSE")


def _rules() -> list[tuple[str, str]]:
    rules: list[tuple[str, str]] = []
    for raw in _MANIFEST.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        command, _, target = line.partition(" ")
        rules.append((command, target.strip()))
    return rules


def test_manifest_exists_and_is_not_empty() -> None:
    assert _MANIFEST.is_file(), "缺少 MANIFEST.in：tests/ 会被打进 sdist"
    assert _rules(), "MANIFEST.in 只有注释，等于没有规则"


def test_every_rule_matches_something() -> None:
    """空转规则会被 setuptools 每次构建打 warning——噪音会掩盖真告警。"""
    for command, target in _rules():
        assert target, f"MANIFEST.in 规则缺少目标: {command}"
        path = REPO / target
        if command == "prune":
            assert path.is_dir(), f"prune 的目标不是目录（空转规则）: {target}"
        elif command in {"include", "exclude", "recursive-include"}:
            assert path.exists(), f"{command} 的目标不存在（空转规则）: {target}"
        elif command in {"global-include", "global-exclude"}:
            assert list(REPO.rglob(target)), f"{command} 未命中任何文件（空转规则）: {target}"
        else:
            raise AssertionError(f"未识别的 MANIFEST.in 指令: {command}")


def test_tests_directory_is_pruned() -> None:
    assert ("prune", "tests") in _rules(), "tests/ 必须被剪掉（否则 sdist 随仓库测试膨胀）"


def test_standard_files_are_included() -> None:
    included = {target for command, target in _rules() if command == "include"}
    missing = [name for name in _INCLUDED if name not in included]
    assert not missing, f"标准文件未显式 include（默认规则变化即丢）: {missing}"


def test_registry_is_consistent() -> None:
    """登记表本身要自洽：条目存在、两类不重叠、图标由清单派生。"""
    assert REQUIRED, "运行时资源登记表为空"
    for item in REQUIRED:
        assert (REPO / item).is_file(), f"登记的资源在磁盘上不存在: {item}"
    assert not set(REQUIRED) & set(EXCLUDED), "同一资源同时被登记为必需与排除"
    assert EXCLUDED, "排除清单为空（维护资源会被打进包）"
    icons = icon_manifest_entries()
    assert icons, "图标清单为空"
    assert not set(icons) & set(REQUIRED), "图标应由清单派生，不重复登记"
    expected = expected_data_members()
    assert expected == sorted(set(expected)), "期望集合必须排序去重"
    assert set(expected) == {*REQUIRED, *icons}
