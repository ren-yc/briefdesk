"""共享就绪门控（briefdesk.plugins._sdk_base.ReadinessGate）的行为契约。

门控是 weflow/qqflow 两插件身份状态机的唯一实现，这里直接对门控本身钉契约：
- 记忆化语义：良性态（health 报 ready/indexing，或注册应答为受理态）记忆；
  被拒态与网络失败不记忆（下一轮重试，自愈）；503 由 reset() 复位。
- 身份闸门：绑定账号比对抛出的 Mismatch 必须原样冒泡，不能被「明细取不到」
  的宽 except 吞掉；明细失败则回落注册，由上游冲突守卫定夺。
- 并发：锁内双检使并发调用只注册一次；取消释放锁且不留成功标记。
- 可观测：版本号只在变化时打一行 INFO。

全部用桩协作者注入（门控按属性名解析），不触碰网络与文件系统。
"""

import asyncio
import logging
from typing import Any

import pytest

from briefdesk.plugins._sdk_base import ReadinessGate


class _Boom(Exception):
    """桩异常：代表身份不符等领域错误。"""


class _Owner:
    """桩 owner：门控按属性名解析协作者，并记录调用次数供断言。"""

    def __init__(
        self,
        *,
        health: dict | Exception,
        accounts: list[dict] | Exception | None = None,
        register_result: tuple[str, str | None] | Exception = ("accepted", "indexing"),
        identity_raises: Exception | None = None,
    ) -> None:
        self.health_calls = 0
        self.accounts_calls = 0
        self.register_calls = 0
        self.error_log_calls = 0
        self._health = health
        self._accounts = accounts if accounts is not None else []
        self._register_result = register_result
        self._identity_raises = identity_raises

    async def fetch_health(self) -> dict:
        self.health_calls += 1
        if isinstance(self._health, Exception):
            raise self._health
        return dict(self._health)

    async def fetch_accounts(self) -> list[dict]:
        self.accounts_calls += 1
        if isinstance(self._accounts, Exception):
            raise self._accounts
        return list(self._accounts)

    async def _register(self) -> tuple[str, str | None]:
        self.register_calls += 1
        if isinstance(self._register_result, Exception):
            raise self._register_result
        return self._register_result

    async def _log_errors(self, accounts: list[dict]) -> None:
        self.error_log_calls += 1

    def _identity(self, accounts: list[dict]) -> bool:
        if self._identity_raises is not None:
            raise self._identity_raises
        return True


def _gate(owner: _Owner, **kwargs: Any) -> ReadinessGate:
    return ReadinessGate(
        owner=owner,
        health_attr="fetch_health",
        accounts_attr="fetch_accounts",
        register_attr="_register",
        log_errors_attr="_log_errors",
        identity_attr="_identity",
        **kwargs,
    )


async def test_own_ready_phase_skips_registration_and_memoizes() -> None:
    owner = _Owner(health={"account": "ready"})
    gate = _gate(owner)
    await gate.ensure_ready()
    await gate.ensure_ready()
    assert owner.health_calls == 1
    assert owner.register_calls == 0
    assert gate.checked is True


async def test_own_indexing_phase_skips_registration() -> None:
    owner = _Owner(health={"account": "indexing"})
    gate = _gate(owner)
    await gate.ensure_ready()
    assert owner.register_calls == 0
    assert gate.checked is True


async def test_zero_account_registers_and_indexing_status_is_benign() -> None:
    owner = _Owner(health={"account": "unregistered"})
    gate = _gate(owner)
    await gate.ensure_ready()
    assert owner.register_calls == 1
    assert gate.checked is True


async def test_rejected_register_state_is_not_memoized() -> None:
    owner = _Owner(health={"account": "unregistered"}, register_result=("invalid_key", None))
    gate = _gate(owner)
    await gate.ensure_ready()
    assert gate.checked is False
    await gate.ensure_ready()
    assert owner.register_calls == 2


async def test_health_failure_propagates_and_is_not_memoized() -> None:
    owner = _Owner(health=_Boom("health down"))
    gate = _gate(owner)
    with pytest.raises(_Boom):
        await gate.ensure_ready()
    assert gate.checked is False


async def test_identity_mismatch_propagates_instead_of_falling_back() -> None:
    owner = _Owner(
        health={"account": "ready"},
        accounts=[{"wxid": "other", "state": "ready"}],
        identity_raises=_Boom("bound to another account"),
    )
    gate = _gate(owner)
    with pytest.raises(_Boom):
        await gate.ensure_ready()
    assert owner.register_calls == 0
    assert gate.checked is False


async def test_accounts_unavailable_falls_through_to_register() -> None:
    owner = _Owner(health={"account": "ready"}, accounts=_Boom("401"))
    gate = _gate(owner)
    await gate.ensure_ready()
    assert owner.register_calls == 1


async def test_error_phase_logs_root_cause_before_registering() -> None:
    owner = _Owner(health={"account": "error"}, accounts=[{"state": "error"}])
    gate = _gate(owner)
    await gate.ensure_ready()
    assert owner.error_log_calls == 1
    assert owner.register_calls == 1


async def test_force_bypasses_memoization() -> None:
    owner = _Owner(health={"account": "ready"})
    gate = _gate(owner)
    await gate.ensure_ready()
    await gate.ensure_ready(force=True)
    assert owner.health_calls == 2


async def test_reset_clears_memoization() -> None:
    owner = _Owner(health={"account": "ready"})
    gate = _gate(owner)
    await gate.ensure_ready()
    gate.reset()
    assert gate.checked is False
    await gate.ensure_ready()
    assert owner.health_calls == 2


async def test_checked_flag_is_settable_for_white_box_callers() -> None:
    owner = _Owner(health={"account": "ready"})
    gate = _gate(owner)
    gate.checked = True
    await gate.ensure_ready()
    assert owner.health_calls == 0


async def test_concurrent_calls_register_once() -> None:
    owner = _Owner(health={"account": "unregistered"})
    gate = _gate(owner)
    await asyncio.gather(gate.ensure_ready(), gate.ensure_ready())
    assert owner.health_calls == 1
    assert owner.register_calls == 1


async def test_cancellation_releases_lock_without_leaving_success_flag() -> None:
    started = asyncio.Event()

    class _SlowOwner(_Owner):
        async def fetch_health(self) -> dict:
            self.health_calls += 1
            if self.health_calls == 1:
                started.set()
                await asyncio.sleep(30)
            return {"account": "ready"}

    owner = _SlowOwner(health={"account": "ready"})
    gate = _gate(owner)
    task = asyncio.create_task(gate.ensure_ready())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert gate.checked is False
    # 取消必须释放锁：后续调用不得死锁，且仍能正常完成
    await asyncio.wait_for(gate.ensure_ready(), timeout=5)
    assert gate.checked is True


async def test_version_logged_once_per_change(caplog: pytest.LogCaptureFixture) -> None:
    owner = _Owner(health={"account": "ready", "version": "0.7.0"})
    gate = _gate(owner, log=logging.getLogger("tests.sdk_base"), version_label="weflow-server")
    with caplog.at_level(logging.INFO, logger="tests.sdk_base"):
        await gate.ensure_ready()
        gate.reset()
        await gate.ensure_ready()
    version_lines = [r for r in caplog.records if "版本" in r.getMessage()]
    assert len(version_lines) == 1
    assert "weflow-server" in version_lines[0].getMessage()
    assert gate.logged_version == "0.7.0"
