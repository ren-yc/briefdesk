"""出境可观测性的验收：本地判定矩阵、发送边界计数与真实引擎接线。

前半部分（判定与计数助手）不接 SDK：验收「判定纯函数」与「指标形状」。后半部分
用真实 `AsyncOpenAI` + `httpx.MockTransport` 跑真实引擎，验收「计数接在哪个
边界上」——排队失败不算、SDK 内部重试单独记、effort 降级算新一次尝试。全部离线，
样本域名一律 `example.invalid`。
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from pydantic import SecretStr

from briefdesk import ai_egress, ai_ports, telemetry
from briefdesk.config import config
from briefdesk.plugins.ai_provider import engine
from briefdesk.plugins.ai_provider.engine import Provider

REMOTE_BASE = "https://ai.example.invalid/v1"
LOCAL_BASE = "http://localhost:11434/v1"


@pytest.fixture(autouse=True)
def _telemetry_isolation(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """遥测隔离且固定开启：本文件的断言都基于「确实记了」。"""
    telemetry.reset_for_tests()
    monkeypatch.setattr(telemetry, "telemetry_dir", lambda: tmp_path / "tel")
    monkeypatch.setattr(config, "telemetry_enabled", True)
    yield
    telemetry.reset_for_tests()


def _counters() -> dict[str, int]:
    return telemetry.snapshot()["counters"]


LOCAL_BASES = [
    "http://localhost",
    "http://localhost/",
    "http://localhost:11434",
    "http://localhost:11434/v1",
    "https://LOCALHOST:8080/v1",
    "http://127.0.0.1",
    "http://127.0.0.1:8000/v1",
    "http://127.255.255.254",
    "http://[::1]",
    "http://[::1]:8080/v1",
    "http://0.0.0.0:3000",
    "  http://localhost:11434/v1  ",
]

REMOTE_BASES = [
    "",
    "   ",
    "localhost:11434",  # 没有 scheme
    "ftp://localhost",
    "http://localhost@remote.example.invalid",  # 主机藏在凭据段
    "http://user:pass@localhost",  # 带凭据段：不猜它想连哪里
    "http://@127.0.0.1",  # 空 userinfo 同样可疑（username 是空串而不是 None）
    "http://local\nhost",  # 控制字符被解析器删掉后才是 localhost
    "http://localhost\r/v1",  # 同上：CR 会让解析结果与字面不一致
    "http://localhost.example.invalid",  # 近似域名
    "http://localhost.",  # 带点写法不猜，按远端
    "http://10.0.0.5",
    "http://172.16.0.1:8080",
    "http://192.168.1.1",
    "http://[::]",  # 未指定地址只有 IPv4 的 0.0.0.0 才算本地
    "http://localhost:99999",  # 端口越界
    "http://localhost:abc",  # 端口非数字
    "http://:8080",
    "http://",
    "not a url",
    "http://[::1",  # 畸形 IPv6
    REMOTE_BASE,
]


@pytest.mark.parametrize("base", LOCAL_BASES)
def test_local_endpoint_matrix_fail_closed(base: str) -> None:
    assert ai_egress.is_local_endpoint(base) is True


@pytest.mark.parametrize("base", REMOTE_BASES)
def test_remote_endpoint_matrix_fail_closed(base: str) -> None:
    # 认不出的写法一律按远端：判错方向时最多多假名化一次
    assert ai_egress.is_local_endpoint(base) is False


def test_remote_and_local_attempts_are_separate() -> None:
    assert ai_egress.record_send_attempt(ai_egress.PORT_CHAT, ai_egress.ROLE_MAIN, REMOTE_BASE) is False
    assert ai_egress.record_send_attempt(ai_egress.PORT_CHAT, ai_egress.ROLE_MAIN, LOCAL_BASE) is True
    assert _counters() == {
        "ai.egress.remote_attempt.chat.main|": 1,
        "ai.local.attempt.chat.main|": 1,
    }


@pytest.mark.parametrize(
    "port",
    [ai_egress.PORT_CHAT, ai_egress.PORT_RAG, ai_egress.PORT_EMBED],
)
def test_each_port_has_its_own_series(port: str) -> None:
    for _ in range(2):
        ai_egress.record_send_attempt(port, ai_egress.ROLE_MAIN, REMOTE_BASE)
    counters = _counters()
    assert counters == {f"ai.egress.remote_attempt.{port}.main|": 2}


def test_queue_failures_never_count_remote_attempt() -> None:
    ai_egress.record_queue_timeout(ai_egress.PORT_RAG, ai_egress.ROLE_JUDGE)
    ai_egress.record_queue_cancelled(ai_egress.PORT_CHAT, ai_egress.ROLE_GEN)
    counters = _counters()
    assert counters == {
        "ai.queue.timeout.rag.judge|": 1,
        "ai.queue.cancelled.chat.gen|": 1,
    }
    assert not any(".attempt." in key for key in counters)


def test_sdk_retry_separate_from_logical_attempt() -> None:
    is_local = ai_egress.record_send_attempt(
        ai_egress.PORT_CHAT, ai_egress.ROLE_MAIN, REMOTE_BASE
    )
    assert is_local is False
    ai_egress.record_sdk_retry(ai_egress.PORT_CHAT, ai_egress.ROLE_MAIN, is_local)
    counters = _counters()
    assert counters["ai.egress.remote_attempt.chat.main|"] == 1
    assert counters["ai.egress.sdk_retry.chat.main|"] == 1

    # 本地端点同一套分桶，但不写远端键
    local_flag = ai_egress.record_send_attempt(
        ai_egress.PORT_EMBED, ai_egress.ROLE_MAIN, LOCAL_BASE
    )
    assert local_flag is True
    ai_egress.record_sdk_retry(ai_egress.PORT_EMBED, ai_egress.ROLE_MAIN, local_flag)
    counters = _counters()
    assert counters["ai.local.attempt.embed.main|"] == 1
    assert counters["ai.local.sdk_retry.embed.main|"] == 1
    assert "ai.egress.sdk_retry.embed.main|" not in counters


async def test_contextvar_metadata_is_isolated_between_tasks() -> None:
    async def _worker(source: str) -> None:
        with ai_egress.use_metadata(
            port=ai_egress.PORT_RAG,
            role=ai_egress.ROLE_JUDGE,
            kind="judge",
            source=source,
        ):
            # 让出控制权后再读：并发任务之间不能串味
            await asyncio.sleep(0)
            assert ai_egress.current_metadata().source == source
            ai_egress.record_send_attempt(
                ai_egress.PORT_RAG, ai_egress.ROLE_JUDGE, REMOTE_BASE
            )

    await asyncio.gather(_worker("weflow"), _worker("qqflow"))
    counters = _counters()
    assert counters == {
        "ai.egress.remote_attempt.rag.judge|weflow": 1,
        "ai.egress.remote_attempt.rag.judge|qqflow": 1,
    }
    # with 退出后回到默认元数据（reset 而不是重设，嵌套时才能回到外层值）
    assert ai_egress.current_metadata() == ai_egress.EgressMetadata()

    with ai_egress.use_metadata(
        port=ai_egress.PORT_CHAT, role=ai_egress.ROLE_MAIN, source="outer"
    ):
        with ai_egress.use_metadata(
            port=ai_egress.PORT_EMBED, role=ai_egress.ROLE_MAIN, source="inner"
        ):
            assert ai_egress.current_metadata().source == "inner"
            assert ai_egress.current_metadata().port == ai_egress.PORT_EMBED
        assert ai_egress.current_metadata().source == "outer"
        assert ai_egress.current_metadata().port == ai_egress.PORT_CHAT
    assert ai_egress.current_metadata() == ai_egress.EgressMetadata()


def test_source_label_stays_out_of_metric_name() -> None:
    with ai_egress.use_metadata(
        port=ai_egress.PORT_CHAT,
        role=ai_egress.ROLE_CLASSIFY,
        kind="sysb",
        source="weflow",
    ):
        ai_egress.record_send_attempt(
            ai_egress.PORT_CHAT, ai_egress.ROLE_CLASSIFY, REMOTE_BASE
        )
    # 指标名只由固定词表拼成；源标签只进遥测维度
    assert _counters() == {"ai.egress.remote_attempt.chat.classify|weflow": 1}


def test_metadata_labels_converge_to_controlled_vocabulary() -> None:
    """陌生人名与 ID 不能原样进遥测键：形态不符整值归到默认桶。"""
    with ai_egress.use_metadata(
        port=ai_egress.PORT_CHAT,
        role=ai_egress.ROLE_MAIN,
        kind="张三",
        source="u_12345",
    ):
        metadata = ai_egress.current_metadata()
        assert metadata.kind == "other"
        assert metadata.source == ""
        ai_egress.record_send_attempt(
            ai_egress.PORT_CHAT, ai_egress.ROLE_MAIN, REMOTE_BASE
        )

    counters = _counters()
    assert counters == {"ai.egress.remote_attempt.chat.main|": 1}
    for key in counters:
        assert "张三" not in key
        assert "u_12345" not in key


def test_metadata_labels_keep_well_formed_values() -> None:
    with ai_egress.use_metadata(
        port=ai_egress.PORT_CHAT,
        role=ai_egress.ROLE_CLASSIFY,
        kind="judge",
        source="weflow",
    ):
        metadata = ai_egress.current_metadata()
        assert metadata.kind == "judge"
        assert metadata.source == "weflow"

    # 空标签是默认值，不算「形态不符」：不能被改写成 other
    with ai_egress.use_metadata(port=ai_egress.PORT_CHAT, role=ai_egress.ROLE_MAIN):
        assert ai_egress.current_metadata().kind == ""
        assert ai_egress.current_metadata().source == ""

    # 超长标签同样按形态不符处理（受控词不含 64 字符以上的值）
    with ai_egress.use_metadata(
        port=ai_egress.PORT_CHAT,
        role=ai_egress.ROLE_MAIN,
        kind="k" * 65,
        source="s" * 65,
    ):
        assert ai_egress.current_metadata().kind == "other"
        assert ai_egress.current_metadata().source == ""


def test_shape_filter_does_not_catch_id_shaped_labels() -> None:
    """形态过滤不是受控词表：全小写、带连字符的 `member-0001` 会原样通过。

    这是已知局限：`_sanitize_label` 只看形态，认不出「长得像 ID」的取值。接线
    的调用方必须从插件名或固定任务标签取维度，不能把业务 ID 直接传进来。钉住
    这条行为是为了让「以为已经挡住身份信息」的改动先在这里失败，而不是留给人
    凭印象判断。
    """
    with ai_egress.use_metadata(
        port=ai_egress.PORT_CHAT,
        role=ai_egress.ROLE_MAIN,
        kind="member-0001",
        source="member-0002",
    ):
        metadata = ai_egress.current_metadata()
        assert metadata.kind == "member-0001"
        assert metadata.source == "member-0002"


def test_unknown_port_or_role_is_rejected() -> None:
    with pytest.raises(ValueError, match="chat"):
        ai_egress.record_send_attempt("chatting", ai_egress.ROLE_MAIN, REMOTE_BASE)
    with pytest.raises(ValueError, match="main"):
        ai_egress.record_queue_timeout(ai_egress.PORT_CHAT, "primary")
    with pytest.raises(ValueError, match="classify"):
        ai_egress.record_sdk_retry(ai_egress.PORT_EMBED, "角色不明", False)
    with pytest.raises(ValueError), ai_egress.use_metadata(
        port="nope", role=ai_egress.ROLE_MAIN
    ):
        pass
    with pytest.raises(ValueError), ai_egress.use_metadata(
        port=ai_egress.PORT_CHAT, role="nope"
    ):
        pass


def test_blocked_metric_is_reserved_only() -> None:
    assert (
        ai_egress.BLOCKED_METRIC_TEMPLATE.format(
            port=ai_egress.PORT_CHAT, role=ai_egress.ROLE_MAIN
        )
        == "ai.egress.blocked.chat.main"
    )
    # 本模块没有阻断路径：所有助手跑一遍也不能产出 blocked 非零
    ai_egress.record_send_attempt(ai_egress.PORT_CHAT, ai_egress.ROLE_MAIN, REMOTE_BASE)
    ai_egress.record_queue_timeout(ai_egress.PORT_CHAT, ai_egress.ROLE_MAIN)
    ai_egress.record_queue_cancelled(ai_egress.PORT_CHAT, ai_egress.ROLE_MAIN)
    ai_egress.record_sdk_retry(ai_egress.PORT_CHAT, ai_egress.ROLE_MAIN, False)
    assert not any("blocked" in key for key in _counters())


def test_disabled_telemetry_is_unknown_not_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    ai_egress.record_send_attempt(ai_egress.PORT_CHAT, ai_egress.ROLE_MAIN, REMOTE_BASE)
    monkeypatch.setattr(config, "telemetry_enabled", False)
    ai_egress.record_send_attempt(ai_egress.PORT_CHAT, ai_egress.ROLE_MAIN, REMOTE_BASE)
    ai_egress.record_send_attempt(ai_egress.PORT_EMBED, ai_egress.ROLE_MAIN, LOCAL_BASE)
    ai_egress.record_queue_cancelled(ai_egress.PORT_CHAT, ai_egress.ROLE_MAIN)
    # 关闭期间既不累加也不落零值：读数只能表达「未知」，不能伪装成零出境
    assert _counters() == {"ai.egress.remote_attempt.chat.main|": 1}
    assert all(value > 0 for value in _counters().values())


# ── 真实引擎接线：发送边界计数（正控）与替身负控 ──
#
# 为什么必须走真实引擎 + MockTransport：计数接在 SDK create 的前一刻、重试计数接在
# httpx 的请求钩子上，用假客户端（SimpleNamespace）测到的只是「测试自己搭的桩被
# 调了几次」。只有真实 AsyncOpenAI 才会装上钩子、才会自己重试、才会写重试序号头。

_ENGINE_DEFAULTS: dict[str, Any] = {
    "ai_api_key": SecretStr("demo-key"),
    "ai_api_base": REMOTE_BASE,
    "ai_model": "demo-model",
    "ai_json_mode": "off",
    "ai_reasoning_effort": "auto",
    "ai_max_concurrency": 0,
    "embed_api_key": SecretStr("demo-embed-key"),
    "embed_api_base": "",
    "embed_model": "demo-embed",
    "embed_batch_size": 2,
}

# 出境相关的键前缀：端口层还会记 .call/.latency 之类的既有指标，不属于本文件口径。
_EGRESS_PREFIXES = ("ai.egress.", "ai.local.", "ai.queue.")


def _egress_counters() -> dict[str, int]:
    return {
        key: value
        for key, value in _counters().items()
        if key.startswith(_EGRESS_PREFIXES)
    }


@pytest.fixture(autouse=True)
def _engine_isolation(monkeypatch: pytest.MonkeyPatch) -> Any:
    """每个用例都从「无缓存客户端 / 无能力记忆 / 无注册供应商」起步。

    客户端按 base_url + api_key 惰性缓存：不清掉就会把上一个用例的 transport
    （以及 MockTransport 的响应序列）带进下一个用例，让计数看起来出现在无关的
    调用上。能力记忆（effort 拒收）同理，会跨用例改变实际发出的参数。
    """
    for name, value in _ENGINE_DEFAULTS.items():
        monkeypatch.setattr(config, name, value)
    engine._client = None
    engine._embed_client = None
    engine._alt_clients.clear()
    engine._ai_semaphore = None
    engine._effort_rejected.clear()
    ai_ports.set_ai(None)
    yield
    ai_ports.set_ai(None)
    engine._client = None
    engine._embed_client = None
    engine._alt_clients.clear()
    engine._ai_semaphore = None
    engine._effort_rejected.clear()


def _install_transport(
    monkeypatch: pytest.MonkeyPatch,
    handler: Any,
) -> None:
    """只把 http 传输层换成 MockTransport，钩子仍由引擎自己装。

    替换的是 `DefaultAsyncHttpxClient` 这个构造点，并把引擎传进来的 `event_hooks`
    原样交给测试客户端：如果引擎漏装钩子，`pop` 会直接 KeyError 让用例失败，而不是
    悄悄测出一个「本来就没人记」的零。其余部分（AsyncOpenAI 构造、重试策略、错误
    映射、请求头）全部是真实 SDK 实现。
    """

    def _factory(**kwargs: Any) -> httpx.AsyncClient:
        event_hooks = kwargs.pop("event_hooks")
        return httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=True,
            event_hooks=event_hooks,
            **kwargs,
        )

    monkeypatch.setattr(engine, "DefaultAsyncHttpxClient", _factory)


def _chat_body(content: str = "ok") -> dict[str, Any]:
    return {
        "id": "chatcmpl-demo",
        "object": "chat.completion",
        "created": 0,
        "model": "demo-model",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
    }


def _embeddings_body(count: int) -> dict[str, Any]:
    return {
        "object": "list",
        "model": "demo-embed",
        "data": [
            {"object": "embedding", "index": index, "embedding": [0.1, 0.2]}
            for index in range(count)
        ],
    }


def _stub_chat_client() -> tuple[SimpleNamespace, AsyncMock]:
    """只用来断言「create 从未被调用」的客户端桩（排队失败路径）。"""
    create = AsyncMock()
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    return client, create


class _BlockingSemaphore:
    """acquire 永不返回的假信号量：把请求固定在排队阶段。

    比预先占满真信号量更稳定——不需要用 sleep 猜「请求是否已经进入等待」，
    `entered` 事件让取消发生在确定的位置。
    """

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.released = 0

    async def acquire(self) -> bool:
        self.entered.set()
        await asyncio.Event().wait()
        return True  # pragma: no cover — 只能被取消/超时结束

    def release(self) -> None:
        self.released += 1


class TestSendBoundary:
    """真实引擎的发送边界：计数时机、端口归属、排队失败与 SDK 重试分桶。"""

    async def test_chat_counts_remote_attempt_before_send(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        requests: list[httpx.Request] = []
        counted_before_transport: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            # 请求已经到达 transport 时计数必须已经落账：这才叫「发送前计数」，
            # 而不是「发送后再补一笔」。
            counted_before_transport.append(
                _counters().get("ai.egress.remote_attempt.chat.main|", 0)
            )
            return httpx.Response(200, json=_chat_body())

        _install_transport(monkeypatch, handler)
        ai_ports.set_ai(Provider())

        await ai_ports.chat(
            [{"role": "user", "content": "hi"}], temperature=0.1, max_tokens=16
        )

        assert counted_before_transport == [1]
        assert len(requests) == 1
        assert _egress_counters() == {"ai.egress.remote_attempt.chat.main|": 1}

    async def test_local_base_counts_local_not_remote(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json=_chat_body())

        _install_transport(monkeypatch, handler)
        monkeypatch.setattr(config, "ai_api_base", "http://127.0.0.1:11434/v1")
        ai_ports.set_ai(Provider())

        await ai_ports.chat(
            [{"role": "user", "content": "hi"}], temperature=0.1, max_tokens=16
        )

        # 本地请求不伪装成出境：remote 桶一个键都不能出现
        assert _egress_counters() == {"ai.local.attempt.chat.main|": 1}
        assert len(requests) == 1

    async def test_rag_and_embed_use_their_own_ports(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        embed_batches: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/embeddings"):
                batch = len(json.loads(request.content)["input"])
                embed_batches.append(batch)
                return httpx.Response(200, json=_embeddings_body(batch))
            return httpx.Response(200, json=_chat_body("answer"))

        _install_transport(monkeypatch, handler)
        monkeypatch.setattr(config, "embed_api_base", REMOTE_BASE)
        ai_ports.set_ai(Provider())

        await ai_ports.rag_chat(
            [{"role": "user", "content": "q"}],
            model="rag-model",
            api_base="https://rag.example.invalid/v1",
            api_key="rag-key",
        )
        vectors = await ai_ports.embed_texts(["a", "b", "c", "d", "e"])

        assert len(vectors) == 5
        # 两条文本一批：5 条文本 = 3 次 create，出境次数按批而不是按条数
        assert embed_batches == [2, 2, 1]
        assert _egress_counters() == {
            "ai.egress.remote_attempt.rag.main|": 1,
            "ai.egress.remote_attempt.embed.main|": 3,
        }

    async def test_queue_timeout_and_cancel_do_not_count_send(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client, create = _stub_chat_client()
        timeout_sem = _BlockingSemaphore()
        cancel_sem = _BlockingSemaphore()
        ai_ports.set_ai(Provider())
        monkeypatch.setattr(engine, "get_ai_client", lambda: client)

        with (
            patch.object(engine, "get_ai_semaphore", return_value=timeout_sem),
            pytest.raises(TimeoutError),
        ):
            await ai_ports.chat([{"role": "user", "content": "x"}], timeout=0.01)

        with patch.object(engine, "get_ai_semaphore", return_value=cancel_sem):
            task = asyncio.create_task(
                ai_ports.chat([{"role": "user", "content": "y"}], timeout=30.0)
            )
            # 等到请求确实卡在 permit 等待上，再取消：否则取消可能发生在
            # 进入 wait_for 之前，测到的是「任务没跑」而不是「排队被取消」。
            await cancel_sem.entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        # 两种排队失败都没有发出请求：create 一次也没被调用
        assert create.await_count == 0
        assert timeout_sem.released == 0
        assert cancel_sem.released == 0
        assert _egress_counters() == {
            "ai.queue.timeout.chat.main|": 1,
            "ai.queue.cancelled.chat.main|": 1,
        }

    async def test_sdk_retry_separate_from_logical_attempt(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if len(requests) == 1:
                # retry-after-ms 让 SDK 的退避接近 0：本用例只关心分桶，不关心等待
                return httpx.Response(
                    500,
                    json={"error": {"message": "upstream hiccup"}},
                    headers={"retry-after-ms": "1"},
                )
            return httpx.Response(200, json=_chat_body())

        _install_transport(monkeypatch, handler)
        ai_ports.set_ai(Provider())

        await ai_ports.chat(
            [{"role": "user", "content": "hi"}], temperature=0.1, max_tokens=16
        )

        assert len(requests) == 2
        # 第一次是原请求（重试序号 0），第二次是 SDK 内部重试（序号 1）：
        # 钩子读到的正是这个头，没有它只能把两次网络包都算成逻辑发送。
        assert requests[1].headers.get("x-stainless-retry-count") == "1"
        assert _egress_counters() == {
            "ai.egress.remote_attempt.chat.main|": 1,
            "ai.egress.sdk_retry.chat.main|": 1,
        }

    async def test_effort_fallback_is_new_attempt_not_sdk_retry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bodies: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            bodies.append(body)
            if "reasoning_effort" in body:
                return httpx.Response(
                    400,
                    json={
                        "error": {
                            "message": "Invalid option for reasoning_effort",
                            "type": "invalid_request_error",
                            "param": "reasoning_effort",
                        }
                    },
                )
            return httpx.Response(200, json=_chat_body())

        _install_transport(monkeypatch, handler)
        monkeypatch.setattr(config, "ai_reasoning_effort", "off")
        ai_ports.set_ai(Provider())

        await ai_ports.chat(
            [{"role": "user", "content": "hi"}], temperature=0.1, max_tokens=16
        )

        assert len(bodies) == 2
        assert bodies[0]["reasoning_effort"] == "none"
        assert "reasoning_effort" not in bodies[1]
        # 降级重发是调用方发起的第二次显式 create：逻辑尝试 2，SDK 重试 0
        assert _egress_counters() == {"ai.egress.remote_attempt.chat.main|": 2}
        assert "ai.egress.sdk_retry.chat.main|" not in _counters()

    async def test_stub_provider_never_counts_egress(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from briefdesk.plugins.benchmark.runner import _StubProvider

        ai_ports.set_ai(_StubProvider())
        await ai_ports.chat(
            [
                {"role": "system", "content": '{"merge"'},
                {"role": "user", "content": "x"},
            ],
            temperature=0.1,
            max_tokens=16,
        )
        # 替身不经过引擎的发送边界：所有出境桶必须为空（而不是记了 0）
        assert _egress_counters() == {}

        # 同一启用遥测的夹具下的正控：真实引擎一次远端 create 必须记 1。
        # 没有这一半，「零出境」也可能只是遥测整体关掉了。
        _install_transport(
            monkeypatch, lambda request: httpx.Response(200, json=_chat_body())
        )
        ai_ports.set_ai(Provider())
        await ai_ports.chat(
            [{"role": "user", "content": "x"}], temperature=0.1, max_tokens=16
        )
        assert _egress_counters() == {"ai.egress.remote_attempt.chat.main|": 1}

    async def test_disabled_telemetry_writes_no_zero_keys(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_transport(
            monkeypatch, lambda request: httpx.Response(200, json=_chat_body())
        )
        monkeypatch.setattr(config, "telemetry_enabled", False)
        ai_ports.set_ai(Provider())

        await ai_ports.chat(
            [{"role": "user", "content": "hi"}], temperature=0.1, max_tokens=16
        )

        # 关闭时不写零值：零值键会被读数方当成「确认没有出境」，而实际是未知
        assert _counters() == {}
        assert _egress_counters() == {}

    async def test_hook_failure_never_breaks_the_request(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """钩子抛异常也必须放行请求：可观测性故障不能变成供应商调用失败。"""
        requests: list[httpx.Request] = []
        attempted: list[bool] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if len(requests) == 1:
                return httpx.Response(
                    500,
                    json={"error": {"message": "upstream hiccup"}},
                    headers={"retry-after-ms": "1"},
                )
            return httpx.Response(200, json=_chat_body("survived"))

        def _boom(*_args: Any, **_kwargs: Any) -> None:
            attempted.append(True)
            raise RuntimeError("遥测后端故障")

        _install_transport(monkeypatch, handler)
        # 只在重试那一次会真正调用它（首次请求序号为 0，钩子提前返回）
        monkeypatch.setattr(ai_egress, "record_sdk_retry", _boom)
        ai_ports.set_ai(Provider())

        resp = await ai_ports.chat(
            [{"role": "user", "content": "hi"}], temperature=0.1, max_tokens=16
        )

        assert resp.choices[0].message.content == "survived"
        assert len(requests) == 2
        # 钩子确实走到了记账、并在那里抛错：异常被吞掉，请求照常完成
        assert attempted == [True]
        assert "ai.egress.sdk_retry.chat.main|" not in _counters()
