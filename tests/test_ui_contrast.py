"""前端配色对比度守卫测试（WCAG AA）。

背景：本项目的深浅两套配色都写在 ``ui/style.css`` 的 ``:root`` 与
``[data-theme="dark"]`` 两个令牌块里。历史上出现过两类回归：

1. 文本令牌（``--text-2`` / ``--text-3``）为了"更淡一点"被逐步调浅，
   最终跌破 4.5:1；
2. 深色模式漏配某个令牌，浅色值直接生效（``.plugin-status-*``、
   ``.env-badge-*`` 都曾这样静默失效）。

因此这里做三件事：解析令牌 → 计算对比度 → 断言阈值与两块的键对称性。
纯静态解析，不依赖浏览器。

阈值（WCAG 2.1）：正文与小字 4.5:1（AA），UI 边界与图形 3:1（1.4.11）。
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
STYLE = REPO / "ui" / "style.css"

AA_TEXT = 4.5
AA_UI = 3.0

_DECL = re.compile(r"(--[a-z0-9-]+)\s*:\s*([^;]+);")


def _strip_comments(css: str) -> str:
    return re.sub(r"/\*[\s\S]*?\*/", "", css)


def _block(css: str, selector: str) -> dict[str, str]:
    """取出某个选择器的第一个规则块里的自定义属性。"""
    idx = css.index(selector)
    start = css.index("{", idx)
    depth = 0
    for i in range(start, len(css)):
        if css[i] == "{":
            depth += 1
        elif css[i] == "}":
            depth -= 1
            if depth == 0:
                body = css[start + 1 : i]
                break
    else:  # pragma: no cover - 括号不平衡时直接失败更好
        raise AssertionError(f"{selector} 规则块未闭合")
    return {m.group(1): m.group(2).strip() for m in _DECL.finditer(body)}


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    h = value.strip().lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _luminance(rgb: tuple[int, int, int]) -> float:
    def channel(c: int) -> float:
        s = c / 255.0
        return s / 12.92 if s <= 0.03928 else ((s + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(fg: str, bg: str) -> float:
    l1, l2 = _luminance(_hex_to_rgb(fg)), _luminance(_hex_to_rgb(bg))
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


def _mix(fg: str, bg: str, pct: float) -> str:
    """近似 color-mix(in srgb, fg pct%, bg)：用于叠在底色上的淡色底。"""
    f, b = _hex_to_rgb(fg), _hex_to_rgb(bg)
    # 解包成三个标量再拼（而非 `% out`）：ruff UP031 要求格式说明符，且元数
    # 显式为 3 时它才能证明改写安全。蓝色分量借 bl —— b 已被底色元组占用
    r, g, bl = (round(f[i] * pct + b[i] * (1 - pct)) for i in range(3))
    return f"#{r:02X}{g:02X}{bl:02X}"


def _tokens() -> tuple[dict[str, str], dict[str, str]]:
    css = _strip_comments(STYLE.read_text(encoding="utf-8"))
    return _block(css, ":root"), _block(css, '[data-theme="dark"]')


def test_token_blocks_define_the_same_keys() -> None:
    """深浅两块的键必须对称。

    缺项不会报错，只会静默继承浅色值——深色模式下常表现为
    "某个徽章突然变成浅色底上的浅色字"。允许 dark 缺少与主题无关的
    结构性令牌（圆角/阴影/宽度）。
    """
    light, dark = _tokens()
    structural = {"--radius", "--radius-sm", "--sidebar-w", "--shadow"}
    missing = (set(light) - set(dark)) - structural
    assert not missing, f"深色模式缺少配色令牌（将继承浅色值）: {sorted(missing)}"
    extra = set(dark) - set(light)
    assert not extra, f"深色模式独有令牌（浅色下无定义）: {sorted(extra)}"


def test_body_text_tokens_meet_aa() -> None:
    """正文/次级/三级文字在 --bg 与 --surface 上都要过 4.5:1。"""
    for name, block in (("light", _tokens()[0]), ("dark", _tokens()[1])):
        for token in ("--text", "--text-2", "--text-3"):
            for base in ("--bg", "--surface"):
                ratio = _contrast(block[token], block[base])
                assert ratio >= AA_TEXT, (
                    f"{name} {token} on {base} = {ratio:.2f}:1 < {AA_TEXT}"
                )


def test_semantic_text_tokens_meet_aa_on_their_real_backgrounds() -> None:
    """语义色在样式表里真实成对使用的底色上过 4.5:1。

    只校验实际存在的配对，避免用臆造的组合制造假失败：

    - ``--warn`` / ``--danger`` / ``--accent`` 各自的 ``--status-*-bg``
      （.status.*、.btn-memo.active、.error-banner.error、.onboard-chip.*）；
    - ``.error-banner.warning`` 是唯一的 ``transparent`` 淡底：12% 警告色
      直接叠在页面 ``--bg`` 上，而非表面色，需单独按 --bg 计算。
    """
    pairs = (
        ("--accent", "--status-ok-bg"),
        ("--warn", "--status-warn-bg"),
        ("--memo", "--status-warn-bg"),
        ("--danger", "--status-danger-bg"),
    )
    for name, block in (("light", _tokens()[0]), ("dark", _tokens()[1])):
        for token, base in pairs:
            ratio = _contrast(block[token], block[base])
            assert ratio >= AA_TEXT, (
                f"{name} {token} on {base} = {ratio:.2f}:1 < {AA_TEXT}"
            )
        # .error-banner.warning：12% --warn 叠在页面底色上（background 为 transparent）
        tint = _mix(block["--warn"], block["--bg"], 0.12)
        ratio = _contrast(block["--warn"], tint)
        assert ratio >= AA_TEXT, (
            f"{name} .error-banner.warning = {ratio:.2f}:1 < {AA_TEXT}"
        )


def test_badge_and_state_tokens_meet_aa() -> None:
    """徽章与运行状态令牌在 12%/16% 淡底上过 4.5:1。"""
    badges = ("--badge-later", "--badge-today", "--badge-soon", "--badge-urgent")
    states = ("--state-ok", "--state-warn", "--state-err", "--state-info")
    for name, block in (("light", _tokens()[0]), ("dark", _tokens()[1])):
        for token in badges:
            tint = _mix(block[token], block["--surface"], 0.12)
            ratio = _contrast(block[token], tint)
            assert ratio >= AA_TEXT, (
                f"{name} {token} on 12% tint = {ratio:.2f}:1 < {AA_TEXT}"
            )
        for token in states:
            tint = _mix(block[token], block["--surface"], 0.16)
            ratio = _contrast(block[token], tint)
            assert ratio >= AA_TEXT, (
                f"{name} {token} on 16% tint = {ratio:.2f}:1 < {AA_TEXT}"
            )


def test_strong_border_meets_ui_contrast() -> None:
    """--border-strong 是可感知边界令牌，需满足 1.4.11 的 3:1。

    --border 刻意保持装饰级低对比（分隔线），不在此约束内；
    交互控件的边框必须用 --border-strong。
    """
    for name, block in (("light", _tokens()[0]), ("dark", _tokens()[1])):
        for base in ("--bg", "--surface"):
            ratio = _contrast(block["--border-strong"], block[base])
            assert ratio >= AA_UI, (
                f"{name} --border-strong on {base} = {ratio:.2f}:1 < {AA_UI}"
            )


def test_on_accent_meets_aa_against_accent_fill() -> None:
    """实心强调底上的文字（按钮、已订阅徽章）需过 4.5:1。"""
    for name, block in (("light", _tokens()[0]), ("dark", _tokens()[1])):
        ratio = _contrast(block["--on-accent"], block["--accent"])
        assert ratio >= AA_TEXT, (
            f"{name} --on-accent on --accent = {ratio:.2f}:1 < {AA_TEXT}"
        )


def test_no_stale_warning_alias_or_legacy_accent_fallback() -> None:
    """守卫两个已修掉的令牌 bug，防止回流。

    1. ``var(--warning, #D97706)``：``--warning`` 从未定义过，10 处引用
       一直吃回退色，改主题色时不会跟随；
    2. ``var(--accent, #2563EB)``：旧主题色回退，与当前墨绿主色不符。
    """
    css = STYLE.read_text(encoding="utf-8")
    assert "--warning" not in css, "--warning 是未定义令牌，请使用 --warn"
    stale = re.findall(r"var\(\s*--accent\s*,\s*#[0-9A-Fa-f]{3,6}\s*\)", css)
    assert not stale, f"--accent 不应带旧主题色回退: {stale}"
