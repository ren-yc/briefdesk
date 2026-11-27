"""RAG 实际派发计划（`ai_ports.resolve_rag_dispatch`）的回归。

为什么需要计划：调用方要不要把出站的展示名换成化名，取决于这次问答落在本地还是
远端端点，而这个决定必须发生在构建 prompt **之前**。计划的价值全在「描述实际
派发」——只描述调用方手里的 override 时，旧供应商的回退路径（override 被丢弃、
请求打到主端点）会让调用方把远端请求误判成本地，真名就这样静默出境，不会有任何
报错。因此计划与实际发送必须共用同一份判断，这里用「假供应商记录实际收到的
base/model」逐项对拍。
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import pytest

from briefdesk import ai_ports
from briefdesk.config import config

_MAIN_BASE = "https://main.example.invalid/v1"
_MAIN_MODEL = "main-model"
_OVERRIDE_BASE = "https://rag.example.invalid/v1"
_OVERRIDE_MODEL = "rag-model"


class _DedicatedProvider:
    """类级声明了 rag_chat 的供应商（新实现与内置 Provider 的形状）。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    async def chat(self, messages: list[dict], *, temperature: float, max_tokens: int):
        raise AssertionError("专用通道生效时不应回退 chat")

    async def rag_chat(
        self,
        messages: list[dict],
        *,
        temperature: float,
        max_tokens: int,
        model: str = "",
        api_base: str = "",
        api_key: str = "",
    ) -> None:
        self.calls.append(
            {"model": model, "api_base": api_base, "api_key": api_key}
        )

    def observed_dispatch(self) -> tuple[str, str]:
        """按供应商实现的回退规则（留空回退主配置）算出实际派发。"""
        call = self.calls[-1]
        return (
            call["model"] or config.ai_model,
            call["api_base"] or config.ai_api_base,
        )


class _LegacyProvider:
    """没有 rag_chat 的旧供应商：端口回退 chat，override 被丢弃。"""

    def __init__(self) -> None:
        self.used = ""

    async def chat(self, messages: list[dict], *, temperature: float, max_tokens: int):
        self.used = "chat"


class _InstanceOnlyProvider(_LegacyProvider):
    """实例级动态属性（Mock 的常见形状）：类上没声明 rag_chat，不算专用通道。

    「这个供应商有没有专用通道」的判断写两份时，最容易在这里分叉：实例走
    `__getattr__` 能拿到 rag_chat，按实例判断就会谎报「有专用通道」。
    """

    def __getattr__(self, name: str) -> Any:
        if name == "rag_chat":
            async def _rag(*args: Any, **kwargs: Any) -> None:
                self.used = "rag"

            return _rag
        raise AttributeError(name)


@pytest.fixture()
def main_channel() -> Iterator[None]:
    """固定主通道的模型与端点（计划里的回退值就是这两项）。"""
    with (
        patch.object(config, "ai_model", _MAIN_MODEL),
        patch.object(config, "ai_api_base", _MAIN_BASE),
    ):
        try:
            yield
        finally:
            ai_ports.set_ai(None)


def _use(provider: Any) -> None:
    ai_ports.set_ai(provider)


def test_dedicated_provider_uses_explicit_override(main_channel: None) -> None:
    _use(_DedicatedProvider())

    plan = ai_ports.resolve_rag_dispatch(
        model=_OVERRIDE_MODEL, api_base=_OVERRIDE_BASE, api_key="<your-api-key>"
    )

    assert plan.dedicated is True
    assert plan.model == _OVERRIDE_MODEL
    assert plan.api_base == _OVERRIDE_BASE
    assert plan.has_api_key is True


def test_dedicated_provider_blank_override_falls_back_to_main(main_channel: None) -> None:
    _use(_DedicatedProvider())

    plan = ai_ports.resolve_rag_dispatch()

    assert plan.dedicated is True
    assert plan.model == _MAIN_MODEL
    assert plan.api_base == _MAIN_BASE
    assert plan.has_api_key is False


