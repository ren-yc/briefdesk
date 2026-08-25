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
from typing import Any, cast

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ValidationError

from briefdesk.config import Settings, config
from briefdesk.secrets_store import SECRET_NAMES, delete_secret, get_secret, set_secret
from briefdesk.server.app import app
from briefdesk.server.web_plugins import get_plugins_info
from briefdesk.settings_env import (
    get_settings_file,
    read_staged,
    source_of,
    write_staged,
)

router = APIRouter()

# 单进程写锁 + 原子写：并发 PUT 串行化
_write_lock = asyncio.Lock()

# 可编辑白名单（键序即 UI 展示顺序）；current 来自 config，该表不含密钥
ENV_SCHEMA: list[dict] = [
    {
        "key": "LOG_LEVEL",
        "type": "select",
        "options": ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        "label": "日志级别",
        "hint": "DEBUG 开启逐条细节；INFO 只保留阶段与汇总",
    },
    {
        "key": "SERVER_PORT",
        "type": "number",
        "min": 1,
        "max": 65535,
        "label": "服务端口",
        "warn": "重启后访问地址将变为新端口",
    },
    {
        "key": "BACKFILL_HOURS",
        "type": "number",
        "min": -1,
        "label": "历史回填窗口（小时）",
        "hint": "-1 = 拉取全部历史（耗时且 AI 调用激增，慎用）",
    },
    {
        "key": "POLL_OVERLAP_SECONDS",
        "type": "number",
        "min": 0,
        "label": "轮询重叠窗口（秒）",
        "hint": "吸收增量轮询边界偏差；重叠部分去重无 AI 开销",
    },
    {
        "key": "IGNORED_EXPIRY_HOURS",
        "type": "number",
        "min": 0,
        "label": "已忽略条目过期（小时）",
        "hint": "0 = 禁用清理",
    },
    {"key": "IGNORE_SELF", "type": "boolean", "label": "过滤自己发送的消息"},
    {
        "key": "REALTIME_BATCH_MAX_COUNT",
        "type": "number",
        "min": 1,
        "label": "实时批缓冲条数",
        "hint": "攒够条数触发 AI 处理；调大可省 token、实时性略降",
    },
    {
        "key": "REALTIME_BATCH_TIMEOUT_MS",
        "type": "number",
        "min": 1,
        "label": "实时批缓冲超时（毫秒）",
    },
    {
        "key": "BACKFILL_BATCH_MAX_COUNT",
        "type": "number",
        "min": 1,
        "label": "回填批量（条）",
        "hint": "回填时单次 AI 分类合并条数；调大省 token",
    },
    {
        "key": "AI_MAX_CONCURRENCY",
        "type": "number",
        "min": 0,
        "label": "AI 请求最大并发",
        "hint": "0 = 不限制；本地模型（如 Ollama）建议设为 1",
    },
    {
        "key": "MAX_CLASSIFY_TOKENS",
        "type": "number",
        "min": 1,
        "label": "单次分类最大输出 token",
    },
    {
        "key": "AI_DISABLE_THINKING",
        "type": "boolean",
        "label": "禁用 AI 思考模式",
        "hint": "Qwen3/Qwen3.5 等模型的 thinking 关闭",
    },
    {
        "key": "DEDUP_SIMILARITY_THRESHOLD",
        "type": "number",
        "min": 0,
        "max": 1,
        "step": 0.01,
        "label": "去重字符重叠阈值",
    },
    {"key": "EMBED_BATCH_SIZE", "type": "number", "min": 1, "label": "嵌入批量大小"},
    {
        "key": "DEDUP_EMBED_THRESHOLD",
        "type": "number",
        "min": 0,
        "max": 1,
        "step": 0.01,
        "label": "嵌入余弦预过滤阈值",
    },
    {"key": "DEDUP_EMBED_TOP_K", "type": "number", "min": 1, "label": "嵌入候选条数"},
    {
        "key": "DEDUP_STRONG_THRESHOLD",
        "type": "number",
        "min": 0,
        "max": 1,
        "step": 0.01,
        "label": "同文本短路阈值",
    },
    {
        "key": "DEDUP_EMBED_FALLBACK_THRESHOLD",
        "type": "number",
        "min": 0,
        "max": 1,
        "step": 0.01,
        "label": "低置信复核阈值",
    },
    {
        "key": "MERGE_WINDOW_MINUTES",
        "type": "number",
        "min": 0,
        "label": "同话题合并窗口（分钟）",
        "hint": "0 = 禁用合并",
    },
    {"key": "MERGE_MAX_CANDIDATES", "type": "number", "min": 1, "label": "合并候选上限"},
    {
        "key": "DB_PATH",
        "type": "text",
        "label": "数据库文件路径",
        "warn": "重启后数据库将使用新路径；旧库不会自动迁移，请备份后手动移动文件",
    },
    {
        "key": "PLUGINS",
        "type": "multi",
        "label": "启用的插件",
        "hint": "\"*\" = 全部发现插件；亦可用显式列表",
    },
    {
        "key": "PLUGINS_DISABLED",
        "type": "multi",
        "label": "禁用的插件",
        "hint": "优先于 PLUGINS",
    },
]

