"""类别管理路由（server 子包）：categories CRUD + 级联删除 + 事件通知。

从原 server.py 拆出（P5 子包化），导入即注册路由。
"""

import re

import aiosqlite
from fastapi import HTTPException

from briefdesk.db import (
    delete_category,
    get_categories,
    insert_category,
    storage_lock,
    toggle_category,
    update_category,
)
from briefdesk.events import EVENT_ITEMS_DELETED, event_bus
from briefdesk.realtime import publish_items_updated
from briefdesk.server.app import app

_NAME_MAX = 20
# F5: 50 字上限与默认类别 prompt（均 >50 字，含"①…②…"细则）矛盾，导致现有类别
# 无法经 UI 编辑（任何整句重写要么超限被拒、要么丢失细则）。放宽为 200。
_PROMPT_MAX = 200
_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def _validate_category_fields(body: dict, *, name_required: bool = False) -> str | None:
    """校验类别字段，非法时返回错误消息（None 表示通过）。

    名称 strip 后非空、≤20 字；提示词 ≤200 字；颜色匹配 #RRGGBB。
    名称上限与前端 maxlength 一致；提示词 200 为后端放宽（前端仍 50，见 ui/index.html）。
    """
    name_raw = body.get("name")
    prompt = body.get("prompt")
    color = body.get("color")
    if name_required or name_raw is not None:
        name = (name_raw or "").strip() if isinstance(name_raw, str) else ""
        if not name:
            return "name is required"
        if len(name) > _NAME_MAX:
            return f"name must be <= {_NAME_MAX} chars"
    else:
        name = ""
    if prompt is not None and (
        not isinstance(prompt, str) or len(prompt) > _PROMPT_MAX
    ):
        return f"prompt must be a string <= {_PROMPT_MAX} chars"
    if color is not None and (not isinstance(color, str) or not _COLOR_RE.match(color)):
        return "color must be a hex color like #RRGGBB"
    return None


def _parse_flag(value: object, *, field: str, default: bool | None = None) -> bool:
    """严格解析布尔开关：仅接受 bool 或 0/1。

    字符串（含 "false"）一律拒绝，避免 bool("false") 恒真的数据删除风险。
    """
    if value is None and default is not None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    raise HTTPException(400, f"{field} must be a boolean or 0/1")


@app.get("/api/categories")
async def api_categories():
    cats = await get_categories()
    return {"categories": cats, "count": len(cats)}


@app.post("/api/categories")
async def api_create_category(body: dict):
    err = _validate_category_fields(body, name_required=True)
    if err:
        raise HTTPException(400, err)
    name = body["name"].strip()
    prompt = body.get("prompt") or ""
    color = body.get("color") or "#6B7280"
    enabled_val = (
        1 if _parse_flag(body.get("enabled"), field="enabled", default=True) else 0
    )
    try:
        async with storage_lock:
            row = await insert_category(name, prompt, color, enabled=enabled_val)
    except aiosqlite.IntegrityError:
        raise HTTPException(409, f"类别已存在: {name}")
    await publish_items_updated({"categoriesChanged": True})
    return {"success": True, "category": row}


@app.post("/api/categories/{cat_id}/update")
async def api_update_category(cat_id: int, body: dict):
    if not any(k in body for k in ("name", "prompt", "color")):
        raise HTTPException(400, "at least one of name/prompt/color required")
    err = _validate_category_fields(body)
    if err:
        raise HTTPException(400, err)
    name = body["name"].strip() if body.get("name") is not None else None
    prompt = body.get("prompt")
    color = body.get("color")
    try:
        async with storage_lock:
            row = await update_category(cat_id, name=name, prompt=prompt, color=color)
    except aiosqlite.IntegrityError:
        raise HTTPException(409, "类别已存在")
    if row is None:
        raise HTTPException(404, "Category not found")
    await publish_items_updated({"categoriesChanged": True})
    return {"success": True, "category": row}


@app.post("/api/categories/{cat_id}/toggle")
async def api_toggle_category(cat_id: int):
    async with storage_lock:
        row = await toggle_category(cat_id)
    if row is None:
        raise HTTPException(404, "Category not found")
    await publish_items_updated({"categoriesChanged": True})
    return {"success": True, "category": row}


@app.post("/api/categories/{cat_id}/delete")
async def api_delete_category(cat_id: int, body: dict):
    """删除类别。body.purgeItems=true 时级联删除该类别全部卡片。"""
    purge_items = _parse_flag(body.get("purgeItems"), field="purgeItems", default=False)
    # 整端点统一持存储锁：级联删除与去重缓存清理原子完成；非 purge 分支
    # 同样锁内执行，与 pipeline 写路径串行化（单连接隐式事务防交叉提交）
    async with storage_lock:
        row, deleted_ids = await delete_category(cat_id, purge_items=purge_items)
        if row is not None and purge_items:
            # 发布 items_deleted：去重插件订阅后同步清理内存缓存，
            # 避免相似新消息被误判重复而永久不显示
            await event_bus.publish(EVENT_ITEMS_DELETED, deleted_ids)
    if row is None:
        raise HTTPException(404, "Category not found")
    await publish_items_updated({"categoriesChanged": True})
    return {
        "success": True,
        "category": row,
        "deletedItems": len(deleted_ids),
    }