def test_legacy_provider_drops_override_and_reports_main_endpoint(
    main_channel: None,
) -> None:
    """回退 chat 时 override 全丢：端点必须是主端点，不能回显 override。

    报 override 会让调用方按「本地」判定不改写展示名，而请求实际打到远端主端点。
    """
    _use(_LegacyProvider())

    plan = ai_ports.resolve_rag_dispatch(
        model=_OVERRIDE_MODEL, api_base=_OVERRIDE_BASE, api_key="<your-api-key>"
    )

    assert plan.dedicated is False
    assert plan.api_base == _MAIN_BASE
    assert plan.api_base != _OVERRIDE_BASE
    # 回退通道不接受 Key 覆盖：凭据层用主通道自己的
    assert plan.has_api_key is False


def test_legacy_provider_model_falls_back_to_main(main_channel: None) -> None:
    _use(_LegacyProvider())

    plan = ai_ports.resolve_rag_dispatch(
        model=_OVERRIDE_MODEL, api_base=_OVERRIDE_BASE
    )

    assert plan.model == _MAIN_MODEL
    assert plan.model != _OVERRIDE_MODEL


def test_plan_never_carries_key_plaintext(main_channel: None) -> None:
    """计划会进日志/快照/断言：Key 只能以布尔出现。"""
    _use(_DedicatedProvider())
    secret = "<your-api-key>"

    plan = ai_ports.resolve_rag_dispatch(model=_OVERRIDE_MODEL, api_key=secret)

    assert "api_key" not in {field.name for field in dataclasses.fields(plan)}
    assert secret not in repr(plan)


async def test_instance_level_rag_chat_is_not_a_dedicated_channel(
    main_channel: None,
) -> None:
    """类上没声明 rag_chat 就不是专用通道——计划与实际发送同一条判断。"""
    provider = _InstanceOnlyProvider()
    _use(provider)

    plan = ai_ports.resolve_rag_dispatch(api_base=_OVERRIDE_BASE)
    await ai_ports.rag_chat(
        [{"role": "user", "content": "q"}], api_base=_OVERRIDE_BASE
    )

    assert plan.dedicated is False
    assert plan.api_base == _MAIN_BASE
    # 实际发送也没走实例级 rag_chat：按实例判断会在这里分叉
    assert provider.used == "chat"


async def test_rag_chat_sends_where_plan_says_for_dedicated_provider(
    main_channel: None,
) -> None:
    provider = _DedicatedProvider()
    _use(provider)
    secret = "<your-api-key>"

    plan = ai_ports.resolve_rag_dispatch(
        model=_OVERRIDE_MODEL, api_base=_OVERRIDE_BASE, api_key=secret
    )
    await ai_ports.rag_chat(
        [{"role": "user", "content": "q"}],
        model=_OVERRIDE_MODEL,
        api_base=_OVERRIDE_BASE,
        api_key=secret,
    )

    assert provider.calls[-1]["model"] == _OVERRIDE_MODEL
    assert provider.calls[-1]["api_base"] == _OVERRIDE_BASE
    assert (plan.model, plan.api_base) == provider.observed_dispatch()
    assert plan.has_api_key is bool(provider.calls[-1]["api_key"])


async def test_rag_chat_sends_where_plan_says_for_legacy_provider(
    main_channel: None,
) -> None:
    """回退路径的对拍：计划说主端点，实际也只能打到主通道。

    chat 不接受 model/api_base 覆盖，所以「实际派发」就是主配置；计划若回显
    override，这条对拍会立刻红。
    """
    provider = _LegacyProvider()
    _use(provider)

    plan = ai_ports.resolve_rag_dispatch(
        model=_OVERRIDE_MODEL, api_base=_OVERRIDE_BASE, api_key="<your-api-key>"
    )
    await ai_ports.rag_chat(
        [{"role": "user", "content": "q"}],
        model=_OVERRIDE_MODEL,
        api_base=_OVERRIDE_BASE,
        api_key="<your-api-key>",
    )

    assert provider.used == "chat"
    assert (plan.model, plan.api_base) == (config.ai_model, config.ai_api_base)
    assert plan.api_base == _MAIN_BASE


