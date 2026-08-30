"""去重辅助逻辑单元测试（不调用 AI / 不访问 DB）。"""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from briefdesk.plugins.dedup.engine import (
    JUDGE_PROMPT,
    CachedItem,
    DedupEngine,
    _embedding_text,
    _parse_images,
)


class AskAiParseFailureTest(unittest.IsolatedAsyncioTestCase):
    """【复核 P2-19】_ask_ai 两次解析失败返回 None（「判定未知」），**绝不
    return False** 被当作明确的 DIFFERENT 票计入计权。调用方 _collect_verdicts
    按各自门禁处置该 None：normal 路径剔除计权、weak 复核当反对票。"""

    async def test_double_parse_failure_returns_none(self):
        engine = DedupEngine()
        item = SimpleNamespace(title="A", source_quote="qa")
        chat = AsyncMock(return_value=SimpleNamespace(choices=[]))
        with patch("briefdesk.plugins.dedup.engine.chat", new=chat):
            verdict = await engine._ask_ai(item, "B", "qb")
        self.assertIsNone(verdict, "解析失败是「未知」，不是 DIFFERENT")
        self.assertIsNot(verdict, False, "False 会被当作明确反对票计入计权")
        self.assertEqual(chat.await_count, 2, "两次尝试后才放弃")

    async def test_collect_verdicts_warns_on_unparseable(self):
        """解析失败的降级必须可见：_collect_verdicts 对 None 与对异常同等记
        WARNING，否则日志里只剩 _ask_ai 的「重试」提示，看不出该候选最终被
        按 fail_note 的口径处置掉了。"""
        engine = DedupEngine()
        cand = SimpleNamespace(title="候选A", source_quote="qa")
        chat = AsyncMock(return_value=SimpleNamespace(choices=[]))
        with (
            patch("briefdesk.plugins.dedup.engine.chat", new=chat),
            self.assertLogs("briefdesk.plugins.dedup.engine", level="WARNING") as cm,
        ):
            out = await engine._collect_verdicts(
                [(cand, 0.9)], "B", "qb", "按反对票计"
            )
        self.assertEqual(out, [None])
        self.assertTrue(
            any("判定未知" in m and "候选A" in m and "按反对票计" in m
                for m in cm.output),
            f"缺少解析失败的降级 WARNING：{cm.output}",
        )


class JudgePromptTest(unittest.TestCase):
    def test_contains_few_shot_examples(self):
        self.assertIn("示例1：", JUDGE_PROMPT)
        self.assertIn('{"same": true}', JUDGE_PROMPT)
        self.assertIn("示例2：", JUDGE_PROMPT)
        self.assertIn('{"same": false}', JUDGE_PROMPT)

    def test_contains_conservative_false_rule(self):
        self.assertIn("如果不确定，请返回 {\"same\": false}", JUDGE_PROMPT)

    def test_safety_rule_uses_plain_format(self):
        # 防回归：安全规则必须与主体/解析器一致地要求无外壳 JSON（{"same": ...}），
        # 带 task 外壳的旧格式不得再出现在 prompt 中
        self.assertIn(
            '输出必须严格且只能是 {"same": true} 或 {"same": false}',
            JUDGE_PROMPT,
        )
        self.assertNotIn('{"task":"dedup"', JUDGE_PROMPT)


class ParseSameRepairTest(unittest.TestCase):
    """json_repair 兜底：常见语法损坏可修复，语义损坏仍拒绝。"""

    def setUp(self):
        self.engine = DedupEngine()

    def test_trailing_comma(self):
        self.assertIs(self.engine._parse_same('{"task":"dedup","data":{"same": true,}}'), True)

    def test_single_quotes(self):
        self.assertIs(self.engine._parse_same("{'task':'dedup','data':{'same': false}}"), False)

    def test_unquoted_keys(self):
        self.assertIs(self.engine._parse_same('{task:"dedup",data:{same: true}}'), True)

    def test_narrative_prefix_and_suffix(self):
        self.assertIs(self.engine._parse_same('结论如下：{"task":"dedup","data":{"same": true}}'), True)
        self.assertIs(self.engine._parse_same('{"task":"dedup","data":{"same": true}}。以上'), True)

    def test_markdown_fence(self):
        self.assertIs(self.engine._parse_same('```json\n{"task":"dedup","data":{"same": false}}\n```'), False)

    def test_truncated_bool_not_accepted(self):
        # 截断布尔会被修复成字符串，必须被类型校验拦截（防误判）
        self.assertIsNone(self.engine._parse_same('{"task":"dedup","data":{"same": tru'))

    def test_repair_disabled_is_strict(self):
        # finish_reason=length 截断路径（repair=False）：可修复的瑕疵也拒绝
        self.assertIsNone(self.engine._parse_same('{"task":"dedup","data":{"same": true,}}', repair=False))


class AskAiMaxTokensTest(unittest.IsolatedAsyncioTestCase):
    async def test_ask_ai_uses_max_tokens_128(self):
        engine = DedupEngine()
        resp = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content='{"task":"dedup","data":{"same": true}}'),
                    finish_reason="stop",
                )
            ]
        )
        chat_mock = AsyncMock(return_value=resp)
        with patch("briefdesk.plugins.dedup.engine.chat", new=chat_mock):
            result = await engine._ask_ai(
                SimpleNamespace(title="a", source_quote="b"),
                "c",
                "d",
            )
        self.assertTrue(result)
        self.assertEqual(chat_mock.call_args.kwargs["max_tokens"], 128)


class ParseSameTest(unittest.TestCase):
    def setUp(self):
        self.engine = DedupEngine()

    def test_true(self):
        # 主路径：无外壳格式
        self.assertTrue(self.engine._parse_same('{"same": true}'))

    def test_legacy_shell_tolerated(self):
        # 兼容旧版 {"task":"dedup","data":{"same": ...}} 外壳
        self.assertTrue(self.engine._parse_same('{"task":"dedup","data":{"same": true}}'))

    def test_false(self):
        self.assertFalse(self.engine._parse_same('{"same": false}'))

    def test_string_false_is_none(self):
        self.assertIsNone(self.engine._parse_same('{"same": "false"}'))

    def test_invalid_json_is_none(self):
        self.assertIsNone(self.engine._parse_same("not json"))

    def test_non_dict_is_none(self):
        self.assertIsNone(self.engine._parse_same("[1, 2]"))

    def test_task_field_ignored_any_shell_tolerated(self):
        # task 字段不再校验：任意外壳 dict 均按 data 取值
        self.assertTrue(self.engine._parse_same('{"task":"other","data":{"same": true}}'))

    def test_missing_data_is_none(self):
        self.assertIsNone(self.engine._parse_same('{"task":"dedup"}'))


class TitleOverlapTest(unittest.TestCase):
    def test_identical(self):
        self.assertEqual(DedupEngine._title_overlap("摄影社招新", "摄影社招新"), 1.0)

    def test_disjoint(self):
        self.assertEqual(DedupEngine._title_overlap("abc", "xyz"), 0.0)

    def test_empty(self):
        self.assertEqual(DedupEngine._title_overlap("", "abc"), 0.0)


