"""REST 轮询器 — 通过 WeFlowClient 拉取历史消息。

只产出源无关数据（PollResult），不触碰 DB；写库由应用层（poll_cycle）完成。

窗口模式（按会话，优先级）：
- window_start_by_session 提供该会话下界（秒级时间戳，含边界）：增量轮询，
  只拉 [下界, now]（会话水位 - overlap）；
- 会话在窗口表中缺省（无水位/新启用）：回退 BACKFILL_HOURS 窗口（启用即回填）；
- BACKFILL_HOURS=-1（全量）：不传 start（服务端不限时间），按 offset
  翻页直至 hasMore=False；单会话翻页有守卫上限。
"""

import logging
import time as time_module
from datetime import UTC, datetime

from briefdesk.config import config
from briefdesk.logger import fmt_dur
from briefdesk.masking import clean_display_name
from briefdesk.plugins.weflow.client import (
    ChatLabSession,
    WeFlowClient,
    WeFlowMessage,
    is_official_session,
    is_private_session,
)
from briefdesk.plugins.weflow.normalize import (
    _APPMSG_LOCAL_TYPE,
    _is_appmsg_content,
    normalize_rest,
    parse_appmsg_xml,
    pre_filter_rest,
)
from briefdesk.sources_base import ProcessedQuery
from briefdesk.types import ContactInfo, PollResult, SessionInfo

logger = logging.getLogger(__name__)

# 翻页守卫：单页条数 × 页数上限，防异常状态下的无界循环
# （所有窗口模式共用；与 qqflow poller 一致，全量上限更高）
_PAGE_LIMIT = 500
_MAX_PAGES = 2000


