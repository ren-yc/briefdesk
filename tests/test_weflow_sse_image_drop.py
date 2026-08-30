"""weflow（新）SSE 图片回查未命中丢弃回归测试。

审查回归：SSE [图片] 回查未命中时旧实现放行纯文本 "[图片]" 进 AI 分类
产生噪音卡；现整条丢弃，与 REST 路径「无 media.url 丢弃」及 qqflow
pre_filter 语义对齐。media.type 明确非图片时同样丢弃（回查被跳过）。
"""

import unittest

from briefdesk.plugins.weflow.normalize import normalize_sse


class _NoMediaClient:
    async def fetch_message_media(self, session_id: str, rawid: str, ts: int):
        return None


def _event(**overrides):
    event = {
        "event": "message.new",
        "rawid": "r1",
        "sessionId": "g",
        "sourceName": "张三",
        "content": "[图片]",
        "timestamp": 1,
    }
    event.update(overrides)
    return event


class WeFlowSseImageDropTest(unittest.IsolatedAsyncioTestCase):
    async def test_lookup_miss_drops_placeholder(self):
        msgs = await normalize_sse(_event(), _NoMediaClient())
        self.assertEqual(msgs, [])

    async def test_non_image_media_type_drops_placeholder(self):
        msgs = await normalize_sse(
            _event(media={"type": "video"}), _NoMediaClient()
        )
        self.assertEqual(msgs, [])

    async def test_lookup_hit_keeps_image_message(self):
        class _HitClient:
            async def fetch_message_media(self, session_id, rawid, ts):
                # 客户端方法契约：返回已提取的相对媒体路径（或 None）
                return "g/images/abc.jpg"

        msgs = await normalize_sse(_event(), _HitClient())
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0].image_urls, ["g/images/abc.jpg"])

    async def test_mixed_text_image_lookup_miss_kept(self):
        # 图片+文字混合消息不受丢弃影响：文字仍有信息价值
        msgs = await normalize_sse(
            _event(content="[图片] 这是说明文字"), _NoMediaClient()
        )
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0].content, "[图片] 这是说明文字")


if __name__ == "__main__":
    unittest.main()
