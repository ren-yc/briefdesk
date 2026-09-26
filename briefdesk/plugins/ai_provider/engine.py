"""OpenAI 兼容 AI 供应商引擎 — chat（共享客户端 / thinking 与 JSON 开关 / 并发预算）与嵌入。

由 ai_provider 插件持有本引擎；引擎模块（classify/dedup/merge）不直接
import 本模块，统一经 core 的 briefdesk.ai_ports 端口函数调用。
loads_json / top_k_similar 为供应商无关工具，定义收归 briefdesk.ai_ports，
此处 re-export 供实验脚本与测试路径兼容。
"""

import asyncio
import logging
from typing import Any, cast

from openai import AsyncOpenAI
from openai.types import CreateEmbeddingResponse
from openai.types.chat import ChatCompletion, ChatCompletionMessageParam

from briefdesk import announcements
from briefdesk.ai_ports import loads_json, top_k_similar  # noqa: F401 — re-export
from briefdesk.config import config
from briefdesk.plugin.base import AIProvider, ChatResponse

logger = logging.getLogger(__name__)

_client: AsyncOpenAI | None = None
_ai_semaphore: asyncio.Semaphore | None = None
_embed_client: AsyncOpenAI | None = None
# 备用通道客户端缓存：键为 (base_url, api_key)。调用方（如 rag 插件）从自己的
# 配置域传入 override，同一组合复用同一实例，避免逐次调用重建连接池。
# 与 _client/_embed_client 同为模块级惰性缓存，不额外引入生命周期管理。
_alt_clients: dict[tuple[str, str], AsyncOpenAI] = {}


# 上游 SDK 默认 read 超时 600s：AI 端点挂起时单请求可拖满 10 分钟，而 dedup/
# merge 的判定请求在存储锁内执行，会冻结整条管道——显式收紧；重试次数同样
# 显式声明（SDK 默认 2），防上游默认变化静默改变行为。
_REQUEST_TIMEOUT = 120.0
_REQUEST_MAX_RETRIES = 2


def get_ai_client() -> AsyncOpenAI:
    """获取共享的 AsyncOpenAI 实例（延迟初始化）。"""
    global _client
    if _client is None:
        _client = AsyncOpenAI(
            api_key=config.ai_api_key.get_secret_value(),
            base_url=config.ai_api_base,
            timeout=_REQUEST_TIMEOUT,
            max_retries=_REQUEST_MAX_RETRIES,
        )
    return _client


