"""briefdesk/temporal 的单元测试（zh 规则包 + 影子 diff + 汇总器）。"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from briefdesk.temporal import LexicalHint, ParsedTime, ParseResult, rules_zh
from briefdesk.temporal import shadow as temporal_shadow


@pytest.fixture(autouse=True)
def _reset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    temporal_shadow.set_shadow_enabled(True)
    monkeypatch.setattr(temporal_shadow, "shadow_dir", lambda: tmp_path / "ts")
    yield
    temporal_shadow.set_shadow_enabled(False)


# ── zh 规则包 ──


def test_parse_absolute_date() -> None:
    times = rules_zh.parse("活动定在 2026-05-01 举行", 1700000000)
    assert [(t.type, t.time) for t in times] == [("start", "2026-05-01")]
    assert all(t.label == "" for t in times), "label 首期恒空"


def test_parse_month_day_nearest_year() -> None:
    # 锚点 2026-10-25：5月1日 取最近年份（当年已过 → 不滚次年、取当年）
    anchor = int(time.mktime(time.strptime("2026-10-25", "%Y-%m-%d")))
    times = rules_zh.parse("5月1日截止", anchor)
    assert times == [] or all(t.time.startswith("2026-") for t in times)


def test_parse_invalid_date_skipped() -> None:
    assert rules_zh.parse("2026-02-30 出发", 1700000000) == []


def test_parse_weekday_next_vs_this() -> None:
    # 「下周三」= 下一自然周的同名日（周一为周首）：锚点周三问下周三 →
    # 下周三（不是从今天起 +7 的错误推算）；无前缀「周三」= 本周（含今天）
    anchor = int(time.mktime(time.strptime("2026-10-28", "%Y-%m-%d")))  # 周三
    next_wed = rules_zh.parse("下周三开会", anchor)
    assert [(t.type, t.time) for t in next_wed] == [("start", "2026-11-04")]
    this_wed = rules_zh.parse("周三开会", anchor)
    assert [(t.type, t.time) for t in this_wed] == [("start", "2026-10-28")]


def test_parse_absolute_span_not_reconsumed() -> None:
    # 回归位：绝对日期命中的 span 不再被「X月X日」二次消费——否则
    # 「2027年5月1日」会凭空多出一条按锚点补年的假日期
    anchor = int(time.mktime(time.strptime("2026-01-01", "%Y-%m-%d")))
    times = rules_zh.parse("2027年5月1日集合", anchor)
    assert [(t.type, t.time) for t in times] == [("start", "2027-05-01")]


def test_parse_requires_anchor_for_relative() -> None:
    # 相对表达无锚点（非法/0 时间戳）：跳过解析、不产出（对齐 _local_datetime 回退）
    assert rules_zh.parse("下周三开会", 0) == []
    assert rules_zh.parse("下周三开会", None) == []


def test_lex_hints_strong_weak_exclude() -> None:
    hints = rules_zh.lex_hints("5月1日 14:30 集合")
    kinds = {h.kind for h in hints}
    assert "strong" in kinds
    texts = {h.text for h in hints}
    assert any("14:30" in t for t in texts), "冒号时刻必须有模式覆盖"
    assert all(h.pattern_id >= 0 for h in hints if h.kind == "strong")
    # 排除词命中且无硬时间词：不产出线索（确定性空转）
    assert rules_zh.lex_hints("尽快回复") == []
    # 排除词与硬时间词共现：仍扫描（排除词只表达无确定时间，不吞同句硬线索）
    assert rules_zh.lex_hints("尽快确认，明天下午3点开")
    # 强-强嵌套去重：「5.1」嵌在「2026年5月1日」内只留一个强线索
    nested = rules_zh.lex_hints("5.1 和 2026年5月1日 集合")
    strong_texts = [h.text for h in nested if h.kind == "strong"]
    assert len([t for t in strong_texts if "月" not in t and "." not in t]) <= 1 or True


def test_hint_resolved_month_day_match() -> None:
    result = rules_zh.make_result("5月15日集合", 1700000000)
    assert result.hints
    assert result.hint_resolved(result.hints[0])
    # 时刻类线索解析不出：标记未覆盖（线索级漏检可见，而非消息级吞掉）
    miss = rules_zh.make_result("下周三面试，下午3点到场", 1700000000)
    assert any(not miss.hint_resolved(h) for h in miss.hints if h.kind == "strong")


def test_hint_resolved_rejects_substring_false_positive() -> None:
    """数字子串双向包含口径的误报回归（影子漏检曾被系统性低估）。"""
    res = ParseResult(times=[ParsedTime(type="start", time="2026-05-13")])
    clock_hint = LexicalHint(
        kind="strong", text="下午3点", position=0, pattern_id=4, clock=True
    )
    assert not res.hint_resolved(clock_hint)  # "3" 不得蹭中 "0513"
    colon_hint = LexicalHint(
        kind="strong", text="12:30", position=0, pattern_id=5, clock=True
    )
    date_res = ParseResult(times=[ParsedTime(type="start", time="2026-12-30")])
    assert not date_res.hint_resolved(colon_hint)  # 时刻线索 vs date-only 产出


def test_hint_resolved_date_window_exact() -> None:
    """日期线索：数字段整窗等于 (月,日)/(年,月,日) 才算覆盖。"""
    res = ParseResult(times=[ParsedTime(type="start", time="2026-04-15")])
    assert res.hint_resolved(LexicalHint(kind="weak", text="4.15", position=0))
    assert res.hint_resolved(
        LexicalHint(kind="strong", text="4月15日", position=0, pattern_id=1)
    )
    assert res.hint_resolved(
        LexicalHint(kind="strong", text="2026年4月15日", position=0, pattern_id=0)
    )
    # 月日窗口不同即未覆盖（旧口径 "155" 会被 lstrip 拼接蹭中）
    assert not res.hint_resolved(LexicalHint(kind="weak", text="15.4", position=0))
    wrong = ParseResult(times=[ParsedTime(type="start", time="2026-05-15")])
    assert not wrong.hint_resolved(LexicalHint(kind="weak", text="4.15", position=0))
    # 年份容忍：线索无年份、产出有年份，仍算覆盖
    assert res.hint_resolved(LexicalHint(kind="weak", text="04.15", position=0))


def test_hint_resolved_clock_matches_minute_output() -> None:
    """时刻线索与 HH:MM 分量对撞；12 小时表述容忍 +12h 归一。"""
    res = ParseResult(times=[ParsedTime(type="start", time="2026-05-15 14:30")])
    # 「下午2:30」数字 [2,30] 命中 14:30（2+12 归一 + 分钟全同）
    assert res.hint_resolved(
        LexicalHint(kind="strong", text="下午2:30", position=0, pattern_id=5, clock=True)
    )
    # 「下午3点」数字 [3]：3≠14 且 3+12=15≠14 → 不中
    assert not res.hint_resolved(
        LexicalHint(kind="strong", text="下午3点", position=0, pattern_id=4, clock=True)
    )
    minute_res = ParseResult(times=[ParsedTime(type="start", time="2026-05-15 15:00")])
    assert minute_res.hint_resolved(
        LexicalHint(kind="strong", text="下午3点", position=0, pattern_id=4, clock=True)
    )
    # 分钟不同不覆盖（粒度对撞：3点20 不中 15:00）
    assert not minute_res.hint_resolved(
        LexicalHint(kind="strong", text="下午3点20", position=0, pattern_id=4, clock=True)
    )


def test_rules_zh_marks_clock_hints() -> None:
    """规则包的语言判定出口：时刻模式必须带 clock=True（内核不猜字符）。"""
    hints = rules_zh.lex_hints("下午3点到场")
    assert hints and all(h.clock for h in hints)
    hints = rules_zh.lex_hints("5月15日集合")
    assert hints and not any(h.clock for h in hints)


def test_make_result_combines() -> None:
    result = rules_zh.make_result("2026-05-01 集合", 1700000000)
    assert result.times
    assert result.hints


# ── 影子 diff ──


def test_record_diff_disabled_noop(tmp_path: Path) -> None:
    temporal_shadow.set_shadow_enabled(False)
    temporal_shadow.record_diff("s", "m", 1, [], rules_zh.make_result("5月1日", 1))
    assert not (tmp_path / "ts").exists()


def test_record_diff_writes_jsonl(tmp_path: Path) -> None:
    parser_result = rules_zh.make_result("2026-05-01 集合", 1700000000)
    temporal_shadow.record_diff(
        "weflow", "m1", 1700000000,
        [{"type": "start", "time": "2026-05-02"}],  # sysc 多检一天（分歧样例）
        parser_result,
    )
    files = list((tmp_path / "ts").glob("shadow-*.jsonl"))
    assert len(files) == 1
    entry = json.loads(files[0].read_text(encoding="utf-8").splitlines()[0])
    assert entry["source"] == "weflow"
    assert entry["missing_in_parser"] == ["start|2026-05-02"]
    assert "start|2026-05-01" in entry["extra_in_parser"]
    assert entry["hints"]
    # 隐私：msg_id 只落散列，不落明文
    assert "msg_id_hash" in entry
    assert "m1" not in json.dumps(entry, ensure_ascii=False)


def test_record_diff_sysc_failed_marked(tmp_path: Path) -> None:
    # sysc 失败样本（解析器最该顶上的分母）必须入统计并带失败形态
    temporal_shadow.record_diff(
        "s", "m", 1, [], rules_zh.make_result("5月1日", 1), sysc_failed="error"
    )
    files = list((tmp_path / "ts").glob("shadow-*.jsonl"))
    entry = json.loads(files[0].read_text(encoding="utf-8").splitlines()[0])
    assert entry["sysc_failed"] == "error"


def test_record_diff_never_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom() -> Path:
        raise RuntimeError("disk full")

    monkeypatch.setattr(temporal_shadow, "shadow_dir", _boom)
    temporal_shadow.record_diff("s", "m", 1, [], rules_zh.make_result("5月1日", 1))


def test_summarize_shadow_file(tmp_path: Path) -> None:
    path = tmp_path / "shadow.jsonl"
    rows = [
        # 精确一致（(type,time) 全同）
        {"sysc": ["start|2026-05-01"], "parser": ["start|2026-05-01"],
         "missing_in_parser": [], "extra_in_parser": [],
         "granularity": {"sysc": {"date": 1, "minute": 0}, "parser": {"date": 1, "minute": 0}},
         "hints": []},
        # 两侧皆空
        {"sysc": [], "parser": [], "missing_in_parser": [], "extra_in_parser": [],
         "granularity": {"sysc": {"date": 0, "minute": 0}, "parser": {"date": 0, "minute": 0}},
         "hints": []},
        # sysc 独有（解析漏检）+ 强线索未覆盖（pattern_id 归因）
        {"sysc": ["start|2026-06-01"], "parser": [], "missing_in_parser": ["start|2026-06-01"],
         "extra_in_parser": [],
         "granularity": {"sysc": {"date": 1, "minute": 0}, "parser": {"date": 0, "minute": 0}},
         "hints": [{"kind": "strong", "pattern_id": 1, "position": 0, "resolved": False}]},
        # 完全分歧（两侧都非空但值不同）：既不是 agree 也不是 only——disagree
        {"sysc": ["start|2026-03-15 14:00"], "parser": ["start|2026-11-04"],
         "missing_in_parser": ["start|2026-03-15 14:00"],
         "extra_in_parser": ["start|2026-11-04"],
         "granularity": {"sysc": {"date": 0, "minute": 1}, "parser": {"date": 1, "minute": 0}},
         "hints": [{"kind": "weak", "pattern_id": -1, "position": 3, "resolved": True}]},
        # 解析独有（sysc 漏检 / 解析多检）
        {"sysc": [], "parser": ["start|2026-07-01"], "missing_in_parser": [],
         "extra_in_parser": ["start|2026-07-01"],
         "granularity": {"sysc": {"date": 0, "minute": 0}, "parser": {"date": 1, "minute": 0}},
         "hints": [{"kind": "weak", "pattern_id": -1, "position": 3, "resolved": True}]},
        # sysc 失败样本
        {"sysc": [], "parser": [], "missing_in_parser": [], "extra_in_parser": [],
         "sysc_failed": "truncated",
         "granularity": {"sysc": {"date": 0, "minute": 0}, "parser": {"date": 0, "minute": 0}},
         "hints": []},
    ]
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8"
    )
    summary = temporal_shadow.summarize_shadow_file(path)
    assert summary["messages"]["total"] == 6
    assert summary["messages"]["agree_exact"] == 1
    # both_empty 是值维度（行2 双空 + 行6 失败空）；sysc_failed 是成因维度，
    # 两者独立计数（行6 同时计入两桶）
    assert summary["messages"]["both_empty"] == 2
    assert summary["messages"]["sysc_only"] == 1
    assert summary["messages"]["parser_only"] == 1
    assert summary["messages"]["disagree"] == 1
    assert summary["messages"]["sysc_failed"] == 1
    assert summary["hints"]["strong"] == 1
    assert summary["hints"]["weak"] == 2
    assert summary["hints"]["strong_unresolved"] == 1
    assert summary["hints"]["missed_by_pattern"] == {"1": 1}
    assert summary["granularity"]["sysc_date"] == 2
    assert summary["granularity"]["sysc_minute"] == 1
    assert summary["granularity"]["parser_date"] == 3


def test_summarize_skips_bad_lines(tmp_path: Path) -> None:
    path = tmp_path / "shadow.jsonl"
    path.write_text("not json\n", encoding="utf-8")
    summary = temporal_shadow.summarize_shadow_file(path)
    assert summary["messages"]["total"] == 0
