"""briefdesk/telemetry.py 的单元测试（纯内存 + 临时落盘目录）。"""

from __future__ import annotations

import json
import logging
import time
from datetime import date, timedelta
from pathlib import Path

import pytest

from briefdesk import telemetry
from briefdesk.config import config


@pytest.fixture(autouse=True)
def _reset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    telemetry.reset_for_tests()
    monkeypatch.setattr(telemetry, "telemetry_dir", lambda: tmp_path / "tel")
    # 固定为开启：本文件的断言都是「记录确实发生了」。宿主 shell 若导出了
    # TELEMETRY_ENABLED=false，config 单例在 import 期就已经是关的——
    # conftest 的清环境夹具晚于 import，改不了已构造的实例，只有在此覆盖
    # 才与宿主无关；关闭语义的用例自己再改成 False。
    monkeypatch.setattr(config, "telemetry_enabled", True)
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


def test_flush_partial_write_failure_does_not_duplicate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """写盘中途失败的回归位：已 append 的行必须推进游标。

    失败模式：旧实现循环后一次性赋值 _cosine_flushed——中途抛异常时游标
    停在旧值，下轮 pending 从旧游标重算，已写入行被再写一遍，且 seq 已
    消耗使同一数据以两个不同 seq 落成重复行（「游标保证不重复」失效，
    标定样本权重被系统性偏置）。
    """
    for i in range(3):
        telemetry.record_cosine_truth("s", float(i) / 10, True)
    from pathlib import Path as _Path

    real_open = _Path.open
    state = {"truth_writes": 0, "armed": False}

    class _BoomFile:
        def __init__(self, wrapped):  # type: ignore[no-untyped-def]
            self._wrapped = wrapped

        def __enter__(self):  # type: ignore[no-untyped-def]
            self._wrapped.__enter__()
            return self

        def __exit__(self, *exc):  # type: ignore[no-untyped-def]
            return self._wrapped.__exit__(*exc)

        def write(self, data):  # type: ignore[no-untyped-def]
            # 只对 cosine_truth 文件计数并在第二条抛错
            if state["armed"]:
                state["truth_writes"] += 1
                if state["truth_writes"] == 2:
                    raise OSError("disk full mid-batch")
            return self._wrapped.write(data)

        def __getattr__(self, name):  # type: ignore[no-untyped-def]
            return getattr(self._wrapped, name)

    def _open(path_self, *args, **kwargs):  # type: ignore[no-untyped-def]
        f = real_open(path_self, *args, **kwargs)
        if "cosine_truth-" in str(path_self):
            state["armed"] = True
            return _BoomFile(f)
        return f

    monkeypatch.setattr(_Path, "open", _open)
    telemetry.flush()
    truth_file = next((tmp_path / "tel").glob("cosine_truth-*.jsonl"))
    written = truth_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(written) == 1, "第一条已 append，异常前只应有一行"
    # 恢复 open，下轮 flush 只补未写行（不重写第一条）
    monkeypatch.setattr(_Path, "open", real_open)
    telemetry._last_flush = 0.0
    telemetry.flush()
    rows = [
        json.loads(line)
        for line in truth_file.read_text(encoding="utf-8").strip().splitlines()
    ]
    assert len(rows) == 3, f"不得重复落盘，实际 {len(rows)} 行"
    assert len({(r["boot"], r["seq"]) for r in rows}) == 3
    assert [r["cosine"] for r in rows] == [0.0, 0.1, 0.2]


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


def test_disabled_switch_skips_recording_and_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """关闭后四个入口全部早退：不计数、不落盘，连目录都不建。

    失败模式：门只加在 flush 上时，进程照样在热路径累计计数，用户关掉开关
    只换来「不写盘」——内存里的量仍在涨，任何一次 snapshot 都能看到被记录
    的内容。
    """
    monkeypatch.setattr(config, "telemetry_enabled", False)
    telemetry.record("judge.call", "weflow")
    telemetry.observe("judge.latency", 12.0, "weflow")
    telemetry.record_cosine_truth("weflow", 0.9, True)
    snap = telemetry.snapshot()
    assert snap["counters"] == {}
    assert snap["latencies"] == {}
    assert telemetry.cosine_truth_rows() == []
    telemetry.flush()
    assert not (tmp_path / "tel").exists(), "关闭时不得创建落盘目录"


