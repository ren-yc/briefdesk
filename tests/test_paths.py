"""路径计算模块（briefdesk/paths.py）直测。

覆盖四类契约——它们各自对应一处失败模式，改动时不要顺手放宽：
- 覆盖变量只读进程环境：写进 `.env` 无效（否则 wheel 用户会以为 .env 能改路径）；
- 每次调用求值：import 之后改环境变量必须立刻生效（import 期常量会让隔离失效）；
- 只算不建目录：目录由各写入点在写入前创建（避免 import 阶段产生副作用）；
- 两种模式识别：直接调用真实的识别函数，而不是只 patch 它的返回值
  （实现恒返回 None 也能让消费方测试全绿）。
"""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from platformdirs import (
    user_cache_dir,
    user_config_dir,
    user_data_dir,
)

from briefdesk import paths


def _fake_package_root(tmp_path: Path, pyproject: str | None) -> Path:
    """构造「包目录 + 父目录 pyproject.toml」的临时树，返回包目录。"""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    if pyproject is not None:
        (tmp_path / "pyproject.toml").write_text(pyproject, encoding="utf-8")
    return pkg


def test_platform_defaults_disable_appauthor(monkeypatch):
    """缺省路径等于 platformdirs(appauthor=False)：Windows 上不多一层 briefdesk。"""
    monkeypatch.delenv("BRIEFDESK_DATA_DIR", raising=False)
    monkeypatch.delenv("BRIEFDESK_CACHE_DIR", raising=False)
    assert paths.user_data_dir() == Path(user_data_dir(paths.APP_NAME, appauthor=False))
    assert paths.user_cache_dir() == Path(
        user_cache_dir(paths.APP_NAME, appauthor=False)
    )
    assert paths.user_config_dir() == Path(
        user_config_dir(paths.APP_NAME, appauthor=False)
    )


@pytest.mark.skipif(sys.platform != "win32", reason="appauthor 回退只在 Windows 生效")
def test_windows_data_dir_has_no_doubled_app_name(monkeypatch):
    """回归：%LOCALAPPDATA%\\briefdesk\\briefdesk 是缺省 appauthor 的错误形态。"""
    monkeypatch.delenv("BRIEFDESK_DATA_DIR", raising=False)
    assert paths.user_data_dir().name == paths.APP_NAME


def test_env_overrides_win_over_platform_defaults(monkeypatch, tmp_path):
    data = tmp_path / "data-root"
    cache = tmp_path / "cache-root"
    monkeypatch.setenv("BRIEFDESK_DATA_DIR", str(data))
    monkeypatch.setenv("BRIEFDESK_CACHE_DIR", str(cache))
    assert paths.user_data_dir() == data
    assert paths.user_cache_dir() == cache
    assert paths.database_path() == data / "data" / "briefdesk.sqlite"


def test_paths_are_evaluated_per_call(monkeypatch, tmp_path):
    """import 之后改环境变量必须生效（守住「不做 import 期常量」）。"""
    first = tmp_path / "first"
    second = tmp_path / "second"
    monkeypatch.setenv("BRIEFDESK_DATA_DIR", str(first))
    before = paths.database_path()
    monkeypatch.setenv("BRIEFDESK_DATA_DIR", str(second))
    after = paths.database_path()
    assert before == first / "data" / "briefdesk.sqlite"
    assert after == second / "data" / "briefdesk.sqlite"


