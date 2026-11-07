"""HTML 报告版面守卫：图表标题不得超出 SVG 视口，路径不得暴露本机目录。

为什么需要宽度守卫：SVG 里的 `<text>` 超出 `viewBox` 是**静默裁剪**——不报错、
不换行，只是右半边不见了。旧版总览图标题 56 字、14px 粗体，按本文件的估算器约
616px，而它从 x=172 起画只剩 348px 可用，成品被切到「dedup·merg」为止，任何测试
都不会红。

估算器只用来卡"明显过长"，不追求复刻浏览器排版；系数与实测的偏差见下方常量说明。
"""

from __future__ import annotations

import re
import unicodedata

FONT_TITLE = 14
"""图表标题字号（charts.py 里所有 `_text(..., size=14, weight="bold")`）。"""

TITLE_X = 12
"""图表标题的左边距：与空图占位的 `_text(12, …)` 同口径。"""

CJK_EM = 1.08
ASCII_EM = 0.58
"""宽度估算系数，按 Microsoft YaHei 实测校准。

对现有全部标题/标签样本，含中文的标签一律**高估** 0~13%（偏保守，不会漏报）：
CJK 按 1em 起算再乘 1.08（全角括号在雅黑下略宽于汉字）；ASCII 与 East Asian
Width 为 A 的字符（`·`、`≥`）按 0.58em。两个字母加一个数字的纯短拉丁标签
（如混淆矩阵里的 F1）会被低估约 22%，但这类标签本身就远离边界，不影响结论。
守卫只用来卡"明显过长"，不追求复刻浏览器排版。
"""

RIGHT_MARGIN = 8
"""右侧余量：字体回退或抗锯齿差异不应把守卫变成误报。"""


def _est_width(text: str, size: int = FONT_TITLE) -> float:
    total = 0.0
    for ch in text:
        wide = unicodedata.east_asian_width(ch) in ("W", "F")
        total += size * (CJK_EM if wide else ASCII_EM)
    return total


def _svg_blocks(html_text: str) -> list[tuple[int, str]]:
    """返回 (视口宽, svg 片段)。属性顺序按 charts.py 的实际输出匹配。"""
    blocks: list[tuple[int, str]] = []
    for match in re.finditer(r'<svg xmlns="[^"]*" width="(\d+)"', html_text):
        start = match.start()
        end = html_text.find("</svg>", start)
        if end < 0:
            continue
        blocks.append((int(match.group(1)), html_text[start:end]))
    return blocks


def _chart_title(svg: str) -> tuple[float, str]:
    """取图表标题：第一条 14px 粗体 `<text>`（条形图与混淆矩阵都这么画）。"""
    match = re.search(
        r'<text x="(\d+)" y="\d+" font-size="14"[^>]*font-weight="bold">([^<]*)</text>',
        svg,
    )
    assert match is not None, svg[:200]
    return float(match.group(1)), match.group(2)


def _axis_labels(svg: str) -> list[tuple[float, str]]:
    """取条形图 y 轴标签：(锚点 x, 文本)。

    正则按 `text-anchor="end"` 匹配，会连带捕获混淆矩阵的行标签（预测是/预测否，
    右对齐在 x=140 而非标签列右缘 164）——它们同样不该越过画布左缘，一并检查
    没有坏处，只影响失败提示里的计数口径（见守卫测试里的说明）。
    """
    return [
        (float(m.group(1)), m.group(2))
        for m in re.finditer(
            r'<text x="(\d+)" y="\d+" font-size="12"[^>]*text-anchor="end"[^>]*>([^<]*)</text>',
            svg,
        )
    ]


