"""类别分配器单元测试（纯计算 + 桩嵌入，不触 DB / 真实 AI）。

覆盖：原型重建（去重/覆盖层胜出/嵌入失败保旧缓存/节流）、分配（max/
mean-of-top-k、逐类阈值、无类达标返回 None）、p5 自相似阈值。
"""

from unittest.mock import AsyncMock, patch

import pytest

from briefdesk.plugins.classify import allocator as al


def _vec(x: float) -> list[float]:
    return [x, 1.0 - x]


@pytest.fixture(autouse=True)
def _fresh_allocator():
    al.allocator = al.CategoryAllocator()
    yield
    al.allocator = al.CategoryAllocator()


class TestRebuild:
    async def test_rebuild_groups_and_dedupes(self):
        samples = [
            {"category": "活动通知", "content": "讲座通知"},
            {"category": "活动通知", "content": "讲座通知"},  # 同文去重
            {"category": "交易", "content": "出自行车"},
        ]
        with patch.object(
            al.ai_ports, "embed_texts", new=AsyncMock(return_value=[_vec(0.9), _vec(0.1)])
        ), patch.object(al.ai_ports, "is_embedding_enabled", return_value=True):
            n = await al.allocator.rebuild(samples, force=True)
        assert n == 2  # 同文去重后 2 条
        stats = al.allocator.stats()
        assert stats["categories"]["活动通知"]["prototypes"] == 1
        assert stats["categories"]["交易"]["prototypes"] == 1

    async def test_recat_overlay_wins_by_weight(self):
        # recat 覆盖层（weight=2）与普通样本（weight=1）同 content：
        # 只保留覆盖层，类别归 category_after
        samples = [
            {"category": "旧类", "content": "同文", "weight": 1},
            {"category": "新类", "content": "同文", "weight": 2},
        ]
        with patch.object(
            al.ai_ports, "embed_texts", new=AsyncMock(return_value=[_vec(0.5)])
        ), patch.object(al.ai_ports, "is_embedding_enabled", return_value=True):
            n = await al.allocator.rebuild(samples, force=True)
        assert n == 1
        stats = al.allocator.stats()
        assert set(stats["categories"]) == {"新类"}

    async def test_embed_failure_keeps_old_cache(self):
        samples1 = [{"category": "A", "content": "x"}]
        with patch.object(
            al.ai_ports, "embed_texts", new=AsyncMock(return_value=[_vec(0.9)])
        ), patch.object(al.ai_ports, "is_embedding_enabled", return_value=True):
            await al.allocator.rebuild(samples1, force=True)
        assert al.allocator.stats()["total_prototypes"] == 1
        # 第二次嵌入失败：保留旧缓存
        with patch.object(
            al.ai_ports, "embed_texts", new=AsyncMock(side_effect=RuntimeError("down"))
        ), patch.object(al.ai_ports, "is_embedding_enabled", return_value=True):
            rc = await al.allocator.rebuild(
                [{"category": "B", "content": "y"}], force=True
            )
        assert rc == -2
        assert al.allocator.stats()["total_prototypes"] == 1

    async def test_empty_samples_clears_cache(self):
        with patch.object(al.ai_ports, "is_embedding_enabled", return_value=True):
            rc = await al.allocator.rebuild([], force=True)
        assert rc == 0
        assert al.allocator.stats()["total_prototypes"] == 0

    async def test_throttle_merges_requests(self):
        with patch.object(
            al.ai_ports, "embed_texts", new=AsyncMock(return_value=[_vec(0.9)])
        ), patch.object(al.ai_ports, "is_embedding_enabled", return_value=True):
            first = await al.allocator.rebuild(
                [{"category": "A", "content": "x"}], force=True
            )
            # 刚重建过：非 force 请求被节流合并（不触发第二次嵌入）
            second = await al.allocator.rebuild(
                [{"category": "A", "content": "y"}], force=False
            )
        assert first == 1
        assert second == -1
        assert al.allocator.stats()["rebuilt_pending"] is True

    async def test_stale_rebuild_does_not_overwrite_newer(self):
        """并发重建的代次守卫：旧任务晚到不得覆盖新缓存。

        失败模式：连续两次类别变更各起一个 rebuild，二者都在 await 嵌入；
        旧任务（样本含旧类名）若后完成，会把旧原型写回，缓存长期停在过期
        口径。用可控的 await 顺序复现：先启动旧任务让它挂在嵌入上，再完整
        跑完新任务，最后放行旧任务——旧任务必须返回 -3 且不改变缓存。
        """
        import asyncio

        old_started = asyncio.Event()
        release_old = asyncio.Event()

        async def _embed(texts):
            if texts == ["old-content"]:
                old_started.set()
                await release_old.wait()
                return [_vec(0.1)]
            return [_vec(0.9)]

        with (
            patch.object(al.ai_ports, "embed_texts", new=_embed),
            patch.object(al.ai_ports, "is_embedding_enabled", return_value=True),
        ):
            old_task = asyncio.create_task(
                al.allocator.rebuild(
                    [{"category": "旧类名", "content": "old-content"}], force=True
                )
            )
            await old_started.wait()
            # 新任务先完成并提交
            rc_new = await al.allocator.rebuild(
                [{"category": "新类名", "content": "new-content"}], force=True
            )
            release_old.set()
            rc_old = await old_task

        assert rc_new == 1
        assert rc_old == -3, "过期代次必须返回 -3 并丢弃结果"
        cats = set(al.allocator.stats()["categories"])
        assert cats == {"新类名"}, f"缓存被旧任务覆盖：{cats}"


