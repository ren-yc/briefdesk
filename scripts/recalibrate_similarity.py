"""DEDUP_SIMILARITY_THRESHOLD 重标定工具 — 度量换轨后的阈值重定流程（只读）。

背景：去重重叠通道的 `_title_overlap` 由单字符集合交/min 改为字符 bigram
Jaccard。该分数同时是重叠通道的召回门禁（>= 阈值才触发 AI 判重）与加权
多数票的票权，换度量 = 改判定行为——旧阈值 0.3 是字符集口径下的经验值，
bigram 口径下分布整体左移（共同词只有少量 bigram 重合），沿用旧值会漏召回。

本脚本在**换度量口径下**重算重叠分数分布，输出候选阈值下的召回/触发率，
供重标定决策（不自动写配置——阈值变更属判定行为变更，由所有者定夺）。
现行默认值即本流程的产物：用例集自有标题对在 0.07 处召回约九成、负例触发
约九成九，而 0.09→0.10 之间有个断崖（0.10 起召回腰斩），故取断崖前的平台。

数据源（二选一，都只读）：
- benchmark dedup 用例目录（--cases-dir，可多个）：用 query 与 items 的
  标题对（期望 same=true 的正样本对召回敏感）重算分数；
- 真实库（--db）：全部存量卡两两标题分数过低会组合爆炸，改为**采样对**
  （每卡与其全局最高分对手，覆盖「会被门禁放进来的最高分候选」）。

输出：分数分位数 + 若干候选阈值下的「正样本召回率 / 全量触发率」对照表。
触发率含义：阈值越低，越多对子触发 AI 判重（成本↑）且弱票越多；召回率
是硬约束——先保召回（同文本重复必须触发），再在余量内取高阈值控成本。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 使脚本可直接运行（python scripts/recalibrate_similarity.py）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from briefdesk.plugins.dedup.engine import DedupEngine

# 与生产重叠通道同一比较面：标题（_best_overlap_candidate 只比 title）


def _pairs_from_cases(
    cases_dirs: list[Path],
) -> list[tuple[str, str, bool]]:
    """从 benchmark dedup 用例提取 (query 标题, item 标题, 期望 same)。

    用例 id 缺失/字段畸形跳过（标定工具不应因个别坏用例失明）。
    """
    pairs: list[tuple[str, str, bool]] = []
    seen_files = bad_files = 0
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
            if (
                not isinstance(raw, dict)
                or str(raw.get("feature", "")) != "dedup"
                or not isinstance(raw.get("cases"), list)
            ):
                continue
            for entry in raw["cases"]:
                if not isinstance(entry, dict):
                    continue
                expected = entry.get("expected")
                query = entry.get("query")
                items = entry.get("items")
                if (
                    not isinstance(expected, dict)
                    or not isinstance(query, dict)
                    or not isinstance(items, list)
                ):
                    continue
                q_title = str(query.get("title", "") or "")
                same = bool(expected.get("same", False))
                for it in items:
                    if not isinstance(it, dict):
                        continue
                    i_title = str(it.get("title", "") or "")
                    if q_title and i_title:
                        pairs.append((q_title, i_title, same))
    if bad_files:
        print(f"警告：{bad_files} 个文件无法解析（跳过）", file=sys.stderr)
    return pairs


def _pairs_from_db(db_path: Path, limit: int) -> list[tuple[str, str, bool]]:
    """从真实库采样标题对：每卡与其全局最高分对手。

    正样本标签不可得（库里没有人工判定的「重复对」真值），expected 一律
    False——这份样本只用于**触发率分布**（门禁会放进来的对子长什么样、
    各阈值下会放多少），召回率必须由用例目录（有真值）承担。grams 预计算
    后两两比对（每卡 O(n) 次集合交并），limit 限制参与卡数防大库组合爆炸。
    """
    import sqlite3

    if not db_path.is_file():
        print(f"数据库不存在: {db_path}", file=sys.stderr)
        raise SystemExit(2)
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT title FROM items WHERE is_verified >= 0 "
            "ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    titles = [r[0] for r in rows if r[0]]

    def _grams(text: str) -> frozenset[str] | set[str]:
        # 与 _title_overlap 同口径的 bigram 构造（含单字符回退语义）；
        # 预计算一次、两两复用，避免每对现场重造集合
        t = text.replace(" ", "")
        if len(t) < 2:
            return set(t)
        return {t[i : i + 2] for i in range(len(t) - 1)}

    grams = [(t, _grams(t)) for t in titles]
    pairs: list[tuple[str, str, bool]] = []
    for i, (a, ga) in enumerate(grams):
        best_title, best_score = "", 0.0
        for j, (b, gb) in enumerate(grams):
            if i == j or not ga or not gb:
                continue
            union = len(ga | gb)
            s = len(ga & gb) / union if union else 0.0
            if s > best_score:
                best_score, best_title = s, b
        if best_title:
            pairs.append((a, best_title, False))
    return pairs


def _percentile(sorted_vals: list[float], pct: float) -> float:
    if not sorted_vals:
        return 0.0
    rank = (len(sorted_vals) - 1) * pct / 100.0
    low = int(rank)
    high = min(low + 1, len(sorted_vals) - 1)
    frac = rank - low
    return sorted_vals[low] * (1 - frac) + sorted_vals[high] * frac


def recalibrate(
    pairs: list[tuple[str, str, bool]], thresholds: list[float]
) -> dict[str, object]:
    """重算分数 + 分位数 + 候选阈值对照表（纯函数，便于单测）。"""
    scored = [(DedupEngine._title_overlap(a, b), same) for a, b, same in pairs]
    pos = sorted(s for s, same in scored if same)
    all_scores = sorted(s for s, _ in scored)
    quantiles = {
        f"p{int(p)}": round(_percentile(all_scores, p), 4)
        for p in (5, 25, 50, 75, 95)
    }
    table = []
    for t in thresholds:
        pos_hit = sum(1 for s in pos if s >= t)
        triggered = sum(1 for s, _ in scored if s >= t)
        table.append(
            {
                "threshold": t,
                "positive_recall": round(pos_hit / len(pos), 4) if pos else None,
                "trigger_rate": round(triggered / len(scored), 4) if scored else None,
                "triggered": triggered,
            }
        )
    return {
        "pairs": len(scored),
        "positive_pairs": len(pos),
        "positive_min": round(min(pos), 4) if pos else None,
        "positive_p5": round(_percentile(pos, 5), 4) if pos else None,
        "all_quantiles": quantiles,
        "thresholds": table,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="DEDUP_SIMILARITY_THRESHOLD 重标定（bigram Jaccard 口径）"
    )
    parser.add_argument(
        "--cases-dir",
        action="append",
        type=Path,
        default=[],
        help="benchmark 用例目录（dedup 用例提供召回真值；可多次指定）",
    )
    parser.add_argument(
        "--db",
        type=Path,
        help="真实库路径（采样对只供触发率分布；只读打开）",
    )
    parser.add_argument(
        "--db-limit",
        type=int,
        default=2000,
        help="真实库采样参与卡数上限（默认 2000）",
    )
    parser.add_argument(
        "--thresholds",
        default="0.05,0.07,0.1,0.15,0.3",
        help="逗号分隔的候选阈值（默认覆盖现行值 0.07 两侧，并留旧口径值 0.3 作对照）",
    )
    args = parser.parse_args()
    pairs: list[tuple[str, str, bool]] = []
    if args.cases_dir:
        pairs += _pairs_from_cases(args.cases_dir)
    if args.db:
        pairs += _pairs_from_db(args.db, args.db_limit)
    if not pairs:
        print("无可用样本：需 --cases-dir（召回真值）或 --db（触发率分布）",
              file=sys.stderr)
        return 2
    thresholds = [
        float(x.strip()) for x in args.thresholds.split(",") if x.strip()
    ]
    result = recalibrate(pairs, thresholds)
    print(json.dumps(result, ensure_ascii=False, indent=1))
    if not args.cases_dir:
        print(
            "\n注意：无用例目录时没有召回真值，上表只反映触发率分布；"
            "阈值取舍必须结合带真值的用例目录复核（正样本召回率是硬约束）。",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
