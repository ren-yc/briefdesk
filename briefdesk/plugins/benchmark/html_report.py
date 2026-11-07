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
from briefdesk.plugins.benchmark.metrics import TITLE_SOFT_RATIO
from briefdesk.plugins.benchmark.report import (
    _FEATURE_METRIC_LABELS,
    _FEATURE_TITLES,
    case_count_text,
    elapsed_text,
    feature_extra_rows,
    format_metric,
)

_CSS = """
body { font-family: "Microsoft YaHei", "PingFang SC", sans-serif; margin: 24px;
       color: #111827; background: #f9fafb; }
h1 { font-size: 22px; }
h2 { font-size: 17px; margin-top: 28px; border-bottom: 2px solid #e5e7eb;
     padding-bottom: 4px; }
.meta { color: #4b5563; font-size: 13px; margin: 4px 0; }
.meta-label { background: #e5e7eb; color: #374151; border-radius: 4px;
              padding: 1px 6px; margin-right: 8px; font-size: 12px; }
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
    """报告头：运行状态与配置分两行，避免一行过长折成两截。

    分组不是排版洁癖：一行 2000px 在常见窗口里会从中间折行，"总用时"这种结论性
    字段会被折到第二行末尾、和它前缀的配置项混在一起读。
    """
    config = payload.get("config") or {}
    vision = "开启" if config.get("vision_enabled") else "关闭"
    embedding = "开启" if config.get("embedding_enabled") else "关闭"
    run_meta = [
        ("运行时间", payload.get("generated_at", "")),
        ("模型", payload.get("model", "")),
        ("对话来源", _provider_text(config, "chat_provider")),
        ("嵌入来源", _provider_text(config, "embed_provider")),
        ("并发", str(payload.get("concurrency", ""))),
        ("推理强度", payload.get("reasoning_effort", "")),
        ("总用时", _dur(payload.get("elapsed_sec"))),
    ]
    config_meta = [
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
    ]
    parts = ["<h1>LLM 功能基准报告</h1>"]
    for label, meta in (("运行", run_meta), ("配置", config_meta)):
        parts.append(
            f'<p class="meta"><span class="meta-label">{label}</span>'
            + "&nbsp;·&nbsp;".join(
                f"<b>{_esc(k)}</b> {_esc(v)}" for k, v in meta
            )
            + "</p>"
        )
    return "".join(parts)


_FEATURE_COLORS = {
    "classify": "#2563EB",
    "dedup": "#7C3AED",
    "merge": "#D97706",
    "title": "#059669",
}

# 总览图的条形标签＝功能名 · 头条指标名。标题只写「各功能核心指标」，由每条自己
# 说明比的是什么——图例塞进标题会让标题长到被 SVG 视口裁掉。
_OVERVIEW_METRICS = {
    "classify": "类别准确率",
    "dedup": "准确率",
    "merge": "准确率",
    "title": "关键词命中率",
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
        value = summary.get(headline_keys[feature])
        # 缺键/None 不画这根条：补 0 会得到一根标着「0.0%」的条，把"这轮没测"
        # 读成"测了是零"——与 _chart_items 同一口径
        if not isinstance(value, (int, float)):
            continue
        items.append(
            (
                f"{_FEATURE_TITLES[feature]} · {_OVERVIEW_METRICS[feature]}",
                float(value),
                _FEATURE_COLORS[feature],
            )
        )
    if not items:
        return ""
    return (
        '<div class="chart">'
        + overview_bar_svg("各功能核心指标", items)
        + "</div>"
    )


def _metric_value(key: str, value: Any) -> str:
    # 与终端报告共用同一套口径（比例 / 字数 / 计数），否则两处渲染会各说各话
    return _esc(format_metric(key, value))


def _metric_table(feature: str, summary: dict[str, Any]) -> str:
    """指标表：主指标恒占一行（缺键「-」）+ 该功能的补充行。

    补充行与终端报告共用 `feature_extra_rows`，两处只有一个文案源。用例数、
    出错数与用时挪到段首 meta 行（见 `_section_meta`），表格只留指标本身。
    """
    rows: list[str] = []
    for key, label in _FEATURE_METRIC_LABELS.get(feature, []):
        # 与终端主表同口径：缺键显示「-」而不是补 0——旧报告里没记录的指标
        # 若渲染成 0.0%，会被读成"测过且为零"（与 docs/architecture.md 的
        # 历史 payload 兼容约定一致）
        text = _metric_value(key, summary[key]) if key in summary else "-"
        rows.append(f"<tr><td>{_esc(label)}</td><td>{text}</td></tr>")
    for label, value in feature_extra_rows(feature, summary):
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
    """该功能的图表。条形图标题一律「<功能>指标」：同报告里两张图同名就分不清。

    混淆矩阵是例外——它画的是判定分布不是比率，标题只写「混淆矩阵」，行列含义
    由图内的「期望是/期望否」「预测是/预测否」四个轴标签表达。
    """
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
        svg = bar_chart_svg(f"{_FEATURE_TITLES['classify']}指标", items)
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
            f"{_FEATURE_TITLES[feature]}指标",
            items,
            color=_FEATURE_COLORS[feature],
        )
        # 轴标签已写明「期望是/期望否」「预测是/预测否」，标题不必重复一遍
        matrix = confusion_svg(
            "混淆矩阵",
            int(summary.get("tp", 0)),
            int(summary.get("fp", 0)),
            int(summary.get("tn", 0)),
            int(summary.get("fn", 0)),
        )
        return f'<div class="chart">{bar}</div><div class="chart">{matrix}</div>'
    if feature != "title":
        # 未知功能不猜格式：静默落进 title 分支会印出「标题指标 / 用例构成」这种
        # 张冠李戴的图，比空图更难发现。空串让调用方少一段，问题在标题处立刻可见。
        return ""
    # 字符相似是描述线（0~1 的比值，和比率同刻度）：标题为开放生成，
    # 逐字相等只衡量抄写不衡量质量，故不再画精确匹配率
    items = _chart_items(summary, [
        ("关键词命中率", "keyword_hit_rate"),
        ("字符相似度均值", "char_similarity_mean"),
        ("字符相似度中位数", "char_similarity_p50"),
    ])
    svg = bar_chart_svg(
        f"{_FEATURE_TITLES['title']}指标",
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


_DATASET_DIR_NAMES = frozenset({"cases"})
"""数据集文件的常规父目录名。只有父目录是它时才保留两级，其余一律只留文件名。