class RemoveItemsTest(unittest.TestCase):
    def test_removes_cache_and_pending_embedding(self):
        engine = DedupEngine()
        engine._embed_cache_ok = True
        engine.add_to_cache("keep", "keep title", source_quote="keep desc")
        engine.add_to_cache("del", "del title", [0.1, 0.2], source_quote="del desc")
        self.assertEqual(len(engine._pending_embeds), 1)

        engine.remove_items(["del"])

        self.assertTrue(all(it.id != "del" for it in engine._cache))
        self.assertTrue(all(row[0] != "del" for row in engine._pending_embeds))
        self.assertTrue(any(it.id == "keep" for it in engine._cache))

    def test_readd_same_id_updates_in_place_and_registers_embedding(self):
        """同 id 重复追加（并发/唯一键冲突路径）：更新而非叠加，且向量照样登记。

        新建与更新两条分支共用同一段向量登记代码，此处钉住更新分支——
        它只在重试路径上才走到，漏登记的后果是该条永远不落库向量、
        重启后才由缓存加载补齐，期间静默不参与余弦候选。
        """
        engine = DedupEngine()
        engine._embed_cache_ok = True
        engine.add_to_cache("dup", "旧标题", source_quote="旧原文")
        self.assertEqual(engine._pending_embeds, [])

        engine.add_to_cache("dup", "新标题", [0.3, 0.4], source_quote="新原文")

        self.assertEqual(len(engine._cache), 1)  # 更新而非叠加
        item = engine._cache[0]
        self.assertEqual(item.title, "新标题")
        self.assertEqual(item.source_quote, "新原文")
        self.assertEqual(item.content_hash, DedupEngine._content_hash("新原文"))
        self.assertEqual(item.embedding, [0.3, 0.4])
        self.assertEqual([row[0] for row in engine._pending_embeds], ["dup"])
        self.assertEqual(engine._pending_embeds[0][2], [0.3, 0.4])

    def test_embedding_not_registered_when_cache_degraded(self):
        """_embed_cache_ok=False（向量加载失败降级）时不登记待落库向量。

        与上一条配对：证明那段登记代码确实受这个开关管，
        合并新建/更新两分支没有把降级判断丢掉。
        """
        engine = DedupEngine()
        engine._embed_cache_ok = False
        engine.add_to_cache("a", "标题", [0.1], source_quote="原文")   # 新建分支
        engine.add_to_cache("a", "标题2", [0.2], source_quote="原文2")  # 更新分支
        self.assertEqual(engine._pending_embeds, [])
        self.assertIsNone(engine._cache[0].embedding)

    def test_remove_unknown_ids_is_harmless_noop(self):
        """路由层 ignore 改发 EVENT_ITEMS_DELETED 后，处理器会对不在缓存的 id
        触发 remove_items：缺失 id 必须幂等无害（不抛错、不动现有条目）。"""
        engine = DedupEngine()
        engine._embed_cache_ok = True
        engine.add_to_cache("keep", "keep title", [0.1], source_quote="keep desc")
        before_cache = list(engine._cache)
        before_pending = list(engine._pending_embeds)

        engine.remove_items(["ghost-a", "ghost-b"])  # 不抛错
        engine.remove_items([])  # 空列表 no-op

        self.assertEqual(engine._cache, before_cache)
        self.assertEqual(engine._pending_embeds, before_pending)
        self.assertEqual(len(engine._cache), 1)


class EmbeddingTextTest(unittest.TestCase):
    def test_format(self):
        self.assertEqual(_embedding_text("标题", "内容"), "标题 内容")

    def test_truncates_long_input(self):
        """【复核 P2-17】超长输入截断至 2000 字符：防单条毒丸文本让嵌入
        通道整体降级且每次重启确定性复现。"""
        text = _embedding_text("标题", "x" * 5000)
        self.assertEqual(len(text), 2000)


class CheckDedupShortCircuitTest(unittest.IsolatedAsyncioTestCase):
    """check_dedup 短路回归：同文本短路与原文哈希精确短路。

    回归背景：同标题（余弦 100%）的 SAME 票被高相似但不同话题的干扰候选
    （如"篮球社招新" vs "羽毛球社招新" 80%）稀释，多数票（>K/2）不达标
    导致真重复漏判入库。对策 = 两重短路：原文哈希精确命中零 AI 判定；
    score ≥ dedup_strong_threshold（0.99）候选 AI 判 SAME 即直接判重。
    """

    def _engine(self, items: list[tuple[str, str, str]]) -> DedupEngine:
        """items: (id, title, source_quote)；构造已加载缓存且带向量的引擎。"""
        engine = DedupEngine()
        engine._cache_loaded = True
        engine._cache = [
            CachedItem(
                id=it_id,
                title=t,
                source_quote=q,
                embedding=[0.1, 0.2],
            )
            for it_id, t, q in items
        ]
        return engine

    @staticmethod
    def _resp(same: bool) -> SimpleNamespace:
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content='{"same": true}' if same else '{"same": false}'
                    ),
                    finish_reason="stop",
                )
            ]
        )

    async def _chat_by_desc(self, same_by_desc: dict[str, bool]):
        """按"消息A"的内容返回判定（候选可能同标题，须按内容区分）。"""

        async def fake_chat(messages: list[dict], **kwargs):
            user = messages[1]["content"]
            block_a = user.split("消息B：")[0]
            a_desc = block_a.split("内容：", 1)[1].strip()
            return self._resp(same_by_desc[a_desc])

        return fake_chat

    async def test_hash_shortcut_skips_ai(self):
        """原文完全一致（哈希全等）→ 不调 AI，直接判重合并。"""
        engine = self._engine([("h1", "篮球社招新", "欢迎加入篮球社")])
        engine._cache[0].content_hash = DedupEngine._content_hash("欢迎加入篮球社")
        with (
            patch(
                "briefdesk.plugins.dedup.engine.chat", new=AsyncMock()
            ) as chat_mock,
            patch(
                "briefdesk.plugins.dedup.engine.merge_source_group", new=AsyncMock()
            ) as merge_mock,
        ):
            result = await engine.check_dedup(
                "篮球社招新", "新生2群", source_quote="欢迎加入篮球社"
            )
        self.assertTrue(result.is_duplicate)
        self.assertEqual(result.similar_to_id, "h1")
        merge_mock.assert_awaited_once_with("h1", "新生2群")
        chat_mock.assert_not_awaited()

    async def test_candidate_snapshot_on_hit(self):
        """命中时 result.candidate = 被并入条目的快照（观察插件记录判定依据）。"""
        engine = self._engine([("h1", "篮球社招新", "欢迎加入篮球社")])
        engine._cache[0].content_hash = DedupEngine._content_hash("欢迎加入篮球社")
        with patch(
            "briefdesk.plugins.dedup.engine.merge_source_group", new=AsyncMock()
        ):
            result = await engine.check_dedup(
                "篮球社招新", "新生2群", source_quote="欢迎加入篮球社"
            )
        self.assertTrue(result.is_duplicate)
        self.assertIsNotNone(result.candidate)
        self.assertEqual(result.candidate.item_id, "h1")
        self.assertEqual(result.candidate.title, "篮球社招新")
        self.assertEqual(result.candidate.source_quote, "欢迎加入篮球社")

    async def test_candidate_snapshot_on_different_verdict(self):
        """未命中时 result.candidate = 参与判定的最高分候选。"""
        engine = self._engine([("a1", "篮球社招新", "内容甲")])
        with (
            patch(
                "briefdesk.plugins.dedup.engine.chat",
                new=AsyncMock(return_value=self._resp(False)),
            ),
            patch(
                "briefdesk.plugins.dedup.engine.merge_source_group", new=AsyncMock()
            ),
        ):
            result = await engine.check_dedup(
                "篮球社招新", "新生2群", q_emb=[0.5, 0.6]
            )
        self.assertFalse(result.is_duplicate)
        self.assertIsNotNone(result.candidate)
        self.assertEqual(result.candidate.item_id, "a1")
        self.assertEqual(result.candidate.title, "篮球社招新")

    async def test_hash_unknown_falls_through(self):
        """旧数据 hash 为空 → 不触发精确短路，走正常候选判定（不误判重复）。"""
        engine = self._engine([("h1", "篮球社招新", "欢迎加入篮球社")])
        with (
            patch(
                "briefdesk.plugins.dedup.engine.chat",
                new=AsyncMock(return_value=self._resp(False)),
            ),
            patch(
                "briefdesk.plugins.dedup.engine.merge_source_group", new=AsyncMock()
            ) as merge_mock,
        ):
            result = await engine.check_dedup(
                "篮球社招新", "新生2群", source_quote="欢迎加入篮球社"
            )
        self.assertFalse(result.is_duplicate)
        merge_mock.assert_not_awaited()

    async def test_strong_same_short_circuits_majority(self):
        """回归：同标题 100% SAME + 干扰 80% DIFFERENT。
        多数票 1/2 会漏判；strong 短路应直接判重。"""
        engine = self._engine(
            [
                ("a1", "篮球社招新", "内容甲"),
                ("b1", "羽毛球社招新", "欢迎加入羽毛球社"),
            ]
        )
        same_by_desc = {"内容甲": True, "欢迎加入羽毛球社": False}
        with (
            patch(
                "briefdesk.plugins.dedup.engine.top_k_similar",
                return_value=[(0, 1.0), (1, 0.8)],
            ),
            patch(
                "briefdesk.plugins.dedup.engine.chat",
                new=AsyncMock(side_effect=await self._chat_by_desc(same_by_desc)),
            ),
            patch(
                "briefdesk.plugins.dedup.engine.merge_source_group", new=AsyncMock()
            ) as merge_mock,
        ):
            result = await engine.check_dedup(
                "篮球社招新", "新生2群", q_emb=[0.5, 0.6]
            )
        self.assertTrue(result.is_duplicate)
        self.assertEqual(result.similar_to_id, "a1")
        merge_mock.assert_awaited_once_with("a1", "新生2群")

    async def test_strong_unparseable_warns_and_falls_through(self):
        """strong 候选解析失败 → 记 WARNING 并保留参与多数票。

        _ask_ai 两次解析失败返回 None（不再抛错），故走不到短路段的 except；
        若不在 None 分支补记 WARNING，该候选的降级在日志里无声无息。
        """
        engine = self._engine([("a1", "篮球社招新", "内容甲")])

        async def unparseable(messages: list[dict], **kwargs):
            return SimpleNamespace(choices=[])  # 无 choices → 两次尝试均无法解析

        with (
            patch(
                "briefdesk.plugins.dedup.engine.top_k_similar",
                return_value=[(0, 1.0)],
            ),
            patch(
                "briefdesk.plugins.dedup.engine.chat",
                new=AsyncMock(side_effect=unparseable),
            ),
            self.assertLogs("briefdesk.plugins.dedup.engine", level="WARNING") as cm,
        ):
            result = await engine.check_dedup(
                "篮球社招新", "新生2群", q_emb=[0.5, 0.6]
            )
        self.assertFalse(result.is_duplicate, "判定未知不得当作重复")
        self.assertTrue(
            any("[strong]" in m and "判定未知" in m for m in cm.output),
            f"缺少 strong 短路的解析失败 WARNING：{cm.output}",
        )

    async def test_strong_different_falls_through_to_majority(self):
        """strong 候选判 DIFFERENT（同文本但内容不同）→ 剔除后剩余候选走多数票。"""
        engine = self._engine(
            [
                ("a1", "篮球社招新", "内容甲"),
                ("b1", "篮球社招新", "内容乙"),
            ]
        )
        same_by_desc = {"内容甲": False, "内容乙": True}
        with (
            patch(
                "briefdesk.plugins.dedup.engine.top_k_similar",
                return_value=[(0, 1.0), (1, 0.8)],
            ),
            patch(
                "briefdesk.plugins.dedup.engine.chat",
                new=AsyncMock(side_effect=await self._chat_by_desc(same_by_desc)),
            ),
            patch(
                "briefdesk.plugins.dedup.engine.merge_source_group", new=AsyncMock()
            ) as merge_mock,
        ):
            result = await engine.check_dedup(
                "篮球社招新", "新生2群", q_emb=[0.5, 0.6]
            )
        # strong (a1,1.0) 判 DIFFERENT 被剔除 → 剩余 (b1,0.8) 单候选判 SAME
        self.assertTrue(result.is_duplicate)
        self.assertEqual(result.similar_to_id, "b1")
        merge_mock.assert_awaited_once_with("b1", "新生2群")

    async def test_strong_different_sole_candidate_snapshots_itself(self):
        """strong 判 DIFFERENT 且无其余候选 → 快照是被剔除的强候选自己。

        这条路径的候选列表已被 remove 清空，快照若误取 candidates[0] 会
        IndexError；取成别的条目则不报错但 benchmark 的负例会记到另一条上。
        """
        engine = self._engine([("only", "篮球社招新", "内容甲")])
        with (
            patch(
                "briefdesk.plugins.dedup.engine.top_k_similar",
                return_value=[(0, 1.0)],
            ),
            patch(
                "briefdesk.plugins.dedup.engine.chat",
                new=AsyncMock(return_value=self._resp(False)),
            ),
            patch(
                "briefdesk.plugins.dedup.engine.merge_source_group", new=AsyncMock()
            ) as merge_mock,
        ):
            result = await engine.check_dedup(
                "篮球社招新", "新生2群", q_emb=[0.5, 0.6]
            )
        self.assertFalse(result.is_duplicate)
        merge_mock.assert_not_awaited()
        self.assertIsNotNone(result.candidate)
        self.assertEqual(result.candidate.item_id, "only")
        self.assertEqual(result.candidate.source_quote, "内容甲")

    async def test_non_strong_still_uses_majority(self):
        """无 ≥0.99 候选（不同标题高相似）→ 维持原多数票语义，不误判。"""
        engine = self._engine(
            [
                ("a1", "位育摄影社招新", "内容甲"),
                ("b1", "南洋模范摄影社招新", "内容乙"),
            ]
        )
        same_by_desc = {"内容甲": False, "内容乙": True}
        with (
            patch(
                "briefdesk.plugins.dedup.engine.top_k_similar",
                return_value=[(0, 0.90), (1, 0.85)],
            ),
            patch(
                "briefdesk.plugins.dedup.engine.chat",
                new=AsyncMock(side_effect=await self._chat_by_desc(same_by_desc)),
            ),
            patch(
                "briefdesk.plugins.dedup.engine.merge_source_group", new=AsyncMock()
            ) as merge_mock,
        ):
            result = await engine.check_dedup(
                "南洋模范摄影社招新", "新生2群", q_emb=[0.5, 0.6]
            )
        # 多数票 1/2 不达标 → 保守不判重
        self.assertFalse(result.is_duplicate)
        merge_mock.assert_not_awaited()


