"""类别分配器 — 实例锚定的零训练分类（影子/灰度组件，阈值待校准）。

设计契约（实例锚定）：
- 类别指纹 = **原型向量集**（每类近 M 天 Top-N 样本逐条嵌入），表达实例
  分布而非规则文本——DEFAULT_CATEGORIES 的 prompt 是规则程序（含互斥排除
  句），余弦不建模否定/前置条件，拿 prompt 当指纹会随启用类别数增长而
  恶化。
- 分配 = 消息向量对每类原型取 max / mean-of-top-k 余弦，达到该类 p5 分位
  阈值才接受；多类同时达标取最高分。逐类阈值（而非全局单一值）缓解大类
  先验偏置。
- recat 覆盖层：recat_log 纠错样本按 category_after 计入新类原型、从原类
  剔除同源 content（防同一文本双锚震荡），权重高于普通样本、只纠分布不
  改阈值。
- 冷启动：某类无样本（新类别）→ 该类不参与分配；**全部类别无样本** →
  分配器整体不可用（返回 None，调用方回退 API 链路）——新类别冷启动自动
  回退，样本累积后重建即接管。

本模块为「就绪 + 待校准」组件：阈值参数经 scripts/calibrate_allocator.py
（留一校准）用 recat_log 数据定值前，allocate 始终可用但只建议在影子模式
消费。嵌入经 ai_ports（EMBED_API_BASE 未启用时 allocate 返回 None 降级）。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from briefdesk import ai_ports, telemetry

logger = logging.getLogger(__name__)

# 原型采样参数（设计契约的保守起点：N≈50–200、M 单独标定）。默认值即「未
# 校准前的保守起点」，校准脚本输出建议后经 THRESHOLD_OVERRIDES / 重建参数覆盖。
PROTOTYPE_TOP_N = 100
PROTOTYPE_WINDOW_DAYS = 90
# mean-of-top-k 的 k：max 对单条噪声原型敏感、全均值被多数平庸样本稀释，
# 取 top-k 均值兼顾（k=3 对 50–200 的原型集是设计契约量级）
_MEAN_TOP_K = 3
# 逐类接受阈值默认值：p5 分位的替换起点。**不是拍脑袋的终值**——校准脚本
# 按每类期望得分与竞争类得分分布输出建议（p25 与竞争类 p75 取大）后回填
# THRESHOLD_OVERRIDES；未校准时本默认值只保证「同文本必过」的最低正确性。
DEFAULT_P5_THRESHOLD = 0.60
# 逐类阈值覆盖（key=类别名）：由校准结论回填，空 = 全用默认值。
THRESHOLD_OVERRIDES: dict[str, float] = {}

# 重建最小间隔（秒）：改类/改名是瞬时操作，连续重建会重复嵌入同一批样本
# （AI 成本）。间隔内的重建请求合并为一次延迟重建标志。
_REBUILD_MIN_INTERVAL = 300.0


@dataclass
class CategoryPrototypes:
    """单类原型集：样本向量 + 逐类接受阈值 + 样本身份（留一校准用）。"""

    category: str
    vectors: list[list[float]] = field(default_factory=list)
    threshold: float = DEFAULT_P5_THRESHOLD
    # 样本身份（content 哈希），留一校准时按身份剔除
    member_keys: list[str] = field(default_factory=list)


@dataclass
class AllocationResult:
    """分配结果：category=None 表示无类达标（调用方回退 API 链路）。"""

    category: str | None
    score: float
    runner_up: str = ""
    runner_up_score: float = 0.0


class CategoryAllocator:
    """类别分配器：原型集缓存 + 逐类阈值 + recat 覆盖层。

    实例归插件所有（setup 构造）：与去重引擎同款生命周期——重建缓存从
    DB 取样本经 ai_ports 嵌入（网络调用，禁在存储锁内调用 allocate 之外的
    重建路径；分配本身纯内存）。
    """

    def __init__(self) -> None:
        self._protos: dict[str, CategoryPrototypes] = {}
        self._rebuilt_at = 0.0
        self._rebuild_pending = False

    # ── 重建 ──

    async def rebuild(
        self,
        samples: list[dict],
        *,
        force: bool = False,
    ) -> int:
        """重建原型缓存（全量替换语义）。

        samples 形状：{"category", "content", "weight"(可选, recat=2)}——
        由调用方（插件 rebuild）从 items 高频样本 + recat_log 纠错样本合成，
        本模块不触 DB（纯计算 + 嵌入，可单测）。force=True 跳过节流
        （校准/测试用）。
        返回嵌入的原型条数（0 = 无样本，缓存清空 → 分配器不可用；
        -1 = 节流合并；-2 = 嵌入失败保留旧缓存）。

        节流合并语义：-1 只表示「本次请求被并入待重建」，**调用方必须保证
        兑现**——事件触发路径拿到 -1 后须等剩余间隔耗尽再重试（见插件的
        _on_categories_changed），否则改名重建会静默丢失（pending 标志只在
        下一次 rebuild 调用到来自行兑现）。
        """
        now = time.monotonic()
        if (
            not force
            and self._rebuilt_at
            and now - self._rebuilt_at < _REBUILD_MIN_INTERVAL
            and not self._rebuild_pending
        ):
            self._rebuild_pending = True
            logger.info("分配器重建节流中（间隔 %ds），本次请求合并为待重建", _REBUILD_MIN_INTERVAL)
            return -1
        # 节流窗口只在**成功**路径上膛（见下方各 return 前的重置）：若在这里
        # 就记时，嵌入失败返回 -2 时窗口已被占用，接下来的纠错事件只能拿到
        # -1 等下一次 force 重试——旧/空缓存最长滞留 5 分钟，与「下个触发点
        # 重试」的承诺相反。失败路径回滚到上一次成功时间（首次失败置 0），
        # 让下一次调用立即重试而非被节流吞掉。
        previous_rebuilt_at = self._rebuilt_at
        self._rebuild_pending = False

        if not is_available():
            logger.info("嵌入未启用，分配器不重建（allocate 将回退）")
            self._protos = {}
            self._rebuilt_at = now
            return 0
        if not samples:
            logger.info("无分类样本（items 为空），分配器置空（冷启动回退 API）")
            self._protos = {}
            self._rebuilt_at = now
            return 0

        # content 去重：recat 覆盖层「计入新类 + 从原类剔除同源 content」
        # 落地为——同 content 出现在多类时只保留 weight 最高的一条
        by_content: dict[str, dict] = {}
        for s in samples:
            key = str(s.get("content", ""))
            if not key:
                continue
            prev = by_content.get(key)
            if prev is None or float(s.get("weight", 1)) > float(prev.get("weight", 1)):
                by_content[key] = s
        deduped = list(by_content.values())

        texts = [str(s["content"])[:2000] for s in deduped]
        try:
            vectors = await ai_ports.embed_texts(texts)
        except Exception:
            # 嵌入失败：保留旧缓存（旧分布优于空缓存），回滚节流窗口让下个
            # 触发点立即重试（不占 300s 窗口，见函数开头注释）
            self._rebuilt_at = previous_rebuilt_at
            logger.warning("分配器原型嵌入失败，保留旧缓存", exc_info=True)
            return -2
        if len(vectors) != len(deduped):
            self._rebuilt_at = previous_rebuilt_at
            logger.warning(
                "分配器嵌入返回数不符（%d/%d），保留旧缓存",
                len(vectors),
                len(deduped),
            )
            return -2

        buckets: dict[str, CategoryPrototypes] = {}
        for s, vec in zip(deduped, vectors):
            cat = str(s.get("category", ""))
            if not cat:
                continue
            proto = buckets.setdefault(cat, CategoryPrototypes(category=cat))
            proto.vectors.append(vec)
            proto.member_keys.append(_content_key(str(s["content"])))
        # 逐类阈值：p5 覆盖 > 类内自相似 p5 > 全局默认。类内自相似 p5 需要
        # O(n²) 两两余弦（50–200 样本可接受），是「该类样本间相似度分布」
        # 的直接度量——样本彼此都不像的类，接受阈自然应更低。
        for cat, proto in buckets.items():
            overrides = dict(THRESHOLD_OVERRIDES)
            override = overrides.get(cat)
            if override is not None:
                proto.threshold = override
            elif len(proto.vectors) >= 2:
                proto.threshold = _p5_self_similarity(proto.vectors)
        self._protos = buckets
        self._rebuilt_at = now  # 节流窗口只在成功路径上膛
        telemetry.record("allocator.rebuild")
        logger.info(
            "分配器重建完成：%d 类 / %d 原型", len(buckets), len(deduped)
        )
        return len(deduped)

    # ── 分配 ──

    async def allocate_with_vector(
        self, content: str, qvec: list[float] | None = None
    ) -> AllocationResult | None:
        """分配主入口：qvec 缺失时锁外现嵌（调用方须在存储锁外调用）。

        缓存空 / 嵌入未启用 / 嵌入失败 → None（调用方回退 API 链路）。
        """
        protos = self._protos
        if not protos:
            return None
        if qvec is None:
            if not is_available():
                return None
            try:
                vecs = await ai_ports.embed_texts([content[:2000]])
            except Exception:
                logger.debug("分配器查询嵌入失败，回退 API", exc_info=True)
                return None
            if not vecs:
                return None
            qvec = vecs[0]

        # 逐类独立过阈 → 候选集内取最高分（契约语义：每类各自达到自己的
        # p5 阈值才接受）。不能先取全局最高分再用冠军类的阈值裁决全局：
        # 高自相似类的阈值会系统性压制低阈值类（冠军 0.9999 阈不过、次高
        # 类 0.51 分远超其 0.0 阈的正确命中被整体否决 → 大量假 None 回退）。
        qualified: list[tuple[str, float]] = []
        all_scores: list[tuple[str, float]] = []
        for cat, proto in protos.items():
            if not proto.vectors:
                continue
            score = _score_against(qvec, proto.vectors)
            all_scores.append((cat, score))
            if score >= proto.threshold:
                qualified.append((cat, score))

        telemetry.record("allocator.allocate")
        all_scores.sort(key=lambda t: t[1], reverse=True)
        if not qualified:
            telemetry.record("allocator.unmatched")
            best = all_scores[0] if all_scores else ("", -1.0)
            runner = all_scores[1] if len(all_scores) > 1 else ("", 0.0)
            return AllocationResult(
                category=None,
                score=best[1],
                runner_up=runner[0],
                runner_up_score=runner[1],
            )
        qualified.sort(key=lambda t: t[1], reverse=True)
        best_cat, best_score = qualified[0]
        # runner_up 取全体评分中的次高（含未达标类：调用方需要知道「差一点
        # 的第二名是谁」做回退诊断，而不只是达标者内部的次名）
        runner = next(
            ((cat, s) for cat, s in all_scores if cat != best_cat),
            ("", 0.0),
        )
        return AllocationResult(
            category=best_cat,
            score=best_score,
            runner_up=runner[0],
            runner_up_score=runner[1],
        )

    # ── 状态 ──

    def stats(self) -> dict:
        """缓存状态（设置页/诊断）：类数、各类原型数与阈值。"""
        return {
            "categories": {
                cat: {
                    "prototypes": len(p.vectors),
                    "threshold": round(p.threshold, 4),
                }
                for cat, p in sorted(self._protos.items())
            },
            "total_prototypes": sum(len(p.vectors) for p in self._protos.values()),
            "rebuilt_pending": self._rebuild_pending,
        }


def _score_against(qvec: list[float], vectors: list[list[float]]) -> float:
    """max / mean-of-top-k 聚合（k=_MEAN_TOP_K；原型少于 k 时退化为均值）。"""
    sims = sorted(
        (ai_ports.cosine_similarity(qvec, v) for v in vectors), reverse=True
    )
    top = sims[:_MEAN_TOP_K]
    if not top:
        return 0.0
    if len(vectors) <= _MEAN_TOP_K:
        return sum(top) / len(top)
    # max 与 mean-of-top-k 取较大者：单条强命中（消息确实属于该类的一条
    # 细分场景）不应被其余平庸原型稀释
    return max(top[0], sum(top) / len(top))


def _p5_self_similarity(vectors: list[list[float]]) -> float:
    """类内两两余弦的 p5 分位（该类「样本间相似度分布」的低尾）。

    留一校准前的在线估计：p5 是分布的保守下尾——用低于它的阈值接受几乎
    不会放进比类内样本彼此更不像的消息。
    """
    sims = [
        ai_ports.cosine_similarity(vectors[i], vectors[j])
        for i in range(len(vectors))
        for j in range(i + 1, len(vectors))
    ]
    if not sims:
        return DEFAULT_P5_THRESHOLD
    sims.sort()
    rank = (len(sims) - 1) * 0.05
    low = int(rank)
    high = min(low + 1, len(sims) - 1)
    frac = rank - low
    p5 = sims[low] * (1 - frac) + sims[high] * frac
    return max(0.0, min(1.0, p5))


def _content_key(content: str) -> str:
    import hashlib

    return hashlib.sha256(content.encode()).hexdigest()[:16]


def is_available() -> bool:
    """分配器可用性 = 嵌入启用（向量能力是分配器的地基）。"""
    return ai_ports.is_embedding_enabled()


# 模块级单例（与 config 同风格）：改名/改类/定期触发点经 rebuild 刷新。
allocator = CategoryAllocator()
