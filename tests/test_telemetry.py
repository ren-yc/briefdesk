"""briefdesk/telemetry.py 的单元测试（纯内存 + 临时落盘目录）。"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from briefdesk import telemetry


@pytest.fixture(autouse=True)
def _reset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    telemetry.reset_for_tests()
    monkeypatch.setattr(telemetry, "telemetry_dir", lambda: tmp_path / "tel")
    yield
    telemetry.reset_for_tests()


def test_record_counts_by_kind_and_source() -> None:
    telemetry.record("judge.call", "weflow")
    telemetry.record("judge.call", "weflow")
    telemetry.record("judge.call", "qqflow")
    telemetry.record("judge.call")
    snap = telemetry.snapshot()
    assert snap["counters"] == {
        "judge.call|": 1,
        "judge.call|qqflow": 1,
        "judge.call|weflow": 2,
    }


def test_observe_percentiles_and_thinning() -> None:
    for i in range(100):
        telemetry.observe("judge.latency", float(i), "src")
    snap = telemetry.snapshot()
    entry = snap["latencies"]["judge.latency"]["src"]
    assert entry["window_count"] == 100
    assert entry["total_count"] == 100
    assert entry["total_ms"] == pytest.approx(sum(range(100)), abs=1.0)
    assert entry["p50_ms"] == pytest.approx(49.5, abs=1.0)
    assert entry["p95_ms"] == pytest.approx(94.0, abs=2.0)
    assert entry["p99_ms"] == pytest.approx(98.0, abs=2.0)


def test_observe_thinning_caps_window_not_totals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 抽稀只收窄分位窗口：全量 count/total 必须保持真实值（谎报总量会让
    # 验收读数失真），窗口分位数是近期分布的近似
    monkeypatch.setattr(telemetry, "_MAX_SAMPLES", 10)
    for i in range(30):
        telemetry.observe("m", float(i))
    snap = telemetry.snapshot()
    entry = snap["latencies"]["m"]["-"]
    assert entry["window_count"] <= 10
    assert entry["total_count"] == 30
    assert entry["total_ms"] == pytest.approx(sum(range(30)), abs=1.0)
    # 抽稀不产生 NaN / 负数
    assert entry["p50_ms"] >= 0


def test_cosine_truth_roundtrip() -> None:
    telemetry.record_cosine_truth("weflow", 0.91, True)
    telemetry.record_cosine_truth("weflow", 0.42, False)
    telemetry.record_cosine_truth("qqflow", 0.77, None)
    rows = telemetry.cosine_truth_rows()
    assert rows == [
        ("weflow", 0.91, True),
        ("weflow", 0.42, False),
        ("qqflow", 0.77, None),
    ]
    snap = telemetry.snapshot()
    assert snap["cosine_truth_count"] == 3


def test_flush_writes_cosine_truth_daily(tmp_path: Path) -> None:
    telemetry.record_cosine_truth("weflow", 0.91, True)
    telemetry.record_cosine_truth("qqflow", 0.42, None)
    telemetry.flush()
    truth_files = list((tmp_path / "tel").glob("cosine_truth-*.jsonl"))
    assert len(truth_files) == 1
    lines = truth_files[0].read_text(encoding="utf-8").strip().splitlines()
    rows = [json.loads(l) for l in lines]
    assert rows == [
        {"boot": telemetry._COSINE_BOOT, "seq": 1, "source": "weflow",
         "cosine": 0.91, "verdict": True},
        {"boot": telemetry._COSINE_BOOT, "seq": 2, "source": "qqflow",
         "cosine": 0.42, "verdict": None},
    ]
    # 增量语义：再次 flush（跨过节流）不重复写已落盘行
    telemetry._last_flush = 0.0
    telemetry.flush()
    lines = truth_files[0].read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2


def test_flush_after_eviction_no_duplicate_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 游标收缩回归位：淘汰 k 行已落盘旧行后，无新数据的 flush 不得重写
    # 旧行（重复行会让标定样本权重系统性偏向近期）
    monkeypatch.setattr(telemetry, "_MAX_COSINE_TRUTH", 5)
    for i in range(8):
        telemetry.record_cosine_truth("s", float(i) / 10, True)
    telemetry.flush()
    truth_file = next((tmp_path / "tel").glob("cosine_truth-*.jsonl"))
    assert len(truth_file.read_text(encoding="utf-8").strip().splitlines()) == 8
    # 缓冲淘汰后（≤5 行且全部已落盘），无新数据的 flush 不追加任何行
    telemetry._last_flush = 0.0
    telemetry.flush()
    assert len(truth_file.read_text(encoding="utf-8").strip().splitlines()) == 8
    # 新数据只追加新行
    telemetry.record_cosine_truth("s", 0.99, False)
    telemetry._last_flush = 0.0
    telemetry.flush()
    lines = truth_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 9
    last = json.loads(lines[-1])
    assert last["cosine"] == 0.99
    assert last["seq"] == 9


def test_cosine_truth_capped_after_flush(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 内存缓冲超限：已落盘旧行淘汰，未落盘尾部保留（权威存储是文件）
    monkeypatch.setattr(telemetry, "_MAX_COSINE_TRUTH", 5)
    for i in range(8):
        telemetry.record_cosine_truth("s", float(i) / 10, True)
    telemetry.flush()
    rows = telemetry.cosine_truth_rows()
    assert len(rows) <= 5


def test_record_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Boom(dict):
        def __setitem__(self, _k: str, _v: int) -> None:
            raise RuntimeError("boom")

    monkeypatch.setattr(telemetry, "_counters", _Boom())
    telemetry.record("x")  # 不应抛出
    telemetry.observe("x", 1.0)
    telemetry.record_cosine_truth("s", 0.5, True)


def test_format_summary_empty_and_nonempty(capsys: pytest.CaptureFixture[str]) -> None:
    assert telemetry.format_summary() == ""
    telemetry.record("judge.call", "weflow")
    telemetry.observe("judge.latency", 12.0, "weflow")
    summary = telemetry.format_summary()
    assert summary.startswith("遥测 ")
    assert "judge.call|weflow=1" in summary
    assert "judge.latency" in summary


def test_flush_writes_latest_and_daily(tmp_path: Path) -> None:
    telemetry.record("judge.call", "weflow")
    telemetry.flush()
    target = tmp_path / "tel"
    latest = target / "latest.json"
    assert latest.exists()
    snap = json.loads(latest.read_text(encoding="utf-8"))
    assert snap["counters"]["judge.call|weflow"] == 1
    jsonl_files = list(target.glob("telemetry-*.jsonl"))
    assert len(jsonl_files) == 1
    lines = jsonl_files[0].read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["counters"]["judge.call|weflow"] == 1


def test_flush_throttles(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    telemetry.record("a", "")
    telemetry.flush()
    telemetry.record("a", "")
    telemetry.record("a", "")
    # 节流窗口内第二次 flush 跳过：latest.json 仍是旧快照
    telemetry.flush()
    snap = json.loads((tmp_path / "tel" / "latest.json").read_text(encoding="utf-8"))
    assert snap["counters"]["a|"] == 1


def test_flush_failure_swallowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def _boom() -> Path:
        raise RuntimeError("disk full")

    monkeypatch.setattr(telemetry, "telemetry_dir", _boom)
    telemetry.record("a", "")
    with caplog.at_level(logging.DEBUG, logger="briefdesk.telemetry"):
        telemetry.flush()  # 不应抛出
    assert any("telemetry flush" in r.message for r in caplog.records)


def test_snapshot_json_safe() -> None:
    telemetry.record("k", "s")
    telemetry.observe("k.lat", 5.0, "s")
    snap = telemetry.snapshot()
    encoded = json.dumps(snap, ensure_ascii=False)
    assert "k|s" in encoded
