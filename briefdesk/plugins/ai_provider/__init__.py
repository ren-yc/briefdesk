"""AI 供应商插件（P4 起）：OpenAI 兼容 chat + 嵌入能力，注册到核心端口。"""

from briefdesk.plugins.ai_provider.plugin import AiProviderPlugin, plugin

__all__ = ["AiProviderPlugin", "plugin"]
