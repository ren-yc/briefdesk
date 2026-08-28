"""F2 索引漂移守卫（两道关）测试：字面 argmax + 语义裁判。

第一关（字面，同步）：quote 在全批消息中做包含率相对比较——自己必须明显
第一；近亲消息（南模/位育）、同事故 pair（电脑/自行车）双向可辨；单条批
无漂移对象，仅拦"与内容几乎无字面交集"的疑似幻觉 quote；平票记入
ambiguous_out 交第二关。
第二关（语义，异步）：嵌入余弦 argmax 复核模糊条目；嵌入不可用/失败一律
放行（宁放行勿误杀——误杀即重试死循环，漏检有下游去重/人工兜底）。

回归锚点：2026-08 第一轮事故（电脑/自行车漂移、女装活动长原文改写误杀）。
"""

import unittest
from unittest.mock import patch

from briefdesk.plugins.classify import engine
from briefdesk.plugins.classify.engine import (
    _char_quote_verdict,
    _parse_response,
)
from briefdesk.types import ClassifyResult


class CharQuoteVerdictTest(unittest.TestCase):
    """第一关：字面相对比较（同步、零成本）。"""

    def test_near_duplicate_correct_labeling_passes(self):
        # 南模/位育近亲消息：正确标注 → 自己明显第一 → 放行
        contents = [
            "南模中学编程社招新，9月1日开始报名，联系王老师",
            "位育中学编程社招新，测试另一条",
        ]
        self.assertIs(_char_quote_verdict("南模中学编程社招新", 0, contents), True)

    def test_near_duplicate_drift_rejected(self):
        # 位育的 quote 标给南模：别人明显更像 → 拦下
        contents = [
            "南模中学编程社招新，9月1日开始报名，联系王老师",
            "位育中学编程社招新，测试另一条",
        ]
        self.assertIs(_char_quote_verdict("位育中学编程社招新", 0, contents), False)

    def test_incident_pair_drift_rejected(self):
        # 第一轮事故回归：自行车 quote 正确/漂移双向
        contents = [
            "大家好，出一个二手电脑，RTX4060的笔记本，机械革命牌子的，报价6000块，有意向的同学可以私聊",
            "大家好，出一个二手自行车，永久牌子的，500块，有意向的同学可以私聊",
        ]
        self.assertIs(_char_quote_verdict(contents[1], 1, contents), True)
        self.assertIs(_char_quote_verdict(contents[1], 0, contents), False)

    def test_long_content_paraphrase_passes(self):
        # 女装活动回归：长原文 + 轻度改写摘录（少写弯引号）→ 放行
        content = (
            "关于编程社老社长女装活动的通知 现定于2026年10月13日（星期二）"
            "下午四时在社团活动室举办“编程社老社长女装活动”，望准时参加。"
        )
        quote = "现定于2026年10月13日下午四时在社团活动室举办编程社老社长女装活动"
        self.assertIs(_char_quote_verdict(quote, 0, [content]), True)

    def test_curly_quotes_normalized(self):
        # 弯引号参与归一化：带/不带弯引号的同句互为子串
        self.assertIs(
            _char_quote_verdict(
                "举办“编程社老社长女装活动”",
                0,
                ["举办“编程社老社长女装活动”的通知"],
            ),
            True,
        )

    def test_single_batch_low_containment_rejected(self):
        # 单条批：quote 与内容几乎无字面交集（疑似幻觉）→ 打回（既有口径）
        self.assertIs(
            _char_quote_verdict(
                "出二手自行车九成新两百块", 0, ["下周三下午三点社团活动室面试"]
            ),
            False,
        )

    def test_single_batch_aligned_passes(self):
        self.assertIs(
            _char_quote_verdict("下周三面试", 0, ["下周三下午三点社团活动室面试"]),
            True,
        )

    def test_exact_duplicates_tie_goes_to_referee(self):
        # 完全相同的两条消息：平票 → None（交语义裁判，最终由其放行）
        contents = ["摄影社招新", "摄影社招新"]
        self.assertIs(_char_quote_verdict("摄影社招新", 0, contents), None)

    def test_empty_quote_passes(self):
        self.assertIs(_char_quote_verdict("", 0, ["任意内容"]), True)


