"""路径计算：只算不建目录，每次调用求值，支持显式覆盖。

覆盖变量只读进程环境（`os.environ`），刻意不读 `.env`：路径是「进程从哪里
启动」的问题，写进项目 `.env` 会被 wheel 用户误当成可配置项。

不设模块级路径常量：常量在 import 期求值后无法被 `monkeypatch.setenv` 影响，
测试与冒烟脚本都无法把数据/缓存重定向到临时目录，写入点也会绕过隔离断言。
"""

import logging
import os
import tomllib
from pathlib import Path

from platformdirs import (
    user_cache_dir as _platform_cache_dir,
)
from platformdirs import (
    user_config_dir as _platform_config_dir,
)
from platformdirs import (
    user_data_dir as _platform_data_dir,
)

logger = logging.getLogger(__name__)

APP_NAME = "briefdesk"


def user_config_dir() -> Path:
    """用户配置目录（UI 暂存 `settings.env` 所在处）。"""
    return Path(_platform_config_dir(APP_NAME, appauthor=False))


def user_data_dir() -> Path:
    """用户数据目录；`BRIEFDESK_DATA_DIR` 优先于平台默认位置。

    Windows 必须传 `appauthor=False`：缺省 platformdirs 把 `appauthor` 回退为
    appname，路径会多出一层 `%LOCALAPPDATA%\\briefdesk\\briefdesk`，与缓存目录
    也对不上（缓存是 `…\\briefdesk\\Cache`）。
    """
    explicit = os.environ.get("BRIEFDESK_DATA_DIR")
    if explicit:
        return Path(explicit)
    return Path(_platform_data_dir(APP_NAME, appauthor=False))


def user_cache_dir() -> Path:
    """用户缓存目录（可安全删除）；`BRIEFDESK_CACHE_DIR` 优先。"""
    explicit = os.environ.get("BRIEFDESK_CACHE_DIR")
    if explicit:
        return Path(explicit)
    return Path(_platform_cache_dir(APP_NAME, appauthor=False))


def database_dir() -> Path:
    """数据库文件所在目录（用户数据目录下的 `data/`）。"""
    return user_data_dir() / "data"


def database_path() -> Path:
    """默认数据库文件路径；显式 `DB_PATH` 完全覆盖它。"""
    return database_dir() / "briefdesk.sqlite"


def benchmark_data_dir() -> Path:
    """Benchmark 用户数据根目录（网页导出用例 + CLI 报告）。"""
    return user_data_dir() / "benchmark"


def benchmark_cases_dir() -> Path:
    """Benchmark 用户用例目录（`*.fromweb.json` 与手写数据集）。"""
    return benchmark_data_dir() / "cases"


def benchmark_reports_dir() -> Path:
    """Benchmark CLI 报告输出目录（Web 报告随各自 run 目录落缓存）。"""
    return benchmark_data_dir() / "reports"


def benchmark_runs_dir() -> Path:
    """Benchmark supervisor 运行目录（缓存目录，可随时删除）。"""
    return user_cache_dir() / "benchmark" / "runs"


def settings_file() -> Path:
    """UI 暂存配置文件路径；`BRIEFDESK_SETTINGS_FILE` 保持既有显式覆盖入口。"""
    explicit = os.environ.get("BRIEFDESK_SETTINGS_FILE")
    if explicit:
        return Path(explicit)
    return user_config_dir() / "settings.env"


def _package_root() -> Path:
    """本包目录（源码下为 仓库/briefdesk，wheel 下为 site-packages/briefdesk）。"""
    return Path(__file__).resolve().parent


def project_dotenv_path() -> Path | None:
    """源码 / editable 模式返回项目根 `.env` 路径；wheel 模式返回 None。

    判据以文件系统为主：包目录的父目录存在 `pyproject.toml` 且其
    `[project].name == "briefdesk"` → 源码模式。刻意不用 importlib.metadata：
    cwd 在 `sys.path[0]` 时，源码树里的 `briefdesk.egg-info`（`pip install -e .`
    的构建残留）会先于 site-packages 的 dist-info 被命中，而 egg-info 没有
    `direct_url.json`——按元数据判定会把 editable 误判成 wheel，开发机随即不再
    读项目根 `.env`。源码直跑（没有 dist-info）同样走文件系统判据，不受影响。

    wheel 安装时包目录在 site-packages 下，其父目录没有本项目 pyproject.toml，
    因此不读任何隐式 `.env`（site-packages 与 cwd 都不读）。pyproject.toml 存在
    但读不出来时不降级为「源码模式」：无法确认这是本项目的树，就不读隐式配置，
    并留一条 warning 供排查（静默改变配置优先级比不读更难查）。
    """
    root = _package_root().parent
    pyproject = root / "pyproject.toml"
    try:
        with pyproject.open("rb") as handle:
            data = tomllib.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, tomllib.TOMLDecodeError) as e:
        logger.warning(
            "项目 pyproject.toml 读取失败，按 wheel 模式处理（不读项目 .env）: %s", e
        )
        return None
    project = data.get("project")
    if not isinstance(project, dict) or project.get("name") != APP_NAME:
        return None
    return root / ".env"