_SECRET_LABELS = {
    "AI_API_KEY": "AI API Key",
    "EMBED_API_KEY": "嵌入 API Key",
    "WEFLOW_API_TOKEN": "WeFlow 访问令牌",
    "QQFLOW_API_TOKEN": "qqflow 访问令牌",
    "QQFLOW_KEY": "qqflow 引导密钥",
}

# 环境变量别名 → Settings 字段名（用于 current 取值与校验回读）
_FIELD_BY_ALIAS = {
    f.alias: name for name, f in Settings.model_fields.items() if f.alias
}


def _schema_of(key: str) -> dict:
    for meta in ENV_SCHEMA:
        if meta["key"] == key:
            return meta
    raise KeyError(key)


def _normalize(key: str, raw: str) -> str:
    """白名单 + 类型/约束校验，返回规范化的暂存字符串；非法抛 HTTPException。"""
    try:
        meta = _schema_of(key)
        field_name = _FIELD_BY_ALIAS[key]
        if meta["type"] == "multi":
            parsed = json.loads(raw)
            if not isinstance(parsed, list) or not all(
                isinstance(x, str) for x in parsed
            ):
                raise HTTPException(422, f"{key}: 须为 JSON 字符串数组")
            Settings(**cast(Any, {key: parsed}))
            return json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))
        probe = Settings(**cast(Any, {key: raw}))
        if meta["type"] == "boolean":
            return "true" if getattr(probe, field_name) else "false"
        if meta["type"] == "number":
            return str(getattr(probe, field_name))
        if meta["type"] == "select" and raw not in meta["options"]:
            raise HTTPException(422, f"{key}: 非法选项 {raw!r}")
        return raw
    except HTTPException:
        raise
    except (ValidationError, ValueError, json.JSONDecodeError) as exc:
        msg = exc.errors()[0]["msg"] if isinstance(exc, ValidationError) else str(exc)
        raise HTTPException(422, f"{key}: 校验失败（{msg}）") from exc
    except KeyError as exc:
        raise HTTPException(422, f"未知配置项: {key}") from exc


@app.get("/api/settings/env")
async def api_settings_env():
    """启动配置面板数据：元数据 + 生效/暂存/来源 + 密钥状态 + 文件路径。"""
    staged = read_staged()
    items = []
    for meta in ENV_SCHEMA:
        key = meta["key"]
        field_name = _FIELD_BY_ALIAS[key]
        current_val = getattr(config, field_name)
        if isinstance(current_val, bool):
            current = bool(current_val)
        elif isinstance(current_val, (int, float)):
            current = current_val
        elif isinstance(current_val, list):
            current = list(current_val)
        else:
            current = str(current_val)
        raw_staged = staged.get(key)
        staged_val: object = None
        if raw_staged is not None:
            if meta["type"] == "multi":
                try:
                    staged_val = json.loads(raw_staged)
                except json.JSONDecodeError:
                    staged_val = raw_staged
            else:
                staged_val = raw_staged
        items.append(
            {
                **meta,
                "current": current,
                "staged": staged_val,
                "source": source_of(key),
            }
        )
    plugin_names = sorted({p.get("name", "") for p in get_plugins_info()} - {""})
    return {
        "filePath": str(get_settings_file()),
        "items": items,
        "pluginOptions": plugin_names,
        "secrets": [
            {
                "name": name,
                "label": _SECRET_LABELS.get(name, name),
                "configured": get_secret(name) is not None,
            }
            for name in SECRET_NAMES
        ],
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
    """写入密钥到系统密钥环（白名单键；value 非空）。"""
    name = payload.name
    value = payload.value
    if name not in SECRET_NAMES:
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
    if name not in SECRET_NAMES:
        raise HTTPException(422, f"未知密钥名: {name!r}")
    delete_secret(name)
    return {"ok": True, "name": name}


app.include_router(router)