"""会话内同话题片段合并阶段插件（slot=post_insert）。

run（存储锁内，由骨架持有锁）：新卡（batch.inserted）与 DB 中同会话
同类别、msg_time 窗口内的未核实卡经 AI 判官逐对判定，同一话题折入
最早头卡（key_info/quote/图片并集去重；多时间点集合化合并）；晚卡
delete_items(keep_raw_messages=True) 删除、合并后重拟标题；去重缓存
同步走 ctx.dedup 服务端口（dedup 插件未启用时跳过）。
候选来自 DB 查询而非当前批次，实时批=1 与回填批都能跨批合并。
"""

import json
import logging
from typing import Any

from briefdesk.config import config
from briefdesk.db import delete_items, get_merge_candidates, update_item_merged
from briefdesk.masking import normalize_subject
from briefdesk.plugin.base import PluginContext, StagePlugin
from briefdesk.plugins.merge.engine import (
    _merge_image_urls,
    _merge_key_info,
    _merge_quote,
    _merge_time_points,
    _parse_extra_json,
    judge_merge,
    summarize_title,
)
from briefdesk.types import BatchContext, MergeCard, MergeCheck, MergeTitleCheck

logger = logging.getLogger(__name__)


def _images_from_db(raw: object) -> list[str]:
    """DB 的 image_urls（JSON 数组字符串或空串）→ 字符串列表（解析失败返回空）。"""
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(u) for u in raw if u]
    try:
        data = json.loads(raw) if isinstance(raw, str) else None
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    return [str(u) for u in data if u]


