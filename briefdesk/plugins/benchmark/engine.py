"""基准运行编排 — 用例装载 + 与生产同引擎执行 + 结果 payload。

用例来源：
- 文件数据集（CLI / 示例）：schema.load_dataset_file + parse_cases；
- 网页导出用例（Web）：cases/<feature>.fromweb.json，load_web_cases 加载。

执行与生产同引擎路径：classify_batch / DedupEngine.check_dedup /
judge_merge / summarize_title，经 briefdesk.ai_ports 端口调用真实供应商；
运行环境由**调用方**负责：runner 子进程经 providers.prepare_scratch 备好自己的
scratch 库与 AI 端口；CLI 手工运行同样自建临时库（见 cli.run_benchmark）。
"""

from __future__ import annotations

import asyncio
import dataclasses
import functools
import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from briefdesk import ai_ports, telemetry
from briefdesk.config import config
from briefdesk.db import ItemText, delete_items, get_item_texts_by_ids, insert_item
from briefdesk.plugins.benchmark.metrics import (
    BinaryCaseEval,
    ClassifyCaseEval,
    TitleCaseEval,
    aggregate_binary,
    aggregate_classify,
    aggregate_dedup,
    aggregate_title,
    evaluate_classify_case,
    title_char_similarity,
)
from briefdesk.plugins.benchmark.schema import (
    BaseCase,
    CategoryDef,
    ClassifyCase,
    DatasetFile,
    DedupCase,
    MergeCase,
    TitleCase,
    card_fields,
    conflicting_dedup_case_ids,
    load_dataset_file,
    parse_cases,
    shared_dedup_item_keys,
)
from briefdesk.plugins.classify.engine import classify_batch
from briefdesk.plugins.dedup.engine import (
    CachedItem,
    DedupEngine,
    _embedding_text,
    build_item_input,
)
from briefdesk.plugins.merge.engine import judge_merge, summarize_title
from briefdesk.types import ClassifyResult

logger = logging.getLogger(__name__)

FEATURES: tuple[str, ...] = ("classify", "dedup", "merge", "title")

FROMWEB_SOURCE_LABEL = "cases/*.fromweb.json"  # 网页导出用例的 dataset 标识（报告/JSON 溯源）

# 基准判官调用的遥测源维度**恒为这个值**，不用例自带 source：网页导出的用例会
# 保留导出时那条卡片的真实源（weflow/qqflow），拿它当维度会让基准的调用次数与
# 耗时混进生产桶——「基准流量单独成桶」就落空了，而基准正是要反复跑、批量跑的那一侧。
_TELEMETRY_SOURCE = "bench"


def provider_label(api_base: str) -> str:
    """端点的脱敏归类：只输出 `ollama` / `deepseek` / `custom`，空配置输出空串。

    为什么报告需要这一位：同一个模型名打到本机 ollama 与云端 API 的结果不可互比，
    而报告此前只记 `model`——两轮看起来同配置、实为不同供应商，事后无从分辨，
    回归对比就在比较两件不同的事。曾经直接记 `ai_api_base` 又走得太远：完整 URL
    含主机名与端口（内网自建服务时就是内网拓扑），而报告 JSON 会长期留在用户目录、
    也可能被贴进 issue，属隐私面。枚举是这两者之间唯一站得住的取舍。

    解析用 `urlsplit`：无 scheme 的写法（`localhost:11434`）会被当成
    scheme='localhost'、hostname 为 None → 归 `custom`。不猜、不补 scheme——
    补出来的归类会让读者误信「这就是本机 ollama」。
    """
    if not api_base:
        return ""
    try:
        parsed = urlsplit(api_base)
        port = parsed.port
    except ValueError:
        # 端口越界等畸形 URL：降级 custom，绝不让基准因为一行配置而整轮失败
        return "custom"
    host = (parsed.hostname or "").lower()
    if port == 11434 or "ollama" in host:
        return "ollama"
    if "deepseek" in host:
        return "deepseek"
    return "custom"


