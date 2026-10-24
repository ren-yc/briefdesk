"""共享就绪门控（briefdesk.plugins._sdk_base.ReadinessGate）的行为契约。

门控是 weflow/qqflow 两插件身份状态机的唯一实现，这里直接对门控本身钉契约：
- 记忆化语义：良性态（health 报 ready/indexing，或注册应答为受理态）记忆；
  被拒态与网络失败不记忆（下一轮重试，自愈）；503 由 reset() 复位。
- 身份闸门：绑定账号比对抛出的 Mismatch 必须原样冒泡，不能被「明细取不到」
  的宽 except 吞掉；明细失败则回落注册，由上游冲突守卫定夺。
- 并发：锁内双检使并发调用只注册一次；取消释放锁且不留成功标记。
  桩协作者必须真挂起（Event / sleep），否则 gather 不构成竞争，断言
  对「没有锁」也成立——那样的测试没有区分力。
- force 语义：force=True 在第一次 await 前就失效成功标志；随后任何
  失败路径（被拒 / 网络错 / Mismatch / 取消）都不留过期的成功记忆。
- 可观测：版本号只在变化时打一行 INFO；注册前打一行平台化描述
  （由 register_desc_attr 回调供词）；error 阶段先记根因再注册。

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

    def _register_desc(self) -> str:
        # 平台描述回调（门控的注册前 INFO 用），桩返回固定文案
        return "注册账号 <桩>"


def _gate(owner: _Owner, **kwargs: Any) -> ReadinessGate:
    return ReadinessGate(
        owner=owner,
        health_attr="fetch_health",
        accounts_attr="fetch_accounts",
        register_attr="_register",
        log_errors_attr="_log_errors",
        identity_attr="_identity",
        register_desc_attr="_register_desc",
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
    # 桩必须真挂起：没有 await 时 gather 里两个调用顺序执行，「只注册一次」
    # 对无锁实现同样成立，测试就证明不了锁的存在。这里让第一个调用在
    # health 处挂起，直到第二个调用也到达门控——有锁：第二个调用等锁后
    # 命中双检短路（health 1 次、register 1 次）；删掉锁：两个调用各自
    # 走完整流程（health 2 次、register 2 次），本测试必红。
    first_in = asyncio.Event()
    second_arrived = asyncio.Event()

    class _HangOwner(_Owner):
        async def fetch_health(self) -> dict:
            self.health_calls += 1
            if self.health_calls == 1:
                first_in.set()
                await second_arrived.wait()
            else:
                # 无锁路径：第二个调用也进来了，照常返回
                pass
            return {"account": "unregistered"}

    owner = _HangOwner(health={"account": "unregistered"})
    gate = _gate(owner)
    t1 = asyncio.create_task(gate.ensure_ready())
    await asyncio.wait_for(first_in.wait(), timeout=5)
    t2 = asyncio.create_task(gate.ensure_ready())
    # 给第二个任务时间撞上门控（有锁则阻塞在 acquire）
    await asyncio.sleep(0.05)
    second_arrived.set()
    await asyncio.wait_for(asyncio.gather(t1, t2), timeout=5)
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


async def test_version_change_logs_another_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # 版本变了必须再打一行：只按「打过没有」记忆的话，服务端热升级后
    # 日志里仍只有旧版本，排查「下游按新契约调用、上游是哪个二进制」即失效。
    owner = _Owner(health={"account": "ready", "version": "0.7.0"})
    gate = _gate(owner, log=logging.getLogger("tests.sdk_base.ver2"))
    with caplog.at_level(logging.INFO, logger="tests.sdk_base.ver2"):
        await gate.ensure_ready()
        owner._health = {"account": "ready", "version": "0.8.0"}
        gate.reset()
        await gate.ensure_ready()
    version_lines = [r.getMessage() for r in caplog.records if "版本" in r.getMessage()]
    assert len(version_lines) == 2
    assert "0.7.0" in version_lines[0] and "0.8.0" in version_lines[1]
    assert gate.logged_version == "0.8.0"


# ---- force 语义：失效在第一次 await 之前 ----


async def test_force_failure_paths_leave_no_stale_success() -> None:
    # 覆盖四条 force 失败路径：注册被拒 / 注册抛错 / 身份 Mismatch / 取消。
    # 修复前 force 只「绕过」记忆化、不清除：任何一条失败路径都会留下过期的
    # 成功标志，随后的普通调用短路、health 增量为 0——正是这条断言的靶子。
    # 先建立成功缓存（health ready，一次普通调用置位）。
    owner = _Owner(health={"account": "ready"})
    gate = _gate(owner)
    await gate.ensure_ready()
    assert gate.checked is True

    # 1) force + 注册被拒
    owner._health = {"account": "unregistered"}
    owner._register_result = ("invalid_key", None)
    await gate.ensure_ready(force=True)
    assert gate.checked is False
    h_before, r_before = owner.health_calls, owner.register_calls
    await gate.ensure_ready()  # 普通调用必须真正重查（不是短路）
    assert (owner.health_calls, owner.register_calls) != (h_before, r_before)

    # 2) force + 注册抛错
    owner._register_result = ("accepted", "indexing")
    await gate.ensure_ready()
    assert gate.checked is True
    owner._register_result = _Boom("register exploded")
    with pytest.raises(_Boom):
        await gate.ensure_ready(force=True)
    assert gate.checked is False

    # 3) force + 身份 Mismatch（须处于「有绑定」阶段才会走身份闸门）
    gate.checked = True
    owner._health = {"account": "ready"}
    owner._register_result = ("accepted", "indexing")
    owner._identity_raises = _Boom("bound to another")
    with pytest.raises(_Boom):
        await gate.ensure_ready(force=True)
    assert gate.checked is False

    # 4) force + 取消（已有成功缓存时强检被取消——旧用例只测了无缓存场景）
    started = asyncio.Event()

    class _HangOwner(_Owner):
        async def fetch_health(self) -> dict:
            self.health_calls += 1
            started.set()
            await asyncio.sleep(30)
            return {"account": "ready"}

    ho = _HangOwner(health={"account": "ready"})
    hg = _gate(ho)
    hg.checked = True
    task = asyncio.create_task(hg.ensure_ready(force=True))
    await asyncio.wait_for(started.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert hg.checked is False


# ---- 注册前 INFO（平台描述回调）与分诊顺序 ----


async def test_register_preceded_by_platformized_info(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # 「无就绪账号（阶段 X），<平台描述>」必须出现在注册之前；
    # 健康失败路径（根本没走到注册）不得打它。
    logger = logging.getLogger("tests.sdk_base.pre")
    owner = _Owner(health={"account": "unregistered"})
    gate = _gate(owner, log=logger)
    with caplog.at_level(logging.INFO, logger="tests.sdk_base.pre"):
        await gate.ensure_ready()
    messages = [r.getMessage() for r in caplog.records]
    idx = [i for i, m in enumerate(messages) if m.startswith("无就绪账号")]
    assert len(idx) == 1
    assert "阶段 unregistered" in messages[idx[0]]
    assert "注册账号 <桩>" in messages[idx[0]]
    reg = [i for i, m in enumerate(messages) if m.startswith("账号注册:")]
    assert reg and idx[0] < reg[0]

    logger2 = logging.getLogger("tests.sdk_base.pre2")
    boom = _Owner(health=_Boom("health down"))
    g2 = _gate(boom, log=logger2)
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="tests.sdk_base.pre2"), pytest.raises(_Boom):
        await g2.ensure_ready()
    assert not [m for m in caplog.messages if m.startswith("无就绪账号")]


async def test_error_phase_logs_root_cause_before_register_info(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # error 阶段：先记根因，再打注册前 INFO、再注册——顺序即
    # 「先知道为什么错、再宣布要补救」。
    logger = logging.getLogger("tests.sdk_base.err")

    class _ErrOwner(_Owner):
        async def _log_errors(self, accounts: list[dict]) -> None:
            self.error_log_calls += 1
            logger.warning("账号 x 初始化失败: bad key")

    owner = _ErrOwner(
        health={"account": "error"},
        accounts=[{"state": "error", "error": "bad key"}],
    )
    gate = _gate(owner, log=logger)
    with caplog.at_level(logging.DEBUG, logger="tests.sdk_base.err"):
        await gate.ensure_ready()
    messages = [r.getMessage() for r in caplog.records]
    root = [i for i, m in enumerate(messages) if "初始化失败" in m]
    pre = [i for i, m in enumerate(messages) if m.startswith("无就绪账号")]
    assert root and pre and root[0] < pre[0]


# ---- 结果日志按平台有无 status 两种口径 ----


async def test_result_log_omits_absent_status(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # qqflow 面的注册回调不带 status：日志不得多一个恒为 None 的尾巴；
    # weflow 面带 status 时照旧两个都打。
    logger1 = logging.getLogger("tests.sdk_base.res1")
    with_status = _Owner(
        health={"account": "unregistered"},
        register_result=("accepted", "indexing"),
    )
    g1 = _gate(with_status, log=logger1)
    with caplog.at_level(logging.INFO, logger="tests.sdk_base.res1"):
        await g1.ensure_ready()
    line = next(m.getMessage() for m in caplog.records
                 if m.name == "tests.sdk_base.res1"
                 and m.getMessage().startswith("账号注册:"))
    assert "state=accepted" in line and "status=indexing" in line

    logger2 = logging.getLogger("tests.sdk_base.res2")
    no_status = _Owner(
        health={"account": "unregistered"},
        register_result=("accepted", None),
    )
    g2 = _gate(no_status, log=logger2)
    with caplog.at_level(logging.INFO, logger="tests.sdk_base.res2"):
        await g2.ensure_ready()
    line = next(m.getMessage() for m in caplog.records
                 if m.name == "tests.sdk_base.res2"
                 and m.getMessage().startswith("账号注册:"))
    assert "state=accepted" in line and "status=" not in line
