"""OpenAI 兼容 AI 供应商引擎 — chat（共享客户端 / 推理强度与 JSON 开关 / 并发预算）与嵌入。

由 ai_provider 插件持有本引擎；引擎模块（classify/dedup/merge）不直接
import 本模块，统一经 core 的 briefdesk.ai_ports 端口函数调用。供应商无关的
工具（JSON 修复解析、余弦相似度）定义在 briefdesk.ai_ports，本模块不 re-export
——需要它们的一方直接 import 端口模块，避免同一符号两处来源。

出境观测也在这里接线：每次显式 create 之前计一次发送尝试（本地/远端分桶），
并用 httpx 请求钩子把 SDK 内部重试单独计数——口径说明见下方 `_sending` 与
`_on_sdk_request`。
"""

import asyncio
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, cast

import httpx
from openai import (
    APIStatusError,
    AsyncOpenAI,
    BadRequestError,
    DefaultAsyncHttpxClient,
    UnprocessableEntityError,
)
from openai.types import CreateEmbeddingResponse
from openai.types.chat import ChatCompletion, ChatCompletionMessageParam

from briefdesk import ai_egress, announcements
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


# ── 出境观测：发送边界计数与 SDK 重试分桶 ──
#
# 口径：计数落在「已拿到并发许可、即将 create」这一刻，而不是端口调用或网络包。
# 端口调用会多记（排队失败、替身短路、参数校验失败都没把请求发出去），网络包会
# 少记（SDK 内部重试被当成新的逻辑发送）。前者会让「没发出去」看起来像出境，
# 后者会把一次被重试两次的 create 读成三次请求。


class _SdkRetryTracker:
    """一次显式 create 期间的重试观测状态（请求局部，create 结束即丢弃）。

    每次 create 各建一份：同一序号在重定向时会再次经过请求钩子，靠 `seen` 去重
    才能保证一次内部重试只 +1；effort 降级会重新 create，因此也用新集合，
    不把两次逻辑发送的重试序号混在一起。
    """

    __slots__ = ("is_local", "port", "role", "seen")

    def __init__(self, port: str, role: str, *, is_local: bool) -> None:
        self.port = port
        self.role = role
        self.is_local = is_local
        self.seen: set[int] = set()


# 当前任务正在进行的 create 的观测状态；未处于任何 create 内时为 None（钩子直接
# 返回）。用 ContextVar 而不是模块级变量：并发的多个 create 分属不同任务，
# 共享一份状态会把 A 的重试记到 B 的端口上。
_RETRY_TRACKER: ContextVar[_SdkRetryTracker | None] = ContextVar(
    "briefdesk_ai_sdk_retry_tracker", default=None
)

# SDK 把已用的重试次数写在请求头里（首次为 0，每次重试 +1）。本仓锁定的 openai
# 客户端写的是带前缀的 `x-stainless-retry-count`；同时接受不带前缀的短名，避免换
# SDK 或换兼容客户端后重试观测静默归零——「真的没重试」与「没读到头」在读数上
# 长得一模一样，看不出区别就只能靠猜。
_RETRY_HEADER_NAMES = ("x-stainless-retry-count", "retry-count")


def _retry_count(headers: httpx.Headers) -> int:
    """从请求头读重试序号；缺失或非数字按 0 —— 认不出就当没重试，不猜。"""
    for name in _RETRY_HEADER_NAMES:
        raw = headers.get(name)
        if raw is None:
            continue
        try:
            return int(raw)
        except ValueError:
            return 0
    return 0


async def _on_sdk_request(request: httpx.Request) -> None:
    """httpx 异步请求钩子：读重试序号头，把 SDK 内部重试记进独立桶。

    为什么在钩子里读头：重试发生在 SDK 内部，只包 transport 只能看到「又发了一次
    请求」，分不清是内部重试还是调用方的新一次发送；SDK 自己写的序号头是二者之间
    唯一的信号。

    钩子跑在真实请求路径上，因此**任何异常都必须吞掉**：抛出去会让一次本来能成功
    的调用失败。吞掉后仍尽力完成计数——先记数再把序号标为已见，这样计数本身失败
    时下一次重试还有机会补上。

    只读重试头，不读也不记 URL、header 全文或 body：钩子经过每一个请求，把请求
    细节写进日志等于把端点与内容一起带出去。
    """
    try:
        tracker = _RETRY_TRACKER.get()
        if tracker is None:
            return
        retries = _retry_count(request.headers)
        if retries <= 0 or retries in tracker.seen:
            return
        ai_egress.record_sdk_retry(tracker.port, tracker.role, tracker.is_local)
        tracker.seen.add(retries)
    except Exception:  # 钩子异常绝不连坐请求
        logger.debug("SDK 重试计数失败（忽略）", exc_info=True)