def test_values_follow_environment_changes_for_all_entrypoints(monkeypatch, tmp_path):
    monkeypatch.setenv("BRIEFDESK_DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("BRIEFDESK_CACHE_DIR", str(tmp_path / "c"))
    monkeypatch.setenv("BRIEFDESK_SETTINGS_FILE", str(tmp_path / "s.env"))
    assert paths.settings_file() == tmp_path / "s.env"
    monkeypatch.delenv("BRIEFDESK_SETTINGS_FILE")
    assert paths.settings_file() == paths.user_config_dir() / "settings.env"


def test_data_dir_ignores_dotenv_and_cwd(monkeypatch, tmp_path):
    """同理：.env 不是路径来源。

    切到含同名键的 .env 目录再断言缺省路径仍来自 platformdirs——
    证明路径计算完全不经过 dotenv 解析链。
    """
    project = tmp_path / "proj"
    project.mkdir()
    (project / ".env").write_text(
        "BRIEFDESK_DATA_DIR=/evil\nBRIEFDESK_CACHE_DIR=/evil-cache\n", encoding="utf-8"
    )
    monkeypatch.chdir(project)
    monkeypatch.delenv("BRIEFDESK_DATA_DIR", raising=False)
    monkeypatch.delenv("BRIEFDESK_CACHE_DIR", raising=False)
    assert paths.user_data_dir() == Path(user_data_dir(paths.APP_NAME, appauthor=False))


def test_paths_do_not_create_directories(monkeypatch, tmp_path):
    """只算不建目录：调用任何路径函数都不得产生文件系统副作用。"""
    root = tmp_path / "not-created"
    monkeypatch.setenv("BRIEFDESK_DATA_DIR", str(root))
    monkeypatch.setenv("BRIEFDESK_CACHE_DIR", str(root / "cache"))
    monkeypatch.setenv("BRIEFDESK_SETTINGS_FILE", str(root / "settings.env"))
    (
        paths.user_config_dir(),
        paths.user_data_dir(),
        paths.user_cache_dir(),
        paths.database_dir(),
        paths.database_path(),
        paths.benchmark_data_dir(),
        paths.benchmark_cases_dir(),
        paths.benchmark_reports_dir(),
        paths.benchmark_runs_dir(),
        paths.settings_file(),
    )
    assert not root.exists()


def test_benchmark_layout_splits_data_and_cache(monkeypatch, tmp_path):
    data = tmp_path / "data-root"
    cache = tmp_path / "cache-root"
    monkeypatch.setenv("BRIEFDESK_DATA_DIR", str(data))
    monkeypatch.setenv("BRIEFDESK_CACHE_DIR", str(cache))
    assert paths.benchmark_cases_dir() == data / "benchmark" / "cases"
    assert paths.benchmark_reports_dir() == data / "benchmark" / "reports"
    assert paths.benchmark_runs_dir() == cache / "benchmark" / "runs"


def test_paths_with_spaces_are_preserved(monkeypatch, tmp_path):
    spaced = tmp_path / "with space" / "data dir"
    monkeypatch.setenv("BRIEFDESK_DATA_DIR", str(spaced))
    assert paths.database_path() == spaced / "data" / "briefdesk.sqlite"


def test_project_dotenv_path_detects_source_tree(tmp_path):
    pkg = _fake_package_root(
        tmp_path, '[project]\nname = "briefdesk"\nversion = "0.1.0"\n'
    )
    with patch.object(paths, "_package_root", return_value=pkg):
        assert paths.project_dotenv_path() == tmp_path / ".env"


def test_project_dotenv_path_ignores_foreign_project(tmp_path):
    pkg = _fake_package_root(tmp_path, '[project]\nname = "other-app"\n')
    with patch.object(paths, "_package_root", return_value=pkg):
        assert paths.project_dotenv_path() is None


def test_project_dotenv_path_without_pyproject_is_wheel_mode(tmp_path):
    pkg = _fake_package_root(tmp_path, None)
    with patch.object(paths, "_package_root", return_value=pkg):
        assert paths.project_dotenv_path() is None


def test_project_dotenv_path_with_unreadable_pyproject_is_wheel_mode(tmp_path, caplog):
    """坏 pyproject.toml 不得静默变成「源码模式」，但也不得静默无痕。"""
    pkg = _fake_package_root(tmp_path, "[project\nname = broken")
    with (
        caplog.at_level("WARNING", logger="briefdesk.paths"),
        patch.object(paths, "_package_root", return_value=pkg),
    ):
        assert paths.project_dotenv_path() is None
    assert "pyproject.toml" in caplog.text


def test_project_dotenv_path_on_real_source_tree():
    """本仓库直跑测试时是源码模式：真实调用必须指向仓库根 .env。"""
    repo_root = Path(__file__).resolve().parent.parent
    assert paths.project_dotenv_path() == repo_root / ".env"
