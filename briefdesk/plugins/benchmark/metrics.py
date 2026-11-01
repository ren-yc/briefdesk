"""基准指标计算 — 纯函数（不调用 AI / 不访问 DB），可独立单测。

每个功能：per-case 评估记录（runner 产出）→ 聚合指标（report 消费）。

新增字段一律带默认值：报告 payload 会长期落盘，旧运行目录的 JSON 必须仍能渲染
（缺字段显示「-」），不能因为加了指标就让历史报告打不开。
"""

from __future__ import annotations

import statistics
import time
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any

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
    skipped: bool = False  # dedup 候选门禁 / merge 余弦预筛门：未触发 AI 判定（非 AI 结论）
    # dedup 实际执行的判定通道（exact/cosine/overlap/cosine_no_candidate/none）；
    # merge 不用通道概念，留空。配置的 EMBED_API_BASE 与真实通道不一致时，
    # 只有逐例记下的通道能暴露「以为在测语义判重、其实全程走字符重叠」。
    channel: str = ""
    # dedup：本次候选选取的余弦链路是否真的跑通（维度一致且算出分数）。
    # 与 channel 分开记：余弦零候选后被重叠兜底接走的用例，channel 是 overlap，
    # 但嵌入链路其实是健康的——只看 channel 会把两者混为一谈。
    cosine_ran: bool = False
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


# dedup 判定通道的全部取值（与 _ProbeDedupEngine 的赋值守域一致；聚合与报告
# 都遍历这份清单，避免两处各自抄一遍漏掉一个通道就少算一类用例）
DEDUP_CHANNELS: tuple[str, ...] = (
    "exact",  # 图片/原文哈希精确短路（零 AI）
    "cosine",  # 余弦候选（含强短路/多数票/弱复核）
    "overlap",  # 字符重叠候选（嵌入不可用或余弦零候选后兜底命中）
    "cosine_no_candidate",  # 余弦跑通但零候选 → 直接判不重复
    "none",  # 候选为空且余弦未跑通（缓存空/无向量/重叠全零）
)


def aggregate_dedup(evals: list[BinaryCaseEval]) -> dict[str, float | int]:
    """dedup 聚合 = 二元指标 + 实际通道分布 + AI 判定成本。

    为什么把通道与成本摊开写：候选门是判定行为的一部分，调低门禁阈值会让
    进入判官的用例成倍增加。报告不写清楚，读者就无法把「召回低」归因给判官
    还是门禁，也看不见放宽门禁换来的 AI 调用代价。
    通道分布还兼作自证——配置了嵌入却整轮 `overlap`，说明测量链路本身有缺陷
    （历史缺陷：基准从不传 q_emb）。
    """
    base: dict[str, float | int] = dict(aggregate_binary(evals))
    valid = [e for e in evals if e.error is None]
    for channel in DEDUP_CHANNELS:
        base[f"channel_{channel}"] = sum(1 for e in valid if e.channel == channel)
    base["cosine_cases"] = sum(1 for e in valid if e.cosine_ran)
    # 命名取「判定用例数」而不是「调用次数」：一例多数票会对多个候选各调一次判官
    # （上限 DEDUP_EMBED_TOP_K），真实 API 调用数可以远大于本数。写成 calls 会被
    # 读成成本上限而低估阈值放宽的代价。
    judged = sum(1 for e in valid if not e.skipped)
    base["ai_judged_cases"] = judged
    base["ai_judged_rate"] = _safe_div(judged, len(valid))
    return base


# ── classify：类别 + 时间提取 + 健壮性 ──


@dataclass
class ClassifyCaseEval:
    """单批分类用例的评估记录（指标分子分母全部保留，聚合时再汇总）。"""

    case_id: str
    messages_total: int = 0
    # 切片维度：类别指标的解释力取决于「这批测的是不是生产分布」，故逐例带上
    mode: str = ""  # 已解析的用例 mode（prod/oov/mix/fictional/single/real/unspecified）
    # 全部消息正文都是附件占位符 → 结构性不可能被分类，从主指标剔除、单独计数
    placeholder_only: bool = False
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
    # 时间点的回声/粒度拆分：回声点（期望值就是消息自己的发送时间）不是从正文
    # 抽取出来的，留在分母里量的是「模型会不会复读时间戳」，不是解析能力
    time_points_echo: int = 0  # 回声点数
    time_points_ex_echo: int = 0  # 非回声期望点数
    time_points_ex_echo_ok: int = 0
    time_points_date_ex_echo: int = 0  # 非回声且 date-only
    time_points_date_ex_echo_ok: int = 0
    time_points_clock_ex_echo: int = 0  # 非回声且带时刻
    time_points_clock_ex_echo_ok: int = 0
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


def _is_date_only(point: str) -> bool:
    """期望时间点是否 date-only（只有 `YYYY-MM-DD`，没有时刻）。"""
    return len(point.strip()) == 10


