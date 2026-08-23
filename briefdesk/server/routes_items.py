"""核心数据路由（server 子包）：items / verify / sessions / sync / context /
status / stream 等。从原 server.py 拆出（P5 子包化），导入即注册路由。
"""

import asyncio
import csv
import io
import json
import os
import re
import tempfile
import time
from typing import Annotated

from fastapi import File, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse, Response, StreamingResponse

import briefdesk.server.callbacks as _callbacks
from briefdesk.config import config
from briefdesk.db import (
    backup_db_to,
    category_exists,
    delete_items,
    get_all_category_count,
    get_all_sessions,
    get_category_counts,
    get_context_messages,
    get_disabled_category_names,
    get_enabled_category_colors,
    get_ignored_count,
    get_items_by_subject,
    get_items_page,
    get_memo_count,
    get_recat_samples,
    get_subject_count,
    storage_lock,
    toggle_session,
    update_item_category,
    update_item_verify,
    update_items_verify,
    validate_restore_file,
)
from briefdesk.events import EVENT_ITEMS_DELETED, event_bus
from briefdesk.realtime import get_shutdown_event, subscribe, unsubscribe
from briefdesk.server.app import app
from briefdesk.status import get_listener, get_status_info
from briefdesk.sync import trigger_sync
from briefdesk.types import ContextMsg

_FILTER_NOW_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$"
)


