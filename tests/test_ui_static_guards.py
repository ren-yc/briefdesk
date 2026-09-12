"""ui/app.js 静态守卫：图标内联解析安全与动态选择器转义。

按 tests/test_announcements.py::UiWiringTest 的静态检查风格：读 app.js
源码断言关键写法存在/不存在。
"""

import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


class IconInlineParserGuardTest(unittest.TestCase):
    """SVG 文本必须先经 DOMParser 解析取根节点再插入 DOM，
    不得直接 span.innerHTML = svgText（未解析字符串赋值）。"""

    def test_svg_parsed_via_domparser(self):
        app = (_ROOT / "ui" / "app.js").read_text(encoding="utf-8")
        self.assertIn(
            'DOMParser().parseFromString(svgText, "image/svg+xml")',
            app,
            "图标内联必须经 DOMParser 解析",
        )
        self.assertIn(
            'svgRoot.querySelector("parsererror")',
            app,
            "必须拒绝解析失败（parsererror）的 SVG 文档",
        )
        self.assertNotIn(
            "span.innerHTML = svgText",
            app,
            "不得把未解析的 SVG 字符串直接 innerHTML",
        )
        # _SVG_CONTENT_RE 形状校验保留在上游
        self.assertIn("_SVG_CONTENT_RE", app)


class DynamicSelectorEscapeGuardTest(unittest.TestCase):
    """动态选择器值一律 CSS.escape（防止含引号/特殊字符的 key/name
    破坏选择器或注入任意属性匹配）。"""

    def test_env_selector_values_escaped(self):
        app = (_ROOT / "ui" / "app.js").read_text(encoding="utf-8")
        self.assertIn(
            '\'#env-items .env-row[data-env-key="\' + CSS.escape(key) + \'"]\'',
            app,
        )
        self.assertIn(
            '\'#env-secrets .env-row[data-sec-name="\' + CSS.escape(name) + \'"]\'',
            app,
        )
        self.assertIn(
            '\'[data-sec-input="\' + CSS.escape(name) + \'"]\'',
            app,
        )


if __name__ == "__main__":
    unittest.main()
