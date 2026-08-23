"""自包含 HTML 图表报告 — 内联 CSS + SVG 图表，无外部资源，浏览器直接打开。

输入与 JSON 导出共用同一 payload（runner._build_payload 产物）：
{"run_id", "generated_at", "model", "ai_api_base", "concurrency",
 "elapsed_sec", "features": {feature: {"dataset", "elapsed_sec", "summary": {...}, "cases": [逐用例 dict]}}}
"""

from __future__ import annotations

import html
from pathlib import Path
from typing import Any

from briefdesk.logger import fmt_dur
from briefdesk.plugins.benchmark.charts import (
    bar_chart_svg,
    confusion_svg,
    overview_bar_svg,
)
from briefdesk.plugins.benchmark.report import _FEATURE_METRIC_LABELS, _FEATURE_TITLES

_CSS = """
body { font-family: "Microsoft YaHei", "PingFang SC", sans-serif; margin: 24px;
       color: #111827; background: #f9fafb; }
h1 { font-size: 22px; }
h2 { font-size: 17px; margin-top: 28px; border-bottom: 2px solid #e5e7eb;
     padding-bottom: 4px; }
.meta { color: #4b5563; font-size: 13px; }
.chart { background: #fff; border: 1px solid #e5e7eb; border-radius: 8px;
         padding: 12px; margin: 10px 0; display: inline-block; }
table { border-collapse: collapse; background: #fff; margin: 8px 0;
        font-size: 13px; width: 100%; }
th, td { border: 1px solid #e5e7eb; padding: 5px 9px; text-align: left; }
th { background: #f3f4f6; white-space: nowrap; }
.ok { color: #16a34a; font-weight: bold; }
.bad { color: #dc2626; font-weight: bold; }
.warn { color: #d97706; }
.muted { color: #9ca3af; }
.dataset { color: #6b7280; font-size: 12px; }
"""


def _esc(value: object) -> str:
    return html.escape(str(value))


def _dur(value: float | None) -> str:
    """耗时展示：缺省显示 '-'，其余按 fmt_dur 统一格式。"""
    if value is None:
        return "-"
    return fmt_dur(value)


def _head() -> str:
    return (
        '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">'
        "<title>LLM 功能基准报告</title>"
        f"<style>{_CSS}</style></head><body>"
    )


def _header(payload: dict[str, Any]) -> str:
    meta = [
        ("运行时间", payload.get("generated_at", "")),
        ("模型", payload.get("model", "")),
        ("API", payload.get("ai_api_base", "")),
        ("并发", str(payload.get("concurrency", ""))),
        ("总用时", _dur(payload.get("elapsed_sec"))),
    ]
    parts = ['<h1>LLM 功能基准报告</h1><p class="meta">']
    parts.append(
        "&nbsp;·&nbsp;".join(
            f"<b>{_esc(k)}</b> {_esc(v)}" for k, v in meta
        )
    )
    parts.append("</p>")
    return "".join(parts)


_FEATURE_COLORS = {
    "classify": "#2563EB",
    "dedup": "#7C3AED",
    "merge": "#D97706",
    "title": "#059669",
}


def _overview(payload: dict[str, Any]) -> str:
    """各功能核心指标总览条形图。"""
    headline_keys = {
        "classify": "category_accuracy",
        "dedup": "accuracy",
        "merge": "accuracy",
        "title": "keyword_hit_rate",
    }
    items: list[tuple[str, float, str]] = []
    for feature in ("classify", "dedup", "merge", "title"):
        summary = payload.get("features", {}).get(feature, {}).get("summary")
        if not summary:
            continue
        items.append(
            (
                _FEATURE_TITLES.get(feature, feature),
                float(summary.get(headline_keys[feature], 0) or 0),
                _FEATURE_COLORS[feature],
            )
        )
    if not items:
        return ""
    return (
        '<div class="chart">'
        + overview_bar_svg(
            "各功能核心指标（classify=类别准确率 / dedup·merge=准确率 / title=关键词命中率）",
            items,
        )
        + "</div>"
    )


_RATE_KEYS = {
    "category_accuracy", "category_precision", "category_recall", "category_f1",
    "time_msg_accuracy", "time_point_recall", "time_point_precision",
    "failure_rate", "summary_fill_rate",
    "accuracy", "precision", "recall", "f1",
    "exact_match_rate", "keyword_hit_rate",
}


