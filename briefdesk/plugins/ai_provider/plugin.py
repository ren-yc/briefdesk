"""AI 供应商插件 — 把 OpenAI 兼容 chat + 嵌入能力注册到核心端口。

setup 构造 Provider 并注册：ctx.ai（服务端口）与 briefdesk.ai_ports（引擎
端口函数转发目标）；teardown 清除注册（幂等）。引擎模块（classify/dedup/
merge）经 ai_ports 端口函数调用，不直接依赖本插件。
"""

from briefdesk.plugin.base import AIProvider, Plugin, PluginContext


class AiProviderPlugin(Plugin, AIProvider):
    """AI 供应商插件（显式实现 Plugin + AIProvider；入口见模块底部 `plugin` 实例）。"""

    name = "ai_provider"
    version = "1.0.0"
    dependencies: tuple[str, ...] = ()

    def __init__(self) -> None:
        self._provider: AIProvider | None = None

    async def setup(self, ctx: PluginContext) -> None:
        # 延迟导入：仅加载本插件依赖，且便于测试替换
        from briefdesk import ai_ports
        from briefdesk.plugins.ai_provider import engine as ai_engine

        provider = ai_engine.Provider()
        ai_ports.set_ai(provider)
        ctx.ai = provider
        self._provider = provider

    async def activate(self, ctx: PluginContext) -> None: ...

    async def teardown(self) -> None:
        from briefdesk import ai_ports

        ai_ports.set_ai(None)
        self._provider = None

    # AIProvider 端口（委托给内部 Provider 实例）
    async def chat(self, messages, *, temperature, max_tokens):
        assert self._provider is not None
        return await self._provider.chat(messages, temperature=temperature, max_tokens=max_tokens)

    async def rag_chat(
        self, messages, *, temperature, max_tokens, model="", api_base="", api_key=""
    ):
        assert self._provider is not None
        return await self._provider.rag_chat(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            model=model,
            api_base=api_base,
            api_key=api_key,
        )

    async def embed_texts(self, texts):
        assert self._provider is not None
        return await self._provider.embed_texts(texts)

    def is_embedding_enabled(self) -> bool:
        return self._provider is not None and self._provider.is_embedding_enabled()

    def embed_model_name(self) -> str:
        assert self._provider is not None
        return self._provider.embed_model_name()


plugin = AiProviderPlugin()
