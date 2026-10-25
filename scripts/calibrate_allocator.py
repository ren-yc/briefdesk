"""类别分配器留一校准工具 — 用 recat_log 纠错样本定逐类阈值（只读）。

校准契约（实例锚定的阈值标定）：每条 recat 样本轮流作查询（留一），其余
样本 + 全部 items 样本建指纹，在「命中率 ≥ API 基线且误报不升」的约束下
为每类选阈值。本脚本输出**分位数建议表**（不自动写配置——阈值属判定行为，
由所有者定夺后回填 allocator.THRESHOLD_OVERRIDES）。

数据源：真实库（--db，只读）或无库时的用法说明。流程：
1. 取 recat_log 纠错样本（每条含 category_before→category_after 与 content）；
2. 留一：对第 i 条，其余 recat 样本 + items 窗口样本建原型（按 recat
   覆盖层语义：本条按 category_after 计入、其 content 从原类剔除）；
3. 查询向量对各类打分（max/mean-of-top-k，与生产 allocate 同口径）；
4. 期望类 = category_after：报告其得分在「该类样本自相似分布」中的分位，
   以及在候选类中的排名；分位中位数即该类 p5 阈值的校准建议。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from briefdesk import ai_ports
from briefdesk.plugins.classify.allocator import (
    _MEAN_TOP_K,
    DEFAULT_P5_THRESHOLD,
    _score_against,
)

_WINDOW_DAYS = 90
_PER_CATEGORY_LIMIT = 100


def _load_samples(db_path: Path) -> tuple[list[dict], list[dict]]:
    """(items 窗口样本, recat 纠错样本)——只读打开，与本 生产同口径。"""
    import sqlite3

    if not db_path.is_file():
        print(f"数据库不存在: {db_path}", file=sys.stderr)
        raise SystemExit(2)
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        item_rows = conn.execute(
            "SELECT category, title, source_quote FROM items "
            "WHERE is_verified >= 0 AND created_at >= datetime('now', ?) "
            "ORDER BY created_at DESC",
            (f"-{_WINDOW_DAYS} days",),
        ).fetchall()
        recat_rows = conn.execute(
            "SELECT category_before, category_after, content FROM recat_log "
            "WHERE category_before != category_after "
            "AND created_at >= datetime('now', ?) "
            "ORDER BY id DESC",
            (f"-{_WINDOW_DAYS} days",),
        ).fetchall()
    finally:
        conn.close()
    per_cat: dict[str, int] = {}
    items: list[dict] = []
    for row in item_rows:
        cat = row["category"]
        if per_cat.get(cat, 0) >= _PER_CATEGORY_LIMIT:
            continue
        per_cat[cat] = per_cat.get(cat, 0) + 1
        text = f"{row['title'] or ''} {row['source_quote'] or ''}".strip()
        if text:
            items.append({"category": cat, "content": text})
    recats = [
        {
            "category_before": row["category_before"],
            "category_after": row["category_after"],
            "content": (row["content"] or "").strip(),
        }
        for row in recat_rows
        if (row["content"] or "").strip()
    ]
    return items, recats


def _dedupe(samples: list[dict]) -> tuple[list[dict], list[str]]:
    """按 content 去重（weight 高者胜出），返回 (样本, content 键列表)。

    与生产 allocator.rebuild 同口径：recat 覆盖层（weight=2）对同 content
    的普通样本（weight=1）胜出——即「计入新类、从原类剔除同源」。若按
    首条胜出且 items 在前，冲突文本会以旧类锚在指纹里，校准测的就不是
    生产分配器。
    """
    import hashlib

    by_content: dict[str, dict] = {}
    for s in samples:
        key = hashlib.sha256(str(s["content"]).encode()).hexdigest()[:16]
        prev = by_content.get(key)
        if prev is None or float(s.get("weight", 1)) > float(prev.get("weight", 1)):
            by_content[key] = s
    out = list(by_content.values())
    keys = [
        hashlib.sha256(str(s["content"]).encode()).hexdigest()[:16] for s in out
    ]
    return out, keys


async def calibrate(db_path: Path) -> dict:
    """留一校准主流程（输出建议表；不写任何配置）。"""
    if not ai_ports.is_embedding_enabled():
        print(
            "嵌入未启用（EMBED_API_BASE 为空）：分配器与校准都以向量为地基，"
            "先配置嵌入再校准。",
            file=sys.stderr,
        )
        raise SystemExit(2)
    items, recats = _load_samples(db_path)
    if not recats:
        print(
            "recat_log 无纠错样本（留一校准需要人工改类记录作真值）。"
            "先在界面上修正若干分类，积累样本后再校准。",
            file=sys.stderr,
        )
        raise SystemExit(2)

    print(
        f"样本：items {len(items)} 条 / recat {len(recats)} 条；"
        f"嵌入中（{len(items) + len(recats)} 次，批量进行）...",
        file=sys.stderr,
    )
    all_samples = [
        {"category": s["category"], "content": s["content"], "weight": 1}
        for s in items
    ] + [
        {"category": r["category_after"], "content": r["content"], "weight": 2}
        for r in recats
    ]
    deduped, keys = _dedupe(all_samples)
    vectors = await ai_ports.embed_texts(
        [str(s["content"])[:2000] for s in deduped]
    )
    if len(vectors) != len(deduped):
        print(
            f"嵌入返回数不符（{len(vectors)}/{len(deduped)}），终止。",
            file=sys.stderr,
        )
        raise SystemExit(2)
    vec_by_key = dict(zip(keys, vectors))
    content_key = {
        str(s["content"]): k for s, k in zip(deduped, keys)
    }

    per_class_scores: dict[str, list[float]] = {}
    per_class_other_scores: dict[str, list[float]] = {}
    margins: list[float] = []
    ranks: list[int] = []
    for i, r in enumerate(recats):
        q_key = content_key.get(r["content"])
        if q_key is None:
            continue
        qvec = vec_by_key[q_key]
        # 留一建指纹：全部样本中剔除本条 content（覆盖层语义——本条不得
        # 给自己投票），其余照常
        protos: dict[str, list[list[float]]] = {}
        for s, k in zip(deduped, keys):
            if k == q_key:
                continue
            protos.setdefault(str(s["category"]), []).append(vec_by_key[k])
        if not protos:
            continue
        scored = sorted(
            ((cat, _score_against(qvec, vecs)) for cat, vecs in protos.items()),
            key=lambda t: t[1],
            reverse=True,
        )
        expected = r["category_after"]
        score = dict(scored).get(expected, 0.0)
        rank = next(
            (rank for rank, (cat, _s) in enumerate(scored) if cat == expected),
            len(scored),
        )
        ranks.append(rank + 1)
        per_class_scores.setdefault(expected, []).append(score)
        # 「误报不升」约束侧：竞争类的最高分（除期望类外）——阈值若低于
        # 它会把语义相近类的消息误收进本类
        other_best = max(
            (s for cat, s in scored if cat != expected), default=0.0
        )
        per_class_other_scores.setdefault(expected, []).append(other_best)
        margins.append(score - other_best)

    report: dict = {
        "window_days": _WINDOW_DAYS,
        "per_category_limit": _PER_CATEGORY_LIMIT,
        "mean_top_k": _MEAN_TOP_K,
        "recat_samples": len(recats),
        "items_samples": len(items),
        "expected_rank_median": _median(ranks) if ranks else None,
        "margin_median": round(_median_f(margins), 4) if margins else None,
        "categories": {},
    }
    for cat, scores in sorted(per_class_scores.items()):
        ordered = sorted(scores)
        p25 = _percentile(ordered, 25)
        # 建议阈值双约束：≥ 期望类得分 p25（防漏收，召回侧）且 ≥ 竞争类
        # 最高分 p75（防误收语义相近类，误报侧）——两侧证据并排给出，
        # 冲突时（p25(other) > p25(expected)）说明该类区分度不足，须人工复核
        others = sorted(per_class_other_scores.get(cat, []))
        other_p75 = _percentile(others, 75) if others else 0.0
        suggested = max(p25, other_p75)
        report["categories"][cat] = {
            "n": len(scores),
            "score_min": round(ordered[0], 4),
            "score_p25": round(p25, 4),
            "score_median": round(_percentile(ordered, 50), 4),
            "other_best_p75": round(other_p75, 4),
            "suggested_threshold": round(max(0.0, min(1.0, suggested)), 4),
            "ambiguous": bool(others and other_p75 > p25),
            "note": (
                "建议 = max(期望类得分 p25, 竞争类最高分 p75)；ambiguous=true "
                "表示两侧约束冲突（类间区分度不足），回填 THRESHOLD_OVERRIDES 前先人工复核该类样本"
            ),
        }
    report["default_threshold"] = DEFAULT_P5_THRESHOLD
    return report


def _percentile(sorted_vals: list[float], pct: float) -> float:
    if not sorted_vals:
        return 0.0
    rank = (len(sorted_vals) - 1) * pct / 100.0
    low = int(rank)
    high = min(low + 1, len(sorted_vals) - 1)
    frac = rank - low
    return sorted_vals[low] * (1 - frac) + sorted_vals[high] * frac


def _median(vals: list[int]) -> float:
    ordered = sorted(vals)
    n = len(ordered)
    if n % 2:
        return float(ordered[n // 2])
    return (ordered[n // 2 - 1] + ordered[n // 2]) / 2


def _median_f(vals: list[float]) -> float:
    ordered = sorted(vals)
    n = len(ordered)
    if not n:
        return 0.0
    if n % 2:
        return ordered[n // 2]
    return (ordered[n // 2 - 1] + ordered[n // 2]) / 2


def main() -> int:
    parser = argparse.ArgumentParser(
        description="类别分配器留一校准（recat_log 真值；只读，输出建议表）"
    )
    parser.add_argument(
        "--db",
        type=Path,
        help="真实库路径（只读打开）；缺省用运行时默认路径",
    )
    args = parser.parse_args()
    db_path = args.db
    if db_path is None:
        from briefdesk.paths import database_path

        db_path = database_path()
    import asyncio

    report = asyncio.run(calibrate(db_path))
    print(json.dumps(report, ensure_ascii=False, indent=1))
    print(
        "\n使用方式：把各 suggested_threshold 回填 "
        "briefdesk/plugins/classify/allocator.py 的 THRESHOLD_OVERRIDES，"
        "重启后 rebuild 生效；随后跑 allocator 影子打分与 benchmark 验收门。",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
