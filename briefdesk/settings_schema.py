"""可供设置页面使用的 Pydantic Settings schema 工具。

插件只需暴露 ``settings_schema()``，由本模块把自己的 BaseSettings 模型
转换为 JSON 安全的字段描述。实际值由插件在调用时重新读取，因此设置页
展示的是当前进程实际采用的配置，而不是一份独立的缓存。
"""

from __future__ import annotations

import json
import math
import re
import types
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any, Union, get_args, get_origin

from pydantic import SecretStr
from pydantic.fields import PydanticUndefined
from pydantic_settings import BaseSettings


def _label_from_name(name: str) -> str:
    """把未声明展示名的字段转换成可读的英文标签。"""
    return re.sub(r"[_-]+", " ", name).strip().title()


def _field_key(model: type[BaseSettings], name: str) -> str:
    field = model.model_fields[name]
    if field.alias:
        return str(field.alias)
    prefix = str(model.model_config.get("env_prefix", ""))
    return f"{prefix}{name}".upper()


def _unwrap_annotation(annotation: Any) -> Any:
    """去掉 Annotated 和 Optional 外壳，返回实际字段类型。"""
    while True:
        origin = get_origin(annotation)
        if origin is Annotated:
            annotation = get_args(annotation)[0]
            continue
        if origin in (Union, types.UnionType):
            non_none = [arg for arg in get_args(annotation) if arg is not type(None)]
            if len(non_none) == 1:
                annotation = non_none[0]
                continue
        return annotation


def _is_secret(annotation: Any) -> bool:
    annotation = _unwrap_annotation(annotation)
    if annotation is SecretStr:
        return True
    origin = get_origin(annotation)
    if origin in (Union, types.UnionType):
        return any(_is_secret(arg) for arg in get_args(annotation))
    return False


def _field_type(annotation: Any) -> tuple[str, str | None]:
    annotation = _unwrap_annotation(annotation)
    origin = get_origin(annotation)
    if origin is list or origin is tuple:
        return "multi", None
    if annotation is bool:
        return "boolean", None
    if annotation is int:
        return "number", "integer"
    if annotation is float:
        return "number", "float"
    return "text", None


def _constraints(field: Any) -> dict[str, Any]:
    result: dict[str, Any] = {}
    names = {
        "ge": "min",
        "gt": "minExclusive",
        "le": "max",
        "lt": "maxExclusive",
    }
    for constraint in field.metadata:
        for source, target in names.items():
            value = getattr(constraint, source, None)
            if value is not None:
                result[target] = value
        multiple_of = getattr(constraint, "multiple_of", None)
        if multiple_of is not None:
            result["step"] = multiple_of
    return result


def build_settings_schema(
    model: type[BaseSettings],
    instance: BaseSettings | None = None,
    *,
    plugin: str = "",
    labels: dict[str, str] | None = None,
    hints: dict[str, str] | None = None,
    warnings: dict[str, str] | None = None,
    options: dict[str, list[str]] | None = None,
) -> list[dict[str, Any]]:
    """从 Settings 模型生成设置字段描述。

    返回值只包含可 JSON 序列化内容，密钥字段不包含明文 ``current``。
    ``instance`` 可由插件传入；为空时为展示配置创建一次模型实例。
    """
    settings: BaseSettings | None = instance
    if settings is None:
        try:
            settings = model()
        except Exception:  # noqa: BLE001 — schema 不应被无效配置阻断
            # 必填字段缺失或环境变量格式错误时，字段元数据仍可用于修复配置。
            settings = None
    labels = labels or {}
    hints = hints or {}
    warnings = warnings or {}
    options = options or {}
    result: list[dict[str, Any]] = []
    for name, field in model.model_fields.items():
        key = _field_key(model, name)
        secret = _is_secret(field.annotation)
        kind, number_kind = _field_type(field.annotation)
        if key in options:
            kind = "select"
        item: dict[str, Any] = {
            "key": key,
            "type": kind,
            "label": labels.get(name, _label_from_name(name)),
            "plugin": plugin,
            "secret": secret,
            "restart": True,
        }
        if number_kind is not None:
            item["numberKind"] = number_kind
        if name in hints:
            item["hint"] = hints[name]
        if name in warnings:
            item["warn"] = warnings[name]
        if key in options:
            item["options"] = list(options[key])
        item.update(_constraints(field))
        if field.default is not PydanticUndefined:
            default = field.default
            if not secret and default is not None:
                if isinstance(default, (list, tuple)):
                    item["default"] = list(default)
                elif isinstance(default, (str, int, float, bool)):
                    item["default"] = default
        if secret:
            # 只下发是否配置，不下发密钥内容；模型会按完整解析链读取值。
            if settings is None:
                item["configured"] = None
            else:
                secret_value = getattr(settings, name)
                item["configured"] = bool(
                    secret_value.get_secret_value()
                    if isinstance(secret_value, SecretStr)
                    else secret_value
                )
        else:
            if settings is None:
                item["current"] = None
            else:
                value = getattr(settings, name)
                if isinstance(value, (list, tuple)):
                    item["current"] = list(value)
                elif isinstance(value, (str, int, float, bool)) or value is None:
                    item["current"] = value
                else:
                    item["current"] = str(value)
        result.append(item)
    return result


