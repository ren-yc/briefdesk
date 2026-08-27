"""AI 供应商端口 — ai_provider 插件在 setup 阶段注册实例，
引擎（classify/dedup/merge）经本模块端口函数调用，核心不依赖具体供应商实现。

- chat / embed_texts / is_embedding_enabled / embed_model_name：转发到
  已注册的 AIProvider 实例（未注册时 chat/embed 抛 RuntimeError 明示
  配置错误，启用性检查安全返回 False）；
- loads_json / top_k_similar：供应商无关的纯工具（JSON 修复解析、
  余弦 Top-K 候选选择）。

模块级单例（与 stages/realtime 同风格）；测试用 set_ai(None) 复位。
"""

from __future__ import annotations

import json

import numpy as np
from json_repair import loads as json_repair_loads

from briefdesk.plugin.base import AIProvider, ChatResponse

_ai: AIProvider | None = None


def set_ai(provider: AIProvider | None) -> None:
    """注入/清除 AI 供应商（ai_provider 插件 setup/teardown 调用）。"""
    global _ai
    _ai = provider


def get_ai() -> AIProvider | None:
    return _ai


def _require_ai() -> AIProvider:
    if _ai is None:
        raise RuntimeError("AI 供应商未注册（ai_provider 插件未启用）")
    return _ai


async def chat(
    messages: list[dict],
    *,
    temperature: float = 0.3,
    max_tokens: int = 4096,
) -> ChatResponse:
    """统一 AI 调用端口（模型/供应商由已注册插件决定）。"""
    return await _require_ai().chat(
        messages, temperature=temperature, max_tokens=max_tokens
    )


async def embed_texts(texts: list[str]) -> list[list[float]]:
    """批量嵌入端口。"""
    return await _require_ai().embed_texts(texts)


def is_embedding_enabled() -> bool:
    """嵌入是否启用（供应商未注册或未配置 EMBED_API_BASE → False）。"""
    ai = get_ai()
    return ai is not None and ai.is_embedding_enabled()


def embed_model_name() -> str:
    """嵌入模型名：优先供应商声明；未注册时回退 config（与旧实现一致，
    引擎/实验脚本在无供应商场景下仍可离线构造缓存）。"""
    ai = get_ai()
    if ai is not None:
        return ai.embed_model_name()
    from briefdesk.config import config

    return config.embed_model or config.ai_model


def loads_json(text: str, *, repair: bool = True) -> object | None:
    """标准解析优先，失败后用 json_repair 修复兜底（原 ai/client.py）。

    json_repair 可修复常见模型输出瑕疵：缺引号/单引号/尾随逗号、
    markdown 围栏残留、前后叙述文本混排、截断补全等。
    repair=False（finish_reason=length 的截断输出）不做修复：截断的
    字符串会被"补全"成残缺值（如残缺标题覆盖原标题），不可信任。
    修复仍是宽松解析：调用方必须继续做 task 外壳与字段类型校验
    （如截断布尔值会被补成字符串，由类型校验拦截，不会误判）。
    无法解析（异常）返回 None；json_repair 对纯文本会返回 ""，
    同样由调用方的结构校验拦截。
    """
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        if not repair:
            return None
        try:
            return json_repair_loads(text)
        except Exception:  # noqa: BLE001 — json_repair 异常统一视为无法解析
            return None


def top_k_similar(
    query_embedding: list[float] | np.ndarray,
    item_embeddings: list[list[float]] | np.ndarray,
    top_k: int,
    threshold: float,
) -> list[tuple[int, float]]:
    """返回 (item_embeddings 中的下标, 余弦相似度) 列表，按相似度降序。

    仅保留相似度 >= threshold 的项，最多 top_k 条。空输入/零向量安全（+1e-12 防 NaN）。
    """
    q = np.asarray(query_embedding, dtype=np.float32)
    m = np.asarray(item_embeddings, dtype=np.float32)
    if m.size == 0:
        return []
    q = q / (np.linalg.norm(q) + 1e-12)
    m = m / (np.linalg.norm(m, axis=1, keepdims=True) + 1e-12)
    sims = m @ q
    # 稳定排序：并列相似度按原始下标序，保证跨次调用确定性（rag 检索依赖）
    ranked = np.argsort(-sims, kind="stable")[:top_k]
    return [(int(i), float(sims[i])) for i in ranked if sims[i] >= threshold]