为什么不能一律留末两级：`--dataset` 接受任意路径，`C:\\Users\\<用户名>\\classify.json`
的末两级恰好是 `<用户名>/classify.json`——用户名照样进报告，等于没脱敏。
"""


def _dataset_short(value: object) -> str:
    """数据集只显示 `cases/xxx.json` 或裸文件名，绝不带盘符与用户目录。

    完整路径带盘符与用户名。报告按 README 可另存分享，绝对路径是不必要的环境
    暴露；文件名已足以定位是哪个数据集，完整路径仍在 report.json 里（CLI 的
    dry-run 也会打印）。
    """
    text = str(value or "")
    if not text:
        return "-"
    parts = [p for p in text.replace("\\", "/").split("/") if p]
    if not parts:
        return "-"
    if len(parts) >= 2 and parts[-2] in _DATASET_DIR_NAMES:
        return "/".join(parts[-2:])
    return parts[-1]


def _section_meta(data: dict[str, Any], summary: dict[str, Any]) -> str:
    """段首一行：数据集 · 用例数（出错数）· 用时。

    用例数与用时原先压在指标表末尾，与"指标"不是一类东西；前置成一行后表格只留
    指标本身。数据集本来就是段首独立一行，一并并进来，省掉一整行。
    """
    parts = [f"数据集 {_esc(_dataset_short(data.get('dataset')))}"]
    parts.append(_esc(case_count_text(summary)))
    elapsed = data.get("elapsed_sec")
    if elapsed is not None:
        parts.append(_esc(elapsed_text(elapsed)))
    return '<p class="dataset">' + "&nbsp;·&nbsp;".join(parts) + "</p>"


def _feature_section(feature: str, data: dict[str, Any]) -> str:
    summary = data.get("summary") or {}
    cases = data.get("cases") or []
    parts = [
        f"<h2>{_esc(_FEATURE_TITLES.get(feature, feature))}</h2>",
        _section_meta(data, summary),
        _feature_charts(feature, summary),
        _metric_table(feature, summary),
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
