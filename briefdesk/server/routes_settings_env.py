"""启动配置路由（server 子包）—「设置 → 启动配置 / 插件」面板后端。

- `GET /api/settings/env`     元数据 + 生效值/暂存值/来源 + 插件开关数据 + 密钥状态
- `PUT /api/settings/env`     批量暂存（白名单 + 类型/约束校验 + 插件依赖/互斥
  复检 409 + 原子写）；响应携带受影响键的最终 staged/source，供前端行级贴片
- `POST /api/settings/secrets`  写入密钥到系统密钥环（keyring）；响应携带该密钥
  的 configured/keyringConfigured 新状态
- `DELETE /api/settings/secrets/{name}`  清除密钥（幂等）；响应同上

设计约束：密钥值**永不下发**（GET 只含 configured 布尔）；暂存文件只存
非密钥键（存储层见 briefdesk/settings_env.py）。PLUGINS 由「插件」面板
逐插件开关编辑（schema 中带 hidden 标记，不在启动配置面板渲染）。
"""

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from briefdesk.config import REASONING_EFFORT_VALUES, Settings, config
from briefdesk.secrets_store import (
    DB_KEYS_BASE,
    SECRET_NAMES,
    delete_db_keys,
    delete_secret,
    get_db_keys,
    get_secret,
    set_db_keys,
    set_secret,
)
from briefdesk.server.app import app
from briefdesk.server.web_plugins import (
    get_plugin_meta,
    get_plugins_info,
    get_settings_schema,
    has_settings_schema_callback,
    validate_plugin_selection,
)
from briefdesk.settings_env import (
    SOURCE_DOTENV,
    SOURCE_ENV,
    StagedCommitError,
    StagedReadError,
    commit_staged_write,
    composed_value,
    get_settings_file,
    prepare_staged_write,
    read_staged,
    read_staged_from_bytes,
    read_staged_strict,
    resolve_alias_layers,
    source_of,
    startup_source,
)
from briefdesk.settings_schema import (
    build_settings_schema,
    normalize_setting,
    staged_value,
)

router = APIRouter()

logger = logging.getLogger(__name__)

# 单进程写锁 + 原子写：并发 PUT 串行化
_write_lock = asyncio.Lock()

# 核心设置的展示覆盖层；字段本身从 Settings.model_fields 自动发现。
_CORE_UI: dict[str, dict[str, Any]] = {
    "PLUGINS": {
        "label": "启用的可选插件",
        "hint": "显式列表，无通配；核心插件恒装配，无需列出",
        # 由「插件」面板逐插件开关编辑，不在启动配置面板渲染
        "hidden": True,
    },
    "PLUGINS_REQUIRED": {
        "label": "必选插件",
        "hint": "这些可选插件装配失败时将阻止应用启动（核心插件恒装配，无需列入）",
    },
    "PLUGIN_PATH": {"label": "开发期插件目录", "hint": "留空表示不扫描外部插件"},
    "AI_API_KEY": {"label": "AI API Key"},
    "AI_API_BASE": {"label": "AI API 地址"},
    "AI_MODEL": {"label": "AI 模型"},
    "AI_MAX_CONCURRENCY": {"label": "AI 请求最大并发", "hint": "0 = 不限制；本地模型建议设为 1"},
    "AI_REASONING_EFFORT": {
        "label": "AI 推理强度",
        "hint": "auto=不干预；off=关闭思考（端点拒收该参数时自动改为不发送）；"
                "omit=不发送该参数；其余为固定强度",
    },
    "AI_VISION_ENABLED": {
        "label": "AI 支持图片输入（视觉模型）",
        "hint": "开启后含图消息将 OCR 文本连同图片一并送入 AI_MODEL；需启用 ocr 插件",
    },
    "AI_VISION_MAX_IMAGES": {
        "label": "单条消息随请求附图上限",
        "hint": "多图消息超出部分只发 OCR 文本",
    },
    "MAX_CLASSIFY_TOKENS": {"label": "单次分类最大输出 token"},
    "BACKFILL_HOURS": {"label": "历史回填窗口（小时）", "hint": "-1 = 拉取全部历史（耗时且 AI 调用激增，慎用）"},
    "IGNORED_EXPIRY_HOURS": {"label": "已忽略条目过期（小时）", "hint": "0 = 禁用清理"},
    "IGNORE_SELF": {"label": "过滤自己发送的消息"},
    "REALTIME_BATCH_MAX_COUNT": {"label": "实时批缓冲条数", "hint": "攒够条数触发 AI 处理；调大可省 token、实时性略降"},
    "REALTIME_BATCH_TIMEOUT_MS": {"label": "实时批缓冲超时（毫秒）"},
    "BACKFILL_BATCH_MAX_COUNT": {"label": "回填批量（条）", "hint": "回填时单次 AI 分类合并条数；调大省 token"},
    "POLL_OVERLAP_SECONDS": {"label": "轮询重叠窗口（秒）", "hint": "吸收增量轮询边界偏差；重叠部分去重无 AI 开销"},
    "DEDUP_SIMILARITY_THRESHOLD": {"label": "去重字符重叠阈值"},
    "EMBED_API_BASE": {"label": "嵌入 API 地址", "hint": "留空则禁用嵌入向量去重与相关功能"},
    "EMBED_MODEL": {"label": "嵌入模型"},
    "EMBED_API_KEY": {"label": "嵌入 API Key"},
    "EMBED_BATCH_SIZE": {"label": "嵌入批量大小"},
    "DEDUP_EMBED_THRESHOLD": {"label": "嵌入余弦预过滤阈值"},
    "DEDUP_EMBED_TOP_K": {"label": "嵌入候选条数"},
    "DEDUP_STRONG_THRESHOLD": {"label": "同文本短路阈值"},
    "DEDUP_EMBED_FALLBACK_THRESHOLD": {"label": "低置信复核阈值"},
    "MERGE_WINDOW_MINUTES": {"label": "同话题合并窗口（分钟）", "hint": "0 = 禁用合并"},
    "MERGE_MAX_CANDIDATES": {"label": "合并候选上限"},
    "DB_PATH": {"label": "数据库文件路径", "warn": "重启后数据库将使用新路径；旧库不会被自动发现或迁移"},
    "SERVER_PORT": {"label": "服务端口", "warn": "重启后访问地址将变为新端口"},
    "LOG_LEVEL": {"label": "日志级别", "hint": "DEBUG 开启逐条细节；INFO 只保留阶段与汇总"},
}

