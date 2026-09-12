"""内置消息源插件测试（weflow / weflow-legacy / qqflow）。"""

import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import SecretStr

from briefdesk.config import Settings
from briefdesk.plugin.base import PluginContext, PluginDisabledError
from briefdesk.plugins.qqflow.plugin import QqFlowPlugin
from briefdesk.plugins.weflow.plugin import WeFlowPlugin
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
            PLUGINS=[], PLUGINS_REQUIRED=[], PLUGIN_PATH=""
        ),
        publish_event=publish_event,
        subscribe_event=subscribe_event,
        register_source=registered.append,
        register_stage=lambda stage: None,
    )
    return ctx, registered


class TestWeFlowLegacyPlugin:
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
        assert registered == [fake_runtime]

    async def test_missing_token_self_disables(self):
        """【决策 ①=1B】必填校验与 weflow/qqflow 统一：缺 token 装配期自禁用。"""
        ctx, _ = _ctx()
        plugin = WeFlowLegacyPlugin()
        with patch(
            "briefdesk.plugins.weflow_legacy.config.WeFlowLegacySettings",
            return_value=SimpleNamespace(api_token=SecretStr("")),
        ), pytest.raises(PluginDisabledError) as cm:
            await plugin.setup(ctx)
        assert "WEFLOW_LEGACY_API_TOKEN" in str(cm.value)

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


class TestQqFlowPlugin:
    async def test_missing_required_config_self_disables(self):
        ctx, _ = _ctx()
        fake_settings = SimpleNamespace(api_token=SecretStr(""), qq="", key=SecretStr(""))
        plugin = QqFlowPlugin()
        with patch(
            "briefdesk.plugins.qqflow.config.QqFlowSettings", return_value=fake_settings
        ), pytest.raises(PluginDisabledError) as cm:
            await plugin.setup(ctx)
        assert "QQFLOW_API_TOKEN" in str(cm.value)

    async def test_partial_config_names_missing_fields(self):
        ctx, _ = _ctx()
        fake_settings = SimpleNamespace(
            api_token=SecretStr("t"), qq="", key=SecretStr("k" * 16)
        )
        plugin = QqFlowPlugin()
        with patch(
            "briefdesk.plugins.qqflow.config.QqFlowSettings", return_value=fake_settings
        ), pytest.raises(PluginDisabledError) as cm:
            await plugin.setup(ctx)
        assert "QQFLOW_QQ" in str(cm.value)
        assert "QQFLOW_API_TOKEN" not in str(cm.value)

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
        assert registered == [fake_runtime]

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


class TestWeFlowPlugin:
    async def test_missing_all_config_lists_everything(self):
        """api_token/wxid 与 DB_KEYS 同时缺失时聚合在一条错误中一次报全，
        不应拆成多次抛出致用户只能看到第一项。"""
        ctx, _ = _ctx()
        fake_settings = SimpleNamespace(
            api_token=SecretStr(""), wxid="", db_keys_map={}
        )
        plugin = WeFlowPlugin()
        with patch(
            "briefdesk.plugins.weflow.config.WeFlowSettings", return_value=fake_settings
        ), pytest.raises(PluginDisabledError) as cm:
            await plugin.setup(ctx)
        message = str(cm.value)
        assert "WEFLOW_API_TOKEN" in message
        assert "WEFLOW_WXID" in message
        assert "WEFLOW_DB_KEYS" in message

    async def test_config_present_registers_runtime(self):
        ctx, registered = _ctx()
        fake_settings = SimpleNamespace(
            api_token=SecretStr("t"), wxid="wx", db_keys_map={"k": "v"}
        )
        fake_runtime = SimpleNamespace(name="weflow")
        plugin = WeFlowPlugin()
        with patch(
            "briefdesk.plugins.weflow.config.WeFlowSettings", return_value=fake_settings
        ), patch(
            "briefdesk.plugins.weflow.runtime.WeFlowSource", return_value=fake_runtime
        ):
            await plugin.setup(ctx)
        assert registered == [fake_runtime]


class SseReconnectInitialMsValidationTest(unittest.TestCase):
    """复核 P3-17：sse_reconnect_initial_ms 不得为 0（零间隔热重连风暴）。"""

    def test_zero_initial_ms_rejected(self):
        from pydantic import ValidationError

        from briefdesk.plugins.qqflow.config import QqFlowSettings
        from briefdesk.plugins.weflow.config import WeFlowSettings
        from briefdesk.plugins.weflow_legacy.config import WeFlowLegacySettings

        for cls in (WeFlowSettings, WeFlowLegacySettings, QqFlowSettings):
            with self.assertRaises(ValidationError, msg=f"{cls.__name__} 应拒绝 0"):
                cls(sse_reconnect_initial_ms=0)

    def test_positive_initial_ms_accepted(self):
        from briefdesk.plugins.weflow.config import WeFlowSettings

        s = WeFlowSettings(sse_reconnect_initial_ms=1)
        assert s.sse_reconnect_initial_ms == 1


class LegacyRestFilterContractTest(unittest.TestCase):
    """legacy REST 回填路径与 SSE 实时路径过滤口径一致。

    SSE 路径的附件占位符过滤（_ATTACHMENT_RE）与图片 mediaType 校验在
    REST 路径同样生效——同一消息不因到达路径不同而入库结果不同。
    """

    def test_rest_attachment_placeholder_dropped(self):
        from briefdesk.plugins.weflow_legacy.normalize import pre_filter_rest

        assert not pre_filter_rest(
                {"serverId": "m1", "localType": 1, "content": "[文件]"}
            )
        assert not pre_filter_rest(
                {"serverId": "m1", "localType": 1, "content": "  [链接]  "}
            )

    def test_rest_voice_media_dropped(self):
        from briefdesk.plugins.weflow_legacy.normalize import pre_filter_rest

        assert not pre_filter_rest(
                {
                    "serverId": "m1",
                    "localType": 3,
                    "mediaType": "voice",
                    "mediaUrl": "/api/v1/media/abc",
                    "content": "[语音]",
                }
            )

    def test_rest_image_passes_with_media(self):
        from briefdesk.plugins.weflow_legacy.normalize import pre_filter_rest

        assert pre_filter_rest(
                {
                    "serverId": "m1",
                    "localType": 3,
                    "mediaType": "image",
                    "mediaUrl": "/api/v1/media/abc",
                    "content": "[图片]",
                }
            )

    def test_sse_same_shape_unchanged(self):
        """回归保护：SSE 同形输入行为不变。"""
        from briefdesk.plugins.weflow_legacy.normalize import pre_filter_sse

        # SSE 占位符文本仍被滤（既有口径）
        assert not pre_filter_sse(
                {
                    "event": "message.new",
                    "rawid": "r1",
                    "sessionId": "s1",
                    "content": "[文件]",
                    "timestamp": 1,
                }
            )
        # SSE 文本消息仍放行
        assert pre_filter_sse(
                {
                    "event": "message.new",
                    "rawid": "r1",
                    "sessionId": "s1",
                    "content": "hello world",
                    "timestamp": 1,
                }
            )
