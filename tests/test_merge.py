"""会话内同话题合并判官单元测试（不触发真实 AI）。"""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from briefdesk.plugins.merge.engine import (
    JUDGE_PROMPT,
    TITLE_PROMPT,
    _build_judge_user_message,
    _build_title_user_message,
    _parse_merge,
    _parse_title,
    judge_merge,
    summarize_title,
)


def _resp(content, finish_reason="stop"):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=content),
                finish_reason=finish_reason,
            )
        ]
    )


class JudgeUserMessageTest(unittest.TestCase):
    """判官 user 消息：两张卡的数据全部由 user 角色承载。"""

    def test_message_contains_both_cards(self):
        p = _build_judge_user_message("塔卡沙团购", "45元", "运费aa", "面交")
        self.assertIn("塔卡沙团购", p)
        self.assertIn("45元", p)
        self.assertIn("运费aa", p)
        self.assertIn("面交", p)

    def test_message_escapes_braces_safely(self):
        # 数据可能含花括号：填充不会被 str.format/f-string 误解析
        p = _build_judge_user_message("{特殊}", "x", "y", "z")
        self.assertIn("{特殊}", p)

    def test_message_does_not_rescan_data_values(self):
        # P6 同款缺陷守卫：数据值含模板占位符字面量时不得被二次替换
        # （旧顺序 replace 链会把 desc_a 里的 "{desc_b}" 换成卡片B 内容）
        p = _build_judge_user_message("甲", "原文含 {desc_b} 字面量", "乙", "丙")
        self.assertIn("原文含 {desc_b} 字面量", p)
        self.assertIn("内容：丙", p)


class JudgeSystemPromptTest(unittest.TestCase):
    """判官 system prompt：只含规则与输出格式，不含具体卡片数据。"""

    def test_rules_only(self):
        self.assertIn('{"merge": true}', JUDGE_PROMPT)
        self.assertNotIn("卡片A（先出现）", JUDGE_PROMPT)
        self.assertNotIn("{title_a}", JUDGE_PROMPT)
        self.assertNotIn("{desc_a}", JUDGE_PROMPT)

    def test_safety_rule_uses_plain_format(self):
        # 防回归：安全规则必须与主体/解析器一致地要求无外壳 JSON（{"merge": ...}），
        # 带 task 外壳的旧格式不得再出现在 prompt 中
        self.assertIn(
            '输出必须严格且只能是 {"merge": true} 或 {"merge": false}',
            JUDGE_PROMPT,
        )
        self.assertNotIn('{"task":"merge"', JUDGE_PROMPT)


class ParseMergeTest(unittest.TestCase):
    def test_true(self):
        # 主路径：无外壳格式
        self.assertIs(_parse_merge('{"merge": true}'), True)

    def test_legacy_shell_tolerated(self):
        # 兼容旧版 {"task":"merge","data":{"merge": ...}} 外壳
        self.assertIs(_parse_merge('{"task":"merge","data":{"merge": true}}'), True)

    def test_false(self):
        self.assertIs(_parse_merge('{"merge": false}'), False)

    def test_fenced(self):
        self.assertIs(_parse_merge('```json\n{"merge": true}\n```'), True)

    def test_garbage_none(self):
        self.assertIsNone(_parse_merge("不是JSON"))
        self.assertIsNone(_parse_merge('{"merge": "yes"}'))
        self.assertIsNone(_parse_merge('["merge"]'))

    def test_task_field_ignored_any_shell_tolerated(self):
        # task 字段不再校验：任意外壳 dict 均按 data 取值
        self.assertIs(_parse_merge('{"task":"other","data":{"merge": true}}'), True)

    def test_missing_data_none(self):
        self.assertIsNone(_parse_merge('{"task":"merge"}'))

    def test_repairable_damage(self):
        self.assertIs(_parse_merge('{"merge": true,}'), True)
        self.assertIs(_parse_merge("{'merge': false}"), False)
        self.assertIs(_parse_merge('结论：{"merge": true}'), True)
        self.assertIs(_parse_merge('```json\n{"merge": false}\n```'), False)

    def test_truncated_bool_not_accepted(self):
        # 截断布尔被修复成字符串 → 类型校验拦截，不误判
        self.assertIsNone(_parse_merge('{"merge": tru'))