def _core_schema() -> list[dict[str, Any]]:
    """核心设置的字段描述（**按请求重建**）。

    必须重建而不是缓存：expected_* 是「按当前文件重新解析」的语义，缓存会把它
    冻结在 import 时刻，用户改完文件后看到的下次启动值仍是旧的；current/source
    来自运行快照，重建不改变其时点。

    文案来源：核心字段的中文 label/hint/warn 由下面的 `_CORE_UI` 覆盖循环给出
    （按 item["key"] 即环境键名 update），未收录进 `_CORE_UI` 的字段回落
    `_label_from_name` 的英文标签——补文案就往 `_CORE_UI` 加条目，不要改回向
    build_settings_schema 传 labels/hints/warnings：它按**字段名**（log_level）
    查，而这里手里只有环境键名（LOG_LEVEL），传了恒落空且不报错（静默失去
    文案）。options 是例外：schema 侧按 key 查、真实生效。
    """
    items = build_settings_schema(
        Settings,
        config,
        options={
            "LOG_LEVEL": ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
            # 必须按环境键名登记：settings_schema 以 key 判 select，否则字段只会渲染成
            # 文本框（见 _field_type 的兜底分支）
            "AI_REASONING_EFFORT": list(REASONING_EFFORT_VALUES),
        },
    )
    for item in items:
        item.update(_CORE_UI.get(item["key"], {}))
    return items


#: 核心密钥条目：**静态元数据**（label/plugin/白名单键名），从 KEYRING_FIELDS
#: 派生保持单一真源。不携带 configured——徽章的 configured 由 `_secret_effective`
#: 每请求按当前解析链活判定（动机与边界见该函数 docstring）。键集合由用例钉住
#: == set(Settings.KEYRING_FIELDS.values())。
_SECRET_LABELS = {
    "AI_API_KEY": "AI API Key",
    "EMBED_API_KEY": "嵌入 API Key",
    "WEFLOW_API_TOKEN": "weflow 访问令牌",
    "WEFLOW_IMG_AES_KEY": "weflow 图片 AES 解密密钥",
    "WEFLOW_IMG_XOR_KEY": "weflow 图片 XOR 解密密钥",
    "WEFLOW_DB_KEYS": "weflow 库密钥映射（JSON；超长自动分片存储）",
    "WEFLOW_LEGACY_API_TOKEN": "WeFlow Legacy 访问令牌",
    "QQFLOW_API_TOKEN": "qqflow 访问令牌",
    "QQFLOW_KEY": "qqflow 引导密钥",
    "RAG_API_KEY": "RAG 问答 API Key",
}