def benchmark_config_snapshot() -> dict[str, Any]:
    """返回不含密钥和 API 地址的实验配置快照。"""
    return {
        "max_concurrency": config.ai_max_concurrency,
        "reasoning_effort": config.ai_reasoning_effort,
        "json_mode": config.ai_json_mode,
        "max_classify_tokens": config.max_classify_tokens,
        "vision_enabled": config.ai_vision_enabled,
        "vision_max_images": config.ai_vision_max_images,
        "embedding_enabled": bool(config.embed_api_base),
        "embedding_model": config.embed_model,
        "embedding_batch_size": config.embed_batch_size,
        # 供应商归类（脱敏）：与 embedding_enabled 布尔并排看，才能区分
        # 「本机 bge-m3」与「云端嵌入服务」——两者同名模型、不同判重表现
        "chat_provider": provider_label(config.ai_api_base),
        "embed_provider": provider_label(config.embed_api_base),
        "dedup_similarity_threshold": config.dedup_similarity_threshold,
        "dedup_embed_threshold": config.dedup_embed_threshold,
        "dedup_embed_top_k": config.dedup_embed_top_k,
        "dedup_embed_fallback_threshold": config.dedup_embed_fallback_threshold,
        "dedup_strong_threshold": config.dedup_strong_threshold,
        "merge_prefilter_threshold": config.merge_prefilter_threshold,
    }


# ── 用例运行器 ──


@dataclasses.dataclass(frozen=True)
class CaseProgress:
    """单条用例完成事件（评估进度回调负载）。

    done 含失败用例；调用方可按 done % N 节流输出（CLI 每 5 条一行）。
    """

    feature: str
    done: int
    total: int
    failed: int
    case_id: str | None = None


class _ProbeDedupEngine(DedupEngine):
    """基准判重探针：把缓存可见集合收窄到本用例，并记录实际判定通道。

    三件事都由探针做，不改生产语义：

    - **作用域**（`_load_cache_rows`）：每例新建引擎、但库是共享的 scratch 库，
      默认接缝加载**全部历史行**——含此前所有用例插入的 items。实测一轮真实
      基准：116 个进入 AI 的用例里只有 18 例命中自己的 item，其余 98 例命中
      别人的卡片，外来候选直接贡献 14 例错判。这不是"多测了一点"，是在测另
      一件事。
    - **AI 调用计数**（`_ask_ai`）：`ai_calls == 0` 表示候选门直接跳过判定。
    - **通道与余弦健康度**（`_exact_shortcut` / `_select_candidates`）：配置写了
      `EMBED_API_BASE` 不代表这轮真走了余弦——基准历史上从不传 `q_emb`，整轮
      都在重叠通道上跑，报告却显示"嵌入开启"。只有逐例记下实际通道，这类测量
      链路缺陷才自证得了。

    覆写私有方法是**刻意**取舍：通道信息只在候选选取那一刻存在，抬成生产 API
    （`DedupResult.channel`）会牵动 types 契约与五个判定出口——为一个观测需求
    改动判定链路更不值。本类已有 `_ask_ai` 覆写先例。
    """

    def __init__(self, item_ids: list[str] | None = None) -> None:
        super().__init__()
        self.ai_calls = 0
        self.channel = ""
        self.cosine_ran = False
        self._scope_ids = list(item_ids or [])

    async def _ask_ai(self, a: CachedItem, b_title: str, b_quote: str) -> bool | None:
        self.ai_calls += 1
        return await super()._ask_ai(a, b_title, b_quote)

    async def _load_cache_rows(self) -> list[ItemText]:
        """只取本用例的 items（未给作用域时回退生产的全量取数）。"""
        if not self._scope_ids:
            return await super()._load_cache_rows()
        return await get_item_texts_by_ids(self._scope_ids)

    def _exact_shortcut(
        self,
        image_urls: list[str] | None,
        source: str,
        source_quote: str,
    ) -> tuple[CachedItem, str] | None:
        hit = super()._exact_shortcut(image_urls, source, source_quote)
        if hit is not None:
            self.channel = "exact"
        return hit

    def _select_candidates(
        self, title: str, q_emb: list[float] | None
    ) -> tuple[list[tuple[CachedItem, float]], str, bool]:
        candidates, metric, cosine_ran = super()._select_candidates(title, q_emb)
        self.cosine_ran = cosine_ran
        # 零候选时不能用 metric 反推通道：生产在「余弦零候选 → 重叠兜底命中」
        # 那一支会把 metric 改写回 overlap，"余弦其实跑过"的事实就丢了（同一
        # 陷阱见 _select_candidates 里公告撤销判据的注释）。
        if candidates:
            self.channel = metric
        elif cosine_ran:
            self.channel = "cosine_no_candidate"
        else:
            self.channel = "none"
        return candidates, metric, cosine_ran


