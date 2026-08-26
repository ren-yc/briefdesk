"""RAG 库层 — rag_chunks / rag_chunk_embeddings / rag_fts 三表数据访问。

rag 三表是 raw_messages 的派生索引：插件自管建表（不在核心 EXPECTED_SCHEMA
内，validate_schema 取交集校验不受影响），不复制事实源语义，可随时重建。
所有函数接受显式连接（生产传核心 get_db()，测试传内存库）；游标纪律：
try/finally close、executemany 后必须 close 再继续。

FTS 说明：trigram 分词对中文可用但仅能匹配 >=3 字符查询串——更短的查询
自动走 rag_chunks 的 LIKE 子串兜底路径（本地库规模下可接受，与 calendar
「取回后内存过滤」同款取舍）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import aiosqlite

# LIKE 兜底路径的转义字符（chr(92) = 反斜杠；避免源码内裸反斜杠）
_BS = chr(92)


@dataclass
class ChunkRow:
    """一条已索引消息（与 raw_messages 行同源，item_id 为产出的卡片可空）。"""

    source: str
    msg_id: str
    session_id: str
    group_name: str
    sender_name: str
    msg_time: int  # 秒级 epoch（raw_messages.timestamp 原值）
    content: str
    item_id: str = ""


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
]


async def ensure_rag_schema(db: aiosqlite.Connection) -> None:
    """创建 rag 三张基础表（幂等；FTS 表由 ensure_fts 惰性创建）。"""

    for ddl in _SCHEMA:
        await db.execute(ddl)
    await db.commit()


# ---------------------------------------------------------------- 元信息 --


async def get_meta(db: aiosqlite.Connection, key: str) -> str | None:
    cursor = await db.execute("SELECT value FROM rag_meta WHERE key = ?", (key,))
    try:
        row = await cursor.fetchone()
    finally:
        await cursor.close()
    return row["value"] if row is not None else None


async def set_meta(db: aiosqlite.Connection, key: str, value: str) -> None:
    cursor = await db.execute(
        "INSERT INTO rag_meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
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
    再失败返回 False（纯向量模式）。结果记入 rag_meta(fts_tokenizer)。"""

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


def _fts_query(query: str) -> str:
    """构建 MATCH 词元：按空白切词、各自双引号包裹并转义内部引号。"""

    parts = [p for p in query.split() if p]
    if not parts:
        parts = [query.strip()] if query.strip() else []
    return " ".join('"' + p.replace('"', '""') + '"' for p in parts)


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
    db: aiosqlite.Connection, query: str, limit: int, session_id: str | None = None
) -> list[ChunkRow]:
    """关键词召回：>=3 字符走 FTS MATCH（rank 排序），更短走 LIKE 子串兜底。

    trigram 最小匹配长度是 3 字符，中文双字词（「开会」）必须走兜底；
    本地库规模下 LIKE 全表扫可接受（与 calendar 内存过滤同款取舍）。
    """

    cleaned = query.strip()
    if not cleaned:
        return []
    use_fts = len(cleaned) >= 3 and bool(await get_meta(db, "fts_tokenizer"))
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
        sql = (
            "SELECT c.* FROM rag_chunks c "
            "WHERE c.content LIKE '%' || ? || '%' ESCAPE ?"
        )
        params = [_like_escape(cleaned), _BS]
        order = " ORDER BY c.msg_time DESC LIMIT ?"
    if session_id is not None:
        sql += " AND c.session_id = ?"
        params.append(session_id)
    sql += order
    params.append(limit)
    cursor = await db.execute(sql, params)
    try:
        rows = await cursor.fetchall()
    finally:
        await cursor.close()
    return [ChunkRow(**dict(r)) for r in rows]


# ---------------------------------------------------------------- 写入侧 --


