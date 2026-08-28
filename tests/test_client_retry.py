"""REST 连接失败重试测试（with_connect_retry 公共工具 + 两源客户端接入）。

覆盖：
- with_connect_retry：瞬态 ConnectError 重试成功 / 耗尽上抛 / 非连接异常
  不重试 / ConnectTimeout（ConnectError 子类）同样重试；
- qqflow：_get 链路（fetch_contacts）与 fetch_health 接入重试；503 就绪
  门控语义保持不变（连接重试只覆盖 TCP 失败，503 不重试）；
- weflow-legacy：_get 链路（fetch_contacts）接入重试。
"""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx

from briefdesk.plugins.qqflow.client import QqFlowClient, QqFlowNotReadyError
from briefdesk.plugins.weflow_legacy.client import WeFlowLegacyClient
from briefdesk.sources_base import with_connect_retry


class _FakeResp:
    """最小 httpx 响应替身（仅覆盖客户端 _get 使用的成员）。"""

    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = json.dumps(self._payload)
        self.url = SimpleNamespace(query=b"")

    @property
    def is_success(self):
        return 200 <= self.status_code < 400

    def json(self):
        return self._payload


class WithConnectRetryTest(unittest.IsolatedAsyncioTestCase):
    async def test_succeeds_after_transient_connect_errors(self):
        calls = 0

        async def fn():
            nonlocal calls
            calls += 1
            if calls < 3:
                raise httpx.ConnectError("All connection attempts failed")
            return "ok"

        result = await with_connect_retry(fn, base_delay=0)
        self.assertEqual(result, "ok")
        self.assertEqual(calls, 3)

    async def test_exhausted_raises_last_error(self):
        calls = 0

        async def fn():
            nonlocal calls
            calls += 1
            raise httpx.ConnectError("All connection attempts failed")

        with self.assertRaisesRegex(httpx.ConnectError, "All connection attempts failed"):
            await with_connect_retry(fn, attempts=2, base_delay=0)
        self.assertEqual(calls, 2)

    async def test_non_connect_error_not_retried(self):
        calls = 0

        async def fn():
            nonlocal calls
            calls += 1
            raise RuntimeError("boom")

        with self.assertRaisesRegex(RuntimeError, "boom"):
            await with_connect_retry(fn, attempts=3, base_delay=0)
        self.assertEqual(calls, 1)

    async def test_connect_timeout_is_retried(self):
        # ConnectTimeout 与 ConnectError 同为连接阶段失败（httpx 中平级），一并重试
        calls = 0

        async def fn():
            nonlocal calls
            calls += 1
            if calls < 2:
                raise httpx.ConnectTimeout("timed out")
            return "ok"

        result = await with_connect_retry(fn, base_delay=0)
        self.assertEqual(result, "ok")
        self.assertEqual(calls, 2)


class QqFlowClientRetryTest(unittest.IsolatedAsyncioTestCase):
    def _client_with_get(self, side_effect):
        client = QqFlowClient(base_url="http://127.0.0.1:5032", api_token="t")
        fake = SimpleNamespace(get=AsyncMock(side_effect=side_effect), post=AsyncMock())
        client._client = fake
        return client, fake

    async def test_fetch_contacts_retries_connect_errors(self):
        resp = _FakeResp(200, {"contacts": [{"username": "u_1", "displayName": "A"}]})
        client, fake = self._client_with_get(
            [httpx.ConnectError("refused"), httpx.ConnectError("refused"), resp]
        )
        result = await client.fetch_contacts()
        self.assertEqual(result, {"u_1": "A"})
        self.assertEqual(fake.get.call_count, 3)

    async def test_fetch_health_retries_connect_errors(self):
        # /health 是标量形状：status + version + account（单个阶段值），
        # 不再下发账号数组 —— 该接口免鉴权，账号清单等于枚举本机账号。
        health = {"status": "starting", "version": "0.5.0", "account": "unregistered"}
        client, fake = self._client_with_get(
            [httpx.ConnectError("refused"), _FakeResp(200, health)]
        )
        result = await client.fetch_health()
        self.assertEqual(result, health)
        self.assertEqual(fake.get.call_count, 2)

    async def test_503_semantics_preserved_after_connect_retry(self):
        # 连接层重试只覆盖 TCP 失败；503（索引期就绪门控）保持
        # QqFlowNotReadyError 语义——连接重试后返回 503 仍抛该瞬态异常
        client, fake = self._client_with_get(
            [httpx.ConnectError("refused"), _FakeResp(503, {"error": "indexing"})]
        )
        with self.assertRaises(QqFlowNotReadyError):
            await client.fetch_contacts()
        self.assertEqual(fake.get.call_count, 2)

    async def test_exhausted_connect_error_still_raises(self):
        # 重试耗尽：原样上抛（poll_cycle 的 lastError 行为保持不变）
        err = httpx.ConnectError("All connection attempts failed")
        client, fake = self._client_with_get([err, httpx.ConnectError("refused"), httpx.ConnectError("refused")])
        with self.assertRaises(httpx.ConnectError):
            await client.fetch_contacts()
        self.assertEqual(fake.get.call_count, 3)


class WeFlowLegacyClientRetryTest(unittest.IsolatedAsyncioTestCase):
    async def test_fetch_contacts_retries_connect_errors(self):
        client = WeFlowLegacyClient(base_url="http://127.0.0.1:5031", api_token="t")
        resp = _FakeResp(
            200, {"contacts": [{"username": "wx_1", "nickname": "B"}]}
        )
        fake = SimpleNamespace(
            get=AsyncMock(
                side_effect=[
                    httpx.ConnectError("refused"),
                    httpx.ConnectError("refused"),
                    resp,
                ]
            )
        )
        client._client = fake
        result = await client.fetch_contacts()
        self.assertEqual(result, {"wx_1": "B"})
        self.assertEqual(fake.get.call_count, 3)


if __name__ == "__main__":
    unittest.main()