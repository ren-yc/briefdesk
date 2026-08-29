"""REST 轮询器 — 通过 QqFlowClient 拉取历史消息，参照 weflow-legacy poller.py。

只产出源无关数据（PollResult），不触碰 DB；写库由应用层（poll_cycle）完成。

翻页策略：/api/v1/messages 按时间倒序返回，用 start=cutoff（秒级时间戳）
表达窗口边界（按会话：增量轮询 cutoff=会话水位-overlap，回退 cutoff=
now-BACKFILL_HOURS，全量时 start 不传），limit/offset 翻页 + 早停
（hasMore=False 或本页最旧消息已早于 cutoff——后续页只会更旧），每会话页数有界。
offset 按实际返回条数累加（非 page*limit 定长，防短页 + hasMore=true 跳行），
并用 seen_local_ids 挡掉翻页期上游插入造成的跨页重复（同 weflow poller）。

BACKFILL_HOURS=-1 表示拉取全部历史：start 不传（服务端不限时间）、不做
年龄早停，仅由 hasMore 驱动翻页，翻页守卫上限放宽。
"""

import logging
import time as time_module
from datetime import UTC, datetime

from briefdesk.config import config
from briefdesk.logger import fmt_dur
from briefdesk.masking import clean_display_name
from briefdesk.plugins.qqflow.client import (
    QqFlowClient,
    QqFlowMessage,
    QqFlowNotReadyError,
)
from briefdesk.plugins.qqflow.normalize import (
    is_self_message,
    normalize_rest,
    pre_filter_rest,
)
from briefdesk.sources_base import ProcessedQuery, session_log_prefix
from briefdesk.types import ContactInfo, PollResult, SessionInfo

logger = logging.getLogger(__name__)

# 单会话翻页守卫上限（500 条/页 × 100 页 = 5 万条），防异常状态下的无界循环；
# 全量拉取（BACKFILL_HOURS=-1）放宽到 2000 页（100 万条）
_MAX_PAGES = 100
_MAX_PAGES_ALL = 2000
_PAGE_LIMIT = 500


