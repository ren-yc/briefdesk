"""基准指标计算 — 纯函数（不调用 AI / 不访问 DB），可独立单测。

每个功能：per-case 评估记录（runner 产出）→ 聚合指标（report 消费）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from briefdesk.plugins.benchmark.schema import ClassifyCase, ClassifyExpected
from briefdesk.types import ClassifyOutcome, ClassifyResult


def _safe_div(num: float, den: float) -> float:
    return num / den if den else 0.0


# ── 通用二元判定指标（dedup / merge 共用；positive = True）──


@dataclass
class BinaryCaseEval:
    """单条布尔判定用例的评估记录。"""

    case_id: str
    predicted: bool
    expected: bool
    skipped: bool = False  # dedup：候选预筛未触发 AI 判定（非 AI 结论）
    error: str | None = None


def aggregate_binary(evals: list[BinaryCaseEval]) -> dict[str, float | int]:
    """聚合二元判定：accuracy / precision / recall / F1 + 混淆矩阵。"""
    tp = fp = tn = fn = 0
    valid = [e for e in evals if e.error is None]
    for e in valid:
        if e.predicted and e.expected:
            tp += 1
        elif e.predicted and not e.expected:
            fp += 1
        elif not e.predicted and not e.expected:
            tn += 1
        else:
            fn += 1
    total = tp + fp + tn + fn
    precision = _safe_div(tp, tp + fp)
    recall = _safe_div(tp, tp + fn)
    return {
        "cases": len(evals),
        "error_cases": len(evals) - len(valid),
        "accuracy": _safe_div(tp + tn, total),
        "precision": precision,
        "recall": recall,
        "f1": _safe_div(2 * precision * recall, precision + recall),
        "tp": tp,
        "fp": fp,
        "tn": tn,
        "fn": fn,
        "skipped": sum(1 for e in evals if e.skipped),
    }


# ── classify：类别 + 时间提取 + 健壮性 ──


@dataclass
class ClassifyCaseEval:
    """单批分类用例的评估记录（指标分子分母全部保留，聚合时再汇总）。"""

    case_id: str
    messages_total: int = 0
    # 类别
    expected_count: int = 0  # 期望应分类的条数
    hits: int = 0  # 类别精确命中数
    model_count: int = 0  # 模型实际分类条数（含期望外误分类）
    false_positives: int = 0  # 模型分类了期望之外的消息
    # 时间（start/end/extra_times）
    time_msgs_expected: int = 0  # 期望含时间点的消息数
    time_msgs_ok: int = 0  # 时间点集合与期望完全一致的消息数
    time_points_expected: int = 0  # 期望时间点总数（主字段 + extra_times）
    time_points_ok: int = 0  # 命中（期望 ⊆ 模型输出）点数
    time_points_model: int = 0  # 模型输出时间点总数
    # 健壮性
    failed: int = 0  # outcome.failed（本轮失败、待下轮重试的消息数）
    summary_filled: int = 0  # 概括标题已填充数
    summary_total: int = 0  # 模型分类结果总数
    error: str | None = None


def _result_points(result: ClassifyResult) -> set[tuple[str, str]]:
    """模型结果的 (type, time) 点集合（主字段 + extra_times）。"""
    pts: set[tuple[str, str]] = set()
    if result.start:
        pts.add(("start", result.start))
    if result.end:
        pts.add(("end", result.end))
    for t in result.extra_times:
        if t.get("type") in ("start", "end") and t.get("time"):
            pts.add((t["type"], t["time"]))
    return pts


def _expected_points(exp: ClassifyExpected) -> set[tuple[str, str]]:
    """期望的 (type, time) 点集合（主字段 + times）。"""
    pts: set[tuple[str, str]] = set()
    if exp.start:
        pts.add(("start", exp.start))
    if exp.end:
        pts.add(("end", exp.end))
    for t in exp.times:
        pts.add((t.type, t.time))
    return pts


def evaluate_classify_case(case: ClassifyCase, outcome: ClassifyOutcome) -> ClassifyCaseEval:
    """把一次 classify_batch 结果与期望比对，产出评估记录。

    - 类别命中：结果存在且 category 与期望一致（期望外输出计为误报）；
    - 时间点：只比较 (type, time)，忽略 label（AI 对任务名的措辞是模糊的）；
      "完全一致"要求模型时间点集合与期望集合精确相等（期望应写全）。
    """
    ev = ClassifyCaseEval(case_id=case.id, messages_total=len(case.messages))
    exp_by_index = {e.index: e for e in case.expected}
    result_by_index = {r.msg_index: r for r in outcome.results}

    for idx, exp in exp_by_index.items():
        result = result_by_index.get(idx)
        if result is not None and result.category == exp.category:
            ev.hits += 1
    ev.expected_count = len(exp_by_index)
    ev.model_count = len(result_by_index)
    ev.false_positives = len(set(result_by_index) - set(exp_by_index))

    for idx, exp in exp_by_index.items():
        exp_pts = _expected_points(exp)
        if not exp_pts:
            continue
        ev.time_msgs_expected += 1
        model_pts = _result_points(result_by_index[idx]) if idx in result_by_index else set()
        ev.time_points_expected += len(exp_pts)
        ev.time_points_ok += len(exp_pts & model_pts)
        ev.time_points_model += len(model_pts)
        if model_pts == exp_pts:
            ev.time_msgs_ok += 1

    ev.failed = len(outcome.failed)
    ev.summary_total = len(outcome.results)
    ev.summary_filled = sum(1 for r in outcome.results if (r.summary or "").strip())
    return ev


def aggregate_classify(evals: list[ClassifyCaseEval]) -> dict[str, float | int]:
    """聚合分类指标（各用例分子分母求和后再算比率）。"""
    valid = [e for e in evals if e.error is None]
    expected = sum(e.expected_count for e in valid)
    hits = sum(e.hits for e in valid)
    model = sum(e.model_count for e in valid)
    precision = _safe_div(hits, model)
    recall = _safe_div(hits, expected)
    msgs_expected = sum(e.time_msgs_expected for e in valid)
    msgs_ok = sum(e.time_msgs_ok for e in valid)
    pts_expected = sum(e.time_points_expected for e in valid)
    pts_ok = sum(e.time_points_ok for e in valid)
    pts_model = sum(e.time_points_model for e in valid)
    msgs_total = sum(e.messages_total for e in valid)
    failed = sum(e.failed for e in valid)
    summary_total = sum(e.summary_total for e in valid)
    summary_filled = sum(e.summary_filled for e in valid)
    return {
        "cases": len(evals),
        "error_cases": len(evals) - len(valid),
        "expected": expected,
        "model_classified": model,
        "category_accuracy": recall,  # 期望集合上的命中率（含漏检惩罚）
        "category_precision": precision,
        "category_recall": recall,
        "category_f1": _safe_div(2 * precision * recall, precision + recall),
        "false_positives": sum(e.false_positives for e in valid),
        "time_msg_accuracy": _safe_div(msgs_ok, msgs_expected),
        "time_point_recall": _safe_div(pts_ok, pts_expected),
        "time_point_precision": _safe_div(pts_ok, pts_model),
        "failure_rate": _safe_div(failed, msgs_total),
        "summary_fill_rate": _safe_div(summary_filled, summary_total),
    }


# ── title：标题重拟 ──


@dataclass
class TitleCaseEval:
    """单条标题重拟用例的评估记录。"""

    case_id: str
    output: str | None = None  # 模型输出；None = 回退原标题
    expected_title: str | None = None  # 精确匹配期望（可选）
    expected_keywords: list[str] = field(default_factory=list)  # 包含匹配期望（可选）
    error: str | None = None

    @property
    def exact_hit(self) -> bool:
        return (
            self.output is not None
            and self.expected_title is not None
            and self.output == self.expected_title
        )

    @property
    def keyword_hit(self) -> bool:
        if self.output is None or not self.expected_keywords:
            return False
        return all(k in self.output for k in self.expected_keywords)

    @property
    def too_long(self) -> bool:
        """标题长度合规：> 30 字视为违规（与 TITLE_PROMPT 的约束一致）。"""
        return self.output is not None and len(self.output) > 30


def aggregate_title(evals: list[TitleCaseEval]) -> dict[str, float | int]:
    valid = [e for e in evals if e.error is None]
    exact_cases = [e for e in valid if e.expected_title]
    kw_cases = [e for e in valid if e.expected_keywords]
    outputs = [e.output for e in valid if e.output is not None]
    return {
        "cases": len(evals),
        "error_cases": len(evals) - len(valid),
        "exact_match_rate": _safe_div(
            sum(1 for e in exact_cases if e.exact_hit), len(exact_cases)
        ),
        "exact_match_cases": len(exact_cases),
        "keyword_hit_rate": _safe_div(
            sum(1 for e in kw_cases if e.keyword_hit), len(kw_cases)
        ),
        "keyword_hit_cases": len(kw_cases),
        "avg_len": _safe_div(sum(len(o) for o in outputs), len(outputs)),
        "too_long_count": sum(1 for e in valid if e.too_long),
        "fallback_count": sum(1 for e in valid if e.output is None),
    }
