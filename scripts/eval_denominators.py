"""评测分母双口径工具 — 验收分母与对照实验分母的分开导出与指纹互斥校验。

双口径约定：
- **验收分母**：只认人工核对用例（origin=manual）——benchmark 验收门的
  唯一分母；fromweb 自标用例（期望=管道自身判定）与合成用例永不入内。
- **对照实验分母**：recorder 观察导出的 held-out 子集（批次指纹与入训
  记录互斥）——联合 vs 独立 adapter 对照实验专用，不与验收分母混用。

本脚本提供两个只读能力（不写任何文件）：
  1. 汇总：扫描用例目录，按 (feature, origin, batch_fingerprint) 分组计数；
  2. 互斥校验：给定训练记录的指纹清单（文件或逗号分隔），验证评测集里
     没有任何用例与入训记录共享指纹（泄漏 = 评测分母被同源数据污染，
     指标会虚高，属硬性失败）。

用例来源字段（origin/batch_fingerprint）由 export_fromweb 单点写入
（origin=cards）；人工核对数据集手工维护 origin=manual；recorder 导出
脚本（训练数据侧）负责打自己的指纹。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def collect_cases(
    cases_dirs: list[Path],
) -> tuple[list[dict[str, object]], int, int]:
    """扫描目录下全部 *.json 数据集，展平为用例列表。

    返回 (用例列表, 扫描文件数, 坏文件数)。非法/损坏文件跳过（计数并
    继续）——汇总工具不应因一个坏文件失明全部；但坏文件数会在 main 输出，
    全部目录为空/缺失时 main 按错误退出（防「扫不到=通过」的假绿）。
    用例按 (feature, id) 去重（首个文件胜出，命名序）——同一用例的多份
    副本（如历史转换脚本产物）只计一次，汇总数与真实评测集对齐。
    """
    cases: list[dict[str, object]] = []
    seen_files = 0
    bad_files = 0
    seen: set[Path] = set()
    for base in cases_dirs:
        if not base.is_dir() or base in seen:
            continue
        seen.add(base)
        for path in sorted(base.glob("*.json")):
            seen_files += 1
            try:
                raw = json.loads(path.read_text(encoding="utf-8-sig"))
            except (json.JSONDecodeError, UnicodeDecodeError, OSError):
                bad_files += 1
                continue
            if not isinstance(raw, dict) or not isinstance(raw.get("cases"), list):
                bad_files += 1
                continue
            # 文件名形态：<feature>.json（手写数据集）或 <feature>.fromweb.json
            # （导出文件）；stem 剥掉 .json 保留 fromweb 标记供缺省归类
            stem = path.name[: -len(".json")]
            feature = str(raw.get("feature") or stem.removesuffix(".fromweb"))
            # 存量缺省：origin 字段出现之前的文件没有该键。fromweb 文件是
            # export_fromweb 产物（来源 cards）——期望=管道自身判定，方向
            # 明确可缺省；**非 fromweb 文件一律归 unmarked（不入验收分母）**：
            # 无标注的手写/示例文件无法证明经过人工核对，fail-open 进分母
            # 会让指标虚高（宁可分母偏小也不掺假）。缺省只补给**无键**的
            # 用例，显式 origin 永不覆盖。
            default_origin = "cards" if stem.endswith(".fromweb") else "unmarked"
            for entry in raw["cases"]:
                if not isinstance(entry, dict):
                    continue
                item = dict(entry)
                item.setdefault("feature", feature)
                item.setdefault("origin", default_origin)
                item["_file"] = path.name
                cases.append(item)
    # (feature, id) 去重：首个文件胜出（sorted 已按文件名定序）
    deduped: dict[tuple[str, object], dict[str, object]] = {}
    for case in cases:
        key = (str(case.get("feature", "?")), case.get("id"))
        deduped.setdefault(key, case)
    return list(deduped.values()), seen_files, bad_files


def summarize(
    cases: list[dict[str, object]],
) -> list[tuple[str, str, str, int]]:
    """按 (feature, origin, fingerprint) 分组计数，排序返回。"""
    counts: dict[tuple[str, str, str], int] = {}
    for case in cases:
        feature = str(case.get("feature", "?"))
        origin = str(case.get("origin", "manual"))
        fingerprint = str(case.get("batch_fingerprint", ""))
        key = (feature, origin, fingerprint)
        counts[key] = counts.get(key, 0) + 1
    return sorted((k[0], k[1], k[2], n) for k, n in counts.items())


def acceptance_denominator(
    cases: list[dict[str, object]],
) -> list[dict[str, object]]:
    """验收分母 = 仅人工核对用例（origin=manual）。

    注意：origin 缺失时**不**在此处缺省——collect_cases 已按来源文件名
    补过缺省（fromweb 文件 → cards）。fromweb 自标（cards）与合成
    （synthetic）用例的期望来自管道自身判定，混入验收分母会让指标对
    「管道一致性」虚高——按批次级切分纪律硬性排除。
    """
    return [c for c in cases if c.get("origin") == "manual"]


def check_disjoint(
    cases: list[dict[str, object]], training_fingerprints: set[str]
) -> list[str]:
    """指纹互斥校验：返回违规描述列表（空 = 无泄漏）。

    与入训记录共享 batch_fingerprint 的用例（无论 origin）一律判泄漏。
    """
    violations: list[str] = []
    for case in cases:
        fp = str(case.get("batch_fingerprint", ""))
        if fp and fp in training_fingerprints:
            violations.append(
                f"泄漏: 用例 {case.get('id', '?')}（origin={case.get('origin', 'manual')}，"
                f"文件 {case.get('_file', '?')}）与入训记录共享指纹 {fp}"
            )
    return violations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--cases-dir",
        action="append",
        default=None,
        help="用例目录（可多次）；缺省 = 用户数据目录 + 包内示例目录",
    )
    parser.add_argument(
        "--training-fingerprints",
        default="",
        help="入训记录的批次指纹（逗号分隔清单或 @文件路径）；提供即做互斥校验",
    )
    parser.add_argument(
        "--acceptance-only",
        action="store_true",
        help="只看验收分母（origin=manual）",
    )
    args = parser.parse_args(argv)

    if args.cases_dir:
        dirs = [Path(p) for p in args.cases_dir]
    else:
        from briefdesk import paths
        from briefdesk.plugins.benchmark.store import PACKAGE_CASES_DIR

        dirs = [paths.benchmark_cases_dir(), PACKAGE_CASES_DIR]

    cases, seen_files, bad_files = collect_cases(dirs)
    existing_dirs = [d for d in dirs if d.is_dir()]
    if not cases:
        # 防假绿：扫不到任何用例时互斥校验毫无意义——按错误退出并说明原因
        print(
            f"未扫描到任何用例（目录 {len(dirs)} 个、存在 {len(existing_dirs)} 个、"
            f"扫描文件 {seen_files} 个、坏文件 {bad_files} 个）——"
            "请核对 --cases-dir 或确认用例文件是否存在"
        )
        return 2
    unmarked = sum(1 for c in cases if c.get("origin") == "unmarked")
    if unmarked:
        print(
            f"警告：{unmarked} 条用例无 origin 标注（unmarked），不入验收分母——"
            "人工核对数据集请显式标注 origin=manual"
        )
    if args.acceptance_only:
        cases = acceptance_denominator(cases)
        if not cases:
            print(
                "验收分母为空（origin=manual 的用例为 0 条）——人工核对用例需显式"
                "标注 origin=manual；unmarked/cards/recorder 一律不入分母"
            )
            return 2

    groups = summarize(cases)
    scope = "验收分母（origin=manual）" if args.acceptance_only else "全部用例"
    print(f"{scope}合计：{len(cases)}（扫描文件 {seen_files}，坏文件 {bad_files}）")
    for feature, origin, fingerprint, count in groups:
        print(f"  feature={feature}  origin={origin}  fp={fingerprint or '-'}  n={count}")
    if bad_files:
        print(f"警告：{bad_files} 个文件损坏/结构非法被跳过（不影响本次统计）")

    if args.training_fingerprints:
        raw = args.training_fingerprints
        if raw.startswith("@"):
            fp_file = Path(raw[1:])
            try:
                fingerprints = {
                    line.strip()
                    for line in fp_file.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                }
            except OSError as e:
                print(f"指纹清单不可读：{fp_file}（{e}）")
                return 2
        else:
            fingerprints = {p.strip() for p in raw.split(",") if p.strip()}
        violations = check_disjoint(cases, fingerprints)
        if violations:
            print("指纹互斥校验失败：")
            for v in violations:
                print(f"  - {v}")
            return 1
        scope_note = (
            "（仅对筛后验收分母校验）" if args.acceptance_only else "（对全部用例校验）"
        )
        print(f"指纹互斥校验通过{scope_note}（比对 {len(fingerprints)} 个入训指纹）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
