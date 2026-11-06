"""基准报告渲染 — 终端表格 + JSON 导出（纯函数，便于单测）。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from briefdesk.logger import fmt_dur
from briefdesk.plugins.benchmark.metrics import (
    DEDUP_CHANNELS,
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
        ("time_point_recall", "时间点召回率(含回声)"),
        ("time_point_recall_ex_echo", "时间点召回率(剔回声)"),
        ("time_point_precision", "时间点精确率"),
        ("failure_rate", "本轮失败率"),
        ("summary_fill_rate", "标题概括填充率"),
    ],
    "dedup": [
        ("accuracy", "判重准确率"),
        ("precision", "判重精确率(same)"),
        ("recall", "判重召回率(same)"),
        ("f1", "判重 F1"),
        ("ai_judged_rate", "AI 判定用例占比"),
    ],
    "merge": [
        ("accuracy", "判定准确率"),
        ("precision", "判定精确率(合并)"),
        ("recall", "判定召回率(合并)"),
        ("f1", "判定 F1"),
    ],
    "title": [
        ("keyword_hit_rate", "关键词命中率"),
        ("char_similarity_mean", "字符相似度均值"),
        ("char_similarity_p50", "字符相似度中位数"),
        ("avg_len", "平均长度(字)"),
        ("too_long_count", "超长(>30字)数"),
        ("fallback_count", "回退原标题数"),
    ],
}


# 比例类指标：乘 100 加百分号。其余是计数或**字数**，不能按比例渲染。
_RATE_KEYS = {
    "category_accuracy", "category_precision", "category_recall", "category_f1",
    "category_recall_incl_placeholder",
    "time_msg_accuracy", "time_point_recall", "time_point_precision",
    "time_point_recall_ex_echo", "time_point_recall_date_ex_echo",
    "time_point_recall_clock_ex_echo",
    "failure_rate", "summary_fill_rate",
    "accuracy", "precision", "recall", "f1", "ai_judged_rate",
    "keyword_hit_rate",
    # 字符相似度是 0~1 的比值，按比例显示才与同表的其它比率可比
    "char_similarity_mean", "char_similarity_p50",
}


def format_metric(key: str, value: float | None) -> str:
    """单个指标的展示文本（终端与 HTML 共用，避免两处口径漂移）。

    平均长度是**字数**而不是比例：照比例渲染过，会把「平均长度 23.8 字」显示成
    「2379.0%」（回归见 tests/test_benchmark.py 的 title 指标渲染用例）。计数类是
    整数，原样输出；缺失值统一显示「-」。

    缺失判断必须在比例分支**之前**：比例分支里的 `value or 0` 会把 None 变成 0，
    于是"旧报告没记录这个指标"被渲染成"0.0%"，读者会当成真实的零（回归见
    tests/test_benchmark_measurement.py 的旧 payload 渲染用例）。
    """
    if value is None:
        return "-"
    if key in _RATE_KEYS:
        return f"{float(value) * 100:.1f}%"
    if key == "avg_len":
        return f"{float(value):.1f} 字"
    return str(value)


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
            mode = f"[{e.mode}] " if e.mode else ""
            # 纯占位符用例仍逐行显示，但标明不进主指标——读者看到"命中 0"时
            # 需要知道这是结构性不可能，而不是模型失败
            note = "（纯占位符，不进主指标）" if e.placeholder_only else ""
            detail = (
                f"{mode}期望 {e.expected_count} 条，命中 {e.hits}，"
                f"模型分类 {e.model_count}（误报 {e.false_positives}），"
                f"时间 {e.time_msgs_ok}/{e.time_msgs_expected} 条完全一致，"
                f"失败 {e.failed}{note}"
            )
            if e.hits == e.expected_count and e.false_positives == 0:
                mark = "✔"
        elif feature == "dedup":
            assert isinstance(e, BinaryCaseEval)
            mark = "✔" if e.predicted == e.expected else "✘"
            skip = "（预筛跳过，未触发 AI）" if e.skipped else ""
            # 通道逐例写出来：只给汇总分布时，"个别用例走了意外通道"看不出来，
            # 而那正是判重错例最常见的成因（嵌入维度不一致、外来候选）
            channel = f" 通道={e.channel}" if e.channel else ""
            detail = f"判定 {e.predicted}，期望 {e.expected}{skip}{channel}"
        elif feature == "merge":
            assert isinstance(e, BinaryCaseEval)
            mark = "✔" if e.predicted == e.expected else "✘"
            # 预筛门（MERGE_PREFILTER_THRESHOLD）开启时门拦用例也记 skipped：
            # 指标归因需要区分「判官判不合并」与「门直接拦下」两种不合并
            skip = "（预筛跳过，未触发 AI）" if e.skipped else ""
            detail = f"判定 {e.predicted}，期望 {e.expected}{skip}"
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
                if e.char_similarity is not None:
                    detail += f"，字符相似 {e.char_similarity:.2f}"
                if e.keyword_hit or e.soft_hit:
                    mark = "✔"
        lines.append(f"  [{mark}] {e.case_id}  {detail}")
    return lines


def _mode_lines(summary: dict[str, float | int]) -> list[str]:
    """classify 的分 mode 小表（旧 payload 没有 by_mode → 不输出）。"""
    by_mode = summary.get("by_mode")
    if not isinstance(by_mode, dict) or not by_mode:
        return []
    lines = ["  -- 分 mode（类别召回 / 精确 / 误报）--"]
    for mode, stats in by_mode.items():
        if not isinstance(stats, dict):
            continue
        recall = float(stats.get("category_recall", 0) or 0) * 100
        precision = float(stats.get("category_precision", 0) or 0) * 100
        lines.append(
            f"  {mode:<10} 召回 {recall:5.1f}%  精确 {precision:5.1f}%  "
            f"命中 {stats.get('hits', 0)}/{stats.get('expected', 0)}  "
            f"误报 {stats.get('false_positives', 0)}  "
            f"用例 {stats.get('cases', 0)}"
        )
    return lines


def render_metrics(feature: str, summary: dict[str, float | int]) -> list[str]:
    """指标表 + 该功能的补充行。

    补充行一律走 `.get` 并"缺键就不写这行"：报告 JSON 会长期落盘，旧运行的
    payload 没有本轮新增的键，渲染历史报告不能因为少一个键而抛 KeyError。
    主表则相反——每个指标都占一行，缺键显示「-」而不是补 0：补 0 会让"这一轮
    没测过这个指标"看起来像"测了，结果是零"。
    """
    lines: list[str] = []
    for key, label in _FEATURE_METRIC_LABELS.get(feature, []):
        text = format_metric(key, summary[key]) if key in summary else "-"
        lines.append(f"  {label:<14} {text}")
    extra: list[str] = []
    if feature == "classify":
        noise = summary.get("false_positives_noise")
        partial = summary.get("false_positives_partial")
        # 误报拆两类才谈得上修法：全噪声用例里的误报是"把闲聊当信息"，部分期望
        # 用例里的是"同批多分了别条"；只给总数时两者混成一个数，看不出该改哪边。
        split = (
            f"，其中噪声用例 {noise} / 部分期望用例 {partial}"
            if noise is not None and partial is not None
            else ""
        )
        extra = [
            (
                f"  期望分类 {summary.get('expected', 0)} 条 / 模型输出 "
                f"{summary.get('model_classified', 0)} 条"
                f"（误报 {summary.get('false_positives', 0)}{split}）"
            ),
        ]
        echo = summary.get("time_points_echo")
        if echo:
            # 拆分键可能是旧 payload 里没有的（回声点数是更早的键）：缺了显示
            # 「-」而不是补 0，理由同主表
            extra.append(
                f"  时间点回声（期望值=消息发送时间，非正文抽取）: {echo} 点，"
                f"剔回声召回 {format_metric('time_point_recall_ex_echo', summary.get('time_point_recall_ex_echo'))}"
                f"（date-only {format_metric('time_point_recall_date_ex_echo', summary.get('time_point_recall_date_ex_echo'))}"
                f" / 带时刻 {format_metric('time_point_recall_clock_ex_echo', summary.get('time_point_recall_clock_ex_echo'))}）"
            )
        ph_cases = summary.get("placeholder_only_cases")
        if ph_cases:
            extra.append(
                f"  纯占位符用例 {ph_cases} 条（期望 {summary.get('placeholder_only_expected', 0)} 条）"
                "已从主指标剔除；含其召回 "
                + format_metric(
                    "category_recall_incl_placeholder",
                    summary.get("category_recall_incl_placeholder"),
                )
            )
        extra += _mode_lines(summary)
        extra.append(
            f"  用例 {summary.get('cases', 0)} 条（出错 {summary.get('error_cases', 0)}）"
        )
    elif feature in ("dedup", "merge"):
        extra = [
            (
                f"  TP {summary.get('tp', 0)} / FP {summary.get('fp', 0)}"
                f" / TN {summary.get('tn', 0)} / FN {summary.get('fn', 0)}"
            ),
            f"  用例 {summary.get('cases', 0)} 条（出错 {summary.get('error_cases', 0)}）",
        ]
        # 两条功能的 skipped 含义不同，必须分开措辞：dedup 的零判官路径有三种
        # （图片/原文哈希短路、判官负缓存命中、候选门直接拒绝），前两种是**正确
        # 且省钱的判定**，第三种才是"没比成"；merge 的只有余弦预筛门一种。写成
        # 一句"预筛跳过"会把短路误读成漏判。
        if summary.get("skipped"):
            if feature == "dedup":
                extra.append(
                    f"  未触发判官: {summary['skipped']} 条"
                    "（含精确短路/判官负缓存/候选门拒绝，逐条见通道分布）"
                )
            else:
                extra.append(f"  预筛跳过（未触发 AI 判定）: {summary['skipped']} 条")
        # 通道分布 + AI 成本：阈值是判定行为的一部分，不写出这两行就无法把
        # "召回低"归因给判官还是门禁，也看不见放宽门禁换来的 AI 调用代价
        if "ai_judged_cases" in summary:
            extra.append(
                f"  经判官裁决的用例 {summary.get('ai_judged_cases', 0)} 条"
                f"（占比 {format_metric('ai_judged_rate', summary.get('ai_judged_rate'))}；"
                "一例可对多个候选各调一次，不是 API 调用上限"
            )
            channels = [
                f"{channel} {summary[f'channel_{channel}']}"
                for channel in DEDUP_CHANNELS
                if summary.get(f"channel_{channel}")
            ]
            if channels:
                extra.append("  判定通道: " + " / ".join(channels))
            if "cosine_cases" in summary:
                extra.append(
                    f"  余弦链路跑通的用例: {summary.get('cosine_cases', 0)} 条"
                )
    elif feature == "title":
        extra = [
            (
                f"  关键词用例 {summary.get('keyword_hit_cases', 0)} 条 / "
                f"字符相似用例 {summary.get('char_similarity_cases', 0)} 条"
            ),
        ]
        if summary.get("title_only_cases"):
            extra.append(
                f"  仅标题期望用例 {summary['title_only_cases']} 条，"
                f"软通过（字符相似达描述线）{summary.get('title_only_soft_pass', 0)} 条"
            )
        extra.append(
            f"  用例 {summary.get('cases', 0)} 条（出错 {summary.get('error_cases', 0)}）"
        )
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