@contextmanager
def _sending(port: str, base: str) -> Iterator[None]:
    """一次显式 create 的观测窗口：先记发送尝试，再装本地的重试观测状态。

    必须在「已拿到并发许可、即将 create」处进入。排队失败（超时/取消）、替身
    短路、参数校验失败都不经过这里，否则「没发出去」会被记成出境尝试。

    `record_send_attempt` 的返回值（是否本地）直接交给重试观测状态复用，避免
    发送与重试各判一次端点而给出互相矛盾的结论。

    角色由端口层写入的元数据决定（目前只有主通道，即 `main`）；直接调用引擎时
    拿到安全默认值，不会因为没有元数据就丢计数。端口取调用点自己的，不由元数据
    决定：端口是代码路径的属性，一次误设的元数据不该把 rag 的发送记进 chat 桶。
    """
    role = ai_egress.current_metadata().role
    is_local = ai_egress.record_send_attempt(port, role, base)
    token = _RETRY_TRACKER.set(_SdkRetryTracker(port, role, is_local=is_local))
    try:
        yield
    finally:
        _RETRY_TRACKER.reset(token)


def _build_http_client() -> httpx.AsyncClient:
    """给 SDK 客户端装请求钩子，其余参数沿用 SDK 自己的默认值。

    用 SDK 的 `DefaultAsyncHttpxClient` 而不是裸 `httpx.AsyncClient`：后者会丢掉
    连接池上限与 `follow_redirects` 默认值，让一次「只加观测」的改动改变真实请求
    行为（重定向不再跟随、并发连接数变化）。
    """
    return DefaultAsyncHttpxClient(event_hooks={"request": [_on_sdk_request]})


