"""启动配置暂存层 — UI「设置 → 启动配置」改动的持久化与来源判定。

存储文件：`briefdesk/paths.py` 的 `settings_file()`（platformdirs 用户配置目录，
Windows 传 `appauthor=False` 以免多出一层 `briefdesk`：Windows
`%LOCALAPPDATA%\\briefdesk\\settings.env`，macOS `~/Library/Application
Support/briefdesk/`，Linux `~/.config/briefdesk/`），也可经环境变量
`BRIEFDESK_SETTINGS_FILE` 显式指定（测试/便携场景）。

- 文件只存非密钥键值（`KEY=VALUE` 行，UTF-8、无注释、键序稳定）；
  密钥一律走系统密钥环（briefdesk/secrets_store.py），绝不落此文件。
- 解析优先级：系统密钥环（仅密钥）> 环境变量 > **项目根 .env** > **用户暂存文件**
  > 默认值。两个 dotenv 层是**独立来源**（不再是同一个 env_file 列表里的两项）：
  `dotenv_layers()` 是唯一的有序层定义，解析（settings_base）与展示
  （`source_of` / 启动快照）都消费它——各写一份判定链必然漂移（小写键就是
  已复现的一例：解析采纳、判定漏看，设置页显示的来源与实际生效值矛盾）。
- 项目根 .env 只在源码 / editable 模式存在（`paths.project_dotenv_path()`）；
  wheel 安装不读任何隐式 .env（site-packages 与 cwd 都不读）。
- 写入为原子操作（同目录临时文件 + os.replace），并发由调用方持锁。

本模块不 import briefdesk.config（config 在 import 期构造配置实例，
避免循环依赖）。
"""

import logging
import os
from pathlib import Path

from dotenv import dotenv_values
from pydantic_settings import BaseSettings

from briefdesk import paths

logger = logging.getLogger(__name__)

# 来源层名：设置页 source / expected_source 的取值，前端按此措辞
SOURCE_ENV = "env"
SOURCE_DOTENV = "dotenv"  # 项目根 .env（仅源码 / editable 模式）
SOURCE_OVERRIDE = "override"  # 用户暂存文件（settings.env）
SOURCE_DEFAULT = "default"


def get_settings_file() -> Path:
    """暂存文件路径：BRIEFDESK_SETTINGS_FILE 优先，否则平台用户配置目录。

    委托 `paths.settings_file()`：平台目录口径（含 Windows 的 `appauthor=False`）
    只在 paths 一处定义，避免两个模块各自手写而漂移。
    """
    return paths.settings_file()


def field_env_key(model: type[BaseSettings], name: str) -> str:
    """字段对应的环境变量键：显式 `alias` 优先，否则 env_prefix + 字段名大写。

    与 settings_schema 共用（它原来自己实现了一份）：键的推导若分两处，
    设置页会按一个键展示、解析却按另一个键生效。
    """
    field = model.model_fields[name]
    if field.alias:
        return str(field.alias)
    prefix = str(model.model_config.get("env_prefix", ""))
    return f"{prefix}{name}".upper()


def dotenv_layers() -> list[tuple[str, Path]]:
    """按优先级从高到低返回 dotenv 层：(层名, 文件)。

    唯一的有序层定义，解析与展示共用。文件不存在也照常返回（读取时静默跳过）；
    项目根 .env 只在源码 / editable 模式出现，wheel 模式下这一层不存在。
    """
    layers: list[tuple[str, Path]] = []
    project = paths.project_dotenv_path()
    if project is not None:
        layers.append((SOURCE_DOTENV, project))
    layers.append((SOURCE_OVERRIDE, paths.settings_file()))
    return layers


def _env_value(alias: str) -> str | None:
    """进程环境变量值（大小写不敏感，与 pydantic-settings 的默认口径一致）。"""
    direct = os.environ.get(alias)
    if direct is not None:
        return direct
    target = alias.upper()
    for key, value in os.environ.items():
        if key.upper() == target:
            return value
    return None


def _file_has_key(path: Path, alias: str) -> bool:
    """文件里是否有该键：大小写不敏感，且 `KEY=` 的空串算「有」。

    为什么空串算有：`DotEnvSettingsSource` 会采纳空串（普通 str 字段得到 `''`、
    int 字段直接 ValidationError），"留空即未设置" 不成立。
    """
    try:
        values = dotenv_values(str(path), encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    target = alias.upper()
    return any(key is not None and key.upper() == target for key in values)


def source_of(alias: str, layers: list[tuple[str, Path]] | None = None) -> str:
    """某配置键当前的生效来源：env / dotenv / override / default。

    判定顺序取自 `dotenv_layers()`（与解析同一份定义，高优先级在前）；显式传入
    `layers` 可判定自定义层序（例如显式 `_env_file` 构造时实际用的文件）。
    """
    if _env_value(alias) is not None:
        return SOURCE_ENV
    for name, path in dotenv_layers() if layers is None else layers:
        if _file_has_key(path, alias):
            return name
    return SOURCE_DEFAULT


# ── 启动快照（source 与 current 同时点）──

_startup_sources: dict[str, str] | None = None


def capture_startup_sources(model: type[BaseSettings]) -> dict[str, str]:
    """按共享层序反推各字段**启动时**的来源并缓存，供设置页展示。

    为什么不能实时判定：`config` 在启动时构造、无热应用——运行中改文件不会改变
    本进程实际生效的值。"下次启动生效"另有 `expected_*` 表达，两者不可混用。
    密钥字段也一并记录，但展示侧不消费（密钥走 configured/keyringConfigured）。
    """
    global _startup_sources
    snapshot = {
        key: source_of(key)
        for key in (field_env_key(model, name) for name in model.model_fields)
    }
    _startup_sources = snapshot
    return snapshot


def startup_source(alias: str) -> str | None:
    """启动快照里该键的来源；尚未捕获（如未 import config）时返回 None。"""
    if _startup_sources is None:
        return None
    return _startup_sources.get(alias)


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
