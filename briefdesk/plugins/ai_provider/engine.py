"""OpenAI 兼容 AI 供应商引擎 — chat（共享客户端 / thinking 与 JSON 开关 / 并发预算）与嵌入。

P4 起由 ai_provider 插件持有本引擎；引擎模块（classify/dedup/merge）不再
直接 import 本模块，统一经 core 的 briefdesk.ai_ports 端口函数调用。
loads_json / top_k_similar 为供应商无关工具，已收归 briefdesk.ai_ports，
此处 re-export 供实验脚本与测试路径兼容。
"""

import asyncio
from typing import cast

from openai import AsyncOpenAI
from openai.types import CreateEmbeddingResponse
from openai.types.chat import ChatCompletion, ChatCompletionMessageParam

from briefdesk.ai_ports import loads_json, top_k_similar  # noqa: F401 — re-export
from briefdesk.config import config
from briefdesk.plugin.base import AIProvider, ChatResponse

_client: AsyncOpenAI | None = None
_ai_semaphore: asyncio.Semaphore | None = None
_embed_client: AsyncOpenAI | None = None


def get_ai_client() -> AsyncOpenAI:
    """获取共享的 AsyncOpenAI 实例（延迟初始化）。"""
    global _client
    if _client is None:
        _client = AsyncOpenAI(
            api_key=config.ai_api_key,
            base_url=config.ai_api_base,
        )
    return _client


def get_ai_semaphore() -> asyncio.Semaphore | None:
    """按 AI_MAX_CONCURRENCY 创建限流信号量，0 表示不限制。

    chat 与 embedding 共用同一并发预算，避免本地模型（并发上限 1）被同时打满。
    """
    global _ai_semaphore
    limit = config.ai_max_concurrency
    if limit <= 0:
        return None
    if _ai_semaphore is None:
        _ai_semaphore = asyncio.Semaphore(limit)
    return _ai_semaphore


def _use_json_object() -> bool:
    """deepseek-v4-flash/pro 与 ollama 兼容端点启用严格 JSON 输出。

    json_object 模式强制模型只输出合法 JSON——配合 classify/summarize/times
    的外壳（{"task":...} 对象根）与 dedup/merge/title 的裸对象输出，
    下游解析不再面对叙述性文本/裸数组等跑偏形态。
    模型名用包含匹配，兼容带前缀的 vendor 命名（如 deepseek/deepseek-v4-flash）。
    """
    if config.ai_api_key == "ollama":
        return True
    return "deepseek-v4-flash" in config.ai_model or "deepseek-v4-pro" in config.ai_model


async def chat(
    messages: list[ChatCompletionMessageParam],
    *,
    temperature: float = 0.3,
    max_tokens: int = 4096,
) -> ChatCompletion:
    """统一 AI 调用入口，模型名从 config.ai_model 读取。"""
    client = get_ai_client()

    async def _create() -> ChatCompletion:
        json_object = _use_json_object()
        if config.ai_disable_thinking:
            if json_object:
                return await client.chat.completions.create(
                    model=config.ai_model,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    reasoning_effort="none",
                    response_format={"type": "json_object"},
                )
            return await client.chat.completions.create(
                model=config.ai_model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                reasoning_effort="none",
            )
        if json_object:
            return await client.chat.completions.create(
                model=config.ai_model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                response_format={"type": "json_object"},
            )
        return await client.chat.completions.create(
            model=config.ai_model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )

    sem = get_ai_semaphore()
    if sem is None:
        return await _create()
    async with sem:
        return await _create()


def is_embedding_enabled() -> bool:
    """EMBED_API_BASE 非空即启用嵌入去重；留空则完全禁用（向后兼容）。"""
    return bool(config.embed_api_base)


def embed_api_base() -> str:
    """嵌入 API 地址，未单独配置时回退到 AI_API_BASE。"""
    return config.embed_api_base or config.ai_api_base


def embed_api_key() -> str:
    """嵌入 API Key，未单独配置时回退到 AI_API_KEY。"""
    return config.embed_api_key or config.ai_api_key


def embed_model_name() -> str:
    """嵌入模型名，未单独配置时回退到 AI_MODEL。"""
    return config.embed_model or config.ai_model


def get_embed_client() -> AsyncOpenAI:
    """获取共享的嵌入客户端（延迟初始化，独立于 chat 客户端）。"""
    global _embed_client
    if _embed_client is None:
        _embed_client = AsyncOpenAI(
            api_key=embed_api_key(),
            base_url=embed_api_base(),
        )
    return _embed_client


async def embed_texts(texts: list[str]) -> list[list[float]]:
    """批量嵌入文本，返回与输入同序的向量列表。按 EMBED_BATCH_SIZE 分批。"""
    if not texts:
        return []
    batch_size = max(1, config.embed_batch_size)
    client = get_embed_client()
    sem = get_ai_semaphore()

    async def _create(chunk: list[str]) -> CreateEmbeddingResponse:
        return await client.embeddings.create(model=embed_model_name(), input=chunk)

    results: list[list[float]] = []
    for i in range(0, len(texts), batch_size):
        chunk = texts[i : i + batch_size]
        if sem is None:
            resp = await _create(chunk)
        else:
            async with sem:
                resp = await _create(chunk)
        # 按 index 排序保证与输入同序（防御性）
        ordered = sorted(resp.data, key=lambda d: d.index)
        results.extend(d.embedding for d in ordered)
    return results


class Provider(AIProvider):
    """OpenAI 兼容供应商实现（显式实现 AIProvider 端口）。

    薄封装：chat / 嵌入直接委托本模块的共享客户端实现。
    """

    async def chat(
        self,
        messages: list[dict],
        *,
        temperature: float,
        max_tokens: int,
    ) -> ChatResponse:
        return cast(
            ChatResponse,
            await chat(
                cast(list[ChatCompletionMessageParam], messages),
                temperature=temperature,
                max_tokens=max_tokens,
            ),
        )

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return await embed_texts(texts)

    def is_embedding_enabled(self) -> bool:
        return is_embedding_enabled()

    def embed_model_name(self) -> str:
        return embed_model_name()
