"""SQLite 数据访问层（aiosqlite，主连接 + 向量专用连接，WAL）。"""

import asyncio
import hashlib
import json
import logging
import re
import sqlite3
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, NotRequired, TypedDict, cast

import aiosqlite

from briefdesk.config import config
from briefdesk.masking import normalize_subject
from briefdesk.types import ContextMsg

_db: aiosqlite.Connection | None = None
_embed_db: aiosqlite.Connection | None = None
_lock = asyncio.Lock()
# 向量专用连接的语句级互斥：load/upsert 在同一连接上串行执行，
# 避免读游标活动期间另一协程 COMMIT 触发 "statements in progress"。
_embed_lock = asyncio.Lock()

# pipeline 入库与 server 删除共用的存储锁。
# server 删除必须在「DB 删除 → 去重缓存移除」之间持有此锁，
# 否则 pipeline 的 check_dedup 可能命中已删除条目，把相似新消息
# 判重后标记 processed，造成该消息在本回填窗口内永久丢失。
storage_lock = asyncio.Lock()

logger = logging.getLogger(__name__)

# 默认分类类别（名称, 描述, 颜色）。描述即各类别的默认 prompt；
# 颜色与前端类别色板一致，保证视觉无变化。
# 注意：描述为「自包含」写法——排除句只描述本类不收的消息特征，
# 不得引用其它类别名（类别设定是动态的，其它类可能不在当前列表中）。
DEFAULT_CATEGORIES: list[tuple[str, str, str]] = [
    (
        "活动通知",
        "讲座、比赛、演出、聚会、社团活动、展览、运动会、学术讲座/会议/比赛、课程安排、考试通知等一次性事件或安排通知（需有具体时间/地点/主题）。排除：招募新成员、岗位招聘信息、物品买卖、科研进展与成果动态等非事件性内容。",
        "#2563EB",
    ),
    ("社团招新", "社团/学生组织明确招募新成员（需有社团名或联系方式）。排除：社团日常活动与演出、企业岗位招聘等非招募内容。", "#7C3AED"),
    ("学术", "科研进展、研究成果、学术观点、课题组动态等研究性内容（需有具体内容）。排除：讲座、会议、比赛等活动公告，课程与考试通知，资料买卖，论文课题辅导等非研究性内容。", "#059669"),
    ("交易", "二手物品/票券的买卖、转让、求购、交换（需有物品名与价格或联系方式）。排除：免费分享、寻物/招领、拼单团购、跑腿代购等非二手交易。", "#D97706"),
    ("实习", "实习机会、内推、校园招聘等岗位信息（需有岗位或企业、时间或报酬）。排除：仅宣讲会/招聘会公告而无岗位实质、学生社团招募、兼职勤工俭学等非岗位内容。", "#DB2777"),
]


# ── 期望 DB schema（启动校验用）──
# 必须与 init_schema() 中的 CREATE TABLE 保持一致；新增/修改列时同步更新此处。
# 已有数据库（存在任意期望表）启动时必须完全匹配，否则 FATAL 退出。
EXPECTED_SCHEMA: dict[str, dict[str, str]] = {
    "processed_messages": {
        "source": "TEXT",
        "msg_id": "TEXT",
        "processed_at": "TEXT",
    },
    "items": {
        "id": "TEXT",
        "category": "TEXT",
        "title": "TEXT",
        "key_info": "TEXT",
        "sender_name": "TEXT",
        "source_quote": "TEXT",
        "source_group": "TEXT",
        "subject": "TEXT",
        "source": "TEXT",
        "source_msg_id": "TEXT",
        "session_id": "TEXT",
        "msg_time": "INTEGER",
        "is_verified": "INTEGER",
        "verified_at": "TEXT",
        "content_hash": "TEXT",
        "image_urls": "TEXT",
        "article_url": "TEXT",
        "start": "TEXT",
        "end": "TEXT",
        "remind_at": "TEXT",
        "extra_times": "TEXT",
        "created_at": "TEXT",
    },
    "sessions": {
        "source": "TEXT",
        "session_id": "TEXT",
        "name": "TEXT",
        "is_group": "INTEGER",
        "is_official": "INTEGER",
        "enabled": "INTEGER",
        "last_seen": "TEXT",
        "last_active": "INTEGER",
        "last_poll_ts": "INTEGER",
    },
    "contacts": {
        "source": "TEXT",
        "sender_id": "TEXT",
        "display_name": "TEXT",
    },
    "raw_messages": {
        "source": "TEXT",
        "msg_id": "TEXT",
        "session_id": "TEXT",
        "group_name": "TEXT",
        "sender_id": "TEXT",
        "sender_name": "TEXT",
        "content": "TEXT",
        "timestamp": "INTEGER",
        "article_url": "TEXT",
    },
    "item_embeddings": {
        "item_id": "TEXT",
        "model": "TEXT",
        "embedding": "TEXT",
        "created_at": "TEXT",
    },
    "categories": {
        "id": "INTEGER",
        "name": "TEXT",
        "prompt": "TEXT",
        "color": "TEXT",
        "enabled": "INTEGER",
        "created_at": "TEXT",
    },
}


class SchemaMismatchError(RuntimeError):
    """数据库 schema 与当前代码期望不一致。"""


async def validate_schema(db: aiosqlite.Connection) -> None:
    """校验既有数据库 schema。

    - 没有任何期望表存在 → 视为全新数据库，校验通过。
    - 存在任意期望表 → 要求全部期望表存在，且列名/类型完全匹配。
    - 不匹配时抛出 SchemaMismatchError。
    """
    rows = await _fetchall(
        db, "SELECT name FROM sqlite_master WHERE type = 'table'"
    )
    existing_tables = {row["name"] for row in rows}
    expected_tables = set(EXPECTED_SCHEMA)
    existing_app_tables = expected_tables & existing_tables

    if not existing_app_tables:
        return

    missing_tables = expected_tables - existing_tables
    if missing_tables:
        raise SchemaMismatchError(
            "缺少表: " + ", ".join(sorted(missing_tables))
        )

    for table, expected_cols in EXPECTED_SCHEMA.items():
        rows = await _fetchall(db, f"PRAGMA table_info({table})")
        actual_cols = {row["name"]: row["type"] for row in rows}

        if set(actual_cols) != set(expected_cols):
            missing_cols = sorted(set(expected_cols) - set(actual_cols))
            extra_cols = sorted(set(actual_cols) - set(expected_cols))
            raise SchemaMismatchError(
                f"表 {table} 列不匹配: "
                f"缺少 {missing_cols or '无'}, 多余 {extra_cols or '无'}"
            )

        for col, expected_type in expected_cols.items():
            actual_type = (actual_cols[col] or "").upper()
            if actual_type != expected_type.upper():
                raise SchemaMismatchError(
                    f"表 {table} 列 {col} 类型不匹配: "
                    f"期望 {expected_type.upper()}, 实际 {actual_type or 'NULL'}"
                )


# ── TypedDicts: DB row shapes ──


class ItemInput(TypedDict):
    """`items` 表字段的唯一定义源 — 除 DB 生成的 id/created_at 外的全部字段。

    可空列声明为 `str | None`（键必在、值可为 None）：insert_item 与
    `SELECT *` 查询行都满足该形状。
    """

    category: str
    title: str
    key_info: str | None
    sender_name: str | None
    source_quote: str
    source_group: str
    subject: str | None  # 信息主体（写时 NFKC/小写/空白折叠归一化，展示与时间线匹配共用）
    source: str
    source_msg_id: str
    session_id: str
    msg_time: int
    is_verified: int
    content_hash: str | None
    image_urls: NotRequired[str]
    article_url: NotRequired[str]  # 文章卡片原文链接（公众号/群聊转发文章）
    start: NotRequired[str | None]  # 活动/讲座等的开始时间 "YYYY-MM-DD HH:MM"
    end: NotRequired[str | None]  # 报名/提交等的截止时间 "YYYY-MM-DD HH:MM"
    extra_times: NotRequired[str]  # 多时间点 JSON：[{"type":"start"|"end","time":"...","label":"..."}]


class ItemRow(ItemInput):
    """Row from the `items` table — ItemInput 加 DB 生成的 id/created_at/verified_at。"""

    id: str
    created_at: str
    verified_at: str | None
    remind_at: NotRequired[str]  # 用户提醒时间 "YYYY-MM-DD HH:MM"，NULL=未设（仅查询行携带）


class ReminderRow(TypedDict):
    """Row from `get_due_reminders` — 到期提醒所需最小字段。"""

    id: str
    title: str
    category: str
    remind_at: str


class SessionRow(TypedDict):
    """Row from the `sessions` table."""

    source: str
    session_id: str
    name: str
    is_group: int
    is_official: int
    enabled: int
    last_seen: str | None
    last_active: int | None  # 会话内最后一条消息时间（秒级 epoch；NULL = 未知）
    last_poll_ts: int | None  # 按会话增量轮询水位（NULL = 待回填）


