"""基准测试 Web 路由（benchmark WebPlugin）— 文件导出 / 处理记录 / 运行 / 报告。

- 网页「导出当前列表为基准用例」把当前筛选的卡片构造成四类用例
  （classify/dedup/merge/title，推导口径见 store.CASE_BUILDERS）并逐功能
  导出为 cases/<feature>.fromweb.json（覆盖式，见 store.py，不触碰数据库）；
- 网页「记录处理过程」打开 benchmark 阶段插件的采集（默认关闭）：管道真实
  处理时点的 dedup/merge 判定累积内存（recorder.py），经
  POST /api/benchmark/export-recorded 导出为 cases/<feature>.fromweb.json
  （覆盖式，含判重/合并命中的正向用例——按卡片最终状态导出观察不到）；
- Web 运行从 cases/*.fromweb.json 加载；
- 运行在后台任务中执行（真实 AI 调用可能耗时数分钟），状态经
  GET /api/benchmark/run 轮询；结果（payload + HTML 报告）驻留内存，
  经 /api/benchmark/report(.json) 取回；
- 运行期间补丁 briefdesk.db.get_db 指向临时库，并经 pipeline.set_processing_paused
  暂停生产处理管道——实时消息延后到下一轮回填窗口处理，不丢失；请勿同时
  手动触发同步。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, cast

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from briefdesk.db import get_items_page
from briefdesk.plugins.benchmark import engine as bench_engine
from briefdesk.plugins.benchmark import recorder as bench_recorder
from briefdesk.plugins.benchmark import store as bench_store
from briefdesk.plugins.benchmark.html_report import build_html_report
from briefdesk.plugins.benchmark.schema import FEATURES

logger = logging.getLogger(__name__)

router = APIRouter()

# 单进程内存态：最近一次运行结果 + 运行中任务
_running_task: asyncio.Task | None = None
_last_result: dict[str, Any] | None = None  # {"run_id", "payload", "html", "error"}


# ── 请求模型 ──


class RunBody(BaseModel):
    features: list[str] | None = None  # 缺省 = 全部


class RecordBody(BaseModel):
    enabled: bool


# ── 用例（文件态：cases/*.fromweb.json）──


def _check_feature(feature: str | None) -> None:
    if feature is not None and feature not in FEATURES:
        raise HTTPException(400, f"未知功能: {feature}")


@router.get("/api/benchmark/cases")
async def list_cases(feature: str | None = None) -> dict[str, Any]:
    """列出网页导出的用例（?feature= 过滤）；每项含 feature 字段。"""
    _check_feature(feature)
    cases = await bench_store.list_fromweb(feature)
    return {"cases": cases}


@router.delete("/api/benchmark/cases")
async def remove_all_cases(feature: str | None = None) -> dict[str, Any]:
    """删除网页导出的用例文件（?feature= 可只删某功能）；返回删除文件数。"""
    _check_feature(feature)
    deleted = await bench_store.delete_all_fromweb(feature)
    return {"deleted": deleted}


@router.post("/api/benchmark/import-current")
async def import_current_list(
    category: str = Query(None),
    verified: str = Query("unverified"),
    q: str = Query(None),
    source_group: str = Query(None, alias="sourceGroup"),
    min_msg_time: int = Query(None, alias="minMsgTime", ge=0),
    hide_expired: bool = Query(False, alias="hideExpired"),
    filter_now: str = Query(None, alias="filterNow"),
) -> dict[str, Any]:
    """把当前筛选条件下的卡片导出为四类基准用例（参数与 /api/items 一致）。

    期望 = 卡片当前状态（人工修正过即 ground truth；推导口径见
    store.CASE_BUILDERS 各构造器）：classify 按批上限拆分；title 每卡一例
    （期望=卡片关键词）；dedup 含同 content_hash 共存对（same=true）与相邻
    不同类别对（same=false）；merge 为同会话时间窗内的主体/发送者配对。
    **已忽略卡片（is_verified=-1）** 恒被纳入 classify 用例作为噪声样本
    （消息进入 messages、不写期望——AI 误分类上来的闲聊，模型不应输出
    分类结果），并在 title/dedup/merge 中排除。
    每功能覆盖导出 cases/<feature>.fromweb.json；某功能无可导出用例时
    保留其原文件（不触碰数据库）。
    """
    now_local = None
    if hide_expired:
        now_local = filter_now or time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())

    async def _fetch_rows(verified_filter: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        offset = 0
        for _ in range(100000):  # 守卫：最多 2000 万条
            page = await get_items_page(
                category=category or None,
                verified=verified_filter,
                q=q or None,
                source_group=source_group or None,
                min_msg_time=min_msg_time,
                hide_expired=hide_expired,
                now_local=now_local if hide_expired else None,
                limit=200,
                offset=offset,
            )
            rows.extend(cast("list[dict[str, Any]]", page["items"]))
            if not page["has_more"] or not page["items"]:
                break
            offset = page["next_offset"]
            if offset <= 0:
                break  # 防死循环兜底
        return rows

    # 当前视图卡片（未核实/备忘录等，正样本）+ 已忽略卡片（噪声样本：AI
    # 误分类的闲聊，期望模型不分类）。忽略行恒被纳入，与当前视图无关。
    view_rows = await _fetch_rows(verified)
    kept_rows = [r for r in view_rows if not bench_store._is_noise(r)]
    noise_rows = await _fetch_rows("ignored")
    seen_ids = {r.get("id") for r in kept_rows}
    noise_rows = [r for r in noise_rows if r.get("id") not in seen_ids]
    classify_rows = kept_rows + noise_rows

    exported: dict[str, int] = {}
    paths: dict[str, str] = {}
    classify_messages = 0
    for feature, builder in bench_store.CASE_BUILDERS.items():
        # classify 含噪声样本（已忽略卡片）；title/dedup/merge 只用有效卡片
        source_rows = classify_rows if feature == "classify" else kept_rows
        cases = builder(source_rows)
        if feature == "classify":
            classify_messages = sum(len(c["messages"]) for c in cases)
        if not cases:
            continue  # 该功能无可导出用例：保留已有文件
        try:
            path = await bench_store.export_fromweb(feature, cases)
        except Exception as e:
            raise HTTPException(400, f"{feature}: {e}") from e
        exported[feature] = len(cases)
        paths[feature] = str(path)
    n_categorized = sum(1 for r in kept_rows if (r.get("category") or "").strip())
    return {
        "counts": {f: exported.get(f, 0) for f in bench_store.CASE_BUILDERS},
        "total": sum(exported.values()),
        "cards": len(kept_rows),
        "messages": classify_messages,
        "noise": len(noise_rows),  # 已忽略卡片数（classify 噪声样本）
        "skipped_no_category": len(kept_rows) - n_categorized,
        "paths": paths,
    }


# ── 处理记录（benchmark 阶段插件采集）──


@router.get("/api/benchmark/record")
async def record_status() -> dict[str, Any]:
    """记录状态：开关 + 各功能已累积用例数。"""
    return bench_recorder.stats()


@router.post("/api/benchmark/record")
async def set_record(body: RecordBody) -> dict[str, Any]:
    """打开/关闭处理过程记录（累积内容不随开关丢失）。"""
    bench_recorder.set_enabled(body.enabled)
    return bench_recorder.stats()


@router.delete("/api/benchmark/record")
async def clear_record() -> dict[str, Any]:
    """丢弃已累积的处理记录（不导出、不触碰 cases 文件）。"""
    cleared = bench_recorder.clear()
    return {"cleared": cleared}


@router.post("/api/benchmark/export-recorded")
async def export_recorded() -> dict[str, Any]:
    """把处理记录导出为基准用例（覆盖 cases/<feature>.fromweb.json），随后清空。

    与「导出当前列表」互补：记录捕获管道处理时点的真实判定（判重命中/
    合并命中的正向用例、判官判定的负向用例、合并后重拟标题事件）；
    某功能无记录时保留其原文件。
    """
    try:
        return await bench_recorder.export_recorded()
    except Exception as e:
        raise HTTPException(400, f"导出处理记录失败: {e}") from e


# ── 运行与报告 ──


def _run_state() -> dict[str, Any]:
    """运行状态（running / 最近结果摘要），供前端轮询。"""
    state: dict[str, Any] = {"running": False}
    if _running_task is not None and not _running_task.done():
        state["running"] = True
    if _last_result is not None:
        state["run_id"] = _last_result["run_id"]
        state["summary"] = {
            f: data.get("summary", {})
            for f, data in _last_result["payload"].get("features", {}).items()
        }
        state["error"] = _last_result.get("error")
        state["started_at"] = _last_result.get("started_at")
        state["elapsed_sec"] = _last_result["payload"].get("elapsed_sec")
    return state


@router.get("/api/benchmark/run")
async def run_status() -> dict[str, Any]:
    return _run_state()


@router.post("/api/benchmark/run")
async def start_run(body: RunBody | None = None) -> dict[str, Any]:
    global _running_task
    if _running_task is not None and not _running_task.done():
        raise HTTPException(409, "基准正在运行中")
    features = list(body.features) if body and body.features else list(FEATURES)
    invalid = [f for f in features if f not in FEATURES]
    if invalid:
        raise HTTPException(400, f"未知功能: {invalid}")

    async def _run() -> None:
        global _last_result
        _last_result = {"run_id": "", "payload": {}, "html": "", "started_at": ""}
        try:
            cases_by_feature: dict[str, list[Any]] = {}
            for f in features:
                cases_by_feature[f] = await bench_engine.load_web_cases(f)
            payload, _evals = await bench_engine.run_benchmark_cases(cases_by_feature)
            _last_result = {
                "run_id": payload["run_id"],
                "payload": payload,
                "html": build_html_report(payload),
                "started_at": payload["generated_at"],
                "error": None,
            }
        except Exception as e:
            logger.exception("基准运行失败")
            _last_result["error"] = f"{type(e).__name__}: {e}"

    _running_task = asyncio.create_task(_run())
    return {"started": True, "features": features}


@router.get("/api/benchmark/report")
async def latest_html_report() -> HTMLResponse:
    """最近一次运行的 HTML 图表报告（自包含，浏览器直接打开/另存）。"""
    if _last_result is None or not _last_result.get("html"):
        raise HTTPException(404, "尚无运行结果")
    return HTMLResponse(_last_result["html"])


@router.get("/api/benchmark/report.json")
async def latest_json_report() -> JSONResponse:
    """最近一次运行的完整结果 payload（指标 + 逐用例明细）。"""
    if _last_result is None or not _last_result.get("payload"):
        raise HTTPException(404, "尚无运行结果")
    return JSONResponse(_last_result["payload"])
