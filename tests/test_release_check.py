"""release_check 的纯函数回归：版本递推、瞬态改版本与产物成员断言。

不联网、不构建：联网与构建部分是 scripts/release_check.py 的运行路径，由实际发布流程验证。
"""

import tarfile
import zipfile
from pathlib import Path

import pytest

from scripts.release_check import (
    INDEXES,
    ReleaseCheckFailure,
    assert_artifact_members,
    next_dev_version,
    patched_version,
    read_base_version,
    twine_upload_command,
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


def test_index_table_covers_both_targets() -> None:
    """两个索引的端点必须都在表里：漏一个会让正式发布走错索引。"""
    assert set(INDEXES) == {"testpypi", "pypi"}
    assert INDEXES["testpypi"]["json"] != INDEXES["pypi"]["json"]


def test_twine_upload_targets_the_requested_repository(tmp_path: Path) -> None:
    """本机试发的仓库名不能写死 testpypi：正式发布的兜底路径会传错索引。"""
    artifacts = [tmp_path / "briefdesk-0.1.0-py3-none-any.whl"]

    command = twine_upload_command(artifacts, target="pypi")

    assert command[command.index("--repository") + 1] == "pypi"
    assert "--non-interactive" in command
    assert command[-1].endswith(".whl")


def _fake_wheel(path: Path, members: list[str]) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for member in members:
            archive.writestr(member, "")
    return path


def _fake_sdist(path: Path, members: list[str]) -> Path:
    import io

    with tarfile.open(path, "w:gz") as archive:
        for member in members:
            payload = b""
            info = tarfile.TarInfo(f"briefdesk-0.1.0/{member}")
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    return path


def test_artifact_members_reject_wheel_without_runtime_resources(tmp_path: Path) -> None:
    """少带运行时资源的 wheel 必须在**上传前**失败：twine check 只看元数据格式。"""
    wheel = _fake_wheel(
        tmp_path / "briefdesk-0.1.0-py3-none-any.whl",
        ["briefdesk/__init__.py", "briefdesk-0.1.0.dist-info/METADATA"],
    )
    sdist = _fake_sdist(
        tmp_path / "briefdesk-0.1.0.tar.gz",
        ["PKG-INFO", "pyproject.toml", "briefdesk/__init__.py"],
    )

    with pytest.raises(ReleaseCheckFailure, match="wheel 资源与期望集合不一致"):
        assert_artifact_members(wheel, sdist)


def test_artifact_members_reject_sdist_without_metadata(tmp_path: Path) -> None:
    """wheel 齐了但 sdist 缺 PKG-INFO：同样拦下（sdist 侧复用 sdist_check 的断言）。"""
    from scripts.runtime_manifest import expected_data_members

    members = [*expected_data_members(), "briefdesk/__init__.py"]
    wheel = _fake_wheel(
        tmp_path / "briefdesk-0.1.0-py3-none-any.whl",
        [*members, "briefdesk-0.1.0.dist-info/METADATA"],
    )
    sdist = _fake_sdist(tmp_path / "briefdesk-0.1.0.tar.gz", members)

    with pytest.raises(ReleaseCheckFailure, match="PKG-INFO"):
        assert_artifact_members(wheel, sdist)


def test_patched_version_requires_static_version_line(tmp_path: Path) -> None:
    """没有静态 version 行时响亮失败，并且**不得**改动文件。"""
    pyproject = tmp_path / "pyproject.toml"
    original = '[project]\nname = "briefdesk"\n'
    pyproject.write_text(original, encoding="utf-8")

    with pytest.raises(ReleaseCheckFailure), patched_version(pyproject, "0.1.0.dev1"):
        pass

    assert pyproject.read_text(encoding="utf-8") == original
