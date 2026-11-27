"""RAG 库层 — rag_chunks / rag_chunk_embeddings / rag_fts / rag_meta / rag_skipped 五表数据访问。

rag 表是 raw_messages 的派生索引：插件自管建表（不在核心 EXPECTED_SCHEMA
内，validate_schema 取交集校验不受影响），不复制事实源语义，可随时重建。
所有函数接受显式连接（生产传核心 get_db()/get_embed_db()，测试传内存库）；
游标纪律：try/finally close、executemany 后必须 close 再继续。

FTS 说明：trigram 分词对中文可用但仅能匹配 >=3 字符「词元」——查询含任一
更短词元时整体走 LIKE 逐词 AND 兜底（本地库规模下可接受，与 calendar
「取回后内存过滤」同款取舍）。作用域过滤对启用会话强制生效（group_only
追加群聊限定），与引擎侧白名单双保险。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, fields

import aiosqlite
import numpy as np

# LIKE 兜底路径的转义字符（chr(92) = 反斜杠；避免源码内裸反斜杠）
_BS = chr(92)


async def fetch_all(
    db: aiosqlite.Connection, sql: str, params: object = ()
) -> list[aiosqlite.Row]:
    """查全部行并保证关闭游标（收敛 try/finally 样板，游标纪律单点保障）。"""

    cursor = await db.execute(sql, params)
    try:
        return list(await cursor.fetchall())
    finally:
        await cursor.close()


async def fetch_one(
    db: aiosqlite.Connection, sql: str, params: object = ()
) -> aiosqlite.Row | None:
    """查首行并保证关闭游标（无行返回 None）。"""

    cursor = await db.execute(sql, params)
    try:
        return await cursor.fetchone()
    finally:
        await cursor.close()


@dataclass
class ChunkRow:
    """一条已索引消息（与 raw_messages 行同源，item_id 为产出的卡片可空）。

    sender_id 与 raw_messages 同源：只用于本地的身份计算（假名化时把同一个人
    认成同一个身份），不进 prompt、不进引用元数据。它排在最后且带默认值——
    旧库/旧数据构造 ChunkRow 时不传这个字段仍能通过，不必同步改所有调用点。
    """

    source: str
    msg_id: str
    session_id: str
    group_name: str
    sender_name: str
    msg_time: int  # 秒级 epoch（raw_messages.timestamp 原值）
    content: str
    item_id: str = ""
    sender_id: str = ""


# 读取行的唯一合法投影面：查询行 → ChunkRow 只认这些名字。
_CHUNK_FIELD_NAMES = tuple(field.name for field in fields(ChunkRow))


def chunk_from_row(row: aiosqlite.Row | Mapping[str, object]) -> ChunkRow:
    """把查询行投影成 ChunkRow：只取 dataclass 已知字段，缺列走默认值。

    为什么不能沿用 `ChunkRow(**dict(row))`：`SELECT c.*` 的列集合随表结构变化，
    rag_chunks 后来追加了 sender_id，多出的列会让 dataclass 构造直接 TypeError
    ——FTS 腿与向量腿会同时哑掉。按已知字段投影后，旧 shape（列少，sender_id
    取默认空串）与新 shape（列多，多余列被丢弃）都能读。

    这是回滚兼容的一半：另一半是保留追加列本身。回滚必须回到带本读取器的版本，
    直接退到更早的基线仍会 TypeError——旧基线假设 `c.*` 的列恰好等于 dataclass
    字段，那正是本函数要消掉的假设。
    """
    data = dict(row)
    return ChunkRow(
        **{name: data[name] for name in _CHUNK_FIELD_NAMES if name in data}
    )


_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS rag_chunks (
        source       TEXT NOT NULL,
        msg_id       TEXT NOT NULL,
        session_id   TEXT NOT NULL,
        group_name   TEXT NOT NULL DEFAULT '',
        sender_name  TEXT NOT NULL DEFAULT '',
        msg_time     INTEGER NOT NULL DEFAULT 0,
        content      TEXT NOT NULL,
        item_id      TEXT NOT NULL DEFAULT '',
        sender_id    TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (source, msg_id)
    )""",
    """CREATE TABLE IF NOT EXISTS rag_chunk_embeddings (
        source      TEXT NOT NULL,
        msg_id      TEXT NOT NULL,
        model       TEXT NOT NULL,
        embedding   TEXT NOT NULL,
        created_at  TEXT NOT NULL,
        PRIMARY KEY (source, msg_id)
    )""",
    """CREATE TABLE IF NOT EXISTS rag_meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )""",
    # 「永不入索引」记账（纯占位符内容）：这些行永远满足回填反连接
    # （rag_chunks 无对应行）却永远不被消费，不记账会让它们每轮重新
    # 占满 LIMIT 预算窗口，令更早的真实消息饿死。
    # raw_messages 内容不可变，跳过判定跨模型/重建恒成立，无需失效机制。
    """CREATE TABLE IF NOT EXISTS rag_skipped (
        source     TEXT NOT NULL,
        msg_id     TEXT NOT NULL,
        PRIMARY KEY (source, msg_id)
    )""",
]


