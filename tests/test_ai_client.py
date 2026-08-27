"""AI 客户端统一调用的思考模式开关测试（不触发真实网络请求）。"""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from pydantic import SecretStr

from briefdesk.config import config
from briefdesk.plugins.ai_provider.engine import chat, embed_texts, rag_chat


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
        ), patch.object(config, "ai_api_key", SecretStr("deepseek")), patch.object(
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
        ), patch.object(config, "ai_api_key", SecretStr("deepseek")), patch.object(
            config, "ai_model", "qwen3.5"
        ):
            await chat([], temperature=0.1, max_tokens=64)

        _, kwargs = create.call_args
        self.assertEqual(kwargs["reasoning_effort"], "none")
        self.assertNotIn("response_format", kwargs)
        self.assertEqual(kwargs["temperature"], 0.1)
        self.assertEqual(kwargs["max_tokens"], 64)


class AltChannelClientTest(unittest.TestCase):
    """备用通道客户端：按 (base_url, api_key) 缓存；两项全空回退主客户端。"""

    def setUp(self) -> None:
        from briefdesk.plugins.ai_provider import engine

        self._engine = engine
        engine._alt_clients.clear()
        self.addCleanup(engine._alt_clients.clear)

    def test_empty_overrides_return_main_client(self) -> None:
        client, _ = _fake_client()
        with patch(
            "briefdesk.plugins.ai_provider.engine.get_ai_client", return_value=client
        ):
            self.assertIs(self._engine.get_alt_client("", ""), client)
        self.assertEqual(self._engine._alt_clients, {})

    def test_same_override_pair_reuses_one_instance(self) -> None:
        with patch.object(config, "ai_api_key", SecretStr("main-key")), patch.object(
            config, "ai_api_base", "https://main.invalid/v1"
        ):
            first = self._engine.get_alt_client("https://alt.invalid/v1", "alt-key")
            second = self._engine.get_alt_client("https://alt.invalid/v1", "alt-key")
            third = self._engine.get_alt_client("https://other.invalid/v1", "alt-key")
        self.assertIs(first, second)
        self.assertIsNot(first, third)
        self.assertEqual(len(self._engine._alt_clients), 2)

    def test_single_override_falls_back_per_item(self) -> None:
        """只给 api_key 时 base 回退主配置（"只换 Key 不换端点"）。"""
        with patch.object(config, "ai_api_key", SecretStr("main-key")), patch.object(
            config, "ai_api_base", "https://main.invalid/v1"
        ):
            self._engine.get_alt_client("", "alt-key")
        self.assertIn(("https://main.invalid/v1", "alt-key"), self._engine._alt_clients)


class RagChatModelFallbackTest(unittest.IsolatedAsyncioTestCase):
    """rag_chat 的 model override：留空回退 ai_model，给值则原样使用。"""

    async def test_empty_model_falls_back_to_ai_model(self):
        client, create = _fake_client()
        with patch(
            "briefdesk.plugins.ai_provider.engine.get_alt_client", return_value=client
        ), patch.object(config, "ai_disable_thinking", False), patch.object(
            config, "ai_model", "deepseek-v4-flash"
        ):
            await rag_chat([], temperature=0.2, max_tokens=128)

        _, kwargs = create.call_args
        self.assertEqual(kwargs["model"], "deepseek-v4-flash")

    async def test_explicit_model_is_used(self):
        client, create = _fake_client()
        with patch(
            "briefdesk.plugins.ai_provider.engine.get_alt_client", return_value=client
        ), patch.object(config, "ai_disable_thinking", False), patch.object(
            config, "ai_model", "deepseek-v4-flash"
        ):
            await rag_chat([], temperature=0.2, max_tokens=128, model="qwen-plus")

        _, kwargs = create.call_args
        self.assertEqual(kwargs["model"], "qwen-plus")
        # 问答走正文，不强制 JSON 外壳
        self.assertNotIn("response_format", kwargs)


