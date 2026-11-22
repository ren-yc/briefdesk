"""AI 供应商端口 — ai_provider 插件在 setup 阶段注册实例，
引擎（classify/dedup/merge）经本模块端口函数调用，核心不依赖具体供应商实现。

- chat / rag_chat / embed_texts / is_embedding_enabled / embed_model_name：
  转发到已注册的 AIProvider 实例（未注册时 chat/embed 抛 RuntimeError 明示
  配置错误，启用性检查安全返回 False）；`rag_chat` 额外接受 model/api_base/
  api_key override，由调用方从自己的配置域下传（见该函数 docstring）；
- `resolve_rag_dispatch`：**可提前查询**的实际派发计划（端点/模型），与
  `rag_chat` 的转发判断共用同一处逻辑。调用方要在构建 prompt 之前知道
  「这次到底会发到哪个端点」（是否本地）才能决定要不要改写展示名；
- 三个转发端口在转发期间用 `ai_egress.use_metadata` 标注出境元数据（端口、
  角色、任务标签、消息源标签），本身**不计数**：端口调用不等于出境，计数在
  供应商引擎的发送边界（排队失败/替身短路都不该算作发送尝试）；
- loads_json / top_k_similar：供应商无关的纯工具（JSON 修复解析、
  余弦 Top-K 候选选择）。

模块级单例（与 stages/realtime 同风格）；测试用 set_ai(None) 复位。
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any

import numpy as np
from json_repair import loads as json_repair_loads

from briefdesk import ai_egress
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
    timeout: float | None = None,
    max_retries: int | None = None,
    telemetry_kind: str = "",
    telemetry_source: str = "",
) -> ChatResponse:
    """统一 AI 调用端口（模型/供应商由已注册插件决定）。

    timeout：单请求超时覆盖（秒）。判官类调用（dedup 判票/strong、merge
    判官/标题）在存储锁内执行，传短超时限制锁的最坏持有时间，防上游挂起
    冻结整条管道；None 用客户端默认。

    max_retries：SDK 重试次数覆盖；判官类调用传 0——SDK 默认重试 2 次会把
    单次判官最坏耗时放大到 3 倍锁内占用。None 时不传该参数（保持既有端口
    契约，只接受旧参数的测试桩不受影响）。

    telemetry_kind：遥测标注（如 "judge" / "classify" / "sysc" /
    "summarize" / "title"），端口层计时并计数，**不透传给供应商**——
    旧测试桩零感知。空串仍计时（归入空维度），只是没有任务维度的标注。
    取消（CancelledError）单独计数且不进延迟分布（半程耗时混入会污染
    分位数）；其余异常（含 TimeoutError——判官超时正是要观测的最坏
    尾部）计入 .error 并保留延迟样本。

    telemetry_source：消息源维度（如 "weflow"），与 telemetry_kind 一样
    只进遥测、**绝不进传给供应商的 kwargs**——调用方不传时归入空维度，
    读数按「指标 → 源」两层聚合，混合源部署才分得清是哪一路的耗时。
    """
    from briefdesk import telemetry

    start = time.perf_counter()
    kwargs: dict[str, Any] = {
        "temperature": temperature,
        "max_tokens": max_tokens,
        "timeout": timeout,
    }
    if max_retries is not None:
        kwargs["max_retries"] = max_retries
    # 端口层只设元数据、不计数：真正的出境计数在供应商引擎「即将 create」处。
    # 在这里计数会把排队失败、替身短路、参数校验失败一并算成发送尝试。用 with
    # 而不是手动 set/reset：这两步之间隔着 await，入口处抛异常也必须恢复，
    # 否则同一个任务后续的调用会沿用错误的端口/源标签。
    with ai_egress.use_metadata(
        port=ai_egress.PORT_CHAT,
        role=ai_egress.ROLE_MAIN,
        kind=telemetry_kind,
        source=telemetry_source,
    ):
        try:
            resp = await _require_ai().chat(messages, **kwargs)
        except asyncio.CancelledError:
            telemetry.record(f"{telemetry_kind or 'chat'}.cancelled", telemetry_source)
            raise
        except Exception:
            elapsed = (time.perf_counter() - start) * 1000.0
            telemetry.record(f"{telemetry_kind or 'chat'}.error", telemetry_source)
            telemetry.observe(
                f"{telemetry_kind or 'chat'}.latency", elapsed, telemetry_source
            )
            raise
        telemetry.record(f"{telemetry_kind or 'chat'}.call", telemetry_source)
        telemetry.observe(
            f"{telemetry_kind or 'chat'}.latency",
            (time.perf_counter() - start) * 1000.0,
            telemetry_source,
        )
    return resp


@dataclass(frozen=True)
class RagDispatch:
    """一次 RAG 问答的**实际派发**计划：真正会被使用的端点与模型。

    为什么调用方需要它：要不要把展示名（姓名、群名）换成化名，取决于这次请求
    落到本地还是远端端点，而这个决定发生在构建 prompt **之前**。只看自己手里的
    override 会判错——供应商没有专用通道时 `rag_chat` 回退走 `chat`，override 被
    静默丢弃、请求打到主端点。若调用方按 override 判成本地、请求却落在远端主机，
    真实姓名就被原样送出（静默泄露，不是报错）。

    Key 只以布尔出现：`has_api_key` 足够判断凭据层是否生效，而计划会进日志、
    快照与测试断言，明文一旦进来就跟着流出去。
    """

    dedicated: bool
    """供应商是否真的实现了专用 rag_chat（类级声明；旧实现与测试桩为 False）。"""
    model: str
    """实际生效模型：专用通道用 override（留空回退主模型）；回退 chat 时恒为主模型。"""
    api_base: str
    """实际生效端点：专用通道用 override（留空回退主端点）；回退 chat 时恒为主端点。"""
    has_api_key: bool
    """这次派发是否带着 override Key；回退 chat 时恒为 False（chat 不接受 Key 覆盖）。"""


def resolve_rag_dispatch(
    *, model: str = "", api_base: str = "", api_key: str = ""
) -> RagDispatch:
    """算出这次 RAG 问答**实际**会派发到哪个端点与模型（只查询，不发送）。

    判定写在这一处、由 `rag_chat` 调用：两份 hasattr 判断迟早会分叉，而分叉的
    后果是「计划说本地、实际发远端」这类静默泄露，不是报错（回归见
    tests/test_ai_dispatch.py）。

    语义（与供应商实现里 override 的解析规则逐项对齐，三项不当作一个原子包）：
    - `dedicated=True`：专用通道生效，model/api_base 各自按 override 回退主配置；
    - `dedicated=False`：旧供应商没有专用通道，回退复用 `chat`，override 全部
      丢弃，模型与端点都是主配置的值——`api_base` 必须报**主端点**，报 override
      会把调用方引向「本次是本地请求」的错误结论。

    失败模式：绕开本函数、直接拿 override 判本地的调用方，会在回退路径上把真名
    发给远端主端点。正确用法是「先取计划 → 按计划的 api_base 判本地 → 再构建
    prompt」，让判定与发送描述同一件事。

    未注册供应商时与 `rag_chat` 一样抛 RuntimeError：计划描述的是「这次调用会
    发生什么」，没有供应商就没有派发可言，静默按主端点出计划只会掩盖配置错误。
    """
    ai = _require_ai()
    from briefdesk.config import config

    # 类级检测：Mock/旧实现等实例级动态属性（__getattr__）不视为实现了 rag_chat，
    # 只有真正声明该方法的供应商类才走专用通道，否则回退复用 chat。
    if not hasattr(type(ai), "rag_chat"):
        return RagDispatch(
            dedicated=False,
            model=config.ai_model,
            api_base=config.ai_api_base,
            has_api_key=False,
        )
    return RagDispatch(
        dedicated=True,
        model=model or config.ai_model,
        api_base=api_base or config.ai_api_base,
        has_api_key=bool(api_key),
    )


async def rag_chat(
    messages: list[dict],
    *,
    temperature: float = 0.2,
    max_tokens: int = 1024,
    model: str = "",
    api_base: str = "",
    api_key: str = "",
) -> ChatResponse:
    """RAG 问答专用端口：模型/端点/Key 由调用方以 override 参数传入。

    override 三元组由调用方（rag 插件）从**自己的**配置域取值下传——`RAG_`
    前缀归 rag 插件所有，核心与 ai_provider 都不读它，故不存在核心配置对
    插件命名空间的侵占，ai_provider 也无需反向依赖 rag 插件。三项留空即
    复用主 AI 通道（与 chat 同源）。

    供应商未实现 rag_chat（旧实现/测试桩）时回退复用 chat 端口。注意此路径
    **静默丢弃 override**：旧供应商没有专用通道的概念，只能按主通道应答。
    回退单独计数（`rag.chat_fallback`，无消息源维度）：它意味着「专用通道
    没生效、问答实际跑在主模型上」，与回答失败是两种故障，合成一个桶就分不开
    ——用户配了 RAG_MODEL 却没被用上，只能从这里看出来。

    走哪条路由由 `resolve_rag_dispatch` 决定（同一份判断，调用方可以提前查询
    实际端点）；override 仍按原值转发，留空时的回退在供应商实现里完成——两处
    的回退规则一致，端口层不替供应商改写参数。
    """
    ai = _require_ai()
    dispatch = resolve_rag_dispatch(model=model, api_base=api_base, api_key=api_key)
    # 端口层只标注「这次是 rag 端口」，计数留给引擎的发送边界（见 engine 的
    # _sending）：rag 的 prompt 准备失败时根本没发送，端口调用次数不等于出境次数。
    with ai_egress.use_metadata(port=ai_egress.PORT_RAG, role=ai_egress.ROLE_MAIN):
        if dispatch.dedicated:
            # 运行时防御性检测仍有意义：测试经 set_ai 注入的桩可能未声明 rag_chat
            # （mypy 对函数体内构造的对象不做协议校验）；静态层面 AIProvider 协议
            # 已含 rag_chat 成员（plugin/base.py），无需 ignore。
            return await ai.rag_chat(
                messages,
                temperature=temperature,
                max_tokens=max_tokens,
                model=model,
                api_base=api_base,
                api_key=api_key,
            )
        from briefdesk import telemetry

        telemetry.record("rag.chat_fallback")
        return await ai.chat(messages, temperature=temperature, max_tokens=max_tokens)


async def embed_texts(texts: list[str]) -> list[list[float]]:
    """批量嵌入端口。"""
    # 嵌入同样只设元数据：一次端口调用会分成多批创建，出境次数以批为单位在引擎侧计。
    with ai_egress.use_metadata(port=ai_egress.PORT_EMBED, role=ai_egress.ROLE_MAIN):
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
    """标准解析优先，失败后用 json_repair 修复兜底。

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


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """标量余弦相似度（单对向量，非矩阵批量）：语义对齐裁判等单对比较的
    共享实现——各引擎若各留一份本地拷贝，改一漏一必然造成判定口径漂移。

    numpy 可用走向量化快路径（float64，与纯 Python 同精度）；导入失败时
    回退纯 Python（数值一致，测试随机向量对拍保证）。任一侧范数为 0 →
    返回 0.0（零向量与任何向量都不相似，与 top_k_similar 的 +1e-12 口径
    语义一致）。
    """
    try:
        import numpy as np
    except ImportError:  # pragma: no cover — numpy 为核心依赖，回退仅为防御
        np = None  # type: ignore[assignment]

    if np is not None:
        va = np.asarray(a, dtype=np.float64)
        vb = np.asarray(b, dtype=np.float64)
        na = float(np.linalg.norm(va))
        nb = float(np.linalg.norm(vb))
        if not na or not nb:
            return 0.0
        return float(va @ vb / (na * nb))

    dot = na = nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if not na or not nb:
        return 0.0
    return dot / ((na**0.5) * (nb**0.5))


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
