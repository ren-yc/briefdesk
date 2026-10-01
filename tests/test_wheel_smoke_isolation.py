"""wheel 冒烟的真实目录泄漏断言：前后快照对比（不跑完整冒烟即可验证）。

为什么单独测这两个函数：原实现断言「真实用户缓存目录不存在」，在任何用过应用的机器上
必然失败——只有干净 runner 才碰巧成立。改成快照对比后，「本次有没有写」与「机器上是否
早有历史记录」解耦，这组用例守住三个方向：新增、改动、删除都要能判出来。
"""

from pathlib import Path

import pytest

from scripts.wheel_smoke import SmokeFailure, assert_tree_unchanged, snapshot_tree


def test_missing_directory_snapshots_empty(tmp_path: Path) -> None:
    assert snapshot_tree(tmp_path / "nope") == {}


def test_unchanged_tree_passes(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    (root / "run-1").mkdir(parents=True)
    (root / "run-1" / "report.json").write_text("{}", encoding="utf-8")

    before = snapshot_tree(root)
    assert before  # 非空快照（含目录与文件）
    assert_tree_unchanged(root, before, why="测试")  # 不抛即通过


def test_new_entry_is_a_leak(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    root.mkdir()
    before = snapshot_tree(root)

    (root / "run-2").mkdir()
    (root / "run-2" / "report.json").write_text("{}", encoding="utf-8")

    with pytest.raises(SmokeFailure, match="run-2"):
        assert_tree_unchanged(root, before, why="真实目录被写入")


def test_in_place_modification_is_a_leak(tmp_path: Path) -> None:
    """已存在的运行目录里被追加写：只看顶层条目集合会漏判，指纹必须抓住。"""
    root = tmp_path / "runs"
    (root / "run-1").mkdir(parents=True)
    target = root / "run-1" / "progress.jsonl"
    target.write_text("a\n", encoding="utf-8")
    before = snapshot_tree(root)

    target.write_text("a\nb\n", encoding="utf-8")

    with pytest.raises(SmokeFailure, match="progress.jsonl"):
        assert_tree_unchanged(root, before, why="真实目录被改写")


def test_removed_entry_is_a_leak(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    (root / "run-1").mkdir(parents=True)
    before = snapshot_tree(root)

    (root / "run-1").rmdir()

    with pytest.raises(SmokeFailure, match="run-1"):
        assert_tree_unchanged(root, before, why="真实目录被删除")