# 回填语句单源：迁移与幂等重跑共用同一条 UPDATE，避免两处手抄漂移。
_SENDER_ID_BACKFILL_SQL = (
    "UPDATE rag_chunks SET sender_id = ("
    "SELECT COALESCE(r.sender_id, '') FROM raw_messages r "
    "WHERE r.source = rag_chunks.source AND r.msg_id = rag_chunks.msg_id) "
    "WHERE sender_id = '' AND EXISTS ("
    "SELECT 1 FROM raw_messages r "
    "WHERE r.source = rag_chunks.source AND r.msg_id = rag_chunks.msg_id)"
)


async def ensure_rag_schema(db: aiosqlite.Connection) -> None:
    """创建 rag 五表中的四张普通表（chunks/embeddings/meta/skipped 幂等；FTS 虚拟表由 ensure_fts 惰性创建）。

    同时把旧库的 rag_chunks 补到当前 shape（见 `_migrate_sender_id`），并把缺失
    的 sender_id 从事实源回填——迁移与回填在同一个提交里完成，中途失败整体回滚、
    重启可重跑。不重建 FTS/向量表，也不改核心库的 schema/version 合同：rag 表是
    派生索引，本函数只追加自己这张表的一列。
    """

    for ddl in _SCHEMA:
        await db.execute(ddl)
    await _migrate_sender_id(db)
    await db.commit()


async def _migrate_sender_id(db: aiosqlite.Connection) -> None:
    """给旧库补 `sender_id` 列并按 `(source, msg_id)` 回填；幂等且整体原子。

    为什么先 `PRAGMA table_info` 再 ALTER：`ALTER TABLE ... ADD COLUMN` 没有
    `IF NOT EXISTS` 形态，重复执行会抛 "duplicate column name"，而 ensure_schema
    在每次启动都会跑——那样第二次启动就再也起不来了。缺列才 ALTER 让本函数可以
    被无脑重复调用（回归见 tests/test_rag_plugin.py 的
    test_sender_id_migration_is_idempotent）。

    为什么必须显式 SAVEPOINT：aiosqlite 默认隔离级别下，DDL 与 DML 各自开启
    隐式事务，外层的 commit 无法把它们合并成一个原子单元——回填失败回滚时
    ALTER 已经生效，库会停在「列在、值全空、且下次启动因列存在而永远不再
    回填」的死状态。SAVEPOINT 把两步包进一个可整体回滚的单元，失败恢复到
    「列不存在」（旧 shape 可读）或「列存在且回填未完成」（下次启动重试）两种
    单一状态之一，绝不出现中间态。

    为什么回填只填空串：非空值可能是运行期从原始消息带下来的更准的值（回填
    之后消息可能被删除，raw 里已经没有这一行）。覆盖非空值等于用「现在查不到」
    擦掉「当时查到了」。

    为什么 raw 缺行/缺 ID 时保持空串而不是用 sender_name 猜：猜出来的 ID 会把
    两个同名的人合并成同一个身份，随后的假名化就会把 A 的化名贴到 B 的证据行上
    ——这比没有身份更糟，因为它看起来是对的。空串的代价只是退化为「按会话 +
    显示名」的 best-effort 身份。
    """
    columns = {
        row["name"] for row in await fetch_all(db, "PRAGMA table_info(rag_chunks)")
    }
    if "sender_id" in columns:
        # 列已存在：只做回填（幂等），仍包进 SAVEPOINT——回填失败同样整体回滚。
        await db.execute("SAVEPOINT sender_id_backfill")
        try:
            await db.execute(_SENDER_ID_BACKFILL_SQL)
        except BaseException:
            await db.execute("ROLLBACK TO sender_id_backfill")
            await db.execute("RELEASE sender_id_backfill")
            raise
        await db.execute("RELEASE sender_id_backfill")
        return
    await db.execute("SAVEPOINT sender_id_migration")
    try:
        await db.execute(
            "ALTER TABLE rag_chunks ADD COLUMN sender_id TEXT NOT NULL DEFAULT ''"
        )
        await db.execute(_SENDER_ID_BACKFILL_SQL)
    except BaseException:
        # CancelledError 也是 BaseException：取消同样不能留下半迁移状态。
        await db.execute("ROLLBACK TO sender_id_migration")
        await db.execute("RELEASE sender_id_migration")
        raise
    await db.execute("RELEASE sender_id_migration")


