"""ai_ports.chat 遥测包装与 merge 余弦-真值旁路的集成测试。

覆盖复核点名的回归缺口：
- chat() 三路计数（call/error/cancelled）与延迟分布口径（TimeoutError
  必须进分布——判官超时正是要观测的最坏尾部；CancelledError 不得进）；
- telemetry_kind 不透传给供应商（旧桩零感知）；
- merge _record_cosine_truth 的 Mock 桩防御与缺向量跳过。
"""

from __future__ import annotations

import asyncio
import importlib
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from briefdesk import ai_ports, telemetry
from briefdesk.config import config

# merge 包的 __init__ 把 plugin（实例）re-export 到包级，须 importlib 取
# 真正的模块对象
merge_plugin: Any = importlib.import_module("briefdesk.plugins.merge.plugin")


@pytest.fixture(autouse=True)
def _reset_telemetry(monkeypatch: pytest.MonkeyPatch) -> Any:
    telemetry.reset_for_tests()
    ai_ports.set_ai(None)
    # 固定为开启：本文件的断言都是「计数确实发生了」。宿主 shell 若导出了
    # TELEMETRY_ENABLED=false，config 单例在 import 期就已关掉，conftest 的
    # 清环境夹具晚于 import、改不了已构造的实例。
    monkeypatch.setattr(config, "telemetry_enabled", True)
    yield
    telemetry.reset_for_tests()
    ai_ports.set_ai(None)


def _resp(content: str = "ok") -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content), finish_reason="stop")]
    )


class TestChatTelemetry:
    async def test_call_counts_and_latency(self):
        provider = Mock()
        provider.chat = AsyncMock(return_value=_resp())
        ai_ports.set_ai(provider)
        await ai_ports.chat([{"role": "user", "content": "x"}], temperature=0.1, max_tokens=4,
                            telemetry_kind="judge.demo")
        snap = telemetry.snapshot()
        assert snap["counters"]["judge.demo.call|"] == 1
        entry = snap["latencies"]["judge.demo.latency"]["-"]
        assert entry["window_count"] == 1
        assert entry["total_count"] == 1

    async def test_kind_not_forwarded_to_provider(self):
        provider = Mock()
        provider.chat = AsyncMock(return_value=_resp())
        ai_ports.set_ai(provider)
        await ai_ports.chat([{"role": "user", "content": "x"}], temperature=0.1, max_tokens=4,
                            telemetry_kind="k", telemetry_source="weflow")
        _args, kwargs = provider.chat.await_args
        assert "telemetry_kind" not in kwargs
        # 源维度同理：只是端口层的读数标注，供应商的参数集不因遥测变化
        assert "telemetry_source" not in kwargs

    async def test_source_dimension_lands_in_counters_and_latency(self):
        provider = Mock()
        provider.chat = AsyncMock(return_value=_resp())
        ai_ports.set_ai(provider)
        await ai_ports.chat([{"role": "user", "content": "x"}], temperature=0.1,
                            max_tokens=4, telemetry_kind="judge.dedup",
                            telemetry_source="weflow")
        snap = telemetry.snapshot()
        assert snap["counters"]["judge.dedup.call|weflow"] == 1
        entry = snap["latencies"]["judge.dedup.latency"]["weflow"]
        assert entry["total_count"] == 1

    async def test_error_bucket_carries_source(self):
        provider = Mock()
        provider.chat = AsyncMock(side_effect=RuntimeError("boom"))
        ai_ports.set_ai(provider)
        with pytest.raises(RuntimeError):
            await ai_ports.chat([{"role": "user", "content": "x"}], temperature=0.1,
                                max_tokens=4, telemetry_kind="judge.merge",
                                telemetry_source="qqflow")
        counters = telemetry.snapshot()["counters"]
        assert counters["judge.merge.error|qqflow"] == 1
        assert "judge.merge.call|qqflow" not in counters

    async def test_timeout_error_counts_and_enters_latency(self):
        provider = Mock()
        provider.chat = AsyncMock(side_effect=TimeoutError())
        ai_ports.set_ai(provider)
        with pytest.raises(asyncio.TimeoutError):
            await ai_ports.chat([{"role": "user", "content": "x"}], temperature=0.1,
                                max_tokens=4, telemetry_kind="judge.dedup")
        snap = telemetry.snapshot()
        assert snap["counters"]["judge.dedup.error|"] == 1
        # 超时是最坏尾部的常态来源：必须留在延迟分布里（否则 p99 系统性低估）
        assert snap["latencies"]["judge.dedup.latency"]["-"]["window_count"] == 1

    async def test_cancelled_counted_and_excluded_from_latency(self):
        provider = Mock()
        provider.chat = AsyncMock(side_effect=asyncio.CancelledError())
        ai_ports.set_ai(provider)
        with pytest.raises(asyncio.CancelledError):
            await ai_ports.chat([{"role": "user", "content": "x"}], temperature=0.1,
                                max_tokens=4, telemetry_kind="title.merge")
        snap = telemetry.snapshot()
        assert snap["counters"]["title.merge.cancelled|"] == 1
        assert snap["latencies"].get("title.merge.latency", {}).get("-") is None