class CategoryCount(TypedDict):
    """Row from `get_category_counts`."""

    key: str
    count: int
    color: str | None  # LEFT JOIN categories 的类别色；遗留类别（已删类别）为 None


class CategoryRow(TypedDict):
    """Row from the `categories` table."""

    id: int
    name: str
    prompt: str
    color: str
    enabled: int
    created_at: str


class CategoryDetail(CategoryRow):
    """CategoryRow 加该类别下 items 卡片数（删除确认框显示用）。"""

    item_count: int


class CategoryColor(TypedDict):
    """Row from `get_enabled_category_colors` — 启用类别的名称与颜色。"""

    name: str
    color: str


class RawMsgInput(TypedDict):
    """Payload for `bulk_insert_raw_messages`."""

    source: str
    msg_id: str
    session_id: str
    group_name: str
    sender_id: str
    sender_name: str
    content: str
    timestamp: int
    article_url: NotRequired[str]  # 文章卡片原文链接（可选，其余消息为空）


class ItemText(TypedDict):
    """Row from `get_all_item_texts`."""

    id: str
    source: str
    title: str
    content_hash: str
    image_urls: str
    source_quote: str


class ItemsPage(TypedDict):
    """A page of cards plus counts for the exact same filter set."""

    items: list[ItemRow]
    total_count: int
    group_count: int
    source_groups: list[str]
    has_more: bool
    next_offset: int
    filter_now: str | None


_ITEM_TIME_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})(?:[ T]\d{2}:\d{2})?$"
)


def _parse_item_time(value: object) -> tuple[datetime, bool] | None:
    """Parse stored local card time and report whether it is date-only."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value:
        return None
    match = _ITEM_TIME_RE.fullmatch(value)
    if match is None:
        return None
    date_only = value == match.group("date")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed, date_only


def _stored_time_expired(value: object, now: datetime) -> bool:
    parsed = _parse_item_time(value)
    if parsed is None:
        return False
    dt, date_only = parsed
    return dt.date() < now.date() if date_only else dt < now


def item_is_expired(
    start: object, end: object, extra_times: object, now_local: object
) -> bool:
    """Match the card-level expiry semantics used by the frontend.

    A card expires only when its primary end time is past and no main/extra
    time point remains upcoming. Date-only deadlines stay active all day.
    """
    if not isinstance(now_local, str):
        return False
    try:
        now = datetime.fromisoformat(now_local)
    except ValueError:
        return False
    if now.tzinfo is not None:
        now = now.astimezone().replace(tzinfo=None)
    if _parse_item_time(end) is None or not _stored_time_expired(end, now):
        return False

    points: list[object] = [start, end]
    if isinstance(extra_times, str) and extra_times:
        try:
            entries = json.loads(extra_times)
        except (json.JSONDecodeError, TypeError):
            entries = []
        if isinstance(entries, list):
            points.extend(
                entry.get("time")
                for entry in entries
                if isinstance(entry, dict)
                and entry.get("type") in {"start", "end"}
            )

    return not any(
        _parse_item_time(point) is not None
        and not _stored_time_expired(point, now)
        for point in points
    )


def _sqlite_item_is_expired(
    start: object, end: object, extra_times: object, now_local: object
) -> int:
    return int(item_is_expired(start, end, extra_times, now_local))


async def get_db() -> aiosqlite.Connection:
    global _db
    if _db is None:
        async with _lock:
            if _db is None:
                conn = await aiosqlite.connect(config.db_path)
                conn.row_factory = aiosqlite.Row
                cursor = await conn.execute("PRAGMA journal_mode = WAL")
                await cursor.close()
                cursor = await conn.execute("PRAGMA foreign_keys = ON")
                await cursor.close()
                await conn.commit()
                try:
                    await validate_schema(conn)
                    await init_schema(conn)
                except SchemaMismatchError as e:
                    logger.critical("数据库 schema 不匹配，拒绝启动: %s", e)
                    await conn.close()
                    raise SystemExit(1) from e
                _db = conn  # Only assign after full init
    return _db


async def get_embed_db() -> aiosqlite.Connection:
    """向量持久化专用连接（与主连接隔离，WAL + busy_timeout）。

    动机：SQLite 的"活动语句"检查是 per-connection 的——主连接上并发读取
    （pipeline 的 async-for 游标）可能让 COMMIT 报 "cannot commit
    transaction - SQL statements in progress"。向量读写走独立连接后，
    主连接的并发读不再影响落库；写写竞争由 WAL + busy_timeout 兜底。
    表结构用 init_schema 幂等补建（同文件，正常启动时主连接已建好）。
    语句级互斥由 _embed_lock 保证（load/upsert 短临界区，不含网络等待）。
    """
    global _embed_db
    if _embed_db is None:
        async with _lock:
            if _embed_db is None:
                conn = await aiosqlite.connect(config.db_path)
                conn.row_factory = aiosqlite.Row
                cursor = await conn.execute("PRAGMA journal_mode = WAL")
                await cursor.close()
                cursor = await conn.execute("PRAGMA busy_timeout = 5000")
                await cursor.close()
                await init_schema(conn)  # 幂等：同文件表结构由主连接维护，此处兜底
                _embed_db = conn
    return _embed_db


async def close_db() -> None:
    """关闭数据库连接（主连接 + 向量专用连接），停止 aiosqlite 后台 worker 线程。

    aiosqlite 的 worker 线程是非 daemon 的，若不关闭连接，
    它会永久阻塞在队列上，解释器退出 join 该线程时挂死。
    """
    global _db, _embed_db
    if _embed_db is not None:
        await _embed_db.close()
        _embed_db = None
    if _db is None:
        return
    await _db.close()
    _db = None


# ── 查询助手（游标纪律：所有游标必须显式关闭，禁止依赖 GC）──
# 未终结语句会残留在连接上，可能阻断同连接后续 COMMIT
# （"cannot commit transaction - SQL statements in progress"）。
# 新增查询一律经 _fetchone/_fetchall/_cursor，不要在业务代码里裸用 db.execute。


async def _fetchone(
    db: aiosqlite.Connection,
    sql: str,
    params: tuple[Any, ...] | list[Any] = (),
) -> aiosqlite.Row | None:
    """单行查询：游标用后即关（try/finally，异常路径同样关闭）。"""
    cursor = await db.execute(sql, params)
    try:
        return await cursor.fetchone()
    finally:
        await cursor.close()


async def _fetchall(
    db: aiosqlite.Connection,
    sql: str,
    params: tuple[Any, ...] | list[Any] = (),
) -> list[aiosqlite.Row]:
    """多行查询：游标用后即关（try/finally，异常路径同样关闭）。"""
    cursor = await db.execute(sql, params)
    try:
        return list(await cursor.fetchall())
    finally:
        await cursor.close()


@asynccontextmanager
async def _cursor(
    db: aiosqlite.Connection,
    sql: str,
    params: tuple[Any, ...] | list[Any] = (),
) -> AsyncIterator[aiosqlite.Cursor]:
    """游标作用域：流式迭代（async for）/ 需要 rowcount、lastrowid 的语句用。

    退出（含异常）自动 close；rowcount/lastrowid 必须在 with 块内读取。
    """
    cursor = await db.execute(sql, params)
    try:
        yield cursor
    finally:
        await cursor.close()


# ── 备份 / 恢复（在线备份 API，WAL 安全）──


async def backup_db_to(path: str) -> None:
    """把当前数据库在线备份到指定文件（SQLite backup API，运行中安全）。"""
    src = await get_db()
    dst = await aiosqlite.connect(path)
    try:
        await src.backup(dst)
    finally:
        await dst.close()


async def validate_restore_file(path: str) -> str | None:
    """校验待恢复的备份文件：完整性 + schema 与当前版本匹配 + 含应用数据表。

    合法返回 None，非法返回错误消息（供接口 400 / 启动忽略）。

    validate_schema 对"无任何应用表"的库会放行（"全新库"语义，仅适用于
    启动建库）；恢复路径必须额外拦截空库——否则上传新建空 sqlite 文件
    也能通过校验，重启时正式库被空库替换导致数据清空。
    """
    try:
        conn = await aiosqlite.connect(path)
    except Exception as e:  # noqa: BLE001 — 打开失败即非法
        return f"无法打开备份文件: {e}"
    try:
        conn.row_factory = aiosqlite.Row
        row = await _fetchone(conn, "PRAGMA integrity_check")
        if not row or row["integrity_check"] != "ok":
            return "备份文件完整性检查失败（integrity_check 非 ok）"
        await validate_schema(conn)
        tables = await _fetchall(
            conn, "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
        existing = {t["name"] for t in tables}
        if not (set(EXPECTED_SCHEMA) & existing):
            return "备份文件不含应用数据表，拒绝恢复（防空库覆盖正式数据）"
        return None
    except SchemaMismatchError as e:
        return f"备份文件 schema 与当前版本不匹配: {e}"
    except Exception as e:  # noqa: BLE001 — 校验失败统一视为非法
        return f"备份文件校验失败: {e}"
    finally:
        await conn.close()


async def apply_pending_restore() -> bool:
    """启动时应用待恢复备份（{db_path}.restore-pending）→ 覆盖正式库。

    校验通过才替换（并清理旧 -wal/-shm，避免旧 WAL 污染新文件）；
    校验失败则删除 pending 并记录错误，不阻断启动。
    返回是否发生了替换。
    """
    import os

    pending = f"{config.db_path}.restore-pending"
    if not os.path.exists(pending):
        return False
    err = await validate_restore_file(pending)
    if err:
        logger.error("检测到待恢复备份但校验失败，已忽略: %s", err)
        try:
            os.remove(pending)
        except OSError:
            pass
        return False
    for suffix in ("", "-wal", "-shm"):
        p = config.db_path + suffix
        if os.path.exists(p):
            os.remove(p)
    os.replace(pending, config.db_path)
    logger.info("已应用恢复备份: %s", config.db_path)
    return True


async def init_schema(db: aiosqlite.Connection) -> None:
    await db.create_function(
        "item_is_expired", 4, _sqlite_item_is_expired, deterministic=True
    )
    await db.execute("""
        CREATE TABLE IF NOT EXISTS processed_messages (
            source       TEXT NOT NULL,
            msg_id       TEXT NOT NULL,
            processed_at TEXT NOT NULL,
            PRIMARY KEY (source, msg_id)
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS items (
            id            TEXT PRIMARY KEY,
            category      TEXT NOT NULL,
            title         TEXT NOT NULL,
            key_info      TEXT,
            sender_name   TEXT,
            source_quote  TEXT NOT NULL,
            source_group  TEXT NOT NULL,
            subject       TEXT,
            source        TEXT NOT NULL DEFAULT '',
            source_msg_id TEXT NOT NULL,
            session_id    TEXT NOT NULL DEFAULT '',
            msg_time      INTEGER NOT NULL DEFAULT 0,
            is_verified   INTEGER DEFAULT 0,
            verified_at   TEXT,
            content_hash  TEXT,
            image_urls    TEXT NOT NULL DEFAULT '',
            article_url   TEXT NOT NULL DEFAULT '',
            start    TEXT,
            end      TEXT,
            remind_at     TEXT,
            extra_times   TEXT NOT NULL DEFAULT '',
            created_at    TEXT NOT NULL,
            UNIQUE (source, source_msg_id)
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            source       TEXT NOT NULL,
            session_id   TEXT NOT NULL,
            name         TEXT NOT NULL,
            is_group     INTEGER NOT NULL,
            is_official  INTEGER NOT NULL DEFAULT 0,
            enabled      INTEGER NOT NULL DEFAULT 0,
            last_seen    TEXT,
            last_active  INTEGER,
            last_poll_ts INTEGER,
            PRIMARY KEY (source, session_id)
        )
    """)

    # 查询性能索引（幂等；日历/到期提醒/主体时间线查询随数据增长避免全表扫描）
    for ddl in (
        "CREATE INDEX IF NOT EXISTS idx_items_start ON items(start)",
        "CREATE INDEX IF NOT EXISTS idx_items_end ON items(end)",
        "CREATE INDEX IF NOT EXISTS idx_items_remind_at ON items(remind_at)",
        "CREATE INDEX IF NOT EXISTS idx_items_subject ON items(subject)",
    ):
        await db.execute(ddl)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS contacts (
            source       TEXT NOT NULL,
            sender_id    TEXT NOT NULL,
            display_name TEXT NOT NULL,
            PRIMARY KEY (source, sender_id)
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS raw_messages (
            source       TEXT NOT NULL,
            msg_id       TEXT NOT NULL,
            session_id   TEXT NOT NULL,
            group_name   TEXT NOT NULL,
            sender_id    TEXT NOT NULL,
            sender_name  TEXT NOT NULL DEFAULT '',
            content      TEXT NOT NULL,
            timestamp    INTEGER NOT NULL,
            article_url  TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (source, msg_id)
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS item_embeddings (
            item_id    TEXT PRIMARY KEY,
            model      TEXT NOT NULL,
            embedding  TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS categories (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT NOT NULL UNIQUE,
            prompt     TEXT NOT NULL DEFAULT '',
            color      TEXT NOT NULL DEFAULT '#6B7280',
            enabled    INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        )
    """)

    # 可选扩展表（不参与 EXPECTED_SCHEMA 严格校验）：
    # 仅 CREATE TABLE IF NOT EXISTS 幂等补建——旧库启动自动加表、不会触发
    # validate_schema 的 FATAL；需要"可选新表"时沿用本模式（新表不进 EXPECTED_SCHEMA）。
    await db.execute("""
        CREATE TABLE IF NOT EXISTS recat_log (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id         TEXT NOT NULL,
            source          TEXT NOT NULL DEFAULT '',
            source_msg_id   TEXT NOT NULL DEFAULT '',
            category_before TEXT NOT NULL,
            category_after  TEXT NOT NULL,
            content         TEXT NOT NULL DEFAULT '',
            created_at      TEXT NOT NULL
        )
    """)
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_recat_log_created ON recat_log(created_at)"
    )

    await _seed_default_categories(db)
    await db.commit()