# ---------------------------------------------------------------- 元信息 --


async def get_meta(db: aiosqlite.Connection, key: str) -> str | None:
    row = await fetch_one(db, "SELECT value FROM rag_meta WHERE key = ?", (key,))
    return row["value"] if row is not None else None


async def set_meta(db: aiosqlite.Connection, key: str, value: str) -> None:
    cursor = await db.execute(
        "INSERT INTO rag_meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    await cursor.close()
    await db.commit()


# ---------------------------------------------------------------- skipped --


async def mark_skipped(db: aiosqlite.Connection, keys: list[tuple[str, str]]) -> None:
    """登记「永不入索引」的行（纯占位符内容），令回填反连接跳过它们。

    跳过判定由引擎 `_indexable`（masking.PLACEHOLDER_ONLY_RE 单源）作出；
    raw_messages 内容不可变，登记恒成立。
    """

    if not keys:
        return
    cursor = await db.executemany(
        "INSERT OR IGNORE INTO rag_skipped(source, msg_id) VALUES(?, ?)", keys
    )
    await cursor.close()
    await db.commit()


# ------------------------------------------------------------------- FTS --


def _fts_ddl(tokenizer: str) -> str:
    return (
        "CREATE VIRTUAL TABLE IF NOT EXISTS rag_fts USING fts5("
        "content, sender_name, group_name, source UNINDEXED, msg_id UNINDEXED, "
        "tokenize='" + tokenizer + "')"
    )


async def ensure_fts(db: aiosqlite.Connection) -> bool:
    """惰性创建 FTS 表：trigram 优先（中文子串可用），失败降级 unicode61，
    再失败返回 False（纯向量模式）。

    表已存在时从 sqlite_master 反解真实 tokenizer 回写 meta——IF NOT EXISTS
    会短路 CREATE，历史 unicode61 表不能被误报成 trigram（口径失真）。
    """

    existing = await fetch_one(
        db, "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'rag_fts'"
    )
    if existing is not None:
        m = re.search(r"tokenize='([^']+)'", existing["sql"] or "")
        tok = m.group(1) if m else "default"
        await set_meta(db, "fts_tokenizer", tok)
        return True
    for tokenizer in ("trigram", "unicode61"):
        try:
            await db.execute(_fts_ddl(tokenizer))
            await db.commit()
        except aiosqlite.OperationalError:
            continue
        await set_meta(db, "fts_tokenizer", tokenizer)
        return True
    await set_meta(db, "fts_tokenizer", "")
    return False


def _like_escape(text: str) -> str:
    return (
        text.replace(_BS, _BS + _BS)
        .replace("%", _BS + "%")
        .replace("_", _BS + "_")
    )


def _tokens(query: str) -> list[str]:
    """查询串切词（单源）：按空白切分；无空白时整串作单词元。

    MATCH 构建、trigram 路由判据与 LIKE 兜底三处共用同一口径，避免各自
    切词导致「按 A 切分路由、按 B 切分检索」的错配。
    """

    parts = [p for p in query.split() if p]
    if parts:
        return parts
    stripped = query.strip()
    return [stripped] if stripped else []


def _fts_query(query: str) -> str:
    """构建 MATCH 词元：切词后各自双引号包裹并转义内部引号。"""

    return " ".join('"' + p.replace('"', '""') + '"' for p in _tokens(query))


def _min_token_len(query: str) -> int:
    """查询串切词后的最短词元长度（trigram 路由判据）。"""

    return min((len(p) for p in _tokens(query)), default=0)


def scope_sql(
    enabled_group_only: bool,
    session_id: str | None = None,
    alias: str = "c",
) -> tuple[str, list[object]]:
    """会话白名单过滤片段（单源；alias 指定被过滤行的表别名）。

    启用会话恒为前提；group_only 追加 is_group=1。session_id 进一步收窄。
    检索侧作用于 chunks（别名 c），回填侧作用于 raw_messages（别名 r）——
    两条路径共用本函数，隐私边界只有一处定义。返回 (SQL 片段, 绑定参数)。
    """

    clause = (
        f" AND EXISTS (SELECT 1 FROM sessions s WHERE s.source = {alias}.source"
        f" AND s.session_id = {alias}.session_id AND s.enabled = 1"
    )
    params: list[object] = []
    if enabled_group_only:
        clause += " AND s.is_group = 1"
    if session_id is not None:
        clause += " AND s.session_id = ?"
        params.append(session_id)
    clause += ")"
    return clause, params


async def sync_fts(db: aiosqlite.Connection, rows: list[ChunkRow]) -> None:
    """同步 FTS 行（先删后插保幂等；表未启用时静默跳过）。"""

    if not await get_meta(db, "fts_tokenizer"):
        return
    for row in rows:
        cursor = await db.execute(
            "DELETE FROM rag_fts WHERE source = ? AND msg_id = ?",
            (row.source, row.msg_id),
        )
        await cursor.close()
        cursor = await db.execute(
            "INSERT INTO rag_fts(content, sender_name, group_name, source, msg_id) "
            "VALUES(?, ?, ?, ?, ?)",
            (row.content, row.sender_name, row.group_name, row.source, row.msg_id),
        )
        await cursor.close()
    await db.commit()


async def fts_search(
    db: aiosqlite.Connection,
    query: str,
    limit: int,
    session_id: str | None = None,
    enabled_group_only: bool = False,
) -> list[ChunkRow]:
    """关键词召回：全部词元 >=3 字走 FTS MATCH（rank 排序），否则 LIKE 兜底。

    trigram 最小匹配长度按「词元」计——整串够长但含 2 字词（「开会 吗」）
    时 FTS 会零命中，必须整体降级；多词兜底为逐词 AND 链（整串含空格会
    必然失配）。
    """

    cleaned = query.strip()
    if not cleaned:
        return []
    use_fts = _min_token_len(cleaned) >= 3 and bool(await get_meta(db, "fts_tokenizer"))
    order = " ORDER BY rank LIMIT ?"
    params: list[object]
    if use_fts:
        # 实测注意：JOIN 场景下 MATCH 只认 FTS 原表名，用别名会报 no such column
        sql = (
            "SELECT c.* FROM rag_fts f "
            "JOIN rag_chunks c ON c.source = f.source AND c.msg_id = f.msg_id "
            "WHERE rag_fts MATCH ?"
        )
        params = [_fts_query(cleaned)]
    else:
        tokens = _tokens(cleaned)
        like_clause = " AND ".join(
            "c.content LIKE '%' || ? || '%' ESCAPE ?" for _ in tokens
        )
        sql = "SELECT c.* FROM rag_chunks c WHERE " + like_clause
        params = []
        for tok in tokens:
            params.extend([_like_escape(tok), _BS])
        order = " ORDER BY c.msg_time DESC LIMIT ?"
    scope_clause, scope_params = scope_sql(enabled_group_only, session_id)
    sql += scope_clause
    params.extend(scope_params)
    sql += order
    params.append(limit)
    rows = await fetch_all(db, sql, params)
    return [chunk_from_row(r) for r in rows]


# ---------------------------------------------------------------- 写入侧 --


async def upsert_chunks(
    db: aiosqlite.Connection,
    rows: list[ChunkRow],
    *,
    sender_id_fill_only: bool = False,
) -> None:
    """批量 upsert 索引行；回填仅补空身份，实时重处理仍覆盖为最新身份。"""

    if not rows:
        return
    cursor = await db.executemany(
        "INSERT INTO rag_chunks(source, msg_id, session_id, group_name, "
        "sender_name, msg_time, content, item_id, sender_id) "
        "VALUES(?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(source, msg_id) DO UPDATE SET "
        "session_id=excluded.session_id, group_name=excluded.group_name, "
        "sender_name=excluded.sender_name, msg_time=excluded.msg_time, "
        "content=excluded.content, "
        # item_id 条件覆盖：回填（backfill）重建的 ChunkRow 不携带 item_id
        # （空串），无条件覆盖会把运行期已写入的「消息→卡片」引用永久清空
        # 空串时保留既有值，非空时（消息重处理）才覆盖。
        "item_id=CASE WHEN excluded.item_id='' THEN rag_chunks.item_id "
        "ELSE excluded.item_id END, "
        # 回填在锁外等待嵌入，快照中的非空身份也可能落后于实时重索引；
        # 只补空值才能保住实时身份，实时写入则仍允许非空新值覆盖旧值。
        # 两种模式都不让空输入擦掉已知身份，避免假名化退化为显示名猜测。
        "sender_id=CASE WHEN excluded.sender_id='' "
        "OR (? AND rag_chunks.sender_id<>'') THEN rag_chunks.sender_id "
        "ELSE excluded.sender_id END",
        [
            (
                r.source, r.msg_id, r.session_id, r.group_name,
                r.sender_name, r.msg_time, r.content, r.item_id, r.sender_id,
                sender_id_fill_only,
            )
            for r in rows
        ],
    )
    await cursor.close()
    await db.commit()


async def upsert_embeddings(
    db: aiosqlite.Connection,
    items: list[tuple[str, str]],
    vectors: list[list[float]],
    model: str,
    created_at: str,
) -> None:
    """批量写入向量（model 变更后旧行自然失配，由回填触发整体重嵌入）。"""

    if not items:
        return
    cursor = await db.executemany(
        "INSERT INTO rag_chunk_embeddings(source, msg_id, model, embedding, created_at) "
        "VALUES(?,?,?,?,?) "
        "ON CONFLICT(source, msg_id) DO UPDATE SET "
        "model=excluded.model, embedding=excluded.embedding, "
        "created_at=excluded.created_at",
        [(s, m, model, json.dumps(v), created_at) for (s, m), v in zip(items, vectors)],
    )
    await cursor.close()
    await db.commit()


# ---------------------------------------------------------------- 读取侧 --


async def fetch_new_embeddings(
    db: aiosqlite.Connection,
    model: str,
    since_created_at: str,
    enabled_group_only: bool = False,
) -> tuple[list[dict], str]:
    """按 created_at 水位增量拉取当前模型的向量行（供引擎缓存增量填充）。

    水位比较用闭区间 >=：秒级精度下同秒提交的行不会被永久跳过
    （entries 按 key 覆盖天然幂等，代价只是边界行重复拉取一次）。
    返回 (raw_rows, max_created_at)；raw_rows 含 chunks 字段 + embedding
    原文，JSON 解析由调用方放到工作线程（避免冻结事件循环）。
    """

    sql = (
        "SELECT c.source, c.msg_id, c.session_id, c.group_name, c.sender_name,"
        " c.msg_time, c.content, c.item_id, c.sender_id, e.embedding, e.created_at"
        " FROM rag_chunk_embeddings e"
        " JOIN rag_chunks c ON c.source = e.source AND c.msg_id = e.msg_id"
        " WHERE e.model = ? AND e.created_at >= ?"
    )
    params: list[object] = [model, since_created_at]
    scope_clause, scope_params = scope_sql(enabled_group_only)
    sql += scope_clause
    params.extend(scope_params)
    rows = [dict(r) for r in await fetch_all(db, sql, params)]
    max_created = max((r["created_at"] for r in rows), default=since_created_at)
    return rows, max_created


def parse_embedding_rows(
    raw_rows: list[dict],
) -> tuple[list[tuple[ChunkRow, np.ndarray]], list[tuple[str, str]]]:
    """纯函数：解析向量行为 float32 ndarray（可放工作线程执行）。

    返回 (entries, bad_keys)；脏 JSON 行不进 entries，其键返回给调用方
    删除（回填反连接下一轮自动重嵌入）。
    """

    entries: list[tuple[ChunkRow, np.ndarray]] = []
    bad_keys: list[tuple[str, str]] = []
    for r in raw_rows:
        key = (r["source"], r["msg_id"])
        try:
            vec = json.loads(r["embedding"])
            if not isinstance(vec, list):
                raise TypeError("embedding 非数组")
            floats = np.asarray(vec, dtype=np.float32)
        except (json.JSONDecodeError, TypeError, ValueError):
            bad_keys.append(key)
            continue
        # 与 FTS 腿同一条投影读取器：两条腿返回的身份字段必须同形（sender_id
        # 只加在一侧时，向量命中拿不到身份、只有 FTS 命中能假名化）。
        entries.append((chunk_from_row(r), floats))
    return entries, bad_keys


async def count_status(db: aiosqlite.Connection) -> dict[str, object]:
    """状态汇总（/api/rag/status 用）。"""

    out: dict[str, object] = {}
    for table in ("rag_chunks", "rag_chunk_embeddings"):
        row = await fetch_one(db, "SELECT COUNT(*) AS c FROM " + table)
        out[table] = row["c"] if row else 0
    out["fts_tokenizer"] = await get_meta(db, "fts_tokenizer") or ""
    return out


async def gc_orphans(
    db: aiosqlite.Connection, embed_db: aiosqlite.Connection | None = None
) -> int:
    """对账清理：不在 raw_messages/chunks 中的索引行级联清除。

    类别删除会级联清 raw_messages 但不发事件（最终一致），维护循环与
    reindex 经此对账。embed_db 提供时向量表在专用连接上清理（与核心
    get_embed_db 隔离约定一致；跨连接子查询读的是同库已提交快照，安全）。
    rag_skipped 一并对账（其宿主 raw 行消失后登记失去意义）。
    返回清理的总行数：先 COUNT 后 DELETE 显式计数——total_changes 差值
    会把共享连接上其它协程的无关写入一并计入，显著夸大返回值。
    """

    removed = 0
    orphan_pred_chunks = (
        " WHERE NOT EXISTS ("
        "SELECT 1 FROM raw_messages r WHERE r.source = rag_chunks.source "
        "AND r.msg_id = rag_chunks.msg_id)"
    )
    row = await fetch_one(db, "SELECT COUNT(*) AS c FROM rag_chunks" + orphan_pred_chunks)
    removed += int(row["c"]) if row else 0
    cursor = await db.execute("DELETE FROM rag_chunks" + orphan_pred_chunks)
    await cursor.close()
    # FTS 表惰性可选（纯向量模式下不存在），GC 先探测再清理
    if await get_meta(db, "fts_tokenizer"):
        row = await fetch_one(
            db,
            "SELECT COUNT(*) AS c FROM rag_fts WHERE NOT EXISTS ("
            "SELECT 1 FROM rag_chunks c WHERE c.source = rag_fts.source "
            "AND c.msg_id = rag_fts.msg_id)",
        )
        removed += int(row["c"]) if row else 0
        cursor = await db.execute(
            "DELETE FROM rag_fts WHERE NOT EXISTS ("
            "SELECT 1 FROM rag_chunks c WHERE c.source = rag_fts.source "
            "AND c.msg_id = rag_fts.msg_id)"
        )
        await cursor.close()
    row = await fetch_one(
        db,
        "SELECT COUNT(*) AS c FROM rag_skipped WHERE NOT EXISTS ("
        "SELECT 1 FROM raw_messages r WHERE r.source = rag_skipped.source "
        "AND r.msg_id = rag_skipped.msg_id)",
    )
    removed += int(row["c"]) if row else 0
    cursor = await db.execute(
        "DELETE FROM rag_skipped WHERE NOT EXISTS ("
        "SELECT 1 FROM raw_messages r WHERE r.source = rag_skipped.source "
        "AND r.msg_id = rag_skipped.msg_id)"
    )
    await cursor.close()
    await db.commit()
    if embed_db is not None:
        # 向量表在专用连接清理：不与主连接上管道/删除路径的半程事务互相提交
        row = await fetch_one(
            embed_db,
            "SELECT COUNT(*) AS c FROM rag_chunk_embeddings WHERE NOT EXISTS ("
            "SELECT 1 FROM rag_chunks c WHERE c.source = rag_chunk_embeddings.source "
            "AND c.msg_id = rag_chunk_embeddings.msg_id)",
        )
        removed += int(row["c"]) if row else 0
        cursor = await embed_db.execute(
            "DELETE FROM rag_chunk_embeddings WHERE NOT EXISTS ("
            "SELECT 1 FROM rag_chunks c WHERE c.source = rag_chunk_embeddings.source "
            "AND c.msg_id = rag_chunk_embeddings.msg_id)"
        )
        await cursor.close()
        await embed_db.commit()
    return removed