class ParseImagesTest(unittest.TestCase):
    """image_urls 归一化助手：DB JSON 字符串 / 调用方列表 → 图片路径集合。"""

    def test_parses_json_array(self):
        self.assertEqual(
            _parse_images('["a.jpg", "b.jpg"]'), frozenset({"a.jpg", "b.jpg"})
        )

    def test_empty_string(self):
        self.assertEqual(_parse_images(""), frozenset())

    def test_filters_empty_entries(self):
        self.assertEqual(_parse_images('["a.jpg", "", null]'), frozenset({"a.jpg"}))

    def test_garbage_is_empty(self):
        self.assertEqual(_parse_images("not json"), frozenset())
        self.assertEqual(_parse_images("{}"), frozenset())

    def test_accepts_list_input(self):
        """调用方直传的列表（消息/合并产物）与 DB JSON 同口径。"""
        self.assertEqual(
            _parse_images(["a.jpg", "b.jpg"]), frozenset({"a.jpg", "b.jpg"})
        )

    def test_list_filters_empty_entries(self):
        self.assertEqual(_parse_images(["a.jpg", ""]), frozenset({"a.jpg"}))

    def test_none_is_empty(self):
        self.assertEqual(_parse_images(None), frozenset())
        self.assertEqual(_parse_images([]), frozenset())


