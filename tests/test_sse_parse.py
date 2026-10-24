"""三源共用 SSE 帧解析助手（briefdesk.sources_base.iter_sse_data_events）测试。

背景：三个 client 此前各持一份逐字节相同的解析块，都走 httpx 的 aiter_lines——
其 splitlines 语义把 U+0085/U+2028/U+2029 当换行，而上游 serde_json 不转义非 ASCII，
含这些字符的 data 行会被拆断、事件静默丢失。助手只按 LF 切行。
"""

import json
import logging
import unittest

import httpx

from briefdesk.sources_base import MAX_SSE_BUFFER_BYTES, iter_sse_data_events


def _response(body: bytes) -> httpx.Response:
    return httpx.Response(
        200,
        content=body,
        headers={"content-type": "text/event-stream"},
    )


def _frame(payload: dict) -> bytes:
    return b"data: " + json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n\n"


class _Collecting(logging.Logger):
    """收集日志的假 logger（助手用传入的 log 记 WARNING）。"""

    def __init__(self):
        super().__init__("test.sse")
        self.warnings: list[str] = []

    def warning(self, msg, *args, **kwargs):
        self.warnings.append(msg % args if args else msg)


class IterSseDataEventsTest(unittest.IsolatedAsyncioTestCase):
    async def _collect(self, body: bytes, log=None):
        events: list[dict] = []
        async for event in iter_sse_data_events(
            _response(body), log=log or _Collecting()
        ):
            events.append(event)
        return events

    async def test_line_separator_inside_data_is_preserved(self):
        body = _frame({"event": "message.new", "rawid": "r1", "content": "a\u2028b"})
        body += _frame({"event": "message.new", "rawid": "r2", "content": "c"})
        events = await self._collect(body)
        self.assertEqual([e["rawid"] for e in events], ["r1", "r2"])
        self.assertEqual(events[0]["content"], "a\u2028b")

    async def test_crlf_frames_comment_id_event_lines(self):
        body = (
            b": ping\r\n"
            b"id: 1\r\n"
            b"event: message.new\r\n"
            b'data: {"event":"message.new","rawid":"r1"}\r\n'
            b"\r\n"
        )
        events = await self._collect(body)
        self.assertEqual([e["rawid"] for e in events], ["r1"])

    async def test_data_without_space_after_colon(self):
        events = await self._collect(b'data:{"rawid":"r1"}\n\n')
        self.assertEqual([e["rawid"] for e in events], ["r1"])

    async def test_unparseable_data_logs_warning_and_continues(self):
        body = b"data: {not json\n\n" + _frame({"rawid": "r1"})
        log = _Collecting()
        events = await self._collect(body, log=log)
        self.assertEqual([e["rawid"] for e in events], ["r1"])
        self.assertTrue(log.warnings)

    async def test_non_object_json_skipped(self):
        body = b"data: [1, 2]\n\n" + _frame({"rawid": "r1"})
        log = _Collecting()
        events = await self._collect(body, log=log)
        self.assertEqual([e["rawid"] for e in events], ["r1"])
        self.assertTrue(log.warnings)

    async def test_buffer_cap_ends_stream_with_warning(self):
        body = b"data: " + b"x" * (MAX_SSE_BUFFER_BYTES + 64)
        log = _Collecting()
        events = await self._collect(body, log=log)
        self.assertEqual(events, [])
        self.assertTrue(any("缓冲超限" in w for w in log.warnings))

    async def test_cap_fires_on_blank_line_less_stream(self):
        """行连续但始终不出现空行的畸形流同样触发上限。

        只量未切行的残留时会漏掉这一类：每行切出后缓冲即归零，上限永远撞不到
        （基线按累计喂入量计量，能抓到）。此处总量超过上限但每行都很短。
        """
        line = b'data: {"rawid": "r1"}\n'
        body = line * ((MAX_SSE_BUFFER_BYTES // len(line)) + 100)
        log = _Collecting()
        events = await self._collect(body, log=log)
        self.assertEqual(events, [], "无空行即无完整帧，不应产出事件")
        self.assertTrue(any("缓冲超限" in w for w in log.warnings), "未触发上限")

    async def test_well_framed_stream_far_beyond_cap_is_not_fused(self):
        """反向：正常分帧的大流量（总量远超上限）不得误报——每帧结束即归零。"""
        frames = b'data: {"rawid": "r1"}\n\n'
        body = frames * ((MAX_SSE_BUFFER_BYTES // len(frames)) + 100)
        log = _Collecting()
        events = await self._collect(body, log=log)
        expected = len(body) // len(frames)
        self.assertEqual(len(events), expected)
        self.assertFalse(any("缓冲超限" in w for w in log.warnings))


if __name__ == "__main__":
    unittest.main()