class JudgeMergeTest(unittest.IsolatedAsyncioTestCase):
    async def test_true_response(self):
        with patch(
            "briefdesk.plugins.merge.engine.chat", new=AsyncMock(return_value=_resp('{"task":"merge","data":{"merge": true}}'))
        ):
            self.assertIs(await judge_merge("a", "b", "c", "d"), True)

    async def test_false_response(self):
        with patch(
            "briefdesk.plugins.merge.engine.chat", new=AsyncMock(return_value=_resp('{"task":"merge","data":{"merge": false}}'))
        ):
            self.assertIs(await judge_merge("a", "b", "c", "d"), False)

    async def test_unparseable_retries_once_then_none(self):
        # 判官输出无法解析 → 重试一次后返回 None（区别于明确的 False：
        # 失败不构成判定依据，观察型插件据此跳过记录）
        chat_mock = AsyncMock(side_effect=[_resp("垃圾输出"), _resp("还是垃圾")])
        with patch("briefdesk.plugins.merge.engine.chat", new=chat_mock):
            self.assertIsNone(await judge_merge("a", "b", "c", "d"))
        self.assertEqual(chat_mock.call_count, 2)

    async def test_transport_error_conservative_none(self):
        chat_mock = AsyncMock(side_effect=RuntimeError("network down"))
        with patch("briefdesk.plugins.merge.engine.chat", new=chat_mock):
            self.assertIsNone(await judge_merge("a", "b", "c", "d"))
        self.assertEqual(chat_mock.call_count, 1)  # 不重试，保守不合并

    async def test_sends_system_then_user_messages(self):
        chat_mock = AsyncMock(return_value=_resp('{"task":"merge","data":{"merge": true}}'))
        with patch("briefdesk.plugins.merge.engine.chat", new=chat_mock):
            await judge_merge("塔卡沙团购", "45元", "运费aa", "面交")
        msgs = chat_mock.call_args.kwargs["messages"]
        self.assertEqual([m["role"] for m in msgs], ["system", "user"])
        self.assertIn('{"merge": true}', msgs[0]["content"])  # system 只含规则/格式
        self.assertNotIn("卡片A（先出现）", msgs[0]["content"])
        self.assertIn("卡片A（先出现）", msgs[1]["content"])  # 数据全部在 user
        self.assertIn("塔卡沙团购", msgs[1]["content"])
        self.assertIn("45元", msgs[1]["content"])
        self.assertIn("运费aa", msgs[1]["content"])
        self.assertIn("面交", msgs[1]["content"])