class SemanticRefereeTest(unittest.IsolatedAsyncioTestCase):
    """第二关：嵌入余弦 argmax；嵌入不可用/失败一律放行。"""

    def setUp(self) -> None:
        self.engine = engine

    async def test_drift_paraphrase_rejected(self):
        # quote 与 contents[1] 语义同向 → 判漂移
        async def fake_embed(texts):
            return [[0.0, 1.0], [1.0, 0.0], [0.05, 1.0]]

        with patch.object(
            self.engine, "is_embedding_enabled", return_value=True
        ), patch.object(self.engine, "embed_texts", side_effect=fake_embed):
            aligned = await self.engine._semantic_quote_referee(
                "位育中学的编程社团开始招收新成员了",
                0,
                ["南模中学编程社纳新", "位育中学编程社纳新"],
            )
        self.assertIs(aligned, False)

    async def test_own_paraphrase_passes(self):
        async def fake_embed(texts):
            return [[1.0, 0.0], [0.9, 0.1], [0.0, 1.0]]

        with patch.object(
            self.engine, "is_embedding_enabled", return_value=True
        ), patch.object(self.engine, "embed_texts", side_effect=fake_embed):
            aligned = await self.engine._semantic_quote_referee(
                "自己的改写", 0, ["自己原文", "无关消息"]
            )
        self.assertIs(aligned, True)

    async def test_embedding_failure_passes(self):
        async def fake_embed(texts):
            raise RuntimeError("ollama down")

        with patch.object(
            self.engine, "is_embedding_enabled", return_value=True
        ), patch.object(self.engine, "embed_texts", side_effect=fake_embed):
            aligned = await self.engine._semantic_quote_referee(
                "quote", 0, ["a", "b"]
            )
        self.assertIs(aligned, True)

    async def test_embedding_disabled_skips_and_never_calls_embed(self):
        called = []

        async def fake_embed(texts):
            called.append(texts)
            return []

        with patch.object(
            self.engine, "is_embedding_enabled", return_value=False
        ), patch.object(self.engine, "embed_texts", side_effect=fake_embed):
            aligned = await self.engine._semantic_quote_referee(
                "quote", 0, ["a", "b"]
            )
        self.assertIs(aligned, True)
        self.assertEqual(called, [])


class ParseResponseAmbiguousOutTest(unittest.TestCase):
    """_parse_response 记录模糊条目：结果暂留、ambiguous_out 上报。"""

    def test_ambiguous_recorded_and_result_kept(self):
        ambiguous = []
        results, retry, _times = _parse_response(
            '[{"index":0,"include":true,"category":"活动通知","quote":"摄影社招新"},'
            '{"index":1,"include":false}]',
            {"活动通知"},
            2,
            contents=["摄影社招新", "摄影社招新"],
            ambiguous_out=ambiguous,
        )
        self.assertEqual(ambiguous, [0])
        self.assertEqual([r.msg_index for r in results], [0])
        self.assertEqual(retry, [])

    def test_no_ambiguous_without_contents(self):
        ambiguous = []
        results, _retry, _times = _parse_response(
            '[{"index":0,"include":true,"category":"活动通知","quote":"任意"}]',
            {"活动通知"},
            1,
            ambiguous_out=ambiguous,
        )
        self.assertEqual(ambiguous, [])
        self.assertEqual([r.msg_index for r in results], [0])


class SemanticRefineIntegrationTest(unittest.IsolatedAsyncioTestCase):
    """端到端：语义裁判摘除漂移条目（results/time_indexes → retry）。"""

    def setUp(self) -> None:
        self.engine = engine

    async def test_refine_demotes_drift(self):
        async def fake_embed(texts):
            # texts = [quote, *contents]：quote 与 contents[1] 同向
            return [[0.0, 1.0], [1.0, 0.0], [0.05, 1.0]]

        results = [
            ClassifyResult(msg_index=0, category="活动通知", quote="q")
        ]
        with patch.object(
            self.engine, "is_embedding_enabled", return_value=True
        ), patch.object(self.engine, "embed_texts", side_effect=fake_embed):
            results, retry, times = await self.engine._semantic_refine(
                [0], results, [], [0], ["南模原文", "位育原文"]
            )
        self.assertEqual(results, [])
        self.assertEqual(retry, [0])
        self.assertEqual(times, [])

    async def test_refine_keeps_aligned(self):
        async def fake_embed(texts):
            return [[1.0, 0.0], [0.9, 0.05], [0.0, 1.0]]

        results = [
            ClassifyResult(msg_index=0, category="活动通知", quote="q")
        ]
        with patch.object(
            self.engine, "is_embedding_enabled", return_value=True
        ), patch.object(self.engine, "embed_texts", side_effect=fake_embed):
            results, retry, times = await self.engine._semantic_refine(
                [0], results, [], [0], ["自己原文", "无关消息"]
            )
        self.assertEqual([r.msg_index for r in results], [0])
        self.assertEqual(retry, [])
        self.assertEqual(times, [0])


if __name__ == "__main__":
    unittest.main()
