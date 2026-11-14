"""benchmark 引擎 merge 预筛的容错与镜像口径单测（不调用真实 AI）。

背景：预筛门是判定行为的一部分，基准必须与生产同口径——生产路径在
「向量缺失/余弦算不出」时降级为不预筛、照常调判官。若基准在余弦异常时
直接中止整轮，供应商返回坏向量的情形（最需要对照数据）反而跑不出报告。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from briefdesk.plugins.benchmark import engine as bench_engine
from briefdesk.plugins.benchmark import store as bench_store


def _chat_resp(content: str) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=content), finish_reason="stop"
            )
        ]
    )


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


class TestTelemetrySourceIsBenchOnly:
    """基准判官的遥测恒记 bench：用例可能带着导出时的真实源。

    网页导出的用例保留原卡片的 source（weflow/qqflow）：拿它当遥测维度，
    基准反复批量跑产生的调用次数与耗时会混进生产桶，「基准流量单独成桶」失效。
    """

    async def test_merge_case_with_production_source_records_bench(self):
        from briefdesk import ai_ports, telemetry
        from briefdesk.config import config

        telemetry.reset_for_tests()
        case = bench_store.MergeCase.model_validate(
            {
                "id": "merge-1",
                "head": {
                    "msg_id": "m1",
                    "content": "团购 价格",
                    "title": "团购",
                    "source": "weflow",
                },
                "tail": {
                    "msg_id": "m2",
                    "content": "补充 运费",
                    "title": "补充",
                    "source": "weflow",
                },
                "expected": {"merge": True},
            }
        )
        # 走真实的 ai_ports.chat 包装（遥测在端口层记），只把供应商换成桩
        provider = Mock()
        provider.chat = AsyncMock(return_value=_chat_resp('{"merge": true}'))
        provider.is_embedding_enabled = Mock(return_value=False)
        ai_ports.set_ai(provider)
        try:
            with patch.object(config, "telemetry_enabled", True):
                result = await bench_engine._run_merge(case)
            counters = telemetry.snapshot()["counters"]
        finally:
            ai_ports.set_ai(None)
            telemetry.reset_for_tests()
        assert result.predicted is True
        assert counters["judge.merge.call|bench"] == 1
        assert not [k for k in counters if k.startswith("judge.merge.call|weflow")]

    async def test_title_case_with_production_source_records_bench(self):
        from briefdesk import ai_ports, telemetry
        from briefdesk.config import config

        telemetry.reset_for_tests()
        case = bench_store.TitleCase.model_validate(
            {
                "id": "title-1",
                "message": {
                    "msg_id": "m1",
                    "content": "团购 价格",
                    "title": "团购",
                    "source": "qqflow",
                },
                "expected": {"title": "团购（含运费）"},
            }
        )
        provider = Mock()
        provider.chat = AsyncMock(return_value=_chat_resp('{"title":"团购（含运费）"}'))
        ai_ports.set_ai(provider)
        try:
            with patch.object(config, "telemetry_enabled", True):
                await bench_engine._run_title(case)
            counters = telemetry.snapshot()["counters"]
        finally:
            ai_ports.set_ai(None)
            telemetry.reset_for_tests()
        assert counters["title.merge.call|bench"] == 1
        assert not [k for k in counters if k.startswith("title.merge.call|qqflow")]
