"""基准用例记录器（benchmark StagePlugin 的采集与导出）。

网页「导出当前列表」只能从**卡片最终状态**推导用例：去重命中/合并命中
会把证据（重复卡/被折叠卡）从库里删掉，所以 dedup 的 same=true 只能
看到"去重漏判"的共存对、merge=true 只能看到"未合并"的共存对——真实的
正向判定（判重命中、合并命中）永远观察不到。

本模块随 benchmark 插件注册为**阶段插件**（slot=post_insert，priority=1），
在真实管道处理时点采集 dedup/merge 阶段写入 BatchContext 的判定观察记录
（briefdesk.types 的 DedupCheck/MergeCheck），累积在内存（记录开关默认关闭，
经 /api/benchmark/record 打开），导出时逐功能覆盖写入
cases/<feature>.fromweb.json（复用 store.export_fromweb 的校验与原子写）。

- 记录的是"判定时点"的事实：dedup 命中的 (query, 命中候选) → same=true；
  候选被判定为不同的 (query, 最高分候选) → same=false；合并判定命中/未命中
  的 (head, tail) 对 → merge=true/false；合并后重拟标题事件 → title 用例
  （期望关键词来自合并后 key_info——分类阶段产物，非标题判官自身输出）；
- 内存上限 _MAX_CASES_PER_FEATURE（超限丢弃并告警，防无界增长）；
- 导出即清空累积器（避免重复导出同一批记录）；导出失败保留记录。
"""

from __future__ import annotations

import logging
import time
from typing import Any

from briefdesk.plugins.benchmark.schema import FEATURES
from briefdesk.plugins.benchmark.store import _split_keywords, new_case_id
from briefdesk.types import BatchContext, DedupCheck, MergeCard, MergeCheck

logger = logging.getLogger(__name__)

# 每功能内存上限：记录超限时丢弃并告警（本地应用内存态，防无界增长）
_MAX_CASES_PER_FEATURE = 5000

_enabled = False
_cases: dict[str, list[dict[str, Any]]] = {f: [] for f in FEATURES}
# 超限告警的降噪闸门（按功能各记一次）：达上限后每条记录都会命中，
# 每条一行 WARNING 会刷屏，而首条已足够说明"该功能的样本不再增长"。
# 按功能分别记——各功能的上限是独立触发的。
_cap_warned: set[str] = set()


def reset() -> None:
    """复位记录器（测试隔离用）：关闭记录并清空累积。"""
    global _enabled
    _enabled = False
    for f in FEATURES:
        _cases[f] = []
    _cap_warned.clear()


def set_enabled(enabled: bool) -> None:
    """开关记录（运行期；累积内容不随开关丢失）。"""
    global _enabled
    _enabled = bool(enabled)


def is_enabled() -> bool:
    return _enabled


def _add(feature: str, case: dict[str, Any]) -> None:
    bucket = _cases[feature]
    if len(bucket) >= _MAX_CASES_PER_FEATURE:
        if feature not in _cap_warned:
            _cap_warned.add(feature)
            logger.warning(
                "基准记录已达上限（%d 条/%s），后续记录不再入库（本告警仅首次）",
                _MAX_CASES_PER_FEATURE,
                feature,
            )
        else:
            logger.debug("基准记录已达上限（%s），丢弃新记录", feature)
        return
    bucket.append(case)


def stats() -> dict[str, Any]:
    """记录状态：开关 + 各功能累积数。"""
    counts = {f: len(_cases[f]) for f in FEATURES}
    return {"enabled": _enabled, "counts": counts, "total": sum(counts.values())}


def clear() -> int:
    """丢弃全部累积记录；返回丢弃条数。"""
    total = sum(len(c) for c in _cases.values())
    for f in FEATURES:
        _cases[f] = []
    return total


# ── 判定观察记录 → 用例主体 ──


def _message_dict(
    *,
    msg_id: str,
    content: str,
    title: str | None,
    key_info: str = "",
    sender_name: str = "",
    sender_id: str = "",
    session_id: str = "",
    group_name: str = "",
    timestamp: int = 0,
    source: str = "",
    image_urls: list[str] | None = None,
    article_url: str = "",
) -> dict[str, Any]:
    """构造 MessageIn 形状的输入字典（与 InternalMessage 字段对齐 + title/key_info 扩展）。"""
    return {
        "msg_id": msg_id,
        "content": content,
        "title": title,
        "key_info": key_info,
        "sender_name": sender_name,
        "sender_id": sender_id,
        "session_id": session_id,
        "group_name": group_name,
        "timestamp": timestamp,
        "source": source,
        "image_urls": list(image_urls or []),
        "article_url": article_url,
    }


