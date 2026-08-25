"""启动配置暂存层 — UI「设置 → 启动配置」改动的持久化与来源判定。

存储文件：`platformdirs.user_config_dir("briefdesk") / "settings.env"`
（Windows: %LOCALAPPDATA%\\briefdesk\\settings.env；macOS: ~/Library/
Application Support/briefdesk/；Linux: ~/.config/briefdesk/），也可经
环境变量 `BRIEFDESK_SETTINGS_FILE` 显式指定（测试/便携场景）。

- 文件只存非密钥键值（`KEY=VALUE` 行，UTF-8、无注释、键序稳定）；
  密钥一律走系统密钥环（briefdesk/secrets_store.py），绝不落此文件。
- 解析优先级：系统密钥环（仅密钥）> 环境变量 > **暂存文件** > `.env` > 默认值。
  三个 Settings（app 级 + weflow/qqflow）的 `env_file` 均为
  `[项目根 .env, 暂存文件]`，pydantic-settings 多文件后加载者优先。
- 写入为原子操作（同目录临时文件 + os.replace），并发由调用方持锁。

本模块不 import briefdesk.config（config 在 import 期构造环境文件列表，
避免循环依赖）。
"""

import logging
import os
from pathlib import Path

from dotenv import dotenv_values
from platformdirs import user_config_dir

logger = logging.getLogger(__name__)

# 显式指定暂存文件的环境变量（测试/便携场景；普通用户不感知）
_SETTINGS_FILE_ENV = "BRIEFDESK_SETTINGS_FILE"

# 项目根目录（本文件上溯三级：briefdesk/settings_env.py → briefdesk/ → 根）。
# 与 config.PROJECT_ROOT 同值，但为规避循环导入在此独立解析。
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def get_settings_file() -> Path:
    """暂存文件路径：BRIEFDESK_SETTINGS_FILE 优先，否则平台用户配置目录。"""
    explicit = os.environ.get(_SETTINGS_FILE_ENV)
    if explicit:
        return Path(explicit)
    return Path(user_config_dir("briefdesk")) / "settings.env"


def read_staged() -> dict[str, str]:
    """读取暂存文件内容（不存在/解析异常 → 空 dict，调用方不回滚）。"""
    path = get_settings_file()
    try:
        raw = dotenv_values(str(path), encoding="utf-8")
    except OSError:
        logger.debug("暂存文件读取失败: %s", path, exc_info=True)
        return {}
    return {k: v for k, v in raw.items() if v is not None}


def write_staged(updates: dict[str, str | None]) -> None:
    """按更新项改写暂存文件：None = 删除该键；全部删除后移除整个文件。

    原子性：先写同目录临时文件再 os.replace；失败时原文件保持不变。
    """
    path = get_settings_file()
    current = read_staged()
    for key, value in updates.items():
        if value is None:
            current.pop(key, None)
        else:
            current[key] = value
    if not current:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.debug("暂存文件删除失败: %s", path, exc_info=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    lines = "".join(f"{k}={v}\n" for k, v in current.items())
    tmp.write_text(lines, encoding="utf-8")
    os.replace(tmp, path)


def source_of(alias: str) -> str:
    """某配置键当前的生效来源：override（暂存）/ env / dotenv / default。

    判定顺序与解析优先级一致（环境变量 > 暂存文件 > .env > 默认）：
    缺文件时静默跳过对应层。
    """
    if alias in read_staged():
        return "override"
    if os.environ.get(alias) is not None:
        return "env"
    try:
        dotenv = dotenv_values(str(PROJECT_ROOT / ".env"), encoding="utf-8")
    except OSError:
        dotenv = {}
    if dotenv.get(alias) is not None:
        return "dotenv"
    return "default"