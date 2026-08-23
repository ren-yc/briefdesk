"""提醒 Web 插件（P5 起）— 卡片提醒设置与到期提醒路由从核心 server 迁出。"""

from briefdesk.plugins.reminders.plugin import RemindersPlugin, plugin

__all__ = ["RemindersPlugin", "plugin"]
