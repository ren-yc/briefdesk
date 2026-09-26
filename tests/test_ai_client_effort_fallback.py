"""关思考参数的自适应降级：端点明确拒收 reasoning_effort 时改为省略并记住。

为什么会需要它：该参数在 OpenAI 规范里是 Optional、取值 model-dependent，第三方兼容
端点差异更大——有的严格校验并 400，有的静默忽略，有的用完全不同的键。硬编码发送会让
「关思考」与「只能用某一类端点」绑死（回归见 issue #3）。

数据一律虚构（见 AGENTS.md）。
"""

from __future__ import annotations

import asyncio
import json
from contextlib import ExitStack, contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
from openai import BadRequestError, InternalServerError, RateLimitError
from pydantic import SecretStr

from briefdesk.config import config
from briefdesk.plugins.ai_provider import engine
from briefdesk.plugins.ai_provider.engine import chat, rag_chat

_DEFAULTS: dict = {
    "ai_api_key": SecretStr("deepseek"),
    "ai_model": "qwen3.5",
    "ai_api_base": "http://endpoint-a/v1",
    "ai_reasoning_effort": "off",
}


@contextmanager
def _configured(**over):
    """在受控配置下运行：默认 off + 固定端点/模型，用例只覆盖关心的字段。"""
    stack = ExitStack()
    for key, value in {**_DEFAULTS, **over}.items():
        stack.enter_context(patch.object(config, key, value))
    try:
        yield
    finally:
        stack.close()


def _http_error(cls=BadRequestError, status: int = 400, param: str = "reasoning_effort"):
    """构造真实形状的 HTTP 错误：SDK 会把整个 error body 拼进 str(exc)（已实测）。"""
    body = {
        "error": {
            "message": "Invalid option: expected one of low|medium|high",
            "type": "invalid_request_error",
            "param": param,
        }
    }
    resp = httpx.Response(
        status, request=httpx.Request("POST", "http://x/v1/chat/completions"), json=body
    )
    return cls(f"Error code: {status} - {json.dumps(body)}", response=resp, body=body)


def _client(*, reject: bool = True, error: Exception | None = None):
    """假客户端 + 调用记录；reject 时「带该参数」的调用会抛错。"""
    recorded: list[dict] = []

    async def create(**kwargs):
        recorded.append(kwargs)
        if reject and "reasoning_effort" in kwargs:
            raise error if error is not None else _http_error()
        return SimpleNamespace(
            choices=[
                SimpleNamespace(message=SimpleNamespace(content="ok"), finish_reason="stop")
            ]
        )

    class _Client(SimpleNamespace):
        def with_options(self, **_kwargs):
            # chat(max_retries=…) 会在共享客户端上派生一次；桩按原样返回自己即可
            return self

    client = _Client(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    return client, recorded


@pytest.fixture(autouse=True)
def _clean_memory():
    engine._effort_rejected.clear()
    yield
    engine._effort_rejected.clear()


async def test_off_degrades_after_rejection():
    """首次带参撞 400 → 立即不带参重试一次，并记住该端点。"""
    client, calls = _client()
    with _configured(), patch.object(engine, "get_ai_client", return_value=client):
        await chat([], temperature=0.1, max_tokens=64)

    assert [("reasoning_effort" in c) for c in calls] == [True, False]
    assert engine._effort_rejected == {("http://endpoint-a/v1", "qwen3.5")}


async def test_memory_applies_to_next_call():
    """记忆生效：下一次调用从一开始就不带该参数，只发一次请求。"""
    client, calls = _client()
    with _configured(), patch.object(engine, "get_ai_client", return_value=client):
        await chat([], temperature=0.1, max_tokens=64)
        calls.clear()
        await chat([], temperature=0.1, max_tokens=64)

    assert len(calls) == 1
    assert "reasoning_effort" not in calls[0]


async def test_unrelated_400_is_not_retried():
    """无关 400（点名的是别的参数）原样抛出：不重试、不记忆，绝不吞成降级。"""
    client, calls = _client(error=_http_error(param="max_tokens"))
    with (
        _configured(),
        patch.object(engine, "get_ai_client", return_value=client),
        pytest.raises(BadRequestError),
    ):
        await chat([], temperature=0.1, max_tokens=64)

    assert len(calls) == 1
    assert engine._effort_rejected == set()


async def test_memory_scoped_per_endpoint():
    """记忆按端点隔离：A 端点拒收不影响 B 端点照常发参数。"""
    client_a, _ = _client()
    with _configured(), patch.object(engine, "get_ai_client", return_value=client_a):
        await chat([], temperature=0.1, max_tokens=64)

    client_b, calls_b = _client(reject=False)
    with _configured(ai_api_base="http://endpoint-b/v1"), patch.object(
        engine, "get_ai_client", return_value=client_b
    ):
        await chat([], temperature=0.1, max_tokens=64)

    assert "reasoning_effort" in calls_b[0]


async def test_base_url_normalized():
    """端点归一化：尾斜杠与大小写不同视为同一端点，不重复探测。"""
    client, _ = _client()
    with _configured(ai_api_base="http://Endpoint-A/v1/"), patch.object(
        engine, "get_ai_client", return_value=client
    ):
        await chat([], temperature=0.1, max_tokens=64)

    assert engine._effort_rejected == {("http://endpoint-a/v1", "qwen3.5")}


async def test_rag_shares_memory_when_override_empty():
    """RAG 走空 override 时回退到主客户端：必须与主通道共享同一份记忆。"""
    client, _ = _client()
    rag_calls: list[dict] = []

    async def rag_create(**kwargs):
        rag_calls.append(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(message=SimpleNamespace(content="ok"), finish_reason="stop")
            ]
        )

    rag_client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=rag_create))
    )
    with _configured(), patch.object(engine, "get_ai_client", return_value=client):
        await chat([], temperature=0.1, max_tokens=64)
    with _configured(), patch.object(engine, "get_alt_client", return_value=rag_client):
        await rag_chat([], api_base="", model="")

    assert len(rag_calls) == 1
    assert "reasoning_effort" not in rag_calls[0]