@app.get("/api/subject/items")
async def api_subject_items(
    subject: str = Query(..., min_length=1),
    limit: int = Query(100, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """主体时间线：该主体的全部历史卡片（跨类别、排除已忽略）。"""
    subject = subject.strip()
    if not subject:
        raise HTTPException(400, "subject is required")
    raw = await get_items_by_subject(subject, limit + 1, offset)
    items = raw[:limit]
    return {
        "items": items,
        "count": await get_subject_count(subject),
        "hasMore": len(raw) > limit,
    }


@app.get("/api/items")
async def api_items(
    category: str = Query(None),
    verified: str = Query("unverified"),
    q: str = Query(None),
    source_group: str = Query(None, alias="sourceGroup"),
    min_msg_time: int = Query(None, alias="minMsgTime", ge=0),
    hide_expired: bool = Query(False, alias="hideExpired"),
    filter_now: str = Query(None, alias="filterNow"),
    limit: int = Query(100, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    now_local = None
    if hide_expired:
        now_local = filter_now or time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        try:
            if _FILTER_NOW_RE.fullmatch(now_local) is None:
                raise ValueError
            time.strptime(now_local, "%Y-%m-%d %H:%M:%S")
        except ValueError as exc:
            raise HTTPException(400, "filterNow must be YYYY-MM-DD HH:MM:SS") from exc
    page = await get_items_page(
        category=category if category else None,
        verified=verified,
        q=q or None,
        source_group=source_group or None,
        min_msg_time=min_msg_time,
        hide_expired=hide_expired,
        now_local=now_local if hide_expired else None,
        limit=limit,
        offset=offset,
    )
    cat_counts = await get_category_counts()
    all_count = await get_all_category_count()
    ignored_count = await get_ignored_count()
    memo_count = await get_memo_count()

    categories = [{"key": "全部", "count": all_count}] + cat_counts
    return {
        "items": page["items"],
        "categories": categories,
        "allCategories": await get_enabled_category_colors(),
        "disabledCategories": await get_disabled_category_names(),
        "ignoredCount": ignored_count,
        "memoCount": memo_count,
        "totalCount": page["total_count"],
        "groupCount": page["group_count"],
        "sourceGroups": page["source_groups"],
        "hasMore": page["has_more"],
        "nextOffset": page["next_offset"],
        "filterNow": page["filter_now"],
        "status": get_status_info(),
    }


# ── 导出（数据复用 / 微调样本）──

_ITEM_EXPORT_COLS = [
    "id", "category", "title", "key_info", "sender_name",
    "source_group", "subject", "source", "source_msg_id", "session_id",
    "msg_time", "start", "end", "extra_times", "article_url", "source_quote", "is_verified",
]


def _export_attachment(content: str, media_type: str, filename: str) -> Response:
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/export/items")
async def api_export_items(
    category: str = Query(None),
    verified: str = Query("unverified"),
    q: str = Query(None),
    source_group: str = Query(None, alias="sourceGroup"),
    min_msg_time: int = Query(None, alias="minMsgTime", ge=0),
    hide_expired: bool = Query(False, alias="hideExpired"),
    filter_now: str = Query(None, alias="filterNow"),
):
    """导出当前筛选条件下的卡片（CSV，翻页全量，参数与 /api/items 一致）。"""
    now_local = None
    if hide_expired:
        now_local = filter_now or time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        try:
            if _FILTER_NOW_RE.fullmatch(now_local) is None:
                raise ValueError
            time.strptime(now_local, "%Y-%m-%d %H:%M:%S")
        except ValueError as exc:
            raise HTTPException(400, "filterNow must be YYYY-MM-DD HH:MM:SS") from exc
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(_ITEM_EXPORT_COLS)
    offset = 0
    for _ in range(100000):  # 守卫：最多 2000 万条
        page = await get_items_page(
            category=category or None,
            verified=verified,
            q=q or None,
            source_group=source_group or None,
            min_msg_time=min_msg_time,
            hide_expired=hide_expired,
            now_local=now_local if hide_expired else None,
            limit=200,
            offset=offset,
        )
        for r in page["items"]:
            w.writerow(
                r.get(c, "") if not isinstance(r.get(c), (dict, list))
                else json.dumps(r.get(c), ensure_ascii=False)
                for c in _ITEM_EXPORT_COLS
            )
        if not page["has_more"] or not page["items"]:
            break
        offset = page["next_offset"]
        if offset <= 0:
            break  # 防死循环兜底
    return _export_attachment(
        out.getvalue(),
        "text/csv; charset=utf-8",
        f"briefdesk-items-{time.strftime('%Y%m%d-%H%M%S')}.csv",
    )


@app.get("/api/export/recat-samples")
async def api_export_recat_samples(fmt: str = Query("jsonl", alias="format")):
    """导出人工分类修正样本（category_before != category_after，供模型微调）。

    format=jsonl（默认）：每行 JSON；format=csv：便于人工复核。
    内容为已脱敏的 source_quote，不包含其它隐私字段。
    """
    samples = await get_recat_samples()
    if fmt == "csv":
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["item_id", "source", "source_msg_id", "category_before", "category_after", "content", "created_at"])
        for s in samples:
            w.writerow([s["item_id"], s["source"], s["source_msg_id"], s["category_before"], s["category_after"], s["content"], s["created_at"]])
        return _export_attachment(
            out.getvalue(),
            "text/csv; charset=utf-8",
            f"briefdesk-recat-samples-{time.strftime('%Y%m%d-%H%M%S')}.csv",
        )
    lines = "\n".join(json.dumps(s, ensure_ascii=False) for s in samples)
    return _export_attachment(
        lines,
        "application/x-ndjson; charset=utf-8",
        f"briefdesk-recat-samples-{time.strftime('%Y%m%d-%H%M%S')}.jsonl",
    )


# ── 备份 / 恢复（本地单文件数据的安全网）──

_RESTORE_MAX_BYTES = 1 << 30  # 1GB 上限


def _read_file_bytes(path: str) -> bytes:
    """同步读文件字节（供 asyncio.to_thread 调用，避免阻塞事件循环）。"""
    with open(path, "rb") as f:
        return f.read()


def _write_file_bytes(path: str, chunks: list[bytes]) -> None:
    """同步写文件字节（供 asyncio.to_thread 调用，避免阻塞事件循环）。"""
    with open(path, "wb") as f:
        f.writelines(chunks)


@app.get("/api/backup")
async def api_backup():
    """下载数据库在线备份（SQLite backup API，WAL 安全，可运行中执行）。"""
    fd, tmp = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    try:
        await backup_db_to(tmp)
        content = await asyncio.to_thread(_read_file_bytes, tmp)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    filename = f"briefdesk-backup-{time.strftime('%Y%m%d-%H%M%S')}.sqlite"
    return Response(
        content=content,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/api/restore")
async def api_restore(file: Annotated[UploadFile, File()]):
    """上传备份并校验（完整性 + schema）；通过后暂存为
    {db_path}.restore-pending，**重启应用后生效**（替换当前数据）。

    采用"重启生效"而非运行中热替换：避免 dedup 内存缓存 / processed
    状态与新文件不一致，实现与安全都最简。
    """
    fd, tmp = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    total = 0
    chunks: list[bytes] = []
    try:
        while chunk := await file.read(1 << 20):
            total += len(chunk)
            if total > _RESTORE_MAX_BYTES:
                raise HTTPException(400, "backup file too large (max 1GB)")
            chunks.append(chunk)
        await asyncio.to_thread(_write_file_bytes, tmp, chunks)
        err = await validate_restore_file(tmp)
        if err:
            raise HTTPException(400, err)
        os.replace(tmp, f"{config.db_path}.restore-pending")
        tmp = ""  # 已改名，不再清理
    finally:
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass
    return {
        "success": True,
        "message": "校验通过，已暂存；重启应用后生效（将替换当前数据）",
    }


@app.post("/api/items/{item_id}/verify")
async def api_verify(item_id: str, body: dict):
    verified = body.get("verified")
    if verified not in (0, 1, -1):
        raise HTTPException(400, "verified must be 0, 1, or -1")
    if not await update_item_verify(item_id, verified):
        raise HTTPException(404, "Item not found")
    cat_counts = await get_category_counts()
    all_count = await get_all_category_count()
    ignored_count = await get_ignored_count()
    memo_count = await get_memo_count()
    return {
        "success": True,
        "categories": [{"key": "全部", "count": all_count}] + cat_counts,
        "ignoredCount": ignored_count,
        "memoCount": memo_count,
    }


@app.post("/api/items/{item_id}/recategorize")
async def api_recategorize(item_id: str, body: dict):
    """手动修正卡片分类。只允许改到启用类别（避免改入停用类别后卡片消失）。"""
    category = (body.get("category") or "").strip()
    if not category:
        raise HTTPException(400, "category is required")
    if not await category_exists(category):
        raise HTTPException(400, f"未知或未启用的类别: {category}")
    row = await update_item_category(item_id, category)
    if row is None:
        raise HTTPException(404, "Item not found")
    return {"success": True, "item": row}


@app.post("/api/items/batch")
async def api_items_batch(body: dict):
    """批量操作卡片：memo / ignore / unverify / delete。

    delete 时发布 items_deleted 事件（去重插件订阅后同步清理内存缓存），
    否则相似新消息会被误判重复而永久不显示。
    """
    ids = body.get("ids")
    action = body.get("action")
    if not isinstance(ids, list) or not ids or not all(isinstance(i, str) for i in ids):
        raise HTTPException(400, "ids must be a non-empty list of strings")
    if len(ids) > 500:
        raise HTTPException(400, "too many ids (max 500)")
    if action not in ("memo", "ignore", "unverify", "delete"):
        raise HTTPException(400, "action must be memo/ignore/unverify/delete")
    if action == "delete":
        # DB 删除与去重缓存清理必须在 pipeline 的存储锁内原子完成，
        # 否则 check_dedup 可能命中已删条目并永久跳过相似新消息
        async with storage_lock:
            affected = await delete_items(ids)
            # 发布 items_deleted：去重插件订阅后同步清理内存缓存
            # （同步处理器，发布期间仍持有存储锁，保持原子）
            await event_bus.publish(EVENT_ITEMS_DELETED, ids)
    else:
        verified = {"memo": 1, "ignore": -1, "unverify": 0}[action]
        affected = await update_items_verify(ids, verified)
    return {"success": True, "affected": affected}


@app.get("/api/sessions")
async def api_sessions():
    sessions = await get_all_sessions()
    return {
        "sessions": sessions,
        "count": len(sessions),
        "backfillHours": config.backfill_hours,
    }


@app.post("/api/sessions/{source}/{session_id}/toggle")
async def api_toggle_session(source: str, session_id: str):
    updated = await toggle_session(source, session_id)
    if updated is None:
        raise HTTPException(404, "Session not found")
    listener = get_listener(source)
    if listener:
        listener.invalidate_session_cache()
    return {"success": True, "session": updated}


@app.post("/api/sessions/refresh")
async def api_refresh_sessions():
    """刷新群聊列表：重新从消息源拉取会话并写库。"""
    cb = _callbacks._refresh_sessions_callback
    if cb is None:
        raise HTTPException(503, "Refresh sessions callback not registered")
    await cb()
    sessions = await get_all_sessions()
    return {
        "sessions": sessions,
        "count": len(sessions),
        "backfillHours": config.backfill_hours,
    }


@app.post("/api/sync")
async def api_sync():
    if trigger_sync(reason="api"):
        return {"success": True, "message": "Sync started"}
    return JSONResponse(
        {"success": False, "message": "Sync already in progress"}, status_code=409
    )


@app.get("/api/context")
async def api_context(
    source: str = Query(...),
    session_id: str = Query(...),
    t: int = Query(0),
    msg_id: str = Query("", alias="msgId"),
):
    msgs: list[ContextMsg] = await get_context_messages(source, session_id, t, msg_id)

    return {"messages": msgs}


@app.get("/api/status")
async def api_status():
    return get_status_info()


@app.get("/api/stream")
async def api_stream():
    """前端实时更新通道：每次有新入库卡片时推送 items_updated 事件。

    同时监听全局关闭事件：服务退出时（signal_shutdown）所有流主动结束，
    否则这些常驻 ASGI 任务会让 uvicorn 优雅退出无限等待。
    """
    shutdown_event = get_shutdown_event()

    async def event_gen():
        queue = await subscribe()
        try:
            # 连接就绪事件，便于前端确认 SSE 已建立
            yield "event: ready\ndata: {}\n\n"
            while True:
                get_task = asyncio.ensure_future(queue.get())
                wait_task = asyncio.ensure_future(shutdown_event.wait())
                try:
                    done, _pending = await asyncio.wait(
                        {get_task, wait_task},
                        return_when=asyncio.FIRST_COMPLETED,
                        timeout=20,
                    )
                finally:
                    # 无论正常结束还是生成器被取消（客户端断开），
                    # 都必须取消未完成的子任务 —— 否则它们会挂到进程退出，
                    # 触发 "Task was destroyed but it is pending!" 警告
                    for t in (get_task, wait_task):
                        if not t.done():
                            t.cancel()
                if shutdown_event.is_set():
                    break
                if get_task in done and not get_task.cancelled():
                    yield f"event: items_updated\ndata: {get_task.result()}\n\n"
                else:
                    # 心跳，保持连接活跃
                    yield "event: ping\ndata: {}\n\n"
        finally:
            await unsubscribe(queue)

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
