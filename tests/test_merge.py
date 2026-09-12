"""会话内同话题合并判官单元测试（不触发真实 AI）。"""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from briefdesk.plugins.merge.engine import (
    JUDGE_PROMPT,
    TITLE_PROMPT,
    _build_judge_user_message,
    _build_title_user_message,
    _clip_desc,
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
        assert "塔卡沙团购" in p
        assert "45元" in p
        assert "运费aa" in p
        assert "面交" in p

    def test_message_escapes_braces_safely(self):
        # 数据可能含花括号：填充不会被 str.format/f-string 误解析
        p = _build_judge_user_message("{特殊}", "x", "y", "z")
        assert "{特殊}" in p

    def test_message_does_not_rescan_data_values(self):
        # P6 同款缺陷守卫：数据值含模板占位符字面量时不得被二次替换
        # （旧顺序 replace 链会把 desc_a 里的 "{desc_b}" 换成卡片B 内容）
        p = _build_judge_user_message("甲", "原文含 {desc_b} 字面量", "乙", "丙")
        assert "原文含 {desc_b} 字面量" in p
        assert "内容：丙" in p


class JudgeSystemPromptTest(unittest.TestCase):
    """判官 system prompt：只含规则与输出格式，不含具体卡片数据。"""

    def test_rules_only(self):
        assert '{"merge": true}' in JUDGE_PROMPT
        assert "卡片A（先出现）" not in JUDGE_PROMPT
        assert "{title_a}" not in JUDGE_PROMPT
        assert "{desc_a}" not in JUDGE_PROMPT

    def test_safety_rule_uses_plain_format(self):
        # 防回归：安全规则必须与主体/解析器一致地要求无外壳 JSON（{"merge": ...}），
        # 带 task 外壳的旧格式不得再出现在 prompt 中
        assert '输出必须严格且只能是 {"merge": true} 或 {"merge": false}' in JUDGE_PROMPT
        assert '{"task":"merge"' not in JUDGE_PROMPT


class ParseMergeTest(unittest.TestCase):
    def test_true(self):
        # 主路径：无外壳格式
        assert _parse_merge('{"merge": true}') is True

    def test_legacy_shell_tolerated(self):
        # 兼容旧版 {"task":"merge","data":{"merge": ...}} 外壳
        assert _parse_merge('{"task":"merge","data":{"merge": true}}') is True

    def test_false(self):
        assert _parse_merge('{"merge": false}') is False

    def test_fenced(self):
        assert _parse_merge('```json\n{"merge": true}\n```') is True

    def test_garbage_none(self):
        assert _parse_merge("不是JSON") is None
        assert _parse_merge('{"merge": "yes"}') is None
        assert _parse_merge('["merge"]') is None

    def test_task_field_ignored_any_shell_tolerated(self):
        # task 字段不再校验：任意外壳 dict 均按 data 取值
        assert _parse_merge('{"task":"other","data":{"merge": true}}') is True

    def test_missing_data_none(self):
        assert _parse_merge('{"task":"merge"}') is None

    def test_repairable_damage(self):
        assert _parse_merge('{"merge": true,}') is True
        assert _parse_merge("{'merge': false}") is False
        assert _parse_merge('结论：{"merge": true}') is True
        assert _parse_merge('```json\n{"merge": false}\n```') is False

    def test_truncated_bool_not_accepted(self):
        # 截断布尔被修复成字符串 → 类型校验拦截，不误判
        assert _parse_merge('{"merge": tru') is None


class TestJudgeMerge:
    async def test_true_response(self):
        with patch(
            "briefdesk.plugins.merge.engine.chat", new=AsyncMock(return_value=_resp('{"task":"merge","data":{"merge": true}}'))
        ):
            assert await judge_merge("a", "b", "c", "d") is True

    async def test_false_response(self):
        with patch(
            "briefdesk.plugins.merge.engine.chat", new=AsyncMock(return_value=_resp('{"task":"merge","data":{"merge": false}}'))
        ):
            assert await judge_merge("a", "b", "c", "d") is False

    async def test_unparseable_retries_once_then_none(self):
        # 判官输出无法解析 → 重试一次后返回 None（区别于明确的 False：
        # 失败不构成判定依据，观察型插件据此跳过记录）
        chat_mock = AsyncMock(side_effect=[_resp("垃圾输出"), _resp("还是垃圾")])
        with patch("briefdesk.plugins.merge.engine.chat", new=chat_mock):
            assert await judge_merge("a", "b", "c", "d") is None
        assert chat_mock.call_count == 2

    async def test_transport_error_conservative_none(self):
        chat_mock = AsyncMock(side_effect=RuntimeError("network down"))
        with patch("briefdesk.plugins.merge.engine.chat", new=chat_mock):
            assert await judge_merge("a", "b", "c", "d") is None
        assert chat_mock.call_count == 1  # 不重试，保守不合并

    async def test_sends_system_then_user_messages(self):
        chat_mock = AsyncMock(return_value=_resp('{"task":"merge","data":{"merge": true}}'))
        with patch("briefdesk.plugins.merge.engine.chat", new=chat_mock):
            await judge_merge("塔卡沙团购", "45元", "运费aa", "面交")
        msgs = chat_mock.call_args.kwargs["messages"]
        assert [m["role"] for m in msgs] == ["system", "user"]
        assert '{"merge": true}' in msgs[0]["content"]  # system 只含规则/格式
        assert "卡片A（先出现）" not in msgs[0]["content"]
        assert "卡片A（先出现）" in msgs[1]["content"]  # 数据全部在 user
        assert "塔卡沙团购" in msgs[1]["content"]
        assert "45元" in msgs[1]["content"]
        assert "运费aa" in msgs[1]["content"]
        assert "面交" in msgs[1]["content"]

    async def test_long_desc_is_clipped_before_send(self):
        # 超长描述在判官 user 消息里被截断，尾部不再送入
        long_desc = "x" * 1000
        chat_mock = AsyncMock(return_value=_resp('{"task":"merge","data":{"merge": true}}'))
        with patch("briefdesk.plugins.merge.engine.chat", new=chat_mock):
            await judge_merge("a", long_desc, "b", "短")
        user = chat_mock.call_args.kwargs["messages"][1]["content"]
        assert "x" * 400 in user
        assert "x" * 401 not in user  # 截断到 400 字符，无更长连续段


class ClipDescTest(unittest.TestCase):
    def test_short_text_unchanged(self):
        assert _clip_desc("短文本") == "短文本"

    def test_long_text_clipped_to_max(self):
        assert len(_clip_desc("x" * 1000)) == 400


class TitleRegenerationTest(unittest.TestCase):
    """合并后重拟标题：user 消息填充、system 规则、JSON 解析与回退语义。"""

    def test_user_message_contains_merged_content(self):
        p = _build_title_user_message("旧标题", "45, 运费AA", "团购\n面交")
        assert "旧标题" in p
        assert "45, 运费AA" in p
        assert "团购\n面交" in p

    def test_title_data_with_literal_placeholder_not_double_replaced(self):
        # P6：数据值的占位符字面量不得被后续 replace 二次替换（单遍填充）
        p = _build_title_user_message("{key_info}", "{quote}", "旧标题 {old_title}")
        assert "原标题：{key_info}" in p
        assert "关键信息：{quote}" in p
        assert "内容：旧标题 {old_title}" in p

    def test_system_prompt_rules_only(self):
        assert '{"title":"新标题"}' in TITLE_PROMPT
        assert "原标题：" not in TITLE_PROMPT
        assert "{old_title}" not in TITLE_PROMPT
        assert "{key_info}" not in TITLE_PROMPT
        assert "{quote}" not in TITLE_PROMPT

    def test_safety_rule_uses_plain_format(self):
        # 防回归：安全规则必须与主体/解析器一致地要求无外壳 JSON（{"title": ...}），
        # 带 task 外壳的旧格式不得再出现在 prompt 中
        assert '输出必须严格且只能是 {"title":"新标题"}' in TITLE_PROMPT
        assert '{"task":"title"' not in TITLE_PROMPT

    def test_parse_title(self):
        # 主路径：无外壳格式
        assert _parse_title('{"title":"塔卡沙团购（5本45元）"}') == "塔卡沙团购（5本45元）"
        assert _parse_title('```json\n{"title":"新标题"}\n```') == "新标题"
        assert _parse_title("不是JSON") is None
        assert _parse_title('{"title":""}') is None
        assert _parse_title('{"title":123}') is None
        assert _parse_title('{"title":"' + "x" * 61 + '"}') is None  # 超长

    def test_parse_title_legacy_shell_tolerated(self):
        # 兼容旧版 {"task":"title","data":{"title":...}} 外壳
        assert _parse_title('{"task":"title","data":{"title":"塔卡沙团购（5本45元）"}}') == "塔卡沙团购（5本45元）"

    def test_parse_title_collapses_whitespace(self):
        assert _parse_title('{"title": "  多行\n  标题  "}') == "多行 标题"

    def test_task_field_ignored_any_shell_tolerated(self):
        # task 字段不再校验：任意外壳 dict 均按 data 取值
        assert _parse_title('{"task":"other","data":{"title":"新标题"}}') == "新标题"

    def test_missing_data_none(self):
        assert _parse_title('{"task":"title"}') is None

    def test_repairable_damage(self):
        assert _parse_title('{"title":"新标题",}') == "新标题"
        assert _parse_title("{'title':'新标题'}") == "新标题"
        assert _parse_title('标题为：{"title":"新标题"}') == "新标题"
        # 缺尾括号（stop 但结构损坏）→ json_repair 补全
        assert _parse_title('{"title":"新标题"') == "新标题"

    def test_truncated_string_still_fails_without_repair(self):
        # finish_reason=length 截断路径（repair=False）：不做修复，
        # 残缺标题（如"新标"）不得覆盖原标题
        assert _parse_title('{"task":"title","data":{"title":"新标', repair=False) is None

    def test_repair_disabled_is_strict(self):
        # repair=False 时即使可修复的瑕疵也按解析失败处理（截断输出不可信任）
        assert _parse_title('{"task":"title","data":{"title":"新标题",}', repair=False) is None
        assert _parse_merge('{"task":"merge","data":{"merge": true,}}', repair=False) is None


class TestSummarizeTitle:
    async def test_success(self):
        with patch(
            "briefdesk.plugins.merge.engine.chat",
            new=AsyncMock(return_value=_resp('{"task":"title","data":{"title":"新标题"}}')),
        ):
            assert await summarize_title("旧", "k", "q") == "新标题"

    async def test_unparseable_returns_none(self):
        with patch(
            "briefdesk.plugins.merge.engine.chat", new=AsyncMock(return_value=_resp("垃圾"))
        ):
            assert await summarize_title("旧", "k", "q") is None

    async def test_transport_error_returns_none(self):
        with patch(
            "briefdesk.plugins.merge.engine.chat",
            new=AsyncMock(side_effect=RuntimeError("network down")),
        ):
            assert await summarize_title("旧", "k", "q") is None

    async def test_sends_system_then_user_messages(self):
        chat_mock = AsyncMock(return_value=_resp('{"task":"title","data":{"title":"新标题"}}'))
        with patch("briefdesk.plugins.merge.engine.chat", new=chat_mock):
            await summarize_title("旧", "k", "q")
        msgs = chat_mock.call_args.kwargs["messages"]
        assert [m["role"] for m in msgs] == ["system", "user"]
        assert '{"title":"新标题"}' in msgs[0]["content"]  # system 只含规则
        assert "原标题：" not in msgs[0]["content"]
        assert "原标题：旧" in msgs[1]["content"]  # 数据全部在 user
        assert "关键信息：k" in msgs[1]["content"]
        assert "内容：q" in msgs[1]["content"]


class TestAfterRunEmbeddingGate:
    """【P3-11】after_run 补嵌入必须按 is_embedding_enabled 门控。

    EMBED_API_BASE 留空（默认配置）时 embed_api_base 回退 chat 端点，
    embed_texts 会打出一发注定失败的 /embeddings——每个含合并的批白发
    一次网络请求并触发公告链路。门控后应零请求、零 DB 存在性复查；
    嵌入启用时行为不变（存活卡带向量重新登记）。
    """

    def _make_batch_ctx(self):
        from briefdesk.plugins.merge.plugin import MergePlugin

        batch = SimpleNamespace(
            reembed_queue=[(7, "合并标题", "合并引文", None, "weflow")]
        )
        ctx = SimpleNamespace(dedup=SimpleNamespace(add_to_cache=Mock()))
        return MergePlugin(), batch, ctx

    async def test_after_run_skips_all_io_when_embedding_disabled(self):
        plugin, batch, ctx = self._make_batch_ctx()
        with (
            patch("briefdesk.ai_ports.is_embedding_enabled", return_value=False),
            patch("briefdesk.ai_ports.embed_texts", new=AsyncMock()) as emb,
            patch(
                "briefdesk.db.get_existing_item_ids", new=AsyncMock()
            ) as exist,
        ):
            await plugin.after_run(batch, ctx)  # 不抛即通过
        emb.assert_not_awaited()
        exist.assert_not_awaited()
        ctx.dedup.add_to_cache.assert_not_called()

    async def test_after_run_embeds_surviving_card_when_enabled(self):
        plugin, batch, ctx = self._make_batch_ctx()
        vector = [0.5, 0.5]
        with (
            patch("briefdesk.ai_ports.is_embedding_enabled", return_value=True),
            patch("briefdesk.ai_ports.embed_texts", new=AsyncMock(return_value=[vector])),
            patch(
                "briefdesk.db.get_existing_item_ids",
                new=AsyncMock(return_value=[7]),
            ),
        ):
            await plugin.after_run(batch, ctx)
        ctx.dedup.add_to_cache.assert_called_once_with(
            7,
            "合并标题",
            embedding=vector,
            image_urls=None,
            source="weflow",
            source_quote="合并引文",
        )


if __name__ == "__main__":
    unittest.main()