# 回声比较的精度表：期望点字符串长度 → strftime 格式。长度即精度，故直接按
# 长度取格式（10=日、16=分钟、19=秒），与 `MessageIn._coerce_timestamp` 接受的
# 三种录入形态一一对应；表里没有的长度按"不是回声"处理（见 _is_echo）。
_ECHO_FORMATS: dict[int, str] = {
    10: "%Y-%m-%d",
    16: "%Y-%m-%d %H:%M",
    19: "%Y-%m-%d %H:%M:%S",
}


def _is_echo(point: str, msg_ts: int) -> bool:
    """期望时间点是否「回声」——值就是这条消息自己的发送时间。

    为什么要单独剔出来：标注者从手机/网页端复制消息时，界面显示的时间常被顺手
    当成活动/截止时间填进期望。这类点不是从正文抽出来的，模型再怎么强也"抽"不出
    正文里不存在的时间；留在分母里量到的是「模型会不会复读消息时间戳」，而不是
    时间解析能力，指标会被结构性压低（当前数据集 232 个期望点里 63 个是回声）。

    比较按**期望点自身的精度**：`YYYY-MM-DD` 比到日、`YYYY-MM-DD HH:MM` 比到分钟、
    带秒的比到秒。不能一律截到 16 字符——那样 `19:00:30` 会被 `19:00` 的发送时间
    判成回声，而带秒的期望点是一次真实的精确抽取，不该被剔出分母。其它长度
    （非三种标准形态）一律不算回声：宁可留在分母里当漏检，也不静默剔掉真样本。

    epoch 按**本地时区**格式化——与 `MessageIn` 的字符串录入口径、与 classify 发给
    模型的发送时刻标注同源；按 UTC 比会把跨零点的消息判错方向。

    `msg_ts <= 0`（缺省/非法时间戳）不算回声：1970-01-01 不是锚点，拿它当基准会
    把真正该进分母的抽取点误剔成回声。
    """
    if not isinstance(msg_ts, int) or msg_ts <= 0:
        return False
    text = point.strip()
    fmt = _ECHO_FORMATS.get(len(text))
    if fmt is None:
        return False
    try:
        local = time.strftime(fmt, time.localtime(msg_ts))
    except (OSError, OverflowError, ValueError):
        return False
    return text == local


def evaluate_classify_case(case: ClassifyCase, outcome: ClassifyOutcome) -> ClassifyCaseEval:
    """把一次 classify_batch 结果与期望比对，产出评估记录。

    - 类别命中：结果存在且 category 与期望一致（期望外输出计为误报）；
    - 时间点：只比较 (type, time)，忽略 label（AI 对任务名的措辞是模糊的）；
      "完全一致"要求模型时间点集合与期望集合精确相等（期望应写全）。
    - 逐点分类出回声 / date-only / 带时刻三个维度并保留分子分母，聚合口径
      （剔不剔回声、按粒度分组）留给聚合决定，本函数不预先做取舍。
    """
    ev = ClassifyCaseEval(
        case_id=case.id,
        messages_total=len(case.messages),
        mode=case.resolved_mode(),
        placeholder_only=case.placeholder_only(),
    )
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
        hit_pts = exp_pts & model_pts
        ev.time_points_ok += len(hit_pts)
        ev.time_points_model += len(model_pts)
        if model_pts == exp_pts:
            ev.time_msgs_ok += 1
        msg_ts = (
            case.messages[idx].timestamp if 0 <= idx < len(case.messages) else 0
        )
        for key in exp_pts:
            point = key[1]
            if _is_echo(point, msg_ts):
                ev.time_points_echo += 1
                continue
            hit = key in hit_pts
            ev.time_points_ex_echo += 1
            if hit:
                ev.time_points_ex_echo_ok += 1
            if _is_date_only(point):
                ev.time_points_date_ex_echo += 1
                if hit:
                    ev.time_points_date_ex_echo_ok += 1
            else:
                ev.time_points_clock_ex_echo += 1
                if hit:
                    ev.time_points_clock_ex_echo_ok += 1

    ev.failed = len(outcome.failed)
    ev.summary_total = len(outcome.results)
    ev.summary_filled = sum(1 for r in outcome.results if (r.summary or "").strip())
    return ev