def _payload() -> dict:
    """四个功能齐全的一轮结果，指标值取自真实报告的量级。"""
    return {
        "run_id": "20261107-120000",
        "generated_at": "2026-11-07 12:00:00",
        "model": "briefdesk/qwen2.5:7b-v3",
        "concurrency": 1,
        "elapsed_sec": 2691.8,
        "config": {
            "reasoning_effort": "off",
            "json_mode": "auto",
            "max_classify_tokens": 4096,
            "vision_enabled": False,
            "vision_max_images": 4,
            "embedding_enabled": True,
            "embedding_model": "bge-m3",
            "embedding_batch_size": 10,
            "dedup_similarity_threshold": 0.07,
            "dedup_embed_threshold": 0.8,
            "dedup_embed_fallback_threshold": 0.65,
            "dedup_strong_threshold": 0.99,
            "dedup_embed_top_k": 3,
            "chat_provider": "ollama",
            "embed_provider": "ollama",
        },
        "features": {
            "classify": {
                "dataset": "C:/Users/someone/AppData/Local/briefdesk/benchmark/cases/classify.fromweb.json",
                "elapsed_sec": 2691.8,
                "summary": {
                    "cases": 841, "error_cases": 0, "expected": 736,
                    "model_classified": 745, "false_positives": 118,
                    "false_positives_noise": 39, "false_positives_partial": 79,
                    "category_accuracy": 0.81, "category_precision": 0.80,
                    "category_recall": 0.81, "category_f1": 0.805,
                    "time_msg_accuracy": 0.155, "time_point_recall": 0.19,
                    "time_point_recall_ex_echo": 0.243,
                    "time_point_precision": 0.37, "failure_rate": 0.002,
                    "summary_fill_rate": 1.0, "time_points_echo": 63,
                    "time_point_recall_date_ex_echo": 0.143,
                    "time_point_recall_clock_ex_echo": 0.269,
                    "placeholder_only_cases": 9, "placeholder_only_expected": 9,
                    "category_recall_incl_placeholder": 0.80,
                },
                "cases": [],
            },
            "dedup": {
                "dataset": "C:/Users/someone/AppData/Local/briefdesk/benchmark/cases/dedup.fromweb.json",
                "elapsed_sec": 37.3,
                "summary": {
                    "cases": 249, "error_cases": 0, "accuracy": 0.924,
                    "precision": 0.983, "recall": 0.873, "f1": 0.925,
                    "tp": 117, "fp": 2, "tn": 113, "fn": 17, "skipped": 15,
                    "ai_judged_cases": 208, "ai_judged_rate": 0.835,
                    "channel_cosine": 99, "channel_overlap": 150,
                    "cosine_cases": 249,
                },
                "cases": [],
            },
            "merge": {
                "dataset": "C:/Users/someone/AppData/Local/briefdesk/benchmark/cases/merge.fromweb.json",
                "elapsed_sec": 12.0,
                "summary": {
                    "cases": 90, "error_cases": 0, "accuracy": 0.989,
                    "precision": 1.0, "recall": 0.978, "f1": 0.989,
                    "tp": 43, "fp": 0, "tn": 46, "fn": 1, "skipped": 3,
                },
                "cases": [],
            },
            "title": {
                "dataset": "C:/Users/someone/AppData/Local/briefdesk/benchmark/cases/title.fromweb.json",
                "elapsed_sec": 22.0,
                "summary": {
                    "cases": 63, "error_cases": 0, "keyword_hit_rate": 0.696,
                    "char_similarity_mean": 0.741, "char_similarity_p50": 0.762,
                    "avg_len": 23.79, "too_long_count": 9, "fallback_count": 1,
                    "keyword_hit_cases": 56, "char_similarity_cases": 63,
                    "title_only_cases": 7, "title_only_soft_pass": 5,
                },
                "cases": [],
            },
        },
    }


def test_chart_titles_start_at_canvas_left_edge() -> None:
    """每个图表标题都从 x=12 起画。

    只断言"装得下"是不够的：标题短了以后，即使锚点退回 x=172 也依然装得下，
    测试会全绿，而可用宽悄悄少掉 160px。这条把锚点本身钉死。
    """
    from briefdesk.plugins.benchmark.html_report import build_html_report

    html_text = build_html_report(_payload())
    blocks = _svg_blocks(html_text)
    assert len(blocks) == 7, f"应有 7 张图（1 总览 + 分类 + 去重×2 + 合并×2 + 标题），实得 {len(blocks)}"
    starts = [_chart_title(svg)[0] for _, svg in blocks]
    assert starts == [TITLE_X] * len(blocks), starts


