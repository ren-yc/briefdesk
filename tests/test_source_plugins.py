"""内置消息源插件测试（weflow-legacy / qqflow）。"""

import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

from pydantic import SecretStr

from briefdesk.config import Settings
from briefdesk.plugin.base import PluginContext, PluginDisabledError
from briefdesk.plugins.qqflow.plugin import QqFlowPlugin
from briefdesk.plugins.weflow_legacy.plugin import WeFlowLegacyPlugin


def _ctx() -> tuple[PluginContext, list]:
    registered: list = []

    async def publish_event(event: str, payload: Any) -> None:
        return None

    def subscribe_event(event: str, handler: Any) -> None:
        return None

    ctx = PluginContext(
        # 用环境变量名（alias）构造：pydantic mypy 插件对带 alias 字段按别名生成签名
        config=Settings(
            PLUGINS=["*"], PLUGINS_DISABLED=[], PLUGINS_REQUIRED=[], PLUGIN_PATH=""
        ),
        publish_event=publish_event,
        subscribe_event=subscribe_event,
        register_source=registered.append,
        register_stage=lambda stage: None,
    )
    return ctx, registered


class WeFlowLegacyPluginTest(unittest.IsolatedAsyncioTestCase):
    async def test_setup_registers_runtime(self):
        ctx, registered = _ctx()
        fake_runtime = SimpleNamespace(name="weflow-legacy")
        plugin = WeFlowLegacyPlugin()
        with patch(
            "briefdesk.plugins.weflow_legacy.config.WeFlowLegacySettings",
            return_value=SimpleNamespace(api_token=SecretStr("t")),
        ), patch(
            "briefdesk.plugins.weflow_legacy.runtime.WeFlowLegacySource", return_value=fake_runtime
        ):
            await plugin.setup(ctx)
        self.assertEqual(registered, [fake_runtime])

    async def test_missing_token_self_disables(self):
        """【决策 ①=1B】必填校验与 weflow/qqflow 统一：缺 token 装配期自禁用。"""
        ctx, _ = _ctx()
        plugin = WeFlowLegacyPlugin()
        with patch(
            "briefdesk.plugins.weflow_legacy.config.WeFlowLegacySettings",
            return_value=SimpleNamespace(api_token=SecretStr("")),
        ), self.assertRaises(PluginDisabledError) as cm:
            await plugin.setup(ctx)
        self.assertIn("WEFLOW_LEGACY_API_TOKEN", str(cm.exception))

    async def test_teardown_closes_runtime(self):
        ctx, _ = _ctx()
        close_spy = AsyncMock()
        fake_runtime = SimpleNamespace(name="weflow-legacy", close=close_spy)
        plugin = WeFlowLegacyPlugin()
        with patch(
            "briefdesk.plugins.weflow_legacy.config.WeFlowLegacySettings",
            return_value=SimpleNamespace(api_token=SecretStr("t")),
        ), patch(
            "briefdesk.plugins.weflow_legacy.runtime.WeFlowLegacySource", return_value=fake_runtime
        ):
            await plugin.setup(ctx)
        await plugin.teardown()
        close_spy.assert_awaited_once()

    async def test_teardown_without_setup_noop(self):
        await WeFlowLegacyPlugin().teardown()


class QqFlowPluginTest(unittest.IsolatedAsyncioTestCase):
    async def test_missing_required_config_self_disables(self):
        ctx, _ = _ctx()
        fake_settings = SimpleNamespace(api_token=SecretStr(""), qq="", key=SecretStr(""))
        plugin = QqFlowPlugin()
        with patch(
            "briefdesk.plugins.qqflow.config.QqFlowSettings", return_value=fake_settings
        ), self.assertRaises(PluginDisabledError) as cm:
            await plugin.setup(ctx)
        self.assertIn("QQFLOW_API_TOKEN", str(cm.exception))

    async def test_partial_config_names_missing_fields(self):
        ctx, _ = _ctx()
        fake_settings = SimpleNamespace(
            api_token=SecretStr("t"), qq="", key=SecretStr("k" * 16)
        )
        plugin = QqFlowPlugin()
        with patch(
            "briefdesk.plugins.qqflow.config.QqFlowSettings", return_value=fake_settings
        ), self.assertRaises(PluginDisabledError) as cm:
            await plugin.setup(ctx)
        self.assertIn("QQFLOW_QQ", str(cm.exception))
        self.assertNotIn("QQFLOW_API_TOKEN", str(cm.exception))

    async def test_config_present_registers_runtime(self):
        ctx, registered = _ctx()
        fake_settings = SimpleNamespace(
            api_token=SecretStr("t"), qq="123", key=SecretStr("k" * 16)
        )
        fake_runtime = SimpleNamespace(name="qqflow")
        plugin = QqFlowPlugin()
        with patch(
            "briefdesk.plugins.qqflow.config.QqFlowSettings", return_value=fake_settings
        ), patch(
            "briefdesk.plugins.qqflow.runtime.QqFlowSource", return_value=fake_runtime
        ):
            await plugin.setup(ctx)
        self.assertEqual(registered, [fake_runtime])

    async def test_teardown_closes_runtime(self):
        ctx, _ = _ctx()
        close_spy = AsyncMock()
        fake_settings = SimpleNamespace(
            api_token=SecretStr("t"), qq="123", key=SecretStr("k" * 16)
        )
        fake_runtime = SimpleNamespace(name="qqflow", close=close_spy)
        plugin = QqFlowPlugin()
        with patch(
            "briefdesk.plugins.qqflow.config.QqFlowSettings", return_value=fake_settings
        ), patch(
            "briefdesk.plugins.qqflow.runtime.QqFlowSource", return_value=fake_runtime
        ):
            await plugin.setup(ctx)
        await plugin.teardown()
        close_spy.assert_awaited_once()
