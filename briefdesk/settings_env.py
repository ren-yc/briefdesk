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


def _lookup_value(values: dict[str, str | None], alias: str) -> str | None:
    """大小写不敏感查表（与 pydantic-settings 的默认口径一致）。"""
    target = alias.upper()
    for key, value in values.items():
        if key is not None and key.upper() == target:
            return value
    return None


def _file_value(path: Path, alias: str) -> str | None:
    """文件里该键的值：大小写不敏感，且 `KEY=` 的空串算「有值」。

    为什么空串算有值：`DotEnvSettingsSource` 会采纳空串（普通 str 字段得到 `''`、
    int 字段直接 ValidationError），"留空即未设置" 不成立。
    """
    try:
        values = dotenv_values(str(path), encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    return _lookup_value(values, alias)


def env_shadowed_dotenv_keys(model: type[BaseSettings]) -> list[str]:
    """项目 .env 里定义了、却被进程环境变量压住且**取值不同**的键名。

    为什么只在「取值不同」时报：同值覆盖虽然同样由环境变量胜出，但取值没变，报出来
    只是噪音——用户需要的是「改了 .env 为什么不生效」。

    为什么跳过密钥字段：密钥还有 keyring 层，而这里的判据只看 env 与 dotenv——keyring
    已配置时解除环境变量也不会让 .env 生效，报「被环境变量压住」会把人引向错方向。

    wheel 模式（`project_dotenv_path()` 为 None）恒为空；坏 .env 只漏报不误报。
    返回键名列表，**绝不返回值**。
    """
    project = paths.project_dotenv_path()
    if project is None:
        return []
    try:
        file_values = dotenv_values(str(project), encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    secret_keys = set(getattr(model, "KEYRING_FIELDS", {}).values())
    shadowed: list[str] = []
    for name in model.model_fields:
        alias = field_env_key(model, name)
        if alias in secret_keys:
            continue
        env_value = _env_value(alias)
        if env_value is None:
            continue  # 不是环境变量压着（含「只有 .env」的情形）
        file_value = _lookup_value(file_values, alias)
        if file_value is None or file_value == env_value:
            continue
        shadowed.append(alias)
    return shadowed


def source_of(alias: str, layers: list[tuple[str, Path]] | None = None) -> str:
    """某配置键当前的生效来源：env / dotenv / override / default。

    判定顺序取自 `dotenv_layers()`（与解析同一份定义，高优先级在前）；显式传入
    `layers` 可判定自定义层序（例如显式 `_env_file` 构造时实际用的文件）。
    """
    return composed_value(alias, layers=layers)[1]


def composed_value(
    alias: str,
    *,
    staged_override: dict[str, str] | None = None,
    layers: list[tuple[str, Path]] | None = None,
) -> tuple[str | None, str]:
    """按共享层序返回该键**下次启动**生效的原始值与来源。

    `staged_override` 传入「写入之后的暂存态」时，暂存层按它取值而不是读磁盘——
    PUT 的组合校验要在落盘前判断下次启动的组合是否自洽。
    """
    env_value = _env_value(alias)
    if env_value is not None:
        return env_value, SOURCE_ENV
    for name, file in dotenv_layers() if layers is None else layers:
        if name == SOURCE_OVERRIDE and staged_override is not None:
            value = staged_override.get(alias)
        else:
            value = _file_value(file, alias)
        if value is not None:
            return value, name
    return None, SOURCE_DEFAULT


# ── 启动快照（source 与 current 同时点）──

#: 按模型登记的运行快照：核心 Settings 在 config 构造后登记，插件模型在插件
#: setup 成功后由 PluginManager 登记——登记时刻即「运行实例取值的那一刻」。
_model_sources: dict[type[BaseSettings], dict[str, str]] = {}
_core_sources: dict[str, str] | None = None


def capture_model_sources(model: type[BaseSettings]) -> dict[str, str]:
    """按共享层序反推该模型各字段**此刻**的来源并登记，供设置页展示。

    为什么不能每次渲染都实时判定：`config`/插件实例在启动时构造、无热应用——
    运行中改文件不会改变本进程实际生效的值。"下次启动生效"另有 `expected_*`
    表达，两者混用会出现「显示已生效、实际没生效」。密钥字段也一并登记，
    但展示侧不消费（密钥走 configured/keyringConfigured）。
    """
    snapshot = {
        key: source_of(key)
        for key in (field_env_key(model, name) for name in model.model_fields)
    }
    _model_sources[model] = snapshot
    return snapshot


def model_sources(model: type[BaseSettings]) -> dict[str, str] | None:
    """该模型已登记的快照；未登记返回 None（调用方决定何时登记）。"""
    return _model_sources.get(model)


def capture_startup_sources(model: type[BaseSettings]) -> dict[str, str]:
    """核心 Settings 的启动快照入口（config.py 在构造 config 后调用）。"""
    global _core_sources
    _core_sources = capture_model_sources(model)
    return _core_sources


def registered_models() -> list[type[BaseSettings]]:
    """已登记来源快照的模型：核心 Settings + 装配成功的插件模型。

    启动期聚合提示（哪些键被环境变量压住）需要的是「本进程在用的模型集合」，
    直接读登记表而不是让调用方各自维护一份名单——登记时刻即模型真正生效的时刻。
    """
    return list(_model_sources)


def startup_source(alias: str) -> str | None:
    """核心快照里该键的来源；尚未捕获（如未 import config）时返回 None。"""
    if _core_sources is None:
        return None
    return _core_sources.get(alias)


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