class TestMergeCosineTruthBypass:
    """_record_cosine_truth 只补标定数据，绝不连坐合并判定。"""

    def _ctx(self, dedup: object) -> SimpleNamespace:
        return SimpleNamespace(dedup=dedup)

    def _row(self, item_id: str = "new1", source: str = "weflow") -> SimpleNamespace:
        return SimpleNamespace(item_id=item_id, msg=SimpleNamespace(source=source))

    def test_mock_dedup_stub_skips(self):
        # 测试桩（Mock）返回 Mock 对象：类型守卫跳过，不抛不记
        stub = Mock()
        stub.cached_embedding = Mock(return_value=Mock())
        merge_plugin._record_cosine_truth(
            self._ctx(stub), self._row(), {"id": "c1"}, True
        )
        assert telemetry.cosine_truth_rows() == []

    def test_missing_vectors_skip(self):
        stub = SimpleNamespace(cached_embedding=lambda _id: None)
        merge_plugin._record_cosine_truth(
            self._ctx(stub), self._row(), {"id": "c1"}, False
        )
        assert telemetry.cosine_truth_rows() == []

    def test_no_cached_embedding_attr_skips(self):
        merge_plugin._record_cosine_truth(
            self._ctx(SimpleNamespace()), self._row(), {"id": "c1"}, True
        )
        assert telemetry.cosine_truth_rows() == []

    def test_vectors_recorded_with_verdict(self):
        stub = SimpleNamespace(
            cached_embedding=lambda _id: [1.0, 0.0] if _id == "new1" else [0.0, 1.0]
        )
        merge_plugin._record_cosine_truth(
            self._ctx(stub), self._row(), {"id": "c1"}, False
        )
        rows = telemetry.cosine_truth_rows()
        assert len(rows) == 1
        source, cos, verdict = rows[0]
        assert (source, verdict) == ("weflow", False)
        assert cos == pytest.approx(0.0)

    def test_embedding_error_swallowed(self):
        def _boom(_id: str) -> list[float]:
            raise RuntimeError("cache gone")

        stub = SimpleNamespace(cached_embedding=_boom)
        merge_plugin._record_cosine_truth(
            self._ctx(stub), self._row(), {"id": "c1"}, True
        )
        assert telemetry.cosine_truth_rows() == []


class TestJudgeSourceDimension:
    """判官调用的源维度：去重取候选条目自带 source，合并取新消息侧 source。"""

    async def test_dedup_judge_carries_item_source(self):
        from briefdesk.plugins.dedup.engine import CachedItem, DedupEngine

        provider = Mock()
        provider.chat = AsyncMock(return_value=_resp('{"same": true}'))
        ai_ports.set_ai(provider)
        # 直接测单候选判定：它是「源维度从哪里来」的唯一连接点，走完整
        # check_dedup 需要库与缓存装配，与本断言无关
        verdict = await DedupEngine()._ask_ai(
            CachedItem(id="i1", title="标题", source="weflow", source_quote="原文"),
            "候选标题",
            "候选原文",
        )
        assert verdict is True
        assert telemetry.snapshot()["counters"]["judge.dedup.call|weflow"] == 1

    async def test_merge_judge_carries_tail_source(self):
        from briefdesk.plugins.merge.engine import judge_merge

        provider = Mock()
        provider.chat = AsyncMock(return_value=_resp('{"merge": false}'))
        ai_ports.set_ai(provider)
        assert await judge_merge("a", "b", "c", "d", source="qqflow") is False
        assert telemetry.snapshot()["counters"]["judge.merge.call|qqflow"] == 1

    async def test_title_rewrite_carries_source(self):
        from briefdesk.plugins.merge.engine import summarize_title

        provider = Mock()
        provider.chat = AsyncMock(return_value=_resp('{"title":"新标题"}'))
        ai_ports.set_ai(provider)
        assert await summarize_title("旧标题", "关键信息", "原文", source="bench") == "新标题"
        assert telemetry.snapshot()["counters"]["title.merge.call|bench"] == 1


class TestRagChatFallback:
    """rag 专用通道回退单独计数：与「回答失败」是两种不同故障。"""

    async def test_counted_when_provider_lacks_rag_chat(self):
        provider = Mock()  # Mock 类没有声明 rag_chat → 走回退分支
        provider.chat = AsyncMock(return_value=_resp())
        ai_ports.set_ai(provider)
        await ai_ports.rag_chat([{"role": "user", "content": "q"}])
        assert telemetry.snapshot()["counters"]["rag.chat_fallback|"] == 1

    async def test_not_counted_when_specialized_channel_exists(self):
        class _Modern(Mock):
            async def rag_chat(self, messages, **kwargs):
                return _resp()

        ai_ports.set_ai(_Modern())
        await ai_ports.rag_chat([{"role": "user", "content": "q"}])
        assert "rag.chat_fallback|" not in telemetry.snapshot()["counters"]