def aggregate_classify(evals: list[ClassifyCaseEval]) -> dict[str, Any]:
    """聚合分类指标（各用例分子分母求和后再算比率）。

    返回类型放宽到 `dict[str, Any]`：`by_mode` 是嵌套表（每 mode 一整个小字典），
    装不进「值只能是 float | int」的签名——为它另开一个返回通道会让报告层要认
    两种 summary 形状。其余功能仍返回扁平表，消费方按 `.get` 取值即可。

    两条口径调整（都为了让数字可解释）：

    - **纯占位符用例不进主指标**：正文全是 `[图片]` 一类占位符的消息在生产里
      根本进不了分类阶段，把它们的期望算进分母会得到一个结构性不可能通过的项，
      扣分与模型能力无关。它们单独计数（`placeholder_only_*`）并在报告里点名，
      另给一条 `category_recall_incl_placeholder` 供与历史报告对比。
    - **按 mode 切片**：词表外样本与生产样本混算时，总分既不代表能力也不代表
      上限（实测同一轮里 oov 与 prod 相差五十个百分点）。切片只统计非占位符、
      非出错用例，保证 `sum(by_mode[*].expected) == expected` 这条自证恒成立。

    时间指标同时给两口径：`time_point_recall` 等旧键保持原定义（含回声点，与
    历史报告可比），`*_ex_echo` 系列剔掉回声点——后者才是时间解析能力的读数。
    """
    valid = [e for e in evals if e.error is None]
    scored = [e for e in valid if not e.placeholder_only]
    placeholder = [e for e in valid if e.placeholder_only]

    expected = sum(e.expected_count for e in scored)
    hits = sum(e.hits for e in scored)
    model = sum(e.model_count for e in scored)
    precision = _safe_div(hits, model)
    recall = _safe_div(hits, expected)
    msgs_expected = sum(e.time_msgs_expected for e in scored)
    msgs_ok = sum(e.time_msgs_ok for e in scored)
    pts_expected = sum(e.time_points_expected for e in scored)
    pts_ok = sum(e.time_points_ok for e in scored)
    pts_model = sum(e.time_points_model for e in scored)
    pts_echo = sum(e.time_points_echo for e in scored)
    pts_ex = sum(e.time_points_ex_echo for e in scored)
    pts_ex_ok = sum(e.time_points_ex_echo_ok for e in scored)
    date_ex = sum(e.time_points_date_ex_echo for e in scored)
    date_ex_ok = sum(e.time_points_date_ex_echo_ok for e in scored)
    clock_ex = sum(e.time_points_clock_ex_echo for e in scored)
    clock_ex_ok = sum(e.time_points_clock_ex_echo_ok for e in scored)
    msgs_total = sum(e.messages_total for e in scored)
    failed = sum(e.failed for e in scored)
    summary_total = sum(e.summary_total for e in scored)
    summary_filled = sum(e.summary_filled for e in scored)
    fp_total = sum(e.false_positives for e in scored)
    # 误报拆两类：全噪声用例（期望为空）里的误报 = 把闲聊当信息；部分期望用例
    # 里的误报 = 同批多分了别条消息。两者的修法完全不同，混在一起就只是扣分。
    fp_noise = sum(e.false_positives for e in scored if e.expected_count == 0)
    by_mode: dict[str, dict[str, float | int]] = {}
    for mode in sorted({e.mode or "unspecified" for e in scored}):
        group = [e for e in scored if (e.mode or "unspecified") == mode]
        g_expected = sum(e.expected_count for e in group)
        g_hits = sum(e.hits for e in group)
        g_model = sum(e.model_count for e in group)
        g_precision = _safe_div(g_hits, g_model)
        g_recall = _safe_div(g_hits, g_expected)
        by_mode[mode] = {
            "cases": len(group),
            "expected": g_expected,
            "hits": g_hits,
            "model": g_model,
            "false_positives": sum(e.false_positives for e in group),
            "category_recall": g_recall,
            "category_precision": g_precision,
            "category_f1": _safe_div(2 * g_precision * g_recall, g_precision + g_recall),
        }
    ph_expected = sum(e.expected_count for e in placeholder)
    ph_hits = sum(e.hits for e in placeholder)
    return {
        "cases": len(evals),
        "error_cases": len(evals) - len(valid),
        "expected": expected,
        "expected_incl_placeholder": expected + ph_expected,
        "model_classified": model,
        "category_accuracy": recall,  # 期望集合上的命中率（含漏检惩罚）
        "category_precision": precision,
        "category_recall": recall,
        "category_f1": _safe_div(2 * precision * recall, precision + recall),
        "false_positives": fp_total,
        "false_positives_noise": fp_noise,
        "false_positives_partial": fp_total - fp_noise,
        "placeholder_only_cases": len(placeholder),
        "placeholder_only_expected": ph_expected,
        "category_recall_incl_placeholder": _safe_div(
            hits + ph_hits, expected + ph_expected
        ),
        "by_mode": by_mode,
        "time_msg_accuracy": _safe_div(msgs_ok, msgs_expected),
        "time_point_recall": _safe_div(pts_ok, pts_expected),
        "time_point_precision": _safe_div(pts_ok, pts_model),
        "time_points_echo": pts_echo,
        "time_point_recall_ex_echo": _safe_div(pts_ex_ok, pts_ex),
        "time_point_recall_date_ex_echo": _safe_div(date_ex_ok, date_ex),
        "time_point_recall_clock_ex_echo": _safe_div(clock_ex_ok, clock_ex),
        "failure_rate": _safe_div(failed, msgs_total),
        "summary_fill_rate": _safe_div(summary_filled, summary_total),
    }


