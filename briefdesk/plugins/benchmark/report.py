"""基准报告渲染 — 终端表格 + JSON 导出（纯函数，便于单测）。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from briefdesk.logger import fmt_dur
from briefdesk.plugins.benchmark.metrics import (
    BinaryCaseEval,
    ClassifyCaseEval,
    TitleCaseEval,
)

_FEATURE_TITLES = {
    "classify": "分类（classify_batch）",
    "dedup": "去重（check_dedup）",
    "merge": "合并判定（judge_merge）",
    "title": "标题重拟（summarize_title）",
}

_FEATURE_METRIC_LABELS: dict[str, list[tuple[str, str]]] = {
    "classify": [
        ("category_accuracy", "类别准确率"),
        ("category_precision", "类别精确率"),
        ("category_recall", "类别召回率"),
        ("category_f1", "类别 F1"),
        ("time_msg_accuracy", "时间点完全一致率"),
        ("time_point_recall", "时间点召回率"),
        ("time_point_precision", "时间点精确率"),
        ("failure_rate", "本轮失败率"),
        ("summary_fill_rate", "标题概括填充率"),
    ],
    "dedup": [
        ("accuracy", "判重准确率"),
        ("precision", "判重精确率(same)"),
        ("recall", "判重召回率(same)"),
        ("f1", "判重 F1"),
    ],
    "merge": [
        ("accuracy", "判定准确率"),
        ("precision", "判定精确率(合并)"),
        ("recall", "判定召回率(合并)"),
        ("f1", "判定 F1"),
    ],
    "title": [
        ("exact_match_rate", "精确匹配率"),
        ("keyword_hit_rate", "关键词命中率"),
        ("avg_len", "平均长度(字)"),
        ("too_long_count", "超长(>30字)数"),
        ("fallback_count", "回退原标题数"),
    ],
}


def _pct(value: float | None) -> str:
    if value is None:
        return "-"
    if isinstance(value, int):
        return str(value)
    return f"{value * 100:.1f}%"


def format_float(value: float | None) -> str:
    return f"{value:.2f}" if isinstance(value, float) else str(value or 0)


def render_case_lines(feature: str, evals: list[Any]) -> list[str]:
    """每条用例一行结论（✔/✘ + 简述），供终端展示。"""
    lines: list[str] = []
    for e in evals:
        mark = "✘"
        detail: str | None = None
        if e.error is not None:
            detail = f"错误: {e.error}"
        elif feature == "classify":
            assert isinstance(e, ClassifyCaseEval)
            detail = (
                f"期望 {e.expected_count} 条，命中 {e.hits}，"
                f"模型分类 {e.model_count}（误报 {e.false_positives}），"
                f"时间 {e.time_msgs_ok}/{e.time_msgs_expected} 条完全一致，"
                f"失败 {e.failed}"
            )
            if e.hits == e.expected_count and e.false_positives == 0:
                mark = "✔"
        elif feature == "dedup":
            assert isinstance(e, BinaryCaseEval)
            mark = "✔" if e.predicted == e.expected else "✘"
            skip = "（预筛跳过，未触发 AI）" if e.skipped else ""
            detail = f"判定 {e.predicted}，期望 {e.expected}{skip}"
        elif feature == "merge":
            assert isinstance(e, BinaryCaseEval)
            mark = "✔" if e.predicted == e.expected else "✘"
            detail = f"判定 {e.predicted}，期望 {e.expected}"
        elif feature == "title":
            assert isinstance(e, TitleCaseEval)
            if e.output is None:
                detail = "回退原标题（模型无输出）"
            else:
                detail = f"输出 {e.output!r}"
                if e.expected_title is not None:
                    detail += f"，期望 {e.expected_title!r}"
                if e.expected_keywords:
                    detail += f"，关键词 {e.expected_keywords}"
                if e.exact_hit or e.keyword_hit:
                    mark = "✔"
        lines.append(f"  [{mark}] {e.case_id}  {detail}")
    return lines


def render_metrics(feature: str, summary: dict[str, float | int]) -> list[str]:
    lines: list[str] = []
    for key, label in _FEATURE_METRIC_LABELS.get(feature, []):
        value = summary.get(key, 0)
        lines.append(f"  {label:<14} {_pct(value)}")
    extra: list[str] = []
    if feature == "classify":
        extra = [
            (
                f"  期望分类 {summary['expected']} 条 / 模型输出 "
                f"{summary['model_classified']} 条（误报 {summary['false_positives']}）"
            ),
            f"  用例 {summary['cases']} 条（出错 {summary['error_cases']}）",
        ]
    elif feature in ("dedup", "merge"):
        extra = [
            f"  TP {summary['tp']} / FP {summary['fp']} / TN {summary['tn']} / FN {summary['fn']}",
            f"  用例 {summary['cases']} 条（出错 {summary['error_cases']}）",
        ]
        if feature == "dedup" and summary.get("skipped"):
            extra.append(f"  预筛跳过（未触发 AI 判定）: {summary['skipped']} 条")
    elif feature == "title":
        extra = [
            (
                f"  精确匹配用例 {summary['exact_match_cases']} 条 / "
                f"关键词用例 {summary['keyword_hit_cases']} 条"
            ),
            f"  用例 {summary['cases']} 条（出错 {summary['error_cases']}）",
        ]
    return lines + extra


def render_feature_block(
    feature: str,
    evals: list[Any],
    summary: dict[str, float | int],
    elapsed_sec: float | None = None,
) -> list[str]:
    lines = [f"== {_FEATURE_TITLES.get(feature, feature)} =="]
    lines += render_case_lines(feature, evals)
    lines.append("  -- 指标 --")
    lines += render_metrics(feature, summary)
    if elapsed_sec is not None:
        lines.append(f"  测试用时 {fmt_dur(elapsed_sec)}")
    return lines


def save_json_report(out_dir: str | Path, payload: dict[str, Any]) -> Path:
    """JSON 结果导出（含逐用例明细，便于二次分析/回归对比）。"""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"run-{payload['run_id']}.json"
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path
