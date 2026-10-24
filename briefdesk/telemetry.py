"""最小遥测集 — 进程内计数器与耗时分位 + 落盘快照（只读验收口径）。

设计约束：
- 事件循环单线程消费，全部结构无锁；record/observe 永不抛错（遥测故障
  绝不连坐管道判定路径），读路径（snapshot/format_summary）同样兜底。
- 不引入外部依赖：分位数用内插法在内存样本上计算，直方图即「样本 +
  分位函数」。内存样本是**有界近因窗口**：超上限按 1/2 抽稀，窗口内的
  分位数仍代表近期分布；全量 count/total 单独累计上报，不与窗口混淆。
- 落盘到用户数据目录 telemetry/ 下：latest.json（最新快照，验收门读数
  入口）+ 每日 jsonl（追加，供跨日趋势）+ cosine_truth 每日 jsonl（merge
  候选对余弦-真值明细，预筛门限 ROC 标定脚本的输入——内存态重启即丢，
  只有落盘明细可供仓外脚本读取）。全部写入用户数据目录，永不入仓库。
- 与 announcements 的分工：遥测只记数，公告（持续性条件横幅）由各引擎
  自行置位/撤销——遥测不置公告。
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any

from briefdesk import paths

logger = logging.getLogger(__name__)

# 单指标内存样本窗口上限：超限按 1/2 抽稀（保序抽偶数位）。窗口内分位数
# 是近期分布的近似；全量计数/求和另维护（见 snapshot 的 *_total 字段）。
_MAX_SAMPLES = 10_000
# 余弦-真值明细的内存上限：超出最旧行先落盘淘汰（flush 增量写 jsonl），
# 防长跑进程无界增长。明细的权威存储是落盘文件，内存只是待写缓冲。
_MAX_COSINE_TRUTH = 20_000
# 落盘节流：两次快照落盘的最小间隔（秒）。节流窗口在写盘前占用——落盘
# 失败（磁盘故障）也不会触发每轮重试风暴；代价是失败后窗口内的数据要等
# 下一个周期落盘。summary/flush 由轮询周期尾部挂钩调用，频率天然有界。
_FLUSH_MIN_INTERVAL = 30.0

# ── 内存结构 ──

_counters: dict[str, int] = defaultdict(int)
# (metric 名, source 维度值) → [耗时样本窗口, 全量条数, 全量毫秒和]
_samples: dict[tuple[str, str], list[Any]] = defaultdict(
    lambda: [[], 0, 0.0]
)
# merge 余弦-真值记录（ROC 标定输入）：(source, cosine, verdict)；
# verdict: True=SAME/MERGE，False=DIFF/不合并，None=判定未知
_cosine_truth: list[tuple[str, float, bool | None]] = []
# 已落盘的余弦-真值行数（增量落盘游标）
_cosine_flushed = 0
# 余弦-真值行的进程内自增序号（跨重启从 1 复用，靠 _COSINE_BOOT 区分批次）
_cosine_seq = 0
# 进程启动批次号（uuid4 十二位短码）：落盘行的去重键组成部分
_COSINE_BOOT = uuid.uuid4().hex[:12]
_last_flush = 0.0


def reset_for_tests() -> None:
    """清空全部内存状态（测试隔离用；生产不调用）。"""
    global _cosine_truth, _last_flush, _cosine_flushed, _cosine_seq
    _counters.clear()
    _samples.clear()
    _cosine_truth = []
    _cosine_flushed = 0
    _cosine_seq = 0
    _last_flush = 0.0


# ── 记录入口 ──


def record(kind: str, source: str = "") -> None:
    """计数 +1（kind 如 "judge.call"；source 为消息源标识，无维度时空串）。"""
    try:
        _counters[f"{kind}|{source}"] += 1
    except Exception:  # noqa: BLE001 — 遥测永不连坐业务路径
        logger.debug("telemetry record 失败（忽略）: %s/%s", kind, source)


def observe(kind: str, ms: float, source: str = "") -> None:
    """记录一次耗时样本（毫秒）。窗口超上限按 1/2 抽稀；全量计数/求和
    独立累计（不随抽稀丢失）。"""
    try:
        bucket = _samples[(kind, source)]
        bucket[0].append(ms)
        bucket[1] += 1
        bucket[2] += ms
        if len(bucket[0]) > _MAX_SAMPLES:
            del bucket[0][::2]
    except Exception:  # noqa: BLE001 — 同上：遥测永不连坐
        logger.debug("telemetry observe 失败（忽略）: %s", kind)


def record_cosine_truth(source: str, cosine: float, verdict: bool | None) -> None:
    """记录 merge 候选对的（余弦, 判定真值）对——预筛门限 ROC 标定的输入。

    verdict 沿用判定语义：True=SAME/MERGE，False=DIFF/不合并，
    None=判官不可用/解析失败（判定未知，不入 ROC 但计入覆盖率分母）。
    明细经 flush 增量写入 cosine_truth 每日 jsonl（标定脚本读文件，内存
    只是待写缓冲）；淘汰只发生在 flush 写盘成功之后——最旧行永远先落盘
    再被淘汰，权威存储是文件，标定数据不因内存上限丢失。
    """
    try:
        _cosine_truth.append((source, float(cosine), verdict))
    except Exception:  # noqa: BLE001 — 同上：遥测永不连坐
        logger.debug("telemetry record_cosine_truth 失败（忽略）")


def cosine_truth_rows() -> list[tuple[str, float, bool | None]]:
    """读取内存中尚未落盘的余弦-真值明细（测试/诊断用；返回副本）。

    权威数据在 telemetry_dir()/cosine_truth-YYYYMMDD.jsonl。
    """
    return list(_cosine_truth)


# ── 汇总与落盘 ──


def _percentile(sorted_values: list[float], pct: float) -> float:
    """线性内插分位数（sorted_values 非空；pct ∈ [0, 100]）。"""
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = (len(sorted_values) - 1) * pct / 100.0
    low = int(rank)
    high = min(low + 1, len(sorted_values) - 1)
    frac = rank - low
    return sorted_values[low] * (1 - frac) + sorted_values[high] * frac


def snapshot() -> dict[str, Any]:
    """当前遥测快照（JSON 安全）。字段名即验收门读数口径：
    counters: {kind|source: count}；
    latencies: {kind: {source: {window_count, p50/p95/p99_ms, total_count, total_ms}}}
    —— window_* 是抽稀窗口内的分位与样本数，total_* 是自进程启动的全量
    累计（分位数只从窗口算，不代表全量）。
    cosine_truth_count 是**未落盘**的明细缓冲行数（权威存储是
    cosine_truth-*.jsonl 文件，落盘后缓冲收缩），不是历史累计行数。
    """
    latencies: dict[str, dict[str, Any]] = defaultdict(dict)
    for (kind, source), (window, n_total, sum_total) in sorted(_samples.items()):
        ordered = sorted(window)
        latencies[kind][source or "-"] = {
            "window_count": len(ordered),
            "p50_ms": round(_percentile(ordered, 50), 1),
            "p95_ms": round(_percentile(ordered, 95), 1),
            "p99_ms": round(_percentile(ordered, 99), 1),
            "total_count": n_total,
            "total_ms": round(sum_total, 1),
        }
    return {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "counters": dict(sorted(_counters.items())),
        "latencies": dict(latencies),
        "cosine_truth_count": len(_cosine_truth),
    }


def format_summary() -> str:
    """单行 INFO 汇总（轮询周期尾部打一条；空遥测返回空串不打日志）。
    计数部分含全量口径；耗时只报窗口分位（全量均值近似 total_ms/total_count）。"""
    try:
        if not _counters and not _samples:
            return ""
        parts: list[str] = []
        for key, count in sorted(_counters.items()):
            parts.append(f"{key}={count}")
        for (kind, _source), (window, _n, _s) in sorted(_samples.items()):
            ordered = sorted(window)
            parts.append(
                f"{kind}:n={len(ordered)},p95={_percentile(ordered, 95):.0f}ms"
            )
        return "遥测 " + " ".join(parts)
    except Exception:  # noqa: BLE001 — 读路径同样不连坐轮询周期
        return ""


def telemetry_dir() -> Path:
    """落盘目录（用户数据目录 telemetry/；不入仓库）。"""
    return paths.user_data_dir() / "telemetry"


def flush() -> None:
    """把快照与余弦-真值明细落盘。

    - latest.json 整文件覆盖（tmp + replace 原子写）；
    - 当日 telemetry-YYYYMMDD.jsonl 追加一行快照；
    - 当日 cosine_truth-YYYYMMDD.jsonl 增量追加未落盘明细行。不重复落盘
      由游标 _cosine_flushed 保证（单进程内幂等）；行内 (boot, seq) 二元组
      可作跨重启的全局去重键——boot 是进程启动时生成的随机批次号，seq 是
      进程内自增序号（重启后从 1 重新计，靠 boot 区分批次）。

    节流：距上次 flush 不足 _FLUSH_MIN_INTERVAL 秒时整体跳过（窗口在写盘
    前占用，落盘失败也不会触发每轮重试风暴）。任何失败只记 DEBUG——
    落盘故障绝不影响调用方（轮询周期尾部）。
    """
    global _last_flush, _cosine_flushed, _cosine_seq
    try:
        now = time.monotonic()
        if _last_flush and now - _last_flush < _FLUSH_MIN_INTERVAL:
            return
        _last_flush = now
        target_dir = telemetry_dir()
        target_dir.mkdir(parents=True, exist_ok=True)
        snap = snapshot()
        latest = target_dir / "latest.json"
        tmp = latest.with_name(latest.name + ".tmp")
        tmp.write_text(
            json.dumps(snap, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        tmp.replace(latest)
        daily = target_dir / f"telemetry-{time.strftime('%Y%m%d')}.jsonl"
        with daily.open("a", encoding="utf-8") as f:
            f.write(json.dumps(snap, ensure_ascii=False) + "\n")
        # 余弦-真值明细增量落盘（标定输入的权威存储）。行内 (boot, seq)：
        # boot 为进程启动批次号（跨重启区分），seq 进程内自增（重启后从 1
        # 复用）——标定脚本按 (boot, seq) 全局去重，或直接不去重（不影响
        # ROC 形状）。不重复落盘由游标保证，seq 只服务跨重启对账。
        pending = _cosine_truth[_cosine_flushed:]
        if pending:
            truth_daily = (
                target_dir / f"cosine_truth-{time.strftime('%Y%m%d')}.jsonl"
            )
            with truth_daily.open("a", encoding="utf-8") as f:
                for source, cosine, verdict in pending:
                    _cosine_seq += 1
                    f.write(
                        json.dumps(
                            {
                                "boot": _COSINE_BOOT,
                                "seq": _cosine_seq,
                                "source": source,
                                "cosine": cosine,
                                "verdict": verdict,
                            },
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
            _cosine_flushed = len(_cosine_truth)
            # 内存缓冲超限：删掉的 k 行都已落盘，游标同步收缩到 len(buffer)——
            # 保留行全部已写、下轮 pending 从新缓冲尾部起算，不重写旧行
            if _cosine_flushed > _MAX_COSINE_TRUTH:
                del _cosine_truth[: _cosine_flushed - _MAX_COSINE_TRUTH]
                _cosine_flushed = len(_cosine_truth)
    except Exception:  # 落盘故障不连坐轮询周期（ruff 对此处的盲捕无告警）
        logger.debug("telemetry flush 失败（忽略）", exc_info=True)
