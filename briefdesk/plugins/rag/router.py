"""RAG 路由组（rag WebPlugin）— /api/rag/ask | /api/rag/status | /api/rag/reindex。

引擎实例经模块级单例获取（plugin.setup 注入）；未就绪统一 503。
Host 白名单与同源校验由核心中间件覆盖，本路由不做额外鉴权。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from briefdesk import ai_ports
from briefdesk.db import get_db
from briefdesk.plugins.rag.db import count_status, gc_orphans
from briefdesk.plugins.rag.engine import RagEngine, get_engine

router = APIRouter()


class CitationModel(BaseModel):
    n: int
    msg_id: str
    source: str
    session_id: str
    sender_name: str
    time: int  # 秒级 epoch（相对时间由前端渲染）
    group_name: str
    snippet: str
    item_id: str


class AskRequest(BaseModel):
    question: str = Field(min_length=2, max_length=500)
    session_id: str | None = None
    # 对话历史（多轮上下文；校验角色并截断防御）
    history: list[dict] = Field(default_factory=list, max_length=40)

    @field_validator("question")
    @classmethod
    def _question_not_blank(cls, v: str) -> str:
        # strip 后仍须 ≥2 字符：纯空白/单字问题直接 422，而非 200 拒答
        stripped = v.strip()
        if len(stripped) < 2:
            raise ValueError("question 需至少 2 个非空白字符")
        return stripped

    @field_validator("history")
    @classmethod
    def _history_sanitized(cls, v: list[dict]) -> list[dict]:
        """仅保留 user/assistant 角色且内容为字符串的条目，逐条截断。"""

        out: list[dict] = []
        for item in v:
            if not isinstance(item, dict):
                continue
            role = item.get("role")
            content = item.get("content", "")
            if role in ("user", "assistant") and isinstance(content, str):
                out.append({"role": role, "content": content[:2000]})
        return out[-20:]  # 至多 20 条历史


class AskResponse(BaseModel):
    refused: bool
    answer: str
    citations: list[CitationModel]


def _require_engine() -> RagEngine:
    engine = get_engine()
    if engine is None:
        raise HTTPException(503, "rag 插件未就绪")
    return engine


@router.post("/api/rag/ask", response_model=AskResponse)
async def rag_ask(req: AskRequest) -> AskResponse:
    """检索式问答：带原文引用、低置信诚实拒答。"""

    engine = _require_engine()
    result = await engine.ask(req.question.strip(), req.session_id, req.history)
    return AskResponse(
        refused=result.refused,
        answer=result.answer,
        citations=[CitationModel(**c) for c in result.citations],
    )


@router.get("/api/rag/status")
async def rag_status() -> dict:
    """索引状态：条数/嵌入模型/FTS 可用性/回填窗口。"""

    engine = _require_engine()
    db = await get_db()
    st = await count_status(db)
    return {
        "chunks": st["rag_chunks"],
        "embedded": st["rag_chunk_embeddings"],
        "fts_tokenizer": st["fts_tokenizer"],
        "model": ai_ports.embed_model_name(),
        "backfill_days": engine.settings.backfill_days,
    }


@router.post("/api/rag/reindex", status_code=202)
async def rag_reindex() -> dict:
    """孤儿对账清理（GC）并触发新一轮补齐回填（有界、可续跑）。"""

    engine = _require_engine()
    removed = await engine.maintenance_gc()
    kicked = engine.request_backfill()
    return {"removed": removed, "kicked": kicked}
