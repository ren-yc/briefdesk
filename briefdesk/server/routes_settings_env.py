"""启动配置路由（server 子包）—「设置 → 启动配置」面板后端。

- `GET /api/settings/env`     元数据 + 生效值/暂存值/来源 + 密钥状态 + 文件路径
- `PUT /api/settings/env`     批量暂存（白名单 + 类型/约束校验 + 原子写）
- `POST /api/settings/secrets`  写入密钥到系统密钥环（keyring）
- `DELETE /api/settings/secrets/{name}`  清除密钥（幂等）

设计约束：密钥值**永不下发**（GET 只含 configured 布尔）；暂存文件只存
非密钥键（存储层见 briefdesk/settings_env.py）。
"""

import asyncio
import json
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from briefdesk.config import Settings, config
from briefdesk.secrets_store import SECRET_NAMES, delete_secret, get_secret, set_secret
from briefdesk.server.app import app
from briefdesk.server.web_plugins import (
    get_plugins_info,
    get_settings_schema,
    has_settings_schema_callback,
)
from briefdesk.settings_env import (
    get_settings_file,
    read_staged,
    source_of,
    write_staged,
)
from briefdesk.settings_schema import (
    build_settings_schema,
    normalize_setting,
    staged_value,
)

router = APIRouter()

# 单进程写锁 + 原子写：并发 PUT 串行化
_write_lock = asyncio.Lock()

# 核心设置的展示覆盖层；字段本身从 Settings.model_fields 自动发现。
_CORE_UI: dict[str, dict[str, Any]] = {
    "PLUGINS": {"label": "启用的插件", "hint": "\"*\" = 全部发现插件；亦可用显式列表"},
    "PLUGINS_DISABLED": {"label": "禁用的插件", "hint": "优先于 PLUGINS"},
    "PLUGINS_REQUIRED": {"label": "必选插件", "hint": "这些插件装配失败时将阻止应用启动"},
    "PLUGIN_PATH": {"label": "开发期插件目录", "hint": "留空表示不扫描外部插件"},
    "AI_API_KEY": {"label": "AI API Key"},
    "AI_API_BASE": {"label": "AI API 地址"},
    "AI_MODEL": {"label": "AI 模型"},
    "AI_MAX_CONCURRENCY": {"label": "AI 请求最大并发", "hint": "0 = 不限制；本地模型建议设为 1"},
    "AI_DISABLE_THINKING": {"label": "禁用 AI 思考模式", "hint": "Qwen3/Qwen3.5 等模型的 thinking 关闭"},
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
    "DB_PATH": {"label": "数据库文件路径", "warn": "重启后数据库将使用新路径；旧库不会自动迁移，请备份后手动移动文件"},
    "SERVER_PORT": {"label": "服务端口", "warn": "重启后访问地址将变为新端口"},
    "LOG_LEVEL": {"label": "日志级别", "hint": "DEBUG 开启逐条细节；INFO 只保留阶段与汇总"},
}

_CORE_SCHEMA = build_settings_schema(
    Settings,
    config,
    labels={key: value["label"] for key, value in _CORE_UI.items() if "label" in value},
    hints={key: value["hint"] for key, value in _CORE_UI.items() if "hint" in value},
    warnings={key: value["warn"] for key, value in _CORE_UI.items() if "warn" in value},
    options={"LOG_LEVEL": ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]},
)
for _item in _CORE_SCHEMA:
    _item.update(_CORE_UI.get(_item["key"], {}))

ENV_SCHEMA: list[dict] = [item for item in _CORE_SCHEMA if not item.get("secret")]
_CORE_SECRET_SCHEMA = [item for item in _CORE_SCHEMA if item.get("secret")]

_SECRET_LABELS = {
    "AI_API_KEY": "AI API Key",
    "EMBED_API_KEY": "嵌入 API Key",
    "WEFLOW_API_TOKEN": "WeFlow 访问令牌",
    "QQFLOW_API_TOKEN": "qqflow 访问令牌",
    "QQFLOW_KEY": "qqflow 引导密钥",
}


