"""插件抽象层 — Plugin 最小契约、PluginContext 与各能力协议。

依赖方向（由 tests/test_no_core_imports_plugins.py 强制）：实现层
briefdesk/plugins/* 可 import 核心与 briefdesk/plugin/*；核心与
briefdesk/plugin/* 永不静态 import briefdesk.plugins.*。
发现/加载约定、装配生命周期与各能力协议的完整约定见
docs/architecture.md「插件框架」。
**实现方式约定：内置插件类显式继承对应能力协议**（mypy 据此强制
实现完整性），第三方插件亦可鸭子实现（manager 只做结构校验）。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from fastapi import APIRouter

from briefdesk.config import Settings
from briefdesk.events import EventHandler
from briefdesk.sources_base import SourceRuntime
from briefdesk.types import BatchContext, DedupResult

PLUGIN_GROUP = "briefdesk.plugins"


class PluginError(Exception):
    """插件加载/装配的致命错误（required 插件失败、依赖解析失败等）。"""


class PluginDisabledError(Exception):
    """插件自检失败主动请求禁用（非致命）：如缺少必填配置。

    setup() 抛本异常 → manager 记 WARNING、跳过该插件，其余插件照常装配。
    """


class Plugin(Protocol):
    """插件最小契约（协议类：内置插件显式继承以声明契约，mypy 强制实现完整性）。"""

    name: str
    version: str
    dependencies: tuple[str, ...]

    async def setup(self, ctx: PluginContext) -> None: ...
    async def activate(self, ctx: PluginContext) -> None: ...
    async def teardown(self) -> None: ...


class SettingsSchemaPlugin(Protocol):
    """可选的插件设置能力。

    为保持第三方旧插件兼容，该方法不是 Plugin 最小生命周期契约的一部分；
    实现后设置页会自动显示该插件当前配置。返回值必须是 JSON 安全字段描述。
    """

    def settings_schema(self) -> list[dict[str, Any]]: ...


class SourcePlugin(Plugin, Protocol):
    """消息源插件能力协议；实现类显式继承本协议。

    setup 阶段构造 SourceRuntime 并经 ctx.register_source 注册；
    teardown 负责关闭该 runtime（幂等）。activate 通常无副作用：
    监听启动由应用层在服务器就绪后统一编排。
    """


class StagePlugin(Plugin, Protocol):
    """管道阶段插件能力协议：单槽位、可多实例（同槽按 priority 升序）；
    实现类显式继承本协议。

    run(batch, ctx) 由 pipeline 骨架在对应槽位调用；存储相阶段（dedup 与
    post_insert 槽）可额外实现 before_run（锁外：预嵌入等网络调用）与
    after_run（锁外：向量落库等收尾），骨架对两槽统一 getattr 探测可选钩子。
    """

    slot: str  # "enrich" | "classify" | "dedup" | "post_insert"
    priority: int

    async def run(self, batch: BatchContext, ctx: PluginContext) -> None: ...


class DedupService(Protocol):
    """去重服务端口；引擎类显式实现本协议。

    dedup 插件在 setup 阶段把 DedupEngine 注册到 ctx.dedup；pipeline 骨架
    不直接使用，merge 阶段与事件清理经此端口同步去重缓存，核心不依赖
    具体实现。
    """

    async def ensure_cache(self) -> None: ...
    async def preembed_batch(
        self, items: list[tuple[str, str]]
    ) -> list[list[float]] | None: ...
    async def check_dedup(
        self,
        title: str,
        source_group: str,
        q_emb: list[float] | None = None,
        image_urls: list[str] | None = None,
        source: str = "",
        source_quote: str = "",
    ) -> DedupResult: ...
    def add_to_cache(
        self,
        item_id: str,
        title: str,
        embedding: list[float] | None = None,
        image_urls: list[str] | None = None,
        source: str = "",
        source_quote: str = "",
    ) -> None: ...
    def remove_items(self, item_ids: list[str]) -> None: ...
    async def flush_pending_embeddings(self) -> None: ...


class ChatMessage(Protocol):
    """AI chat 响应的单条消息（供应商无关的最小形状）。"""

    content: str | None


class ChatChoice(Protocol):
    """AI chat 响应的单个候选（供应商无关的最小形状）。"""

    message: ChatMessage
    finish_reason: str | None


class ChatResponse(Protocol):
    """AI chat 响应的最小形状（openai 响应与测试 fake 均满足）。"""

    choices: list[ChatChoice]


class AIProvider(Protocol):
    """AI 供应商能力端口；ai_provider 插件 setup 注册到 ctx.ai。

    chat 返回供应商响应对象（须满足 ChatResponse 形状）；嵌入能力由
    is_embedding_enabled 门控（EMBED_API_BASE 留空禁用）。
    """

    async def chat(
        self,
        messages: list[dict],
        *,
        temperature: float,
        max_tokens: int,
        timeout: float | None = None,
    ) -> ChatResponse: ...
    async def rag_chat(
        self,
        messages: list[dict],
        *,
        temperature: float,
        max_tokens: int,
        model: str = "",
        api_base: str = "",
        api_key: str = "",
    ) -> ChatResponse: ...
    async def embed_texts(self, texts: list[str]) -> list[list[float]]: ...
    def is_embedding_enabled(self) -> bool: ...
    def embed_model_name(self) -> str: ...


def _noop_register(*args: Any, **kwargs: Any) -> None:
    """默认端口：未注入时静默丢弃（测试/单插件场景）。"""


class WebPlugin(Plugin, Protocol):
    """Web 扩展插件能力协议；实现类显式继承本协议。

    router()：返回挂载到应用根路径的 APIRouter（路径前缀由插件自定，
    如 /api/calendar）；setup 阶段经 ctx.register_router 注册。
    asset_dir()：可选静态资源目录，经 ctx.register_plugin_assets 注册
    （核心动态路由 /plugin-assets/<name>/ 服务，浏览器直连）。
    """

    def router(self) -> APIRouter: ...
    def asset_dir(self) -> Path | None: ...


@dataclass
class PluginContext:
    """核心注入给插件的服务端口（插件不得 import 核心内部实现）。

    端口一览：register_source（源插件注册 SourceRuntime）、register_stage
    （阶段插件注册）、dedup（去重服务端口）、ai（AI 供应商端口）、
    register_router / register_plugin_assets（Web 插件注册路由与静态资源；
    可选端口默认 noop，未注入时静默丢弃）。
    """

    config: Settings
    publish_event: Callable[[str, Any], Awaitable[None]]
    subscribe_event: Callable[[str, EventHandler], None]
    register_source: Callable[[SourceRuntime], None]
    register_stage: Callable[[StagePlugin], None]
    dedup: DedupService | None = None
    ai: AIProvider | None = None  # AI 供应商（ai_provider 插件 setup 赋值）
    register_router: Callable[[APIRouter], None] = _noop_register
    register_plugin_assets: Callable[[str, str], None] = _noop_register
