"""分类观察记录与采集链路测试：_parse_response 四元组 + recorder classify。

覆盖（分类数据采集交付物口径）：
- per-instance 观察记录的四种形态（include/exclude/uncertain/error）与
  输入摘要截断；
- offset 平移与拆半重试合并；
- recorder 批级聚合（expected 只收 include，uncertain 不污染评分）；
- 节律导出首见锚定不导出（防首批记录立即清空）。
"""

import importlib
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from briefdesk.plugins.classify.engine import _parse_response
from briefdesk.types import BatchContext, ClassifyObservation, InternalMessage

# merge/benchmark 包的 __init__ 把实例 re-export 到包级，importlib 取真模块
bench_recorder: object = importlib.import_module("briefdesk.plugins.benchmark.recorder")

ALLOWED = {"活动通知", "交易"}


def _make_batch(observations: list[ClassifyObservation]) -> BatchContext:
    msgs = [
        InternalMessage(
            msg_id=f"m{i}",
            content=f"内容{i}",
            sender_name="甲",
            sender_id="u",
            session_id="s",
            group_name="g",
            timestamp=100 + i,
            source="weflow-legacy",
        )
        for i in range(3)
    ]
    b = BatchContext(messages=msgs, client=SimpleNamespace())
    b.classify_checks = observations
    return b


class TestParseResponseObservations:
    def test_include_and_exclude(self):
        _results, _retry, _times, obs = _parse_response(
            '{"task":"classify","data":['
            '{"index":0,"include":true,"category":"活动通知","quote":"q0","key":["k"]},'
            '{"index":1,"include":false}]}',
            ALLOWED,
            2,
            ["内容零", "内容一"],
        )
        assert [(o.msg_index, o.outcome) for o in obs] == [
            (0, "include"),
            (1, "exclude"),
        ]
        inc = obs[0]
        assert inc.category == "活动通知"
        assert inc.input_digest == "内容零"
        assert inc.quote == "q0"
        assert inc.key == "k"

    def test_unknown_category_is_uncertain(self):
        _results, retry, _times, obs = _parse_response(
            '{"task":"classify","data":[{"index":0,"include":true,"category":"幻觉类"}]}',
            ALLOWED,
            1,
            ["内容零"],
        )
        assert retry == [0]
        assert len(obs) == 1
        assert obs[0].outcome == "uncertain"

    def test_missing_index_becomes_uncertain(self):
        # AI 漏回 index 1 → 并入 retry，观察记 uncertain
        _results, retry, _times, obs = _parse_response(
            '{"task":"classify","data":[{"index":0,"include":true,"category":"交易"}]}',
            ALLOWED,
            2,
            ["a", "b"],
        )
        assert retry == [1]
        assert [o.outcome for o in obs] == ["include", "uncertain"]

    async def test_semantic_refine_demotion_flips_observation(self):
        """语义裁判否决的条目观察必须从 include 翻为 uncertain。

        回归点：字面模糊条目在 _parse_response 时点已记 include（含
        category）；第二关语义裁判确认漂移后条目移出 results 并入 retry，
        若观察不跟着翻，这批行内错位最严重的样本会带着伪 category 进
        采集数据（benchmark 评分分母失真、训练导出注入伪真值）。
        """
        from unittest.mock import AsyncMock, patch

        from briefdesk.plugins.classify import engine as ce
        from briefdesk.types import InternalMessage

        messages = [
            InternalMessage(
                msg_id=f"m{i}",
                content=f"完整原文{i}",
                sender_name="甲",
                sender_id="u",
                session_id="s",
                group_name="g",
                timestamp=100,
                source="weflow-legacy",
            )
            for i in range(2)
        ]
        content = (
            '{"task":"classify","data":['
            '{"index":0,"include":true,"category":"活动通知",'
            '"quote":"别人的内容","key":["k"]},'
            '{"index":1,"include":true,"category":"交易",'
            '"quote":"完整原文1","key":["k1"]}'
            "]}"
        )
        cats = [{"name": "活动通知", "prompt": "p1"}, {"name": "交易", "prompt": "p2"}]
        with (
            patch.object(
                ce, "_char_quote_verdict", staticmethod(lambda *a, **k: None)
            ),
            # 语义裁判返回「0 号被否决」的形态（真实签名三元组）：
            # results 全空、retry 含 0
            patch.object(
                ce,
                "_semantic_refine",
                new=AsyncMock(return_value=([], [0], [])),
            ),
            patch.object(ce, "extract_times", new=AsyncMock()),
            patch.object(ce, "summarize_results", new=AsyncMock()),
            patch.object(
                ce,
                "chat",
                new=AsyncMock(
                    return_value=SimpleNamespace(
                        choices=[
                            SimpleNamespace(
                                message=SimpleNamespace(content=content),
                                finish_reason="stop",
                            )
                        ]
                    )
                ),
            ),
            # 类别查询桩在引擎模块属性上：真实查库会开真实库连接
            patch(
                "briefdesk.plugins.classify.engine.get_enabled_categories",
                new=AsyncMock(return_value=cats),
            ),
        ):
            outcome = await ce.classify_batch(messages)
        assert [r.msg_index for r in outcome.results] == []
        obs = {o.msg_index: o for o in outcome.observations}
        assert obs[0].outcome == "uncertain", "被语义裁判否决的条目不得保留 include"
        assert obs[0].category == "", "伪 category 必须一并清除"
        # 1 号未被裁判否决（retry 只含 0）：字面模糊进入 ambiguous 后裁判
        # 放行的形态仍应保留原判定——此处 mock 只否决 0 号
        assert obs[1].outcome == "include"
        assert 0 in outcome.failed

    def test_digest_truncated_to_80(self):
        long_content = "x" * 200
        _results, _retry, _times, obs = _parse_response(
            '{"task":"classify","data":[{"index":0,"include":false}]}',
            ALLOWED,
            1,
            [long_content],
        )
        assert len(obs[0].input_digest) == 80

    def test_no_contents_leaves_digest_empty(self):
        _results, _retry, _times, obs = _parse_response(
            '{"task":"classify","data":[{"index":0,"include":false}]}',
            ALLOWED,
            1,
        )
        assert obs[0].input_digest == ""