# ── title：标题重拟 ──


# 标题"软通过"线：字符相似度阈值。
# 这是**描述线不是门槛**——取在当前数据集的中位数附近（实测 median 0.762、
# mean 0.741、≥0.70 占 41/63），用途是把"输出与期望标题大致同形"与"跑题"分开
# 显示，不给验收提供依据。标题是开放生成，真正能作门槛的是关键词命中。
# 导出（不带下划线）：终端与 HTML 两处渲染都要判同一条线，各写一个数必然漂移。
TITLE_SOFT_RATIO = 0.70


def title_char_similarity(output: str | None, expected_title: str | None) -> float | None:
    """输出与期望标题的字符相似度（difflib ratio，0~1）；任一为空 → None。

    为什么用序列比对而不是 bigram Jaccard：这里比的是"两段标题像不像"，
    需要容忍局部增删改（difflib 按最长公共匹配计分），而去重通道的 bigram
    Jaccard 是为短中文标题的**召回门禁**调的，两处目的不同、不必强行统一。
    """
    if output is None or not expected_title:
        return None
    return SequenceMatcher(None, output, expected_title).ratio()


@dataclass
class TitleCaseEval:
    """单条标题重拟用例的评估记录。"""

    case_id: str
    output: str | None = None  # 模型输出；None = 回退原标题
    expected_title: str | None = None  # 精确期望（保留供明细展示）
    expected_keywords: list[str] = field(default_factory=list)  # 包含匹配期望（可选）
    # 由 runner 填的派生分（不是聚合时才算）：payload 用 dataclasses.asdict 落盘，
    # 属性（@property）不会被 asdict 收进去，报告明细就拿不到逐例相似度。
    char_similarity: float | None = None
    error: str | None = None

    @property
    def keyword_hit(self) -> bool:
        if self.output is None or not self.expected_keywords:
            return False
        return all(k in self.output for k in self.expected_keywords)

    @property
    def soft_hit(self) -> bool:
        """无关键词期望时的软通过：字符相似度达描述线。

        精确匹配整轮只有 1/63 命中——那是"模型会不会逐字照抄"，不是标题质量。
        只有标题期望的用例没有可验证的硬标准，故只给软判定并显式标注，不参与
        任何门槛。有关键词期望时以关键词为准（软判不越过硬判）。
        """
        if self.expected_keywords:
            return False
        return self.char_similarity is not None and self.char_similarity >= TITLE_SOFT_RATIO

    @property
    def too_long(self) -> bool:
        """标题长度合规：> 30 字视为违规（与 TITLE_PROMPT 的约束一致）。"""
        return self.output is not None and len(self.output) > 30


def aggregate_title(evals: list[TitleCaseEval]) -> dict[str, float | int]:
    """标题指标：关键词命中率（硬）+ 长度合规 + 字符相似（软，描述用）。

    为什么撤掉精确匹配率：标题是开放生成，同一件事有无数种写法，逐字相等衡量的是
    抄写而非质量（当前数据集 63 例只有 1 例精确命中，读者却会把它当"模型差"）。
    字符相似度作为替代的**软**指标：能区分"接近期望"与"跑题"，但不承担验收。
    """
    valid = [e for e in evals if e.error is None]
    kw_cases = [e for e in valid if e.expected_keywords]
    outputs = [e.output for e in valid if e.output is not None]
    sims = [
        e.char_similarity
        for e in valid
        if e.char_similarity is not None
    ]
    title_only = [e for e in valid if e.expected_title and not e.expected_keywords]
    return {
        "cases": len(evals),
        "error_cases": len(evals) - len(valid),
        "keyword_hit_rate": _safe_div(
            sum(1 for e in kw_cases if e.keyword_hit), len(kw_cases)
        ),
        "keyword_hit_cases": len(kw_cases),
        # 软指标三件：分母、均值、中位数。中位数比均值更能代表"典型有多像"
        # （跑题的长尾会把均值拽下去），两者并报才看得出分布形状。
        "char_similarity_cases": len(sims),
        "char_similarity_mean": _safe_div(sum(sims), len(sims)),
        "char_similarity_p50": statistics.median(sims) if sims else 0.0,
        "title_only_cases": len(title_only),
        "title_only_soft_pass": sum(1 for e in title_only if e.soft_hit),
        "avg_len": _safe_div(sum(len(o) for o in outputs), len(outputs)),
        "too_long_count": sum(1 for e in valid if e.too_long),
        "fallback_count": sum(1 for e in valid if e.output is None),
    }
