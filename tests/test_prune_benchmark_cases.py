"""用例裁剪脚本纯函数与写盘行为单测（只用临时目录，不碰用户数据）。

`prune_benchmark_cases.py` 是对**数据集文件**的破坏性操作（删用例不可撤销，仓库内
又不允许留 *.bak），所以它的校验强度就是安全边界本身：待删 id 未全部命中必须拒绝、
结构非法必须拒绝、写出的字节形态必须与仓库文本约定一致。这里钉的就是这几条。
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location(
    "prune_benchmark_cases", _REPO / "scripts" / "prune_benchmark_cases.py"
)
if _SPEC is None or _SPEC.loader is None:  # pragma: no cover — 文件必在
    raise RuntimeError("无法加载 prune_benchmark_cases.py")
_mod = importlib.util.module_from_spec(_SPEC)
sys.modules.setdefault("prune_benchmark_cases", _mod)
_SPEC.loader.exec_module(_mod)

DatasetError = _mod.DatasetError


def _dataset(ids: list[str], *, same: bool = True) -> dict:
    return {
        "feature": "dedup",
        "description": "夹具",
        "cases": [
            {
                "id": i,
                "items": [
                    {
                        "msg_id": f"{i}-a",
                        "content": f"内容 {i}",
                        "title": f"标题 {i}",
                        "source": "bench",
                    }
                ],
                "query": {
                    "msg_id": f"{i}-q",
                    "content": f"查询 {i}",
                    "title": f"查询 {i}",
                    "source": "bench",
                },
                "expected": {"same": same},
            }
            for i in ids
        ],
    }


class TestPruneCases:
    def test_removes_only_named_ids(self):
        raw = _dataset(["a", "b", "c"])
        new, removed = _mod.prune_cases(raw, ["b"])
        assert removed == 1
        assert [c["id"] for c in new["cases"]] == ["a", "c"]

    def test_input_not_mutated(self):
        """原对象不能被就地改：dry-run 与 --apply 共用同一次解析结果。"""
        raw = _dataset(["a", "b"])
        before = json.dumps(raw, ensure_ascii=False, sort_keys=True)
        _mod.prune_cases(raw, ["a"])
        assert json.dumps(raw, ensure_ascii=False, sort_keys=True) == before

    def test_all_ids_must_exist(self):
        """部分命中一律拒绝：静默少删会让「重复已清理」的结论不成立。"""
        raw = _dataset(["a", "b"])
        with pytest.raises(DatasetError) as ctx:
            _mod.prune_cases(raw, ["b", "ghost"])
        assert "ghost" in str(ctx.value)

    def test_no_hit_is_error_not_silent_zero(self):
        """一条都没命中必须报错而不是原样写回——那是选错文件的典型形态。"""
        raw = _dataset(["dd-0001", "dd-0002"])
        with pytest.raises(DatasetError):
            _mod.prune_cases(raw, ["dd-001"])

    def test_missing_cases_array(self):
        with pytest.raises(DatasetError):
            _mod.prune_cases({"feature": "dedup"}, ["x"])

    def test_duplicate_id_in_dataset_is_refused(self):
        """顶层结构校验不查 id 唯一性，所以「恰好命中一条」必须在这里查。

        重复 id 属数据集损坏：此时 --drop 会一次删掉多行，而报出的"删除 N 条"
        仍然自洽，没人会去数——拒绝比替用户决定删哪几条诚实。
        """
        raw = _dataset(["a", "b"])
        raw["cases"].append(json.loads(json.dumps(raw["cases"][0])))  # 复制出同 id 行
        with pytest.raises(DatasetError) as ctx:
            _mod.prune_cases(raw, ["a"])
        assert "重复出现" in str(ctx.value)

    def test_partial_miss_still_refused(self):
        raw = _dataset(["a", "b"])
        with pytest.raises(DatasetError) as ctx:
            _mod.prune_cases(raw, ["a", "ghost"])
        assert "拒绝部分删除" in str(ctx.value)


class TestStats:
    def test_dedup_balance(self):
        raw = _dataset(["a", "b", "c"], same=True)
        raw["cases"][2]["expected"] = {"same": False}
        stats = _mod._stats(raw)
        assert stats == {"cases": 3, "same_true": 2, "same_false": 1}

    def test_merge_balance(self):
        raw = {
            "feature": "merge",
            "cases": [
                {"id": "m1", "expected": {"merge": True}},
                {"id": "m2", "expected": {"merge": False}},
            ],
        }
        assert _mod._stats(raw) == {"cases": 2, "merge_true": 1}


class TestMainWrite:
    def _write_dataset(self, tmp: Path, raw: dict) -> Path:
        path = tmp / "dedup.json"
        path.write_text(
            json.dumps(raw, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        return path

    def _run(self, argv: list[str], monkeypatch) -> int:
        monkeypatch.setattr(sys, "argv", ["prune_benchmark_cases.py", *argv])
        return _mod.main()

    def test_dry_run_leaves_file_untouched(self, tmp_path: Path, monkeypatch):
        raw = _dataset(["a", "b"])
        path = self._write_dataset(tmp_path, raw)
        code = self._run(["--file", str(path), "--drop", "b"], monkeypatch)
        assert code == 0
        assert [c["id"] for c in json.loads(path.read_text(encoding="utf-8"))["cases"]] == [
            "a",
            "b",
        ]

    def test_apply_writes_lf_utf8_no_bom(self, tmp_path: Path, monkeypatch):
        path = self._write_dataset(tmp_path, _dataset(["a", "b"]))
        code = self._run(["--file", str(path), "--drop", "b", "--apply"], monkeypatch)
        assert code == 0
        blob = path.read_bytes()
        assert not blob.startswith(b"\xef\xbb\xbf"), "写出不得带 BOM"
        assert b"\r\n" not in blob, "写出必须是 LF（Windows 默认换行会写成 CRLF）"
        text = blob.decode("utf-8")
        assert "标题 a" in text, "中文不得被转成 \\u 转义（再次读取要肉眼可比）"
        assert [c["id"] for c in json.loads(text)["cases"]] == ["a"]

    def test_invalid_structure_rejected_before_write(
        self, tmp_path: Path, monkeypatch
    ):
        """顶层结构非法（feature 不认识）→ 退出码 2 且不写盘。"""
        path = tmp_path / "broken.json"
        raw = _dataset(["a"])
        raw["feature"] = "not-a-feature"
        path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        code = self._run(["--file", str(path), "--drop", "a", "--apply"], monkeypatch)
        assert code == 2
        assert json.loads(path.read_text(encoding="utf-8"))["cases"], "失败路径不得改文件"

    def test_unknown_ids_exit_two(self, tmp_path: Path, monkeypatch):
        path = self._write_dataset(tmp_path, _dataset(["a", "b"]))
        code = self._run(["--file", str(path), "--drop", "ghost", "--apply"], monkeypatch)
        assert code == 2
        assert len(json.loads(path.read_text(encoding="utf-8"))["cases"]) == 2