class TestAllocate:
    async def _seed(self, vectors_by_cat: dict[str, list[list[float]]]):
        samples = [
            {"category": cat, "content": f"{cat}-{i}"}
            for cat, vecs in vectors_by_cat.items()
            for i, _v in enumerate(vecs)
        ]
        flat = [v for vecs in vectors_by_cat.values() for v in vecs]
        with patch.object(
            al.ai_ports, "embed_texts", new=AsyncMock(return_value=flat)
        ), patch.object(al.ai_ports, "is_embedding_enabled", return_value=True):
            rc = await al.allocator.rebuild(samples, force=True)
        assert rc == len(flat)

    async def test_best_category_wins(self):
        # 类 A 原型贴近 (1,0)，类 B 贴近 (0,1)；查询 (0.95, 0.05) → A。
        # A 类只放 2 个高相似原型（p5 自相似阈 ≈0.9998），查询分 0.9995 恰在
        # 阈下——原型必须含一个与查询更近的条目（查询本身在同分布内）
        await self._seed(
            {"A": [_vec(1.0), _vec(0.98), [0.95, 0.05]], "B": [_vec(0.0), _vec(0.02)]}
        )
        result = await al.allocator.allocate_with_vector("q", qvec=[0.95, 0.05])
        assert result is not None and result.category == "A"
        assert result.runner_up == "B"

    async def test_below_threshold_returns_none(self):
        await self._seed({"A": [_vec(1.0), _vec(0.98)]})
        # 正交查询：分数 0 低于该类 p5 阈值 → 无类达标
        result = await al.allocator.allocate_with_vector("q", qvec=[0.0, 1.0])
        assert result is not None and result.category is None

    async def test_per_class_thresholds_independent(self):
        """逐类阈值独立裁决：低阈值类的正确命中不得被高阈值冠军类否决。

        回归点（逐类过阈 → 候选集内取最高分）：A 类同向原型（p5 阈 ≈1.0）
        与 B 类正交原型（p5 阈 ≈0.0）并存时，查询略偏离共享方向——A 得分
        0.9998 低于自身阈值、B 得分 0.51 远超自身阈值，B 必须命中。
        旧实现用全局冠军类（A）的阈值裁决全局，返回 None。
        """
        await self._seed(
            {
                "A": [[1.0, 0.0], [0.999, 0.001]],
                "B": [[1.0, 0.0], [0.0, 1.0]],
            }
        )
        import math

        q = [math.cos(0.02), math.sin(0.02)]
        result = await al.allocator.allocate_with_vector("q", qvec=q)
        assert result is not None and result.category == "B"
        assert result.runner_up == "A"

    async def test_unqualified_returns_top_scores_for_diagnostics(self):
        """无类达标时返回全局最高分与次高分（回退诊断用），category=None。"""
        await self._seed({"A": [[1.0, 0.0], [0.999, 0.001]]})
        result = await al.allocator.allocate_with_vector("q", qvec=[0.0, 1.0])
        assert result is not None
        assert result.category is None
        assert result.runner_up == "" or result.score >= result.runner_up_score

    async def test_empty_cache_returns_none(self):
        assert await al.allocator.allocate_with_vector("q", qvec=[1.0, 0.0]) is None

    async def test_query_embed_failure_returns_none(self):
        await self._seed({"A": [_vec(1.0)]})
        with patch.object(al.ai_ports, "is_embedding_enabled", return_value=True), \
            patch.object(
                al.ai_ports, "embed_texts", new=AsyncMock(side_effect=RuntimeError("down"))
            ):
            result = await al.allocator.allocate_with_vector("q")
        assert result is None


class TestScoreAggregation:
    def test_max_dominates_when_single_strong(self):
        # top-1 强、top-2/3 弱：max 语义生效
        q = [1.0, 0.0]
        vecs = [[1.0, 0.0], [0.0, 1.0], [0.0, 1.0], [0.0, 1.0]]
        assert al._score_against(q, vecs) > 0.99

    def test_mean_topk_when_balanced(self):
        q = [1.0, 0.0]
        vecs = [[0.9, 0.1], [0.8, 0.2], [0.7, 0.3]]
        s = al._score_against(q, vecs)
        assert 0.7 < s < 1.0

    def test_empty_vectors_zero(self):
        assert al._score_against([1.0, 0.0], []) == 0.0


class TestP5SelfSimilarity:
    def test_identical_vectors_high_threshold(self):
        vecs = [[1.0, 0.0]] * 5
        assert al._p5_self_similarity(vecs) > 0.99

    def test_orthogonal_vectors_low_threshold(self):
        vecs = [[1.0, 0.0], [0.0, 1.0], [1.0, 0.0], [0.0, 1.0]]
        p5 = al._p5_self_similarity(vecs)
        assert 0.0 <= p5 < 0.3