async def poll(
    client: WeFlowClient,
    enabled_sessions: list[SessionInfo],
    is_processed: ProcessedQuery,
    *,
    window_start_by_session: dict[str, int | None] | None = None,
) -> PollResult:
    """执行一次完整的 REST 轮询。

        Args:
            client: WeFlowClient 实例
            enabled_sessions: 应用层传入的已启用会话列表（源不访问 DB）
            is_processed: 应用层提供的已处理查询，用于剔除已处理消息
            window_start_by_session: 应用层按会话计算的增量窗口下界
                （session_id → 秒级时间戳，含边界）；值为 None 的会话按
                BACKFILL_HOURS 回退（-1 = 拉取全部历史）

        Returns:
            PollResult: 包含 messages / sessions / contacts / session_count
    """
    result = PollResult()
    now = datetime.now(UTC)
    pull_all = config.backfill_hours == -1
    backfill_seconds = config.backfill_hours * 60 * 60
    poll_start = time_module.perf_counter()

    if pull_all:
        logger.warning(
            "[weflow] BACKFILL_HOURS=-1：将拉取全部历史消息，每次同步都会全量扫描，"
            "消息量大时非常耗时且 AI 调用量激增；建议全量拉取完成后改回正常小时数"
        )
    backfill_count = 0
    if window_start_by_session is not None:
        backfill_count = sum(
            1
            for s in enabled_sessions
            if window_start_by_session.get(s.session_id) is None
        )
    window_desc = "全量" if pull_all else (
        f"按会话窗口（{backfill_count} 会话回填）" if backfill_count else "按会话窗口"
    )
    logger.info(
        f"[weflow] poll 开始: 窗口 {window_desc}, {len(enabled_sessions)} 个启用会话"
    )

    # 获取联系人（只产出数据，写库由应用层完成）
    # 与 qqflow 对齐：contacts 失败即中止本轮，避免消息以 wxid 显示名
    # 永久入库；异常由 run_poll_cycle 记入 lastError，下一轮可重试。
    try:
        contacts: dict[str, str] = await client.fetch_contacts()
    except Exception as e:
        logger.error(f"[weflow] 拉取联系人失败: {e}")
        raise

    # 获取会话列表（发现会话的唯一途径，写库由应用层完成）。
    # chatlab 格式：type 权威（group/private/channel），默认 limit=100 会
    # 截断会话发现，client 内已显式传大 limit。
    all_sessions: list[ChatLabSession] = []
    try:
        all_sessions = await client.fetch_sessions()
    except Exception as e:
        logger.error(f"Failed to fetch sessions: {e}")
        raise

    # 兜底：私聊/公众号的对端可能不在上游 contacts 集合（数据缺失），
    # 用会话显示名补全映射（仅私聊与公众号；群会话 id 是群号，不能当发送者）。
    # 上游会话显示名同样可能携带脏数据，净化后为空才不兜底。
    for s in all_sessions:
        if (is_private_session(s) or is_official_session(s)) and s.get("id"):
            uid = s["id"]
            if uid not in contacts:
                name = clean_display_name(s.get("name"))
                if name:
                    contacts[uid] = name

    result.contacts = [
        ContactInfo(source=client.name, sender_id=username, display_name=name)
        for username, name in contacts.items()
    ]
    logger.info(f"Loaded {len(contacts)} contacts")

    result.sessions = [
        SessionInfo(
            source=client.name,
            session_id=s["id"],
            name=clean_display_name(s.get("name")) or s["id"],
            is_group=(s.get("type") == "group"),
            is_official=is_official_session(s),
            last_active_at=s.get("lastMessageAt", 0),
        )
        for s in all_sessions
    ]

    result.session_count = len(enabled_sessions)

    if not enabled_sessions:
        logger.info(
            f"No enabled sessions — discovered {len(all_sessions)} total, enable groups in settings"
        )
        return result

    logger.info(
        f"Polling {len(enabled_sessions)} enabled sessions ({len(all_sessions)} total discovered)"
    )

    total_raw = 0
    total_skipped = 0
    total_self = 0  # IGNORE_SELF 预滤的自消息数（不进管道、不标记 processed）
    session_idx = 0
    # IGNORE_SELF 开启时检测 isSend 字段是否可得（微信 DB 原义字段，
    # 个别 WeFlow 版本可能不返回；缺失则自消息过滤实际未生效，需告警）
    saw_is_send_field = False
    # 群成员映射按 session 缓存一轮：同一轮内重复会话不重复请求
    group_members_cache: dict[str, dict[str, str]] = {}

    for session in enabled_sessions:
        session_idx += 1
        session_id = session.session_id
        label = session.name or session_id
        session_start = time_module.perf_counter()
        try:
            if pull_all:
                # 全量拉取：不传 start（服务端不限时间），不做年龄截止
                start_ts: int | None = None
                session_mode = "全量"
            else:
                start_ts = (
                    window_start_by_session.get(session_id)
                    if window_start_by_session is not None
                    else None
                )
                if start_ts is None:
                    # 无水位/新启用会话：回退窗口（启用即回填一次）
                    start_ts = int(now.timestamp()) - backfill_seconds
                    session_mode = "回填"
                else:
                    session_mode = "增量"
            cutoff = start_ts if start_ts is not None else 0
            logger.debug(
                f"  [{session_idx}/{len(enabled_sessions)}] {label}: "
                f"拉取中 ({session_mode}, media=True)"
            )
            # 统一翻页：所有模式都按 offset 递增直至 hasMore=False / 空页，
            # 单会话不再有"每轮最多 500 条"的硬顶；_MAX_PAGES 仅为防异常
            # 状态下的无界循环守卫（全量/异常时打 WARNING）。
            messages: list[WeFlowMessage] = []
            offset = 0
            for page in range(_MAX_PAGES):
                resp = await client.fetch_messages(
                    session_id,
                    start_ts,
                    limit=_PAGE_LIMIT,
                    offset=offset,
                    media=True,
                )
                page_msgs = resp.get("messages", [])
                if not page_msgs:
                    break
                messages.extend(page_msgs)
                offset += len(page_msgs)
                if not resp.get("hasMore"):
                    break
            else:
                logger.warning(
                    f"  [{session_idx}/{len(enabled_sessions)}] {label}: "
                    f"达到翻页守卫上限 {_MAX_PAGES} 页，可能未拉完"
                )
            total_raw += len(messages)

            # 普通消息按 serverId 查已处理；文章卡片按拆条粒度查
            # （msg_id=serverId_1..serverId_n，见 normalize._article_messages），否则
            # 已处理的公众号文章每轮都被当作"新"并重复回查/归一化。
            # content 为占位符（media=True 渲染）时拆条数未知，留待回查
            # XML 后按拆条粒度补查。
            msg_ids: list[str] = []
            article_split_n: dict[str, int] = {}
            for m in messages:
                msg_ids.append(m["serverId"])
                if m.get("localType") == _APPMSG_LOCAL_TYPE:
                    n = len(parse_appmsg_xml(m.get("content", "")))
                    if n:
                        article_split_n[m["serverId"]] = n
                        msg_ids.extend(f"{m['serverId']}_{i}" for i in range(1, n + 1))
            processed_set = await is_processed(msg_ids)

            candidates: list[WeFlowMessage] = []
            seen_server_ids: set[str] = set()  # 翻页期间上游插入 → offset 漂移产生跨页重复
            session_new = 0
            session_skipped = 0
            session_old = 0
            session_processed = 0
            session_self = 0

            for msg in messages:
                if "isSend" in msg:
                    saw_is_send_field = True
                if msg.get("createTime", 0) < cutoff:
                    session_old += 1
                    continue
                if not pre_filter_rest(msg):
                    session_skipped += 1
                    total_skipped += 1
                    continue
                # IGNORE_SELF 预滤：自己发送的消息不进候选（避免每轮重查
                # 已处理/文章回查的无效开销；不标记 processed，关闭开关可恢复）
                if config.ignore_self and msg.get("isSend") == 1:
                    session_self += 1
                    continue

                msg_id = msg["serverId"]
                if msg_id in seen_server_ids:
                    # 同一条消息跨页重复（offset 翻页期间上游插入导致漂移）：
                    # 只保留先到者，避免重复入管道触发唯一键冲突/重复分类
                    logger.debug("重复 serverId 跳过（翻页漂移）: %s", msg_id)
                    continue
                seen_server_ids.add(msg_id)
                if msg_id in processed_set:
                    session_processed += 1
                    continue
                # 文章卡片：拆条全部已处理 → 计"已处理"并跳过；
                # 部分处理时保留为候选，由 pipeline 入口按拆条过滤已处理部分
                split_n = article_split_n.get(msg_id)
                if split_n is not None and all(
                    f"{msg_id}_{i}" in processed_set for i in range(1, split_n + 1)
                ):
                    session_processed += 1
                    continue

                candidates.append(msg)

            group_members: dict[str, str] = {}
            if candidates and session.is_group:
                # 懒加载：仅有新候选时才查群成员；404（群不存在）由 client
                # 降级为空映射，其他错误传播中止本轮，避免错误名永久入库
                if session_id not in group_members_cache:
                    group_members_cache[session_id] = await client.fetch_group_members(
                        session_id
                    )
                else:
                    logger.debug(
                        f"  [{session_idx}/{len(enabled_sessions)}] {label}: 群成员命中本轮缓存"
                    )
                group_members = group_members_cache[session_id]

            for msg in candidates:
                # media=True 回填时 WeFlow 会把文章卡片 XML 渲染成占位符
                # （如 "[视频号] 标题"）；内容非 XML 时回查 media=False 的
                # 原始消息再解析，拿不到则按占位符原文处理（解析失败丢弃）
                if (
                    msg.get("localType") == _APPMSG_LOCAL_TYPE
                    and not _is_appmsg_content(msg.get("content", ""))
                ):
                    raw = await client.fetch_message_raw(
                        session_id, msg["serverId"], msg.get("createTime", 0)
                    )
                    if raw and raw.get("content"):
                        logger.debug(
                            "文章卡片占位符回查成功: %s → %d bytes",
                            msg["serverId"],
                            len(raw["content"]),
                        )
                        msg["content"] = raw["content"]
                    else:
                        logger.debug(
                            "文章卡片占位符回查未命中: %s（按原文处理）",
                            msg["serverId"],
                        )
                # 占位符回查后的文章卡片：拆条全部已处理 → 计"已处理"并跳过
                # （此时才拿到 XML、可知拆条数；部分处理仍走 normalize，
                # 由 pipeline 入口按拆条过滤）
                if msg.get("localType") == _APPMSG_LOCAL_TYPE:
                    articles = parse_appmsg_xml(msg.get("content", ""))
                    if articles:
                        split_ids = [
                            f"{msg['serverId']}_{i}"
                            for i in range(1, len(articles) + 1)
                        ]
                        split_processed = await is_processed(split_ids)
                        if all(pid in split_processed for pid in split_ids):
                            session_processed += 1
                            continue
                # 文章卡片拆条后返回多条；解析失败返回空列表（维持丢弃语义）
                normalized_list = normalize_rest(
                    msg, session_id, label, contacts, group_members
                )
                result.messages.extend(normalized_list)
                session_new += len(normalized_list)

            # 每会话必打 INFO 汇总行（含 0 条），避免"无消息会话静默"造成
            # 轮询提前结束的误读；窗口模式随行标注（增量/回填/全量）。
            parts = [
                f"{len(messages)} 条 → {session_new} 新",
                f"{session_skipped} 过滤",
            ]
            if session_self:
                parts.append(f"{session_self} 自己")
            if not pull_all:
                if session_mode == "回填":
                    parts.append(f"{session_old} 超 {config.backfill_hours}h")
                else:
                    parts.append(f"{session_old} 超窗口")
            parts.append(f"{session_processed} 已处理")
            parts.append(f"窗口={session_mode}")
            parts.append(fmt_dur(time_module.perf_counter() - session_start))
            logger.info(
                f"  [{session_idx}/{len(enabled_sessions)}] {label}: "
                + ", ".join(parts)
            )
            total_self += session_self

        except Exception as e:
            logger.error(
                f"  [{session_idx}/{len(enabled_sessions)}] {label}: 失败 — {e}"
            )
            raise

    if config.ignore_self and total_raw and not saw_is_send_field:
        logger.warning(
            "[weflow] IGNORE_SELF=true 但本轮消息均无 isSend 字段：当前 WeFlow "
            "版本可能不提供该字段，自己发送的消息过滤未生效"
        )

    summary = (
        f"[weflow] poll 完成: {len(result.messages)} 条新消息 "
        f"(原始 {total_raw}, 预过滤 {total_skipped}, "
        f"{len(enabled_sessions)} 会话, {fmt_dur(time_module.perf_counter() - poll_start)})"
    )
    if total_self:
        summary = summary[:-1] + f", 自己 {total_self})"
    logger.info(summary)
    return result
