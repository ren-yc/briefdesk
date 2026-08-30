"""侧边栏布局结构守卫测试（纯静态解析，不依赖浏览器）。

背景：侧边栏为三段式——顶部工具入口（日历/问一问，由插件 ui.js 注入
核心 ``#nav-top`` 容器）→ 横线分隔 → 分类导航（``#category-nav``，JS 渲染）
→ 底部状态筛选/设置（``.nav-special``）。历史上插件入口插在 nav-special
内部，置顶分组后这里钉住 DOM 次序、设置入口位置与分隔线守卫写法，
防止无意识回流。

插件前端随插件包分发（核心 ``ui/`` 不写死插件入口，见
``tests/test_web_plugins.py`` 的 ``CoreFrontendBoundaryTest``），故对插件
文件只做「锚点指向 #nav-top」的文本断言，不解析其行为。
"""

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
INDEX = REPO / "ui" / "index.html"
STYLE = REPO / "ui" / "style.css"
CALENDAR_UI = REPO / "briefdesk" / "plugins" / "calendar" / "ui" / "ui.js"
RAG_UI = REPO / "briefdesk" / "plugins" / "rag" / "ui" / "ui.js"

_SEGMENTS = ('id="nav-top"', 'id="category-nav"', 'class="nav-special"')


def test_nav_top_container_sits_above_category_nav() -> None:
    """侧边栏顺序必须是 工具入口(#nav-top) → 分类导航 → 底部组(.nav-special)。"""
    html = INDEX.read_text(encoding="utf-8")
    assert 'id="nav-top"' in html, "index.html 缺少 #nav-top 工具入口容器"
    order = [html.index(seg) for seg in _SEGMENTS]
    assert order == sorted(order), "侧边栏三段顺序错位：工具入口 → 分类导航 → 底部组"


def test_settings_link_lives_in_nav_special_after_ignored() -> None:
    """设置入口在底部组内「已忽略」之后；顶栏不再保留设置入口。"""
    html = INDEX.read_text(encoding="utf-8")
    start = html.index('class="nav-special"')
    end = html.index("</div>", html.index('id="ignored-link"'))
    segment = html[start:end]
    assert segment.index('id="ignored-link"') < segment.index('id="settings-link"'), (
        "设置入口应位于底部组「已忽略」之后"
    )
    assert "settings-link-top" not in html, "顶栏旧设置入口（settings-link-top）应已移除"
    assert "top-settings-link" not in html


def test_divider_rule_guards_on_content() -> None:
    """分隔线用 :has(.cat-link) 守卫：有入口才画线，插件全停用不留孤线。

    不用 :empty——它对容器内的空白文本节点敏感（一次 HTML 格式化即失效），
    失效形态是「无插件也常驻一条线」，静默且无报错。
    """
    css = STYLE.read_text(encoding="utf-8")
    i_mobile = css.index("@media (max-width: 768px)")
    desktop = css[:i_mobile]
    assert "#nav-top:has(.cat-link)" in desktop, "缺少 #nav-top 分隔线守卫规则（桌面）"
    # 移动端转横向并去分隔线（与 .nav-special 的移动端处理一致）
    mobile = css[i_mobile:css.index("@media (max-width: 640px)")]
    assert "#nav-top:has(.cat-link)" in mobile, "移动端缺少 #nav-top 分隔线覆盖"
    assert ".top-settings-link" not in css, "顶栏设置样式应随入口一并移除"


def test_plugin_frontends_anchor_nav_top() -> None:
    """插件 ui.js 必须锚定 #nav-top：日历插首位、问一问追加末位。

    prepend/append 组合与插件加载顺序无关，恒为 日历→问一问；
    两者都保留旧锚点回退（兼容旧核心）。
    """
    cal = CALENDAR_UI.read_text(encoding="utf-8")
    assert 'getElementById("nav-top")' in cal, "日历入口未锚定 #nav-top"
    assert "insertBefore($calendarBtn, $navTop.firstChild)" in cal, (
        "日历入口应插入 #nav-top 首位"
    )
    rag = RAG_UI.read_text(encoding="utf-8")
    assert 'getElementById("nav-top")' in rag, "问一问入口未锚定 #nav-top"
    assert "$navTop.appendChild($navLink)" in rag, "问一问入口应追加到 #nav-top 末位"
