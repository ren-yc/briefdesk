"""benchmark 引擎 merge 预筛的容错与镜像口径单测（不调用真实 AI）。

背景：预筛门是判定行为的一部分，基准必须与生产同口径——生产路径在
「向量缺失/余弦算不出」时降级为不预筛、照常调判官。若基准在余弦异常时
直接中止整轮，供应商返回坏向量的情形（最需要对照数据）反而跑不出报告。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from briefdesk.plugins.benchmark import engine as bench_engine
from briefdesk.plugins.benchmark import store as bench_store


def _merge_case() -> bench_store.MergeCase:
    return bench_store.MergeCase.model_validate(
        {
            "id": "merge-1",
            "head": {"msg_id": "m1", "content": "团购 价格", "title": "团购"},
            "tail": {"msg_id": "m2", "content": "补充 运费", "title": "补充"},
            "expected": {"merge": True},
        }
    )


class TestMergePrefilterResilience:
    """预筛门的三种降级：嵌入失败、余弦异常、正常低于阈值。"""

    @pytest.fixture(autouse=True)
    def _enable(self):
        with (
            patch.object(bench_engine.config, "merge_prefilter_threshold", 0.8),
            patch.object(bench_engine.ai_ports, "is_embedding_enabled", return_value=True),
        ):
            yield

    async def test_cosine_exception_degrades_to_judge(self):
        """坏向量令 cosine_similarity 抛异常 → 跳过预筛、继续判官（不中止）。"""
        judge = AsyncMock(return_value=True)
        with (
            patch.object(bench_engine.ai_ports, "embed_texts", AsyncMock(return_value=[[1.0], [1.0]])),
            patch.object(
                bench_engine.ai_ports,
                "cosine_similarity",
                side_effect=ValueError("维度不一致"),
            ),
            patch.object(bench_engine, "judge_merge", judge),
        ):
            result = await bench_engine._run_merge(_merge_case())
        assert judge.await_count == 1, "余弦异常后必须继续调判官"
        assert result.predicted is True
        assert not result.skipped

    async def test_embed_failure_degrades_to_judge(self):
        judge = AsyncMock(return_value=False)
        with (
            patch.object(
                bench_engine.ai_ports, "embed_texts", AsyncMock(side_effect=RuntimeError("down"))
            ),
            patch.object(bench_engine, "judge_merge", judge),
        ):
            result = await bench_engine._run_merge(_merge_case())
        assert judge.await_count == 1
        assert result.predicted is False

    async def test_low_cosine_skips_judge(self):
        """正常低于阈值：不调判官，predicted=False 且 skipped=True。"""
        judge = AsyncMock(return_value=True)
        with (
            patch.object(bench_engine.ai_ports, "embed_texts", AsyncMock(return_value=[[1.0, 0.0], [0.0, 1.0]])),
            patch.object(bench_engine, "judge_merge", judge),
        ):
            result = await bench_engine._run_merge(_merge_case())
        assert judge.await_count == 0, "低于阈值不应调用判官"
        assert result.predicted is False
        assert result.skipped is True