class ChatJsonObjectTest(unittest.IsolatedAsyncioTestCase):
    """严格 JSON 输出开关：ollama api key / deepseek-v4 模型传 response_format。"""

    async def _call(self, *, api_key: str, model: str, disable_thinking: bool):
        client, create = _fake_client()
        with patch("briefdesk.plugins.ai_provider.engine.get_ai_client", return_value=client), patch.object(
            config, "ai_disable_thinking", disable_thinking
        ), patch.object(config, "ai_api_key", SecretStr(api_key)), patch.object(
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


class EmbedBatchCountTest(unittest.IsolatedAsyncioTestCase):
    """P2 修复：embed_texts 每 chunk 校验返回向量数量——供应商少返即抛错，
    绝不产生错位结果（错位向量会持久化进 item_embeddings，永久污染余弦通道）。"""

    def _client(self, data):
        create = AsyncMock(return_value=SimpleNamespace(data=data))
        client = SimpleNamespace(embeddings=SimpleNamespace(create=create))
        return client, create

    def _patches(self, client):
        return (
            patch(
                "briefdesk.plugins.ai_provider.engine.get_embed_client",
                return_value=client,
            ),
            patch.object(config, "embed_batch_size", 10),
            patch.object(config, "ai_max_concurrency", 0),
        )

    async def test_short_return_raises_value_error(self):
        # 请求 2 条实返 1 条：必须抛 ValueError（调用方已有整批回退路径）
        client, create = self._client([SimpleNamespace(index=0, embedding=[0.1])])
        p_client, p_batch, p_sem = self._patches(client)
        with p_client, p_batch, p_sem, self.assertRaises(ValueError):
            await embed_texts(["a", "b"])
        self.assertEqual(len(create.call_args.kwargs["input"]), 2)

    async def test_full_return_keeps_input_order(self):
        # 数量一致时按 index 排序还原输入顺序（既有防御性排序不受影响）
        client, _ = self._client(
            [
                SimpleNamespace(index=1, embedding=[0.2]),
                SimpleNamespace(index=0, embedding=[0.1]),
            ]
        )
        p_client, p_batch, p_sem = self._patches(client)
        with p_client, p_batch, p_sem:
            got = await embed_texts(["a", "b"])
        self.assertEqual(got, [[0.1], [0.2]])


class EmbedAnnouncementTest(unittest.IsolatedAsyncioTestCase):
    """嵌入公告联动：失败置位 embedding_unreachable、成功撤销、未配置归 disabled。"""

    def setUp(self) -> None:
        from briefdesk import announcements

        self.announcements = announcements
        announcements.reset_announcements()
        self.addCleanup(announcements.reset_announcements)

    def _embed_client(self, *, error: Exception | None = None):
        create = AsyncMock(
            return_value=SimpleNamespace(
                data=[SimpleNamespace(index=0, embedding=[0.1])]
            )
        )
        if error is not None:
            create.side_effect = error
        return SimpleNamespace(embeddings=SimpleNamespace(create=create))

    def _patches(self, client, *, embed_base: str):
        return (
            patch(
                "briefdesk.plugins.ai_provider.engine.get_embed_client",
                return_value=client,
            ),
            patch.object(config, "embed_batch_size", 10),
            patch.object(config, "ai_max_concurrency", 0),
            patch.object(config, "embed_api_base", embed_base),
            patch.object(config, "embed_model", "bge-m3"),
        )

    async def test_failure_announces_unreachable_and_reraises(self):
        client = self._embed_client(error=RuntimeError("connection refused"))
        p = self._patches(client, embed_base="http://embed.invalid/v1")
        with p[0], p[1], p[2], p[3], p[4], self.assertRaises(RuntimeError):
            await embed_texts(["a"])
        items = self.announcements.get_announcements()
        self.assertEqual([x["code"] for x in items], ["embedding_unreachable"])
        self.assertEqual(items[0]["level"], "warning")
        self.assertIn("http://embed.invalid/v1", items[0]["message"])

    async def test_success_revokes_stale_announcement(self):
        client = self._embed_client()
        p = self._patches(client, embed_base="http://embed.invalid/v1")
        await self.announcements.announce("embedding_unreachable", "warning", "stale")
        with p[0], p[1], p[2], p[3], p[4]:
            await embed_texts(["a"])
        self.assertEqual(self.announcements.get_announcements(), [])

    async def test_failure_when_disabled_announces_disabled_not_unreachable(self):
        client = self._embed_client(error=RuntimeError("boom"))
        p = self._patches(client, embed_base="")
        with p[0], p[1], p[2], p[3], p[4], self.assertRaises(RuntimeError):
            await embed_texts(["a"])
        self.assertEqual(
            [x["code"] for x in self.announcements.get_announcements()],
            ["embedding_disabled"],
        )


class PluginSetupAnnouncementTest(unittest.IsolatedAsyncioTestCase):
    """ai_provider setup：按嵌入配置置位/撤销 embedding_disabled 公告。"""

    def setUp(self) -> None:
        from briefdesk import announcements

        self.announcements = announcements
        announcements.reset_announcements()
        self.addCleanup(announcements.reset_announcements)

    async def _setup_plugin(self, *, embed_base: str):
        from briefdesk.plugin.base import PluginContext
        from briefdesk.plugins.ai_provider.plugin import AiProviderPlugin

        plugin = AiProviderPlugin()
        try:
            with patch.object(config, "embed_api_base", embed_base):
                await plugin.setup(
                    PluginContext(
                        config=config,
                        publish_event=AsyncMock(),
                        subscribe_event=lambda *a, **k: None,
                        register_source=lambda *a, **k: None,
                        register_stage=lambda *a, **k: None,
                    )
                )
        except BaseException:
            await plugin.teardown()
            raise
        return plugin

    async def test_setup_without_embed_announces_disabled(self):
        plugin = await self._setup_plugin(embed_base="")
        try:
            self.assertEqual(
                [x["code"] for x in self.announcements.get_announcements()],
                ["embedding_disabled"],
            )
        finally:
            await plugin.teardown()

    async def test_setup_with_embed_does_not_announce_disabled(self):
        plugin = await self._setup_plugin(embed_base="http://embed.invalid/v1")
        try:
            self.assertEqual(self.announcements.get_announcements(), [])
        finally:
            await plugin.teardown()


if __name__ == "__main__":
    unittest.main()
