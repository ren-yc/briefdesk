"""release_check 的纯函数回归：版本递推与「瞬态改版本 + 逐字节还原」。

不联网、不构建：联网与构建部分是 scripts/release_check.py 的运行路径，由实际发布流程验证。
"""

from pathlib import Path

import pytest

from scripts.release_check import (
    ReleaseCheckFailure,
    next_dev_version,
    patched_version,
    read_base_version,
)


def test_next_dev_version_picks_first_free() -> None:
    assert next_dev_version("0.1.0", set()) == "0.1.0.dev1"
    assert next_dev_version("0.1.0", {"0.1.0.dev1", "0.1.0.dev2"}) == "0.1.0.dev3"
    # 有空洞就补空洞，避免版本号无谓地一直涨
    assert next_dev_version("0.1.0", {"0.1.0.dev2"}) == "0.1.0.dev1"


def test_next_dev_version_folds_existing_dev_suffix() -> None:
    """仓库版本若已被写成 dev 号，仍归并到同一版本线，不产生 0.1.0.dev1.dev1。"""
    assert next_dev_version("0.1.0.dev5", {"0.1.0.dev1", "0.1.0.dev5"}) == "0.1.0.dev2"


def test_read_base_version_reads_this_project() -> None:
    """仓库自己的 pyproject 必须有静态 version——预检与构建都依赖它。"""
    assert read_base_version()


def test_patched_version_restores_original_bytes(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    original = '[project]\nname = "briefdesk"\nversion = "0.1.0"\n'
    pyproject.write_text(original, encoding="utf-8")

    with patched_version(pyproject, "0.1.0.dev7"):
        assert 'version = "0.1.0.dev7"' in pyproject.read_text(encoding="utf-8")

    assert pyproject.read_text(encoding="utf-8") == original, "退出上下文必须逐字节还原"


def test_patched_version_requires_static_version_line(tmp_path: Path) -> None:
    """没有静态 version 行时响亮失败，并且**不得**改动文件。"""
    pyproject = tmp_path / "pyproject.toml"
    original = '[project]\nname = "briefdesk"\n'
    pyproject.write_text(original, encoding="utf-8")

    with pytest.raises(ReleaseCheckFailure), patched_version(pyproject, "0.1.0.dev1"):
        pass

    assert pyproject.read_text(encoding="utf-8") == original
