"""RAG 引擎 — 批次索引、历史回填、混合检索与引用式问答编排。

写路径（post_insert 槽位）：before_run 锁外预嵌入 → run 锁内纯 SQLite 落库
（pipeline 骨架对 post_insert 全程持 _storage_lock，run 内严禁网络调用）；
读路径（/api/rag/ask）：向量 + FTS 双路召回 → RRF 融合 → 拒答门 →
AI 引用式回答。rag 三表是 raw_messages 的派生索引，不复制事实源语义。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone

import aiosqlite

from briefdesk import ai_ports
from briefdesk.db import get_db
from briefdesk.plugins.rag.config import RagSettings
from briefdesk.plugins.rag.db import (
    ChunkRow,
    ensure_fts,
    ensure_rag_schema,
    fts_search,
    load_embeddings,
    sync_fts,
    upsert_chunks,
    upsert_embeddings,
)
from briefdesk.types import BatchContext

logger = logging.getLogger(__name__)

_RRF_K = 60  # RRF 平滑常数（论文默认，抑制单路名次噪声）
_VEC_MIN_COS = 0.05  # 向量路最低余弦：低于此视为噪声不入融合（0 相似垃圾）


@dataclass
class Hit:
    """一条融合后的检索命中。"""

    chunk: ChunkRow
    cos: float  # 向量余弦；仅 FTS 命中时为 0.0
    rrf: float  # 双路倒数排名融合分
    has_fts: bool  # 是否被关键词路命中（拒答门的放行信号之一）


@dataclass
class _FusionEntry:
    """RRF 融合的中间累积态。"""

    chunk: ChunkRow
    cos: float = 0.0
    rrf: float = 0.0
    has_fts: bool = False


@dataclass
class AskResult:
    """一次问答的产出（refused=True 时 answer 为拒答文案、citations 为空）。"""

    refused: bool
    answer: str = ""
    citations: list[dict] = field(default_factory=list)


class RagEngine:
    """RAG 检索引擎（rag 插件私有，经模块级单例暴露给路由层）。"""

    def __init__(
        self,
        settings: RagSettings,
        db_factory: Callable[[], Awaitable[aiosqlite.Connection]] | None = None,
    ) -> None:
        self.settings = settings
        # 生产用核心连接单例；测试注入内存库工厂（签名兼容 get_db）
        self._db_factory = db_factory or (lambda: get_db())
        # before_run 预嵌入暂存：(source, msg_id) → 向量；run 消费后清空
        self._pending: dict[tuple[str, str], list[float]] = {}
        # FTS 可用性惰性探测结果（None=未探测；False=纯向量模式）
        self._fts_enabled: bool | None = None
        # 回填重启钩子（plugin.activate 注入；/api/rag/reindex 经此拉起新一轮）
        self.on_backfill_kick: Callable[[], None] | None = None

    async def teardown(self) -> None:
        self._pending.clear()

    async def _ensure_db_ready(self) -> aiosqlite.Connection:
        db = await self._db_factory()
        await ensure_rag_schema(db)
        if self._fts_enabled is None:
            self._fts_enabled = await ensure_fts(db)
            if not self._fts_enabled:
                logger.warning("rag: FTS 不可用，降级纯向量检索")
        return db

    # ---------------------------------------------------------- 索引路径 --

    async def before_run(self, batch: BatchContext) -> None:
        """锁外预嵌入：只允许在这里发生网络调用。"""

        self._pending.clear()
        candidates = [m for m in batch.messages if m.content.strip()]
        if not candidates:
            return
        try:
            vectors = await ai_ports.embed_texts(
                [m.content for m in candidates]
            )
        except Exception:  # noqa: BLE001 — 只放弃嵌入，不影响管道；
        # 内容仍会在 run 入索引，缺失向量由历史回填的反连接自动补齐
            logger.exception("rag: 批次预嵌入失败（本批仅跳过嵌入）")
            return
        if len(vectors) != len(candidates):
            logger.warning(
                "rag: 嵌入返回数不符（%d/%d），本批仅跳过嵌入",
                len(vectors), len(candidates),
            )
            return
        for msg, vec in zip(candidates, vectors):
            self._pending[(msg.source, msg.msg_id)] = vec

    async def run(self, batch: BatchContext) -> None:
        """锁内落库：纯 SQLite 操作，禁止任何网络调用。"""

        db = await self._ensure_db_ready()
        item_map = {(r.msg.source, r.msg.msg_id): r.item_id for r in batch.inserted}
        rows: list[ChunkRow] = []
        now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
        emb_items: list[tuple[str, str]] = []
        emb_vecs: list[list[float]] = []
        model = ai_ports.embed_model_name()
        for msg in batch.messages:
            if not msg.content.strip():
                continue
            row = ChunkRow(
                source=msg.source,
                msg_id=msg.msg_id,
                session_id=msg.session_id,
                group_name=msg.group_name,
                sender_name=msg.sender_name,
                msg_time=msg.timestamp,
                content=msg.content,
                item_id=item_map.get((msg.source, msg.msg_id), ""),
            )
            rows.append(row)
            vec = self._pending.pop((row.source, row.msg_id), None)
            if vec is not None:
                emb_items.append((row.source, row.msg_id))
                emb_vecs.append(vec)
        if not rows:
            return
        await upsert_chunks(db, rows)
        if self._fts_enabled:
            await sync_fts(db, rows)
        if emb_items:
            await upsert_embeddings(db, emb_items, emb_vecs, model, now_iso)

    # ---------------------------------------------------------- 历史回填 --

    async def backfill_step(self, now_ts: int) -> int:
        """单轮有界回填：索引窗口内缺失/模型失配的 raw_messages。

        反连接条件天然可续跑：嵌入失败或预算截断的行下一轮仍会被选中；
        返回本轮处理条数，0 表示回填完成。backfill_days：>0 窗口天，
        0 关闭，-1 全量。"""

        if self.settings.backfill_days == 0:
            return 0
        db = await self._ensure_db_ready()
        model = ai_ports.embed_model_name()
        sql = (
            "SELECT r.source, r.msg_id, r.session_id, r.group_name, "
            "r.sender_name, r.timestamp AS msg_time, r.content "
            "FROM raw_messages r "
            "LEFT JOIN rag_chunks c ON c.source = r.source AND c.msg_id = r.msg_id "
            "LEFT JOIN rag_chunk_embeddings e ON e.source = r.source "
            "AND e.msg_id = r.msg_id "
            "WHERE (c.msg_id IS NULL OR e.msg_id IS NULL OR e.model <> ?)"
        )
        params: list[object] = [model]
        if self.settings.backfill_days > 0:
            sql += " AND r.timestamp >= ?"
            params.append(now_ts - self.settings.backfill_days * 86400)
        sql += " ORDER BY r.timestamp DESC LIMIT ?"
        params.append(self.settings.backfill_budget_per_cycle)
        cursor = await db.execute(sql, params)
        try:
            raw_rows = await cursor.fetchall()
        finally:
            await cursor.close()
        rows = [
            ChunkRow(
                source=r["source"], msg_id=r["msg_id"], session_id=r["session_id"],
                group_name=r["group_name"], sender_name=r["sender_name"],
                msg_time=r["msg_time"], content=r["content"],
            )
            for r in raw_rows
            if r["content"] and r["content"].strip()
        ]
        if not rows:
            return 0
        # 分批嵌入；失败时保留已成功前缀，剩余行下轮反连接自动重试
        vectors: list[list[float]] = []
        batch_size = self.settings.backfill_batch
        try:
            for start in range(0, len(rows), batch_size):
                part = rows[start : start + batch_size]
                vectors.extend(await ai_ports.embed_texts([r.content for r in part]))
        except Exception:  # noqa: BLE001 — 同上：只放弃嵌入，不拖垮回填循环
            logger.exception("rag: 回填补入嵌入失败（已得 %d 条）", len(vectors))
        now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
        await upsert_chunks(db, rows)
        if self._fts_enabled:
            await sync_fts(db, rows)
        paired = min(len(vectors), len(rows))
        if paired:
            await upsert_embeddings(
                db,
                [(r.source, r.msg_id) for r in rows[:paired]],
                vectors[:paired],
                model,
                now_iso,
            )
        logger.info("rag: 回填本轮处理 %d 条（含向量 %d 条）", len(rows), paired)
        return len(rows)

    # ---------------------------------------------------------- 检索路径 --

    async def retrieve(
        self, question: str, session_id: str | None = None
    ) -> list[Hit] | None:
        """混合检索：向量 Top-K 与 FTS 双路召回 → RRF 融合 → 拒答门。

        拒答门：前 5 名中存在 FTS 命中则放行（关键词是硬证据）；
        否则要求 top1 余弦 >= min_score。返回 None 表示应诚实拒答。
        """

        cleaned = question.strip()
        if not cleaned:
            return None
        try:
            q_vec = (await ai_ports.embed_texts([cleaned]))[0]
        except Exception:  # noqa: BLE001 — 查询嵌入失败按拒答处理
            logger.exception("rag: 查询嵌入失败")
            return None
        db = await self._ensure_db_ready()
        model = ai_ports.embed_model_name()
        chunks, vectors = await load_embeddings(db, model, session_id)
        vec_hits: list[tuple[ChunkRow, float]] = []
        if vectors:
            for idx, sim in ai_ports.top_k_similar(
                q_vec, vectors, self.settings.top_k, _VEC_MIN_COS
            ):
                vec_hits.append((chunks[idx], float(sim)))
        fts_rows: list[ChunkRow] = []
        if self._fts_enabled:
            fts_rows = await fts_search(db, cleaned, self.settings.fts_limit, session_id)

        # RRF 融合：key = (source, msg_id)；并列按 msg_time 新者在前
        fused: dict[tuple[str, str], _FusionEntry] = {}
        for rank, (chunk, sim) in enumerate(vec_hits, start=1):
            key = (chunk.source, chunk.msg_id)
            entry = fused.setdefault(key, _FusionEntry(chunk=chunk))
            entry.cos = max(entry.cos, sim)
            entry.rrf += 1.0 / (_RRF_K + rank)
        for rank, chunk in enumerate(fts_rows, start=1):
            key = (chunk.source, chunk.msg_id)
            entry = fused.setdefault(key, _FusionEntry(chunk=chunk))
            entry.has_fts = True
            entry.rrf += 1.0 / (_RRF_K + rank)

        hits = sorted(
            (
                Hit(chunk=e.chunk, cos=e.cos, rrf=e.rrf, has_fts=e.has_fts)
                for e in fused.values()
            ),
            key=lambda h: (-h.rrf, -h.chunk.msg_time),
        )
        if not hits:
            return None
        if not any(h.has_fts for h in hits[:5]) and hits[0].cos < self.settings.min_score:
            return None
        return hits[: self.settings.max_evidence]

    # ---------------------------------------------------------- 问答路径 --

    _CITE_RE = re.compile(r"\[(\d{1,2})\]")

    @staticmethod
    def _citation(n: int, hit: Hit) -> dict:
        chunk = hit.chunk
        return {
            "n": n,
            "msg_id": chunk.msg_id,
            "source": chunk.source,
            "session_id": chunk.session_id,
            "sender_name": chunk.sender_name,
            "time": chunk.msg_time,
            "group_name": chunk.group_name,
            "snippet": chunk.content[:120],
            "item_id": chunk.item_id,
        }

    async def ask(
        self, question: str, session_id: str | None = None
    ) -> AskResult:
        """检索 + 引用式回答；检索为空 → 诚实拒答且不调用 AI。"""

        from briefdesk.plugins.rag.prompts import build_answer_prompt

        hits = await self.retrieve(question, session_id)
        if not hits:
            return AskResult(refused=True, answer="没有在群聊记录里找到相关消息。")
        messages = build_answer_prompt(datetime.now(), question.strip(), hits)
        resp = await ai_ports.chat(messages, temperature=0.2, max_tokens=1024)
        content = ""
        if getattr(resp, "choices", None):
            content = (resp.choices[0].message.content or "").strip()
        cited = {
            int(m.group(1))
            for m in self._CITE_RE.finditer(content)
            if 1 <= int(m.group(1)) <= len(hits)
        }
        nums = sorted(cited) if cited else list(range(1, len(hits) + 1))
        citations = [self._citation(n, hits[n - 1]) for n in nums]
        return AskResult(refused=False, answer=content, citations=citations)

    def request_backfill(self) -> bool:
        """请求拉起一轮回填循环（reindex 后补齐）；返回是否已触发。"""

        if self.on_backfill_kick is None:
            return False
        self.on_backfill_kick()
        return True


_instance: RagEngine | None = None


def set_engine(engine: RagEngine | None) -> None:
    """注入/清除引擎实例（rag 插件 setup/teardown 调用）。"""

    global _instance
    _instance = engine


def get_engine() -> RagEngine | None:
    return _instance
