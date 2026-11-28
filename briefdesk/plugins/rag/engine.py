"""RAG 引擎 — 批次索引、历史回填、混合检索与引用式问答编排。

写路径（post_insert 槽位）：before_run 锁外预嵌入 → run 锁内纯 SQLite 落库
（pipeline 骨架对存储相两槽统一探测可选钩子；run 内严禁网络调用）。
读路径（/api/rag/ask）：向量缓存（created_at 水位增量填充，解析放工作线程）
→ FTS 双路召回 → RRF 融合 → 拒答门 → AI 引用式回答。

出站假名化：证据行里的姓名/群名先看**实际派发端点**（供应商没有专用 rag_chat
时 override 会被丢弃、请求走主端点），远端才换成化名，本地原样；回答文本按请求
内逆映射还原。准备失败时这次回答的请求根本不发生——绝不回退成真名重发。

作用域语义（隐私边界，见 architecture.md）：启用会话恒为前提；
RAG_GROUP_ONLY（默认开）进一步限定群聊——停用会话即时不可问出。
rag 五表（chunks/embeddings/fts/meta/skipped）是 raw_messages 的派生索引；向量表
走专用连接（get_embed_db 同款隔离约定），脏行删除不与主连接上的管道事务
互相提交；共享连接上的写统一持 db.get_embed_lock()/pipeline 存储锁。
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import aiosqlite
import numpy as np

from briefdesk import ai_ports
from briefdesk.config import config
from briefdesk.db import get_db, get_embed_db, get_embed_lock, storage_lock
from briefdesk.masking import PLACEHOLDER_ONLY_RE
from briefdesk.plugins.rag.config import RagSettings
from briefdesk.plugins.rag.db import (
    ChunkRow,
    ChunkWriteOutcomeUnknownError,
    ensure_fts,
    ensure_rag_schema,
    fetch_all,
    fetch_new_embeddings,
    fetch_one,
    fts_search,
    gc_orphans,
    mark_skipped,
    parse_embedding_rows,
    scope_sql,
    sync_fts,
    upsert_chunks,
    upsert_embeddings,
)
from briefdesk.plugins.rag.prompts import EvidenceNames, build_answer_prompt
from briefdesk.pseudonym import (
    NameContext,
    PseudonymError,
    group_identity_key,
    person_identity_key,
    pseudonymize_enabled,
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


def _citation_index(n: object) -> int | None:
    """引用编号：接受 int（排除 bool）、整数值浮点与纯数字字符串；其它一律拒绝。

    此前 int(n) 把 True 当 1、把 "1" 当 1、任意异常类型静默跳过，语义不明确。
    整数值浮点（1.0）必须接受：JSON 模式下部分供应商把整数输出为浮点，拒绝
    会让显式引用列表整个落空、回答显示为无依据。
    """
    if isinstance(n, bool):
        return None
    if isinstance(n, int):
        return n
    if isinstance(n, float) and n.is_integer():
        return int(n)
    if isinstance(n, str) and n.strip().isdigit():
        return int(n.strip())
    return None


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
        # FTS 可用性惰性探测结果（None=未探测；False=纯向量模式）
        self._fts_enabled: bool | None = None
        self._schema_ready = False
        self._schema_lock = asyncio.Lock()
        # 向量缓存：created_at 水位增量填充；模型切换/行数回退全量重建
        self._vec_model: str | None = None
        self._vec_watermark = ""
        self._vec_count_seen = 0
        self._vec_full_retry = False
        self._vec_invalidations = 0
        self._vec_applied_invalidations = 0
        self._vec_unsettled_writes = 0
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

    @property
    def _vec_cache_dirty(self) -> bool:
        return self._vec_applied_invalidations != self._vec_invalidations

    async def teardown(self) -> None:
        self._vec_entries.clear()
        self._vec_count_seen = 0
        self._vec_watermark = ""
        self._vec_full_retry = False
        self._matrix = None
        self._matrix_keys = []
        self._matrix_chunks = []

    async def _ensure_db_ready(self) -> aiosqlite.Connection:
        db = await self._db_factory()
        if not self._schema_ready:
            # 并发首次调用会交错迁移与提交；锁内复查，避免等待者重复迁移。
            async with self._schema_lock:
                if not self._schema_ready:
                    await ensure_rag_schema(db)
                    self._fts_enabled = await ensure_fts(db)
                    if not self._fts_enabled:
                        logger.warning("rag: FTS 不可用，降级纯向量检索")
                    self._schema_ready = True
        return db

    async def prepare(self) -> None:
        """装配期预热入口（唯一）：建 rag 表 + FTS 探测，提前到 activate。

        调用方必须持 storage_lock（rag 插件在 activate 中锁内调用）——否则
        首次建表 + set_meta 的 commit 发生在锁外，违反锁纪律。不承担连接
        关闭责任：生产 _db_factory 即 get_db() 的模块级共享单例，全仓无人
        关闭该连接；在此关闭会破坏单例（后续 run/ask 拿到已关连接）。
        """
        await self._ensure_db_ready()

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
        *,
        sender_id_fill_only: bool = False,
    ) -> bool:
        """落库唯一出口：chunks → FTS 同步 → 向量（专用连接）。

        实时批次与历史回填共用本方法，令「入了 chunks 却漏同步 FTS」在成功
        路径上不可能发生；chunk 写入与 FTS 同步之间被取消仍可能缺 FTS 行。
        缺少当前模型 embedding 的行会被反连接选中回填并同步 FTS；已有当前
        模型 embedding 却缺 FTS 的行不会被选中，仍可能让检索静默少一条腿。
        model 由调用方传入而非在此现取：回填的反连接谓词已按某个 model 值筛过
        行，落库必须写同一个值，否则下一轮又把这批选回来（空转死循环）。
        返回是否写入了向量，供调用方决定要不要触发自愈回填。

        chunk 写入成功、本批回滚已确认或事务已结束时才收尾；回滚未获确认且
        事务仍打开时保留登记，否则提交前快照会确认随后才发布的身份更新。

        连接纪律：调用方持有 storage_lock 与否由调用路径决定（run 在管道锁内、
        backfill_step 自行持锁）；本方法对共享 embed 连接的写统一持
        db.get_embed_lock()——dedup 的向量落库共用同一条连接，语句级互斥
        防止一方的 commit 把另一方未完成的批量写入提前提交。
        """
        # 缓存成员关系既不能证明行是全新的，也不能证明 SQL 中尚无此行；
        # 按缓存状态筛选只是猜缓存没有的答案，每种猜法都会留下缺口。
        # 水位之上的 embedding 已提交但尚未缓存时，元数据更新可与刷新交错，
        # 让刷新安装提交前身份并越过其水位，后续增量读取永不再访该行。
        # 所有非空批次都在提交前登记世代及未收尾写入，不等刷新锁。
        # 写入活跃时，每个后续刷新都全量重读；脏状态合并为每次刷新一次，
        # 而非每个批次一次，以此换取写入命运确定后的下次刷新不再服务旧身份。
        # 回滚成功即已丢弃本批，RELEASE 失败不改命运；事务已结束也不能再晚发布。
        # 回滚失败且事务仍打开才保留登记，否则提交前快照可能确认尚未发布的世代。
        registered = bool(rows)
        if registered:
            self._vec_invalidations += 1
            self._vec_unsettled_writes += 1
        outcome_known = True
        try:
            await upsert_chunks(db, rows, sender_id_fill_only=sender_id_fill_only)
        except ChunkWriteOutcomeUnknownError:
            outcome_known = False
            raise
        finally:
            if registered and outcome_known:
                self._vec_unsettled_writes -= 1
        if self._fts_enabled:
            await sync_fts(db, rows)
        if not emb_items:
            return False
        now_iso = datetime.now(UTC).isoformat(timespec="seconds")
        edb = await self._embed_factory()
        async with get_embed_lock():
            await upsert_embeddings(edb, emb_items, emb_vecs, model, now_iso)
        return True

    async def before_run(self, batch: BatchContext) -> None:
        """锁外预嵌入：只允许在这里发生网络调用。

        向量写入 batch.preembeddings（批上下文级，每批新建天然隔离）——
        引擎级共享字典会在并发批次（实时批 flush 与回填同时跑）下互相
        clear 丢向量。
        """
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
            batch.preembeddings[(msg.source, msg.msg_id)] = vec
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
                sender_id=msg.sender_id,
            )
            rows.append(row)
            vec = batch.preembeddings.pop((row.source, row.msg_id), None)
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
        # 未被消费的 preembeddings（如整批被作用域过滤）随批上下文丢弃，
        # 不存在跨批串味：BatchContext 每批新建

    # ------------------------------------------------------------ 历史回填 --

    async def backfill_step(self, now_ts: int) -> int:
        """单轮有界回填：索引窗口内缺失/模型失配的 raw_messages。

        反连接条件天然可续跑：嵌入失败或预算截断的行下一轮仍会被选中；
        返回本轮处理条数，0 表示回填完成。backfill_days：>0 窗口天，
        0 关闭，-1 全量。last_cycle_embed_failed 供维护循环做失败退避。

        写路径持 pipeline 存储锁（本方法运行于维护循环、不在锁内）：
        chunks/FTS/skipped 落主连接，不变量见 db.storage_lock 的权威论证；
        嵌入（网络）在锁外。
        """

        self.last_cycle_embed_failed = False
        # 先建表再判 days=0：维护循环在全新库上也要能安全空转（GC/预热依赖表）
        db = await self._ensure_db_ready()
        if self.settings.backfill_days == 0:
            return 0
        model = ai_ports.embed_model_name()
        # 作用域谓词与检索侧共用单源（这里过滤 raw_messages，别名 r）；
        # NOT EXISTS 排除已登记 skipped 的「永不入索引」行——占位符消息永远
        # 满足反连接却永远不被消费，不排除会每轮占满预算窗口，令更早的
        # 真实消息饿死
        scope, scope_params = scope_sql(self.settings.group_only, alias="r")
        # 内容以既有 chunk 优先：raw_messages 在管道入口（OCR 之前）落库，而实时
        # 索引写入的是 OCR 增强后的文本；换嵌入模型回填时若用原文覆盖，会把 OCR
        # 文本从索引里抹掉，纯图片消息的原文还是占位符（_indexable 为假 → 登记
        # rag_skipped），旧向量永不更新。
        sql = (
            "SELECT r.source, r.msg_id, r.session_id, r.group_name, "
            "r.sender_name, r.sender_id, r.timestamp AS msg_time, "
            "COALESCE(c.content, r.content) AS content "
            "FROM raw_messages r "
            "LEFT JOIN rag_chunks c ON c.source = r.source AND c.msg_id = r.msg_id "
            "LEFT JOIN rag_chunk_embeddings e ON e.source = r.source "
            "AND e.msg_id = r.msg_id "
            "WHERE (c.msg_id IS NULL OR e.msg_id IS NULL OR e.model <> ?)"
            " AND trim(COALESCE(c.content, r.content), ' ' || char(9)) <> ''"
            " AND NOT EXISTS (SELECT 1 FROM rag_skipped k "
            "WHERE k.source = r.source AND k.msg_id = r.msg_id)"
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
                content=r["content"], sender_id=r["sender_id"],
            )
            for r in raw_rows
            if _indexable(r["content"])
        ]
        # 被过滤的「永不入索引」行登记 skipped（消费一次即永久出队）。
        # 登记 commit 与落库同持存储锁，此处仅计算键。
        skipped_keys = [
            (r["source"], r["msg_id"])
            for r in raw_rows
            if not _indexable(r["content"])
        ]
        if not rows:
            async with storage_lock:
                await mark_skipped(db, skipped_keys)
            return len(skipped_keys)
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
        async with storage_lock:
            if skipped_keys:
                await mark_skipped(db, skipped_keys)
            await self._persist_chunks(
                db,
                rows,
                [(r.source, r.msg_id) for r in rows[:paired]],
                vectors[:paired],
                model,
                sender_id_fill_only=True,
            )
        self.last_cycle_embed_failed = failed
        logger.info("rag: 回填本轮处理 %d 条（含向量 %d 条）", len(rows), paired)
        return len(rows)

    # ------------------------------------------------------------ 维护循环 --

    async def maintenance_gc(self) -> int:
        """孤儿对账：chunks/FTS 在主连接清，向量在专用连接清。

        写库持 pipeline 存储锁 + 共享 embed 连接互斥锁（与 backfill_step
        同一纪律；reindex 端点同样经此方法，锁语义单点收口）。存储锁的必要性：
        主连接上的 DELETE+commit 若与管道/删除路径的隐式多语句事务交叉，
        会把对方半程写入提前提交；embed 锁的必要性：
        gc_orphans 直接在专用连接上写而不自持锁，互斥须由调用方给出。
        """

        db = await self._db_factory()
        edb = await self._embed_factory()
        async with storage_lock, get_embed_lock():
            removed = await gc_orphans(db, edb)
        if removed:
            logger.info("rag: GC 清理孤儿索引 %d 行", removed)
        return removed

    async def warm_vectors(self, force_full: bool = False) -> None:
        """填充/刷新向量缓存；force_full 用于维护周期整表重扫（覆盖删除）。

        与 retrieve 的缓存刷新共用 _refresh_lock：维护循环与并发 ask 的
        水位/整表重建不交错（交错当前是良性的，但锁纪律名存实亡会诱使
        后续在 _refresh_vector_cache 中加入非幂等步骤）。

        force_full 只给查询传空水位，未切换模型时成功前保留原水位与计数；模型切换会在读取前清空缓存，切换后失败不保留原缓存内容。删除检测由全表 key 差集承担，失败后必须全量重试，不能退回不会裁剪旧 key 的增量读取。

        维度决策只看原水位闭区间内的解析行，空批才退回全表；安装仍用全表，避免旧维度左右新批决策或旧行身份漏更新。维度漂移在脏行删除成功后逐出旧缓存，全量读取不再递归。解析与脏行删除成功后才裁剪并重组矩阵，空表同步清空两者并更新水位与计数。矩阵构造失败的一致性不在保证内。
        """

        async with self._refresh_lock:
            await self._refresh_vector_cache(force_full=force_full)

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

    async def _refresh_vector_cache(self, *, force_full: bool = False) -> None:
        # 首个 await 前同时捕获世代与收尾状态，未收尾写入及刷新中的新登记
        # 不能随旧快照消账；退出时不重读收尾计数。
        invalidations = self._vec_invalidations
        settled = self._vec_unsettled_writes == 0
        previous_watermark = self._vec_watermark
        force_full = force_full or self._vec_cache_dirty or self._vec_full_retry
        if force_full:
            # 全量失败后必须整表重试：清缓存可能已使行数回退检测失效，
            # 普通增量读取又不裁剪 key 差集，否则已删内容会静默留在检索中。
            self._vec_full_retry = True
        model = ai_ports.embed_model_name()
        if self._vec_model != model:
            logger.info("rag: 嵌入模型切换 %s -> %s，重建向量缓存", self._vec_model, model)
            self._vec_cache_clear()
            self._vec_model = model
        edb = await self._embed_factory()
        # 共享 embed 连接上的读也持互斥锁：本方法结尾的 bad-key 删除会 commit，
        # 不持锁的 commit 可能把 dedup 未完成的批量向量写入提前提交
        async with get_embed_lock():
            row = await fetch_one(
                edb, "SELECT COUNT(*) AS c FROM rag_chunk_embeddings"
            )
            total = int(row["c"]) if row is not None else 0
            if not force_full and total < self._vec_count_seen:
                self._vec_cache_clear()  # 有删除（GC），水位失效，整表重建
            raw_rows, max_created = await fetch_new_embeddings(
                edb, model, "" if force_full else self._vec_watermark,
                self.settings.group_only,
            )
        if force_full:
            # 先记录整表 key 差集，解析与脏行删除成功后才裁剪缓存并重组矩阵；
            # 解析或脏行删除失败、取消时不能留下已裁剪字典与旧矩阵。
            fetched_keys = {(r["source"], r["msg_id"]) for r in raw_rows}
        if not raw_rows:
            if force_full:
                # 全表已空：同步清空缓存与矩阵，否则旧向量仍可参与检索。
                self._vec_entries.clear()
                self._rebuild_matrix()
                self._vec_watermark = max_created
                self._vec_count_seen = total
                self._vec_full_retry = False
            if settled:
                self._vec_applied_invalidations = max(
                    self._vec_applied_invalidations, invalidations
                )
            return
        entries, bad_keys = await asyncio.to_thread(parse_embedding_rows, raw_rows)
        # 维度对账：同模型名下供应商原地换维度或历史脏行会让
        # float32 矩阵重组抛 inhomogeneous shape、/api/rag/ask 持续 500 直到
        # 人工清表。以现有缓存维度为基准（无缓存取批内多数维度），不一致的
        # 行按脏行同款删除（回填反连接重嵌入，产出当前模型维度，全表收敛）；
        # 批内自洽但与缓存基准整体不同 = 供应商全局换维度，视同模型切换
        # 整表重建。不一致向量绝不进矩阵。
        full_rebuild = False
        if entries:
            decision_entries = entries
            if force_full and previous_watermark:
                recent_keys = {
                    (row["source"], row["msg_id"]) for row in raw_rows
                    if row["created_at"] >= previous_watermark
                }
                decision_entries = [
                    (chunk, vec) for chunk, vec in entries
                    if (chunk.source, chunk.msg_id) in recent_keys
                ] or entries
            batch_dims = {vec.shape[0] for _, vec in decision_entries}
            cached_dim = (
                next(iter(self._vec_entries.values()))[1].shape[0]
                if self._vec_entries
                else None
            )
            if cached_dim is None:
                if len(batch_dims) == 1:
                    ref_dim = next(iter(batch_dims))
                else:
                    counts: dict[int, int] = {}
                    for _, vec in decision_entries:
                        counts[vec.shape[0]] = counts.get(vec.shape[0], 0) + 1
                    ref_dim = max(
                        counts, key=lambda dimension: (counts[dimension], dimension)
                    )
            elif batch_dims == {cached_dim}:
                ref_dim = cached_dim
            elif len(batch_dims) == 1:
                logger.warning(
                    "rag: 向量维度漂移（缓存 %d 维 -> 批内 %d 维，模型名未变），"
                    "整表重建缓存；旧行由回填按当前维度重嵌入",
                    cached_dim, next(iter(batch_dims)),
                )
                ref_dim = next(iter(batch_dims))
                full_rebuild = True
            else:
                ref_dim = cached_dim
            kept: list[tuple[ChunkRow, np.ndarray]] = []
            for chunk, vec in entries:
                if vec.shape[0] == ref_dim:
                    kept.append((chunk, vec))
                else:
                    bad_keys.append((chunk.source, chunk.msg_id))
            entries = kept
        if bad_keys:
            await self._delete_bad_embeddings(edb, bad_keys)
        if full_rebuild:
            # 已删的旧维度 key 仍在 fetched_keys，差集裁剪不会逐出其缓存；
            # 删除成功后必须清缓存，否则新旧维度混装会让矩阵重组报形状不齐。
            self._vec_cache_clear()
        if force_full:
            for key in list(self._vec_entries):
                if key not in fetched_keys:
                    del self._vec_entries[key]
        for chunk, vec in entries:
            self._vec_entries[(chunk.source, chunk.msg_id)] = (chunk, vec)
        self._rebuild_matrix()
        self._vec_watermark = max_created
        self._vec_count_seen = total
        if force_full:
            self._vec_full_retry = False
        if full_rebuild and not force_full:
            # 增量漂移才再拉全表，把水位之前的当前维度行收进缓存；全量读取
            # 已安装全批，再递归会让原水位边界丢失，旧维度重新左右决策。
            self._vec_watermark = ""
            await self._refresh_vector_cache()
        # 递归重建可能已确认更新的世代，外层不能把确认计数倒退。
        if settled:
            self._vec_applied_invalidations = max(
                self._vec_applied_invalidations, invalidations
            )

    @staticmethod
    async def _delete_bad_embeddings(
        edb: aiosqlite.Connection, bad_keys: list[tuple[str, str]]
    ) -> None:
        # 专用连接上逐条原子删除；反连接下一轮自动重嵌入。
        # 持共享 embed 连接互斥锁（commit 不与 dedup 的批量写交错）。
        async with get_embed_lock():
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
            # 嵌入端点故障不应连坐关键词问答——降级 FTS-only，仍可回答可命中问题
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
    def _name_context(dispatch: ai_ports.RagDispatch) -> NameContext:
        """按**实际派发端点**决定这次请求要不要假名化。

        判定只看 `dispatch.api_base`，绝不拿本插件的 `settings.api_base`
        （override）代替：供应商没有专用 rag_chat 时，`ai_ports.rag_chat` 回退
        复用 `chat`，override 被静默丢弃、请求落到主端点。按 override 判成本地、
        请求却发往远端主机，姓名就被原样送出去了——这是静默泄露，不是报错
        （回归见 tests/test_rag_plugin.py 的
        test_rag_fallback_pseudonymizes_for_actual_main_endpoint）。

        本地端点给 disabled 上下文：名字原样透传，也不碰磁盘上的别名注册表。
        """
        enabled = pseudonymize_enabled(
            config.ai_pseudonymize_names,
            remote_endpoint=ai_ports.effective_remote(dispatch.api_base),
        )
        return NameContext(enabled=enabled)

    @staticmethod
    async def _prepare_evidence_names(
        context: NameContext, hits: list[Hit]
    ) -> list[EvidenceNames]:
        """准备每条证据行的展示名（按身份键复用同一化名）。

        收集与出站全部复用 pseudonym 模块的身份键单源（`person_identity_key` /
        `group_identity_key`）：本层另写一套键规则，就会出现「准备用的键」与
        「还原目标用的键」口径分叉——缺 ID 的发送者被错并成同一人（同会话两条
        证据共用一个化名、还原都还原成同一个人），或按残缺键查不到映射而静默
        回退真名出境。

        先按输入顺序收敛每个身份的最终显示名（空显示名跳过、不锁定空串：先空
        后真名的人拿真名出站，也拿真名还原）；再用最终显示名重算身份键并发取
        别名。缺 ID 时显示名参与身份键，最终名替换后键随名收敛——同一身份的
        每条证据从此只有一个键、一个化名、一个还原目标（别名调用发生在最终名
        上，逆映射登记的正是最终名）。人与群共享**一次** gather：冻结模块对新
        身份有合并落盘窗口，逐条 await 或分两轮 gather 会让每个窗口各等一次，
        证据越多准备越慢。

        启用假名化时出站映射只查身份键，查不到（映射遗漏）按准备失败处理，
        绝不静默回退真名；准备失败（注册表损坏、锁超时、写盘失败）同样向上抛，
        调用方据此让这次回答的请求根本不发生。不要捕获后回退原名——那等于把
        假名化失败悄悄升级成一次明文出境。
        """
        if not context.enabled:
            # 关闭时不做任何准备：每条证据用原值（含未清洗的空白），与未接入
            # 假名化时逐字节一致，也不产生任何读盘或 await。
            return [
                EvidenceNames(group=hit.chunk.group_name, sender=hit.chunk.sender_name)
                for hit in hits
            ]

        person_final: dict[str, str] = {}
        group_final: dict[str, str] = {}
        for hit in hits:
            chunk = hit.chunk
            if (chunk.sender_name or "").strip():
                pkey0 = person_identity_key(
                    source=chunk.source,
                    sender_id=chunk.sender_id,
                    session_id=chunk.session_id,
                    display_name=chunk.sender_name,
                )
                person_final[pkey0] = chunk.sender_name.strip()
            if (chunk.group_name or "").strip():
                gkey0 = group_identity_key(
                    source=chunk.source,
                    session_id=chunk.session_id,
                    group_name=chunk.group_name,
                )
                group_final[gkey0] = chunk.group_name.strip()

        # 第二轮：用最终显示名重算身份键。缺 ID 时最终名正是键的一部分，键随
        # 名收敛；同一身份的每条证据由此拿到同一个键。
        person_jobs: dict[str, tuple[str, str, str, str]] = {}
        group_jobs: dict[str, tuple[str, str, str]] = {}
        plans: list[tuple[str | None, str | None]] = []
        for hit in hits:
            chunk = hit.chunk
            pkey0 = person_identity_key(
                source=chunk.source,
                sender_id=chunk.sender_id,
                session_id=chunk.session_id,
                display_name=chunk.sender_name,
            )
            final_name = person_final.get(pkey0, "")
            if final_name:
                pkey = person_identity_key(
                    source=chunk.source,
                    sender_id=chunk.sender_id,
                    session_id=chunk.session_id,
                    display_name=final_name,
                )
                person_jobs.setdefault(
                    pkey,
                    (chunk.source, chunk.sender_id, chunk.session_id, final_name),
                )
            else:
                pkey = None
            gkey0 = group_identity_key(
                source=chunk.source,
                session_id=chunk.session_id,
                group_name=chunk.group_name,
            )
            final_group = group_final.get(gkey0, "")
            if final_group:
                gkey = group_identity_key(
                    source=chunk.source,
                    session_id=chunk.session_id,
                    group_name=final_group,
                )
                group_jobs.setdefault(
                    gkey, (chunk.source, chunk.session_id, final_group)
                )
            else:
                gkey = None
            plans.append((pkey, gkey))

        # 人与群一起进同一次 gather：窗口按「登记到提交」计时，分两轮 gather
        # 会让后一轮的新身份再各等一个窗口。别名结果按身份键归还。
        async def _person(job: tuple[str, str, str, str]) -> str:
            source, sender_id, session_id, name = job
            return await context.person_alias(
                source=source, sender_id=sender_id,
                session_id=session_id, display_name=name,
            )

        async def _group(job: tuple[str, str, str]) -> str:
            source, session_id, name = job
            return await context.group_alias(
                source=source, session_id=session_id, group_name=name,
            )

        jobs: list[tuple[str, Any]] = [
            *(("person", job) for job in person_jobs.values()),
            *(("group", job) for job in group_jobs.values()),
        ]
        aliases = (
            await asyncio.gather(
                *(_person(job) if kind == "person" else _group(job) for kind, job in jobs)
            )
            if jobs
            else []
        )
        alias_by_key: dict[str, str] = {}
        for (kind, job), alias in zip(jobs, aliases, strict=True):
            key = (
                person_identity_key(
                    source=job[0], sender_id=job[1],
                    session_id=job[2], display_name=job[3],
                )
                if kind == "person"
                else group_identity_key(
                    source=job[0], session_id=job[1], group_name=job[2]
                )
            )
            alias_by_key[key] = alias

        # 出站映射按预先算好的键投影；启用假名化后查不到映射（映射遗漏）按
        # 准备失败处理，绝不静默回退真名。整批无显示名的身份没有注册键，
        # 出站保持空串——空名不是身份，也不构成出境风险。
        names: list[EvidenceNames] = []
        for hit, (pkey, gkey) in zip(hits, plans, strict=True):
            if pkey is None:
                sender = ""
            else:
                sender = alias_by_key.get(pkey, "")
                if not sender:
                    raise PseudonymError("证据发送者别名缺失：出站映射不回退真名")
            if gkey is None:
                group = ""
            else:
                group = alias_by_key.get(gkey, "")
                if not group:
                    raise PseudonymError("证据群名别名缺失：出站映射不回退真名")
            names.append(EvidenceNames(group=group, sender=sender))
        return names

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
        """检索 + 引用式回答；检索为空 → 诚实拒答且不调用 AI。

        遥测恒等式：`rag.ask` = refused + answered + retrieve_error +
        answer_error + cancelled。`ask` 必须在检索**之前**计数——检索本身就会
        抛（嵌入端点故障、库损坏），计在之后意味着这些失败连分母都没有，读数
        永远看着「零失败」。取消单列：CancelledError 是 BaseException 子类，
        `except Exception` 兜不到它，而取消路径上「ask 已计、终态桶全空」会让
        恒等式失效。

        假名化准备失败落在 answer_error：这次回答的 create 没发生，与「请求发出
        但失败」在「没有答案」上同类；已发生的查询嵌入仍如实留在 embed 桶里
        （那是另一个端口，无法随本次失败回滚）。
        """

        from briefdesk import telemetry

        telemetry.record("rag.ask")
        try:
            hits = await self.retrieve(question, session_id)
        except asyncio.CancelledError:
            telemetry.record("rag.cancelled")
            raise
        except Exception:
            telemetry.record("rag.retrieve_error")
            raise
        if not hits:
            telemetry.record("rag.refused")
            return AskResult(refused=True, answer="没有在群聊记录里找到相关消息。")
        # 问答模型通道 override 来自本插件配置域（三项留空 = 复用主链路 AI）。
        # 先按同一三元组取一次**实际派发计划**，再用它决定要不要假名化、并用
        # 完全相同的值发送：计划与发送描述同一件事，否则回退到主端点的请求会
        # 按 override 判成本地而带上真名。
        override = {
            "model": self.settings.model,
            "api_base": self.settings.api_base,
            "api_key": self.settings.api_key.get_secret_value(),
        }
        try:
            dispatch = ai_ports.resolve_rag_dispatch(**override)
            context = self._name_context(dispatch)
            names = await self._prepare_evidence_names(context, hits)
            prompt_history = history or []
            if context.enabled and prompt_history:
                # 历史含上轮还原的真名，当前证据映射无法覆盖已离开命中集的人；
                # 启用时整段丢弃，避免还原结果再次出境。
                prompt_history = []
                telemetry.record("rag.history_dropped")
            messages = build_answer_prompt(
                datetime.now(UTC).astimezone(), question.strip(), hits, prompt_history,
                self.settings.evidence_chars, names=names,
            )
            resp = await ai_ports.rag_chat(
                messages,
                temperature=0.2,
                max_tokens=1024,
                **override,
            )
            content = ""
            choices = getattr(resp, "choices", None) or []
            if choices:
                content = (choices[0].message.content or "").strip()
            if not content:
                # 空 choices / 空答案：按拒答返回，不伪装成「已答但无引用」
                logger.warning(
                    "rag: 模型返回空响应（choices=%d），按拒答处理", len(choices)
                )
                telemetry.record("rag.refused")
                return AskResult(refused=True, answer="模型未返回内容，请稍后重试。")
            # 双态解析：deepseek 系强制 json_object 输出 → 优先 JSON 契约；
            # 其它供应商纯文本 → 回退 [n] 正则（兼容两路）
            answer = content
            cited: set[int] = set()
            explicit = False  # 模型给出了 citations 列表（含空列表）→ 不再回退全部证据
            parsed = ai_ports.loads_json(content)
            if isinstance(parsed, dict) and isinstance(parsed.get("answer"), str):
                answer = parsed["answer"].strip()
                if not answer:
                    logger.warning("rag: JSON 契约下 answer 为空，按拒答处理")
                    telemetry.record("rag.refused")
                    return AskResult(refused=True, answer="模型未返回内容，请稍后重试。")
                nums = parsed.get("citations")
                if isinstance(nums, list):
                    explicit = True
                    for n in nums:
                        v = _citation_index(n)
                        if v is not None and 1 <= v <= len(hits):
                            cited.add(v)
            if not cited and not explicit:
                cited = {
                    int(m.group(1))
                    for m in self._CITE_RE.finditer(content)
                    if 1 <= int(m.group(1)) <= len(hits)
                }
                if cited:
                    # 与「无专用通道」是两种故障：这条是 JSON 契约没生效、引用
                    # 只能从正文 [n] 里正则捡回来，丢的是结构化引用（而非模型
                    # override）。命中才记——正则一无所获属另一形态（回退全部
                    # 证据），记进来会把两种不同成因混成一个桶。
                    telemetry.record("rag.citation_fallback")
            if explicit:
                nums = sorted(cited)
            else:
                nums = sorted(cited) if cited else list(range(1, len(hits) + 1))
            # 入站还原只作用在答案自由文本上：此时两种输出形态（JSON 契约与纯文本）
            # 都已经解析完，剩下的是要给人看的文字。引用编号、source/msg_id 与证据
            # 下标原样保留——化名只替换显示了身份的那部分文字，编号不是身份。
            # 还原失败（模型把化名抄错）保留化名并计遥测，不改发真名、不重试明文。
            answer = context.restore(answer, field="answer")
            citations = [self._citation(n, hits[n - 1]) for n in nums]
        except asyncio.CancelledError:
            telemetry.record("rag.cancelled")
            raise
        except Exception:
            telemetry.record("rag.answer_error")
            raise
        telemetry.record("rag.answered")
        # 引用维度单独分桶：answered 是两者的分母，「有引用的回答占比」
        # 由 with/no_citation 两桶读出（空引用列表也是有效回答形态）
        if citations:
            telemetry.record("rag.answered_with_citation")
        else:
            telemetry.record("rag.answered_no_citation")
        return AskResult(refused=False, answer=answer, citations=citations)


_instance: RagEngine | None = None


def set_engine(engine: RagEngine | None) -> None:
    """注入/清除引擎实例（rag 插件 setup/teardown 调用）。"""

    global _instance
    _instance = engine


def get_engine() -> RagEngine | None:
    return _instance
