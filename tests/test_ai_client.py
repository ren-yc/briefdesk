"""AI 客户端统一调用的思考模式开关测试（不触发真实网络请求）。"""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from briefdesk.config import config
from briefdesk.plugins.ai_provider.engine import chat


def _fake_client():
    """返回 (fake_client, create_mock)，create_mock 记录 chat.completions.create 调用。"""
    create = AsyncMock(
        return_value=SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="ok"),
                    finish_reason="stop",
                )
            ]
        )
    )
    client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=create)
        )
    )
    return client, create


class ChatThinkingSwitchTest(unittest.IsolatedAsyncioTestCase):
    async def test_default_does_not_pass_reasoning_effort(self):
        client, create = _fake_client()
        with patch("briefdesk.plugins.ai_provider.engine.get_ai_client", return_value=client), patch.object(
            config, "ai_disable_thinking", False
        ), patch.object(config, "ai_api_key", "deepseek"), patch.object(
            config, "ai_model", "qwen3.5"
        ):
            await chat([], temperature=0.1, max_tokens=64)

        _, kwargs = create.call_args
        self.assertNotIn("reasoning_effort", kwargs)
        self.assertNotIn("response_format", kwargs)
        self.assertEqual(kwargs["temperature"], 0.1)
        self.assertEqual(kwargs["max_tokens"], 64)

    async def test_disabled_passes_reasoning_effort_none(self):
        client, create = _fake_client()
        with patch("briefdesk.plugins.ai_provider.engine.get_ai_client", return_value=client), patch.object(
            config, "ai_disable_thinking", True
        ), patch.object(config, "ai_api_key", "deepseek"), patch.object(
            config, "ai_model", "qwen3.5"
        ):
            await chat([], temperature=0.1, max_tokens=64)

        _, kwargs = create.call_args
        self.assertEqual(kwargs["reasoning_effort"], "none")
        self.assertNotIn("response_format", kwargs)
        self.assertEqual(kwargs["temperature"], 0.1)
        self.assertEqual(kwargs["max_tokens"], 64)


class ChatJsonObjectTest(unittest.IsolatedAsyncioTestCase):
    """严格 JSON 输出开关：ollama api key / deepseek-v4 模型传 response_format。"""

    async def _call(self, *, api_key: str, model: str, disable_thinking: bool):
        client, create = _fake_client()
        with patch("briefdesk.plugins.ai_provider.engine.get_ai_client", return_value=client), patch.object(
            config, "ai_disable_thinking", disable_thinking
        ), patch.object(config, "ai_api_key", api_key), patch.object(
            config, "ai_model", model
        ):
            await chat([], temperature=0.1, max_tokens=64)
        return create.call_args.kwargs

    async def test_ollama_api_key_passes_response_format(self):
        kwargs = await self._call(
            api_key="ollama", model="qwen2.5:7b", disable_thinking=False
        )
        self.assertEqual(kwargs["response_format"], {"type": "json_object"})

    async def test_ollama_with_thinking_disabled_still_passes(self):
        kwargs = await self._call(
            api_key="ollama", model="qwen2.5:7b", disable_thinking=True
        )
        self.assertEqual(kwargs["response_format"], {"type": "json_object"})
        self.assertEqual(kwargs["reasoning_effort"], "none")

    async def test_deepseek_v4_flash_passes_response_format(self):
        kwargs = await self._call(
            api_key="deepseek", model="deepseek-v4-flash", disable_thinking=False
        )
        self.assertEqual(kwargs["response_format"], {"type": "json_object"})

    async def test_vendor_prefixed_v4_model_passes(self):
        kwargs = await self._call(
            api_key="deepseek", model="vendor/deepseek-v4-pro", disable_thinking=False
        )
        self.assertEqual(kwargs["response_format"], {"type": "json_object"})

    async def test_other_model_does_not_pass_response_format(self):
        kwargs = await self._call(
            api_key="deepseek", model="qwen3.5", disable_thinking=False
        )
        self.assertNotIn("response_format", kwargs)


if __name__ == "__main__":
    unittest.main()