async def _seed_default_categories(db: aiosqlite.Connection) -> None:
    """类别表为空时播种默认五类（用户删光重启后恢复，需求已确认）。

    与 schema DDL 同事务提交，get_db 是唯一 DB 入口，首次使用时必然播种。
    """
    row = await _fetchone(db, "SELECT COUNT(*) as cnt FROM categories")
    if row and row["cnt"] > 0:
        return
    now = datetime.now(UTC).isoformat()
    cursor = await db.executemany(
        "INSERT INTO categories (name, prompt, color, enabled, created_at) "
        "VALUES (?, ?, ?, 1, ?)",
        [(name, prompt, color, now) for name, prompt, color in DEFAULT_CATEGORIES],
    )
    await cursor.close()


# ── Items CRUD ──


async def insert_item(item: ItemInput) -> str:
    db = await get_db()
    item_id = str(uuid.uuid4())
    created_at = datetime.now(UTC).isoformat()
    # INSERT OR IGNORE + RETURNING：唯一键 (source, source_msg_id) 冲突时
    # RETURNING 返回空行，此时回查已存在行的真实 id —— 避免向调用方返回
    # "幽灵 id"（新 uuid 但实际未插入），防止去重缓存/合并阶段拿到无效 id。
    row = await _fetchone(
        db,
        """INSERT OR IGNORE INTO items
           (id, category, title, key_info,
            sender_name, source_quote, source_group, subject, source, source_msg_id, session_id,
            msg_time, is_verified, content_hash, image_urls, article_url, start, end,
            extra_times, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           RETURNING id""",
        (
            item_id,
            item["category"],
            item["title"],
            item.get("key_info"),
            item.get("sender_name"),
            item.get("source_quote"),
            item.get("source_group"),
            item.get("subject"),
            item["source"],
            item["source_msg_id"],
            item.get("session_id", ""),
            item.get("msg_time", 0),
            item.get("is_verified", 0),
            item.get("content_hash"),
            item.get("image_urls", ""),
            item.get("article_url", ""),
            item.get("start"),
            item.get("end"),
            item.get("extra_times", ""),
            created_at,
        ),
    )
    await db.commit()
    if row is not None:
        return str(row["id"])
    # 冲突路径：返回已存在的真实行 id（与幽灵 id 语义一致但指向真实数据）
    row2 = await _fetchone(
        db,
        "SELECT id FROM items WHERE source = ? AND source_msg_id = ?",
        (item["source"], item["source_msg_id"]),
    )
    if row2 is not None:
        return str(row2["id"])
    return item_id  # 极端兜底（冲突却查不到行，理论不可达）


