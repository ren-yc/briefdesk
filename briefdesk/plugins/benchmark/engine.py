"""基准运行编排 — 用例装载 + 与生产同引擎执行 + 结果 payload。

用例来源：
- 文件数据集（CLI / 示例）：schema.load_dataset_file + parse_cases；
- 网页导出用例（Web）：cases/<feature>.fromweb.json，load_web_cases 加载。

执行与生产同引擎路径：classify_batch / DedupEngine.check_dedup /
judge_merge / summarize_title，经 briefdesk.ai_ports 端口调用真实供应商；
运行环境见 providers.bench_environment（补丁式临时库，不动应用连接）。
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from briefdesk.config import config
from briefdesk.db import insert_item
from briefdesk.plugins.benchmark.metrics import (
    BinaryCaseEval,
    ClassifyCaseEval,
    TitleCaseEval,
    aggregate_binary,
    aggregate_classify,
    aggregate_title,
    evaluate_classify_case,
)
from briefdesk.plugins.benchmark.providers import bench_environment
from briefdesk.plugins.benchmark.schema import (
    BaseCase,
    CategoryDef,
    ClassifyCase,
    DatasetFile,
    DedupCase,
    MergeCase,
    TitleCase,
    card_fields,
    load_dataset_file,
    parse_cases,
)
from briefdesk.plugins.classify.engine import classify_batch
from briefdesk.plugins.dedup.engine import CachedItem, DedupEngine, build_item_input
from briefdesk.plugins.merge.engine import judge_merge, summarize_title
from briefdesk.types import ClassifyResult

logger = logging.getLogger(__name__)

FEATURES: tuple[str, ...] = ("classify", "dedup", "merge", "title")

FROMWEB_SOURCE_LABEL = "cases/*.fromweb.json"  # 网页导出用例的 dataset 标识（报告/JSON 溯源）


# ── 用例运行器 ──


@dataclasses.dataclass(frozen=True)
class CaseProgress:
    """单条用例完成事件（评估进度回调负载）。

    done 含失败用例；调用方可按 done % N 节流输出（CLI 每 5 条一行）。
    """

    feature: str
    done: int
    total: int
    failed: int
    case_id: str | None = None


class _ProbeDedupEngine(DedupEngine):
    """记录 AI 判定调用次数：ai_calls == 0 表示候选预筛直接跳过（未触发 AI）。"""

    def __init__(self) -> None:
        super().__init__()
        self.ai_calls = 0

    async def _ask_ai(self, a: CachedItem, b_title: str, b_quote: str) -> bool | None:
        self.ai_calls += 1
        return await super()._ask_ai(a, b_title, b_quote)


async def _run_classify(case: ClassifyCase) -> ClassifyCaseEval:
    messages = [m.to_internal() for m in case.messages]
    outcome = await classify_batch(messages)
    return evaluate_classify_case(case, outcome)


async def _run_dedup(case: DedupCase) -> BinaryCaseEval:
    # 与真实管道同路径：items 先作为"已有卡片"入库（dedup 缓存预热从库加载），
    # 再对 query 做候选预筛 + AI 判重。类别对判重无影响，统一用默认值。
    engine = _ProbeDedupEngine()
    for item in case.items:
        msg = item.to_internal()
        title, _content = card_fields(item)
        await insert_item(
            build_item_input(
                msg, ClassifyResult(msg_index=0, category="活动通知"), title
            )
        )
    q_title, q_content = card_fields(case.query)
    result = await engine.check_dedup(
        q_title,
        source_group=case.query.group_name or "bench",
        source_quote=q_content,
        image_urls=list(case.query.image_urls) or None,
        source=case.query.source,
    )
    return BinaryCaseEval(
        case_id=case.id,
        predicted=result.is_duplicate,
        expected=case.expected.same,
        skipped=engine.ai_calls == 0,
    )


async def _run_merge(case: MergeCase) -> BinaryCaseEval:
    head_title, head_content = card_fields(case.head)
    tail_title, tail_content = card_fields(case.tail)
    merged = await judge_merge(head_title, head_content, tail_title, tail_content)
    if merged is None:
        return BinaryCaseEval(
            case_id=case.id,
            predicted=False,
            expected=case.expected.merge,
            error="合并判官失败（无判定结论）",
        )
    return BinaryCaseEval(
        case_id=case.id,
        predicted=merged,
        expected=case.expected.merge,
    )


async def _run_title(case: TitleCase) -> TitleCaseEval:
    title, _desc = card_fields(case.message)
    old_title = case.old_title or title
    key_info = case.key_info or case.message.key_info
    output = await summarize_title(old_title, key_info, case.message.content)
    return TitleCaseEval(
        case_id=case.id,
        output=output,
        expected_title=case.expected.title,
        expected_keywords=case.expected.keywords or [],
    )


_RUNNERS: dict[str, Any] = {
    "classify": _run_classify,
    "dedup": _run_dedup,
    "merge": _run_merge,
    "title": _run_title,
}


def _error_eval(feature: str, case: BaseCase, err: Exception) -> Any:
    """用例运行失败时的占位评估记录（不拖垮整轮基准）。"""
    message = f"{type(err).__name__}: {err}"
    if feature == "classify":
        assert isinstance(case, ClassifyCase)
        return ClassifyCaseEval(
            case_id=case.id, messages_total=len(case.messages), error=message
        )
    if feature == "title":
        assert isinstance(case, TitleCase)
        return TitleCaseEval(case_id=case.id, error=message)
    assert isinstance(case, (DedupCase, MergeCase))
    return BinaryCaseEval(
        case_id=case.id, predicted=False, expected=False, error=message
    )


async def _run_feature(
    feature: str,
    cases: list[BaseCase],
    concurrency: int,
    progress: Callable[[CaseProgress], None] | None = None,
) -> list[Any]:
    sem = asyncio.Semaphore(max(1, concurrency))
    total = len(cases)
    done = 0
    failed = 0

    async def _guarded(worker: Any, case: BaseCase) -> Any:
        nonlocal done, failed
        async with sem:
            try:
                ev = await worker(case)
            except Exception as e:  # noqa: BLE001 — 单条失败不拖垮整轮
                logger.warning("用例 %s 运行失败: %s", case.id, e)
                failed += 1
                ev = _error_eval(feature, case, e)
        # 计数与回调间无 await 点，事件循环内原子，done 严格递增
        done += 1
        if progress is not None:
            progress(
                CaseProgress(
                    feature=feature, done=done, total=total, failed=failed, case_id=case.id
                )
            )
        return ev

    return await asyncio.gather(*[_guarded(_RUNNERS[feature], c) for c in cases])


def _aggregate(feature: str, evals: list[Any]) -> dict[str, float | int]:
    if feature == "classify":
        return aggregate_classify(evals)  # type: ignore[arg-type]
    if feature == "title":
        return aggregate_title(evals)  # type: ignore[arg-type]
    return aggregate_binary(evals)  # type: ignore[arg-type]


# ── 用例装载 ──


def load_file_cases(path: str | Path) -> list[BaseCase]:
    """从文件数据集加载并校验用例（顶层结构 + 逐条 schema）。"""
    dataset = load_dataset_file(path)
    return parse_cases(dataset)


def load_file_dataset(path: str | Path) -> DatasetFile:
    """加载文件数据集顶层结构（含 categories 声明，供类别覆盖）。"""
    return load_dataset_file(path)


async def load_web_cases(feature: str) -> list[BaseCase]:
    """从 cases/<feature>.fromweb.json（网页导出）加载某功能全部用例。

    逐条校验，非法项跳过并告警；文件缺失/不可读返回空列表（见 store.list_fromweb）。
    """
    from briefdesk.plugins.benchmark import store as bench_store

    raw_cases = await bench_store.list_fromweb(feature)
    cases, errors = bench_store.parse_cases_with_errors(feature, raw_cases)
    for err in errors:
        logger.warning("网页导出用例跳过: %s", err)
    return cases


# ── 运行主入口 ──


async def run_benchmark_cases(
    cases_by_feature: dict[str, list[BaseCase]],
    category_defs: list[CategoryDef] | None = None,
    *,
    concurrency: int = 1,
    dataset_label: str | dict[str, str] = FROMWEB_SOURCE_LABEL,
    progress: Callable[[CaseProgress], None] | None = None,
) -> tuple[dict[str, Any], dict[str, list[Any]]]:
    """在基准环境内运行指定功能用例。

    返回 (payload, evals_by_feature)：payload 与 JSON/HTML 报告同构
    （case 明细为 dict）；evals 为逐用例评估对象（CLI 终端渲染用）。
    dataset_label 标识用例来源（Web 运行为 cases/*.fromweb.json；
    CLI 传各数据集文件路径），写入报告供溯源。
    progress（可选）：每条用例 settle 后回调一次（成功/失败都算），
    CLI 据此输出评估进度；缺省无进度输出。
    """
    started = time.monotonic()
    async with bench_environment(categories=category_defs):
        summaries: dict[str, dict[str, float | int]] = {}
        evals_by_feature: dict[str, list[Any]] = {}
        elapsed: dict[str, float] = {}
        for feature, cases in cases_by_feature.items():
            if not cases:
                continue
            f_start = time.monotonic()
            evals = await _run_feature(feature, cases, concurrency, progress=progress)
            elapsed[feature] = round(time.monotonic() - f_start, 3)
            summaries[feature] = _aggregate(feature, evals)
            evals_by_feature[feature] = evals
    payload = _build_payload(
        summaries,
        evals_by_feature,
        concurrency,
        dataset_label,
        elapsed=elapsed,
        total_elapsed=round(time.monotonic() - started, 3),
    )
    return payload, evals_by_feature


def _build_payload(
    summaries: dict[str, dict[str, float | int]],
    evals_by_feature: dict[str, list[Any]],
    concurrency: int,
    dataset_label: str | dict[str, str] = FROMWEB_SOURCE_LABEL,
    *,
    elapsed: dict[str, float] | None = None,
    total_elapsed: float | None = None,
) -> dict[str, Any]:
    """结果 payload（run_id/模型信息/逐功能 summary+逐用例明细/测试用时）。

    elapsed_sec 为墙钟耗时（秒）：顶层 = 整轮总用时；features[f] 内 =
    该功能用例执行用时（不含聚合）。
    """

    def _label(feature: str) -> str:
        if isinstance(dataset_label, dict):
            return dataset_label.get(feature) or FROMWEB_SOURCE_LABEL
        return dataset_label

    return {
        "run_id": time.strftime("%Y%m%d-%H%M%S"),
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "model": config.ai_model,
        "ai_api_base": config.ai_api_base,
        "concurrency": max(1, concurrency),
        "elapsed_sec": total_elapsed,
        "features": {
            f: {
                "dataset": _label(f),
                "elapsed_sec": (elapsed or {}).get(f),
                "summary": summaries.get(f, {}),
                "cases": [dataclasses.asdict(e) for e in evals_by_feature.get(f, [])],
            }
            for f in summaries
        },
    }
