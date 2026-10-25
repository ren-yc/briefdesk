"""影子运行 — 时间解析器只记录不生效，与 sysc 现产出逐字段 diff。

职责（时间解析剥离的第一步，交付物为「可信的影子统计」）：- 采集：管道时间提取（sysc）完成后，把「sysc 产出 vs 规则解析产出」的
  逐字段差异写入影子日志（用户数据目录 temporal_shadow/ 下每日 jsonl）；
  sysc 失败样本（异常/空响应/截断）同样采集并带失败形态标记——它们正是
  解析器最该顶上的分母，漏采会让「sysc 降为兜底」的收益无法测量。
- 统计：start/end 集合差按 (type, time) 对比（type 互换也是分歧）、
  粒度分布（date-only vs 分钟级）、线索级漏检（逐条线索标记 resolved，
  按 pattern_id 归因到模式）。消息级/线索级/值级三段量纲不同，汇总分开
  分栏，不做跨量纲比值。
- 边界：影子期解析产出**绝不写回** ClassifyResult（只记录不生效）；
  采集自身故障绝不连坐管道（全部吞异常记 DEBUG）。

隐私：影子日志含词法片段（真实消息子串），msg_id 只落 SHA-256 截断散列；
文件在用户数据目录（不入仓库），人工查阅或外发时须按含聊天片段对待。

开关：影子采集由环境变量 TEMPORAL_SHADOW=on 开启（默认 off，见
docs/architecture.md 环境变量表）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any

from briefdesk import paths

logger = logging.getLogger(__name__)

_SHADOW_ON: bool | None = None  # 惰性读取 env（测试可 monkeypatch）


def shadow_enabled() -> bool:
    """影子采集开关（TEMPORAL_SHADOW=on 开启；默认 off）。"""
    global _SHADOW_ON
    if _SHADOW_ON is None:
        import os

        _SHADOW_ON = os.environ.get("TEMPORAL_SHADOW", "").strip().lower() == "on"
    return _SHADOW_ON


def set_shadow_enabled(value: bool) -> None:
    """显式置位开关（测试用；生产走 env）。"""
    global _SHADOW_ON
    _SHADOW_ON = value


def shadow_dir() -> Path:
    """影子日志目录（用户数据目录 temporal_shadow/；不入仓库）。"""
    return paths.user_data_dir() / "temporal_shadow"


def _hash_msg_id(msg_id: str) -> str:
    """msg_id 散列（同消息对账够用；日志会被人查阅，不落明文标识）。"""
    return hashlib.sha256(msg_id.encode()).hexdigest()[:12]


def record_diff(
    source: str,
    msg_id: str,
    anchor_epoch: int | None,
    sysc_times: list[dict[str, Any]],
    parser_result: Any,
    sysc_failed: str = "",
) -> None:
    """采集一条消息的影子 diff（sysc 完成后调用；全部吞异常）。

    sysc_times：sysc 产出的 times 数组（{type,time,label} 原始 dict 列表，
    含空）；parser_result：规则包 ParseResult（times + hints）；
    sysc_failed：失败形态（"error"/"empty_choices"/"truncated"，成功恒空）。
    """
    try:
        if not shadow_enabled():
            return
        sysc_pairs = sorted(
            {
                (str(t.get("type") or ""), str(t.get("time") or ""))
                for t in sysc_times
                if isinstance(t, dict) and t.get("time")
            }
        )
        parser_pairs = sorted({(p.type, p.time) for p in parser_result.times})
        missing = [f"{t}|{v}" for t, v in sysc_pairs if (t, v) not in parser_pairs]
        extra = [f"{t}|{v}" for t, v in parser_pairs if (t, v) not in sysc_pairs]
        sysc_values = sorted({v for _t, v in sysc_pairs})
        parser_values = sorted({v for _t, v in parser_pairs})

        def _granularity(values: list[str]) -> dict[str, int]:
            return {
                "date": sum(1 for v in values if " " not in v),
                "minute": sum(1 for v in values if " " in v),
            }

        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "source": source,
            "msg_id_hash": _hash_msg_id(msg_id),
            "anchor": anchor_epoch or 0,
            "sysc_failed": sysc_failed,
            "sysc": [f"{t}|{v}" for t, v in sysc_pairs],
            "parser": [f"{t}|{v}" for t, v in parser_pairs],
            "missing_in_parser": missing,
            "extra_in_parser": extra,
            "granularity": {
                "sysc": _granularity(sysc_values),
                "parser": _granularity(parser_values),
            },
            "hints": [
                {
                    "kind": h.kind,
                    "pattern_id": h.pattern_id,
                    "position": h.position,
                    "resolved": parser_result.hint_resolved(h),
                }
                for h in parser_result.hints
            ],
        }
        target_dir = shadow_dir()
        target_dir.mkdir(parents=True, exist_ok=True)
        daily = target_dir / f"shadow-{time.strftime('%Y%m%d')}.jsonl"
        with daily.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:  # 影子采集故障绝不连坐管道（盲捕为设计意图，ruff 未启用 BLE001）
        logger.debug("temporal shadow 采集失败（忽略）", exc_info=True)


def summarize_shadow_file(path: Path) -> dict[str, Any]:
    """汇总一份影子日志（diff 报告生成器；只读）。

    返回三段量纲分明的统计（不做跨量纲比值——比值留给报告层按需计算）：
    消息级（total/一致/分歧/双空/sysc_failed）、线索级（强/弱/未覆盖，
    按 pattern_id 分桶）、值级（粒度分布）。
    """
    total = 0
    both_empty = 0
    sysc_only = 0
    parser_only = 0
    agree_exact = 0
    disagree = 0
    sysc_failed = 0
    strong_hints = 0
    weak_hints = 0
    strong_unresolved = 0
    missed_by_pattern: dict[str, int] = {}
    granularity: dict[str, int] = {
        "sysc_date": 0, "sysc_minute": 0,
        "parser_date": 0, "parser_minute": 0,
    }
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            total += 1
            if entry.get("sysc_failed"):
                sysc_failed += 1
            sysc = entry.get("sysc") or []
            parser = entry.get("parser") or []
            missing = entry.get("missing_in_parser") or []
            extra = entry.get("extra_in_parser") or []
            if not sysc and not parser:
                both_empty += 1
            elif not missing and not extra:
                agree_exact += 1
            elif sysc and not parser:
                sysc_only += 1
            elif parser and not sysc:
                parser_only += 1
            else:
                disagree += 1
            for hint in entry.get("hints") or []:
                if hint.get("kind") == "strong":
                    strong_hints += 1
                    if not hint.get("resolved"):
                        strong_unresolved += 1
                        pid = str(hint.get("pattern_id", "?"))
                        missed_by_pattern[pid] = missed_by_pattern.get(pid, 0) + 1
                else:
                    weak_hints += 1
            g = entry.get("granularity") or {}
            granularity["sysc_date"] += int((g.get("sysc") or {}).get("date") or 0)
            granularity["sysc_minute"] += int((g.get("sysc") or {}).get("minute") or 0)
            granularity["parser_date"] += int((g.get("parser") or {}).get("date") or 0)
            granularity["parser_minute"] += int((g.get("parser") or {}).get("minute") or 0)
    return {
        "messages": {
            "total": total,
            "agree_exact": agree_exact,
            "disagree": disagree,
            "both_empty": both_empty,
            "sysc_only": sysc_only,
            "parser_only": parser_only,
            "sysc_failed": sysc_failed,
        },
        "hints": {
            "strong": strong_hints,
            "weak": weak_hints,
            "strong_unresolved": strong_unresolved,
            "missed_by_pattern": dict(sorted(missed_by_pattern.items())),
        },
        "granularity": granularity,
    }