def _metric_value(key: str, value: Any) -> str:
    if key in _RATE_KEYS:
        return f"{float(value or 0) * 100:.1f}%"
    if key == "avg_len":
        return f"{float(value or 0):.1f} 字"
    return _esc(value)


def _metric_table(feature: str, summary: dict[str, Any], elapsed_sec: float | None = None) -> str:
    rows: list[str] = []
    for key, label in _FEATURE_METRIC_LABELS.get(feature, []):
        rows.append(
            f"<tr><td>{_esc(label)}</td><td>{_metric_value(key, summary.get(key, 0))}</td></tr>"
        )
    extras: list[tuple[str, str]] = []
    if feature == "classify":
        extras = [
            ("期望分类", f"{summary.get('expected', 0)} 条"),
            ("模型输出", f"{summary.get('model_classified', 0)} 条（误报 {summary.get('false_positives', 0)}）"),
        ]
    elif feature in ("dedup", "merge"):
        extras = [
            ("混淆矩阵", f"TP {summary.get('tp', 0)} / FP {summary.get('fp', 0)} / TN {summary.get('tn', 0)} / FN {summary.get('fn', 0)}"),
        ]
        if feature == "dedup" and summary.get("skipped"):
            extras.append(("预筛跳过", f"{summary['skipped']} 条（未触发 AI 判定）"))
    elif feature == "title":
        extras = [
            ("平均长度", f"{summary.get('avg_len', 0):.1f} 字"),
            ("超长/回退", f"{summary.get('too_long_count', 0)} / {summary.get('fallback_count', 0)} 条"),
        ]
    extras.append(("用例", f"{summary.get('cases', 0)} 条（出错 {summary.get('error_cases', 0)}）"))
    extras.append(("测试用时", _dur(elapsed_sec)))
    for label, value in extras:
        rows.append(f"<tr><td>{_esc(label)}</td><td>{_esc(value)}</td></tr>")
    return f"<table><tr><th>指标</th><th>值</th></tr>{''.join(rows)}</table>"


def _feature_charts(feature: str, summary: dict[str, Any]) -> str:
    if feature == "classify":
        items = [
            ("类别准确率", summary.get("category_accuracy", 0)),
            ("类别精确率", summary.get("category_precision", 0)),
            ("类别召回率", summary.get("category_recall", 0)),
            ("类别 F1", summary.get("category_f1", 0)),
            ("时间点完全一致率", summary.get("time_msg_accuracy", 0)),
            ("时间点召回率", summary.get("time_point_recall", 0)),
            ("时间点精确率", summary.get("time_point_precision", 0)),
            ("本轮失败率", summary.get("failure_rate", 0)),
            ("标题概括填充率", summary.get("summary_fill_rate", 0)),
        ]
        svg = bar_chart_svg("分类指标", [(label, float(v or 0)) for label, v in items])
        return f'<div class="chart">{svg}</div>'
    if feature in ("dedup", "merge"):
        items = [
            ("准确率", summary.get("accuracy", 0)),
            ("精确率", summary.get("precision", 0)),
            ("召回率", summary.get("recall", 0)),
            ("F1", summary.get("f1", 0)),
        ]
        bar = bar_chart_svg(
            "判定指标",
            [(label, float(v or 0)) for label, v in items],
            color=_FEATURE_COLORS[feature],
        )
        matrix = confusion_svg(
            "混淆矩阵（行=预测，列=期望）",
            int(summary.get("tp", 0)),
            int(summary.get("fp", 0)),
            int(summary.get("tn", 0)),
            int(summary.get("fn", 0)),
        )
        return f'<div class="chart">{bar}</div><div class="chart">{matrix}</div>'
    items = [
        ("精确匹配率", summary.get("exact_match_rate", 0)),
        ("关键词命中率", summary.get("keyword_hit_rate", 0)),
    ]
    svg = bar_chart_svg(
        "标题指标",
        [(label, float(v or 0)) for label, v in items],
        color=_FEATURE_COLORS["title"],
    )
    return f'<div class="chart">{svg}</div>'