def test_switch_is_read_per_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """开关每次调用现读：先关后开能立刻恢复记录。

    失败模式：把开关缓存成模块级常量后，「重启后生效」悄悄变成「导入时
    生效」——本进程内无论怎么改配置都停在导入时的状态。
    """
    monkeypatch.setattr(config, "telemetry_enabled", False)
    telemetry.record("a")
    assert telemetry.snapshot()["counters"] == {}
    monkeypatch.setattr(config, "telemetry_enabled", True)
    telemetry.record("a")
    assert telemetry.snapshot()["counters"] == {"a|": 1}


def _dated_name(prefix: str, days_ago: int) -> str:
    # 与写入侧同口径的本地日历日（见 telemetry._prune_expired）
    today = date(*time.localtime()[:3])
    return f"{prefix}-{(today - timedelta(days=days_ago)):%Y%m%d}.jsonl"


def test_flush_prunes_only_expired_dated_files(tmp_path: Path) -> None:
    """保留期只删「过期 + 严格命名 + 直属」的文件，其余一律不碰。

    失败模式：宽 glob 或递归删除会连中间文件（`latest.json.tmp`）与用户自行
    放进目录的东西一起删；只按前缀匹配则任何自定义名字都会被误判成年纪。
    本用例故意不产生任何计数——空快照轮次同样要清理，否则「永远没有 AI
    流量」的部署永远等不到清理。
    """
    target = tmp_path / "tel"
    target.mkdir()
    expired_snapshot = _dated_name("telemetry", 15)
    keep_snapshot = _dated_name("telemetry", 14)
    expired_truth = _dated_name("cosine_truth", 181)
    keep_truth = _dated_name("cosine_truth", 180)
    unrelated = ["latest.json", "latest.json.tmp", "notes.jsonl", "telemetry-weekly.jsonl"]
    for name in [expired_snapshot, keep_snapshot, expired_truth, keep_truth, *unrelated]:
        (target / name).write_text("x\n", encoding="utf-8")
    telemetry.flush()
    remaining = {p.name for p in target.iterdir()}
    assert expired_snapshot not in remaining, "15 天前的快照应被删除"
    assert expired_truth not in remaining, "181 天前的余弦真值应被删除"
    assert {keep_snapshot, keep_truth, *unrelated} <= remaining, "未过期与不匹配的文件必须保留"


def test_empty_snapshot_keeps_previous_latest(tmp_path: Path) -> None:
    """空快照不覆盖 latest.json，也不追加当日快照行。

    失败模式：重启后第一个轮询周期通常没有 AI 流量，无条件覆盖会把上一会话
    的验收读数清成空——读数窗口从「上次有流量」塌缩成「本次启动」。
    """
    telemetry.record("judge.call", "weflow")
    telemetry.flush()
    target = tmp_path / "tel"
    latest = target / "latest.json"
    before = latest.read_text(encoding="utf-8")
    daily = next(target.glob("telemetry-*.jsonl"))
    lines_before = len(daily.read_text(encoding="utf-8").strip().splitlines())
    # 模拟重启：内存清空、节流窗口归零，落盘文件保留
    telemetry.reset_for_tests()
    telemetry.flush()
    assert latest.read_text(encoding="utf-8") == before
    assert len(daily.read_text(encoding="utf-8").strip().splitlines()) == lines_before


def test_empty_snapshot_still_flushes_cosine_truth(tmp_path: Path) -> None:
    """空快照守卫只拦快照：待落盘的余弦真值照常增量写入。

    否则只有余弦真值、没有 AI 计数的轮次会把标定样本一直留在内存里，进程
    退出即丢——而这正是「合并预筛跑过一轮但没走判官」的典型形态。
    """
    telemetry.record_cosine_truth("weflow", 0.42, False)
    telemetry.flush()
    target = tmp_path / "tel"
    assert not (target / "latest.json").exists()
    truth = next(target.glob("cosine_truth-*.jsonl"))
    assert len(truth.read_text(encoding="utf-8").strip().splitlines()) == 1
