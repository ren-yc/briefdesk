"""REST 轮询器 — 通过 WeFlowClient 拉取历史消息（weflow-server :5033）。

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
    WeFlowClient,
    WeFlowMessage,
    WeFlowNotReadyError,
    WeFlowSession,
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
_PAGE_LIMIT = 500
_MAX_PAGES = 2000


def _session_kind(s: WeFlowSession) -> tuple[bool, bool]:
    """会话类型判定 →（是否群聊, 是否公众号）。

    sessionType 权威（group/private/official/other）：chatlab 格式的 type
    实测不可靠（official 会话也返回 private），故用原生格式的 sessionType。
    缺失时回退数字 type：上游 SessionKind 枚举序为
    private=0 / group=1 / official=2 / other=3（`store::SessionKind`），
    other 与 private 同等对待（非群非公众号）。
    """
    st = s.get("sessionType", "")
    if st == "group":
        return True, False
    if st == "official":
        return False, True
    if st in ("private", "other"):
        return False, False
    # 兜底：数字 type（枚举序，见上）
    type_num = s.get("type")
    return type_num == 1, type_num == 2


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

    # 503 就绪门控（无账号/索引期）是启动期预期瞬态：静默跳过本轮，不污染 lastError
    try:
        await client.ensure_ready()
        contacts: dict[str, str] = await client.fetch_contacts()
        all_sessions: list[WeFlowSession] = await client.fetch_sessions()
        # 兜底：私聊/公众号的对端可能不在上游 contacts 集合（数据缺失），
        # 用会话显示名补全映射（仅私聊与公众号；群会话 id 是群号，不能当发送者）
        for s in all_sessions:
            is_group, _ = _session_kind(s)
            if not is_group and s.get("username"):
                uid = s["username"]
                if uid not in contacts:
                    name = clean_display_name(s.get("displayName"))
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
                session_id=s["username"],
                name=clean_display_name(s.get("displayName")) or s["username"],
                is_group=_session_kind(s)[0],
                is_official=_session_kind(s)[1],
                last_active_at=s.get("lastTimestamp", 0),
            )
            # 服务端可能产出空 username 的脏会话（数据缺陷），无 id 的会话无意义
            for s in all_sessions
            if s.get("username")
        ]
    except WeFlowNotReadyError:
        logger.info("weflow-server 未就绪（503），本轮轮询跳过")
        # 发现阶段即不可用：全部传入的启用会话都视为未成功拉取，
        # 应用层（poll_cycle）据此跳过它们的水位推进，防窗口消息永久漏拉
        result.failed_sessions = {s.session_id for s in enabled_sessions}
        return result

    result.session_count = len(enabled_sessions)

    if not enabled_sessions:
        logger.info(
            f"No enabled sessions — discovered {len(result.sessions)} total, "
            "enable groups in settings"
        )
        return result

    logger.info(
        f"Polling {len(enabled_sessions)} enabled sessions "
        f"({len(result.sessions)} total discovered)"
    )

    total_raw = 0
    total_skipped = 0
    total_self = 0  # IGNORE_SELF 预滤的自消息数（不进管道、不标记 processed）
    not_ready_skips = 0
    session_idx = 0
    # IGNORE_SELF 开启时检测 isSend 字段是否可得（微信 DB 原义字段，
    # 个别版本可能不返回；缺失则自消息过滤实际未生效，需告警）
    saw_is_send_field = False
    # 不再查 /api/v1/group-members：其 groupNickname 出自上游从未被写入的
    # group_cards（恒空），displayName 与消息自带 senderName 同出
    # contacts.display_name()，纯冗余（每个启用群省一次请求）。

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
            # 统一翻页：所有模式都按 offset 递增直至 hasMore=False / 空页；
            # _MAX_PAGES 仅为防异常状态下的无界循环守卫（全量/异常时打 WARNING）
            messages: list[WeFlowMessage] = []
            offset = 0
            for page in range(_MAX_PAGES):
                resp = await client.fetch_messages(
                    session_id,
                    start_ts,
                    limit=_PAGE_LIMIT,
                    offset=offset,
                    media=True,
                    not_found_ok=True,
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
            # 已处理的公众号文章每轮都被当作"新"并重复归一化。
            # rawContent 在 media=1 时也保留（weflow-server 不渲染占位符），
            # 拆条数可直接解析得出。
            msg_ids: list[str] = []
            article_split_n: dict[str, int] = {}
            for m in messages:
                msg_ids.append(str(m["serverId"]))
                if m.get("localType") == _APPMSG_LOCAL_TYPE:
                    raw = m.get("rawContent") or ""
                    if not _is_appmsg_content(raw):
                        raw = m.get("content", "")
                    n = len(parse_appmsg_xml(raw))
                    if n:
                        article_split_n[str(m["serverId"])] = n
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

                msg_id = str(msg["serverId"])
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

            for msg in candidates:
                # 文章卡片：rawContent 已在 media=True 下保留，直接拆条；
                # 仍按拆条粒度查已处理（部分处理时由 pipeline 入口过滤）
                if msg.get("localType") == _APPMSG_LOCAL_TYPE:
                    raw = msg.get("rawContent") or ""
                    if not _is_appmsg_content(raw):
                        raw = msg.get("content", "")
                    articles = parse_appmsg_xml(raw)
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
                normalized_list = normalize_rest(msg, session_id, label, contacts)
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

        except WeFlowNotReadyError:
            # 索引期偶发 503：静默跳过该会话，保留已收集部分；记入
            # failed_sessions 让应用层跳过该会话的水位推进（防永久漏拉）
            not_ready_skips += 1
            result.failed_sessions.add(session_id)
            logger.info(
                f"  [{session_idx}/{len(enabled_sessions)}] {label}: "
                "weflow-server 未就绪 503，跳过"
            )
            continue
        except Exception as e:
            logger.error(
                f"  [{session_idx}/{len(enabled_sessions)}] {label}: 失败 — {e}"
            )
            raise

    if config.ignore_self and total_raw and not saw_is_send_field:
        logger.warning(
            "[weflow] IGNORE_SELF=true 但本轮消息均无 isSend 字段：当前 weflow-server "
            "版本可能不提供该字段，自己发送的消息过滤未生效"
        )

    summary = (
        f"[weflow] poll 完成: {len(result.messages)} 条新消息 "
        f"(原始 {total_raw}, 预过滤 {total_skipped}, "
        f"{len(enabled_sessions)} 会话, {fmt_dur(time_module.perf_counter() - poll_start)})"
    )
    if total_self:
        summary = summary[:-1] + f", 自己 {total_self})"
    if not_ready_skips:
        summary = summary[:-1] + f", 503 跳过 {not_ready_skips} 会话)"
    logger.info(summary)
    return result