async def _run_classify(case: ClassifyCase) -> ClassifyCaseEval:
    messages = [m.to_internal() for m in case.messages]
    outcome = await classify_batch(messages)
    return evaluate_classify_case(case, outcome)


async def _run_dedup(case: DedupCase, *, recycle: bool = True) -> BinaryCaseEval:
    # 与真实管道同路径：items 先作为"已有卡片"入库（dedup 缓存预热从库加载），
    # 再对 query 做候选预筛 + AI 判重。类别对判重无影响，统一用默认值。
    item_ids: list[str] = []
    try:
        # 插入也在 try 内：中途抛异常时已插入的行照样要回收——异常由 _guarded 收成
        # error 用例、不向上冒泡，这一步不删就会一路留到整轮结束
        for item in case.items:
            msg = item.to_internal()
            title, _content = card_fields(item)
            item_ids.append(
                await insert_item(
                    build_item_input(
                        msg, ClassifyResult(msg_index=0, category="活动通知"), title
                    )
                )
            )
        engine = _ProbeDedupEngine(item_ids)
        # 预热是拿到 q_emb 的前提，不是性能优化：preembed_batch 首行判
        # _embed_cache_ok，缓存未加载时它恒返回 None——省掉这一步，基准会在
        # 「配置了嵌入、实际全程走字符重叠」的假象下跑完整轮（正是那个历史缺陷
        # 的另一半）。生产不需显式预热，因为 dedup 插件 setup 已在服务启动前做过。
        await engine.ensure_cache()
        q_title, q_content = card_fields(case.query)
        # 与生产 before_run 同口径：锁外批量预嵌，未启用/失败回退重叠通道。绝不
        # 在 check_dedup 内补嵌入——存储锁内禁远程嵌入是生产约束，不为基准放宽。
        embeddings = await engine.preembed_batch([(q_title, q_content)])
        q_emb = embeddings[0] if embeddings else None
        result = await engine.check_dedup(
            q_title,
            source_group=case.query.group_name or "bench",
            q_emb=q_emb,
            source_quote=q_content,
            image_urls=list(case.query.image_urls) or None,
            source=case.query.source,
        )
        return BinaryCaseEval(
            case_id=case.id,
            predicted=result.is_duplicate,
            expected=case.expected.same,
            skipped=engine.ai_calls == 0,
            channel=engine.channel,
            cosine_ran=engine.cosine_ran,
        )
    finally:
        # 回收只为控制 scratch 库体积：不回收则每例 ensure_cache 都要把全表向量
        # 读一遍（load_embeddings 没有按 id 过滤的入口），用例越多越慢。
        # 但**共享卡片时一律不删**——两条用例的 items 用同一 (source, msg_id) 时
        # insert_item 的冲突分支会让两边拿到同一个行 id，谁先跑完谁删，另一条还
        # 没判的用例就取到空缓存、静默判成「无候选」：作用域没错，是被别人拆了台。
        # 调用方按数据集算出 recycle（见 _run_feature），键全唯一才回收。
        # 删在 finally：异常用例也不能把残行留给下一例。
        if recycle:
            await delete_items(item_ids)