def _escape_like(term: str) -> str:
    """转义 LIKE 通配符与转义符，使用户搜索词按字面量匹配。"""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _items_where(
    category: str | None,
    verified: str,
    q: str | None,
    *,
    source_group: str | None = None,
    min_msg_time: int | None = None,
    hide_expired: bool = False,
    now_local: str | None = None,
) -> tuple[str, list[Any]]:
    """列表、总条数与组数共用的完整过滤条件。

    返回 ("WHERE 1=1 …", params)，不含 ORDER/LIMIT。
    """
    sql = "WHERE 1=1"
    params: list[Any] = []

    if category and category != "全部":
        sql += " AND category = ?"
        params.append(category)
    if verified == "memo":
        sql += " AND is_verified = 1"
    elif verified == "ignored":
        sql += " AND is_verified = -1"
    elif verified != "all":
        sql += " AND is_verified = 0"
    if q and q.strip():
        # 多词 OR：空白拆分，每词对全部可检索字段 LIKE，词间 OR
        # （支撑关键词订阅的联合检索；字段含 key_info/subject）。
        # 纯空白 q 被 q.strip() 拦截，避免拼出空子句 "AND ()" 的 SQL 语法错误。
        clauses: list[str] = []
        for term in q.split():
            like = f"%{_escape_like(term)}%"
            clauses.append(
                "(title LIKE ? ESCAPE '\\' OR source_quote LIKE ? ESCAPE '\\' "
                "OR source_group LIKE ? ESCAPE '\\' "
                "OR sender_name LIKE ? ESCAPE '\\' OR category LIKE ? ESCAPE '\\' "
                "OR key_info LIKE ? ESCAPE '\\' OR subject LIKE ? ESCAPE '\\')"
            )
            params.extend([like] * 7)
        sql += " AND (" + " OR ".join(clauses) + ")"
    if source_group:
        # 包含匹配：来源下拉选项已按 ", " 拆成单个来源，选中项须能命中
        # 多来源合并卡片（source_group 形如 "群A, 群B"）
        sql += " AND source_group LIKE ? ESCAPE '\\'"
        params.append(f"%{_escape_like(source_group)}%")
    if min_msg_time is not None:
        sql += " AND msg_time >= ?"
        params.append(min_msg_time)

    # 列表只展示启用类别与启用会话。会话按复合键匹配，避免多源同名/同 ID
    # 在前端扁平 Set 中互相误伤；无 sessions 行的遗留卡片继续保留。
    sql += _DISABLED_CAT_SQL
    sql += (
        " AND NOT EXISTS (SELECT 1 FROM sessions s "
        "WHERE s.source = items.source AND s.session_id = items.session_id "
        "AND s.enabled = 0)"
    )
    if hide_expired:
        if now_local is None:
            raise ValueError("now_local is required when hide_expired is true")
        sql += " AND item_is_expired(start, end, extra_times, ?) = 0"
        params.append(now_local)
    return sql, params


async def get_items(
    category: str | None = None,
    verified: str = "unverified",
    q: str | None = None,
    limit: int = 100,
    offset: int = 0,
    *,
    source_group: str | None = None,
    min_msg_time: int | None = None,
    hide_expired: bool = False,
    now_local: str | None = None,
) -> list[ItemRow]:
    db = await get_db()
    where, params = _items_where(
        category,
        verified,
        q,
        source_group=source_group,
        min_msg_time=min_msg_time,
        hide_expired=hide_expired,
        now_local=now_local,
    )
    sql = (
        f"SELECT * FROM items {where} "
        "ORDER BY msg_time DESC, id DESC LIMIT ? OFFSET ?"
    )
    params.extend([limit, offset])
    rows = await _fetchall(db, sql, tuple(params))
    return [cast(ItemRow, dict(row)) for row in rows]


async def get_group_count(
    category: str | None = None,
    verified: str = "unverified",
    q: str | None = None,
    *,
    source_group: str | None = None,
    min_msg_time: int | None = None,
    hide_expired: bool = False,
    now_local: str | None = None,
) -> int:
    """当前视图的组数（与列表渲染块口径一致），供合并模式头部计数。

    组 = 有主体的 (subject, category) 去重键（每组一块，含 1 成员组）；
    无主体条目各为一块。停用类别排除（同侧边栏/可见列表口径）。
    """
    db = await get_db()
    where, params = _items_where(
        category,
        verified,
        q,
        source_group=source_group,
        min_msg_time=min_msg_time,
        hide_expired=hide_expired,
        now_local=now_local,
    )
    sql = (
        "SELECT "
        "(SELECT COUNT(*) FROM (SELECT subject, category FROM items "
        f"{where} AND subject IS NOT NULL AND TRIM(subject) != '' GROUP BY subject, category)) "
        f"+ (SELECT COUNT(*) FROM items {where} AND (subject IS NULL OR TRIM(subject) = '')) "
        "AS group_count"
    )
    row = await _fetchone(
        db,
        sql,
        tuple(params) + tuple(params),
    )
    return row["group_count"] if row else 0


async def get_items_page(
    category: str | None = None,
    verified: str = "unverified",
    q: str | None = None,
    limit: int = 100,
    offset: int = 0,
    *,
    source_group: str | None = None,
    min_msg_time: int | None = None,
    hide_expired: bool = False,
    now_local: str | None = None,
) -> ItemsPage:
    """Return one stable page and full-list counts under identical filters."""
    db = await get_db()
    where, params = _items_where(
        category,
        verified,
        q,
        source_group=source_group,
        min_msg_time=min_msg_time,
        hide_expired=hide_expired,
        now_local=now_local,
    )

    # 页面、计数和来源群选项全部从同一条语句的 filtered CTE 读取，避免实时
    # 写入在多次 SELECT 之间改变结果集。LEFT JOIN 保证空页也能返回完整元数据。
    source_where = ""
    source_params: list[Any] = []
    if q and q.strip():
        source_where, source_params = _items_where(
            category,
            verified,
            q,
            min_msg_time=min_msg_time,
            hide_expired=hide_expired,
            now_local=now_local,
        )
        source_groups_sql = (
            "COALESCE((SELECT json_group_array(source_group) FROM ("
            "SELECT DISTINCT source_group FROM source_filtered "
            "WHERE source_group != '' ORDER BY source_group"
            ")), '[]')"
        )
        source_cte = f", source_filtered AS (SELECT * FROM items {source_where})"
    else:
        source_groups_sql = "'[]'"
        source_cte = ""
    sql = f"""
        WITH filtered AS (
            SELECT * FROM items {where}
        ){source_cte},
        page AS (
            SELECT * FROM filtered
            ORDER BY msg_time DESC, id DESC
            LIMIT ? OFFSET ?
        ),
        metadata AS (
            SELECT
                (SELECT COUNT(*) FROM filtered) AS total_count,
                (SELECT COUNT(*) FROM (
                    SELECT subject, category FROM filtered
                    WHERE subject IS NOT NULL AND TRIM(subject) != ''
                    GROUP BY subject, category
                ))
                + (SELECT COUNT(*) FROM filtered
                   WHERE subject IS NULL OR TRIM(subject) = '') AS group_count,
                {source_groups_sql} AS source_groups
        )
        SELECT page.*, metadata.total_count, metadata.group_count, metadata.source_groups
        FROM metadata
        LEFT JOIN page ON 1 = 1
        ORDER BY page.msg_time DESC, page.id DESC
    """
    rows = list(
        await _fetchall(
            db,
            sql,
            tuple(params) + tuple(source_params) + (limit, offset),
        )
    )

    total_count = int(rows[0]["total_count"]) if rows else 0
    group_count = int(rows[0]["group_count"]) if rows else 0
    source_groups_raw = rows[0]["source_groups"] if rows else "[]"
    try:
        source_groups_value = json.loads(source_groups_raw or "[]")
    except (json.JSONDecodeError, TypeError):
        source_groups_value = []
    # 多来源合并卡片的 source_group 为 ", " 连接的字符串（见 merge_source_group），
    # 拆成单个来源后去重排序，供搜索筛选「来源」下拉逐项展示
    seen_groups: set[str] = set()
    source_groups: list[str] = []
    for group in (
        source_groups_value if isinstance(source_groups_value, list) else []
    ):
        for part in str(group).split(", "):
            p = part.strip()
            if p and p not in seen_groups:
                seen_groups.add(p)
                source_groups.append(p)
    source_groups.sort()
    metadata_keys = {"total_count", "group_count", "source_groups"}
    items = []
    for row in rows:
        item = dict(row)
        if item.get("id") is None:
            continue
        for key in metadata_keys:
            item.pop(key, None)
        items.append(cast(ItemRow, item))

    next_offset = offset + len(items)
    return {
        "items": items,
        "total_count": total_count,
        "group_count": group_count,
        "source_groups": source_groups,
        "has_more": next_offset < total_count,
        "next_offset": next_offset,
        "filter_now": now_local if hide_expired else None,
    }


# 停用类别的卡片不参与任何侧边栏计数（前端显示层面同样被过滤，两侧一致）；
# 已删除类别（categories 无行）的遗留卡片同样不计数：侧边栏只统计
# categories 表中仍存在的分类，避免"删了类别还显示计数"。
_DISABLED_CAT_SQL = (
    " AND category NOT IN (SELECT name FROM categories WHERE enabled = 0)"
)


