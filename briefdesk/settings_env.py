"""启动配置暂存层 — UI「设置 → 启动配置」改动的持久化与来源判定。

存储文件：`briefdesk/paths.py` 的 `settings_file()`（platformdirs 用户配置目录，
Windows 传 `appauthor=False` 以免多出一层 `briefdesk`：Windows
`%LOCALAPPDATA%\\briefdesk\\settings.env`，macOS `~/Library/Application
Support/briefdesk/`，Linux `~/.config/briefdesk/`），也可经环境变量
`BRIEFDESK_SETTINGS_FILE` 显式指定（测试/便携场景）。

- 文件只存非密钥键值（`KEY=VALUE` 行，UTF-8、无注释、键序稳定）；
  密钥一律走系统密钥环（briefdesk/secrets_store.py），绝不落此文件。
- 解析优先级：系统密钥环（仅密钥）> 环境变量 > **暂存文件** > `.env` > 默认值。
  六个 Settings（app 级 + weflow/weflow-legacy/qqflow/rag/benchmark）的 `env_file` 均为
  `[项目根 .env, 暂存文件]`，pydantic-settings 多文件后加载者优先。
- 写入为原子操作（同目录临时文件 + os.replace），并发由调用方持锁。

本模块不 import briefdesk.config（config 在 import 期构造环境文件列表，
避免循环依赖）。
"""

import logging
import os
from pathlib import Path

from dotenv import dotenv_values

from briefdesk import paths

logger = logging.getLogger(__name__)

# 项目根目录（本文件上溯三级：briefdesk/settings_env.py → briefdesk/ → 根）。
# 本模块不 import briefdesk.config（config 在 import 期构造环境文件列表，会循环），
# 且 settings_base 的 env_file 列表在 import 期就要用它，故在此独立解析。
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def get_settings_file() -> Path:
    """暂存文件路径：BRIEFDESK_SETTINGS_FILE 优先，否则平台用户配置目录。

    委托 `paths.settings_file()`：平台目录口径（含 Windows 的 `appauthor=False`）
    只在 paths 一处定义，避免两个模块各自手写而漂移。
    """
    return paths.settings_file()


def read_staged() -> dict[str, str]:
    """读取暂存文件内容（不存在/解析异常 → 空 dict，调用方不回滚）。"""
    path = get_settings_file()
    try:
        raw = dotenv_values(str(path), encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        logger.debug("暂存文件读取失败: %s", path, exc_info=True)
        return {}
    return {k: v for k, v in raw.items() if v is not None}


def write_staged(updates: dict[str, str | None]) -> None:
    """按更新项改写暂存文件：None = 删除该键；全部删除后移除整个文件。

    原子性：先写同目录临时文件再 os.replace；失败时原文件保持不变。
    """
    path = get_settings_file()
    current = read_staged()
    bad = [k for k, v in updates.items() if v is not None and ("\n" in v or "\r" in v)]
    if bad:
        # 纵深防御：normalize_setting 已拒绝换行值，此处防其他调用路径回归——
        # 换行会把 KEY=VALUE 行格式拆出伪键（可注入任意配置行，含密钥名）
        raise ValueError(f"暂存值不能包含换行: {bad}")
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
    try:
        tmp.write_text(lines, encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        # 写入/替换失败时清理残留 .tmp（原文件未被 os.replace 触碰，保持不变）
        tmp.unlink(missing_ok=True)
        raise


def source_of(alias: str) -> str:
    """某配置键当前的生效来源：env / override（暂存）/ dotenv / default。

    判定顺序与解析优先级一致（环境变量 > 暂存文件 > .env > 默认）：
    缺文件时静默跳过对应层。
    """
    if os.environ.get(alias) is not None:
        return "env"
    if alias in read_staged():
        return "override"
    try:
        dotenv = dotenv_values(str(PROJECT_ROOT / ".env"), encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        dotenv = {}
    if dotenv.get(alias) is not None:
        return "dotenv"
    return "default"