class TitleRegenerationTest(unittest.TestCase):
    """合并后重拟标题：user 消息填充、system 规则、JSON 解析与回退语义。"""

    def test_user_message_contains_merged_content(self):
        p = _build_title_user_message("旧标题", "45, 运费AA", "团购\n面交")
        self.assertIn("旧标题", p)
        self.assertIn("45, 运费AA", p)
        self.assertIn("团购\n面交", p)

    def test_title_data_with_literal_placeholder_not_double_replaced(self):
        # P6：数据值的占位符字面量不得被后续 replace 二次替换（单遍填充）
        p = _build_title_user_message("{key_info}", "{quote}", "旧标题 {old_title}")
        self.assertIn("原标题：{key_info}", p)
        self.assertIn("关键信息：{quote}", p)
        self.assertIn("内容：旧标题 {old_title}", p)

    def test_system_prompt_rules_only(self):
        self.assertIn('{"title":"新标题"}', TITLE_PROMPT)
        self.assertNotIn("原标题：", TITLE_PROMPT)
        self.assertNotIn("{old_title}", TITLE_PROMPT)
        self.assertNotIn("{key_info}", TITLE_PROMPT)
        self.assertNotIn("{quote}", TITLE_PROMPT)

    def test_safety_rule_uses_plain_format(self):
        # 防回归：安全规则必须与主体/解析器一致地要求无外壳 JSON（{"title": ...}），
        # 带 task 外壳的旧格式不得再出现在 prompt 中
        self.assertIn(
            '输出必须严格且只能是 {"title":"新标题"}',
            TITLE_PROMPT,
        )
        self.assertNotIn('{"task":"title"', TITLE_PROMPT)

    def test_parse_title(self):
        # 主路径：无外壳格式
        self.assertEqual(_parse_title('{"title":"塔卡沙团购（5本45元）"}'), "塔卡沙团购（5本45元）")
        self.assertEqual(_parse_title('```json\n{"title":"新标题"}\n```'), "新标题")
        self.assertIsNone(_parse_title("不是JSON"))
        self.assertIsNone(_parse_title('{"title":""}'))
        self.assertIsNone(_parse_title('{"title":123}'))
        self.assertIsNone(_parse_title('{"title":"' + "x" * 61 + '"}'))  # 超长

    def test_parse_title_legacy_shell_tolerated(self):
        # 兼容旧版 {"task":"title","data":{"title":...}} 外壳
        self.assertEqual(
            _parse_title('{"task":"title","data":{"title":"塔卡沙团购（5本45元）"}}'),
            "塔卡沙团购（5本45元）",
        )

    def test_parse_title_collapses_whitespace(self):
        self.assertEqual(_parse_title('{"title": "  多行\n  标题  "}'), "多行 标题")

    def test_task_field_ignored_any_shell_tolerated(self):
        # task 字段不再校验：任意外壳 dict 均按 data 取值
        self.assertEqual(_parse_title('{"task":"other","data":{"title":"新标题"}}'), "新标题")

    def test_missing_data_none(self):
        self.assertIsNone(_parse_title('{"task":"title"}'))

    def test_repairable_damage(self):
        self.assertEqual(_parse_title('{"title":"新标题",}'), "新标题")
        self.assertEqual(_parse_title("{'title':'新标题'}"), "新标题")
        self.assertEqual(_parse_title('标题为：{"title":"新标题"}'), "新标题")
        # 缺尾括号（stop 但结构损坏）→ json_repair 补全
        self.assertEqual(_parse_title('{"title":"新标题"'), "新标题")

    def test_truncated_string_still_fails_without_repair(self):
        # finish_reason=length 截断路径（repair=False）：不做修复，
        # 残缺标题（如"新标"）不得覆盖原标题
        self.assertIsNone(_parse_title('{"task":"title","data":{"title":"新标', repair=False))

    def test_repair_disabled_is_strict(self):
        # repair=False 时即使可修复的瑕疵也按解析失败处理（截断输出不可信任）
        self.assertIsNone(_parse_title('{"task":"title","data":{"title":"新标题",}', repair=False))
        self.assertIsNone(_parse_merge('{"task":"merge","data":{"merge": true,}}', repair=False))


class SummarizeTitleTest(unittest.IsolatedAsyncioTestCase):
    async def test_success(self):
        with patch(
            "briefdesk.plugins.merge.engine.chat",
            new=AsyncMock(return_value=_resp('{"task":"title","data":{"title":"新标题"}}')),
        ):
            self.assertEqual(await summarize_title("旧", "k", "q"), "新标题")

    async def test_unparseable_returns_none(self):
        with patch(
            "briefdesk.plugins.merge.engine.chat", new=AsyncMock(return_value=_resp("垃圾"))
        ):
            self.assertIsNone(await summarize_title("旧", "k", "q"))

    async def test_transport_error_returns_none(self):
        with patch(
            "briefdesk.plugins.merge.engine.chat",
            new=AsyncMock(side_effect=RuntimeError("network down")),
        ):
            self.assertIsNone(await summarize_title("旧", "k", "q"))

    async def test_sends_system_then_user_messages(self):
        chat_mock = AsyncMock(return_value=_resp('{"task":"title","data":{"title":"新标题"}}'))
        with patch("briefdesk.plugins.merge.engine.chat", new=chat_mock):
            await summarize_title("旧", "k", "q")
        msgs = chat_mock.call_args.kwargs["messages"]
        self.assertEqual([m["role"] for m in msgs], ["system", "user"])
        self.assertIn('{"title":"新标题"}', msgs[0]["content"])  # system 只含规则
        self.assertNotIn("原标题：", msgs[0]["content"])
        self.assertIn("原标题：旧", msgs[1]["content"])  # 数据全部在 user
        self.assertIn("关键信息：k", msgs[1]["content"])
        self.assertIn("内容：q", msgs[1]["content"])


if __name__ == "__main__":
    unittest.main()