async def poll(
    client: QqFlowClient,
    enabled_sessions: list[SessionInfo],
    is_processed: ProcessedQuery,
    *,
    window_start_by_session: dict[str, int | None] | None = None,
) -> PollResult:
    """执行一次完整的 REST 轮询。

        Args:
            client: QqFlowClient 实例
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
    max_pages = _MAX_PAGES_ALL if pull_all else _MAX_PAGES
    poll_start = time_module.perf_counter()

    if pull_all:
        logger.warning(
            "BACKFILL_HOURS=-1：将拉取全部历史消息，每次同步都会全量扫描，"
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
    # 503 就绪门控（索引期）是启动期预期瞬态：静默跳过本轮，不污染 lastError
    try:
        await client.ensure_ready()
        contacts: dict[str, str] = await client.fetch_contacts()
        all_sessions = await client.fetch_sessions()
        # 兜底：私聊对端可能不在上游 contacts 集合（数据缺失），用会话显示名补全
        # 映射（仅私聊；群会话 id 是群号，不能当发送者）。上游会话显示名同样
        # 可能携带脏数据，净化后为空才不兜底。
        for s in all_sessions:
            if s.get("type") == 1 and s.get("username"):
                uid = s["username"]
                if uid not in contacts:
                    name = clean_display_name(s.get("displayName"))
                    if name:
                        contacts[uid] = name
        result.contacts = [
            ContactInfo(source=client.name, sender_id=username, display_name=name)
            for username, name in contacts.items()
        ]
        logger.info("联系人加载完成: %d 名", len(contacts))

        result.sessions = [
            SessionInfo(
                source=client.name,
                session_id=s["username"],
                name=clean_display_name(s.get("displayName")) or s["username"],
                is_group=s.get("type") == 2,
                last_active_at=s.get("lastTimestamp", 0),
            )
            # 服务端可能产出空 username 的脏会话（数据缺陷），无 id 的会话无意义
            for s in all_sessions
            if s.get("username")
        ]
    except QqFlowNotReadyError:
        logger.info("qqflow-server 未就绪（503），本轮轮询跳过")
        # 发现阶段即不可用：全部传入的启用会话都视为未成功拉取，
        # 应用层（poll_cycle）据此跳过它们的水位推进，防窗口消息永久漏拉
        result.failed_sessions = {s.session_id for s in enabled_sessions}
        return result

    result.session_count = len(enabled_sessions)

    # 会话发现之后才打：此时 result.sessions 已填充，「共发现」才有值
    logger.info(
        "poll 开始: 窗口 %s, %d 个启用会话（共发现 %d 个）",
        window_desc,
        len(enabled_sessions),
        len(result.sessions),
    )

    if not enabled_sessions:
        logger.info("无启用会话（共发现 %d 个），请在设置中启用群组", len(result.sessions))
        return result

    total_raw = 0
    total_skipped = 0
    total_self = 0  # IGNORE_SELF 预滤的自消息数（不进管道、不标记 processed）
    not_ready_skips = 0
    session_idx = 0
    # 不再查 /api/v1/group-members：其 groupNickname 与消息自带的 senderName
    # 是上游同一条 display_sender 链的产物，纯冗余（每个启用群省一次请求）。

    for session in enabled_sessions:
        session_idx += 1
        session_id = session.session_id
        label = session.name or session_id
        log_prefix = session_log_prefix(session_idx, len(enabled_sessions), label)
        session_start = time_module.perf_counter()
        try:
            if pull_all:
                # 全量拉取：cutoff=0（不按年龄早停）、start 不传（服务端不限时间）
                start_param: int | None = None
                cutoff = 0
                session_mode = "全量"
            else:
                start_param = (
                    window_start_by_session.get(session_id)
                    if window_start_by_session is not None
                    else None
                )
                if start_param is None:
                    # 无水位/新启用会话：回退窗口（启用即回填一次）
                    start_param = int(now.timestamp()) - backfill_seconds
                    cutoff = start_param
                    session_mode = "回填"
                else:
                    cutoff = start_param
                    session_mode = "增量"
            candidates: list[QqFlowMessage] = []
            seen_local_ids: set[str] = set()  # 翻页期间上游插入 → offset 漂移产生跨页重复
            session_raw = 0
            session_old = 0
            session_self = 0
            session_skipped = 0
            hit_old = False
            offset = 0

            for page in range(max_pages):
                resp = await client.fetch_messages(
                    session_id,
                    start=start_param,
                    limit=_PAGE_LIMIT,
                    offset=offset,
                )
                msgs = resp.get("messages", [])
                session_raw += len(msgs)
                total_raw += len(msgs)
                logger.debug(
                    "%s第 %d 页 → %d 条 (hasMore=%s)",
                    log_prefix,
                    page + 1,
                    len(msgs),
                    resp.get("hasMore"),
                )
                if not msgs:
                    break
                # 按实际返回条数步进，而非 page * _PAGE_LIMIT 定长：上游返回
                # 短页却仍报 hasMore=true 时，定长步进会跳过中间那批行。
                offset += len(msgs)

                for msg in msgs:
                    # 响应按时间倒序：一旦碰到早于窗口的消息，本页其余只会更旧
                    if msg.get("createTime", 0) < cutoff:
                        session_old += 1
                        hit_old = True
                        break
                    # IGNORE_SELF 预滤：自己发送的消息不进候选（不标记
                    # processed，关闭开关可恢复；后续轮次重滤成本可忽略）
                    if config.ignore_self and is_self_message(msg, client.self_uid):
                        session_self += 1
                        continue
                    msg_id = str(msg["localId"])
                    if msg_id in seen_local_ids:
                        # 同一条消息跨页重复（offset 翻页期间上游插入导致漂移）：
                        # 只保留先到者，避免重复入管道触发唯一键冲突/重复分类
                        logger.debug("重复 localId 跳过（翻页漂移）: %s", msg_id)
                        continue
                    seen_local_ids.add(msg_id)
                    if pre_filter_rest(msg):
                        candidates.append(msg)
                    else:
                        session_skipped += 1
                        total_skipped += 1

                if not resp.get("hasMore") or hit_old:
                    break
            else:
                logger.warning(
                    "%s达到翻页守卫上限 %d 页，可能未拉完窗口内消息",
                    log_prefix,
                    max_pages,
                )

            session_new = session_processed = 0
            if candidates:
                msg_ids = [str(m["localId"]) for m in candidates]
                processed_set = await is_processed(msg_ids)

                pending: list[QqFlowMessage] = []
                for msg in candidates:
                    if str(msg["localId"]) in processed_set:
                        session_processed += 1
                        continue
                    pending.append(msg)

                for msg in pending:
                    normalized = normalize_rest(
                        msg,
                        session_id,
                        label,
                        contacts,
                        self_uid=client.self_uid,
                    )
                    result.messages.append(normalized)
                    session_new += 1

            # 每会话必打 INFO 汇总行（含 0 条），避免"无消息会话静默"造成
            # 轮询提前结束的误读；窗口模式随行标注（增量/回填/全量）。
            parts = [
                f"{session_raw} 条 → {session_new} 新",
                f"{session_skipped} 过滤",
                f"{session_processed} 已处理",
            ]
            if session_self:
                parts.append(f"{session_self} 自己")
            if not pull_all:
                if session_mode == "回填":
                    parts.append(f"{session_old} 超 {config.backfill_hours}h")
                else:
                    parts.append(f"{session_old} 超窗口")
            parts.append(f"窗口={session_mode}")
            parts.append(fmt_dur(time_module.perf_counter() - session_start))
            logger.info("%s%s", log_prefix, ", ".join(parts))
            total_self += session_self

        except QqFlowNotReadyError:
            # 索引期偶发 503：静默跳过该会话，保留已收集部分；记入
            # failed_sessions 让应用层跳过该会话的水位推进（防永久漏拉）
            not_ready_skips += 1
            result.failed_sessions.add(session_id)
            logger.info("%sqqflow-server 索引期 503，跳过", log_prefix)
            continue
        except Exception as e:
            # 同 weflow：失败只由 run_poll_cycle 记一条带栈 ERROR，会话标签
            # 走异常链（顺带进 status.lastError），原因文本保留在末尾。
            raise RuntimeError(f"会话「{label}」拉取失败: {e}") from e

    summary = (
        f"poll 完成: {len(result.messages)} 条新消息 "
        f"(原始 {total_raw}, 预过滤 {total_skipped}, "
        f"{len(enabled_sessions)} 会话, {fmt_dur(time_module.perf_counter() - poll_start)})"
    )
    if total_self:
        summary = summary[:-1] + f", 自己 {total_self})"
    if not_ready_skips:
        summary = summary[:-1] + f", 503 跳过 {not_ready_skips} 会话)"
    logger.info(summary)
    return result
