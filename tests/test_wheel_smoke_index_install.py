"""索引安装验证的命令构造、日志断言与版本探测守卫：不联网即可回归。

为什么单独测：冒烟脚本本身只在 CI 与手工流程里跑，但这几处写错都是**静默失效**——
`:all:` 会把运行时依赖也拖去源码构建、丢掉 `--no-build-isolation` 会让构建依赖回到目标索引
（依赖混淆）、不关缓存则 `--no-binary` 形同虚设（pip 直接用缓存里的 wheel）、少了日志断言则
在 pip 选回 wheel 或混进试发索引时仍然「装成功」并全绿通过。
"""

import subprocess
import sys
from pathlib import Path

import pytest

from scripts.wheel_smoke import (
    INDEXES,
    INSTALLED_GUARD,
    PROJECT_NAME,
    VERSION_PROBE,
    SmokeFailure,
    assert_index_install_log,
    build_requirements,
    index_install_command,
)

VERSION = "0.1.0.dev1"


def test_sdist_command_forces_source_distribution() -> None:
    command = index_install_command(Path("python"), VERSION, index="testpypi", from_sdist=True)

    assert "--no-binary" in command
    assert command[command.index("--no-binary") + 1] == PROJECT_NAME  # 只对本包禁 wheel
    assert ":all:" not in command
    # 缓存里有同名 wheel 时 pip 会直接用它、--no-binary 形同虚设（实测），必须关缓存
    assert "--no-cache-dir" in command
    assert "--no-build-isolation" in command  # 构建后端由脚本先从 PyPI 预装
    assert "--no-deps" in command
    assert command[command.index("--index-url") + 1] == INDEXES["testpypi"]["simple"]
    assert command[-1] == f"{PROJECT_NAME}=={VERSION}"


def test_wheel_command_stays_on_the_wheel_path() -> None:
    command = index_install_command(Path("python"), VERSION, index="testpypi", from_sdist=False)

    assert "--no-binary" not in command
    assert "--no-cache-dir" not in command  # 装 wheel 时仍走缓存，不白白拖慢
    assert "--no-build-isolation" not in command
    assert command[-1] == f"{PROJECT_NAME}=={VERSION}"


def test_command_targets_the_requested_index() -> None:
    """两个索引各用各的 simple 端点：写死一个会让另一个验错东西。"""
    command = index_install_command(Path("python"), VERSION, index="pypi", from_sdist=False)

    assert command[command.index("--index-url") + 1] == INDEXES["pypi"]["simple"]
    assert not any("test.pypi.org" in item for item in command)


def test_build_requirements_come_from_pyproject() -> None:
    """构建后端必须读得到：空列表意味着读取路径失效，sdist 构建会悄悄用默认后端。"""
    requires = build_requirements()

    assert requires
    assert any(item.startswith("setuptools") for item in requires)


def test_version_probe_carries_installed_guard() -> None:
    """版本探测必须带安装路径守卫：裸探测会把仓库源码树/构建残留当成被测对象。"""
    assert VERSION_PROBE.startswith(INSTALLED_GUARD)
    assert "importlib.metadata" in VERSION_PROBE


def test_repo_egg_info_fools_cwd_version_lookup(tmp_path: Path) -> None:
    """记录陷阱本身：cwd 里的 briefdesk.egg-info 会盖过 site-packages 的已装版本。

    `python -c` 把 cwd 放进 sys.path 首位，而 `python -m build` 会在仓库根留下
    briefdesk.egg-info（静态版本号）——探测版本时不换目录，断言就随构建残留漂移。
    """
    egg_info = tmp_path / "briefdesk.egg-info"
    egg_info.mkdir()
    (egg_info / "PKG-INFO").write_text(
        "Metadata-Version: 2.4\nName: briefdesk\nVersion: 9.9.9\n", encoding="utf-8"
    )

    result = subprocess.run(
        [sys.executable, "-c", "import importlib.metadata as m; print(m.version('briefdesk'))"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    assert result.stdout.strip() == "9.9.9"


def test_sdist_log_with_download_and_build_passes() -> None:
    output = (
        f"Downloading {INDEXES['testpypi']['simple']}briefdesk-{VERSION}.tar.gz (548 kB)\n"
        "Building wheel for briefdesk (pyproject.toml): finished with status 'done'\n"
    )

    assert_index_install_log(output, VERSION, index="testpypi", from_sdist=True)  # 不抛即通过


def test_wheel_log_with_pypi_marker_passes() -> None:
    output = (
        f"Downloading {INDEXES['pypi']['marker']}/packages/ab/cd/"
        f"briefdesk-{VERSION}-py3-none-any.whl (622 kB)\n"
    )

    assert_index_install_log(output, VERSION, index="pypi", from_sdist=False)


def test_wheel_fallback_is_rejected() -> None:
    """--from-sdist 被跳过（pip 选回 wheel）时必须失败，且失败信息要带上定位行。"""
    output = (
        f"Collecting {PROJECT_NAME}=={VERSION}\n"
        f"  Using cached {PROJECT_NAME}-{VERSION}-py3-none-any.whl\n"
    )

    with pytest.raises(SmokeFailure, match="源码分发包") as excinfo:
        assert_index_install_log(output, VERSION, index="testpypi", from_sdist=True)
    # 失败信息要能直接看出「pip 用了缓存里的 wheel」，否则只能盲猜
    assert "Using cached" in str(excinfo.value)


def test_sdist_without_local_build_is_rejected() -> None:
    """下到了 sdist 却没有本地构建记录：同样要拦（构建被跳过时资源断言失去意义）。"""
    output = f"Downloaded {PROJECT_NAME}-{VERSION}.tar.gz\n"

    with pytest.raises(SmokeFailure, match="本地构建"):
        assert_index_install_log(output, VERSION, index="testpypi", from_sdist=True)


def test_pypi_log_without_its_marker_is_rejected() -> None:
    """正式索引的标记取 files.pythonhosted.org：test.pypi.org 是它的子串反例，不能当判据。"""
    output = f"Downloading {INDEXES['testpypi']['simple']}{PROJECT_NAME}-{VERSION}-py3-none-any.whl\n"

    with pytest.raises(SmokeFailure, match="files.pythonhosted.org"):
        assert_index_install_log(output, VERSION, index="pypi", from_sdist=False)


def test_pypi_log_mixing_testpypi_is_rejected() -> None:
    """依赖从 PyPI 下、本包却来自试发索引：两个标记同时出现时必须失败。"""
    output = (
        f"Downloading {INDEXES['pypi']['marker']}/packages/ab/cd/fastapi-0.142.0-py3-none-any.whl\n"
        f"Downloading {PROJECT_NAME}-{VERSION}-py3-none-any.whl\n"
        f"Looking in indexes: {INDEXES['testpypi']['simple']}\n"
    )

    with pytest.raises(SmokeFailure, match="混进了试发索引"):
        assert_index_install_log(output, VERSION, index="pypi", from_sdist=False)