async def test_warning_logged_once_under_concurrency(caplog):
    """并发首撞：真并发下多个请求各自探测一次，但记忆与告警只落一次。

    桩里让出一次执行权（await sleep(0)），三个请求才会同时「在路上」——否则失败是
    即时抛出的，后两个会直接命中记忆、根本不探测，测不到并发路径。
    """
    calls: list[dict] = []

    async def create(**kwargs):
        await asyncio.sleep(0)
        calls.append(kwargs)
        if "reasoning_effort" in kwargs:
            raise _http_error()
        return SimpleNamespace(
            choices=[
                SimpleNamespace(message=SimpleNamespace(content="ok"), finish_reason="stop")
            ]
        )

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    with (
        _configured(),
        patch.object(engine, "get_ai_client", return_value=client),
        caplog.at_level("WARNING"),
    ):
        await asyncio.gather(
                chat([], temperature=0.1, max_tokens=64),
                chat([], temperature=0.1, max_tokens=64),
                chat([], temperature=0.1, max_tokens=64),
            )

    # 只钉不变量，不钉次数：谁先撞上 400 取决于调度，「已看到记忆的请求直接省略」与
    # 「还没看到的各探测一次」两种交错都合法（真端点上还受网络往返影响）
    with_param = [c for c in calls if "reasoning_effort" in c]
    without_param = [c for c in calls if "reasoning_effort" not in c]
    assert 1 <= len(with_param) <= 3, calls
    assert without_param, "至少有一个请求走了省略路径"
    assert len(engine._effort_rejected) == 1
    assert sum("不接受 reasoning_effort" in r.message for r in caplog.records) == 1


async def test_retry_failure_propagates_and_keeps_memory():
    """降级重试本身失败：异常照抛，但「该端点拒收该参数」这一事实仍然记住。"""
    calls: list[dict] = []
    server_error = _http_error(cls=InternalServerError, status=500, param="")

    async def create(**kwargs):
        calls.append(kwargs)
        if "reasoning_effort" in kwargs:
            raise _http_error()
        raise server_error

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    with (
        _configured(),
        patch.object(engine, "get_ai_client", return_value=client),
        pytest.raises(InternalServerError),
    ):
        await chat([], temperature=0.1, max_tokens=64)

    assert len(calls) == 2
    assert engine._effort_rejected == {("http://endpoint-a/v1", "qwen3.5")}


async def test_429_does_not_degrade():
    """限流不是「参数不兼容」：不重试、不记忆。"""
    client, calls = _client(error=_http_error(cls=RateLimitError, status=429))
    with (
        _configured(),
        patch.object(engine, "get_ai_client", return_value=client),
        pytest.raises(RateLimitError),
    ):
        await chat([], temperature=0.1, max_tokens=64)

    assert len(calls) == 1
    assert engine._effort_rejected == set()


async def test_exactly_one_extra_request_with_max_retries_zero():
    """判官路径（max_retries=0）：首次恰好多一次请求，之后恢复一次。"""
    client, calls = _client()
    with _configured(), patch.object(engine, "get_ai_client", return_value=client):
        await chat([], temperature=0.1, max_tokens=64, timeout=45.0, max_retries=0)
        assert len(calls) == 2
        calls.clear()
        await chat([], temperature=0.1, max_tokens=64, timeout=45.0, max_retries=0)

    assert len(calls) == 1