_CORE_SECRET_SCHEMA: list[dict[str, Any]] = [
    {"key": name, "label": _SECRET_LABELS.get(name, name), "plugin": "", "secret": True}
    for name in Settings.KEYRING_FIELDS.values()
]


def _all_schema(dynamic: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """核心字段加全部已发现插件的可选字段（含禁用/自禁用插件，便于启用前
    预配置必填项），按 key 去重。

    `dynamic` 传入本次请求已构建的插件 schema：一次 GET/PUT 要同时用非密钥项
    与密钥项，各建一遍会把插件侧的解析成本翻倍（每项含逐字段的来源判定与
    一次实例构造）。缺省时自行取，兼容单点调用。
    """
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    shared_dynamic = get_settings_schema() if dynamic is None else dynamic
    for item in [*_core_schema(), *shared_dynamic]:
        key = item.get("key")
        if not isinstance(key, str) or item.get("secret") or key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _secret_schema(dynamic: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """核心密钥加当前选中插件密钥；无 manager 时保留旧版兼容列表。

    `dynamic` 与 `_all_schema` 共用同一份插件 schema（见其 docstring 的成本理由）。
    """
    plugin_items = get_settings_schema() if dynamic is None else dynamic
    result = [*(_CORE_SECRET_SCHEMA)]
    if plugin_items or has_settings_schema_callback():
        result.extend(item for item in plugin_items if item.get("secret"))
    else:
        result.extend(
            {
                "key": name,
                "label": _SECRET_LABELS.get(name, name),
                "plugin": name.split("_", 1)[0] if "_" in name else "",
                "secret": True,
            }
            for name in SECRET_NAMES
            if name not in {item["key"] for item in result}
        )
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in result:
        key = item.get("key")
        if isinstance(key, str) and key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def _keyring_secret_truthy(name: str) -> bool:
    """钥匙串里该密钥是否有真值条目（keyringConfigured 徽章的语义，不得挪用）。

    `WEFLOW_DB_KEYS` 走 `get_db_keys()`：分段合并后 `json.loads` 校验不通过时
    返回 None——用 `get_secret(规范名)` 判会把「存了下落的坏段」报成已配置，
    与解析链（`KeyringSource._read_value` 同函数）矛盾（回归见
    tests/test_settings_env.py 的 DB_KEYS 坏 JSON 用例）。空串条目按未配置：
    `secrets set X ""` 留的空串不得以「已配置」压过环境变量/.env 的有效值
    （与 `KeyringSource.__call__` 的真值过滤同口径）。
    """
    if name == DB_KEYS_BASE:
        return get_db_keys() is not None
    return bool(get_secret(name))


def _secret_effective(name: str, *, keyring_truthy: bool | None = None) -> bool:
    """该密钥按**当前完整解析链**是否有有效值（configured 徽章的活判定）。

    口径 = keyring > 环境变量 > 项目 .env > 暂存 的胜出值真值：keyring 层真值，
    否则 `composed_value` 胜出值的真值。**只判胜出值、不判层存在性**——环境变量
    留了空串行时「层存在」但真实链采纳 `''`，按存在判会谎报「已配置」（失败模式
    回归见 tests/test_settings_env.py 的空串覆盖用例）。

    替代旧实现里 import 期冻结的 `configured`：真实链已无值而徽章谎报已配置
    （进程实测复现；守卫见 tests/test_settings_env.py 的陈旧快照用例）。本节是
    这条口径的**单点权威**，其余调用点只写一行指针。
    成立前提：各 Settings 模型零参构造且不传 `_env_file`——`composed_value` 看不见
    init 与 file_secret 层，当前核心与全部插件均满足，将来若有用注入形态构造
    密钥，本判定与真实链漂移。已知 exotic 边界：同一 dotenv 文件内大小写变体键
    并存时，pydantic 的 `parse_env_vars` 取末个、`_lookup_value` 取首个非 None——
    徽章可能与真实链不一致（与 `source_of` 共享的既有口径差，未另修）。
    keyring 查找名与 env 键必须同串（本判定用 schema 键查 keyring，真实链
    `KeyringSource` 用 `KEYRING_FIELDS` 值查）；parity 由用例钉住，分叉即假值。
    隐私红线：判定经 `composed_value` 读真实文件，只返回 bool；任何日志、异常
    detail 与响应都不得带出取值。
    """
    if keyring_truthy is None:
        keyring_truthy = _keyring_secret_truthy(name)
    if keyring_truthy:
        return True
    value, _source = composed_value(name)
    return bool(value)


def _schema_of(key: str, schema: list[dict[str, Any]] | None = None) -> dict:
    for meta in (_all_schema() if schema is None else schema):
        if meta["key"] == key:
            return meta
    raise KeyError(key)


def _item_state(meta: dict[str, Any], raw_staged: str | None) -> dict[str, Any]:
    """单键展示状态：暂存态 + 运行值 + 覆盖态 + 下次启动值（GET/PUT 同口径）。

    三个时点必须分清：current/source 是**运行快照**（本进程正在用），staged 是草稿，
    expected_* 是**下次启动**。overridden 只在「草稿被更高优先层压住」时为真——
    被覆盖时不提示「已生效」，也不改运行值；恢复默认（null）后草稿消失、覆盖态解除。
    """
    staged_val = (
        staged_value(raw_staged, meta["type"]) if raw_staged is not None else None
    )
    # 覆盖态属**预期**语义（与 expected_* 同源）：判据是「下次启动时草稿会不会被
    # 更高层压住」，不是启动快照——启动快照只描述本进程正在用的值（source/current）。
    expected_source = meta.get("expected_source")
    expected_available = bool(meta.get("expectedAvailable", True))
    overridden = bool(
        staged_val is not None
        and expected_available
        and expected_source in (SOURCE_ENV, SOURCE_DOTENV)
    )
    return {
        "staged": staged_val,
        "source": meta.get("source"),
        "overridden": overridden,
        "override_source": expected_source if overridden else None,
        "override_value": meta.get("expected_value") if overridden else None,
        "expected_value": meta.get("expected_value"),
        "expected_source": meta.get("expected_source"),
        "expectedAvailable": meta.get("expectedAvailable", True),
        "expectedError": meta.get("expectedError", ""),
    }


def _fresh_item_state(
    key: str, staged_now: dict[str, str], schema: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """写操作后该键的最终状态（与 GET 同口径，供前端行级贴片）。"""
    meta = _schema_of(key, schema)  # PUT 白名单已放行，必命中
    return _item_state(meta, staged_now.get(key))


class _PluginsUnresolvable(Exception):
    """插件名单无法解析为合法的字符串数组（含各层坏值与名单缺失语义）。

    显式不可用而不是回落旧值：删除最后一个 PLUGINS 暂存后回落启动 config.plugins，
    会放行「保存成功、重启必失败」的必选组合；解析失败被静默吞掉则会把坏 JSON
    等同于「无冲突」放行。两类都必须让 PUT 以 409 点名。
    """

    def __init__(self, key: str, detail: str) -> None:
        super().__init__(detail)
        self.key = key
        self.detail = detail


def _dedupe_issues(issues: list[dict[str, str]]) -> list[dict[str, str]]:
    """同名 issue 去重：启用名单不可解析时，候选解析与必选复检会各报一条。

    两条内容相同，重复下发只会让 409 的明细看起来像两个独立问题。
    """
    seen: set[tuple[tuple[str, str], ...]] = set()
    unique: list[dict[str, str]] = []
    for issue in issues:
        key = tuple(sorted((k, str(v)) for k, v in issue.items()))
        if key in seen:
            continue
        seen.add(key)
        unique.append(issue)
    return unique


def _resolve_plugin_list(key: str, staged_override: dict[str, str] | None = None) -> list[str] | None:
    """按共享层序解析 PLUGINS / PLUGINS_REQUIRED 为字符串数组。

    返回 None 表示所有层都未提供（真实默认 []）；解析失败/元素非字符串抛
    _PluginsUnresolvable——不回落 config.plugins、不过滤坏元素。空串是「已提供
    的坏值」（数组字段会解码失败），按不可用处理，不等同未设置。

    逐层解码判可用性、胜出值判类型，两者与真实构造语义一一对应：任一层提供了
    该键但 JSON 解码失败即不可用（真实 Settings() 会在那一层解码失败，只看胜出
    值会放行「保存成功、下次启动失败」的配置）；元素类型只对胜出值判——低层
    合法 JSON 但被高层覆盖的错类型（如 [17]）按 pydantic 合并语义是可启动的。
    层序与胜出值共用 resolve_alias_layers 一份定义（来源展示取自同一结果）。
    """
    layers = resolve_alias_layers(key, staged_override=staged_override)
    if not layers:
        return None
    winning_source, raw = layers[0]
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise _PluginsUnresolvable(
            key, f"{key} 不是合法 JSON（{exc.msg}，来源 {winning_source}）"
        ) from exc
    for source, value in layers[1:]:
        try:
            json.loads(value)
        except json.JSONDecodeError as exc:
            raise _PluginsUnresolvable(
                key, f"{key} 不是合法 JSON（{exc.msg}，来源 {source}）"
            ) from exc
    if not isinstance(parsed, list) or not all(isinstance(n, str) for n in parsed):
        raise _PluginsUnresolvable(key, f"{key} 须为 JSON 字符串数组")
    return parsed


def _desired_plugins(staged_override: dict[str, str] | None = None) -> list[str]:
    """下次启动的可选插件启用列表：按**共享层序**合成（env > 项目 .env > 暂存）。

    只看暂存会漏掉被 .env/环境变量压住的情形——那种组合重启后由更高层决定，
    校验必须按实际生效的组合判，否则会放行「重启即中止」的配置。
    staged_override 传「写入之后的暂存态」时按它取值（PUT 落盘前预检）。
    所有层都未提供时取模型默认 []——不是启动 config.plugins（那是本进程的旧值，
    下次启动已不生效）。解析失败属防御分支（暂存值写入时已过 normalize_setting，
    但高层 env/.env 里的坏值仍会走到这里）。
    """
    parsed = _resolve_plugin_list("PLUGINS", staged_override=staged_override)
    return parsed if parsed is not None else []


def _required_plugin_issues(staged: dict[str, str]) -> list[dict[str, str]]:
    """PLUGINS_REQUIRED 与 PLUGINS 的自洽复检（写入暂存之前的快返回）。

    两个键由「插件」面板分别写入：单独看各自合法，合起来却可能矛盾——某插件被
    勾成必选却不在启用列表里。这里提前拦下，并按依赖复检同一形状回 issue 明细。
    名单解析失败/必选名单混入非字符串都属不可用：显式报 issue，不静默过滤后放行
    （过滤会让 REQUIRED=[17] 这类坏值留到启动期才炸）。

    核心插件恒装配，列入必选视为恒满足（面板也不提供它们的必选芯片）。
    """
    try:
        parsed = _resolve_plugin_list("PLUGINS_REQUIRED", staged_override=staged)
        required = parsed if parsed is not None else []
    except _PluginsUnresolvable as exc:
        return [{"type": "unavailable", "key": exc.key, "detail": exc.detail}]
    if not required:
        return []
    known = {p.get("name") for p in get_plugins_info() if p.get("name")}
    core = {p.get("name") for p in get_plugins_info() if p.get("core")}
    # 启用名单本身不可解析时同样按不可用报：这里若让 _PluginsUnresolvable 冒出去，
    # 路由会变成 500——坏名单（高层 env/项目 .env 或暂存）配有效必选名单时必然触发。
    try:
        desired = set(_desired_plugins(staged))
    except _PluginsUnresolvable as exc:
        return [{"type": "unavailable", "key": exc.key, "detail": exc.detail}]
    issues: list[dict[str, str]] = []
    for name in required:
        if known and name not in known:
            issues.append(
                {"type": "unknown", "plugin": name, "detail": f"未知插件名: {name}"}
            )
        elif name not in core and name not in desired:
            issues.append(
                {
                    "type": "required_not_enabled",
                    "plugin": name,
                    "detail": f"必选插件 {name} 不在启用列表中（重启将无法启动）",
                }
            )
    return issues


def _plugin_toggle_data(staged: dict[str, str]) -> tuple[list[dict[str, Any]], str | None]:
    """「插件」面板数据：声明元数据 + 期望启用态 + 当前进程装配状态。

    返回 (面板数据, 预期不可用说明)。名单无法解析时 enabled 没有可信答案——
    面板项的 enabled 置 None（前端显式未知，不渲染成「全关」），说明文案随
    pluginsExpectedError 下发；不抛错：设置页不能因坏名单整页 500。

    加载失败记录（无插件实例、元数据缺失）兜底追加，保证面板仍可见其
    失败原因。
    """
    try:
        parsed = _resolve_plugin_list("PLUGINS", staged_override=staged)
        desired = set(parsed) if parsed is not None else set()
        unavailable: str | None = None
    except _PluginsUnresolvable as exc:
        desired = set()
        unavailable = exc.detail
    runtime = {p.get("name"): p for p in get_plugins_info() if p.get("name")}
    plugins: list[dict[str, Any]] = []
    seen: set[str] = set()
    for meta in get_plugin_meta():
        name = meta["name"]
        seen.add(name)
        rt = runtime.get(name, {})
        plugins.append(
            {
                **meta,
                # 未知预期：None = 显式未知，区别于 False（确定关闭）
                "enabled": True if meta["core"] else (name in desired if unavailable is None else None),
                "status": rt.get("status", "discovered"),
                "reason": rt.get("reason", ""),
            }
        )
    for name, rt in runtime.items():
        if name in seen:
            continue
        plugins.append(
            {
                "name": name,
                "version": "",
                "dependencies": [],
                "conflicts": [],
                "core": False,
                "enabled": name in desired if unavailable is None else None,
                "status": rt.get("status", ""),
                "reason": rt.get("reason", ""),
            }
        )
    return plugins, unavailable


def _plugins_source() -> str:
    """PLUGINS 的运行来源：返回**真正压住该键的那一层**。

    显示与开关禁用判据必须是同一个答案：enabled 按实时层序合成（_desired_plugins），
    开关可编辑性必须与它一致。方向不能写反：启动快照回落 default 是「无快照」的
    占位值（真值字符串）——运行中往项目 .env 新增 PLUGINS 时若先取启动侧，实时
    的 dotenv 永远被 default 挡住，开关显示可编辑、草稿重启却不生效。
    """
    startup = startup_source("PLUGINS")
    realtime = source_of("PLUGINS")
    # 优先取「压住它的层」（实时侧在前：草稿的生效与否由当前文件状态决定）；
    # 两侧都没被压住时才是运行值/快照/默认。
    for value in (realtime, startup):
        if value in (SOURCE_ENV, SOURCE_DOTENV):
            return value
    return realtime or startup or "default"


def _normalize(key: str, raw: str, schema: list[dict[str, Any]] | None = None) -> str:
    """动态 schema + 类型/约束校验，返回规范化的暂存字符串。"""
    try:
        meta = _schema_of(key, schema)
        return normalize_setting(meta, raw)
    except HTTPException:
        raise
    except (ValueError, json.JSONDecodeError) as exc:
        msg = str(exc)
        raise HTTPException(422, f"{key}: 校验失败（{msg}）") from exc
    except KeyError as exc:
        raise HTTPException(422, f"未知配置项: {key}") from exc


@app.get("/api/settings/env")
async def api_settings_env():
    """启动配置面板数据：元数据 + 生效/暂存/来源 + 密钥状态 + 文件路径。

    插件侧 schema 本请求只构建一次（非密钥项与密钥项共用）——每项含逐字段来源
    判定与一次实例构造，构建遍数与面板打开成本成正比（设计要点见 architecture）。
    """
    staged = read_staged()
    dynamic = get_settings_schema()
    items = [
        {**meta, **_item_state(meta, staged.get(meta["key"]))}
        for meta in _all_schema(dynamic)
    ]
    # PLUGINS_REQUIRED 芯片选项：仅可选插件（核心插件恒装配，列入无意义）
    plugin_names = sorted(
        {p.get("name", "") for p in get_plugins_info() if not p.get("core")} - {""}
    )
    secrets = []
    for meta in _secret_schema(dynamic):
        name = meta["key"]
        # keyringConfigured = 钥匙串里有真值条目（独立徽章，语义冻结：前端靠它
        # 决定输入框隐藏与「替换/清除」入口，不随 configured 口径改动）。
        # 插件密钥项同样走活判定（见 _secret_effective）：未装配插件时
        # settings is None，硬编码或快照取值都会与真实链分叉。
        keyring_truthy = _keyring_secret_truthy(name)
        secrets.append(
            {
                "name": name,
                "label": meta.get("label", _SECRET_LABELS.get(name, name)),
                "plugin": meta.get("plugin", ""),
                # configured = 按当前解析链是否有有效值（活判定，见 _secret_effective）；
                # keyringConfigured 只表示钥匙串条目，避免环境变量/.env 配置被误报为钥匙串配置。
                "configured": _secret_effective(name, keyring_truthy=keyring_truthy),
                "keyringConfigured": keyring_truthy,
            }
        )
    plugins_data, plugins_expected_error = _plugin_toggle_data(staged)
    # 来源判定一次求值后复用：函数内读盘（source_of 走文件层），两次独立调用在
    # 文件刚好被外部改动的间隙会给出互相矛盾的来源与覆盖态。
    plugins_source = _plugins_source()
    return {
        "filePath": str(get_settings_file()),
        "items": items,
        "pluginOptions": plugin_names,
        "plugins": plugins_data,
        # PLUGINS 的来源 = 真正压住该键的那一层（env/dotenv 时前端禁用开关）。
        "pluginsSource": plugins_source,
        "pluginsOverridden": plugins_source in (SOURCE_ENV, SOURCE_DOTENV),
        # 预期名单不可用（坏 JSON/非字符串数组）时显式说明：开关与启停展示不能
        # 把未知当「全关」，也不能假装一切正常。
        "pluginsExpectedError": plugins_expected_error or "",
        "secrets": secrets,
    }


class EnvPutPayload(BaseModel):
    items: dict[str, str | None]


class SecretsPutPayload(BaseModel):
    name: str
    value: str = ""


@app.put("/api/settings/env")
async def api_settings_env_put(payload: EnvPutPayload):
    """批量暂存：{items: {KEY: value|null}}；null = 恢复默认（移除暂存）。

    校验与提交针对同一份候选字节：预检看到的就是落盘后的解析结果（值含
    ${VAR} 展开时，内存 dict 合并直接 json.loads 会看到展开前的原文）。锁内
    严格读取——暂存读不出来时拒绝写入，不把读失败当「无暂存」覆盖成空。
    """
    raw_items = payload.items
    if not isinstance(raw_items, dict):
        raise HTTPException(422, "请求体须为 {items: {KEY: value|null}}")
    # 白名单与单键校验共用**写入前**的一份 schema：构建是同步读盘而路由是
    # async def，逐键重建会让多键保存冻结事件循环、SSE 与全部 API 一起停摆
    # （成本论证见 architecture 的设计要点）。构建数由
    # tests/test_settings_env.py 的 SchemaRebuildCountTest 直接钉住（计数比
    # 时间断言确定）。
    pre_schema = _all_schema()
    updates: dict[str, str | None] = {}
    for key, raw in raw_items.items():
        # 键名先过白名单（null 删除路径同样受白名单约束）
        try:
            _schema_of(key, pre_schema)
        except KeyError as exc:
            raise HTTPException(422, f"未知配置项: {key}") from exc
        if raw is None:
            updates[key] = None
            continue
        if not isinstance(raw, str):
            raise HTTPException(422, f"{key}: 值须为字符串或 null")
        updates[key] = _normalize(key, raw, pre_schema)
    async with _write_lock:
        # 严格读取现有暂存并合成候选字节：读不出可靠旧值时拒绝写入——
        # 当空覆盖等于把整个暂存态静默抹掉。
        try:
            _merged, candidate = prepare_staged_write(updates)
        except StagedReadError as exc:
            raise HTTPException(409, f"暂存状态无法读取，拒绝写入: {exc}") from exc
        # 插件组合复检只看**候选字节回读**（dotenv 解析过一遍，含变量展开），
        # 与提交后的解析严格同源；单键 422 校验已在 _normalize 完成。
        if {"PLUGINS", "PLUGINS_REQUIRED"} & set(updates):
            try:
                _candidate_staged = read_staged_from_bytes(candidate)
            except (OSError, UnicodeDecodeError) as exc:
                raise HTTPException(409, f"候选暂存无法解析，拒绝写入: {exc}") from exc
            issues: list[dict[str, str]] = []
            try:
                desired = _desired_plugins(_candidate_staged)
            except _PluginsUnresolvable as exc:
                issues.append({"type": "unavailable", "key": exc.key, "detail": exc.detail})
                desired = []
            if "PLUGINS" in updates and issues == []:
                # 包装层约定：无管理器可校验时返回 None（测试/未装配），不是「无问题」
                # 的语义——这里统一成列表，避免 None + list
                issues = list(validate_plugin_selection(desired) or [])
            issues += _required_plugin_issues(_candidate_staged)
            if issues:
                raise HTTPException(status_code=409, detail={"issues": _dedupe_issues(issues)})
        try:
            commit_staged_write(candidate)
        except StagedCommitError as exc:
            # 写入/替换/删除失败必须显式失败：静默 ok:true 是丢写入。不用 409——
            # 那是「客户端可调整后重试」的语义，OS 层失败改多少次都一样；也不回显
            # 暂存值（可能含敏感内容）。日志脱敏：只记阶段与异常类别。
            logger.error(
                "暂存提交失败（%s）: %s", exc.stage, type(exc).__name__
            )
            raise HTTPException(
                500,
                {
                    "type": "staged_commit_failed",
                    "stage": exc.stage,
                    "message": f"暂存状态提交失败（{exc.stage}），请稍后重试",
                },
            ) from exc
    # 写后重读暂存（严格口径：吞 OSError 的展示读取会把「已写入但读不出」变成
    # ok:true + 空状态，比报错更坏），回传受影响键的最终态：前端据此做行级贴片，
    # 不必整面重拉（整面重载会丢其它行的未暂存编辑与「插件」面板的开关草稿）。
    # 重读失败时写入已发生：明确「已写入、状态未知」，不得谎报未保存。
    try:
        staged_now = read_staged_strict()
    except StagedReadError as exc:
        logger.error("暂存写入后重读失败: %s", type(exc).__name__)
        raise HTTPException(
            500,
            {
                "type": "staged_state_unknown",
                "message": "配置已写入，但当前状态未知（读取失败），请刷新确认",
            },
        ) from exc
    # expected_* 必须反映**提交后**的文件状态（pre_schema 里的下次启动值是写前的），
    # 所以回传阶段重建一份、全部受影响键共用——一次成功 PUT 的构建数因此恒为 2
    # （写前 + 写后两个时点），与提交键数无关。失败路径（409/422/500）走不到这里。
    post_schema = _all_schema()
    return {
        "ok": True,
        "filePath": str(get_settings_file()),
        "items": {key: _fresh_item_state(key, staged_now, post_schema) for key in updates},
    }


@app.post("/api/settings/secrets")
async def api_secrets_set(payload: SecretsPutPayload):
    """写入密钥到系统密钥环（当前核心/选中插件 schema 白名单）。"""
    name = payload.name
    value = payload.value
    if name not in {meta["key"] for meta in _secret_schema()}:
        raise HTTPException(422, f"未知密钥名: {name!r}")
    if not isinstance(value, str) or not value:
        raise HTTPException(422, "密钥值不能为空")
    try:
        if name == DB_KEYS_BASE:
            set_db_keys(value)
        else:
            set_secret(name, value)
    except Exception as exc:  # SecretsStoreError 等统一转可读错误
        raise HTTPException(500, f"密钥环写入失败: {exc}") from exc
    # 回传活判定而非硬编码 True：`set_db_keys` 不校验 JSON，写入非 JSON 值后
    # 解析链拿不到可用密钥（get_db_keys 全段解析失败返回 None），硬编码会让
    # POST 说「已配置」而紧接的 GET 说「未配置」自相矛盾（回归见
    # tests/test_settings_env.py 的 DB_KEYS 坏 JSON 写入用例）。
    keyring_truthy = _keyring_secret_truthy(name)
    return {
        "ok": True,
        "name": name,
        "configured": _secret_effective(name, keyring_truthy=keyring_truthy),
        "keyringConfigured": keyring_truthy,
    }


@app.delete("/api/settings/secrets/{name}")
async def api_secrets_delete(name: str):
    """清除密钥（幂等：未配置也视为成功）。"""
    if name not in {meta["key"] for meta in _secret_schema()}:
        raise HTTPException(422, f"未知密钥名: {name!r}")
    if name == DB_KEYS_BASE:
        delete_db_keys()
    else:
        delete_secret(name)
    # configured 是否仍为真取决于该密钥是否另有环境变量/.env 等低层配置——只有
    # 服务端能判定，回传供前端行级贴片（keyringConfigured 删除后恒为 False）；
    # 判定口径见 _secret_effective。注意：**上方的白名单构建仍必须保留**，它校验
    # 密钥名合法性，与 configured 判定无关。
    return {
        "ok": True,
        "name": name,
        "configured": _secret_effective(name, keyring_truthy=False),
        "keyringConfigured": False,
    }


app.include_router(router)
