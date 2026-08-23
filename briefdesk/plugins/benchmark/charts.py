"""纯 Python SVG 图表生成 — 无第三方绘图依赖，产物可嵌入自包含 HTML 报告。

所有函数返回完整 <svg> 字符串（可独立打开），中文文本由浏览器按系统字体渲染。
"""

from __future__ import annotations

import html
from collections.abc import Callable

_CHART_WIDTH = 520
_LABEL_COL = 172  # 条形图标签列右缘（x 坐标）
_PLOT_RIGHT = 470  # 条形图绘图区右缘
_ROW_H = 26  # 条形图每行高度
_CHART_TOP = 42  # 条形图第一行中心 y


def _fmt_pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def _text(
    x: float,
    y: float,
    content: str,
    *,
    size: int = 12,
    fill: str = "#374151",
    anchor: str = "start",
    weight: str = "normal",
) -> str:
    return (
        f'<text x="{x:.0f}" y="{y:.0f}" font-size="{size}" fill="{fill}" '
        f'text-anchor="{anchor}" font-weight="{weight}">'
        f"{html.escape(content)}</text>"
    )


def _empty_svg(title: str, width: int) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="52" '
        f'viewBox="0 0 {width} 52">'
        f'{_text(12, 26, title, size=14, fill="#1f2937", weight="bold")}'
        f'{_text(12, 44, "（无数据）", fill="#9ca3af")}'
        "</svg>"
    )


def bar_chart_svg(
    title: str,
    items: list[tuple[str, float]],
    *,
    value_max: float = 1.0,
    value_fmt: Callable[[float], str] = _fmt_pct,
    color: str = "#2563EB",
    width: int = _CHART_WIDTH,
) -> str:
    """横向条形图：items 为 (标签, 数值) 列表；value_max 为满刻度值。

    标签在左侧（text-anchor=end），条形 + 数值在右侧；数值默认按百分比显示。
    """
    if not items:
        return _empty_svg(title, width)
    height = _CHART_TOP + len(items) * _ROW_H + 12
    max_v = max(value_max, max(v for _, v in items), 1e-9)
    plot_left = _LABEL_COL
    plot_right = _PLOT_RIGHT
    parts = [
        (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
            f'viewBox="0 0 {width} {height}">'
        ),
        _text(plot_left, 18, title, size=14, fill="#1f2937", weight="bold"),
    ]
    for i, (label, value) in enumerate(items):
        y = _CHART_TOP + i * _ROW_H
        v = max(0.0, min(float(value), max_v))
        bar_w = (v / max_v) * (plot_right - plot_left)
        parts.append(
            _text(plot_left - 8, y, label, fill="#374151", anchor="end")
        )
        parts.append(
            f'<rect x="{plot_left}" y="{y - 10}" width="{bar_w:.1f}" '
            f'height="14" rx="2" fill="{color}"/>'
        )
        parts.append(
            _text(plot_left + bar_w + 6, y, value_fmt(v), fill="#111827")
        )
    parts.append("</svg>")
    return "".join(parts)


def confusion_svg(
    title: str,
    tp: int,
    fp: int,
    tn: int,
    fn: int,
    *,
    width: int = 420,
) -> str:
    """2×2 混淆矩阵热力图：行=预测，列=期望；颜色按占比加深（绿=正确，红=错误）。"""
    total = max(1, tp + fp + tn + fn)
    cell = 64
    origin_x = 150
    origin_y = 46
    parts = [
        (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
            f'height="{origin_y + 2 * cell + 34}" viewBox="0 0 {width} '
            f'{origin_y + 2 * cell + 34}">'
        ),
        _text(origin_x, 18, title, size=14, fill="#1f2937", weight="bold"),
    ]
    for col, label in ((0, "期望是"), (1, "期望否")):
        cx = origin_x + col * cell + cell / 2
        parts.append(_text(cx, origin_y - 8, label, fill="#4b5563", anchor="middle"))
    rows: list[tuple[str, list[tuple[str, int, bool]]]] = [
        ("预测是", [("TP", tp, True), ("FP", fp, False)]),
        ("预测否", [("FN", fn, False), ("TN", tn, True)]),
    ]
    for row, (row_label, cells) in enumerate(rows):
        ry = origin_y + row * cell
        parts.append(
            _text(origin_x - 10, ry + cell / 2 + 4, row_label, fill="#4b5563", anchor="end")
        )
        for col, (name, count, good) in enumerate(cells):
            x = origin_x + col * cell
            ratio = count / total
            base = "34,197,94" if good else "239,68,68"
            alpha = 0.10 + 0.75 * ratio
            parts.append(
                f'<rect x="{x}" y="{ry}" width="{cell - 6}" height="{cell - 6}" '
                f'rx="4" fill="rgba({base},{alpha:.2f})" stroke="#e5e7eb"/>'
            )
            cx = x + (cell - 6) / 2
            parts.append(
                _text(cx, ry + cell / 2, str(count), size=18, fill="#111827", anchor="middle", weight="bold")
            )
            parts.append(_text(cx, ry + cell / 2 + 18, name, size=11, fill="#6b7280", anchor="middle"))
    return "".join(parts) + "</svg>"


def overview_bar_svg(
    title: str,
    items: list[tuple[str, float, str]],
    *,
    width: int = _CHART_WIDTH,
) -> str:
    """多色横向条形图（总览用）：items 为 (标签, 数值, 颜色)。"""
    if not items:
        return _empty_svg(title, width)
    height = _CHART_TOP + len(items) * _ROW_H + 12
    plot_left = _LABEL_COL
    plot_right = _PLOT_RIGHT
    max_v = max(max(v for _, v, _ in items), 1e-9)
    parts = [
        (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
            f'viewBox="0 0 {width} {height}">'
        ),
        _text(plot_left, 18, title, size=14, fill="#1f2937", weight="bold"),
    ]
    for i, (label, value, color) in enumerate(items):
        y = _CHART_TOP + i * _ROW_H
        v = max(0.0, min(float(value), max_v))
        bar_w = (v / max_v) * (plot_right - plot_left)
        parts.append(_text(plot_left - 8, y, label, fill="#374151", anchor="end"))
        parts.append(
            f'<rect x="{plot_left}" y="{y - 10}" width="{bar_w:.1f}" '
            f'height="14" rx="2" fill="{color}"/>'
        )
        parts.append(
            _text(plot_left + bar_w + 6, y, _fmt_pct(v), fill="#111827")
        )
    parts.append("</svg>")
    return "".join(parts)
