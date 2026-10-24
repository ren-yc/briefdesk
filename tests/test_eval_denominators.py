"""scripts/eval_denominators.py 的单元测试（临时目录，不触真实用例）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.eval_denominators import (
    acceptance_denominator,
    check_disjoint,
    collect_cases,
    main,
    summarize,
)


def _write_dataset(
    path: Path, feature: str, cases: list[dict[str, object]]
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"feature": feature, "cases": cases}, ensure_ascii=False),
        encoding="utf-8",
    )


def test_collect_cases_flattens_and_skips_bad(tmp_path: Path) -> None:
    _write_dataset(
        tmp_path / "a" / "classify.fromweb.json",
        "classify",
        [
            {"id": "c1", "origin": "cards", "batch_fingerprint": "fp1"},
            {"id": "c2", "origin": "manual"},
        ],
    )
    _write_dataset(
        tmp_path / "a" / "dedup.json",
        "dedup",
        [{"id": "d1", "origin": "recorder", "batch_fingerprint": "fp2"}],
    )
    (tmp_path / "a" / "broken.json").write_text("not json", encoding="utf-8")
    (tmp_path / "a" / "x.tmp").write_text("{}", encoding="utf-8")
    cases, seen_files, bad_files = collect_cases([tmp_path / "a", tmp_path / "missing"])
    assert {c["id"] for c in cases} == {"c1", "c2", "d1"}
    assert all(c.get("_file") for c in cases)
    assert seen_files == 3  # broken.json 计入扫描数（x.tmp 不匹配 glob）
    assert bad_files == 1


def test_collect_cases_legacy_fromweb_defaults_to_cards(
    tmp_path: Path,
) -> None:
    # 回归位：字段出现前的存量 fromweb 文件无 origin 键——这些用例的期望
    # 来自管道自身判定，按来源文件名缺省为 cards，绝不冒充人工核对混进
    # 验收分母（曾因缺省 manual 整批误入验收分母）
    _write_dataset(
        tmp_path / "cases" / "classify.fromweb.json",
        "classify",
        [{"id": "old-1"}, {"id": "old-2", "origin": "manual"}],
    )
    cases, _seen, _bad = collect_cases([tmp_path / "cases"])
    by_id = {c["id"]: c for c in cases}
    assert by_id["old-1"]["origin"] == "cards"
    assert by_id["old-2"]["origin"] == "manual"  # 显式 origin 永不被缺省覆盖


def test_unmarked_non_fromweb_files_excluded_from_denominator(
    tmp_path: Path,
) -> None:
    # 回归位：无 origin 键的非 fromweb 文件（手写/示例模板）归 unmarked、
    # 不入验收分母——无法证明经过人工核对的数据 fail-open 进分母会让
    # 指标虚高（宁可分母偏小也不掺假）
    _write_dataset(
        tmp_path / "cases" / "dedup.json",
        "dedup",
        [{"id": "e1"}, {"id": "m1", "origin": "manual"}],
    )
    cases, _seen, _bad = collect_cases([tmp_path / "cases"])
    by_id = {c["id"]: c for c in cases}
    assert by_id["e1"]["origin"] == "unmarked"
    assert {c["id"] for c in acceptance_denominator(cases)} == {"m1"}


def test_load_dataset_file_bad_encoding_and_json(tmp_path: Path) -> None:
    # 读取侧兜底回归位：坏编码/坏 JSON → 含路径与原因的 DatasetError
    #（CLI 面对坏文件应收到可操作的错误信息，而非裸 UnicodeDecodeError）
    from briefdesk.plugins.benchmark.schema import DatasetError, load_dataset_file

    gbk_file = tmp_path / "dedup.json"
    gbk_file.write_bytes('{"note": "中文"}'.encode("gbk"))
    with pytest.raises(DatasetError, match="UTF-8"):
        load_dataset_file(gbk_file)
    bad_json = tmp_path / "dedup2.json"
    bad_json.write_text("{not json", encoding="utf-8")
    with pytest.raises(DatasetError, match="JSON"):
        load_dataset_file(bad_json)


def test_summarize_groups_by_feature_origin_fingerprint() -> None:
    cases: list[dict[str, object]] = [
        {"feature": "classify", "origin": "cards", "batch_fingerprint": "fp1"},
        {"feature": "classify", "origin": "cards", "batch_fingerprint": "fp1"},
        {"feature": "dedup", "origin": "manual", "batch_fingerprint": ""},
    ]
    groups = summarize(cases)
    assert ("classify", "cards", "fp1", 2) in groups
    assert ("dedup", "manual", "", 1) in groups


def test_acceptance_denominator_only_manual() -> None:
    # collect_cases 已按来源文件名补过缺省：这里只测筛选取值本身
    # （无 origin 键的用例不该出现——fromweb 补 cards、其余补 manual）
    cases: list[dict[str, object]] = [
        {"id": "m", "origin": "manual"},
        {"id": "c", "origin": "cards"},
        {"id": "s", "origin": "synthetic"},
    ]
    kept = acceptance_denominator(cases)
    assert {c["id"] for c in kept} == {"m"}


def test_check_disjoint_flags_any_origin() -> None:
    cases: list[dict[str, object]] = [
        {"id": "a", "origin": "cards", "batch_fingerprint": "fp-shared"},
        {"id": "b", "origin": "manual", "batch_fingerprint": "fp-ok"},
        {"id": "c", "batch_fingerprint": ""},  # 无指纹不参与校验
    ]
    violations = check_disjoint(cases, {"fp-shared"})
    assert len(violations) == 1
    assert "a" in violations[0]
    assert check_disjoint(cases, {"fp-other"}) == []


def test_main_mutual_exclusion_exit_codes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_dataset(
        tmp_path / "cases" / "classify.fromweb.json",
        "classify",
        [{"id": "c1", "origin": "cards", "batch_fingerprint": "fp-leak"}],
    )
    cases_dir = str(tmp_path / "cases")

    # 泄漏 → 1
    code = main(
        ["--cases-dir", cases_dir, "--training-fingerprints", "fp-leak,fp-x"]
    )
    assert code == 1
    # 干净 → 0
    code = main(
        ["--cases-dir", cases_dir, "--training-fingerprints", "fp-x"]
    )
    assert code == 0

    # @文件清单
    fp_file = tmp_path / "fps.txt"
    fp_file.write_text("fp-leak\n", encoding="utf-8")
    code = main(["--cases-dir", cases_dir, "--training-fingerprints", f"@{fp_file}"])
    assert code == 1

    # 清单不可读 → 2
    code = main(
        ["--cases-dir", cases_dir, "--training-fingerprints", f"@{tmp_path / 'nope.txt'}"]
    )
    assert code == 2


def test_main_empty_result_exit_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # 防假绿：扫不到用例 / 验收分母为空都按错误退出（exit 0 + 「通过」
    # 会把「扫不到」伪装成「校验通过」）
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    code = main(["--cases-dir", str(empty_dir), "--training-fingerprints", "fp-x"])
    out = capsys.readouterr().out
    assert code == 2
    assert "未扫描到任何用例" in out

    _write_dataset(
        tmp_path / "cases" / "classify.fromweb.json",
        "classify",
        [{"id": "c1", "origin": "cards", "batch_fingerprint": "fp-leak"}],
    )
    code = main(
        [
            "--cases-dir", str(tmp_path / "cases"),
            "--training-fingerprints", "fp-x",
            "--acceptance-only",
        ]
    )
    out = capsys.readouterr().out
    assert code == 2
    assert "验收分母为空" in out


def test_main_acceptance_only_filters(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_dataset(
        tmp_path / "cases" / "dedup.json",
        "dedup",
        [
            {"id": "m1", "origin": "manual", "batch_fingerprint": "fp-clean"},
            {"id": "c1", "origin": "cards", "batch_fingerprint": "fp-leak"},
        ],
    )
    # 验收分母只含 manual：cards 的泄漏用例被滤掉后校验通过（manual 用例
    # 指纹干净）；不加 --acceptance-only 时同样输入必须失败（对照断言在上一用例）
    code = main(
        [
            "--cases-dir", str(tmp_path / "cases"),
            "--training-fingerprints", "fp-leak",
            "--acceptance-only",
        ]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "指纹互斥校验通过" in out
