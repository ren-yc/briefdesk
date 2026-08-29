"""RAG 引擎 — 批次索引、历史回填、混合检索与引用式问答编排。

写路径（post_insert 槽位）：before_run 锁外预嵌入 → run 锁内纯 SQLite 落库
（pipeline 骨架对存储相两槽统一探测可选钩子；run 内严禁网络调用）。
读路径（/api/rag/ask）：向量缓存（created_at 水位增量填充，解析放工作线程）
→ FTS 双路召回 → RRF 融合 → 拒答门 → AI 引用式回答。

作用域语义（隐私边界，见 architecture.md）：启用会话恒为前提；
RAG_GROUP_ONLY（默认开）进一步限定群聊——停用会话即时不可问出。
rag 三表是 raw_messages 的派生索引；向量表走专用连接（get_embed_db 同款
隔离约定），脏行删除不与主连接上的管道事务互相提交。
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

import aiosqlite
import numpy as np

from briefdesk import ai_ports
from briefdesk.db import get_db, get_embed_db
from briefdesk.masking import PLACEHOLDER_ONLY_RE
from briefdesk.plugins.rag.config import RagSettings
from briefdesk.plugins.rag.db import (
    ChunkRow,
    ensure_fts,
    ensure_rag_schema,
    fetch_all,
    fetch_new_embeddings,
    fetch_one,
    fts_search,
    gc_orphans,
    parse_embedding_rows,
    scope_sql,
    sync_fts,
    upsert_chunks,
    upsert_embeddings,
)
from briefdesk.types import BatchContext

logger = logging.getLogger(__name__)

_RRF_K = 60  # RRF 平滑常数（论文默认，抑制单路名次噪声）
_VEC_MIN_COS = 0.05  # 向量路最低余弦：低于此视为噪声不入融合
_EMBED_FAIL_BACKOFF_BASE = 60.0  # 回填失败退避基数（秒）
_EMBED_FAIL_BACKOFF_CAP = 600.0  # 退避封顶（秒）


def embed_fail_backoff(step: int) -> float:
    """回填嵌入失败的指数退避秒数（基数 60、封顶 600）。

    基数与封顶同处一地：维护循环只按轮次问本函数，不各自持有一半策略。
    """

    return min(_EMBED_FAIL_BACKOFF_BASE * (2**step), _EMBED_FAIL_BACKOFF_CAP)


def _indexable(content: str) -> bool:
    """OCR 失败/附件占位符不入检索索引。

    判定复用 pipeline 入口同一单源 `masking.PLACEHOLDER_ONLY_RE`（整条仅由
    方括号片段构成），吃的同样是原始 content —— `[图片][图片]`、`[语音通话]`
    等此前漏网的变体一并挡下，不再是七词白名单。
    """

    text = content.strip()
    return bool(text) and not PLACEHOLDER_ONLY_RE.match(text)


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
        embed_factory: Callable[[], Awaitable[aiosqlite.Connection]] | None = None,
    ) -> None:
        self.settings = settings
        # 生产用核心连接单例；测试注入内存库工厂（签名兼容 get_db/get_embed_db）
        self._db_factory = db_factory or (lambda: get_db())
        self._embed_factory = embed_factory or (lambda: get_embed_db())
        # before_run 预嵌入暂存：(source, msg_id) → 向量；run 消费后清空
        self._pending: dict[tuple[str, str], list[float]] = {}
        # FTS 可用性惰性探测结果（None=未探测；False=纯向量模式）
        self._fts_enabled: bool | None = None
        self._schema_ready = False
        # 向量缓存：created_at 水位增量填充；模型切换/行数回退全量重建
        self._vec_model: str | None = None
        self._vec_watermark = ""
        self._vec_count_seen = 0
        self._vec_entries: dict[tuple[str, str], tuple[ChunkRow, np.ndarray]] = {}
        # 预组 float32 矩阵（缓存变更时重组，ask 路径零转换零拷贝直用）
        self._matrix: np.ndarray | None = None
        self._matrix_keys: list[tuple[str, str]] = []
        self._matrix_chunks: list[ChunkRow] = []
        self._matrix_sessions: list[tuple[str, str]] = []
        self._refresh_lock = asyncio.Lock()
        # 嵌入降级自愈：before_run 成功前最多踢一次回填，防供应商宕机刷踢
        self._kicked_since_embed_ok = False
        self._stale_warned = False
        # 回填维护循环观测位（plugin 循环据此做失败退避）
        self.last_cycle_embed_failed = False
        # 维护循环拉起钩子（plugin.activate 注入；reindex 经此重启循环）
        self.on_backfill_kick: Callable[[], None] | None = None

    async def teardown(self) -> None:
        self._pending.clear()
        self._vec_entries.clear()
        self._vec_count_seen = 0
        self._vec_watermark = ""
        self._matrix = None
        self._matrix_keys = []
        self._matrix_chunks = []

    async def _ensure_db_ready(self) -> aiosqlite.Connection:
        db = await self._db_factory()
        if not self._schema_ready:
            await ensure_rag_schema(db)
            self._fts_enabled = await ensure_fts(db)
            if not self._fts_enabled:
                logger.warning("rag: FTS 不可用，降级纯向量检索")
            self._schema_ready = True
        return db

    @staticmethod
    async def _allowed_sessions(
        db: aiosqlite.Connection, group_only: bool, session_id: str | None
    ) -> set[tuple[str, str]]:
        """当前可检索会话集合（恒非 None；group_only 关且未收窄时含全部启用会话）。

        启用状态每次查询现取——停用会话即时失效，不依赖索引期快照。
        返回 (source, session_id) 元组集合（跨源撞名安全），恒非 None。
        """

        sql = "SELECT source, session_id FROM sessions WHERE enabled = 1"
        params: list[object] = []
        if group_only:
            sql += " AND is_group = 1"
        if session_id is not None:
            sql += " AND session_id = ?"
            params.append(session_id)
        rows = await fetch_all(db, sql, params)
        return {(r["source"], r["session_id"]) for r in rows}

    # ------------------------------------------------------------ 索引路径 --

    async def _persist_chunks(
        self,
        db: aiosqlite.Connection,
        rows: list[ChunkRow],
        emb_items: list[tuple[str, str]],
        emb_vecs: list[list[float]],
        model: str,
    ) -> bool:
        """落库唯一出口：chunks → FTS 同步 → 向量（专用连接）。

        实时批次与历史回填共用本方法，令「入了 chunks 却漏同步 FTS」在结构上
        不可能发生——那种漏同步不会报错，只会让检索静默少一条腿（FTS 索引与
        rag_chunks 分叉，且反连接补不回来，它只看向量缺失）。
        model 由调用方传入而非在此现取：回填的反连接谓词已按某个 model 值筛过
        行，落库必须写同一个值，否则下一轮又把这批选回来（空转死循环）。
        返回是否写入了向量，供调用方决定要不要触发自愈回填。
        """
        await upsert_chunks(db, rows)
        if self._fts_enabled:
            await sync_fts(db, rows)
        if not emb_items:
            return False
        now_iso = datetime.now(UTC).isoformat(timespec="seconds")
        edb = await self._embed_factory()
        await upsert_embeddings(edb, emb_items, emb_vecs, model, now_iso)
        return True

    async def before_run(self, batch: BatchContext) -> None:
        """锁外预嵌入：只允许在这里发生网络调用。"""

        self._pending.clear()
        candidates = [m for m in batch.messages if _indexable(m.content)]
        if not candidates:
            return
        try:
            vectors = await ai_ports.embed_texts([m.content for m in candidates])
        except Exception:
            # 内容仍会在 run 入索引，缺失向量由维护循环的反连接自动补齐
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
        self._kicked_since_embed_ok = False  # 嵌入恢复，允许后续再次降级自愈

    async def run(self, batch: BatchContext) -> None:
        """锁内落库：纯 SQLite 操作，禁止任何网络调用。"""

        db = await self._ensure_db_ready()
        allowed = await self._allowed_sessions(db, self.settings.group_only, None)
        item_map = {(r.msg.source, r.msg.msg_id): r.item_id for r in batch.inserted}
        rows: list[ChunkRow] = []
        emb_items: list[tuple[str, str]] = []
        emb_vecs: list[list[float]] = []
        model = ai_ports.embed_model_name()
        for msg in batch.messages:
            if not _indexable(msg.content):
                continue
            if (msg.source, msg.session_id) not in allowed:
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
        embedded = await self._persist_chunks(db, rows, emb_items, emb_vecs, model)
        # 内容已入索引但零向量：嵌入能力在而预嵌入没发生 → 自愈踢一次
        if (
            not embedded
            and ai_ports.is_embedding_enabled()
            and not self._kicked_since_embed_ok
            and self.request_backfill()
        ):
            self._kicked_since_embed_ok = True
            logger.warning("rag: 实时批次缺向量，已触发补齐回填")
        # 未被消费的 pending（如整批被作用域过滤）直接丢弃，避免跨批串味
        self._pending.clear()

    # ------------------------------------------------------------ 历史回填 --

    async def backfill_step(self, now_ts: int) -> int:
        """单轮有界回填：索引窗口内缺失/模型失配的 raw_messages。

        反连接条件天然可续跑：嵌入失败或预算截断的行下一轮仍会被选中；
        返回本轮处理条数，0 表示回填完成。backfill_days：>0 窗口天，
        0 关闭，-1 全量。last_cycle_embed_failed 供维护循环做失败退避。
        """

        self.last_cycle_embed_failed = False
        # 先建表再判 days=0：维护循环在全新库上也要能安全空转（GC/预热依赖表）
        db = await self._ensure_db_ready()
        if self.settings.backfill_days == 0:
            return 0
        model = ai_ports.embed_model_name()
        # 作用域谓词与检索侧共用单源（这里过滤 raw_messages，别名 r）
        scope, scope_params = scope_sql(self.settings.group_only, alias="r")
        sql = (
            "SELECT r.source, r.msg_id, r.session_id, r.group_name, "
            "r.sender_name, r.timestamp AS msg_time, r.content "
            "FROM raw_messages r "
            "LEFT JOIN rag_chunks c ON c.source = r.source AND c.msg_id = r.msg_id "
            "LEFT JOIN rag_chunk_embeddings e ON e.source = r.source "
            "AND e.msg_id = r.msg_id "
            "WHERE (c.msg_id IS NULL OR e.msg_id IS NULL OR e.model <> ?)"
            " AND trim(r.content, ' ' || char(9)) <> ''"
            + scope
        )
        # 绑定顺序须随 SQL 文本顺序：model → scope（当前无参）→ 窗口 → LIMIT
        params: list[object] = [model, *scope_params]
        if self.settings.backfill_days > 0:
            sql += " AND r.timestamp >= ?"
            params.append(now_ts - self.settings.backfill_days * 86400)
        sql += " ORDER BY r.timestamp DESC LIMIT ?"
        params.append(self.settings.backfill_budget_per_cycle)
        raw_rows = await fetch_all(db, sql, params)
        rows = [
            ChunkRow(
                source=r["source"], msg_id=r["msg_id"],
                session_id=r["session_id"], group_name=r["group_name"],
                sender_name=r["sender_name"], msg_time=r["msg_time"],
                content=r["content"],
            )
            for r in raw_rows
            if _indexable(r["content"])
        ]
        if not rows:
            return 0
        # 分批嵌入 + 逐批数量守卫：供应商短返回时截断到已对齐前缀，
        # 防错位向量静默落库（反连接无法纠正的错误数据）
        vectors: list[list[float]] = []
        failed = False
        batch_size = self.settings.backfill_batch
        for start in range(0, len(rows), batch_size):
            part = rows[start : start + batch_size]
            try:
                part_vecs = await ai_ports.embed_texts([r.content for r in part])
            except Exception:
                logger.exception("rag: 回填嵌入批次失败（已得 %d 条）", len(vectors))
                failed = True
                break
            if len(part_vecs) != len(part):
                logger.warning(
                    "rag: 回填嵌入返回数不符（%d/%d），截断至已对齐前缀",
                    len(part_vecs), len(part),
                )
                failed = True
                break
            vectors.extend(part_vecs)
        # 截断到已对齐前缀：向量少于行数时只登记配对上的那部分，
        # 其余行仍入 chunks（反连接下轮按缺向量重新选回）
        paired = min(len(vectors), len(rows))
        await self._persist_chunks(
            db,
            rows,
            [(r.source, r.msg_id) for r in rows[:paired]],
            vectors[:paired],
            model,
        )
        self.last_cycle_embed_failed = failed
        logger.info("rag: 回填本轮处理 %d 条（含向量 %d 条）", len(rows), paired)
        return len(rows)

    # ------------------------------------------------------------ 维护循环 --

    async def maintenance_gc(self) -> int:
        """孤儿对账：chunks/FTS 在主连接清，向量在专用连接清。"""

        db = await self._db_factory()
        edb = await self._embed_factory()
        removed = await gc_orphans(db, edb)
        if removed:
            logger.info("rag: GC 清理孤儿索引 %d 行", removed)
        return removed

    async def warm_vectors(self, force_full: bool = False) -> None:
        """填充/刷新向量缓存；force_full 用于维护周期整表重扫（覆盖删除）。"""

        if force_full:
            self._vec_watermark = ""
            self._vec_count_seen = 0
        await self._refresh_vector_cache()

    def _vec_cache_clear(self) -> None:
        self._vec_entries.clear()
        self._vec_watermark = ""
        self._vec_count_seen = 0
        self._matrix = None
        self._matrix_keys = []
        self._matrix_chunks = []
        self._matrix_sessions = []

    def _rebuild_matrix(self) -> None:
        """缓存变更后重组 float32 矩阵（ask 路径零转换直接用）。"""

        if not self._vec_entries:
            self._matrix = None
            self._matrix_keys = []
            self._matrix_chunks = []
            self._matrix_sessions = []
            return
        keys = list(self._vec_entries.keys())
        self._matrix_keys = keys
        self._matrix_chunks = [self._vec_entries[k][0] for k in keys]
        self._matrix_sessions = [
            (chunk.source, chunk.session_id) for chunk in self._matrix_chunks
        ]
        self._matrix = np.asarray(
            [self._vec_entries[k][1] for k in keys], dtype=np.float32
        )

    async def _refresh_vector_cache(self) -> None:
        model = ai_ports.embed_model_name()
        if self._vec_model != model:
            logger.info("rag: 嵌入模型切换 %s -> %s，重建向量缓存", self._vec_model, model)
            self._vec_cache_clear()
            self._vec_model = model
        edb = await self._embed_factory()
        row = await fetch_one(edb, "SELECT COUNT(*) AS c FROM rag_chunk_embeddings")
        total = int(row["c"]) if row is not None else 0
        if total < self._vec_count_seen:
            self._vec_cache_clear()  # 有删除（GC），水位失效，整表重建
        raw_rows, max_created = await fetch_new_embeddings(
            edb, model, self._vec_watermark, self.settings.group_only
        )
        if not raw_rows:
            return
        entries, bad_keys = await asyncio.to_thread(parse_embedding_rows, raw_rows)
        if bad_keys:
            await self._delete_bad_embeddings(edb, bad_keys)
        for chunk, vec in entries:
            self._vec_entries[(chunk.source, chunk.msg_id)] = (chunk, vec)
        self._vec_watermark = max_created
        self._vec_count_seen = total
        self._rebuild_matrix()

    @staticmethod
    async def _delete_bad_embeddings(
        edb: aiosqlite.Connection, bad_keys: list[tuple[str, str]]
    ) -> None:
        # 专用连接上逐条原子删除；反连接下一轮自动重嵌入
        for key in bad_keys:
            cursor = await edb.execute(
                "DELETE FROM rag_chunk_embeddings WHERE source = ? AND msg_id = ?",
                key,
            )
            await cursor.close()
        await edb.commit()

    def request_backfill(self) -> bool:
        """请求拉起一轮回填循环（reindex/降级自愈）；返回是否已触发。"""

        if self.on_backfill_kick is None:
            return False
        self.on_backfill_kick()
        return True

    # ------------------------------------------------------------ 检索路径 --

    async def retrieve(
        self, question: str, session_id: str | None = None
    ) -> list[Hit] | None:
        """混合检索：向量缓存 + FTS 双路召回 → RRF 融合 → 拒答门。

        拒答门：存在任一 FTS 命中即放行（最优 FTS 命中的融合位次数学上
        ≤2，「排第 6 之后」不可达，故无需窗口判断）；否则要求 top1 余弦
        ≥ min_score。返回 None 表示应诚实拒答。
        """

        cleaned = question.strip()
        if not cleaned:
            return None
        q_vec: list[float] | None = None
        try:
            q_vec = (await ai_ports.embed_texts([cleaned]))[0]
        except Exception:
            # F4：嵌入端点故障不应连坐关键词问答——降级 FTS-only，仍可回答可命中问题
            logger.warning(
                "rag: 查询嵌入失败，降级为 FTS-only 检索（仅关键词可命中）",
                exc_info=True,
            )
        db = await self._ensure_db_ready()
        async with self._refresh_lock:
            await self._refresh_vector_cache()
        allowed = await self._allowed_sessions(db, self.settings.group_only, session_id)
        if (
            not self._vec_entries
            and not self._stale_warned
            and self.settings.backfill_days == 0
        ):
            # 换模型 + 回填关闭：检索将静默全哑，至少告警一次
            logger.warning(
                "rag: 当前模型 %s 无可用向量且 RAG_BACKFILL_DAYS=0，向量路不可用",
                ai_ports.embed_model_name(),
            )
            self._stale_warned = True
        if self._vec_entries:
            self._stale_warned = False
        # 向量腿：预组 float32 矩阵上做作用域/收窄过滤（会话元组判定，
        # 跨源撞名安全；缓存填充时已按白名单过滤，这里再按查询参数收窄）
        vec_hits: list[tuple[ChunkRow, float]] = []
        if q_vec is not None and self._matrix is not None and self._matrix_keys:
            idxs = [
                i for i, sess in enumerate(self._matrix_sessions) if sess in allowed
            ]
            if idxs:
                mats = (
                    self._matrix[idxs]
                    if len(idxs) != len(self._matrix_keys)
                    else self._matrix
                )
                chunks = [self._matrix_chunks[i] for i in idxs]
                for idx, sim in ai_ports.top_k_similar(
                    q_vec, mats, self.settings.top_k, _VEC_MIN_COS
                ):
                    vec_hits.append((chunks[idx], float(sim)))
        fts_rows: list[ChunkRow] = []
        if self._fts_enabled:
            fts_rows = await fts_search(
                db, cleaned, self.settings.fts_limit, session_id,
                enabled_group_only=self.settings.group_only,
            )

        fused: dict[tuple[str, str], _FusionEntry] = {}
        for rank, (chunk, sim) in enumerate(vec_hits, start=1):
            entry = fused.setdefault(
                (chunk.source, chunk.msg_id), _FusionEntry(chunk=chunk)
            )
            entry.cos = max(entry.cos, sim)
            entry.rrf += 1.0 / (_RRF_K + rank)
        for rank, chunk in enumerate(fts_rows, start=1):
            entry = fused.setdefault(
                (chunk.source, chunk.msg_id), _FusionEntry(chunk=chunk)
            )
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
        if not any(h.has_fts for h in hits) and hits[0].cos < self.settings.min_score:
            return None
        return hits[: self.settings.max_evidence]

    # ------------------------------------------------------------ 问答路径 --

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
        self,
        question: str,
        session_id: str | None = None,
        history: list[dict] | None = None,
    ) -> AskResult:
        """检索 + 引用式回答；检索为空 → 诚实拒答且不调用 AI。"""

        from briefdesk.plugins.rag.prompts import build_answer_prompt

        hits = await self.retrieve(question, session_id)
        if not hits:
            return AskResult(refused=True, answer="没有在群聊记录里找到相关消息。")
        messages = build_answer_prompt(
            datetime.now(UTC).astimezone(), question.strip(), hits, history or [],
            self.settings.evidence_chars,
        )
        # 问答模型通道 override 来自本插件配置域（三项留空 = 复用主链路 AI）
        resp = await ai_ports.rag_chat(
            messages,
            temperature=0.2,
            max_tokens=1024,
            model=self.settings.model,
            api_base=self.settings.api_base,
            api_key=self.settings.api_key.get_secret_value(),
        )
        content = ""
        if getattr(resp, "choices", None):
            content = (resp.choices[0].message.content or "").strip()
        # 双态解析：deepseek 系强制 json_object 输出 → 优先 JSON 契约；
        # 其它供应商纯文本 → 回退 [n] 正则（兼容两路）
        answer = content
        cited: set[int] = set()
        parsed = ai_ports.loads_json(content)
        if isinstance(parsed, dict) and isinstance(parsed.get("answer"), str):
            answer = parsed["answer"].strip()
            nums = parsed.get("citations")
            if isinstance(nums, list):
                for n in nums:
                    try:
                        v = int(n)
                    except (TypeError, ValueError):
                        continue
                    if 1 <= v <= len(hits):
                        cited.add(v)
        if not cited:
            cited = {
                int(m.group(1))
                for m in self._CITE_RE.finditer(content)
                if 1 <= int(m.group(1)) <= len(hits)
            }
        nums = sorted(cited) if cited else list(range(1, len(hits) + 1))
        citations = [self._citation(n, hits[n - 1]) for n in nums]
        return AskResult(refused=False, answer=answer, citations=citations)


_instance: RagEngine | None = None


def set_engine(engine: RagEngine | None) -> None:
    """注入/清除引擎实例（rag 插件 setup/teardown 调用）。"""

    global _instance
    _instance = engine


def get_engine() -> RagEngine | None:
    return _instance