class ImageUrlShortCircuitTest(unittest.IsolatedAsyncioTestCase):
    """图片精确短路：限定源内 image_urls 集合完全一致 → 零 AI 直接判重。

    回归背景：同一张海报图片重发（OCR 原文逐字相同、image_urls 完全一致），
    分类 AI 对两次处理产出不同标题（"模政社团招新" vs "模拟政协招新"）→
    content_hash 失效、余弦 0.65 擦边未召回、单候选 AI 判定恰好判错 →
    重复卡入库。图片路径（上游内容寻址）在重发场景是确定性证据，直接短路。

    源限定：仅 weflow-legacy（图片消息无混合文本，同图必同文）参与；qqflow 实测
    存在图片+文字混合消息（同图可配不同文字），同图不等同于重复，不得短路。
    """

    def _engine(
        self, items: list[tuple[str, str, str, list[str], str]]
    ) -> DedupEngine:
        """items: (id, title, source_quote, image_urls, source)。"""
        engine = DedupEngine()
        engine._cache_loaded = True
        engine._cache = [
            CachedItem(
                id=it_id,
                title=t,
                source_quote=q,
                image_urls=frozenset(imgs),
                source=src,
                embedding=[0.1, 0.2],
            )
            for it_id, t, q, imgs, src in items
        ]
        return engine

    async def test_identical_images_skip_ai(self):
        """weflow-legacy 同图重发（标题措辞不同也不影响）→ 不调 AI，直接判重合并。"""
        engine = self._engine(
            [("img1", "模政社团招新", "杨高模政社团招新", ["a/b.jpg"], "weflow-legacy")]
        )
        with (
            patch("briefdesk.plugins.dedup.engine.chat", new=AsyncMock()) as chat_mock,
            patch(
                "briefdesk.plugins.dedup.engine.merge_source_group", new=AsyncMock()
            ) as merge_mock,
        ):
            result = await engine.check_dedup(
                "模拟政协招新",
                "我们四个",
                image_urls=["a/b.jpg"],
                source="weflow-legacy",
            )
        self.assertTrue(result.is_duplicate)
        self.assertEqual(result.similar_to_id, "img1")
        merge_mock.assert_awaited_once_with("img1", "我们四个")
        chat_mock.assert_not_awaited()

    async def test_order_insensitive_set_equality(self):
        """weflow-legacy 多图乱序（集合相等）→ 仍判重。"""
        engine = self._engine(
            [("img1", "摄影展", "内容", ["a.jpg", "b.jpg"], "weflow-legacy")]
        )
        with (
            patch(
                "briefdesk.plugins.dedup.engine.merge_source_group", new=AsyncMock()
            ) as merge_mock,
        ):
            result = await engine.check_dedup(
                "摄影展",
                "群A",
                image_urls=["b.jpg", "a.jpg"],
                source="weflow-legacy",
            )
        self.assertTrue(result.is_duplicate)
        self.assertEqual(result.similar_to_id, "img1")
        merge_mock.assert_awaited_once_with("img1", "群A")

    async def test_qqflow_same_image_different_text_not_short_circuit(self):
        """qqflow 同图不同文（图片+文字混合消息）→ 不短路，走原判定链（此处
        无候选 → 不判重）。这是本限定的核心回归：同图在 qqflow 不等同于重复。"""
        engine = self._engine(
            [("img1", "abc", "def", ["a.jpg"], "qqflow")]
        )
        with (
            patch("briefdesk.plugins.dedup.engine.chat", new=AsyncMock()) as chat_mock,
            patch(
                "briefdesk.plugins.dedup.engine.merge_source_group", new=AsyncMock()
            ) as merge_mock,
        ):
            result = await engine.check_dedup(
                "xyz", "群A", image_urls=["a.jpg"], source="qqflow"
            )
        self.assertFalse(result.is_duplicate)
        merge_mock.assert_not_awaited()
        chat_mock.assert_not_awaited()  # 无候选（重叠为 0）也不产生 AI 调用

    async def test_weflow_query_qqflow_cache_not_short_circuit(self):
        """查询 weflow-legacy 但缓存条目属 qqflow（源不一致）→ 不短路。"""
        engine = self._engine(
            [("img1", "abc", "def", ["a.jpg"], "qqflow")]
        )
        with (
            patch("briefdesk.plugins.dedup.engine.chat", new=AsyncMock()) as chat_mock,
            patch(
                "briefdesk.plugins.dedup.engine.merge_source_group", new=AsyncMock()
            ) as merge_mock,
        ):
            result = await engine.check_dedup(
                "xyz", "群A", image_urls=["a.jpg"], source="weflow-legacy"
            )
        self.assertFalse(result.is_duplicate)
        merge_mock.assert_not_awaited()
        chat_mock.assert_not_awaited()

    async def test_unknown_source_not_short_circuit(self):
        """查询 source 为空（未知）→ 保守不短路。"""
        engine = self._engine(
            [("img1", "abc", "def", ["a.jpg"], "weflow-legacy")]
        )
        with (
            patch("briefdesk.plugins.dedup.engine.chat", new=AsyncMock()) as chat_mock,
            patch(
                "briefdesk.plugins.dedup.engine.merge_source_group", new=AsyncMock()
            ) as merge_mock,
        ):
            result = await engine.check_dedup(
                "xyz", "群A", image_urls=["a.jpg"]
            )
        self.assertFalse(result.is_duplicate)
        merge_mock.assert_not_awaited()
        chat_mock.assert_not_awaited()

    async def test_different_images_falls_through(self):
        """图片不同 → 不触发短路，正常走候选路径（此处无候选 → 不判重）。"""
        engine = self._engine(
            [("img1", "abc", "def", ["a.jpg"], "weflow-legacy")]
        )
        with (
            patch("briefdesk.plugins.dedup.engine.chat", new=AsyncMock()) as chat_mock,
            patch(
                "briefdesk.plugins.dedup.engine.merge_source_group", new=AsyncMock()
            ) as merge_mock,
        ):
            result = await engine.check_dedup(
                "xyz", "群A", image_urls=["b.jpg"], source="weflow-legacy"
            )
        self.assertFalse(result.is_duplicate)
        merge_mock.assert_not_awaited()
        chat_mock.assert_not_awaited()  # 无候选（重叠为 0）也不产生 AI 调用

    async def test_query_without_images_falls_through(self):
        """查询无图 → 图片短路不参与；缓存有图也不误判。"""
        engine = self._engine(
            [("img1", "abc", "def", ["a.jpg"], "weflow-legacy")]
        )
        with (
            patch("briefdesk.plugins.dedup.engine.chat", new=AsyncMock()) as chat_mock,
            patch(
                "briefdesk.plugins.dedup.engine.merge_source_group", new=AsyncMock()
            ) as merge_mock,
        ):
            result = await engine.check_dedup(
                "xyz", "群A", source="weflow-legacy"
            )
        self.assertFalse(result.is_duplicate)
        merge_mock.assert_not_awaited()
        chat_mock.assert_not_awaited()

    async def test_partial_overlap_not_short_circuit(self):
        """集合相等而非子集：查询 1 图命中缓存多图之一 → 不短路（防装饰图误判）。"""
        engine = self._engine(
            [("img1", "abc", "def", ["a.jpg", "b.jpg"], "weflow-legacy")]
        )
        with (
            patch("briefdesk.plugins.dedup.engine.chat", new=AsyncMock()) as chat_mock,
            patch(
                "briefdesk.plugins.dedup.engine.merge_source_group", new=AsyncMock()
            ) as merge_mock,
        ):
            result = await engine.check_dedup(
                "xyz", "群A", image_urls=["a.jpg"], source="weflow-legacy"
            )
        self.assertFalse(result.is_duplicate)
        merge_mock.assert_not_awaited()
        chat_mock.assert_not_awaited()

    async def test_cache_images_from_db_format(self):
        """缓存条目经 _parse_images（DB JSON 字符串）装载后同样命中短路。"""
        engine = DedupEngine()
        engine._cache_loaded = True
        engine._cache = [
            CachedItem(
                id="img1",
                title="模政社团招新",
                source_quote="杨高模政社团招新",
                image_urls=_parse_images('["a/b.jpg"]'),
                source="weflow-legacy",
            )
        ]
        with (
            patch("briefdesk.plugins.dedup.engine.chat", new=AsyncMock()) as chat_mock,
            patch(
                "briefdesk.plugins.dedup.engine.merge_source_group", new=AsyncMock()
            ) as merge_mock,
        ):
            result = await engine.check_dedup(
                "模拟政协招新",
                "我们四个",
                image_urls=["a/b.jpg"],
                source="weflow-legacy",
            )
        self.assertTrue(result.is_duplicate)
        self.assertEqual(result.similar_to_id, "img1")
        merge_mock.assert_awaited_once_with("img1", "我们四个")
        chat_mock.assert_not_awaited()

    def test_add_to_cache_stores_source(self):
        """add_to_cache 记录 source：同图条目在源不一致时不得互相短路。"""
        engine = DedupEngine()
        engine._cache_loaded = True
        engine.add_to_cache("w1", "t", image_urls=["a.jpg"], source="weflow-legacy")
        engine.add_to_cache("q1", "t", image_urls=["a.jpg"], source="qqflow")
        by_id = {it.id: it for it in engine._cache}
        self.assertEqual(by_id["w1"].source, "weflow-legacy")
        self.assertEqual(by_id["q1"].source, "qqflow")
        self.assertEqual(by_id["w1"].image_urls, frozenset({"a.jpg"}))