def _all_schema() -> list[dict[str, Any]]:
    """核心字段加当前选中插件字段，按 key 去重。"""
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in [*ENV_SCHEMA, *get_settings_schema()]:
        key = item.get("key")
        if not isinstance(key, str) or item.get("secret") or key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _secret_schema() -> list[dict[str, Any]]:
    """核心密钥加当前选中插件密钥；无 manager 时保留旧版兼容列表。"""
    dynamic = get_settings_schema()
    result = [*(_CORE_SECRET_SCHEMA)]
    if dynamic or has_settings_schema_callback():
        result.extend(item for item in dynamic if item.get("secret"))
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


def _schema_of(key: str) -> dict:
    for meta in _all_schema():
        if meta["key"] == key:
            return meta
    raise KeyError(key)


def _normalize(key: str, raw: str) -> str:
    """动态 schema + 类型/约束校验，返回规范化的暂存字符串。"""
    try:
        meta = _schema_of(key)
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
    """启动配置面板数据：元数据 + 生效/暂存/来源 + 密钥状态 + 文件路径。"""
    staged = read_staged()
    items = []
    for meta in _all_schema():
        key = meta["key"]
        raw_staged = staged.get(key)
        staged_val = (
            staged_value(raw_staged, meta["type"])
            if raw_staged is not None
            else None
        )
        items.append(
            {
                **meta,
                "staged": staged_val,
                "source": source_of(key),
            }
        )
    plugin_names = sorted({p.get("name", "") for p in get_plugins_info()} - {""})
    secrets = []
    for meta in _secret_schema():
        name = meta["key"]
        keyring_configured = get_secret(name) is not None
        secrets.append(
            {
                "name": name,
                "label": meta.get("label", _SECRET_LABELS.get(name, name)),
                "plugin": meta.get("plugin", ""),
                # configured = 当前解析链是否有有效值；keyringConfigured 只表示
                # 是否存在钥匙串条目，避免环境变量/.env 配置被误报为钥匙串配置。
                "configured": bool(meta.get("configured")) or keyring_configured,
                "keyringConfigured": keyring_configured,
            }
        )
    return {
        "filePath": str(get_settings_file()),
        "items": items,
        "pluginOptions": plugin_names,
        "secrets": secrets,
    }


class EnvPutPayload(BaseModel):
    items: dict[str, str | None]


class SecretsPutPayload(BaseModel):
    name: str
    value: str = ""


@app.put("/api/settings/env")
async def api_settings_env_put(payload: EnvPutPayload):
    """批量暂存：{items: {KEY: value|null}}；null = 恢复默认（移除暂存）。"""
    raw_items = payload.items
    if not isinstance(raw_items, dict):
        raise HTTPException(422, "请求体须为 {items: {KEY: value|null}}")
    updates: dict[str, str | None] = {}
    for key, raw in raw_items.items():
        # 键名先过白名单（null 删除路径同样受白名单约束）
        try:
            _schema_of(key)
        except KeyError as exc:
            raise HTTPException(422, f"未知配置项: {key}") from exc
        if raw is None:
            updates[key] = None
            continue
        if not isinstance(raw, str):
            raise HTTPException(422, f"{key}: 值须为字符串或 null")
        updates[key] = _normalize(key, raw)
    async with _write_lock:
        write_staged(updates)
    return {"ok": True, "filePath": str(get_settings_file())}


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
        set_secret(name, value)
    except Exception as exc:  # SecretsStoreError 等统一转可读错误
        raise HTTPException(500, f"密钥环写入失败: {exc}") from exc
    return {"ok": True, "name": name}


@app.delete("/api/settings/secrets/{name}")
async def api_secrets_delete(name: str):
    """清除密钥（幂等：未配置也视为成功）。"""
    if name not in {meta["key"] for meta in _secret_schema()}:
        raise HTTPException(422, f"未知密钥名: {name!r}")
    delete_secret(name)
    return {"ok": True, "name": name}


app.include_router(router)