class MergePlugin(StagePlugin):
    """同话题合并阶段插件（显式实现 StagePlugin；入口见模块底部 `plugin` 实例）。"""

    name = "merge"
    version = "1.0.0"
    dependencies: tuple[str, ...] = (
        "dedup",
        "ai_provider",  # 判官/重拟标题依赖 AI 供应商
    )
    slot = "post_insert"
    priority = 0

    async def setup(self, ctx: PluginContext) -> None:
        ctx.register_stage(self)

    async def activate(self, ctx: PluginContext) -> None: ...

    async def teardown(self) -> None: ...

    async def run(self, batch: BatchContext, ctx: PluginContext) -> None:
        if config.merge_window_minutes <= 0 or not batch.inserted:
            return
        merged = 0
        window_seconds = config.merge_window_minutes * 60
        for row in batch.inserted:
            candidates = await get_merge_candidates(
                source=row.msg.source,
                session_id=row.msg.session_id,
                category=row.result.category,
                around_ts=row.msg.timestamp,
                window_seconds=window_seconds,
                exclude_ids=[row.item_id],
                limit=config.merge_max_candidates,
            )
            for cand in candidates:
                # 已设提醒的卡不参与合并：吸收它会丢用户提醒，保守跳过
                if cand.get("remind_at"):
                    continue
                cand_desc = " ".join(
                    x
                    for x in (cand["title"], cand["key_info"], cand["source_quote"])
                    if x
                )
                new_desc = " ".join(
                    x
                    for x in (row.title, row.result.key_info, row.result.quote)
                    if x
                )
                same = await judge_merge(
                    cand["title"] or "", cand_desc, row.title, new_desc
                )
                if same is None:
                    # 判官失败：保守不合并；失败不构成判定依据，不记录观察数据
                    continue
                mcheck = MergeCheck(
                    same=same,
                    head=MergeCard(
                        title=cand["title"] or "",
                        desc=cand_desc,
                        key_info=cand["key_info"] or "",
                        subject=cand["subject"] or "",
                        sender_name=cand.get("sender_name") or "",
                        session_id=cand.get("session_id") or "",
                        group_name=cand.get("source_group") or "",
                        msg_time=cand.get("msg_time") or 0,
                        source=cand.get("source") or "",
                        msg_id=cand.get("source_msg_id") or cand["id"],
                        source_quote=cand["source_quote"] or "",
                        image_urls=_images_from_db(cand.get("image_urls") or ""),
                    ),
                    tail=MergeCard(
                        title=row.title,
                        desc=new_desc,
                        key_info=row.result.key_info or "",
                        subject=normalize_subject(row.result.subject) or "",
                        sender_name=row.msg.sender_name,
                        session_id=row.msg.session_id,
                        group_name=row.msg.group_name,
                        msg_time=row.msg.timestamp,
                        source=row.msg.source,
                        msg_id=row.msg.msg_id,
                        source_quote=row.msg.content,
                        image_urls=list(row.msg.image_urls),
                    ),
                )
                batch.merge_checks.append(mcheck)
                if not same:
                    continue
                # 存活卡 = msg_time 最早的话题头卡（保证同一话题收敛到同一张卡）
                survive: dict[str, Any]
                absorb: dict[str, Any]
                if cand["msg_time"] <= row.msg.timestamp:
                    survive = {
                        "id": cand["id"],
                        "source": cand["source"],
                        "title": cand["title"],
                        "key_info": cand["key_info"] or "",
                        "quote": cand["source_quote"] or "",
                        "subject": cand["subject"] or "",
                        "start": cand["start"] or "",
                        "end": cand["end"] or "",
                        "extra_times": cand.get("extra_times") or "",
                        "msg_time": cand["msg_time"],
                        "image_urls": cand["image_urls"] or "",
                        "article_url": cand.get("article_url") or "",
                    }
                    absorb = {
                        "id": row.item_id,
                        "title": row.title,
                        "key_info": row.result.key_info or "",
                        "quote": row.msg.content,
                        "subject": normalize_subject(row.result.subject) or "",
                        "start": row.result.start or "",
                        "end": row.result.end or "",
                        "extra_times": row.result.extra_times,
                        "msg_time": row.msg.timestamp,
                        "image_urls": (
                            json.dumps(row.msg.image_urls)
                            if row.msg.image_urls
                            else ""
                        ),
                        "article_url": row.msg.article_url or "",
                    }
                else:
                    survive = {
                        "id": row.item_id,
                        "source": row.msg.source,
                        "title": row.title,
                        "key_info": row.result.key_info or "",
                        "quote": row.msg.content,
                        "subject": normalize_subject(row.result.subject) or "",
                        "start": row.result.start or "",
                        "end": row.result.end or "",
                        "extra_times": row.result.extra_times,
                        "msg_time": row.msg.timestamp,
                        "image_urls": (
                            json.dumps(row.msg.image_urls)
                            if row.msg.image_urls
                            else ""
                        ),
                        "article_url": row.msg.article_url or "",
                    }
                    absorb = {
                        "id": cand["id"],
                        "title": cand["title"],
                        "key_info": cand["key_info"] or "",
                        "quote": cand["source_quote"] or "",
                        "subject": cand["subject"] or "",
                        "start": cand["start"] or "",
                        "end": cand["end"] or "",
                        "extra_times": cand.get("extra_times") or "",
                        "msg_time": cand["msg_time"],
                        "image_urls": cand["image_urls"] or "",
                        "article_url": cand.get("article_url") or "",
                    }
                merged_quote = _merge_quote([survive["quote"], absorb["quote"]])
                merged_title = survive["title"]
                merged_subject = survive["subject"] or absorb["subject"]
                # 多时间点集合化合并：主值取每类最早，其余全部进结构化 extra_times，
                # 供卡片徽章与日历逐点渲染，一个不丢
                merged_start, merged_end, merged_extra = _merge_time_points(
                    [
                        ("start", survive["start"]),
                        ("start", absorb["start"]),
                        ("end", survive["end"]),
                        ("end", absorb["end"]),
                    ],
                    _parse_extra_json(survive["extra_times"])
                    + _parse_extra_json(absorb["extra_times"]),
                )
                merged_key = _merge_key_info(
                    [survive["key_info"], absorb["key_info"]]
                )
                merged_msg_time = min(survive["msg_time"], absorb["msg_time"])
                # 合并后重拟标题：卡片内容已变，头句未必概括合并后的完整话题；
                # 重拟失败（None）保守回退原标题
                merged_title = await summarize_title(
                    merged_title, merged_key, merged_quote
                ) or merged_title
                # 重拟标题观察记录（期望关键词由观察方从 key_info 拆分——来自
                # 分类阶段而非本判官，可作独立 ground truth）
                mcheck.title = MergeTitleCheck(
                    old_title=survive["title"],
                    key_info=merged_key,
                    quote=merged_quote,
                )
                merged_images = _merge_image_urls(
                    [survive["image_urls"], absorb["image_urls"]]
                )
                await update_item_merged(
                    survive["id"],
                    title=merged_title,
                    key_info=merged_key,
                    source_quote=merged_quote,
                    subject=merged_subject,
                    start=merged_start,
                    end=merged_end,
                    msg_time=merged_msg_time,
                    image_urls=merged_images,
                    extra_times=json.dumps(merged_extra) if merged_extra else "",
                    # 原文链接合并：存活卡优先，缺失则继承被吸收卡——文章卡片
                    # 拆条的同话题多卡合并后「原文链接」不从卡片上消失
                    article_url=survive["article_url"] or absorb["article_url"],
                )
                # 保留被吸收片段的 raw 行：它们仍属该对话上下文（/api/context 引用）
                await delete_items([absorb["id"]], keep_raw_messages=True)
                # 去重缓存同步：删两张、按合并后文本重加存活卡（带合并后图片集合
                # 与源名，保持图片精确短路可用）；未带向量（不参与余弦候选），
                # 重启后由缓存加载补齐
                if ctx.dedup is not None:
                    ctx.dedup.remove_items([survive["id"], absorb["id"]])
                    try:
                        merged_imgs = json.loads(merged_images) if merged_images else None
                    except json.JSONDecodeError:
                        merged_imgs = None
                    ctx.dedup.add_to_cache(
                        survive["id"],
                        merged_title,
                        image_urls=merged_imgs,
                        source=survive["source"],
                        source_quote=merged_quote,
                    )
                    # 补嵌请求锁内登记、after_run 锁外消化（复核 P2-20）：
                    # 合并后文本未嵌入，存活卡否则退出余弦候选直到重启
                    batch.reembed_queue.append(
                        (survive["id"], merged_title, merged_quote,
                         merged_imgs, survive["source"])
                    )
                logger.info(
                    '会话合并: "%s" ← 吸收 "%s" (%s)',
                    merged_title,
                    absorb["title"],
                    row.msg.group_name,
                )
                merged += 1
                break  # 每张新卡最多并入一张头卡
        batch.merged = merged

    async def after_run(self, batch: BatchContext, ctx: PluginContext) -> None:
        """锁外：对合并后的存活卡补嵌入（网络调用不得在存储锁内）。

        run 的 add_to_cache 未带向量（合并后文本尚未嵌入）；此处补嵌后带
        向量重新登记，存活卡立即回归余弦候选集，不必等重启重预热（复核
        P2-20）。嵌入失败静默——不劣于旧行为（重启后由缓存加载补齐）。
        add_to_cache 幂等更新为全字段覆盖，须带上 run 时登记的完整参数，
        否则图片短路/来源字段会被默认值清空。
        """
        if not batch.reembed_queue or ctx.dedup is None:
            return
        from briefdesk.ai_ports import embed_texts  # 延迟：依赖 ai_provider 插件
        from briefdesk.plugins.dedup.engine import _embedding_text

        try:
            vectors = await embed_texts(
                [
                    _embedding_text(title, quote)
                    for _item_id, title, quote, _imgs, _src in batch.reembed_queue
                ]
            )
        except Exception:
            logger.debug("merge: 存活卡补嵌失败，重启后由缓存加载补齐", exc_info=True)
            return
        if len(vectors) != len(batch.reembed_queue):
            logger.warning(
                "merge: 补嵌返回数不符（%d/%d），跳过本轮补嵌",
                len(vectors),
                len(batch.reembed_queue),
            )
            return
        for (item_id, title, quote, images, source), vec in zip(
            batch.reembed_queue, vectors
        ):
            ctx.dedup.add_to_cache(
                item_id,
                title,
                embedding=vec,
                image_urls=images,
                source=source,
                source_quote=quote,
            )


plugin = MergePlugin()