async def upsert_chunks(db: aiosqlite.Connection, rows: list[ChunkRow]) -> None:
    """批量 upsert 索引行（消息重处理时以最新内容覆盖）。"""

    if not rows:
        return
    cursor = await db.executemany(
        "INSERT INTO rag_chunks(source, msg_id, session_id, group_name, "
        "sender_name, msg_time, content, item_id) VALUES(?,?,?,?,?,?,?,?) "
        "ON CONFLICT(source, msg_id) DO UPDATE SET "
        "session_id=excluded.session_id, group_name=excluded.group_name, "
        "sender_name=excluded.sender_name, msg_time=excluded.msg_time, "
        "content=excluded.content, item_id=excluded.item_id",
        [
            (
                r.source, r.msg_id, r.session_id, r.group_name,
                r.sender_name, r.msg_time, r.content, r.item_id,
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


async def load_embeddings(
    db: aiosqlite.Connection, model: str, session_id: str | None = None
) -> tuple[list[ChunkRow], list[list[float]]]:
    """加载指定模型的全部向量（含所属索引行；JSON 解析失败按脏行丢弃）。"""

    sql = (
        "SELECT c.*, e.embedding FROM rag_chunks c "
        "JOIN rag_chunk_embeddings e ON c.source = e.source AND c.msg_id = e.msg_id "
        "WHERE e.model = ?"
    )
    params: list[object] = [model]
    if session_id is not None:
        sql += " AND c.session_id = ?"
        params.append(session_id)
    cursor = await db.execute(sql, params)
    try:
        rows = await cursor.fetchall()
    finally:
        await cursor.close()
    chunks: list[ChunkRow] = []
    vectors: list[list[float]] = []
    for r in rows:
        d = dict(r)
        raw = d.pop("embedding")
        try:
            vec = json.loads(raw)
            if not isinstance(vec, list):
                raise ValueError("embedding 非数组")
            floats = [float(x) for x in vec]
        except (json.JSONDecodeError, TypeError, ValueError):
            # 脏向量行直接删除：回填反连接（e.msg_id IS NULL）下一轮自动重嵌入
            cursor = await db.execute(
                "DELETE FROM rag_chunk_embeddings WHERE source = ? AND msg_id = ?",
                (d["source"], d["msg_id"]),
            )
            await cursor.close()
            await db.commit()
            continue
        chunks.append(ChunkRow(**d))
        vectors.append(floats)
    return chunks, vectors


async def count_status(db: aiosqlite.Connection) -> dict[str, object]:
    """状态汇总（/api/rag/status 用）。"""

    out: dict[str, object] = {}
    for table in ("rag_chunks", "rag_chunk_embeddings"):
        cursor = await db.execute("SELECT COUNT(*) AS c FROM " + table)
        try:
            row = await cursor.fetchone()
        finally:
            await cursor.close()
        out[table] = row["c"] if row else 0
    out["fts_tokenizer"] = await get_meta(db, "fts_tokenizer") or ""
    return out


async def gc_orphans(db: aiosqlite.Connection) -> int:
    """三表对账清理：不在 raw_messages 中的索引行级联清除。

    类别删除会级联清 raw_messages 但不发事件（最终一致），reindex 时经此
    对账。返回清理的总行数。
    """

    before = db.total_changes
    for ddl in (
        "DELETE FROM rag_chunks WHERE NOT EXISTS ("
        "SELECT 1 FROM raw_messages r WHERE r.source = rag_chunks.source "
        "AND r.msg_id = rag_chunks.msg_id)",
        "DELETE FROM rag_chunk_embeddings WHERE NOT EXISTS ("
        "SELECT 1 FROM rag_chunks c WHERE c.source = rag_chunk_embeddings.source "
        "AND c.msg_id = rag_chunk_embeddings.msg_id)",
    ):
        cursor = await db.execute(ddl)
        await cursor.close()
    # FTS 表惰性可选（纯向量模式下不存在），GC 先探测再清理
    if await get_meta(db, "fts_tokenizer"):
        cursor = await db.execute(
            "DELETE FROM rag_fts WHERE NOT EXISTS ("
            "SELECT 1 FROM rag_chunks c WHERE c.source = rag_fts.source "
            "AND c.msg_id = rag_fts.msg_id)"
        )
        await cursor.close()
    await db.commit()
    return db.total_changes - before
