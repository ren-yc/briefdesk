"""插件框架 — 协议（base）与发现/装配（manager）。

本包是核心侧的框架；插件实现层在 briefdesk/plugins/，
由 manager 经 entry points / PLUGIN_PATH 动态发现。
"""

from briefdesk.plugin.base import (
    PLUGIN_GROUP,
    Plugin,
    PluginContext,
    PluginDisabledError,
    PluginError,
)
from briefdesk.plugin.manager import PluginManager, PluginRecord

__all__ = [
    "PLUGIN_GROUP",
    "Plugin",
    "PluginContext",
    "PluginDisabledError",
    "PluginError",
    "PluginManager",
    "PluginRecord",
]