async def _run_merge(case: MergeCase) -> BinaryCaseEval:
    head_title, head_content = card_fields(case.head)
    tail_title, tail_content = card_fields(case.tail)
    # 余弦预筛门与生产同口径：嵌入启用时对两侧判官输入文本嵌入后算余弦
    # （生产走去重缓存向量，同一 `_embedding_text` 产出），低于阈值的用例
    # 直接判不合并、不经 AI——预筛是判定行为的一部分，跳过的用例按
    # predicted=False 计入混淆矩阵（预筛漏真合并会打低 F1，硬门如实反映）；
    # skipped 仅作提示计数。嵌入未启用/失败 → 预筛跳过，全部用例走判官
    # （与生产「向量缺失不预筛」同语义）。
    if config.merge_prefilter_threshold >= 0 and ai_ports.is_embedding_enabled():
        vectors: list[list[float]] | None = None
        try:
            vectors = await ai_ports.embed_texts(
                [
                    _embedding_text(head_title, head_content),
                    _embedding_text(tail_title, tail_content),
                ]
            )
        except Exception:
            logger.debug("merge 基准预筛嵌入失败，本用例跳过预筛", exc_info=True)
        if vectors is not None and len(vectors) == 2:
            # 余弦计算与生产 `_candidate_cosine` 同口径地容错：坏向量（维度
            # 不一致、含非数值）会让 cosine_similarity 抛异常。生产路径此时
            # 降级为「不预筛、照常调判官」，基准必须镜像同一行为——否则供应
            # 商返回异常向量时整个 merge benchmark 中止，而这恰恰是最需要
            # 对照数据的情形（回归见 tests/test_benchmark_prefilter.py）。
            try:
                cos = ai_ports.cosine_similarity(vectors[0], vectors[1])
            except Exception:
                logger.debug("merge 基准预筛余弦计算失败，本用例跳过预筛", exc_info=True)
            else:
                if cos < config.merge_prefilter_threshold:
                    telemetry.record("merge.prefilter_skip")
                    return BinaryCaseEval(
                        case_id=case.id,
                        predicted=False,
                        expected=case.expected.merge,
                        skipped=True,
                    )
    merged = await judge_merge(
        head_title,
        head_content,
        tail_title,
        tail_content,
        # 基准流量单独成桶：用例可能带着导出时的真实源，恒记 bench 才不与生产混
        source=_TELEMETRY_SOURCE,
    )
    if merged is None:
        return BinaryCaseEval(
            case_id=case.id,
            predicted=False,
            expected=case.expected.merge,
            error="合并判官失败（无判定结论）",
        )
    return BinaryCaseEval(
        case_id=case.id,
        predicted=merged,
        expected=case.expected.merge,
    )


async def _run_title(case: TitleCase) -> TitleCaseEval:
    title, _desc = card_fields(case.message)
    old_title = case.old_title or title
    key_info = case.key_info or case.message.key_info
    output = await summarize_title(
        old_title,
        key_info,
        case.message.content,
        source=_TELEMETRY_SOURCE,
    )
    return TitleCaseEval(
        case_id=case.id,
        output=output,
        expected_title=case.expected.title,
        expected_keywords=case.expected.keywords or [],
        char_similarity=title_char_similarity(output, case.expected.title),
    )


_RUNNERS: dict[str, Any] = {
    "classify": _run_classify,
    "dedup": _run_dedup,
    "merge": _run_merge,
    "title": _run_title,
}


def _error_eval(feature: str, case: BaseCase, err: Exception) -> Any:
    """用例运行失败时的占位评估记录（不拖垮整轮基准）。"""
    message = f"{type(err).__name__}: {err}"
    if feature == "classify":
        assert isinstance(case, ClassifyCase)
        return ClassifyCaseEval(
            case_id=case.id, messages_total=len(case.messages), error=message
        )
    if feature == "title":
        assert isinstance(case, TitleCase)
        return TitleCaseEval(case_id=case.id, error=message)
    assert isinstance(case, (DedupCase, MergeCase))
    return BinaryCaseEval(
        case_id=case.id, predicted=False, expected=False, error=message
    )