def test_chart_titles_fit_within_viewport() -> None:
    """所有图表标题必须落在自己的 SVG 视口内（含 8px 右边距）。

    这条直接锁住"标题写长句被静默裁掉"的回归：旧版 56 字标题按本文件估算器约
    616px，从 x=172 起画需要 796px，视口只有 520px，超宽 276px。
    """
    from briefdesk.plugins.benchmark.html_report import build_html_report

    html_text = build_html_report(_payload())
    blocks = _svg_blocks(html_text)
    assert len(blocks) == 7, f"SVG 块数不符，守卫会空转：{len(blocks)}"
    overflows = []
    for width, svg in blocks:
        x, title = _chart_title(svg)
        need = x + _est_width(title) + RIGHT_MARGIN
        if need > width:
            overflows.append(f"{title!r}: x={x:.0f} 估算宽={_est_width(title):.0f} "
                             f"需要 {need:.0f}px > 视口 {width}px")
    assert not overflows, "图表标题超出视口：" + "；".join(overflows)


def test_chart_labels_fit_left_of_plot_area() -> None:
    """条形图 y 轴标签不得越过画布左缘。

    标签右对齐到 x=164，长标签会往左溢出画布——同样是静默裁剪。旧版总览标签
    「标题重拟（summarize_title）」12px 按本文件估算器约 182px，左端落在 −18px，
    文字开头的「标题重拟」被削掉了一角。
    """
    from briefdesk.plugins.benchmark.html_report import build_html_report

    html_text = build_html_report(_payload())
    checked = 0
    overflows = []
    for _width, svg in _svg_blocks(html_text):
        for anchor, label in _axis_labels(svg):
            checked += 1
            left = anchor - _est_width(label, size=12)
            if left < 0:
                overflows.append(f"{label!r}: 需要 {_est_width(label, 12):.0f}px，"
                                 f"右端 x={anchor:.0f} → 左端 {left:.0f}px")
    assert checked >= 18, (
        "应检查到总览 4 条 + 各功能条形 22 条 + 混淆矩阵行标签 4 条"
        f"（当前实测 30 条），实得 {checked} 条"
    )
    assert not overflows, "条形图标签越过画布左缘：" + "；".join(overflows)


def test_overview_labels_are_short_and_complete() -> None:
    """总览图标题只写短语，指标名下沉到条形标签。"""
    from briefdesk.plugins.benchmark.html_report import build_html_report

    html_text = build_html_report(_payload())
    overview = next(svg for _, svg in _svg_blocks(html_text) if "各功能核心指标" in svg)
    assert "classify=" not in overview, "指标图例不该塞回标题"
    assert "各功能核心指标" in overview
    for label in ("分类 · 类别准确率", "去重 · 准确率", "合并 · 准确率", "标题 · 关键词命中率"):
        assert label in overview, label


def test_overview_skips_missing_headline_instead_of_zero_bar() -> None:
    """总览图缺头条指标时不画 0.0% 的假条（与 _chart_items 同口径）。"""
    from briefdesk.plugins.benchmark.html_report import build_html_report

    payload = _payload()
    payload["features"]["merge"]["summary"].pop("accuracy")
    html_text = build_html_report(payload)
    overview = next(svg for _, svg in _svg_blocks(html_text) if "各功能核心指标" in svg)
    assert "合并 · 准确率" not in overview
    assert "去重 · 准确率" in overview
    # 真的 0 值仍然照画：占位不能吃掉真实零值
    payload["features"]["merge"]["summary"]["accuracy"] = 0.0
    overview = next(
        svg for _, svg in _svg_blocks(build_html_report(payload)) if "各功能核心指标" in svg
    )
    assert "合并 · 准确率" in overview