class DedupTieredCandidateTest(unittest.IsolatedAsyncioTestCase):
    """门禁分级与兜底回归：弱候选低置信复核（②）、重叠兜底（①）、
    strong 剔除收窄（④）与无候选诊断（⑥）。

    回归背景：两条同题招新消息嵌入余弦 0.7528 < 0.80 门禁，
    唯一真实候选被预筛静默丢弃 → 重复卡入库且无任何日志。
    """

    def _engine(self, items: list[tuple[str, str, str]]) -> DedupEngine:
        """items: (id, title, source_quote)；构造已加载缓存且带向量的引擎。"""
        engine = DedupEngine()
        engine._cache_loaded = True
        engine._cache = [
            CachedItem(
                id=it_id,
                title=t,
                source_quote=d,
                embedding=[0.1, 0.2],
            )
            for it_id, t, d in items
        ]
        return engine

    @staticmethod
    def _resp(same: bool) -> SimpleNamespace:
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content='{"same": true}' if same else '{"same": false}'
                    ),
                    finish_reason="stop",
                )
            ]
        )

    async def _chat_by_desc(self, same_by_desc: dict[str, bool]):
        """按"消息A"的内容返回判定（候选可能同标题，须按内容区分）。"""

        async def fake_chat(messages: list[dict], **kwargs):
            user = messages[1]["content"]
            block_a = user.split("消息B：")[0]
            a_desc = block_a.split("内容：", 1)[1].strip()
            return self._resp(same_by_desc[a_desc])

        return fake_chat

    async def test_weak_candidate_unanimous_same_hits(self):
        """weak 区间场景（②）：余弦 0.75（[0.65, 0.80)）全员判 SAME → 判重合并。"""
        engine = self._engine([("w1", "玉言辩论社招新", "位育玉言辩论社招新啦")])
        with (
            patch(
                "briefdesk.plugins.dedup.engine.top_k_similar",
                return_value=[(0, 0.75)],
            ),
            patch(
                "briefdesk.plugins.dedup.engine.chat",
                new=AsyncMock(return_value=self._resp(True)),
            ),
            patch(
                "briefdesk.plugins.dedup.engine.merge_source_group", new=AsyncMock()
            ) as merge_mock,
        ):
            result = await engine.check_dedup(
                "玉言辩论社招新", "新生2群", q_emb=[0.5, 0.6]
            )
        self.assertTrue(result.is_duplicate)
        self.assertEqual(result.similar_to_id, "w1")
        merge_mock.assert_awaited_once_with("w1", "新生2群")

    async def test_weak_candidate_any_different_misses(self):
        """weak 候选存在 DIFFERENT 票 → 保守不判重。"""
        engine = self._engine([("w1", "玉言辩论社招新", "位育玉言辩论社招新啦")])
        with (
            patch(
                "briefdesk.plugins.dedup.engine.top_k_similar",
                return_value=[(0, 0.75)],
            ),
            patch(
                "briefdesk.plugins.dedup.engine.chat",
                new=AsyncMock(return_value=self._resp(False)),
            ),
            patch(
                "briefdesk.plugins.dedup.engine.merge_source_group", new=AsyncMock()
            ) as merge_mock,
        ):
            result = await engine.check_dedup(
                "玉言辩论社招新", "新生2群", q_emb=[0.5, 0.6]
            )
        self.assertFalse(result.is_duplicate)
        merge_mock.assert_not_awaited()

    async def test_weak_never_participates_when_normal_exists(self):
        """normal（≥0.80）+ weak（<0.80）混合：weak 不参与投票，normal 加权多数
        0.80 < (0.80+0.85)/2 不达标 → 不判重（弱票 SAME 不得抬高误判），
        且 weak 候选不产生 AI 调用。"""
        engine = self._engine(
            [
                ("n1", "篮球社招新", "内容甲"),
                ("n2", "羽毛球社招新", "内容乙"),
                ("w1", "排球社招新", "内容丙"),
            ]
        )
        same_by_desc = {"内容甲": True, "内容乙": False, "内容丙": True}
        with (
            patch(
                "briefdesk.plugins.dedup.engine.top_k_similar",
                return_value=[(0, 0.80), (1, 0.85), (2, 0.70)],
            ),
            patch(
                "briefdesk.plugins.dedup.engine.chat",
                new=AsyncMock(side_effect=await self._chat_by_desc(same_by_desc)),
            ) as chat_mock,
            patch(
                "briefdesk.plugins.dedup.engine.merge_source_group", new=AsyncMock()
            ) as merge_mock,
        ):
            result = await engine.check_dedup(
                "篮球社招新", "新生2群", q_emb=[0.5, 0.6]
            )
        self.assertFalse(result.is_duplicate)
        merge_mock.assert_not_awaited()
        self.assertEqual(chat_mock.await_count, 2)  # 仅 normal 候选被判定

    async def test_overlap_fallback_after_cosine_zero_hits(self):
        """重叠兜底场景（①）：余弦零候选（全部 < 0.65）→ 标题逐字相同
        overlap 1.0 → strong 短路（≥0.99）→ AI 判 SAME → 判重。"""
        engine = self._engine([("o1", "玉言辩论社招新", "位育玉言辩论社招新啦")])
        with (
            patch(
                "briefdesk.plugins.dedup.engine.top_k_similar",
                return_value=[],
            ),
            patch(
                "briefdesk.plugins.dedup.engine.chat",
                new=AsyncMock(return_value=self._resp(True)),
            ),
            patch(
                "briefdesk.plugins.dedup.engine.merge_source_group", new=AsyncMock()
            ) as merge_mock,
        ):
            result = await engine.check_dedup(
                "玉言辩论社招新", "新生2群", q_emb=[0.5, 0.6]
            )
        self.assertTrue(result.is_duplicate)
        self.assertEqual(result.similar_to_id, "o1")
        merge_mock.assert_awaited_once_with("o1", "新生2群")

    async def test_no_candidates_logs_diagnosis(self):
        """⑥：余弦零候选且重叠低于阈值 → DEBUG 诊断（含 cosine top-1 差距）+ 不判重。

        同时钉住重叠扫描只做一次：兜底采纳与诊断展示共用同一个结果。
        _best_overlap_candidate 是全缓存 O(n) 扫描，而「无候选」是每条不重复
        消息的常规路径——算两遍不会有任何可观测的错误，只会白烧一倍 CPU，
        所以只能靠调用计数钉住。
        """
        engine = self._engine([("x1", "篮球社招新", "欢迎加入篮球社")])

        def fake_topk(_q, _m, top_k, threshold):
            # 阈值 0 的 top-1 诊断调用返回最高余弦；正常召回返回空（全部低于 fallback）
            return [(0, 0.50)] if threshold == 0 else []

        overlap_calls = []
        real_overlap = DedupEngine._best_overlap_candidate

        def counting_overlap(self_, title):
            overlap_calls.append(title)
            return real_overlap(self_, title)

        with (
            patch(
                "briefdesk.plugins.dedup.engine.top_k_similar",
                side_effect=fake_topk,
            ),
            patch.object(
                DedupEngine, "_best_overlap_candidate", counting_overlap
            ),
            patch(
                "briefdesk.plugins.dedup.engine.merge_source_group", new=AsyncMock()
            ) as merge_mock,
            # 无候选是常规路径，诊断记在 DEBUG（不占 WARNING，见 _select_candidates）
            self.assertLogs("briefdesk.plugins.dedup.engine", level="DEBUG") as logs,
        ):
            # 与缓存共 1 字（"新"），overlap = 1/5 = 0.20 < 阈值 0.30：
            # 落在"有候选但不够格"分支，诊断才报得出差距
            result = await engine.check_dedup(
                "转让一台全新自行车", "新生2群", q_emb=[0.5, 0.6]
            )
        self.assertFalse(result.is_duplicate)
        merge_mock.assert_not_awaited()
        self.assertTrue(any("判重无候选" in line for line in logs.output))
        self.assertTrue(any("cosine top-1=" in line for line in logs.output))
        # 诊断里仍要报出重叠差距（复用的是兜底那次扫描的结果，不是省掉了信息）
        self.assertTrue(any("overlap top-1=0.20" in line for line in logs.output))
        self.assertEqual(overlap_calls, ["转让一台全新自行车"])

    async def test_zero_overlap_diagnosed_apart_from_empty_cache(self):
        """重叠全零（缓存非空但无共同字符）不得归因为「缓存为空」。

        两种成因都让 _best_overlap_candidate 返回 None（它要求严格 > 0），
        混为一谈会把排查引向"缓存没预热"的错误方向。
        """
        engine = self._engine([("x1", "篮球社招新", "欢迎加入篮球社")])
        with self.assertLogs(
            "briefdesk.plugins.dedup.engine", level="DEBUG"
        ) as logs:
            # 与"篮球社招新"零共同字符
            result = await engine.check_dedup("明天下午停电通知", "新生2群")
        self.assertFalse(result.is_duplicate)
        joined = "\n".join(logs.output)
        self.assertIn("overlap 全零", joined)
        self.assertIn("缓存 1 条", joined)
        self.assertNotIn("缓存为空", joined)

    async def test_empty_cache_diagnoses_cache_empty(self):
        """⑥ 的另一支：缓存为空时重叠候选为 None → 诊断报「缓存为空」。

        与上一条配对，确保复用 overlap_cand 后 None 分支仍然走得到。
        """
        engine = DedupEngine()
        engine._cache_loaded = True
        engine._cache = []
        with self.assertLogs(
            "briefdesk.plugins.dedup.engine", level="DEBUG"
        ) as logs:
            result = await engine.check_dedup("任意标题", "新生2群")
        self.assertFalse(result.is_duplicate)
        self.assertIsNone(result.candidate)
        self.assertTrue(any("缓存为空" in line for line in logs.output))

    async def test_strong_different_removes_only_judged(self):
        """④：两个 ≥0.99 候选，判 DIFFERENT 的只剔除自身，另一 SAME 候选仍命中。"""
        engine = self._engine(
            [
                ("s1", "玉言辩论社招新", "内容甲"),
                ("s2", "玉言辩论社招新", "内容乙"),
            ]
        )
        same_by_desc = {"内容甲": False, "内容乙": True}
        with (
            patch(
                "briefdesk.plugins.dedup.engine.top_k_similar",
                return_value=[(0, 1.0), (1, 0.99)],
            ),
            patch(
                "briefdesk.plugins.dedup.engine.chat",
                new=AsyncMock(side_effect=await self._chat_by_desc(same_by_desc)),
            ),
            patch(
                "briefdesk.plugins.dedup.engine.merge_source_group", new=AsyncMock()
            ) as merge_mock,
        ):
            result = await engine.check_dedup(
                "玉言辩论社招新", "新生2群", q_emb=[0.5, 0.6]
            )
        self.assertTrue(result.is_duplicate)
        self.assertEqual(result.similar_to_id, "s2")
        merge_mock.assert_awaited_once_with("s2", "新生2群")