async def _run_feature(
    feature: str,
    cases: list[BaseCase],
    concurrency: int,
    progress: Callable[[CaseProgress], None] | None = None,
) -> list[Any]:
    sem = asyncio.Semaphore(max(1, concurrency))
    total = len(cases)
    done = 0
    failed = 0

    runner: Any = _RUNNERS[feature]
    if feature == "dedup":
        # 回收策略按**整批**用例算，不能按单条：共享卡片的危害发生在两条用例之间
        # （先跑完的那条删掉两边共用的行），单条用例看不出自己是不是共享方。
        recycle = not shared_dedup_item_keys(cases)
        if not recycle:
            logger.info(
                "dedup 用例跨例共享历史卡片，本轮不回收 scratch 行"
                "（避免先跑完的用例把共用卡片删掉、让后跑的用例取到空缓存）"
            )
        runner = functools.partial(runner, recycle=recycle)

    async def _guarded(worker: Any, case: BaseCase) -> Any:
        nonlocal done, failed
        async with sem:
            try:
                ev = await worker(case)
            except Exception as e:  # noqa: BLE001 — 单条失败不拖垮整轮
                logger.warning("用例 %s 运行失败: %s", case.id, e)
                failed += 1
                ev = _error_eval(feature, case, e)
        # 计数与回调间无 await 点，事件循环内原子，done 严格递增
        done += 1
        if progress is not None:
            progress(
                CaseProgress(
                    feature=feature, done=done, total=total, failed=failed, case_id=case.id
                )
            )
        return ev

    return await asyncio.gather(*[_guarded(runner, c) for c in cases])


def _aggregate(feature: str, evals: list[Any]) -> dict[str, Any]:
    """按功能聚合。返回类型放宽：classify 的 summary 含嵌套的 by_mode 表。"""
    if feature == "classify":
        return aggregate_classify(evals)  # type: ignore[arg-type]
    if feature == "title":
        return aggregate_title(evals)  # type: ignore[arg-type]
    if feature == "dedup":
        # dedup 走自己的聚合：除二元指标外还要通道分布与 AI 调用成本。与 merge
        # 分开的另一个理由是 merge 的摘要不该长出一堆恒为 0 的 channel_* 键。
        return aggregate_dedup(evals)  # type: ignore[arg-type]
    return aggregate_binary(evals)  # type: ignore[arg-type]


# ── 用例装载 ──


def load_file_cases(path: str | Path) -> list[BaseCase]:
    """从文件数据集加载并校验用例（顶层结构 + 逐条 schema）。"""
    dataset = load_dataset_file(path)
    return parse_cases(dataset)


def load_file_dataset(path: str | Path) -> DatasetFile:
    """加载文件数据集顶层结构（含 categories 声明，供类别覆盖）。"""
    return load_dataset_file(path)


async def load_web_cases(
    feature: str, cases_dir: str | Path | None = None
) -> list[BaseCase]:
    """从 cases/<feature>.fromweb.json（网页导出）加载某功能全部用例。

    逐条校验，非法项跳过并告警；文件缺失/不可读返回空列表（见 store.list_fromweb）。
    cases_dir 覆盖包内默认目录，见 store.fromweb_path。
    """
    from briefdesk.plugins.benchmark import store as bench_store

    raw_cases = await bench_store.list_fromweb(
        feature, None if cases_dir is None else Path(cases_dir)
    )
    cases, errors = bench_store.parse_cases_with_errors(feature, raw_cases)
    for err in errors:
        logger.warning("网页导出用例跳过: %s", err)
    # 同键不同卡的 items 会让判重的缓存作用域指向别人的卡片。文件数据集在
    # parse_cases 里整批拒绝（用户可改文件重跑）；网页导出是逐条容错路径，整轮
    # 失败不是这里该有的行为，但"只打 WARNING 然后照跑"更糟——冲突例的 channel
    # 照样正常记 overlap/cosine，报告看不出这条结果是错的。两侧期望在这种数据下
    # 都不可信，故把双方都剔出本轮：用例数明显减少 + WARNING 点名 id，比留一批
    # 看起来正常、实际错判的结果诚实。
    conflicts = conflicting_dedup_case_ids(cases)
    if conflicts:
        ids = sorted(conflicts)
        logger.warning(
            "网页导出判重用例存在同键不同卡（items 唯一键 (source, msg_id) 撞车），"
            "剔除 %d 条不参与本轮: %s",
            len(ids),
            ", ".join(ids),
        )
        cases = [c for c in cases if c.id not in conflicts]
    return cases