def get_alt_client(api_base: str = "", api_key: str = "") -> AsyncOpenAI:
    """按 override 取备用通道客户端；两项都留空即返回主 AI 客户端。

    本函数只提供「换端点/换 Key」的机制，不知道调用方是谁——具体取值由调用
    方从自己的配置域传入（rag 插件传 RAG_API_BASE/RAG_API_KEY）。单项留空即
    该项回退主 AI 配置，便于「只换模型不换端点」这类组合。
    """
    base = api_base or config.ai_api_base
    key = api_key or config.ai_api_key.get_secret_value()
    if not api_base and not api_key:
        return get_ai_client()
    cached = _alt_clients.get((base, key))
    if cached is None:
        cached = AsyncOpenAI(
            api_key=key,
            base_url=base,
            timeout=_REQUEST_TIMEOUT,
            max_retries=_REQUEST_MAX_RETRIES,
        )
        _alt_clients[(base, key)] = cached
    return cached


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

    显式开关 AI_JSON_MODE=on/off 优先于启发式：非默认 key 的 ollama 端点、
    或其它支持 json_object 的模型可经 on 强制开启；需要关闭时用 off。
    """
    mode = config.ai_json_mode
    if mode == "on":
        return True
    if mode == "off":
        return False
    if config.ai_api_key.get_secret_value() == "ollama":
        return True
    return "deepseek-v4-flash" in config.ai_model or "deepseek-v4-pro" in config.ai_model


def _norm_base(base: str) -> str:
    """端点归一化：去尾斜杠并小写——同一端点的两种写法不应各记一份能力。"""
    return (base or "").strip().rstrip("/").lower()


def _effort_key(base: str, model: str) -> tuple[str, str]:
    """推理强度的「端点 + 模型」标识。

    用**生效值**而不是原始入参：rag_chat 的 override 留空会回退到主客户端与
    config.ai_model（见 get_alt_client 与 chat_model），否则主通道与 RAG 会被当成
    两个端点；自适应降级的能力记忆也以它为键。
    """
    return (_norm_base(base or config.ai_api_base), (model or config.ai_model).strip())


def _reasoning_kwargs(key: tuple[str, str]) -> dict[str, Any]:
    """按 AI_REASONING_EFFORT 给出要发送的推理强度参数（{} = 不发送）。

    auto / omit：不发送；off：发送 "none" 尽力关闭思考（端点明确拒收时由调用方改为
    不发送）；其余取值固定发送该强度。key 供降级路径记录「该端点拒收过该参数」。
    """
    mode = config.ai_reasoning_effort
    if mode in ("auto", "omit"):
        return {}
    return {"reasoning_effort": "none" if mode == "off" else mode}


async def chat(
    messages: list[ChatCompletionMessageParam],
    *,
    temperature: float = 0.3,
    max_tokens: int = 4096,
    timeout: float | None = None,
    max_retries: int | None = None,
) -> ChatCompletion:
    """统一 AI 调用入口，模型名从 config.ai_model 读取。

    timeout：单请求超时覆盖（秒），None 用客户端默认（_REQUEST_TIMEOUT）。
    max_retries：重试次数覆盖（None 用客户端默认 _REQUEST_MAX_RETRIES）；
    判官类调用传 0——SDK 默认重试 2 次会把锁内单次判官的最坏耗时放大 3 倍。
    """
    client = get_ai_client()
    if max_retries is not None:
        # 共享底层 http 连接池，仅覆盖该次调用的重试策略
        client = client.with_options(max_retries=max_retries)
    per_request: dict = {"timeout": timeout} if timeout is not None else {}

    key = _effort_key(config.ai_api_base, config.ai_model)

    async def _create() -> ChatCompletion:
        json_object = _use_json_object()
        extra: dict = {"response_format": {"type": "json_object"}} if json_object else {}
        return await client.chat.completions.create(
            model=config.ai_model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            # SDK 的 create() 把 reasoning_effort 声明成字面量联合，动态拼参数会让
            # mypy 判定「没有匹配的重载」；这里显式放宽为 Any，语义不变
            **cast(Any, _reasoning_kwargs(key)),
            **extra,
            **per_request,
        )

    sem = get_ai_semaphore()
    if sem is None:
        return await _create()
    if timeout is None:
        async with sem:
            return await _create()
    # 判官类调用：许可等待也计入 timeout，锁内最坏持有 = timeout（请求）+ timeout（排队）。
    # 此前 async with sem 的排队等待不在任何超时之内。
    await asyncio.wait_for(sem.acquire(), timeout)
    try:
        return await _create()
    finally:
        sem.release()


async def close_clients() -> None:
    """关闭并清空全部惰性缓存的 OpenAI 客户端（插件 teardown 调用；同一实例只关一次）。

    AsyncOpenAI 持有底层 http 连接池，进程内不复用却继续存活会让退出时残留
    连接；_alt_clients 按 (base_url, api_key) 去重，同一实例可能同时在主缓存
    与备用缓存里，按对象身份去重后再关。
    """
    global _client, _embed_client, _ai_semaphore
    targets = {
        id(c): c
        for c in (_client, _embed_client, *_alt_clients.values())
        if c is not None
    }
    _client = None
    _embed_client = None
    _alt_clients.clear()
    _ai_semaphore = None
    for c in targets.values():
        try:
            await c.close()
        except Exception as e:  # noqa: BLE001 — 关闭失败不阻断 teardown
            logger.debug("关闭 AI 客户端失败（忽略）: %r", e)


async def rag_chat(
    messages: list[ChatCompletionMessageParam],
    *,
    temperature: float = 0.2,
    max_tokens: int = 1024,
    model: str = "",
    api_base: str = "",
    api_key: str = "",
) -> ChatCompletion:
    """RAG 问答专用入口：模型/端点/Key 由调用方传入，留空逐项回退主 AI 配置。

    override 取值由调用方（rag 插件）从自己的配置域给出，本模块不读 `RAG_*`
    ——避免 ai_provider 反向依赖依赖它的插件。

    与 chat 的差异：不强制 JSON 外壳（问答为正文；引用由 RAG prompt 约束）；
    仍遵守 AI_REASONING_EFFORT 与并发信号量。
    """
    client = get_alt_client(api_base, api_key)
    chat_model = model or config.ai_model

    key = _effort_key(api_base or config.ai_api_base, chat_model)

    async def _create() -> ChatCompletion:
        return await client.chat.completions.create(
            model=chat_model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            **cast(Any, _reasoning_kwargs(key)),
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
    return (config.embed_api_key or config.ai_api_key).get_secret_value()


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
            timeout=_REQUEST_TIMEOUT,
            max_retries=_REQUEST_MAX_RETRIES,
        )
    return _embed_client


# 公告 code：嵌入持续性条件的顶部横幅（注册表见 briefdesk.announcements）
_ANNOUNCE_EMBEDDING_DISABLED = "embedding_disabled"
_ANNOUNCE_EMBEDDING_UNREACHABLE = "embedding_unreachable"

_EMBEDDING_DISABLED_MESSAGE = (
    "嵌入模型未启用（EMBED_API_BASE 未配置）：RAG 向量检索不可用，语义去重降级"
    "为字符重叠。可在 .env 配置 EMBED_API_BASE / EMBED_MODEL 后重启应用生效。"
)


def _embedding_unreachable_message() -> str:
    return (
        f"嵌入服务不可用或异常（{embed_api_base()}，模型 {embed_model_name()}）："
        "RAG 向量检索与向量去重暂时降级；服务恢复后本公告自动消失。"
    )


async def announce_embedding_state() -> None:
    """setup 钩子：按嵌入配置置位/撤销"未启用"公告（可达性由运行时探测）。"""
    if is_embedding_enabled():
        await announcements.revoke(_ANNOUNCE_EMBEDDING_DISABLED)
    else:
        await announcements.announce(
            _ANNOUNCE_EMBEDDING_DISABLED, "warning", _EMBEDDING_DISABLED_MESSAGE
        )


async def _announce_embed_failure() -> None:
    """嵌入调用失败时的公告分流：已配置 → 不可达；未配置 → 归入未启用。"""
    if is_embedding_enabled():
        await announcements.announce(
            _ANNOUNCE_EMBEDDING_UNREACHABLE,
            "warning",
            _embedding_unreachable_message(),
        )
    else:
        await announcements.announce(
            _ANNOUNCE_EMBEDDING_DISABLED, "warning", _EMBEDDING_DISABLED_MESSAGE
        )


async def embed_texts(texts: list[str]) -> list[list[float]]:
    """批量嵌入文本，返回与输入同序的向量列表。按 EMBED_BATCH_SIZE 分批。

    失败/恢复联动公告（所有嵌入调用方的唯一咽喉）：任一异常置位
    embedding_unreachable（EMBED_API_BASE 未配置时归入 embedding_disabled），
    全部成功撤销——公告随条件自动出现与消失。
    """
    if not texts:
        return []
    batch_size = max(1, config.embed_batch_size)
    client = get_embed_client()
    sem = get_ai_semaphore()

    async def _create(chunk: list[str]) -> CreateEmbeddingResponse:
        return await client.embeddings.create(model=embed_model_name(), input=chunk)

    try:
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
            if len(ordered) != len(chunk):
                # 数量不符即整批失败（调用方 preembed_batch 已有整批回退路径）：
                # 少返时后续文本向量整体错位，错位一旦 upsert 进 item_embeddings
                # （按 item_id 持久化）将永久污染余弦通道且重启不自愈
                raise ValueError(
                    f"嵌入返回数量不符：请求 {len(chunk)} 条，实际 {len(ordered)} 条"
                )
            # 数量相符但 index 序列非 0..n-1 连续（端点返回重复/错乱
            # index）时，排序无法纠偏，向量仍整体错位——同样整批失败防污染。
            if any(d.index != i for i, d in enumerate(ordered)):
                raise ValueError(
                    f"嵌入返回 index 序列不连续：期望 0..{len(chunk)-1}，"
                    f"实际 {[d.index for d in ordered]}"
                )
            results.extend(d.embedding for d in ordered)
    except Exception:
        await _announce_embed_failure()
        raise
    await announcements.revoke(_ANNOUNCE_EMBEDDING_UNREACHABLE)
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
        timeout: float | None = None,
        max_retries: int | None = None,
    ) -> ChatResponse:
        return cast(
            ChatResponse,
            await chat(
                cast(list[ChatCompletionMessageParam], messages),
                temperature=temperature,
                max_tokens=max_tokens,
                timeout=timeout,
                max_retries=max_retries,
            ),
        )

    async def rag_chat(
        self,
        messages: list[dict],
        *,
        temperature: float,
        max_tokens: int,
        model: str = "",
        api_base: str = "",
        api_key: str = "",
    ) -> ChatResponse:
        return cast(
            ChatResponse,
            await rag_chat(
                cast(list[ChatCompletionMessageParam], messages),
                temperature=temperature,
                max_tokens=max_tokens,
                model=model,
                api_base=api_base,
                api_key=api_key,
            ),
        )

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return await embed_texts(texts)

    def is_embedding_enabled(self) -> bool:
        return is_embedding_enabled()

    def embed_model_name(self) -> str:
        return embed_model_name()