class WeightedMajorityTest(unittest.IsolatedAsyncioTestCase):
    """⑦ 加权多数票：票权 = 候选相似度，SAME 权重和 > 总权重一半才命中。

    动机：等权多数票下 0.90 高置信 SAME 会被 0.80 低置信 DIFFERENT 稀释成
    平票漏判；加权后高相似候选的判定更可信，低置信票只作参考。
    """

    def _engine(self, items: list[tuple[str, str, str]]) -> DedupEngine:
        engine = DedupEngine()
        engine._cache_loaded = True
        engine._cache = [
            CachedItem(id=it_id, title=t, source_quote=d, embedding=[0.1, 0.2])
            for it_id, t, d in items
        ]
        return engine

    @staticmethod
    def _resp(same: bool) -> SimpleNamespace:
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content='{"same": true}' if same else '{"same": false}'
                    ),
                    finish_reason="stop",
                )
            ]
        )

    async def _chat_by_desc(self, same_by_desc: dict[str, bool]):
        async def fake_chat(messages: list[dict], **kwargs):
            user = messages[1]["content"]
            block_a = user.split("消息B：")[0]
            a_desc = block_a.split("内容：", 1)[1].strip()
            return self._resp(same_by_desc[a_desc])

        return fake_chat

    async def test_high_confidence_same_outweighs_low_confidence_different(self):
        """0.90 SAME vs 0.80 DIFFERENT → 加权命中（旧等权 1/2 漏判）。"""
        engine = self._engine(
            [
                ("n1", "篮球社招新", "内容甲"),
                ("n2", "羽毛球社招新", "内容乙"),
            ]
        )
        same_by_desc = {"内容甲": True, "内容乙": False}
        with (
            patch(
                "briefdesk.plugins.dedup.engine.top_k_similar",
                return_value=[(0, 0.90), (1, 0.80)],
            ),
            patch(
                "briefdesk.plugins.dedup.engine.chat",
                new=AsyncMock(side_effect=await self._chat_by_desc(same_by_desc)),
            ),
            patch(
                "briefdesk.plugins.dedup.engine.merge_source_group", new=AsyncMock()
            ) as merge_mock,
        ):
            result = await engine.check_dedup(
                "篮球社招新", "新生2群", q_emb=[0.5, 0.6]
            )
        self.assertTrue(result.is_duplicate)
        self.assertEqual(result.similar_to_id, "n1")
        merge_mock.assert_awaited_once_with("n1", "新生2群")

    async def test_low_confidence_same_loses_to_high_confidence_different(self):
        """0.80 SAME vs 0.90 DIFFERENT → 高置信 DIFFERENT 胜出，保守不判重。"""
        engine = self._engine(
            [
                ("n1", "篮球社招新", "内容甲"),
                ("n2", "羽毛球社招新", "内容乙"),
            ]
        )
        same_by_desc = {"内容甲": True, "内容乙": False}
        with (
            patch(
                "briefdesk.plugins.dedup.engine.top_k_similar",
                return_value=[(0, 0.80), (1, 0.90)],
            ),
            patch(
                "briefdesk.plugins.dedup.engine.chat",
                new=AsyncMock(side_effect=await self._chat_by_desc(same_by_desc)),
            ),
            patch(
                "briefdesk.plugins.dedup.engine.merge_source_group", new=AsyncMock()
            ) as merge_mock,
        ):
            result = await engine.check_dedup(
                "篮球社招新", "新生2群", q_emb=[0.5, 0.6]
            )
        self.assertFalse(result.is_duplicate)
        merge_mock.assert_not_awaited()

    async def test_weighted_tie_is_conservative(self):
        """SAME 与 DIFFERENT 权重相等（0.85 == 0.85）→ 严格大于不成立，不判重。"""
        engine = self._engine(
            [
                ("n1", "篮球社招新", "内容甲"),
                ("n2", "羽毛球社招新", "内容乙"),
            ]
        )
        same_by_desc = {"内容甲": True, "内容乙": False}
        with (
            patch(
                "briefdesk.plugins.dedup.engine.top_k_similar",
                return_value=[(0, 0.85), (1, 0.85)],
            ),
            patch(
                "briefdesk.plugins.dedup.engine.chat",
                new=AsyncMock(side_effect=await self._chat_by_desc(same_by_desc)),
            ),
            patch(
                "briefdesk.plugins.dedup.engine.merge_source_group", new=AsyncMock()
            ) as merge_mock,
        ):
            result = await engine.check_dedup(
                "篮球社招新", "新生2群", q_emb=[0.5, 0.6]
            )
        self.assertFalse(result.is_duplicate)
        merge_mock.assert_not_awaited()

    async def test_equal_weights_equivalent_to_majority(self):
        """等权 2 SAME vs 1 DIFFERENT → 加权多数命中（等价原 >K/2 规则）。"""
        engine = self._engine(
            [
                ("n1", "篮球社招新", "内容甲"),
                ("n2", "羽毛球社招新", "内容乙"),
                ("n3", "排球社招新", "内容丙"),
            ]
        )
        same_by_desc = {"内容甲": True, "内容乙": True, "内容丙": False}
        with (
            patch(
                "briefdesk.plugins.dedup.engine.top_k_similar",
                return_value=[(0, 0.85), (1, 0.85), (2, 0.85)],
            ),
            patch(
                "briefdesk.plugins.dedup.engine.chat",
                new=AsyncMock(side_effect=await self._chat_by_desc(same_by_desc)),
            ),
            patch(
                "briefdesk.plugins.dedup.engine.merge_source_group", new=AsyncMock()
            ) as merge_mock,
        ):
            result = await engine.check_dedup(
                "篮球社招新", "新生2群", q_emb=[0.5, 0.6]
            )
        self.assertTrue(result.is_duplicate)
        self.assertEqual(result.similar_to_id, "n1")
        merge_mock.assert_awaited_once_with("n1", "新生2群")


class QuoteShortcutTest(unittest.IsolatedAsyncioTestCase):
    """原文哈希精确短路（原文逐字节等价与哈希等价两类判定合一）。

    回归背景：同一条原文被上游重复投递（msg_id 不同但内容逐字节相同）时，
    AI 概括的标题不稳定令余弦擦边（非 99%+）、单候选 AI 判定误判 DIFFERENT
    → 重复卡入库。对策 = 原文（source_quote）哈希全等直接判重，零 AI；
    纯占位符原文（[图片] 等）不参与（交给 image_urls 源限定短路），防
    同文异图误判。
    """

    def _engine(self, items: list[tuple[str, str, str]]) -> DedupEngine:
        """items: (id, title, source_quote)；构造已加载缓存。

        content_hash 由原文派生（与入库/预热同公式），模拟 DB 加载值。
        """
        engine = DedupEngine()
        engine._cache_loaded = True
        engine._cache = [
            CachedItem(
                id=it_id,
                title=t,
                content_hash=DedupEngine._content_hash(q) if q else "",
                source_quote=q,
            )
            for it_id, t, q in items
        ]
        return engine

    @staticmethod
    def _resp(same: bool) -> SimpleNamespace:
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content='{"same": true}' if same else '{"same": false}'
                    ),
                    finish_reason="stop",
                )
            ]
        )

    async def test_quote_shortcut_same_source_quote(self):
        """原文逐字节一致 → 直接判重合并，不调用 AI。"""
        quote = "工作提醒：\n1. 一寸照片（穿校服，白底）＋工作格言，截止日期7月31日"
        engine = self._engine([("q1", "部门工作提醒（多项任务）", quote)])
        with (
            patch("briefdesk.plugins.dedup.engine.chat", new=AsyncMock()) as chat_mock,
            patch(
                "briefdesk.plugins.dedup.engine.merge_source_group", new=AsyncMock()
            ) as merge_mock,
        ):
            result = await engine.check_dedup(
                "部门工作提醒（多项任务）",
                "我们四个",
                source_quote=quote,
            )
        self.assertTrue(result.is_duplicate)
        self.assertEqual(result.similar_to_id, "q1")
        merge_mock.assert_awaited_once_with("q1", "我们四个")
        chat_mock.assert_not_awaited()

    async def test_quote_skips_placeholder(self):
        """纯占位符原文（[图片]）不触发原文短路，交由正常判定（防同文异图误判）。"""
        engine = self._engine([("p1", "部门工作提醒", "[图片]")])
        with (
            patch(
                "briefdesk.plugins.dedup.engine.chat",
                new=AsyncMock(return_value=self._resp(False)),
            ) as chat_mock,
            patch(
                "briefdesk.plugins.dedup.engine.merge_source_group", new=AsyncMock()
            ) as merge_mock,
        ):
            result = await engine.check_dedup(
                "部门工作提醒", "群", source_quote="[图片]"
            )
        # 原文短路未触发 → 走候选路径（title 相同 overlap 1.0 → strong 单候选
        # → chat 判 DIFFERENT）→ 不判重、不合并
        self.assertFalse(result.is_duplicate)
        merge_mock.assert_not_awaited()
        chat_mock.assert_awaited()

    async def test_quote_differs_no_shortcut(self):
        """原文不同 → 不触发原文短路，正常路径不判重。"""
        engine = self._engine(
            [("d1", "部门工作提醒", "原文A：照片截止7月31日")]
        )
        with (
            patch(
                "briefdesk.plugins.dedup.engine.merge_source_group", new=AsyncMock()
            ) as merge_mock,
        ):
            result = await engine.check_dedup(
                "其它标题", "群", source_quote="原文B：视频截止8月15日"
            )
        self.assertFalse(result.is_duplicate)
        merge_mock.assert_not_awaited()

    async def test_quote_empty_no_shortcut(self):
        """source_quote 为空 → 不参与原文短路（兼容未传参的旧调用）。"""
        engine = self._engine([("e1", "部门工作提醒", "原文")])
        with (
            patch(
                "briefdesk.plugins.dedup.engine.merge_source_group", new=AsyncMock()
            ) as merge_mock,
        ):
            result = await engine.check_dedup("其它标题", "群")
        self.assertFalse(result.is_duplicate)
        merge_mock.assert_not_awaited()

