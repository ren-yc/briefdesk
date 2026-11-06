"""自包含 HTML 图表报告 — 内联 CSS + SVG 图表，无外部资源，浏览器直接打开。

输入与 JSON 导出共用同一 payload（engine._build_payload 产物）：
{"run_id", "generated_at", "model", "concurrency", "config",
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
from briefdesk.plugins.benchmark.metrics import DEDUP_CHANNELS, TITLE_SOFT_RATIO
from briefdesk.plugins.benchmark.report import (
    _FEATURE_METRIC_LABELS,
    _FEATURE_TITLES,
    format_metric,
)

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


def _provider_text(config: dict[str, Any], key: str) -> str:
    """供应商归类的展示文本：三值枚举 + 两种缺省。

    - 键不存在 →「-」：那是字段引入之前跑的历史报告，不能假装"未配置"；
    - 值为空串 →「未配置」：本轮确实没配这一路（如嵌入留空）；
    - 其余 → `ollama` / `deepseek` / `custom`。

    只可能显示这三个词——完整地址含主机名与端口，属隐私面，永不入报告
    （归类逻辑见 engine.provider_label）。
    """
    if key not in config:
        return "-"
    label = config.get(key)
    if not isinstance(label, str) or not label:
        return "未配置"
    return label


def _header(payload: dict[str, Any]) -> str:
    config = payload.get("config") or {}
    vision = "开启" if config.get("vision_enabled") else "关闭"
    embedding = "开启" if config.get("embedding_enabled") else "关闭"
    meta = [
        ("运行时间", payload.get("generated_at", "")),
        ("模型", payload.get("model", "")),
        ("对话来源", _provider_text(config, "chat_provider")),
        ("嵌入来源", _provider_text(config, "embed_provider")),
        ("并发", str(payload.get("concurrency", ""))),
        ("推理强度", payload.get("reasoning_effort", "")),
        ("JSON 模式", config.get("json_mode", "")),
        ("分类最大输出", config.get("max_classify_tokens", "")),
        ("视觉分类", vision),
        ("视觉图片上限", config.get("vision_max_images", "")),
        ("嵌入去重", embedding),
        ("嵌入模型", config.get("embedding_model", "")),
        ("嵌入批大小", config.get("embedding_batch_size", "")),
        ("去重阈值", (
            f"字符 {config.get('dedup_similarity_threshold', '')} / "
            f"嵌入 {config.get('dedup_embed_threshold', '')} / "
            f"回退 {config.get('dedup_embed_fallback_threshold', '')} / "
            f"强匹配 {config.get('dedup_strong_threshold', '')} / "
            f"Top-K {config.get('dedup_embed_top_k', '')}"
        )),
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


def _metric_value(key: str, value: Any) -> str:
    # 与终端报告共用同一套口径（比例 / 字数 / 计数），否则两处渲染会各说各话
    return _esc(format_metric(key, value))


def _fmt(summary: dict[str, Any], key: str) -> str:
    """按指标口径格式化 summary[key]（缺键显示「-」，与主表同口径）。

    补 0 会让"这一轮没测过该指标"读成"测了是零"——补充行虽然多数被父键
    存在性守着，但守的是父键（如回声点数），不保证拆分键本身在旧 payload 里
    也有。
    """
    return format_metric(key, summary[key]) if key in summary else "-"


def _metric_table(feature: str, summary: dict[str, Any], elapsed_sec: float | None = None) -> str:
    rows: list[str] = []
    for key, label in _FEATURE_METRIC_LABELS.get(feature, []):
        # 与终端主表同口径：缺键显示「-」而不是补 0——旧报告里没记录的指标
        # 若渲染成 0.0%，会被读成"测过且为零"（与 docs/architecture.md 的
        # 历史 payload 兼容约定一致）
        text = _metric_value(key, summary[key]) if key in summary else "-"
        rows.append(f"<tr><td>{_esc(label)}</td><td>{text}</td></tr>")
    extras: list[tuple[str, str]] = []
    if feature == "classify":
        noise = summary.get("false_positives_noise")
        partial = summary.get("false_positives_partial")
        # 误报拆两类才谈得上修法：全噪声用例里的误报是"把闲聊当信息"，部分期望
        # 用例里的是"同批多分了别条"；合成一个数就只是扣分，看不出该改哪边。
        fp_text = f"{summary.get('false_positives', 0)} 条"
        if noise is not None and partial is not None:
            fp_text += f"（噪声用例 {noise} / 部分期望用例 {partial}）"
        extras = [
            ("期望分类", f"{summary.get('expected', 0)} 条"),
            ("模型输出", f"{summary.get('model_classified', 0)} 条（误报 {fp_text}）"),
        ]
        echo = summary.get("time_points_echo")
        if echo:
            extras.append(
                (
                    "时间点回声",
                    f"{echo} 点是消息自己的发送时间（非正文抽取），已从解析能力口径剔除",
                )
            )
            extras.append(
                (
                    "回声口径拆分",
                    (
                        f"剔回声后 date-only {_fmt(summary, 'time_point_recall_date_ex_echo')}"
                        f" / 带时刻 {_fmt(summary, 'time_point_recall_clock_ex_echo')}"
                    ),
                )
            )
        if summary.get("placeholder_only_cases"):
            extras.append(
                (
                    "纯占位符用例",
                    (
                        f"{summary['placeholder_only_cases']} 条（期望 "
                        f"{summary.get('placeholder_only_expected', 0)} 条）不计入主指标；"
                        f"含其召回 {_fmt(summary, 'category_recall_incl_placeholder')}"
                    ),
                )
            )
    elif feature in ("dedup", "merge"):
        extras = [
            ("混淆矩阵", f"TP {summary.get('tp', 0)} / FP {summary.get('fp', 0)} / TN {summary.get('tn', 0)} / FN {summary.get('fn', 0)}"),
        ]
        # 两条功能的 skipped 含义不同：dedup 的零判官路径有三种（图片/原文哈希
        # 短路、判官负缓存命中、候选门直接拒绝），前两种是正确且省钱的判定，第三种
        # 才是"没比成"；merge 只有余弦预筛门一种。标签写死"预筛跳过"会把短路误读成漏判。
        if summary.get("skipped"):
            if feature == "dedup":
                extras.append(
                    (
                        "未触发判官",
                        (
                            f"{summary['skipped']} 条"
                            "（含精确短路/判官负缓存/候选门拒绝，逐条见下方通道列）"
                        ),
                    )
                )
            else:
                extras.append(("预筛跳过", f"{summary['skipped']} 条（未触发 AI 判定）"))
        # 通道分布与 AI 成本只属于 dedup（merge 无通道概念）。不写出来的后果：
        # 阈值放宽使 AI 调用翻倍、或整轮其实没走余弦，报告上都长得一样。
        if "ai_judged_cases" in summary:
            extras.append(
                (
                    "经判官裁决",
                    (
                        f"{summary.get('ai_judged_cases', 0)} 条"
                        f"（占比 {_fmt(summary, 'ai_judged_rate')}；"
                        "一例可对多个候选各调一次判官，本数不是 API 调用上限"
                    ),
                )
            )
            distribution = " / ".join(
                f"{channel} {summary[f'channel_{channel}']}"
                for channel in DEDUP_CHANNELS
                if summary.get(f"channel_{channel}")
            )
            if distribution:
                extras.append(("判定通道", distribution))
            if "cosine_cases" in summary:
                extras.append(
                    ("余弦链路跑通", f"{summary.get('cosine_cases', 0)} 条（配置与实走不一致时以此核对）")
                )
    elif feature == "title":
        extras = [
            ("平均长度", f"{summary.get('avg_len', 0):.1f} 字"),
            ("超长/回退", f"{summary.get('too_long_count', 0)} / {summary.get('fallback_count', 0)} 条"),
        ]
        if summary.get("title_only_cases"):
            extras.append(
                (
                    "仅标题期望用例",
                    (
                        f"{summary['title_only_cases']} 条，软通过"
                        f"（字符相似 ≥{TITLE_SOFT_RATIO:.2f}）"
                        f"{summary.get('title_only_soft_pass', 0)} 条；"
                        "字符相似是描述指标，不作门槛"
                    ),
                )
            )
    extras.append(("用例", f"{summary.get('cases', 0)} 条（出错 {summary.get('error_cases', 0)}）"))
    extras.append(("测试用时", _dur(elapsed_sec)))
    for label, value in extras:
        rows.append(f"<tr><td>{_esc(label)}</td><td>{_esc(value)}</td></tr>")
    return f"<table><tr><th>指标</th><th>值</th></tr>{''.join(rows)}</table>"


def _mode_table(summary: dict[str, Any]) -> str:
    """classify 的分 mode 小表；旧 payload 没有 by_mode → 不输出空表。"""
    by_mode = summary.get("by_mode")
    if not isinstance(by_mode, dict) or not by_mode:
        return ""
    rows = []
    for mode, stats in by_mode.items():
        if not isinstance(stats, dict):
            continue
        rows.append(
            "<tr>"
            f"<td>{_esc(mode)}</td>"
            f"<td>{_esc(stats.get('cases', 0))}</td>"
            f"<td>{_esc(stats.get('hits', 0))}/{_esc(stats.get('expected', 0))}</td>"
            f"<td>{_esc(format_metric('category_recall', stats.get('category_recall', 0)))}</td>"
            f"<td>{_esc(format_metric('category_precision', stats.get('category_precision', 0)))}</td>"
            f"<td>{_esc(stats.get('false_positives', 0))}</td>"
            "</tr>"
        )
    if not rows:
        return ""
    return (
        "<p>按 mode 切片（同一轮里词表外样本与生产样本混算出的总分既不代表能力"
        "也不代表上限，验收须按切片读数）</p>"
        "<table><tr><th>mode</th><th>用例</th><th>命中/期望</th>"
        "<th>召回</th><th>精确</th><th>误报</th></tr>"
        f"{''.join(rows)}</table>"
    )


def _chart_items(
    summary: dict[str, Any], spec: list[tuple[str, str]]
) -> list[tuple[str, float]]:
    """把 (标签, 键名) 规格翻成图表数据，**缺失的指标不画**。

    为什么不能补 0：历史 payload 没有本轮新增的键，补 0 会画出一根标着
    「0.0%」的条，和主表补 0 是同一个误读——"没测过"看起来像"测了是零"。
    图表没有「-」的位置，缺的指标直接不出现；全部缺失时 `bar_chart_svg`
    自己会退化成占位图。

    判据是 `summary.get(key) is not None` 而不是 `key in summary`：键存在
    但值为 null 的 payload（JSON 里 `null` 反序列化成 None）与缺键同义，
    否则主表显示「-」、图表却画 0.0%，同一份报告自相矛盾。
    """
    return [
        (label, float(summary[key]))
        for label, key in spec
        if summary.get(key) is not None
    ]


def _feature_charts(feature: str, summary: dict[str, Any]) -> str:
    if feature == "classify":
        items = _chart_items(summary, [
            ("类别准确率", "category_accuracy"),
            ("类别精确率", "category_precision"),
            ("类别召回率", "category_recall"),
            ("类别 F1", "category_f1"),
            ("时间点完全一致率", "time_msg_accuracy"),
            ("时间点召回率(含回声)", "time_point_recall"),
            ("时间点召回率(剔回声)", "time_point_recall_ex_echo"),
            ("时间点精确率", "time_point_precision"),
            ("本轮失败率", "failure_rate"),
            ("标题概括填充率", "summary_fill_rate"),
        ])
        svg = bar_chart_svg("分类指标", items)
        return f'<div class="chart">{svg}</div>'
    if feature in ("dedup", "merge"):
        # AI 判定占比只在 dedup 出现（merge 摘要没有该键）：候选门阈值一改它就用
        # 跳变，图上没有这根条就只能从指标表里数出来；缺键时由 _chart_items 跳过
        items = _chart_items(summary, [
            ("准确率", "accuracy"),
            ("精确率", "precision"),
            ("召回率", "recall"),
            ("F1", "f1"),
            ("判官裁决用例占比", "ai_judged_rate"),
        ])
        bar = bar_chart_svg(
            "判定指标",
            items,
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
    # 字符相似是描述线（0~1 的比值，和比率同刻度）：标题为开放生成，
    # 逐字相等只衡量抄写不衡量质量，故不再画精确匹配率
    items = _chart_items(summary, [
        ("关键词命中率", "keyword_hit_rate"),
        ("字符相似度均值", "char_similarity_mean"),
        ("字符相似度中位数", "char_similarity_p50"),
    ])
    svg = bar_chart_svg(
        "标题指标",
        items,
        color=_FEATURE_COLORS["title"],
    )
    return f'<div class="chart">{svg}</div>'


def _title_ok(case: dict[str, Any]) -> tuple[bool, bool]:
    """标题用例的 (硬通过, 软通过)。

    硬判定只有关键词命中：逐字相等对开放生成没有判别力（当前数据集 63 例只有
    1 例逐字相同，把它当质量读数会把结论带偏）。
    软判定给「只有标题期望」的用例：字符相似达描述线算软通过，明细里同时显示
    相似度，读者看得见这是宽松判定而不是漏判。

    旧 payload 没有 `char_similarity` 键（该字段是本轮新增的逐例派生分）：
    那时逐字相等就是当时的通过条件，落在它上面的历史用例不能被重渲染成失败，
    因此按旧口径判为软通过。新 payload 一定有该键（`dataclasses.asdict` 会写入，
    空值为 null），故这条回退只对历史报告生效，不参与新报告的判定。
    """
    output = case.get("output")
    if output is None:
        return False, False
    keywords = case.get("expected_keywords") or []
    if keywords:
        return bool(all(k in output for k in keywords)), False
    if "char_similarity" not in case:
        expected_title = case.get("expected_title")
        return False, bool(expected_title) and output == expected_title
    similarity = case.get("char_similarity")
    soft = isinstance(similarity, (int, float)) and similarity >= TITLE_SOFT_RATIO
    return False, soft


def _verdict(feature: str, case: dict[str, Any]) -> str:
    """逐用例结论：✔ / ✘ / 错误（title 另有「软」）。"""
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
        hard, soft = _title_ok(case)
        if hard:
            return '<span class="ok">✔</span>'
        if soft:
            return '<span class="warn">软</span>'
        return '<span class="bad">✘</span>'
    return '<span class="ok">✔</span>' if ok else '<span class="bad">✘</span>'


def _case_rows(feature: str, cases: list[dict[str, Any]]) -> str:
    if feature == "classify":
        headers = [
            "用例", "mode", "结论", "类别命中", "模型输出", "误报",
            "时间完全一致", "回声点", "失败", "说明",
        ]
        rows = []
        for c in cases:
            # 纯占位符用例在主指标之外，但逐行仍要看得见——它们的 case_id 也印在
            # 指标段里，两处口径必须能对上
            note = c.get("error") or ""
            if c.get("placeholder_only"):
                note = f"{note} 纯占位符（不进主指标）".strip()
            rows.append(
                "<tr>"
                f"<td>{_esc(c.get('case_id'))}</td>"
                f"<td>{_esc(c.get('mode', ''))}</td>"
                f"<td>{_verdict(feature, c)}</td>"
                f"<td>{_esc(c.get('hits', 0))}/{_esc(c.get('expected_count', 0))}</td>"
                f"<td>{_esc(c.get('model_count', 0))}</td>"
                f"<td>{_esc(c.get('false_positives', 0))}</td>"
                f"<td>{_esc(c.get('time_msgs_ok', 0))}/{_esc(c.get('time_msgs_expected', 0))}</td>"
                f"<td>{_esc(c.get('time_points_echo', 0))}</td>"
                f"<td>{_esc(c.get('failed', 0))}</td>"
                f"<td class=\"muted\">{_esc(note)}</td>"
                "</tr>"
            )
    elif feature in ("dedup", "merge"):
        channel_col = any(c.get("channel") for c in cases)
        headers = (
            ["用例", "结论", "判定", "期望", "预筛跳过", "通道", "余弦跑通", "说明"]
            if channel_col
            else ["用例", "结论", "判定", "期望", "预筛跳过", "说明"]
        )
        rows = []
        for c in cases:
            # dedup 的「预筛跳过」= 候选门禁未触发 AI；merge 的 = 余弦预筛门拦下
            skipped = "是" if c.get("skipped") else ""
            cells = [
                _esc(c.get("case_id")),
                _verdict(feature, c),
                _esc(c.get("predicted")),
                _esc(c.get("expected")),
                _esc(skipped),
            ]
            if channel_col:
                # 通道是判重的证据来源：配置写了嵌入、明细却是整列 overlap，
                # 一眼就能看出这轮量的不是语义判重
                cells.append(_esc(c.get("channel", "")))
                cells.append("是" if c.get("cosine_ran") else "")
            cells.append(_esc(c.get("error", "")))
            rows.append("<tr>" + "".join(f"<td>{cell}</td>" for cell in cells) + "</tr>")
    else:  # title
        headers = ["用例", "结论", "输出", "期望标题", "关键词", "字符相似", "说明"]
        rows = []
        for c in cases:
            output = c.get("output")
            if output is None:
                output_html = '<span class="warn">回退原标题</span>'
            else:
                output_html = _esc(output)
            similarity = c.get("char_similarity")
            sim_text = (
                f"{similarity:.2f}"
                if isinstance(similarity, (int, float))
                else "-"
            )
            rows.append(
                "<tr>"
                f"<td>{_esc(c.get('case_id'))}</td>"
                f"<td>{_verdict(feature, c)}</td>"
                f"<td>{output_html}</td>"
                f"<td>{_esc(c.get('expected_title', ''))}</td>"
                f"<td>{_esc('、'.join(c.get('expected_keywords') or []))}</td>"
                f"<td>{_esc(sim_text)}</td>"
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
    ]
    if feature == "classify":
        parts.append(_mode_table(summary))
    parts.append(_case_rows(feature, cases))
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