def staged_value(raw: str, setting_type: str) -> object:
    """把暂存文件中的字符串转换成前端控件可识别的值。"""
    if setting_type == "multi":
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw
    return raw


def normalize_setting(meta: dict[str, Any], raw: str) -> str:
    """按 schema 元数据校验并规范化一个暂存值。"""
    setting_type = meta.get("type")
    if setting_type == "multi":
        parsed = json.loads(raw)
        if not isinstance(parsed, list) or not all(
            isinstance(value, str) for value in parsed
        ):
            raise ValueError("须为 JSON 字符串数组")
        return json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))
    if setting_type == "boolean":
        boolean_value = raw.strip().lower()
        if boolean_value not in {"true", "false"}:
            raise ValueError("须为 true 或 false")
        return boolean_value
    if setting_type == "select":
        if raw not in meta.get("options", []):
            raise ValueError(f"非法选项 {raw!r}")
        return raw
    if setting_type == "number":
        try:
            if meta.get("numberKind") == "integer":
                number_value: int | float = int(raw)
            else:
                number_value = float(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("须为数字") from exc
        if not math.isfinite(float(number_value)):
            raise ValueError("须为有限数字")
        if "min" in meta and number_value < meta["min"]:
            raise ValueError(f"必须大于等于 {meta['min']}")
        if "minExclusive" in meta and number_value <= meta["minExclusive"]:
            raise ValueError(f"必须大于 {meta['minExclusive']}")
        if "max" in meta and number_value > meta["max"]:
            raise ValueError(f"必须小于等于 {meta['max']}")
        if "maxExclusive" in meta and number_value >= meta["maxExclusive"]:
            raise ValueError(f"必须小于 {meta['maxExclusive']}")
        if "step" in meta:
            try:
                step = Decimal(str(meta["step"]))
                is_multiple = step != 0 and (
                    Decimal(str(number_value)) % step == 0
                )
            except (InvalidOperation, TypeError, ValueError):
                is_multiple = False
            if not is_multiple:
                raise ValueError(f"必须是 {meta['step']} 的倍数")
        return str(number_value) if meta.get("numberKind") == "float" else str(int(number_value))
    if not isinstance(raw, str):
        raise TypeError("值须为字符串")
    # 暂存文件是 KEY=VALUE 行格式：值含 CR/LF 会被回读拆成独立行——既破坏
    # round-trip，更可借任一 text 字段向暂存文件注入任意 KEY=VALUE（绕过键
    # 白名单与「密钥只走 keyring」分层，因为路由层白名单只过滤键名，管不住
    # 值内注入）。「 #」是 dotenv 行内注释起点，值会被截断，一并拒绝。
    if "\n" in raw or "\r" in raw:
        raise ValueError("值不能包含换行符")
    if " #" in raw:
        raise ValueError("值不能包含「 #」（dotenv 行内注释起点）")
    return raw