def _verdict(feature: str, case: dict[str, Any]) -> str:
    """逐用例结论：✔ / ✘ / 错误。"""
    if case.get("error"):
        return '<span class="bad">错误</span>'
    if feature == "classify":
        ok = (
            int(case.get("hits", 0)) == int(case.get("expected_count", 0))
            and int(case.get("false_positives", 0)) == 0
        )
    elif feature in ("dedup", "merge"):
        ok = bool(case.get("predicted")) == bool(case.get("expected"))
    else:  # title
        output = case.get("output")
        ok = bool(
            output is not None
            and (
                (case.get("expected_title") and output == case.get("expected_title"))
                or (
                    case.get("expected_keywords")
                    and all(k in output for k in case["expected_keywords"])
                )
            )
        )
    return '<span class="ok">✔</span>' if ok else '<span class="bad">✘</span>'


def _case_rows(feature: str, cases: list[dict[str, Any]]) -> str:
    if feature == "classify":
        headers = ["用例", "结论", "类别命中", "模型输出", "误报", "时间完全一致", "失败", "说明"]
        rows = []
        for c in cases:
            rows.append(
                "<tr>"
                f"<td>{_esc(c.get('case_id'))}</td>"
                f"<td>{_verdict(feature, c)}</td>"
                f"<td>{_esc(c.get('hits', 0))}/{_esc(c.get('expected_count', 0))}</td>"
                f"<td>{_esc(c.get('model_count', 0))}</td>"
                f"<td>{_esc(c.get('false_positives', 0))}</td>"
                f"<td>{_esc(c.get('time_msgs_ok', 0))}/{_esc(c.get('time_msgs_expected', 0))}</td>"
                f"<td>{_esc(c.get('failed', 0))}</td>"
                f"<td class=\"muted\">{_esc(c.get('error', ''))}</td>"
                "</tr>"
            )
    elif feature in ("dedup", "merge"):
        headers = ["用例", "结论", "判定", "期望", "预筛跳过", "说明"]
        rows = []
        for c in cases:
            skipped = "是" if feature == "dedup" and c.get("skipped") else ""
            rows.append(
                "<tr>"
                f"<td>{_esc(c.get('case_id'))}</td>"
                f"<td>{_verdict(feature, c)}</td>"
                f"<td>{_esc(c.get('predicted'))}</td>"
                f"<td>{_esc(c.get('expected'))}</td>"
                f"<td>{_esc(skipped)}</td>"
                f"<td class=\"muted\">{_esc(c.get('error', ''))}</td>"
                "</tr>"
            )
    else:  # title
        headers = ["用例", "结论", "输出", "期望标题", "关键词", "说明"]
        rows = []
        for c in cases:
            output = c.get("output")
            if output is None:
                output_html = '<span class="warn">回退原标题</span>'
            else:
                output_html = _esc(output)
            rows.append(
                "<tr>"
                f"<td>{_esc(c.get('case_id'))}</td>"
                f"<td>{_verdict(feature, c)}</td>"
                f"<td>{output_html}</td>"
                f"<td>{_esc(c.get('expected_title', ''))}</td>"
                f"<td>{_esc('、'.join(c.get('expected_keywords') or []))}</td>"
                f"<td class=\"muted\">{_esc(c.get('error', ''))}</td>"
                "</tr>"
            )
    header = "".join(f"<th>{_esc(h)}</th>" for h in headers)
    return f"<table><tr>{header}</tr>{''.join(rows)}</table>"


def _feature_section(feature: str, data: dict[str, Any]) -> str:
    summary = data.get("summary") or {}
    cases = data.get("cases") or []
    parts = [
        f"<h2>{_esc(_FEATURE_TITLES.get(feature, feature))}</h2>",
        f'<p class="dataset">数据集: {_esc(data.get("dataset", ""))}</p>',
        _feature_charts(feature, summary),
        _metric_table(feature, summary, data.get("elapsed_sec")),
        _case_rows(feature, cases),
    ]
    return "".join(parts)


def build_html_report(payload: dict[str, Any]) -> str:
    """由基准结果 payload 构建自包含 HTML 报告。"""
    parts = [_head(), _header(payload), _overview(payload)]
    for feature in ("classify", "dedup", "merge", "title"):
        data = payload.get("features", {}).get(feature)
        if data:
            parts.append(_feature_section(feature, data))
    parts.append("</body></html>")
    return "".join(parts)


def save_html_report(out_dir: str | Path, run_id: str, html_text: str) -> Path:
    """HTML 报告导出（与 JSON 同目录、同 run_id）。"""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"run-{run_id}.html"
    path.write_text(html_text, encoding="utf-8")
    return path
