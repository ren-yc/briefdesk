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


class WriteErrorHelperGuardTest(unittest.TestCase):
    """写操作失败一律经 showWriteError：窗口期 409 必须与真实失败区分。

    各调用点曾各自 showToast("...失败...")，窗口内后端闸门拒绝时会把
    「基准运行中、现在重试不会成功」说成普通失败，用户反复重试。
    """

    def test_app_js_write_sites_use_shared_helper(self):
        app = (_ROOT / "ui" / "app.js").read_text(encoding="utf-8")
        self.assertIn(
            "function showWriteError(err, fallbackMsg, errorDuration = 4000)", app
        )
        # 定义 1 处 + 7 个调用点（单卡/撤销/批量/改分类/时间线/会话发现/设置保存）
        self.assertEqual(app.count("showWriteError("), 8)
        # 旧写法不得残留
        self.assertNotIn('showToast("批量操作失败，请重试"', app)
        self.assertNotIn('showToast("分类修改失败，请重试"', app)

    def test_benchmark_plugin_ui_formats_detail_objects(self):
        ui = (
            _ROOT / "briefdesk" / "plugins" / "benchmark" / "ui" / "ui.js"
        ).read_text(encoding="utf-8")
        self.assertIn("function detailText(", ui)
        # detail 为对象（写闸门 / 备份防线 / 导出守卫的 409）时直接拼接会
        # 渲染成 "[object Object]"
        self.assertNotIn('(data.detail || ("HTTP " + res.status))', ui)
        self.assertEqual(ui.count("detailText(data, res.status)"), 3)

    def test_plugin_uis_have_failure_fallbacks(self):
        """插件前端必须给出各自的失败文案（「读响应体识别窗口」的判据已随窗口机制删除）。"""
        for name, fallback in (
            ("reminders", 'showWriteError(err, "提醒设置失败，请重试")'),
            ("rag", "问答服务暂时不可用"),
        ):
            ui = (
                _ROOT / "briefdesk" / "plugins" / name / "ui" / "ui.js"
            ).read_text(encoding="utf-8")
            self.assertIn(fallback, ui)


if __name__ == "__main__":
    unittest.main()