# ── 出站目的地声明（假名化的「远端」判据） ──
#
# 路由由供应商实现决定，配置 URL 只能描述内置实现：第三方插件可以把请求发到任何
# 地方，而配置里写着 localhost。策略因此先问供应商有没有声明目的地，没声明就按
# 远端处理——判定失败要往隐私这侧倒。

_LOCAL_BASE = "http://127.0.0.1:11434/v1"


class _DeclaresLocal:
    """声明「请求留在本机」的供应商：策略应采信它的声明。"""

    def effective_remote(self, base: str) -> bool:
        return False


class _DeclaresNothing:
    """漏写 return 的声明（返回 None）：不能因此被读成本地。"""

    def effective_remote(self, base: str) -> Any:
        return None


class _DeclaresZero:
    """返回 0 的声明：`0 == False` 但 `0 is not False`，不能因此被读成本地。"""

    def effective_remote(self, base: str) -> Any:
        return 0


class _BrokenDeclaration:
    """声明过程失败：不可信，按远端处理。"""

    def effective_remote(self, base: str) -> bool:
        raise RuntimeError("声明失败")


class _InstanceOnlyRemote(_LegacyProvider):
    """实例级动态 effective_remote（Mock 的常见形状）：类上没声明就不算声明。"""

    def __getattr__(self, name: str) -> Any:
        if name == "effective_remote":
            return lambda base: False
        raise AttributeError(name)


def test_effective_remote_falls_back_to_url_without_provider() -> None:
    """未注册供应商（配置解析、纯函数路径）：URL 是唯一线索。"""
    ai_ports.set_ai(None)
    assert ai_ports.effective_remote(_LOCAL_BASE) is False
    assert ai_ports.effective_remote(_MAIN_BASE) is True


def test_unregistered_declaration_treats_local_url_as_remote() -> None:
    """不声明目的地的供应商不能靠 localhost 免于假名化（回归：按 URL 判定一切）。"""
    _use(_LegacyProvider())
    try:
        assert ai_ports.effective_remote(_LOCAL_BASE) is True
    finally:
        ai_ports.set_ai(None)


def test_declared_local_destination_is_honoured() -> None:
    """供应商声明请求留在本机时采信声明（回归：忽略声明一律按 URL）。"""
    _use(_DeclaresLocal())
    try:
        assert ai_ports.effective_remote(_MAIN_BASE) is False
    finally:
        ai_ports.set_ai(None)


def test_declaration_failure_falls_back_to_remote() -> None:
    """声明抛错按远端（回归：失败降级成本地，把真名交给未知接收方）。"""
    _use(_BrokenDeclaration())
    try:
        assert ai_ports.effective_remote(_LOCAL_BASE) is True
    finally:
        ai_ports.set_ai(None)


def test_instance_only_declaration_is_not_trusted() -> None:
    """按类查声明（回归：按实例查会把 Mock 凭空生成的属性当真实声明）。"""
    _use(_InstanceOnlyRemote())
    try:
        assert ai_ports.effective_remote(_LOCAL_BASE) is True
    finally:
        ai_ports.set_ai(None)


@pytest.mark.parametrize("provider_type", [_DeclaresNothing, _DeclaresZero])
def test_non_boolean_declaration_falls_back_to_remote(provider_type: Any) -> None:
    """声明返回 None／0 这类非布尔值按远端（回归：漏写 return 或 `!= False` 被读成本地）。"""
    _use(provider_type())
    try:
        assert ai_ports.effective_remote(_LOCAL_BASE) is True
    finally:
        ai_ports.set_ai(None)


def test_builtin_provider_declares_url_based_destination() -> None:
    """内置供应商发往配置端点，声明结果与 URL 判定一致（行为不变的基线）。"""
    from briefdesk.plugins.ai_provider.engine import Provider

    provider = Provider()
    assert provider.effective_remote(_LOCAL_BASE) is False
    assert provider.effective_remote(_MAIN_BASE) is True