async def get_category_counts() -> list[CategoryCount]:
    db = await get_db()
    rows = await _fetchall(
        db,
        "SELECT i.category as key, COUNT(*) as count, c.color as color "
        "FROM items i LEFT JOIN categories c ON c.name = i.category "
        "WHERE i.is_verified = 0 AND c.name IS NOT NULL"
        + _DISABLED_CAT_SQL
        + " GROUP BY i.category ORDER BY count DESC",
    )
    return [cast(CategoryCount, dict(row)) for row in rows]


async def get_all_category_count() -> int:
    db = await get_db()
    row = await _fetchone(
        db,
        "SELECT COUNT(*) as count FROM items WHERE is_verified = 0" + _DISABLED_CAT_SQL,
    )
    return row["count"] if row else 0


async def get_memo_count() -> int:
    db = await get_db()
    row = await _fetchone(
        db,
        "SELECT COUNT(*) as count FROM items WHERE is_verified = 1" + _DISABLED_CAT_SQL,
    )
    return row["count"] if row else 0


async def get_ignored_count() -> int:
    db = await get_db()
    row = await _fetchone(
        db,
        "SELECT COUNT(*) as count FROM items WHERE is_verified = -1" + _DISABLED_CAT_SQL,
    )
    return row["count"] if row else 0


async def update_item_verify(item_id: str, verified: int) -> bool:
    """更新卡片验证状态（memo=1 / ignored=-1 / unverify=0）；返回是否命中卡片。"""
    db = await get_db()
    now = datetime.now(UTC).isoformat()
    async with _cursor(
        db,
        "UPDATE items SET is_verified = ?, verified_at = ? WHERE id = ?",
        (verified, now, item_id),
    ) as cursor:
        changed = cursor.rowcount > 0
    await db.commit()
    return changed


async def update_items_verify(ids: list[str], verified: int) -> int:
    """批量更新卡片验证状态（memo=1 / ignored=-1 / unverify=0）；返回受影响行数。"""
    if not ids:
        return 0
    db = await get_db()
    now = datetime.now(UTC).isoformat()
    placeholders = ",".join("?" for _ in ids)
    async with _cursor(
        db,
        f"UPDATE items SET is_verified = ?, verified_at = ? WHERE id IN ({placeholders})",
        (verified, now, *ids),
    ) as cursor:
        affected = cursor.rowcount
    await db.commit()
    return affected


async def delete_items(ids: list[str], *, keep_raw_messages: bool = False) -> int:
    """批量删除卡片：item_embeddings → (raw_messages) → items（单事务）。

    raw_messages 先于 items 删除（子查询依赖 items 中尚未删除的行）；
    保留 processed_messages，避免消息在回填窗口内被重新处理
    （同 purge_expired_ignored / delete_category）。
    keep_raw_messages=True 时保留原文行：会话合并吸收的片段卡仍属于
    对话上下文（/api/context 引用展示），与闲聊消息的 raw 行保留语义一致。
    返回删除的卡片数。
    """
    if not ids:
        return 0
    db = await get_db()
    placeholders = ",".join("?" for _ in ids)
    try:
        await db.execute(
            f"DELETE FROM item_embeddings WHERE item_id IN ({placeholders})", tuple(ids)
        )
        if not keep_raw_messages:
            await db.execute(
                f"DELETE FROM raw_messages WHERE (source, msg_id) IN ("
                f"SELECT source, source_msg_id FROM items WHERE id IN ({placeholders}))",
                tuple(ids),
            )
        async with _cursor(
            db,
            f"DELETE FROM items WHERE id IN ({placeholders})",
            tuple(ids),
        ) as cursor:
            deleted = cursor.rowcount
    except Exception:
        # 多步写异常路径必须回滚：悬挂事务会被下一个不相干写操作的
        # commit 收尾提交，造成部分写入提前可见
        await db.rollback()
        raise
    await db.commit()
    return deleted


async def update_item_category(item_id: str, category: str) -> ItemRow | None:
    """手动修正卡片分类类别；返回新行，卡片不存在返回 None。

    不改 is_verified；去重缓存只存 title/source_quote（不含类别），无需同步。
    类别确实变化时写入 recat_log（人工修正样本，供模型微调导出），
    与 UPDATE 同事务；日志写入失败仅 WARNING，不影响主流程。
    """
    db = await get_db()
    old = await _fetchone(db, "SELECT * FROM items WHERE id = ?", (item_id,))
    if old is None or old["category"] == category:
        return cast(ItemRow, dict(old)) if old else None
    async with _cursor(
        db, "UPDATE items SET category = ? WHERE id = ?", (category, item_id)
    ) as cursor:
        changed = cursor.rowcount > 0
    if not changed:
        return None
    try:
        await db.execute(
            "INSERT INTO recat_log "
            "(item_id, source, source_msg_id, category_before, category_after, content, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                item_id,
                old["source"] or "",
                old["source_msg_id"] or "",
                old["category"],
                category,
                old["source_quote"] or "",
                datetime.now(UTC).isoformat(),
            ),
        )
    except Exception:  # noqa: BLE001 — 样本记录失败不应阻断分类修正
        logger.warning("recat_log 写入失败（不影响分类修正）: item_id=%s", item_id)
    await db.commit()
    row = await _fetchone(db, "SELECT * FROM items WHERE id = ?", (item_id,))
    return cast(ItemRow, dict(row)) if row else None


async def get_recat_samples(limit: int = 10000) -> list[dict]:
    """读取人工分类修正样本（category_before != category_after，导出供微调）。"""
    db = await get_db()
    rows = await _fetchall(
        db,
        "SELECT item_id, source, source_msg_id, category_before, category_after, "
        "content, created_at FROM recat_log "
        "WHERE category_before != category_after "
        "ORDER BY id DESC LIMIT ?",
        (limit,),
    )
    return [dict(row) for row in rows]


async def category_exists(name: str) -> bool:
    """启用类别存在性校验（recategorize 用：只允许改到启用类别，
    避免卡片被改入停用类别后从界面消失）。"""
    db = await get_db()
    row = await _fetchone(
        db, "SELECT 1 FROM categories WHERE name = ? AND enabled = 1", (name,)
    )
    return row is not None


# ── 会话内同话题片段合并 ──


async def get_merge_candidates(
    source: str,
    session_id: str,
    category: str,
    around_ts: int,
    window_seconds: int,
    exclude_ids: list[str],
    limit: int,
) -> list[ItemRow]:
    """合并候选：同会话同类别、未核实、msg_time 落在新卡消息时间 ± 窗口内。

    按 msg_time 升序（最早的话题头卡在前）：合并时存活卡取最早一张，
    保证同一话题始终收敛到同一张头卡。exclude_ids 排除新卡自身。
    """
    db = await get_db()
    sql = (
        "SELECT * FROM items WHERE source = ? AND session_id = ? "
        "AND category = ? AND is_verified = 0 "
        "AND msg_time BETWEEN ? AND ?"
    )
    params: list[Any] = [
        source,
        session_id,
        category,
        around_ts - window_seconds,
        around_ts + window_seconds,
    ]
    if exclude_ids:
        placeholders = ",".join("?" for _ in exclude_ids)
        sql += f" AND id NOT IN ({placeholders})"
        params.extend(exclude_ids)
    sql += " ORDER BY msg_time ASC LIMIT ?"
    params.append(limit)
    rows = await _fetchall(db, sql, tuple(params))
    return [cast(ItemRow, dict(row)) for row in rows]


async def update_item_merged(
    item_id: str,
    title: str,
    key_info: str,
    source_quote: str,
    subject: str,
    start: str,
    end: str,
    msg_time: int,
    image_urls: str,
    extra_times: str = "",
) -> None:
    """合并后回写存活卡的内容字段（含 content_hash 重算）。

    不改 is_verified/verified_at/remind_at/category/source 等元数据；
    调用方需同步维护去重内存缓存（删除被吸收卡、按新文本重加存活卡）。
    同步删除该卡的 item_embeddings：合并改写了标题/原文，旧向量语义失效，
    重启后缓存加载会按新文本重算，避免用合并前文本的向量参与余弦（语义漂移）。
    """
    db = await get_db()
    content_hash = hashlib.sha256(source_quote.encode()).hexdigest()[:16]
    await db.execute(
        "UPDATE items SET title = ?, key_info = ?, "
        "source_quote = ?, subject = ?, start = ?, end = ?, "
        "msg_time = ?, image_urls = ?, extra_times = ?, content_hash = ? WHERE id = ?",
        (
            title,
            key_info or None,
            source_quote,
            subject or None,
            start or None,
            end or None,
            msg_time,
            image_urls,
            extra_times,
            content_hash,
            item_id,
        ),
    )
    await db.execute(
        "DELETE FROM item_embeddings WHERE item_id = ?", (item_id,)
    )
    await db.commit()


# ── 提醒 / 日历 / 主体时间线 ──