# ── 运行主入口 ──


async def run_benchmark_cases(
    cases_by_feature: dict[str, list[BaseCase]],
    category_defs: list[CategoryDef] | None = None,
    *,
    concurrency: int = 1,
    dataset_label: str | dict[str, str] = FROMWEB_SOURCE_LABEL,
    progress: Callable[[CaseProgress], None] | None = None,
    run_id: str | None = None,
) -> tuple[dict[str, Any], dict[str, list[Any]]]:
    """在基准环境内运行指定功能用例。

    返回 (payload, evals_by_feature)：payload 与 JSON/HTML 报告同构
    （case 明细为 dict）；evals 为逐用例评估对象（CLI 终端渲染用）。
    dataset_label 标识用例来源（Web 运行为 cases/*.fromweb.json；
    CLI 传各数据集文件路径），写入报告供溯源。
    progress（可选）：每条用例 settle 后回调一次（成功/失败都算），
    CLI 据此输出评估进度；缺省无进度输出。
    run_id：结果标识，缺省落回本地时间戳（父进程给定时以它为准）。
    """
    started = time.monotonic()

    async def _run_all() -> tuple[
        dict[str, dict[str, Any]], dict[str, list[Any]], dict[str, float]
    ]:
        # summary 值类型放宽到 Any：classify 的 summary 里带嵌套的 by_mode 表
        summaries: dict[str, dict[str, Any]] = {}
        evals_by_feature: dict[str, list[Any]] = {}
        elapsed: dict[str, float] = {}
        for feature, cases in cases_by_feature.items():
            if not cases:
                continue
            f_start = time.monotonic()
            evals = await _run_feature(feature, cases, concurrency, progress=progress)
            elapsed[feature] = round(time.monotonic() - f_start, 3)
            summaries[feature] = _aggregate(feature, evals)
            evals_by_feature[feature] = evals
        return summaries, evals_by_feature, elapsed

    summaries, evals_by_feature, elapsed = await _run_all()
    payload = _build_payload(
        summaries,
        evals_by_feature,
        concurrency,
        dataset_label,
        elapsed=elapsed,
        total_elapsed=round(time.monotonic() - started, 3),
        run_id=run_id,
    )
    return payload, evals_by_feature


def _build_payload(
    summaries: dict[str, dict[str, Any]],
    evals_by_feature: dict[str, list[Any]],
    concurrency: int,
    dataset_label: str | dict[str, str] = FROMWEB_SOURCE_LABEL,
    *,
    elapsed: dict[str, float] | None = None,
    total_elapsed: float | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    """结果 payload（run_id/模型信息/配置快照/逐功能明细/测试用时）。

    elapsed_sec 为墙钟耗时（秒）：顶层 = 整轮总用时；features[f] 内 =
    该功能用例执行用时（不含聚合）。
    """

    def _label(feature: str) -> str:
        if isinstance(dataset_label, dict):
            return dataset_label.get(feature) or FROMWEB_SOURCE_LABEL
        return dataset_label

    return {
        # run_id 由调用方给定时以它为准（父进程生成 → POST 响应、目录名与报告三处同源）；
        # 缺省（手工/独立运行）才落回本地时间戳。
        "run_id": run_id or time.strftime("%Y%m%d-%H%M%S"),
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "model": config.ai_model,
        # 推理强度与模型、端点同级记录：同一模型在不同强度下的结果不可互比，
        # 报告必须自带这一项，否则事后无法判断某次运行到底发了什么参数
        "reasoning_effort": config.ai_reasoning_effort,
        "config": benchmark_config_snapshot(),
        "concurrency": max(1, concurrency),
        "elapsed_sec": total_elapsed,
        "features": {
            f: {
                "dataset": _label(f),
                "elapsed_sec": (elapsed or {}).get(f),
                "summary": summaries.get(f, {}),
                "cases": [dataclasses.asdict(e) for e in evals_by_feature.get(f, [])],
            }
            for f in summaries
        },
    }
