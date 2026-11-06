"""benchmark 用例数据集裁剪工具 — 按 id 删除用例（默认只预演，不写盘）。

用途：数据集里存在逐字重复的用例（同一侧消息一字不差地出现在两条用例里）时，
同一份证据会被计入分母两次；更糟的是同一张历史卡片可能同时支持 `same=true` 与
`same=false` 两个相反标签，让判重指标失去解释力。这类删除是**数据集变更**，不
是代码变更，故不自动执行：默认 dry-run，要写盘必须显式 `--apply`。

为什么单独一个脚本而不是手改 JSON：数据集动辄几百条、每条十几个字段，手删容易
连带破坏相邻用例的括号结构；而且删除后必须重新核对「待删 id 全部命中、总数对得上、
正负平衡变成多少」——这些断言只有脚本能给。

安全边界：
- 只碰 `--file` 指定的那一个文件，且必须通过 `schema.load_dataset_file` 的结构校验
  （校验不过直接拒绝，绝不"顺手修好再写"）；
- 不写仓库内备份（AGENTS 禁止 `*.bak` 入库）：要留档请在仓库外自行复制；
- 写出为 UTF-8 无 BOM、LF、2 空格缩进、`ensure_ascii=False`——与仓库文本约定一致，
  也保证再次读取时字节可比。

用法：
    python scripts/prune_benchmark_cases.py --file <cases.json> --drop id1 id2
    python scripts/prune_benchmark_cases.py --file <cases.json> --drop id1 --apply
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 使脚本可直接运行（python scripts/prune_benchmark_cases.py）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from briefdesk.plugins.benchmark.schema import DatasetError, load_dataset_file


def prune_cases(raw: dict[str, object], drop: list[str]) -> tuple[dict[str, object], int]:
    """按 id 删除用例，返回 (新数据, 实际删除数)。

    校验放在写入之前：id 一个都没命中就报 2——那通常意味着文件选错了（数据集同名
    id 前缀不同，如 `dd-001` 与 `dd-0001`），静默"删除 0 条、原样写回"会让人以为
    清理已完成。

    顶层结构校验（load_dataset_file）不查逐条用例、也不查 id 唯一性，所以这里补
    「每个待删 id 必须恰好命中一条」：文件里存在重复 id 时 `--drop` 会一次删掉多行
    而报出的"删除 N 条"仍然自洽，没人会去数——重复 id 本就是数据集损坏，拒绝比
    替用户决定删哪几条诚实。
    """
    cases = raw.get("cases")
    if not isinstance(cases, list):
        raise DatasetError("数据集缺少 cases 数组")
    drop_set = set(drop)
    hits = {
        i: sum(1 for c in cases if isinstance(c, dict) and c.get("id") == i)
        for i in sorted(drop_set)
    }
    if not any(hits.values()):
        raise DatasetError(
            f"待删 id 一条都没命中（{len(drop_set)} 个）：{sorted(drop_set)[:5]}…；"
            "请确认选对了文件与 id 写法"
        )
    missing = sorted(i for i, n in hits.items() if n == 0)
    if missing:
        raise DatasetError(f"以下待删 id 不在数据集里，拒绝部分删除: {missing}")
    duplicated = {i: n for i, n in hits.items() if n > 1}
    if duplicated:
        raise DatasetError(
            f"待删 id 在数据集里重复出现，拒绝猜测该删哪几条: {duplicated}；"
            "请先修复用例 id 重复（同一数据集内 id 必须唯一）"
        )
    kept = [c for c in cases if not (isinstance(c, dict) and c.get("id") in drop_set)]
    removed = len(cases) - len(kept)
    new = dict(raw)
    new["cases"] = kept
    return new, removed


def _stats(raw: dict[str, object]) -> dict[str, object]:
    """删除前后的对照读数（dedup 关心正负平衡，其它 feature 只给总数）。"""
    value = raw.get("cases")
    cases = [c for c in (value if isinstance(value, list) else []) if isinstance(c, dict)]
    out: dict[str, object] = {"cases": len(cases)}
    if raw.get("feature") == "dedup":
        out["same_true"] = sum(
            1
            for c in cases
            if isinstance(c.get("expected"), dict) and c["expected"].get("same") is True
        )
        out["same_false"] = sum(
            1
            for c in cases
            if isinstance(c.get("expected"), dict) and c["expected"].get("same") is False
        )
    if raw.get("feature") == "merge":
        out["merge_true"] = sum(
            1
            for c in cases
            if isinstance(c.get("expected"), dict) and c["expected"].get("merge") is True
        )
    return out


def main() -> int:
    parser = argparse.ArgumentParser(
        description="按 id 裁剪 benchmark 用例数据集（默认 dry-run）"
    )
    parser.add_argument("--file", type=Path, required=True, help="数据集 JSON 文件路径")
    parser.add_argument(
        "--drop", nargs="+", required=True, metavar="ID", help="要删除的用例 id"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="真正写回文件（缺省只预演；写回不可撤销，仓库外请自行留档）",
    )
    args = parser.parse_args()

    try:
        dataset = load_dataset_file(args.file)
    except DatasetError as e:
        print(f"[prune] 结构校验失败：{e}", file=sys.stderr)
        return 2
    raw = json.loads(args.file.read_text(encoding="utf-8-sig"))
    before = _stats(raw)
    try:
        new, removed = prune_cases(raw, list(args.drop))
    except DatasetError as e:
        print(f"[prune] {e}", file=sys.stderr)
        return 2
    after = _stats(new)
    print(f"文件: {args.file}")
    print(f"feature: {dataset.feature}  顶层校验: 通过")
    print(f"删除 {removed} 条 → 剩余 {after['cases']} 条")
    print(f"  删除前: {before}")
    print(f"  删除后: {after}")
    if not args.apply:
        print("（dry-run，未写盘；确认无误后加 --apply）")
        return 0
    text = json.dumps(new, ensure_ascii=False, indent=2) + "\n"
    # newline="\n" 强制 LF：Windows 上默认换行会写成 CRLF，与仓库文本约定不一致，
    # 也会让同一份数据两次导出的字节不可比
    args.file.write_text(text, encoding="utf-8", newline="\n")
    print(f"已写回: {args.file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