def test_dataset_line_shows_only_tail_segments() -> None:
    """数据集只显示 `cases/xxx.json` 或裸文件名，绝不暴露盘符与用户名。"""
    from briefdesk.plugins.benchmark.html_report import (
        _dataset_short,
        build_html_report,
    )

    html_text = build_html_report(_payload())
    assert "cases/classify.fromweb.json" in html_text
    assert "Users" not in html_text
    assert ":\\" not in html_text
    assert "C:/" not in html_text
    # 短绝对路径同样要脱敏：末两级恰好是「用户名/文件名」时不能整段留下
    assert _dataset_short(r"C:\Users\someone\classify.json") == "classify.json"
    assert _dataset_short(r"C:\classify.json") == "classify.json"
    assert _dataset_short("/home/someone/cases/dedup.json") == "cases/dedup.json"
    assert _dataset_short("cases/classify.json") == "cases/classify.json"
    assert _dataset_short("") == "-"


def test_extra_rows_skip_absent_counts_instead_of_fake_zero() -> None:
    """补充行缺键不出行：旧 payload 没有 `char_similarity_cases` 时不能印「0 条」。

    补 0 的读法是「这轮测了但一条都没命中」，而真相是那一轮根本没记录该口径——
    与主表「缺键显示 -」同一个理由。旧版 payload 的 title 摘要就只带
    `keyword_hit_cases`（见 tests/test_benchmark_measurement.py 的
    `TestReportBackwardCompatibility`）。
    """
    from briefdesk.plugins.benchmark.report import feature_extra_rows

    rows = dict(feature_extra_rows("title", {"cases": 63, "keyword_hit_cases": 56}))
    assert rows["用例构成"] == "关键词 56 条"
    # 两个计数都没有 → 整行不出现，而不是空值或「0 条」
    assert "用例构成" not in dict(feature_extra_rows("title", {"cases": 63}))
    # 都有则两段并列
    rows = dict(
        feature_extra_rows("title", {"keyword_hit_cases": 56, "char_similarity_cases": 63})
    )
    assert rows["用例构成"] == "关键词 56 条 / 字符相似 63 条"


def test_core_counts_show_dash_instead_of_fake_zero() -> None:
    """早期就有的核心计数缺键时显示「-」，不是 0——两条渲染路径必须同口径。"""
    from briefdesk.plugins.benchmark.report import case_count_text, feature_extra_rows

    rows = dict(feature_extra_rows("classify", {}))
    assert rows["期望分类"] == "-", rows
    assert rows["模型输出"] == "-（误报 -）", rows
    rows = dict(feature_extra_rows("dedup", {}))
    assert rows["混淆矩阵"] == "TP - / FP - / TN - / FN -", rows
    # 出错数缺失时不写「出错 -」：正常 payload 恒有，缺了就是那段信息不存在
    assert case_count_text({"cases": 3}) == "用例 3 条"
    assert case_count_text({"cases": 3, "error_cases": 1}) == "用例 3 条（出错 1）"
    assert case_count_text({}) == "用例 -"


def test_section_meta_carries_cases_and_elapsed() -> None:
    """用例数/出错数/用时在段首 meta 行，不再占指标表两行。

    用时标签与终端 `render_feature_block` 的尾行保持一致（都叫「测试用时」）：
    同一个值在两处换名字，读者会当成两个指标。
    """
    from briefdesk.plugins.benchmark.html_report import _section_meta

    data = _payload()["features"]["dedup"]
    meta = _section_meta(data, data["summary"])
    assert "数据集 cases/dedup.fromweb.json" in meta
    assert "用例 249 条（出错 0）" in meta
    assert "测试用时 37.3s" in meta
    # 缺用时（旧 payload）不写"测试用时 -"
    meta = _section_meta({"dataset": "cases/dedup.json"}, {"cases": 1, "error_cases": 0})
    assert "测试用时" not in meta
    # 缺用例数时与终端同口径显示「-」，不补 0
    assert "用例 -" in _section_meta({"dataset": "cases/dedup.json"}, {})