class LockEmbedFallbackTest(unittest.IsolatedAsyncioTestCase):
    """P1 修复回归：q_emb 缺失（preembed 失败/未预嵌）时判重绝不触发远程嵌入。

    check_dedup 运行于 pipeline 存储锁内：此前 q_emb=None 且嵌入就绪会逐条
    await embed_texts——嵌入端点挂起时以"行数 × SDK 超时"放大锁持有时间。
    修复后一律降级字符重叠通道，零嵌入调用。"""

    def _engine(self, items):
        engine = DedupEngine()
        engine._cache_loaded = True
        engine._embed_cache_ok = True  # 嵌入功能可用，但本条查询没有预计算向量
        engine._cache = [
            CachedItem(id=i, title=t, source_quote=d, embedding=[0.1, 0.2])
            for i, t, d in items
        ]
        return engine

    @staticmethod
    def _resp(same):
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content='{"same": true}' if same else '{"same": false}'
                    ),
                    finish_reason="stop",
                )
            ]
        )

    async def test_missing_q_emb_never_calls_embed_overlap_channel_hits(self):
        """无预嵌向量 → 零 embed 调用、零余弦调用；标题逐字相同经 overlap 兜底
        进 strong 短路判定，行为与有向量时一致。"""
        engine = self._engine([("o1", "玉言辩论社招新", "位育玉言辩论社招新啦")])
        embed_mock = AsyncMock(return_value=[[0.5, 0.6]])
        topk_mock = Mock(side_effect=AssertionError("余弦通道不应被触发"))
        with (
            patch("briefdesk.plugins.dedup.engine.embed_texts", new=embed_mock),
            patch("briefdesk.plugins.dedup.engine.top_k_similar", new=topk_mock),
            patch(
                "briefdesk.plugins.dedup.engine.chat",
                new=AsyncMock(return_value=self._resp(True)),
            ),
            patch(
                "briefdesk.plugins.dedup.engine.merge_source_group", new=AsyncMock()
            ) as merge_mock,
        ):
            result = await engine.check_dedup("玉言辩论社招新", "新生2群")
        embed_mock.assert_not_awaited()
        topk_mock.assert_not_called()
        self.assertTrue(result.is_duplicate)
        self.assertEqual(result.similar_to_id, "o1")
        merge_mock.assert_awaited_once_with("o1", "新生2群")

    async def test_missing_q_emb_no_candidate_returns_clean(self):
        """无预嵌向量且重叠无候选 → 正常返回不判重，仍零嵌入/AI 调用。"""
        engine = self._engine([("x1", "篮球社招新", "欢迎加入篮球社")])
        embed_mock = AsyncMock()
        chat_mock = AsyncMock()
        with (
            patch("briefdesk.plugins.dedup.engine.embed_texts", new=embed_mock),
            patch("briefdesk.plugins.dedup.engine.chat", new=chat_mock),
            patch(
                "briefdesk.plugins.dedup.engine.merge_source_group", new=AsyncMock()
            ),
        ):
            result = await engine.check_dedup("完全无关的话题", "新生2群")
        embed_mock.assert_not_awaited()
        chat_mock.assert_not_awaited()
        self.assertFalse(result.is_duplicate)


class CandidateErrorIsolationTest(unittest.IsolatedAsyncioTestCase):
    """单候选 AI 异常不中止整批：gather 改 return_exceptions=True，
    加权多数票路径异常候选剔除出计权（既无 SAME 票也不占分母——远程
    审计 S1 语义；全部失败退化为保守不判重）并打 WARNING。"""

    def _engine(self, items):
        engine = DedupEngine()
        engine._cache_loaded = True
        engine._cache = [
            CachedItem(id=i, title=t, source_quote=d, embedding=[0.1, 0.2])
            for i, t, d in items
        ]
        return engine

    @staticmethod
    def _resp(same):
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content='{"same": true}' if same else '{"same": false}'
                    ),
                    finish_reason="stop",
                )
            ]
        )

    @staticmethod
    def _chat_by_desc(outcome_by_desc):
        async def fake_chat(messages, **kwargs):
            user = messages[1]["content"]
            block_a = user.split("消息B：")[0]
            desc = block_a.split("内容：", 1)[1].strip()
            outcome = outcome_by_desc[desc]
            if isinstance(outcome, Exception):
                raise outcome
            return CandidateErrorIsolationTest._resp(bool(outcome))

        return fake_chat

    async def test_vote_survives_single_candidate_error(self):
        """三候选一票异常：剔除后剩 0.85 对 0.85，未过半数 → 不判重（不抛错）。"""
        engine = self._engine(
            [
                ("n1", "篮球社招新", "内容甲"),
                ("n2", "羽毛球社招新", "内容乙"),
                ("n3", "排球社招新", "内容丙"),
            ]
        )
        outcomes = {
            "内容甲": True,
            "内容乙": RuntimeError("判重 API 瞬时故障"),
            "内容丙": False,
        }
        with (
            patch(
                "briefdesk.plugins.dedup.engine.top_k_similar",
                return_value=[(0, 0.85), (1, 0.85), (2, 0.85)],
            ),
            patch(
                "briefdesk.plugins.dedup.engine.chat",
                new=AsyncMock(side_effect=self._chat_by_desc(outcomes)),
            ),
            patch(
                "briefdesk.plugins.dedup.engine.merge_source_group", new=AsyncMock()
            ) as merge_mock,
            self.assertLogs("briefdesk.plugins.dedup.engine", level="WARNING") as logs,
        ):
            result = await engine.check_dedup("篮球社招新", "新生2群", q_emb=[0.5, 0.6])
        self.assertFalse(result.is_duplicate)
        merge_mock.assert_not_awaited()
        self.assertTrue(any("剔除该候选票" in line for line in logs.output))

    async def test_error_candidate_zero_weight_others_can_still_hit(self):
        """异常票剔除出计权：其余两 SAME 票 1.7 > 剩余半数 0.85 → 命中 n1。"""
        engine = self._engine(
            [
                ("n1", "篮球社招新", "内容甲"),
                ("n2", "羽毛球社招新", "内容乙"),
                ("n3", "排球社招新", "内容丙"),
            ]
        )
        outcomes = {
            "内容甲": True,
            "内容乙": RuntimeError("判重 API 瞬时故障"),
            "内容丙": True,
        }
        with (
            patch(
                "briefdesk.plugins.dedup.engine.top_k_similar",
                return_value=[(0, 0.85), (1, 0.85), (2, 0.85)],
            ),
            patch(
                "briefdesk.plugins.dedup.engine.chat",
                new=AsyncMock(side_effect=self._chat_by_desc(outcomes)),
            ),
            patch(
                "briefdesk.plugins.dedup.engine.merge_source_group", new=AsyncMock()
            ) as merge_mock,
        ):
            result = await engine.check_dedup("篮球社招新", "新生2群", q_emb=[0.5, 0.6])
        self.assertTrue(result.is_duplicate)
        self.assertEqual(result.similar_to_id, "n1")
        merge_mock.assert_awaited_once_with("n1", "新生2群")