async def set_item_reminder(item_id: str, remind_at: str | None) -> bool:
    """设置/清除卡片提醒时间（None=清除）；返回是否命中卡片。

    返回命中与否供前端"先清后通知"竞态判定：多标签页同时到点时，
    只有一个标签页的清除调用命中并负责通知。清除分支限定
    `remind_at IS NOT NULL`——对无提醒卡片清除必须返回 False，
    否则 rowcount 恒真，多标签页互斥判据失效（重复通知）。
    """
    db = await get_db()
    if remind_at is None:
        async with _cursor(
            db,
            "UPDATE items SET remind_at = NULL WHERE id = ? AND remind_at IS NOT NULL",
            (item_id,),
        ) as cursor:
            changed = cursor.rowcount > 0
    else:
        async with _cursor(
            db, "UPDATE items SET remind_at = ? WHERE id = ?", (remind_at, item_id)
        ) as cursor:
            changed = cursor.rowcount > 0
    await db.commit()
    return changed


async def get_due_reminders(now_local: str) -> list[ReminderRow]:
    """到期提醒查询：remind_at 不晚于 now_local 的卡片（排除已忽略）。

    now_local 为本地墙钟 "YYYY-MM-DD HH:MM"（remind_at 同为 naive 本地时间，
    字符串比较即时间序）。排除 is_verified=-1：忽略即放弃提醒但保留数据，
    撤销忽略（is_verified=0/1）后该卡片重新进入结果。此处不清除 remind_at
    ——清除由前端"先清后通知"竞态经 POST /reminder 完成，多标签页只有抢到
    清除权的一方负责通知。
    """
    db = await get_db()
    rows = await _fetchall(
        db,
        """SELECT id, title, category, remind_at FROM items
           WHERE remind_at IS NOT NULL
             AND remind_at <= ?
             AND is_verified >= 0
           ORDER BY remind_at""",
        (now_local,),
    )
    return [cast(ReminderRow, dict(row)) for row in rows]


async def get_items_by_subject(subject: str, limit: int, offset: int) -> list[ItemRow]:
    """主体时间线：跨类别查询该主体的全部历史卡片（排除已忽略）。

    subject 入库前已归一化（NFKC/小写/空白折叠），查询词同步归一化后
    精确匹配，兼容跨写法聚合。
    """
    db = await get_db()
    rows = await _fetchall(
        db,
        """SELECT * FROM items
           WHERE subject = ? AND is_verified >= 0
           ORDER BY msg_time DESC LIMIT ? OFFSET ?""",
        (normalize_subject(subject), limit, offset),
    )
    return [cast(ItemRow, dict(row)) for row in rows]


async def get_subject_count(subject: str) -> int:
    """主体时间线总数（排除已忽略），subject 查询词归一化后匹配。"""
    db = await get_db()
    row = await _fetchone(
        db,
        "SELECT COUNT(*) as cnt FROM items WHERE subject = ? AND is_verified >= 0",
        (normalize_subject(subject),),
    )
    return row["cnt"] if row else 0


async def purge_expired_ignored(expiry_hours: int) -> int:
    """删除被忽略超过 expiry_hours 的条目（items 及对应 raw_messages）。

    保留 processed_messages：避免同一条消息在回填窗口内被重新处理、重新入库。
    先删 raw_messages（子查询依赖 items 中尚未删除的行），再删 items。
    注：verified_at 为 NULL 的旧行（迁移前入库）不匹配任何比较，不会被清理。
    """
    db = await get_db()
    cutoff = (datetime.now(UTC) - timedelta(hours=expiry_hours)).isoformat()
    try:
        await db.execute(
            "DELETE FROM item_embeddings WHERE item_id IN ("
            "SELECT id FROM items WHERE is_verified = -1 AND verified_at <= ?"
            ")",
            (cutoff,),
        )
        await db.execute(
            "DELETE FROM raw_messages WHERE (source, msg_id) IN ("
            "SELECT source, source_msg_id FROM items WHERE is_verified = -1 AND verified_at <= ?"
            ")",
            (cutoff,),
        )
        async with _cursor(
            db,
            "DELETE FROM items WHERE is_verified = -1 AND verified_at <= ?",
            (cutoff,),
        ) as cursor:
            purged = cursor.rowcount
    except Exception:
        # 多步写异常路径必须回滚：悬挂事务会被下一个不相干写操作的
        # commit 收尾提交，造成部分写入提前可见
        await db.rollback()
        raise
    await db.commit()
    return purged


# ── Processed Messages ──


_PROCESSED_QUERY_CHUNK = 900  # 单语句占位符预算，远低于 SQLite 变量上限（32766）


async def are_messages_processed(source: str, msg_ids: list[str]) -> set[str]:
    """批量查询已处理消息；按 900 条分块，防超 SQLite 变量上限整批崩。"""
    if not msg_ids:
        return set()
    db = await get_db()
    found: set[str] = set()
    for start in range(0, len(msg_ids), _PROCESSED_QUERY_CHUNK):
        chunk = msg_ids[start : start + _PROCESSED_QUERY_CHUNK]
        placeholders = ",".join("?" for _ in chunk)
        # 物化读取（_fetchall）：流式 async for 会保持活动游标，与实时监听
        # 管道并发 commit 时触发 "cannot commit transaction - SQL statements in progress"
        rows = await _fetchall(
            db,
            f"SELECT msg_id FROM processed_messages WHERE source = ? "
            f"AND msg_id IN ({placeholders})",
            (source, *chunk),
        )
        found.update(row["msg_id"] for row in rows)
    return found


async def mark_message_processed(source: str, msg_id: str) -> None:
    db = await get_db()
    now = datetime.now(UTC).isoformat()
    await db.execute(
        "INSERT OR IGNORE INTO processed_messages (source, msg_id, processed_at) VALUES (?, ?, ?)",
        (source, msg_id, now),
    )
    await db.commit()


# ── 会话水位（增量轮询）──


async def get_session_last_polls(
    source: str, session_ids: list[str]
) -> dict[str, int | None]:
    """批量读取会话水位（sessions.last_poll_ts，秒级时间戳；NULL = 待回填）。"""
    if not session_ids:
        return {}
    db = await get_db()
    placeholders = ",".join("?" for _ in session_ids)
    rows = await _fetchall(
        db,
        f"SELECT session_id, last_poll_ts FROM sessions "
        f"WHERE source = ? AND session_id IN ({placeholders})",
        (source, *session_ids),
    )
    return {row["session_id"]: row["last_poll_ts"] for row in rows}


async def update_session_last_polls(source: str, rows: list[tuple[str, int]]) -> None:
    """批量写入会话水位（一轮 poll 成功处理后统一推进，保持 at-least-once）。"""
    if not rows:
        return
    db = await get_db()
    cursor = await db.executemany(
        "UPDATE sessions SET last_poll_ts = ? WHERE source = ? AND session_id = ?",
        [(ts, source, session_id) for session_id, ts in rows],
    )
    await cursor.close()
    await db.commit()


async def get_oldest_unprocessed_by_session(source: str) -> dict[str, int]:
    """各会话最早一条已落 raw_messages 但未标记 processed 的消息时间戳。

    即分类失败待回填消息按会话分组的最早时间；无未处理消息的会话不出现在
    结果中。增量轮询按会话用它钉窗口下界：仅"有未处理消息"的会话以其
    最久远未处理消息为下界，其余会话水位不受影响（调用方只取启用会话）。
    """
    db = await get_db()
    rows = await _fetchall(
        db,
        """
        SELECT r.session_id, MIN(r.timestamp) AS ts
        FROM raw_messages r
        LEFT JOIN processed_messages p
          ON p.source = r.source AND p.msg_id = r.msg_id
        WHERE r.source = ? AND p.msg_id IS NULL
        GROUP BY r.session_id
        """,
        (source,),
    )
    return {row["session_id"]: int(row["ts"]) for row in rows}


# ── Sessions ──


async def upsert_session(
    source: str,
    session_id: str,
    name: str,
    is_group: bool,
    is_official: bool = False,
    last_active_at: int | None = None,
) -> None:
    """写入/更新会话（单语句 UPSERT，原子）。

    旧实现为 SELECT 判存在 + INSERT/UPDATE 两步，多源并发刷新同一会话时
    存在读-改-写竞态（可能触发主键冲突）。enabled/last_poll_ts 不在
    DO UPDATE 更新列中，保留用户启用状态与会话水位（与旧语义一致）。
    """
    db = await get_db()
    now = datetime.now(UTC).isoformat()
    await db.execute(
        "INSERT INTO sessions "
        "(source, session_id, name, is_group, is_official, enabled, last_seen, last_active) "
        "VALUES (?, ?, ?, ?, ?, 0, ?, ?) "
        "ON CONFLICT(source, session_id) DO UPDATE SET "
        "name = excluded.name, is_group = excluded.is_group, "
        "is_official = excluded.is_official, last_seen = excluded.last_seen, "
        "last_active = excluded.last_active",
        (
            source,
            session_id,
            name,
            1 if is_group else 0,
            1 if is_official else 0,
            now,
            last_active_at,
        ),
    )
    await db.commit()