def get_ai_client() -> AsyncOpenAI:
    """获取共享的 AsyncOpenAI 实例（延迟初始化）。"""
    global _client
    if _client is None:
        _client = AsyncOpenAI(
            api_key=config.ai_api_key.get_secret_value(),
            base_url=config.ai_api_base,
            timeout=_REQUEST_TIMEOUT,
            max_retries=_REQUEST_MAX_RETRIES,
            http_client=_build_http_client(),
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
            http_client=_build_http_client(),
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

    auto / omit：不发送；off：发送 "none" 尽力关闭思考——但**命中能力记忆**（该端点
    曾明确拒收过该参数）时改为不发送，省略即交给服务端默认；其余取值固定发送该强度。
    """
    mode = config.ai_reasoning_effort
    if mode in ("auto", "omit"):
        return {}
    if mode == "off":
        return {} if key in _effort_rejected else {"reasoning_effort": "none"}
    return {"reasoning_effort": mode}


# 端点能力记忆：键 = (归一化端点, 生效模型)，命中即不再发送该参数。
# 进程内不落盘：这是「一次请求就能确认」的端点事实，落盘会因换 provider、走代理或改
# API_BASE 而过期；重启后多付一次探测即可。
_effort_rejected: set[tuple[str, str]] = set()


def _names_reasoning_effort(exc: APIStatusError) -> bool:
    """报错是否点名 reasoning_effort。

    结构化优先（error.param == "reasoning_effort"），退化为整串匹配——真实 SDK 会把
    整个 error body 拼进 str(exc)，形如
    Error code: 400 - {'error': {..., 'param': 'reasoning_effort'}}。
    **刻意不放宽到裸 "reasoning"**：那会把「reasoning_tokens 超限」一类无关 400 误判成
    参数不兼容，进而让好端点在后续调用里静默不关思考。代价是「既不点名、body 里也不给
    param」的端点不会被自动降级——那种情况请显式设 AI_REASONING_EFFORT=omit。
    """
    if exc.status_code not in (400, 422):
        return False
    try:
        param = str((exc.response.json().get("error") or {}).get("param") or "").lower()
    except Exception:  # noqa: BLE001 — 响应不是 JSON：直接退回字符串匹配，无需记录
        return "reasoning_effort" in str(exc).lower()
    if param == "reasoning_effort":
        return True
    return "reasoning_effort" in str(exc).lower()


async def _create_with_effort_fallback(key: tuple[str, str], call: Any) -> ChatCompletion:
    """带一次能力降级的调用（**必须在已持有并发许可处调用**，内部不再取许可）。

    只对「端点明确拒收 reasoning_effort」降级：改为不发送该参数并记住，避免后续
    每次调用都撞一次 400（记忆的键设计与不落盘理由见 _effort_rejected 上方）。
    其它错误原样抛出——把无关 400 也吞成重试会掩盖真配置错误。
    并发首撞时各请求都会各重试一次（可接受的启动成本），但记忆与告警只落一次：
    check 与 add 之间没有 await，事件循环内原子。

    两次 `call` 都是**新的显式 create**（各经一次发送边界计数），但 SDK 内部重试
    仍只属于各自那一次 create 的重试桶：降级重发不是「同一个请求被重试」。
    """
    effort = _reasoning_kwargs(key)
    try:
        return await call(effort)
    except (BadRequestError, UnprocessableEntityError) as e:
        if (
            # 只有 off 是「尽力关思考」，可以降级；显式强度是用户给出的契约——端点拒收
            # 就让它响亮报错，既不静默省略，也不把该端点记进 off 的能力记忆
            config.ai_reasoning_effort != "off"
            or not effort
            or not _names_reasoning_effort(e)
        ):
            raise
        if key not in _effort_rejected:
            _effort_rejected.add(key)
            logger.warning(
                "端点 %s 不接受 reasoning_effort=%r（模型 %s）：本进程起省略该参数，"
                "改由服务端默认；要固定取值请设 AI_REASONING_EFFORT。原始错误：%s",
                key[0], effort["reasoning_effort"], key[1], e,
            )
        return await call({})


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

    json_object = _use_json_object()
    extra: dict = {"response_format": {"type": "json_object"}} if json_object else {}

    async def _send(effort: dict[str, Any]) -> ChatCompletion:
        # effort 降级的第二次发送也走这里：它是新的一次显式 create，应当各自计数，
        # 而 SDK 内部重试仍归到这次 create 的重试桶里。
        with _sending(ai_egress.PORT_CHAT, config.ai_api_base):
            return await client.chat.completions.create(
                model=config.ai_model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                # SDK 的 create() 把 reasoning_effort 声明成字面量联合，动态拼参数会让
                # mypy 判定「没有匹配的重载」；这里显式放宽为 Any，语义不变
                **cast(Any, effort),
                **extra,
                **per_request,
            )

    async def _create() -> ChatCompletion:
        return await _create_with_effort_fallback(key, _send)

    sem = get_ai_semaphore()
    if sem is None:
        return await _create()
    if timeout is None:
        async with sem:
            return await _create()
    # 判官类调用：许可等待也计入 timeout，锁内最坏持有 = timeout（请求）+ timeout（排队）。
    # 此前 async with sem 的排队等待不在任何超时之内。
    role = ai_egress.current_metadata().role
    try:
        await asyncio.wait_for(sem.acquire(), timeout)
    except TimeoutError:
        # 排队阶段超时：请求根本没发出去，只进排队桶，不动发送桶。
        ai_egress.record_queue_timeout(ai_egress.PORT_CHAT, role)
        raise
    except asyncio.CancelledError:
        # 取消同样只证明「没轮到我」，不能算一次出境尝试。
        ai_egress.record_queue_cancelled(ai_egress.PORT_CHAT, role)
        raise
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

    async def _send(effort: dict[str, Any]) -> ChatCompletion:
        # 计数用的是这次真正指向的端点：override 留空时与主通道同源，按主 base 判
        # 本地/远端，避免把「空 override」判成未知端点而一律按远端。
        with _sending(ai_egress.PORT_RAG, api_base or config.ai_api_base):
            return await client.chat.completions.create(
                model=chat_model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                **cast(Any, effort),
            )

    async def _create() -> ChatCompletion:
        return await _create_with_effort_fallback(key, _send)

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
            http_client=_build_http_client(),
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
        # 每批 chunk 是一次显式 create：计数按批次而不是文本条数，否则同一批
        # 20 条文本会被读成 20 次出境。
        with _sending(ai_egress.PORT_EMBED, embed_api_base()):
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
