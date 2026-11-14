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
                            telemetry_kind="k")
        _args, kwargs = provider.chat.await_args
        assert "telemetry_kind" not in kwargs

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