class AskAiFailureIsolationTest(unittest.IsolatedAsyncioTestCase):
    """S1 回归：单个候选 AI 判定失败不得抛穿 check_dedup 中止整轮管道。

    失败候选按"无票"处理（剔除权重、不参与多数票），全部失败保守判
    不重复——与 classify（failed 重试）/merge（None 降级）的容错语义对齐。
    """

    def _engine(self, items: list[tuple[str, str, str]]) -> DedupEngine:
        engine = DedupEngine()
        engine._cache_loaded = True
        engine._cache = [
            CachedItem(id=i, title=t, source_quote=q, embedding=[0.1, 0.2])
            for i, t, q in items
        ]
        return engine

    @staticmethod
    def _resp(same: bool) -> SimpleNamespace:
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content='{"same": true}' if same else '{"same": false}'
                    ),
                    finish_reason="stop",
                )
            ]
        )

    async def test_single_candidate_ai_failure_returns_not_duplicate(self):
        """单候选 AI 报错：保守返回不重复，异常不得抛出。"""
        engine = self._engine([("a1", "篮球社招新", "内容甲")])
        with (
            patch(
                "briefdesk.plugins.dedup.engine.chat",
                new=AsyncMock(side_effect=RuntimeError("API down")),
            ),
            patch(
                "briefdesk.plugins.dedup.engine.merge_source_group", new=AsyncMock()
            ),
        ):
            result = await engine.check_dedup("篮球社招新", "新生2群", q_emb=[0.5, 0.6])
        self.assertFalse(result.is_duplicate)

    async def test_majority_vote_excludes_failed_candidate_weight(self):
        """两候选一败一 SAME：失败票剔除后，剩余 SAME 票正常计权命中。"""
        engine = self._engine(
            [("a1", "篮球社招新", "内容甲"), ("a2", "篮球社团招新啦", "内容乙")]
        )

        async def flaky_chat(messages, **kwargs):
            # 按"消息A"引用内容判定成败（与候选内部排序解耦）：
            # a1（内容甲）的判定必失败，a2 正常判 SAME
            user = messages[1]["content"]
            block_a = user.split("消息B：")[0]
            if "内容甲" in block_a:
                raise RuntimeError("API down")
            return self._resp(True)

        with (
            patch("briefdesk.plugins.dedup.engine.chat", new=flaky_chat),
            patch(
                "briefdesk.plugins.dedup.engine.merge_source_group",
                new=AsyncMock(),
            ) as merge_mock,
        ):
            result = await engine.check_dedup("篮球社招新", "新生2群", q_emb=[0.5, 0.6])
        self.assertTrue(result.is_duplicate)
        self.assertEqual(result.similar_to_id, "a2")
        merge_mock.assert_awaited_once_with("a2", "新生2群")

    async def test_strong_shortcircuit_failure_conservative(self):
        """strong 候选（余弦≥0.99）AI 失败：不抛错，无其余候选保守判不重复。"""
        engine = self._engine([("s1", "篮球社招新", "内容甲")])
        with (
            patch(
                "briefdesk.plugins.dedup.engine.chat",
                new=AsyncMock(side_effect=RuntimeError("API down")),
            ),
            patch(
                "briefdesk.plugins.dedup.engine.merge_source_group", new=AsyncMock()
            ) as merge_mock,
        ):
            result = await engine.check_dedup("篮球社招新", "新生2群", q_emb=[0.1, 0.2])
        self.assertFalse(result.is_duplicate)
        merge_mock.assert_not_awaited()

    async def test_weak_mode_all_failed_conservative(self):
        """weak 低置信复核全员 AI 失败：视为非 SAME 票，保守不判重。"""
        engine = DedupEngine()
        engine._cache_loaded = True
        engine._cache = [
            CachedItem(id="w1", title="羽毛球社招新", source_quote="内容甲", embedding=[0.7, 0.7])
        ]
        with (
            patch(
                "briefdesk.plugins.dedup.engine.chat",
                new=AsyncMock(side_effect=RuntimeError("API down")),
            ),
            patch(
                "briefdesk.plugins.dedup.engine.merge_source_group", new=AsyncMock()
            ),
        ):
            result = await engine.check_dedup("羽毛球社招新", "新生2群", q_emb=[1.0, 0.0])
        self.assertFalse(result.is_duplicate)

class DegradedChannelLogGateTest(unittest.IsolatedAsyncioTestCase):
    """检索通道降级的三个一次性闸门：首条 WARNING/INFO 可见，其后降 DEBUG。

    三者都是配置/数据态，对进程内**每条消息**恒成立。不加闸门就是每条消息
    一行告警，批次里的阶段行与真正的告警全被挤出屏幕；但完全静默又会让
    「判重悄悄退化成字面匹配」无从发现——故首条保留在默认级别。

    这里同时钉住文案里的持续性表述：维度不一致在向量重建前不会自愈
    （load_embeddings 按模型名取，模型名不变而维度变了则永不重嵌），
    唯一那行 WARNING 若读起来像瞬态抖动就会被放过去。
    """

    _LOGGER = "briefdesk.plugins.dedup.engine"

    @staticmethod
    def _engine(embedding):
        """构造只含一条缓存的引擎（embedding 决定走哪条降级分支）。"""
        engine = DedupEngine()
        engine._cache_loaded = True
        engine._embed_cache_ok = True
        engine._cache = [
            CachedItem(
                id="c1", title="部门工作提醒", source_quote="原文甲", embedding=embedding
            )
        ]
        return engine

    async def _check(self, engine, q_emb):
        with patch(
            "briefdesk.plugins.dedup.engine.merge_source_group", new=AsyncMock()
        ):
            return await engine.check_dedup("其它标题", "群", q_emb=q_emb)

    async def test_dim_mismatch_warns_once_then_debug(self):
        # 缓存 2 维、query 3 维 → 走「维度不一致」分支
        engine = self._engine([0.1, 0.2])
        with self.assertLogs(self._LOGGER, level="WARNING") as logs:
            result = await self._check(engine, [0.1, 0.2, 0.3])
        self.assertFalse(result.is_duplicate, "降级不改判定：回退重叠后仍不重复")
        joined = "\n".join(logs.output)
        self.assertIn("query=3", joined)
        self.assertIn("缓存=2", joined)
        self.assertIn("需重建 item_embeddings", joined, "文案须说清不会自愈")

        # 第二条同状态消息：WARNING 不再出现，信息仍完整保留在 DEBUG
        with self.assertNoLogs(self._LOGGER, level="WARNING"):
            await self._check(engine, [0.1, 0.2, 0.3])
        with self.assertLogs(self._LOGGER, level="DEBUG") as logs2:
            await self._check(engine, [0.1, 0.2, 0.3])
        self.assertTrue(
            any("维度不一致" in m for m in logs2.output), logs2.output
        )

    async def test_cosine_failure_warns_once_with_stack_then_debug(self):
        """余弦异常：首条带栈（真 numpy 异常靠它定位），其后降 DEBUG。"""
        engine = self._engine([0.1, 0.2])
        boom = Mock(side_effect=ValueError("inhomogeneous shape"))
        with patch("briefdesk.plugins.dedup.engine.top_k_similar", new=boom):
            with self.assertLogs(self._LOGGER, level="WARNING") as logs:
                result = await self._check(engine, [0.3, 0.4])
            self.assertFalse(result.is_duplicate)
            self.assertIn("Traceback", "\n".join(logs.output), "首条须带栈")

            with self.assertNoLogs(self._LOGGER, level="WARNING"):
                await self._check(engine, [0.3, 0.4])

    async def test_empty_cache_embeddings_logs_once_then_debug(self):
        """缓存无向量（EMBED_* 配错/历史未补齐）：闸门此前无测试，一并钉住。"""
        engine = self._engine(None)
        with self.assertLogs(self._LOGGER, level="INFO") as logs:
            await self._check(engine, [0.1, 0.2])
        self.assertTrue(
            any("缓存无嵌入向量" in m for m in logs.output), logs.output
        )
        with self.assertNoLogs(self._LOGGER, level="INFO"):
            await self._check(engine, [0.1, 0.2])

    async def test_gates_are_per_instance(self):
        """闸门是实例级：另一个引擎（或重建后的实例）仍能看见首条告警。

        先把第一个引擎的闸门推到关闭态（第二次调用必须静默），再验证新实例
        照样告警——两步都在，才能区分「实例级闸门」与「压根没闸门」。
        """
        first = self._engine([0.1, 0.2])
        with self.assertLogs(self._LOGGER, level="WARNING"):
            await self._check(first, [0.1, 0.2, 0.3])
        with self.assertNoLogs(self._LOGGER, level="WARNING"):
            await self._check(first, [0.1, 0.2, 0.3])

        second = self._engine([0.1, 0.2])
        with self.assertLogs(self._LOGGER, level="WARNING"):
            await self._check(second, [0.1, 0.2, 0.3])


if __name__ == "__main__":
    unittest.main()
