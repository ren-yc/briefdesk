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
from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any, Union, cast, get_args, get_origin

from pydantic import SecretStr
from pydantic.fields import PydanticUndefined
from pydantic_settings import BaseSettings

from briefdesk.settings_env import (
    capture_model_sources,
    field_env_key,
    model_sources,
    source_of,
)


def _label_from_name(name: str) -> str:
    """把未声明展示名的字段转换成可读的英文标签。"""
    return re.sub(r"[_-]+", " ", name).strip().title()


def _field_key(model: type[BaseSettings], name: str) -> str:
    """字段 → 环境变量键；规则与解析侧共用（见 settings_env.field_env_key）。"""
    return field_env_key(model, name)


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


def _render_default(value: Any) -> dict[str, Any]:
    """把字段默认值转成可下发的 JSON 值；不可下发（字典/Decimal 等）时返回空 dict。"""
    if isinstance(value, (list, tuple)):
        return {"default": list(value)}
    if isinstance(value, (str, int, float, bool)):
        return {"default": value}
    return {}


def render_value(value: Any) -> Any:
    """把字段值转成可下发的 JSON 值；列表保序、标量原样、其余转字符串。"""
    if isinstance(value, (list, tuple)):
        return list(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _sanitize_error(text: str) -> str:
    """脱敏并截断校验错误：pydantic 会把出错的值回显在 input_value 里。"""
    head = text.split(" [input_value=")[0]
    lines = [line.strip() for line in head.splitlines() if line.strip()]
    return (lines[-1][:200] if lines else "") or "配置解析失败"


def _expected_instance(model: type[BaseSettings]) -> tuple[BaseSettings | None, str]:
    """构造「下次启动生效值」用的全新实例；坏配置返回 (None, 脱敏摘要)。

    为什么单独构造：`expected_*` 是「按当前文件重新解析」的语义，与运行实例
    （`current`）无关；坏配置只影响这一路，字段元数据与运行值仍要照常下发。
    """
    try:
        return model(), ""
    except Exception as e:  # noqa: BLE001 — 坏配置不得让设置页 500
        return None, _sanitize_error(str(e))


def build_settings_schema(
    model: type[BaseSettings],
    instance: BaseSettings | None = None,
    *,
    running: bool = True,
    plugin: str = "",
    labels: dict[str, str] | None = None,
    hints: dict[str, str] | None = None,
    warnings: dict[str, str] | None = None,
    options: dict[str, list[str]] | None = None,
) -> list[dict[str, Any]]:
    """从 Settings 模型生成设置字段描述。

    三个时点必须分清（混用会让设置页显示「已生效」而实际没有）：

    - `current` / `source`：**运行快照**——本进程正在用的值，以及它来自哪一层
      （来源读按模型登记的快照，见 settings_env.capture_model_sources）。
      `running=False`（插件未装配/装配失败/无运行实例）时不展示这两项，item 带
      `running: False`，由前端显示「无运行值」，而不是拿下次启动值冒充。
    - `expected_value` / `expected_source`：按**当前文件与环境**重新解析得到的
      「下次启动生效值/来源」；解析失败时置 `expectedAvailable=False` + 脱敏摘要。
    - 密钥字段：只下发 `configured`，不参与上述任何值时点。

    返回值只包含可 JSON 序列化内容，密钥字段不包含明文。
    """
    settings: BaseSettings | None = instance
    if settings is None and running:
        try:
            settings = model()
        except Exception:  # noqa: BLE001 — schema 不应被无效配置阻断
            # 必填字段缺失或环境变量格式错误时，字段元数据仍可用于修复配置。
            settings = None
    sources = model_sources(model) or capture_model_sources(model)
    expected, expected_error = _expected_instance(model)
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
            "running": running,
        }
        if not running:
            item.pop("current", None)
        if number_kind is not None:
            item["numberKind"] = number_kind
        if name in hints:
            item["hint"] = hints[name]
        if name in warnings:
            item["warn"] = warnings[name]
        if key in options:
            item["options"] = list(options[key])
        item.update(_constraints(field))
        if running:
            item["source"] = sources.get(key) or source_of(key)
        if field.default is not PydanticUndefined:
            if not secret and field.default is not None:
                item.update(_render_default(field.default))
        elif field.default_factory is not None and not secret:
            # 工厂字段（如 DB_PATH）：field.default 是 PydanticUndefined，默认值只有
            # 调用工厂才知道——漏读会让设置页丢掉这一个键的默认值展示。工厂抛错或
            # 返回不可下发值时只省略该键，不阻断整份 schema（与上面实例化失败仍保留
            # 字段元数据同口径）。
            try:
                # 工厂签名是 Callable[[], Any] | Callable[[dict], Any] 的联合，联合调用
                # mypy 无法判定实参个数，这里显式取零参形态：需要 validated_data 的工厂
                # 会走下面的异常分支省略该键（schema 期拿不到实例数据）。
                factory = cast(Callable[[], Any], field.default_factory)
                factory_default = factory()
            except Exception:  # noqa: BLE001 — 默认值展示失败不应阻断 schema
                factory_default = None
            if factory_default is not None:
                item.update(_render_default(factory_default))
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
            if running:
                item["current"] = (
                    None if settings is None else render_value(getattr(settings, name))
                )
            if expected is None:
                item["expectedAvailable"] = False
                item["expectedError"] = expected_error or "配置解析失败"
            else:
                item["expectedAvailable"] = True
                item["expected_source"] = source_of(key)
                item["expected_value"] = render_value(getattr(expected, name))
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
            # 空串也走这条：前端可能把「没有运行值」的空输入框当成改动提交，报错要
            # 直接点明正确路径（行内「恢复默认」），否则用户只会看到笼统的保存失败
            raise ValueError("须为数字（留空不等于恢复默认；清空请用「恢复默认」）") from exc
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