class TestRecorderClassify:
    @pytest.fixture(autouse=True)
    def _reset(self):
        import briefdesk.plugins.benchmark.recorder as rec

        rec.reset()
        yield
        rec.reset()

    def _batch_with_obs(self):
        inc = ClassifyObservation(
            msg_index=0, outcome="include", category="活动通知",
            input_digest="内容0", quote="q", key="k",
        )
        exc = ClassifyObservation(msg_index=1, outcome="exclude", input_digest="内容1")
        unc = ClassifyObservation(msg_index=2, outcome="uncertain", input_digest="内容2")
        return _make_batch([inc, exc, unc])

    def test_batch_case_aggregates_include_only_expected(self):
        rec = bench_recorder
        batch = self._batch_with_obs()
        added = rec.build_classify_cases(batch)
        assert added == 1
        cases = rec._cases["classify"]
        assert len(cases) == 1
        case = cases[0]
        # expected 只收 include（uncertain/exclude 无类别真值，不入评分）
        assert case["expected"] == [{"index": 0, "category": "活动通知"}]
        assert len(case["messages"]) == 3
        assert "uncertain+error 1" in case["note"]

    def test_no_observations_adds_nothing(self):
        rec = bench_recorder
        batch = _make_batch([])
        assert rec.build_classify_cases(batch) == 0
        assert rec.stats()["counts"]["classify"] == 0

    async def test_scheduled_export_anchors_first_call(self, tmp_path, monkeypatch):
        rec = bench_recorder
        from briefdesk import paths

        monkeypatch.setattr(
            paths, "benchmark_cases_dir", lambda: tmp_path, raising=False
        )
        batch = self._batch_with_obs()
        rec.build_classify_cases(batch)
        # 首见调用只锚定不导出（防首批记录立即清空）
        result = await rec.scheduled_export()
        assert result is None
        assert rec.stats()["counts"]["classify"] == 1
        # 未到节律间隔：继续不导出
        result = await rec.scheduled_export()
        assert result is None
        assert rec.stats()["counts"]["classify"] == 1

    async def test_scheduled_export_failure_retries_next_call(
        self, tmp_path, monkeypatch
    ):
        """导出失败后下个批尾立即重试（锚点回拨一个周期，非再等 24h）。

        回归点：失败若回退为 0 会命中「首见锚定」分支，记录在内存多滞留
        一个完整节律周期，进程退出即丢——恰是节律导出要防的场景。
        """
        rec = bench_recorder
        from briefdesk import paths

        monkeypatch.setattr(
            paths, "benchmark_cases_dir", lambda: tmp_path, raising=False
        )
        rec.build_classify_cases(self._batch_with_obs())
        assert await rec.scheduled_export() is None  # 首见锚定
        # 把锚点拨到已到期
        rec._last_scheduled_export -= rec._EXPORT_INTERVAL_SECONDS
        with patch.object(
            rec,
            "export_recorded",
            side_effect=RuntimeError("disk full"),
        ):
            result = await rec.scheduled_export()
        assert result is None
        assert rec.stats()["counts"]["classify"] == 1, "失败后记录保留"
        # 失败后锚点已回拨：下一次调用立即重试且成功
        result = await rec.scheduled_export()
        assert result is not None and result["total"] == 1
        assert rec.stats()["counts"]["classify"] == 0