def build_dedup_case(check: DedupCheck) -> dict[str, Any] | None:
    """DedupCheck → dedup 用例主体；无实际比较（candidate 为空）返回 None。

    items = 判定时点的对照候选（命中 = 被并入条目；未命中 = 最高分候选），
    query = 被判定的新消息，expected = 判定结论（期望 = 管道判定本身）。
    """
    if check.candidate is None:
        return None
    cand = check.candidate
    msg = check.msg
    return {
        "id": new_case_id(),
        "note": "由处理记录导出："
        + ("判重命中 → 同一信息" if check.is_duplicate else "候选判定不同 → 不同信息"),
        "items": [
            _message_dict(
                msg_id=cand.item_id,
                content=cand.source_quote or cand.title or "",
                title=cand.title,
                source=cand.source,
                image_urls=cand.image_urls,
            )
        ],
        "query": _message_dict(
            msg_id=msg.msg_id,
            content=msg.content,
            title=check.title,
            sender_name=msg.sender_name,
            sender_id=msg.sender_id,
            session_id=msg.session_id,
            group_name=msg.group_name,
            timestamp=msg.timestamp,
            source=msg.source,
            image_urls=msg.image_urls,
            article_url=msg.article_url,
        ),
        "expected": {"same": check.is_duplicate},
    }


def _merge_card_message(card: MergeCard) -> dict[str, Any]:
    """MergeCard → MessageIn 形状：content 用判官实际看到的完整描述（可复现判定）。"""
    return _message_dict(
        msg_id=card.msg_id,
        content=card.desc,
        title=card.title,
        key_info=card.key_info,
        sender_name=card.sender_name,
        session_id=card.session_id,
        group_name=card.group_name,
        timestamp=card.msg_time,
        source=card.source,
        image_urls=card.image_urls,
    )


def build_merge_case(check: MergeCheck) -> dict[str, Any]:
    """MergeCheck → merge 用例主体（head/tail 顺序与判官调用一致，期望=判定结论）。"""
    return {
        "id": new_case_id(),
        "note": "由处理记录导出："
        + ("合并判定命中 → 同话题片段" if check.same else "合并判定未命中 → 不同话题"),
        "head": _merge_card_message(check.head),
        "tail": _merge_card_message(check.tail),
        "expected": {"merge": check.same},
    }


def build_title_case(check: MergeCheck) -> dict[str, Any] | None:
    """合并后重拟标题事件 → title 用例主体；无关键信息则无独立期望，返回 None。

    期望 = 合并后 key_info 拆分的关键词（分类阶段产物，非标题判官自身输出），
    与网页导出的 title 用例同口径；无 key_info 时不以标题判官自身输出作期望
    （自指期望无评估意义）。
    """
    evt = check.title
    if evt is None:
        return None
    keywords = _split_keywords(evt.key_info)
    if not keywords:
        return None
    return {
        "id": new_case_id(),
        "note": "由处理记录导出（合并后重拟标题）：期望=关键信息包含",
        "message": _message_dict(
            msg_id=check.tail.msg_id,
            content=evt.quote,
            title=evt.old_title,
            key_info=evt.key_info,
            sender_name=check.tail.sender_name,
            session_id=check.tail.session_id,
            group_name=check.tail.group_name,
            timestamp=check.tail.msg_time,
            source=check.tail.source,
            image_urls=check.tail.image_urls,
        ),
        "old_title": evt.old_title,
        "key_info": evt.key_info,
        "expected": {"keywords": keywords},
    }


def record_batch(batch: BatchContext) -> None:
    """阶段插件 run 入口：把批内判定观察记录构造成用例并累积（锁内，纯内存）。"""
    for dcheck in batch.dedup_checks:
        case = build_dedup_case(dcheck)
        if case is not None:
            _add("dedup", case)
    for mcheck in batch.merge_checks:
        mcase = build_merge_case(mcheck)
        if mcase is not None:
            _add("merge", mcase)
        tcase = build_title_case(mcheck)
        if tcase is not None:
            _add("title", tcase)


# ── 导出 ──


async def export_recorded() -> dict[str, Any]:
    """把累积记录逐功能导出为 cases/<feature>.fromweb.json（覆盖式），随后清空。

    复用 store.export_fromweb 的逐条校验与原子写：任一功能校验失败即抛
    DatasetError（该功能不落盘）；导出成功后清空累积器。
    """
    from briefdesk.plugins.benchmark import store as bench_store

    exported: dict[str, int] = {}
    paths: dict[str, str] = {}
    for feature in FEATURES:
        cases = _cases[feature]
        if not cases:
            continue  # 该功能无记录：保留已有文件
        description = (
            f"网页「导出处理记录为基准用例」于 {time.strftime('%Y-%m-%d %H:%M:%S')}"
            "导出（覆盖式，期望=管道处理时点的判定）"
        )
        path = await bench_store.export_fromweb(feature, cases, description=description)
        exported[feature] = len(cases)
        paths[feature] = str(path)
    clear()
    return {
        "counts": {f: exported.get(f, 0) for f in FEATURES},
        "total": sum(exported.values()),
        "paths": paths,
    }