async def get_all_sessions() -> list[SessionRow]:
    db = await get_db()
    rows = await _fetchall(db, "SELECT * FROM sessions ORDER BY name")
    return [cast(SessionRow, dict(row)) for row in rows]


async def get_enabled_sessions(source: str) -> list[SessionRow]:
    db = await get_db()
    rows = await _fetchall(
        db,
        "SELECT * FROM sessions WHERE source = ? AND enabled = 1 ORDER BY name",
        (source,),
    )
    return [cast(SessionRow, dict(row)) for row in rows]


async def toggle_session(source: str, session_id: str) -> SessionRow | None:
    db = await get_db()
    await db.execute(
        "UPDATE sessions SET enabled = CASE WHEN enabled = 1 THEN 0 ELSE 1 END "
        "WHERE source = ? AND session_id = ?",
        (source, session_id),
    )
    row = await _fetchone(
        db,
        "SELECT * FROM sessions WHERE source = ? AND session_id = ?",
        (source, session_id),
    )
    if row and row["enabled"] == 1:
        # 启用即回填：清空会话水位（NULL = 待回填），下次 poll 按
        # BACKFILL_HOURS 窗口回填一次，之后转入增量轮询。
        await db.execute(
            "UPDATE sessions SET last_poll_ts = NULL "
            "WHERE source = ? AND session_id = ?",
            (source, session_id),
        )
    await db.commit()
    return cast(SessionRow, dict(row)) if row else None


# ── Categories ──


async def get_categories() -> list[CategoryDetail]:
    """全部分类类别（含各类别下 items 卡片数），按 id 排序。"""
    db = await get_db()
    rows = await _fetchall(
        db,
        "SELECT c.*, (SELECT COUNT(*) FROM items i WHERE i.category = c.name) AS item_count "
        "FROM categories c ORDER BY c.id",
    )
    return [cast(CategoryDetail, dict(row)) for row in rows]


async def get_enabled_categories() -> list[CategoryRow]:
    """启用的分类类别（AI 分类构建 system prompt 用）。"""
    db = await get_db()
    rows = await _fetchall(
        db, "SELECT * FROM categories WHERE enabled = 1 ORDER BY id"
    )
    return [cast(CategoryRow, dict(row)) for row in rows]


async def get_enabled_category_colors() -> list[CategoryColor]:
    """启用类别的 name/color（/api/items 的 allCategories 携带，前端据此
    判定类别存在性并给卡片着色）。与计数无关，含 0 卡片类别。"""
    db = await get_db()
    rows = await _fetchall(
        db, "SELECT name, color FROM categories WHERE enabled = 1 ORDER BY id"
    )
    return [cast(CategoryColor, dict(row)) for row in rows]


async def get_disabled_category_names() -> list[str]:
    """停用的类别名（列表查询由后端统一排除，接口仍保留该字段供客户端参考）。"""
    db = await get_db()
    rows = await _fetchall(
        db, "SELECT name FROM categories WHERE enabled = 0 ORDER BY id"
    )
    return [row["name"] for row in rows]


async def insert_category(
    name: str, prompt: str, color: str, enabled: int = 1
) -> CategoryRow:
    """新增分类类别；重名抛 aiosqlite.IntegrityError（由 server 转 409）。"""
    db = await get_db()
    now = datetime.now(UTC).isoformat()
    async with _cursor(
        db,
        "INSERT INTO categories (name, prompt, color, enabled, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (name, prompt, color, enabled, now),
    ) as cursor:
        cat_id = cursor.lastrowid
    await db.commit()
    if cat_id is None:
        raise RuntimeError("Failed to get inserted category id")
    row = await _get_category(db, cat_id)
    if row is None:
        raise RuntimeError(f"Category {cat_id} missing after insert")
    return row


async def update_category(
    cat_id: int,
    name: str | None = None,
    prompt: str | None = None,
    color: str | None = None,
) -> CategoryRow | None:
    """更新类别名称/描述/颜色；返回新行，不存在返回 None。

    改名时同一事务内同步 items.category（旧名→新名），避免侧边栏聚合分裂。
    重名抛 aiosqlite.IntegrityError（由 server 转 409）。
    """
    db = await get_db()
    row = await _fetchone(
        db, "SELECT * FROM categories WHERE id = ?", (cat_id,)
    )
    if not row:
        return None
    old_name = row["name"]
    try:
        if name is not None:
            await db.execute("UPDATE categories SET name = ? WHERE id = ?", (name, cat_id))
            if name != old_name:
                await db.execute(
                    "UPDATE items SET category = ? WHERE category = ?",
                    (name, old_name),
                )
        if prompt is not None:
            await db.execute(
                "UPDATE categories SET prompt = ? WHERE id = ?", (prompt, cat_id)
            )
        if color is not None:
            await db.execute(
                "UPDATE categories SET color = ? WHERE id = ?", (color, cat_id)
            )
    except Exception:
        # 改名 + items 同步是两步写：中途失败若不回滚，悬挂事务被后续
        # commit 收尾后会出现"类别已改、卡片未跟上"的孤儿卡片
        await db.rollback()
        raise
    await db.commit()
    return await _get_category(db, cat_id)


async def toggle_category(cat_id: int) -> CategoryRow | None:
    """翻转类别启用状态；返回新行，不存在返回 None。"""
    db = await get_db()
    await db.execute(
        "UPDATE categories SET enabled = CASE WHEN enabled = 1 THEN 0 ELSE 1 END "
        "WHERE id = ?",
        (cat_id,),
    )
    await db.commit()
    return await _get_category(db, cat_id)


async def delete_category(
    cat_id: int, purge_items: bool
) -> tuple[CategoryRow | None, list[str]]:
    """删除类别；返回 (被删行, 级联删除的 item_id 列表)。

    purge_items=True 时级联删除该类别全部卡片：顺序为 item_embeddings →
    raw_messages → items → categories（item_embeddings 无外键须显式删；
    raw 先于 items，因子查询依赖 items 中尚未删除的行）。不动
    processed_messages，避免消息在回填窗口内被重新处理（同 purge_expired_ignored）。
    """
    db = await get_db()
    row = await _fetchone(
        db, "SELECT * FROM categories WHERE id = ?", (cat_id,)
    )
    if not row:
        return None, []
    deleted_ids: list[str] = []
    try:
        if purge_items:
            rows = await _fetchall(
                db, "SELECT id FROM items WHERE category = ?", (row["name"],)
            )
            deleted_ids = [r["id"] for r in rows]
            if deleted_ids:
                placeholders = ",".join("?" for _ in deleted_ids)
                await db.execute(
                    f"DELETE FROM item_embeddings WHERE item_id IN ({placeholders})",
                    tuple(deleted_ids),
                )
                await db.execute(
                    f"DELETE FROM raw_messages WHERE (source, msg_id) IN ("
                    f"SELECT source, source_msg_id FROM items WHERE id IN ({placeholders}))",
                    tuple(deleted_ids),
                )
                await db.execute(
                    f"DELETE FROM items WHERE id IN ({placeholders})", tuple(deleted_ids)
                )
        await db.execute("DELETE FROM categories WHERE id = ?", (cat_id,))
    except Exception:
        # 级联删除是三表多步写：中途失败若不回滚，悬挂事务被后续 commit
        # 收尾后会留下删了一半的卡片（如 embeddings 删了但 items 还在）
        await db.rollback()
        raise
    await db.commit()
    return cast(CategoryRow, dict(row)), deleted_ids


async def _get_category(db: aiosqlite.Connection, cat_id: int) -> CategoryRow | None:
    """按 id 读取类别行（写操作后回读返回用）。"""
    row = await _fetchone(db, "SELECT * FROM categories WHERE id = ?", (cat_id,))
    return cast(CategoryRow, dict(row)) if row else None


# ── Contacts ──


async def upsert_contact(source: str, sender_id: str, display_name: str) -> None:
    db = await get_db()
    await db.execute(
        "INSERT OR REPLACE INTO contacts (source, sender_id, display_name) VALUES (?, ?, ?)",
        (source, sender_id, display_name),
    )
    await db.commit()


# ── Raw Messages ──


# raw_messages 单条 SQL 插入的行数上限：SQLite 单语句变量上限 32766，
# 每行 9 个占位符，500 行 = 4500 参数，远低于上限且留足余量
# （整批一次拼接在回填超 ~3640 条时会抛 "too many SQL variables"）。
_RAW_INSERT_CHUNK = 500


async def bulk_insert_raw_messages(messages: list[RawMsgInput]) -> None:
    """批量写入 raw_messages（单事务）；INSERT OR IGNORE 幂等。

    分块 executemany 落库：避免单条 SQL 拼接全部行触发 SQLite 变量上限
    （32766 个，超过 ~3640 行即报错导致整轮回填失败）。
    """
    if not messages:
        return
    db = await get_db()
    for start in range(0, len(messages), _RAW_INSERT_CHUNK):
        chunk = messages[start : start + _RAW_INSERT_CHUNK]
        params: list[Any] = []
        for m in chunk:
            params.extend(
                [
                    m["source"],
                    m["msg_id"],
                    m["session_id"],
                    m["group_name"],
                    m["sender_id"],
                    m["sender_name"],
                    m["content"],
                    m["timestamp"],
                    m.get("article_url", ""),
                ]
            )
        cursor = await db.executemany(
            "INSERT OR IGNORE INTO raw_messages "
            "(source, msg_id, session_id, group_name, sender_id, sender_name, content, timestamp, article_url) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [params[i : i + 9] for i in range(0, len(params), 9)],
        )
        await cursor.close()
    await db.commit()


_OCR_MARKER_LINE_RE = re.compile(r"^\[(?:OCR|图片 \d+ OCR 结果)\]$")

# weflow 图片 raw 占位符为 [图片]，qqflow 为 [image]；两者都在上下文里
# 回填 items.source_quote 的 OCR 正文，保持一致的展示行为。
_IMAGE_PLACEHOLDER_RE = re.compile(r"^\[(?:图片|image)\]$")


def _context_content(content: str, source_quote: str) -> str:
    """上下文展示用内容：图片占位消息回填 OCR 原文，去掉 [OCR]/[图片 N OCR 结果] 标记行。

    OCR 消息的 content 形如 "[OCR]\\n[图片 1 OCR 结果]\\n正文…"，直接在原文列表里
    显示会只剩标记行；这里回填 items.source_quote 并剥掉标记，让上下文高亮行展示真实正文。
    """
    if not source_quote or not _IMAGE_PLACEHOLDER_RE.match(content.strip()):
        return content
    lines = [ln.strip() for ln in source_quote.split("\n")]
    meaningful = [ln for ln in lines if ln and not _OCR_MARKER_LINE_RE.match(ln)]
    return "\n".join(meaningful) if meaningful else source_quote


async def get_context_messages(
    source: str, session_id: str, around_time: int, target_msg_id: str = ""
) -> list[ContextMsg]:
    """窗口内上下文消息；target_msg_id 提供时保证目标消息必达。

    高活跃会话里 ±1h 窗口的消息可能远超 30 条，旧的「窗口内最早 30 条」
    会把目标消息（卡片 msg_time 常在窗口中部）截掉，前端据此高亮落空。
    改为以 around_time 为锚点的双向取数：锚点前最近 15 条 + 锚点起（含
    目标消息）最近 15 条，列表稳定 ≤30 条且目标必然入选；target_msg_id
    另作兜底，锚点与消息时间戳偶发不一致时按 msg_id 补回目标行。
    """
    db = await get_db()
    since = around_time - 3600
    until = around_time + 3600
    select = """SELECT COALESCE(
                 CASE WHEN s.is_group = 1
                       AND r.sender_name <> ''
                       AND r.sender_name <> r.sender_id
                      THEN r.sender_name END,
                 c.display_name,
                 r.sender_name,
                 r.sender_id
               ) as sender,
               r.content, r.timestamp as time, r.msg_id, r.article_url,
               i.source_quote
           FROM raw_messages r
           LEFT JOIN contacts c ON r.source = c.source AND r.sender_id = c.sender_id
           LEFT JOIN sessions s ON r.source = s.source AND r.session_id = s.session_id
           LEFT JOIN items i ON i.source = r.source AND i.source_msg_id = r.msg_id
           WHERE r.source = ? AND r.session_id = ? AND r.timestamp BETWEEN ? AND ?"""
    before_rows = [
        dict(r)
        for r in await _fetchall(
            db,
            select + " AND r.timestamp < ? ORDER BY r.timestamp DESC LIMIT 15",
            (source, session_id, since, until, around_time),
        )
    ]
    after_rows = [
        dict(r)
        for r in await _fetchall(
            db,
            select + " AND r.timestamp >= ? ORDER BY r.timestamp ASC LIMIT 15",
            (source, session_id, since, until, around_time),
        )
    ]
    # 前半段倒序取最近 15 条，合并前反转回时间升序；目标消息（timestamp == 锚点）
    # 位于后半段首部，必然入选
    rows = before_rows[::-1] + after_rows
    if target_msg_id and not any(r["msg_id"] == target_msg_id for r in rows):
        target = await _fetchone(
            db,
            select + " AND r.msg_id = ?",
            (source, session_id, since, until, target_msg_id),
        )
        if target is not None:
            rows.append(dict(target))
            # 兜底补入后超限：丢弃离锚点最远的一条非目标消息（并列丢较晚者），
            # 保持 ≤30 且不丢刚补回的目标行
            if len(rows) > 30:
                farthest = max(
                    (r for r in rows if r["msg_id"] != target_msg_id),
                    key=lambda r: (abs(r["time"] - around_time), -r["time"]),
                )
                rows.remove(farthest)
            rows.sort(key=lambda r: r["time"])
    out: list[ContextMsg] = []
    for d in rows:
        d["content"] = _context_content(d["content"], d.get("source_quote") or "")
        d.pop("source_quote", None)
        out.append(cast(ContextMsg, d))
    return out


# ── Dedup helpers ──


async def merge_source_group(item_id: str, new_group: str) -> None:
    """把消息来源群名并入存活卡的 source_group（逗号分隔、精确匹配去重）。

    精确去重而非子串匹配（C3）：群名互为子串（如"我们四个" vs "我们四个2"、
    "篮球社" vs "篮球社团招新群"）时不再误判"已包含"而丢失来源记录。
    """
    db = await get_db()
    row = await _fetchone(
        db, "SELECT source_group FROM items WHERE id = ?", (item_id,)
    )
    if not row:
        return
    existing = row["source_group"] or ""
    groups = [g.strip() for g in existing.split(",") if g.strip()]
    if new_group not in groups:
        groups.append(new_group)
        merged = ", ".join(groups)
        await db.execute(
            "UPDATE items SET source_group = ? WHERE id = ?", (merged, item_id)
        )
        await db.commit()


async def get_all_item_texts() -> list[ItemText]:
    """全量已处理卡片（is_verified >= 0），供去重缓存预热。

    source_quote 返回真实原文列（不拼接其它字段）：与运行期查询/入库共用
    `_embedding_text(title, source_quote)` 公式，保证重启加载的向量与实时
    向量处于同一语义空间（此前拼接 AI 摘要导致跨重启余弦召回失真）。
    """
    db = await get_db()
    rows = await _fetchall(
        db,
        "SELECT id, source, title, "
        "COALESCE(content_hash, '') as content_hash, COALESCE(image_urls, '') as image_urls, "
        "COALESCE(source_quote, '') as source_quote "
        "FROM items WHERE is_verified >= 0",
    )
    return [cast(ItemText, dict(row)) for row in rows]


async def load_embeddings(model: str) -> dict[str, list[float]]:
    """读取指定模型的全部已存向量，返回 {item_id: embedding}。

    走向量专用连接（_embed_lock 串行）：不占用主连接，也不与
    向量写入交错产生活动语句。
    """
    db = await get_embed_db()
    async with _embed_lock:
        rows = await _fetchall(
            db,
            "SELECT item_id, embedding FROM item_embeddings WHERE model = ?",
            (model,),
        )
    return {
        row["item_id"]: json.loads(row["embedding"]) for row in rows
    }


async def upsert_embeddings(rows: list[tuple[str, str, list[float]]]) -> None:
    """批量写入向量（INSERT OR REPLACE，按 item_id 覆盖旧模型的向量）。

    独立 embed 连接 + _embed_lock 串行 + 游标先 close 再 commit：
    杜绝"活动语句未终结就 COMMIT"导致的 OperationalError
    （cannot commit transaction - SQL statements in progress）。
    跨连接写锁竞争（database is locked）按指数退避重试（最多 3 次）。
    """
    if not rows:
        return
    db = await get_embed_db()
    now = datetime.now(UTC).isoformat()
    params = [
        (item_id, model, json.dumps(emb), now) for item_id, model, emb in rows
    ]
    for attempt in range(3):
        async with _embed_lock:
            cursor = await db.executemany(
                "INSERT OR REPLACE INTO item_embeddings "
                "(item_id, model, embedding, created_at) VALUES (?, ?, ?, ?)",
                params,
            )
            await cursor.close()
            try:
                await db.commit()
                return
            except sqlite3.OperationalError as e:
                if "statements in progress" not in str(e) and "locked" not in str(e):
                    raise
                if attempt >= 2:
                    raise
        await asyncio.sleep(0.1 * (attempt + 1))
