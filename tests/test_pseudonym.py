"""briefdesk/pseudonym.py 的单元测试：身份键、落盘、还原与失败红线。

全部用例用 `tmp_path` 隔离注册表，绝不触碰真实用户数据目录；样本一律虚构
（人名、群名、ID 都是示例值）。
"""

from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import hmac
import json
import os
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

from briefdesk import pseudonym, telemetry
from briefdesk.config import config
from briefdesk.masking import clean_display_name, mask_content


@pytest.fixture(autouse=True)
def _telemetry_isolation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """遥测与落盘都隔离到临时目录，且固定为开启（断言都基于「确实记了」）。"""
    telemetry.reset_for_tests()
    monkeypatch.setattr(telemetry, "telemetry_dir", lambda: tmp_path / "tel")
    monkeypatch.setattr(config, "telemetry_enabled", True)
    yield
    telemetry.reset_for_tests()


def _counters() -> dict[str, int]:
    return telemetry.snapshot()["counters"]


def _make(base: Path, **kwargs: object) -> pseudonym.Pseudonymizer:
    """构造隔离实例；默认把合并窗口设为 0 以免用例互相等窗口。"""
    kwargs.setdefault("flush_window", 0.0)
    return pseudonym.Pseudonymizer(base, **kwargs)  # type: ignore[arg-type]


async def _person(
    pz: pseudonym.Pseudonymizer,
    *,
    sender_id: str = "",
    session_id: str = "会话-0001",
    name: str = "示例甲",
    source: str = "weflow",
) -> str:
    ctx = pseudonym.NameContext(pz)
    return await ctx.person_alias(
        source=source,
        sender_id=sender_id,
        session_id=session_id,
        display_name=name,
    )


async def _group(
    pz: pseudonym.Pseudonymizer,
    *,
    session_id: str = "会话-0001",
    name: str = "示例群",
    source: str = "weflow",
) -> str:
    ctx = pseudonym.NameContext(pz)
    return await ctx.group_alias(
        source=source, session_id=session_id, group_name=name
    )


def _collision_digest(salt: bytes, kind: str, identity_key: str) -> str:
    """强制短码碰撞的摘要：前 4 字节固定 → 前 6 位 base32 一定相同。

    形态必须与注册表键校验一致（64 位小写十六进制）：长度不对的摘要写进注册表
    后会让下次启动直接读不进来。
    """
    tail = hashlib.sha256(f"{kind}:{identity_key}".encode()).hexdigest()
    digest = "00000000" + tail[:56]
    assert len(digest) == 64
    return digest


def _digest_for_code(code: str) -> str:
    """构造前 6 位 base32 恰好等于 `code` 的合法摘要（64 位小写十六进制）。

    短码取 base32 编码的前 6 个字符（30 bit = 前 3 字节 + 第 4 字节的高 6 位），
    所以把这 30 bit 写进前 4 字节、低位补 0，其余字节固定为 0，摘要形态仍然合法。
    """
    assert len(code) == pseudonym.ALIAS_CODE_LENGTH
    bits = (
        "".join(f"{pseudonym._BASE32_CHARS.index(char):05b}" for char in code)
        + "00"
    )
    head = bytes(int(bits[index : index + 8], 2) for index in range(0, 32, 8))
    digest = head.hex() + "00" * 28
    assert len(digest) == 64
    assert base64.b32encode(bytes.fromhex(digest)).decode("ascii")[:6] == code
    return digest


def _forced_digest(code: str) -> Callable[[bytes, str, str], str]:
    """摘要注入：所有身份的短码都落到 code，但摘要本身按身份区分。

    短码相同、摘要不同才是「短码碰撞」的真实形态：摘要相同会被缓存当成同一个
    身份，根本走不到分配后缀那条路径。
    """
    head = _digest_for_code(code)[:8]

    def _digest_fn(salt: bytes, kind: str, identity_key: str) -> str:
        tail = hashlib.sha256(f"{kind}:{identity_key}".encode()).hexdigest()
        digest = head + tail[:56]
        assert base64.b32encode(bytes.fromhex(digest)).decode("ascii")[:6] == code
        return digest

    return _digest_fn


def _registry_payload(base: Path) -> dict[str, object]:
    raw = json.loads((base / pseudonym.REGISTRY_FILE_NAME).read_text("utf-8"))
    assert isinstance(raw, dict)
    return raw


def _aliases(base: Path) -> dict[str, object]:
    aliases = _registry_payload(base)["aliases"]
    assert isinstance(aliases, dict)
    return aliases


# ── 身份键 ──


def test_identity_keys_are_domain_separated() -> None:
    a = pseudonym.person_identity_key(
        source="weflow",
        sender_id="member-0001",
        session_id="会话-0001",
        display_name="示例甲",
    )
    b = pseudonym.person_identity_key(
        source="weflow",
        sender_id="member-0001",
        session_id="会话-0002",
        display_name="示例甲-改名",
    )
    assert a == b  # 有 ID 时会话与显示名都不参与身份
    g = pseudonym.group_identity_key(
        source="weflow", session_id="会话-0001", group_name="示例群"
    )
    assert g != a  # 人/群分域

    no_id_1 = pseudonym.person_identity_key(
        source="weflow",
        sender_id="",
        session_id="会话-0001",
        display_name="示例甲",
    )
    no_id_2 = pseudonym.person_identity_key(
        source="weflow",
        sender_id="",
        session_id="会话-0002",
        display_name="示例甲",
    )
    assert no_id_1 != no_id_2  # 无 ID 的承诺止于同源同会话


async def test_id_identity_ignores_session_and_name(tmp_path: Path) -> None:
    pz = _make(tmp_path / "reg")
    first = await _person(pz, sender_id="member-0001", session_id="会话-0001")
    same_person = await _person(
        pz, sender_id="member-0001", session_id="会话-9999", name="示例甲-改名"
    )
    assert same_person == first
    assert first.startswith(pseudonym.ALIAS_PERSON_PREFIX)
    assert len(first) == len(pseudonym.ALIAS_PERSON_PREFIX) + 6
    # 不同数据源的 ID 空间不混用
    other_source = await _person(
        pz, sender_id="member-0001", session_id="会话-0001", source="qqflow"
    )
    assert other_source != first


async def test_no_id_identity_is_best_effort_per_session(tmp_path: Path) -> None:
    pz = _make(tmp_path / "reg")
    same_session = [
        await _person(pz, session_id="会话-0001", name="示例甲") for _ in range(2)
    ]
    assert same_session[0] == same_session[1]
    other_session = await _person(pz, session_id="会话-0002", name="示例甲")
    assert other_session != same_session[0]


async def test_group_identity_preserves_same_name_groups(tmp_path: Path) -> None:
    pz = _make(tmp_path / "reg")
    group_a = await _group(pz, session_id="会话-0001", name="示例群")
    group_b = await _group(pz, session_id="会话-0002", name="示例群")
    assert group_a != group_b  # 同名不同群不合并
    assert group_a.startswith(pseudonym.ALIAS_GROUP_PREFIX)
    renamed = await _group(pz, session_id="会话-0001", name="示例群-改名")
    assert renamed == group_a  # 改群名不换别名


# ── 落盘 ──


async def test_salt_and_alias_persist_without_plaintext(tmp_path: Path) -> None:
    base = tmp_path / "reg"
    pz = _make(base)
    person = await _person(
        pz, sender_id="成员-0001", session_id="会话-0001", name="示例甲"
    )
    group = await _group(pz, session_id="会话-0001", name="示例群")

    payload = _registry_payload(base)
    assert payload["version"] == pseudonym.REGISTRY_VERSION
    salt_value = payload["salt"]
    assert isinstance(salt_value, str)
    salt = base64.b64decode(salt_value, validate=True)
    assert len(salt) == 32
    aliases = _aliases(base)
    assert set(aliases) and all(
        len(digest) == 64 and all(c in "0123456789abcdef" for c in digest)
        for digest in aliases
    )
    assert {person, group} <= set(aliases.values())

    raw = (base / pseudonym.REGISTRY_FILE_NAME).read_text("utf-8")
    # 注册表里只有盐与哈希：明文姓名/群名/ID/会话号一个都不能有
    for plaintext in ("示例甲", "示例群", "成员-0001", "会话-0001"):
        assert plaintext not in raw

    # 重启（新实例）后同一身份仍是同一别名，盐不重新生成
    restarted = _make(base)
    assert (
        await _person(
            restarted, sender_id="成员-0001", session_id="会话-0002", name="示例甲"
        )
        == person
    )
    assert await _group(restarted, session_id="会话-0001", name="示例群") == group
    assert _registry_payload(base)["salt"] == salt_value


async def test_forced_collision_suffix_survives_restart(tmp_path: Path) -> None:
    base = tmp_path / "reg"
    pz = _make(base, digest_fn=_collision_digest)
    first = await _person(pz, sender_id="member-0001", name="示例甲")
    second = await _person(pz, sender_id="member-0002", name="示例乙")
    assert second == f"{first}-1"  # 后来者拿稳定后缀，旧别名不动
    assert first.startswith(pseudonym.ALIAS_PERSON_PREFIX)

    restarted = _make(base, digest_fn=_collision_digest)
    assert await _person(restarted, sender_id="member-0001", name="示例甲") == first
    assert await _person(restarted, sender_id="member-0002", name="示例乙") == second
    third = await _person(restarted, sender_id="member-0003", name="示例丙")
    assert third == f"{first}-2"


async def test_batched_flush_commits_before_first_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path / "reg"
    pz = pseudonym.Pseudonymizer(base, flush_window=0.3)

    # 首次初始化立即提交：不等合并窗口，别名返回时已经在盘上
    started = time.monotonic()
    first = await _person(pz, sender_id="member-0001", name="示例甲")
    assert time.monotonic() - started < 0.3
    assert pz.commit_count == 1
    assert first in _aliases(base).values()

    # 已提交别名命中零 I/O：不加锁、不再落盘
    def _forbidden_lock(*args: object, **kwargs: object) -> None:
        raise AssertionError("已提交别名不应再取注册锁")

    with monkeypatch.context() as patched:
        patched.setattr(pseudonym, "_registry_lock", _forbidden_lock)
        assert await _person(pz, sender_id="member-0001", name="示例甲") == first

    # 同一合并窗口内的新身份并进一次落盘；返回时都已在盘上
    before = pz.commit_count
    pair = await asyncio.gather(
        _person(pz, sender_id="member-0002", name="示例乙"),
        _person(pz, sender_id="member-0003", name="示例丙"),
    )
    assert pz.commit_count == before + 1
    aliases = _aliases(base)
    assert set(pair) <= set(aliases.values())
    assert first not in pair


async def test_concurrent_registrars_never_duplicate_aliases(tmp_path: Path) -> None:
    base = tmp_path / "reg"
    pz = pseudonym.Pseudonymizer(base, flush_window=0.01)
    identities = [f"member-{index:04d}" for index in range(12)]
    aliases = await asyncio.gather(
        *(
            _person(pz, sender_id=identity, name=f"示例{index}")
            for index, identity in enumerate(identities)
        )
    )
    assert len(set(aliases)) == len(aliases)
    stored = _aliases(base)
    assert len(stored) == len(aliases)
    assert set(aliases) <= set(stored.values())

    restarted = pseudonym.Pseudonymizer(base, flush_window=0.01)
    again = [
        await _person(restarted, sender_id=identity, name=f"示例{index}")
        for index, identity in enumerate(identities)
    ]
    assert again == aliases


def test_survives_event_loop_rebuild(tmp_path: Path) -> None:
    """同一实例跨事件循环复用（客户端缓存被清、进程内换 loop）仍能提交。"""
    pz = _make(tmp_path / "reg")
    first = asyncio.run(_person(pz, sender_id="member-0001", name="示例甲"))
    second = asyncio.run(_person(pz, sender_id="member-0002", name="示例乙"))
    assert first != second
    assert {first, second} <= set(_aliases(tmp_path / "reg").values())


# ── 初始化：写线程、单次建表与失败传播 ──


def test_fixed_salt_golden_vector() -> None:
    """固定盐的独立金向量：摘要与别名由标准库单独算一遍，不复用实现内部函数。"""
    salt = bytes(range(32))
    identity_key = '["person","weflow","sender_id","member-0001"]'
    assert (
        pseudonym.person_identity_key(
            source="weflow",
            sender_id="member-0001",
            session_id="会话-0001",
            display_name="示例甲",
        )
        == identity_key
    )

    payload = json.dumps(
        [1, "person", identity_key],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    expected_digest = hmac.new(salt, payload, hashlib.sha256).hexdigest()
    # 钉住字面量：实现与测试同时改错时，独立计算那条断言才会失效
    assert expected_digest == (
        "35b91c565ae9935fce4ce960a7a63f10eb77df2e0fa5a6b586cbeaf66e277d7d"
    )
    assert pseudonym._default_digest(salt, "person", identity_key) == expected_digest

    expected_code = base64.b32encode(bytes.fromhex(expected_digest)).decode("ascii")[:6]
    assert expected_code == "GW4RYV"
    assert pseudonym._short_code(expected_digest) == expected_code
    assert pseudonym.ALIAS_PERSON_PREFIX + expected_code == "同学GW4RYV"


async def test_fixed_salt_golden_vector_through_public_alias_for(
    tmp_path: Path,
) -> None:
    """固定盐走公开 `alias_for`：返回的别名与落盘 digest 都要对上金向量。"""
    base = tmp_path / "reg"
    base.mkdir(parents=True)
    salt = bytes(range(32))
    payload = {
        "version": pseudonym.REGISTRY_VERSION,
        "salt": base64.b64encode(salt).decode("ascii"),
        "aliases": {},
    }
    (base / pseudonym.REGISTRY_FILE_NAME).write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    identity_key = '["person","weflow","sender_id","member-0001"]'
    pz = _make(base)
    alias = await pz.alias_for(pseudonym.KIND_PERSON, identity_key, "示例甲")
    assert alias == "同学GW4RYV"
    assert _aliases(base) == {
        "35b91c565ae9935fce4ce960a7a63f10eb77df2e0fa5a6b586cbeaf66e277d7d": alias
    }


async def test_first_load_runs_off_the_event_loop_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """注册表加载必须在写线程里跑：同步读盘 + 抢锁会把事件循环整个卡住。"""
    seen: list[bool] = []
    original_load = pseudonym.Pseudonymizer._load

    def _spy(self: pseudonym.Pseudonymizer) -> pseudonym._Snapshot:
        seen.append(threading.current_thread() is threading.main_thread())
        return original_load(self)

    monkeypatch.setattr(pseudonym.Pseudonymizer, "_load", _spy)
    pz = _make(tmp_path / "reg")
    assert await _person(pz, sender_id="member-0001", name="示例甲")
    assert seen and seen[0] is False


async def test_event_loop_progresses_while_load_is_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """写线程卡在加载里时事件循环仍能推进：另起协程能在阻塞窗口内跑完。

    「加载在别的线程」只证明了调用点不在主线程；这里把加载钉在屏障上，再看
    另一个协程能否完成——否则整条出境管道仍会被一次首启加载拖停。
    """
    base = tmp_path / "reg"
    started = threading.Event()
    release = threading.Event()
    original_load = pseudonym.Pseudonymizer._load

    def _blocking_load(self: pseudonym.Pseudonymizer) -> pseudonym._Snapshot:
        started.set()
        assert release.wait(10.0), "写线程等待释放超时"
        return original_load(self)

    async def _side_work() -> str:
        return "ok"

    pz = _make(base)
    with monkeypatch.context() as patched:
        patched.setattr(pseudonym.Pseudonymizer, "_load", _blocking_load)
        pending = asyncio.ensure_future(
            _person(pz, sender_id="member-0001", name="示例甲")
        )
        try:
            assert await asyncio.to_thread(started.wait, 10.0)
            # 阻塞窗口内另起协程：它能跑完，说明事件循环没被写线程拖住
            assert await asyncio.wait_for(_side_work(), timeout=1.0) == "ok"
            assert not pending.done()
            assert pz._registry is None
        finally:
            release.set()
        alias = await pending
    assert alias.startswith(pseudonym.ALIAS_PERSON_PREFIX)
    assert alias in _aliases(base).values()


async def test_concurrent_first_start_creates_table_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """并发首启只建一次表：各建各的盐会互相覆盖，先提交的那批别名全部失效。"""
    salt_draws: list[int] = []
    real_urandom = os.urandom

    def _counting_urandom(size: int) -> bytes:
        if size == pseudonym._SALT_SIZE:
            salt_draws.append(size)
        return real_urandom(size)

    load_calls: list[int] = []
    original_load = pseudonym.Pseudonymizer._load

    def _spy_load(self: pseudonym.Pseudonymizer) -> pseudonym._Snapshot:
        # 只数「真的要读一次」的调用：已加载后 `_commit_sync` 里的调用是纯缓存命中
        if self._registry is None:
            load_calls.append(1)
        return original_load(self)

    monkeypatch.setattr(pseudonym.os, "urandom", _counting_urandom)
    monkeypatch.setattr(pseudonym.Pseudonymizer, "_load", _spy_load)
    base = tmp_path / "reg"
    pz = _make(base)
    aliases = await asyncio.gather(
        *(
            _person(pz, sender_id=f"member-{index:04d}", name=f"示例{index}")
            for index in range(8)
        )
    )
    # 共享初始化 future：8 个并发首启合并成一次加载、一次建表
    assert len(load_calls) == 1
    assert len(salt_draws) == 1
    assert len(set(aliases)) == 8
    assert set(aliases) <= set(_aliases(base).values())


async def test_initialization_failure_reaches_every_waiter(tmp_path: Path) -> None:
    """初始化失败时所有等待者都拿到异常，且实例里不留半成品。"""
    base = tmp_path / "reg"
    base.mkdir(parents=True)
    # 注册表位置是目录：读取必然失败（不是「文件不存在」，不能建表覆盖）
    broken = base / pseudonym.REGISTRY_FILE_NAME
    broken.mkdir()

    pz = _make(base)
    results = await asyncio.gather(
        *(
            _person(pz, sender_id=f"member-{index:04d}", name="示例甲")
            for index in range(5)
        ),
        return_exceptions=True,
    )
    assert len(results) == 5
    assert all(
        isinstance(item, pseudonym.PseudonymRegistryError) for item in results
    )
    assert pz.commit_count == 0
    # 失败后两个字段都为空：没有「有注册表、空缓存」的半成品可供后续请求误用
    assert pz._registry is None
    assert pz._committed == {}
    # 失败不被缓存：下一次调用仍重新尝试并同样失败
    with pytest.raises(pseudonym.PseudonymRegistryError):
        await _person(pz, sender_id="member-0001", name="示例甲")
    assert pz._registry is None and pz._committed == {}

    # 修复条件后同一实例能成功重试（失败没有被钉死在实例上）
    broken.rmdir()
    recovered = await _person(pz, sender_id="member-0001", name="示例甲")
    assert recovered.startswith(pseudonym.ALIAS_PERSON_PREFIX)
    assert recovered in _aliases(base).values()
    assert pz.commit_count == 1


async def test_waiter_sees_no_half_built_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """写线程停在「已读盘、未发布」时，并发请求继续等同一个 future。

    只测「同时发起若干请求」证明不了这一点：如果发布分成两步（先注册表后缓存），
    晚到的请求会在缓存还空着时就返回，把已知身份重新分配一遍。这里用线程屏障把
    写线程卡在发布之前，直接看晚到请求的状态。
    """
    base = tmp_path / "reg"
    seed = _make(base)
    known_a = await _person(seed, sender_id="member-0001", name="示例甲")
    known_b = await _person(seed, sender_id="member-0002", name="示例乙")

    read_done = threading.Event()
    release = threading.Event()
    original_load = pseudonym.Pseudonymizer._load

    def _stalled_load(self: pseudonym.Pseudonymizer) -> pseudonym._Snapshot:
        snapshot = original_load(self)  # 已读盘并构造完整快照，但还没发布
        read_done.set()
        assert release.wait(10.0), "写线程等待释放超时"
        return snapshot

    pz = _make(base)
    with monkeypatch.context() as patched:
        patched.setattr(pseudonym.Pseudonymizer, "_load", _stalled_load)
        first = asyncio.ensure_future(
            _person(pz, sender_id="member-0001", name="示例甲")
        )
        try:
            assert await asyncio.to_thread(read_done.wait, 10.0)
            # 未发布：两个字段都没有值，晚到请求不能据此提前返回
            assert pz._registry is None
            assert pz._committed == {}
            late = asyncio.ensure_future(
                _person(pz, sender_id="member-0003", name="示例丙")
            )
            await asyncio.sleep(0.05)
            assert not first.done() and not late.done()
            assert pz._registry is None
        finally:
            release.set()
        first_alias, late_alias = await asyncio.gather(first, late)

    # 发布后拿到的是完整缓存：盘上既有映射与新别名都在
    assert first_alias == known_a
    assert known_b in pz._committed.values()
    assert set(pz._committed.values()) == {known_a, known_b, late_alias}
    # 已知身份走缓存（只有新身份那次落盘），证明缓存不是空的
    assert pz.commit_count == 1
    assert late_alias in _aliases(base).values()


async def test_cache_fill_failure_publishes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """填缓存阶段抛错：两个字段都不变，后续调用仍抛错而不是拿到半张表。"""
    base = tmp_path / "reg"
    seed = _make(base)
    known = await _person(seed, sender_id="member-0001", name="示例甲")

    def _failing_build(
        self: pseudonym.Pseudonymizer, registry: pseudonym._Registry
    ) -> pseudonym._Snapshot:
        raise pseudonym.PseudonymRegistryError("模拟填缓存失败")

    pz = _make(base)
    with monkeypatch.context() as patched:
        patched.setattr(pseudonym.Pseudonymizer, "_build_snapshot", _failing_build)
        with pytest.raises(pseudonym.PseudonymRegistryError):
            await _person(pz, sender_id="member-0001", name="示例甲")
        assert pz._registry is None
        assert pz._committed == {}
        # 失败不被缓存：再调一次仍然抛错，而不是拿空注册表算出新别名
        with pytest.raises(pseudonym.PseudonymRegistryError):
            await _person(pz, sender_id="member-0001", name="示例甲")
        assert pz._registry is None and pz._committed == {}

    # 条件恢复后同一实例能加载出完整缓存（已知身份仍是原别名、且不重新落盘）
    assert await _person(pz, sender_id="member-0001", name="示例甲") == known
    assert pz.commit_count == 0


async def test_loaded_registry_serves_known_identities_from_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """重启后盘上已有映射进内存缓存：已知身份命中纯内存，不读盘、不加锁。"""
    base = tmp_path / "reg"
    pz = _make(base)
    first = await _person(pz, sender_id="member-0001", name="示例甲")

    restarted = _make(base)
    # 先让新实例加载注册表（顺带登记一个陌生身份），再验证旧身份零 I/O
    await _person(restarted, sender_id="member-0009", name="示例壬")

    def _forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("已注册身份不应再读盘、加锁或写盘")

    with monkeypatch.context() as patched:
        patched.setattr(pseudonym, "_read_registry", _forbidden)
        patched.setattr(pseudonym, "_registry_lock", _forbidden)
        patched.setattr(pseudonym, "_atomic_write", _forbidden)
        assert (
            await _person(restarted, sender_id="member-0001", name="示例甲") == first
        )


async def test_first_request_for_known_identity_reads_disk_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """新实例首次请求已知身份：只读一次盘，不取锁、不写盘。"""
    base = tmp_path / "reg"
    seed = _make(base)
    known = await _person(seed, sender_id="member-0001", name="示例甲")

    reads: list[Path] = []
    original_read = pseudonym._read_registry

    def _spy_read(path: Path) -> pseudonym._Registry:
        reads.append(path)
        return original_read(path)

    def _forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("已知身份首次加载不应取锁或写盘")

    restarted = _make(base)
    with monkeypatch.context() as patched:
        patched.setattr(pseudonym, "_read_registry", _spy_read)
        patched.setattr(pseudonym, "_registry_lock", _forbidden)
        patched.setattr(pseudonym, "_atomic_write", _forbidden)
        assert (
            await _person(restarted, sender_id="member-0001", name="示例甲") == known
        )
    # 一次加载 → 一次读盘；缓存填好后不再落盘
    assert reads == [base / pseudonym.REGISTRY_FILE_NAME]
    assert restarted.commit_count == 0


# ── 失败红线 ──


async def test_replace_failure_keeps_old_map_and_prevents_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path / "reg"
    pz = _make(base)
    first = await _person(pz, sender_id="member-0001", name="示例甲")
    registry_path = base / pseudonym.REGISTRY_FILE_NAME
    before_bytes = registry_path.read_bytes()

    def _fail_replace(tmp: str, path: Path) -> None:
        raise OSError("模拟替换失败")

    with monkeypatch.context() as patched:
        patched.setattr(pseudonym, "_replace_file", _fail_replace)
        with pytest.raises(pseudonym.PseudonymWriteError):
            await _person(pz, sender_id="member-0002", name="示例乙")
        assert pz._durable is False
        # 旧表原样保留、没留下临时文件、旧别名照常可用
        assert registry_path.read_bytes() == before_bytes
        assert not list(base.glob("*.tmp"))
        assert await _person(pz, sender_id="member-0001", name="示例甲") == first

    # 写路径恢复后可以继续登记，且不污染已提交的旧别名
    second = await _person(pz, sender_id="member-0002", name="示例乙")
    assert pz._durable is True
    assert second != first
    assert {first, second} <= set(_aliases(base).values())


def test_atomic_write_directory_fsync_failure_is_write_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """替换后目录同步失败也必须阻断出站，不能把已可见的新文件当成持久提交。"""
    registry_path = tmp_path / pseudonym.REGISTRY_FILE_NAME
    registry_path.write_bytes(b"old-registry")
    payload = b"new-registry"
    syncs: list[int] = []
    directory_fds: list[int] = []
    closed: list[int] = []
    original_open = os.open
    original_fsync = os.fsync
    original_close = os.close

    def _open(path_arg, flags, *args, **kwargs):
        if os.fspath(path_arg) == os.fspath(registry_path.parent):
            assert flags == os.O_RDONLY
            assert registry_path.read_bytes() == payload
            assert len(syncs) == 1
            # Windows 不能按 POSIX 方式打开目录，用已替换文件的句柄模拟目录句柄。
            descriptor = original_open(registry_path, flags)
            directory_fds.append(descriptor)
            return descriptor
        return original_open(path_arg, flags, *args, **kwargs)

    def _sync(descriptor: int) -> None:
        syncs.append(descriptor)
        if len(syncs) == 2:
            raise OSError("模拟目录同步失败")
        original_fsync(descriptor)

    def _close(descriptor: int) -> None:
        closed.append(descriptor)
        original_close(descriptor)

    with monkeypatch.context() as patched:
        patched.setattr(pseudonym.sys, "platform", "linux")
        patched.setattr(pseudonym.os, "open", _open)
        patched.setattr(pseudonym.os, "fsync", _sync)
        patched.setattr(pseudonym.os, "close", _close)
        with pytest.raises(pseudonym.PseudonymWriteError):
            pseudonym._atomic_write(registry_path, payload)

    assert len(syncs) == 2
    assert len(directory_fds) == 1
    assert closed == directory_fds
    assert registry_path.read_bytes() == payload
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize(
    "retry_fails", [False, True], ids=["retry_succeeds", "retry_fails_closed"]
)
async def test_directory_fsync_failure_retry_rewrites_before_publishing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, retry_fails: bool
) -> None:
    """杀死「重试只认盘上摘要、跳过写盘」：同步成功前不得发布别名。

    用文件句柄模拟 POSIX 目录句柄，让 Windows 也能验证替换后的同步故障。
    """
    base = tmp_path / "reg"
    pz = _make(base)
    await pz._ensure_registry()
    registry_path = pz.registry_file
    events: list[str] = []
    directory_fds: set[int] = set()
    original_write = pseudonym._atomic_write
    original_replace = pseudonym._replace_file
    original_open = os.open
    original_fsync = os.fsync
    original_close = os.close

    def _write(path: Path, payload: bytes) -> None:
        events.append("write")
        with monkeypatch.context() as patched:
            patched.setattr(pseudonym, "sys", SimpleNamespace(platform="linux"))
            original_write(path, payload)

    def _replace(tmp: str, path: Path) -> None:
        original_replace(tmp, path)
        events.append("replace")

    def _open(path_arg, flags, *args, **kwargs):
        if os.fspath(path_arg) == os.fspath(base):
            assert flags == os.O_RDONLY
            descriptor = original_open(registry_path, flags)
            directory_fds.add(descriptor)
            return descriptor
        return original_open(path_arg, flags, *args, **kwargs)

    def _sync(descriptor: int) -> None:
        if descriptor in directory_fds:
            if events.count("write") == 1 or retry_fails:
                events.append("directory_fsync_failed")
                raise OSError("模拟目录同步失败")
            events.append("directory_fsynced")
            return
        original_fsync(descriptor)
        events.append("file_fsynced")

    def _close(descriptor: int) -> None:
        directory_fds.discard(descriptor)
        original_close(descriptor)

    failed_write = ["write", "file_fsynced", "replace", "directory_fsync_failed"]
    with monkeypatch.context() as patched:
        patched.setattr(pseudonym, "_atomic_write", _write)
        patched.setattr(pseudonym, "_replace_file", _replace)
        patched.setattr(pseudonym.os, "open", _open)
        patched.setattr(pseudonym.os, "fsync", _sync)
        patched.setattr(pseudonym.os, "close", _close)
        with pytest.raises(pseudonym.PseudonymWriteError):
            await _person(pz, sender_id="member-0001", name="示例甲")
        assert events == failed_write
        assert pz._durable is False
        assert pz._committed == {}
        visible_bytes = registry_path.read_bytes()
        visible_aliases = _aliases(base)
        assert len(visible_aliases) == 1
        visible_alias = next(iter(visible_aliases.values()))

        if retry_fails:
            with pytest.raises(pseudonym.PseudonymWriteError):
                await _person(pz, sender_id="member-0001", name="示例甲")
            assert events == failed_write * 2
            assert pz._durable is False
            assert pz._committed == {}
        else:
            alias = await _person(pz, sender_id="member-0001", name="示例甲")
            events.append("alias_returned")
            assert alias == visible_alias
            assert events == failed_write + [
                "write",
                "file_fsynced",
                "replace",
                "directory_fsynced",
                "alias_returned",
            ]
            assert pz._durable is True
        assert events.count("write") == 2
        assert events.count("replace") == 2
        assert not directory_fds
        assert registry_path.read_bytes() == visible_bytes


def test_atomic_write_windows_skips_directory_fsync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows 只同步文件；无条件同步目录会把该平台的原子写入误报为失败。"""
    registry_path = tmp_path / pseudonym.REGISTRY_FILE_NAME
    syncs: list[int] = []
    original_fsync = os.fsync

    def _sync(descriptor: int) -> None:
        syncs.append(descriptor)
        original_fsync(descriptor)

    with monkeypatch.context() as patched:
        patched.setattr(pseudonym.sys, "platform", "win32")
        patched.setattr(pseudonym.os, "fsync", _sync)
        pseudonym._atomic_write(registry_path, b"new-registry")

    assert len(syncs) == 1
    assert registry_path.read_bytes() == b"new-registry"


async def test_unexpected_commit_error_hides_original_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """意外异常只暴露类型：原始错误文本可能夹带真名、路径或 SDK 请求片段。"""
    pz = _make(tmp_path / "reg")

    def _failing_commit(
        self: pseudonym.Pseudonymizer, batch: dict[str, pseudonym._AliasCode]
    ) -> dict[str, str]:
        raise RuntimeError("模拟失败：示例甲 的真实姓名")

    with monkeypatch.context() as patched:
        patched.setattr(pseudonym.Pseudonymizer, "_commit_sync", _failing_commit)
        with pytest.raises(pseudonym.PseudonymError) as excinfo:
            await _person(pz, sender_id="member-0001", name="示例甲")
    message = str(excinfo.value)
    assert "RuntimeError" in message
    assert "示例甲" not in message
    assert pz.commit_count == 0


def test_flush_window_must_be_finite_and_bounded(tmp_path: Path) -> None:
    """合并窗口非法值直接抛：nan/inf 会让 sleep 永不醒来，超上限则违背出境时限。"""
    for window in (-0.001, 5.001, float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError):
            pseudonym.Pseudonymizer(tmp_path / "bad", flush_window=window)
    pseudonym.Pseudonymizer(tmp_path / "low", flush_window=0.0)
    pseudonym.Pseudonymizer(tmp_path / "high", flush_window=pseudonym.MAX_FLUSH_WINDOW)
    # 上限就是契约里的 5 秒：再放宽会让新别名迟迟不能出境
    assert pseudonym.MAX_FLUSH_WINDOW == 5.0


async def test_stale_flush_batch_keeps_newer_batch_future(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """旧批次收尾时不能清掉新批次的 future：否则等它的调用方永远醒不来。"""
    pz = _make(tmp_path / "reg")
    loop = asyncio.get_running_loop()
    stale: asyncio.Future[None] = loop.create_future()
    newer: asyncio.Future[None] = loop.create_future()
    pz._flush_future = stale
    pz._pending = {"0" * 64: pseudonym._AliasCode("同学", "ABCDEF")}

    started = threading.Event()
    release = threading.Event()

    def _blocking_commit(batch: dict[str, pseudonym._AliasCode]) -> dict[str, str]:
        started.set()
        release.wait(5.0)
        return {digest: "同学ABCDEF" for digest in batch}

    pz._commit_sync = _blocking_commit  # type: ignore[method-assign]
    task = loop.create_task(pz._flush_batch(immediate=True))
    assert await asyncio.to_thread(started.wait, 5.0)

    # 旧批次还在线程里跑，此时新批次已经换上新 future
    pz._flush_future = newer
    release.set()
    await task
    assert pz._flush_future is newer
    assert not newer.done()


@pytest.mark.skipif(
    sys.platform != "win32", reason="锁文件初始化只在 Windows 分支里读写字节"
)
def test_lock_file_init_os_error_is_lock_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """fstat/write 失败必须归一成锁错误：否则调用方会当成别的异常继续往下走。"""
    lock_path = tmp_path / "mapping.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:

        def _failing_fstat(*args: object, **kwargs: object) -> object:
            raise OSError("模拟 fstat 失败")

        monkeypatch.setattr(pseudonym.os, "fstat", _failing_fstat)
        with pytest.raises(pseudonym.PseudonymLockError):
            pseudonym._acquire_lock(fd, 0.05)
    finally:
        os.close(fd)


def test_lock_contention_fails_closed(tmp_path: Path) -> None:
    """另一个写者持锁时准备失败：不猜、不等无限、不放行真名。"""
    base = tmp_path / "reg"
    holding = threading.Event()
    release = threading.Event()

    def _hold() -> None:
        with pseudonym._registry_lock(
            base / pseudonym.LOCK_FILE_NAME, timeout=1.0
        ):
            holding.set()
            release.wait(5.0)

    holder = threading.Thread(target=_hold, daemon=True)
    holder.start()
    try:
        assert holding.wait(5.0)
        tight = pseudonym.Pseudonymizer(base, flush_window=0.0, lock_timeout=0.05)
        with pytest.raises(pseudonym.PseudonymLockError):
            asyncio.run(_person(tight, sender_id="member-0001", name="示例甲"))
        assert _counters().get("ai.pseudonym.prepare_failed|") == 1
    finally:
        release.set()
        holder.join(5.0)

    # 锁释放后照常可用（取消/超时路径不留下死锁）
    recovered = asyncio.run(_person(_make(base), sender_id="member-0001", name="示例甲"))
    assert recovered.startswith(pseudonym.ALIAS_PERSON_PREFIX)


def _dump(payload: dict[str, object]) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _valid_registry_payload() -> dict[str, object]:
    return {
        "version": pseudonym.REGISTRY_VERSION,
        "salt": base64.b64encode(b"s" * 32).decode("ascii"),
        "aliases": {"a" * 64: "同学ABCDEF"},
    }


@pytest.mark.parametrize("duplicate", ["version", "aliases", "digest", "salt"])
def test_read_registry_rejects_duplicate_keys(
    tmp_path: Path, duplicate: str
) -> None:
    """原始 JSON 的同值或异值重复键都要拒绝，按值比较会漏掉两份有效的盐。"""
    salt = base64.b64encode(b"s" * 32).decode("ascii")
    other_salt = base64.b64encode(b"t" * 32).decode("ascii")
    digest = "a" * 64
    alias = "同学ABCDEF"
    aliases_json = f'{{"{digest}":"{alias}"}}'
    if duplicate == "version":
        raw = (
            f'{{"version":1,"version":1,"salt":"{salt}",'
            f'"aliases":{aliases_json}}}'
        )
    elif duplicate == "aliases":
        raw = (
            f'{{"version":1,"salt":"{salt}","aliases":{aliases_json},'
            f'"aliases":{aliases_json}}}'
        )
    elif duplicate == "salt":
        raw = (
            f'{{"version":1,"salt":"{salt}","salt":"{other_salt}",'
            f'"aliases":{aliases_json}}}'
        )
    else:
        raw = (
            f'{{"version":1,"salt":"{salt}","aliases":'
            f'{{"{digest}":"{alias}","{digest}":"{alias}"}}}}'
        )
    registry_path = tmp_path / pseudonym.REGISTRY_FILE_NAME
    original = raw.encode("utf-8")
    registry_path.write_bytes(original)

    with pytest.raises(pseudonym.PseudonymRegistryError) as excinfo:
        pseudonym._read_registry(registry_path)

    message = str(excinfo.value)
    assert "重复键" in message
    for marker in ("version", "aliases", digest, salt, other_salt, alias, raw):
        assert marker not in message
    assert registry_path.read_bytes() == original


@pytest.mark.parametrize(
    "field", ["unknown", "version", "salt", "aliases", "aliases-swapped"]
)
def test_read_registry_requires_exact_fields(tmp_path: Path, field: str) -> None:
    """未知或缺失字段都要拒绝，等量替换不能绕过集合校验后泄漏 KeyError。"""
    payload = _valid_registry_payload()
    if field == "unknown":
        payload[field] = "示例额外值"
    elif field == "aliases-swapped":
        payload.pop("aliases")
        payload["unknown"] = "示例额外值"
    else:
        payload.pop(field)
    registry_path = tmp_path / pseudonym.REGISTRY_FILE_NAME
    original = _dump(payload)
    registry_path.write_bytes(original)

    with pytest.raises(pseudonym.PseudonymRegistryError):
        pseudonym._read_registry(registry_path)
    assert registry_path.read_bytes() == original


@pytest.mark.parametrize("version", [1.0, True, "1"], ids=["float", "bool", "string"])
def test_read_registry_requires_integer_version(tmp_path: Path, version: object) -> None:
    """数值相等不代表格式正确，浮点数与 bool 不能冒充注册表整数版本。"""
    payload = _valid_registry_payload()
    payload["version"] = version
    registry_path = tmp_path / pseudonym.REGISTRY_FILE_NAME
    original = _dump(payload)
    registry_path.write_bytes(original)

    with pytest.raises(pseudonym.PseudonymRegistryError):
        pseudonym._read_registry(registry_path)
    assert registry_path.read_bytes() == original


def test_read_registry_accepts_current_version(tmp_path: Path) -> None:
    """严格校验不能拒绝本安装已有的合法注册表，否则正常重启也会阻断出站。"""
    registry_path = tmp_path / pseudonym.REGISTRY_FILE_NAME
    original = _dump(_valid_registry_payload())
    registry_path.write_bytes(original)

    registry = pseudonym._read_registry(registry_path)
    assert registry.salt == b"s" * 32
    assert registry.aliases == {"a" * 64: "同学ABCDEF"}
    assert registry_path.read_bytes() == original


def _only_alias(payload: dict[str, object]) -> tuple[dict[str, object], str]:
    """取出注册表里唯一的条目（键与值都是文件里的原样内容）。"""
    aliases = payload["aliases"]
    assert isinstance(aliases, dict) and len(aliases) == 1
    key = next(iter(aliases))
    assert isinstance(key, str)
    return aliases, key


def _corrupt_json(payload: dict[str, object]) -> bytes:
    return b"{not-valid-json"


def _corrupt_version_number(payload: dict[str, object]) -> bytes:
    payload["version"] = 99
    return _dump(payload)


def _corrupt_version_bool(payload: dict[str, object]) -> bytes:
    # bool 是 int 的子类：`version: true` 用 `!=` 比较会等于 1 而混过版本校验
    payload["version"] = True
    return _dump(payload)


def _corrupt_missing_salt(payload: dict[str, object]) -> bytes:
    payload.pop("salt", None)
    return _dump(payload)


def _corrupt_bad_base64(payload: dict[str, object]) -> bytes:
    payload["salt"] = "!!!不是合法 base64!!!"
    return _dump(payload)


def _corrupt_short_salt(payload: dict[str, object]) -> bytes:
    payload["salt"] = base64.b64encode(b"too-short").decode("ascii")
    return _dump(payload)


def _corrupt_aliases_not_object(payload: dict[str, object]) -> bytes:
    payload["aliases"] = ["不是对象"]
    return _dump(payload)


def _corrupt_aliases_missing(payload: dict[str, object]) -> bytes:
    # 缺字段当空表处理等于「注册表被清空也放行」：历史别名全部失效却没人报错
    payload.pop("aliases", None)
    return _dump(payload)


def _corrupt_aliases_null(payload: dict[str, object]) -> bytes:
    payload["aliases"] = None
    return _dump(payload)


def _corrupt_plaintext_alias(payload: dict[str, object]) -> bytes:
    aliases, key = _only_alias(payload)
    aliases[key] = "示例甲"
    return _dump(payload)


def _corrupt_digest_uppercase(payload: dict[str, object]) -> bytes:
    aliases, key = _only_alias(payload)
    value = aliases.pop(key)
    aliases[key.upper()] = value
    return _dump(payload)


def _corrupt_digest_short(payload: dict[str, object]) -> bytes:
    aliases, key = _only_alias(payload)
    value = aliases.pop(key)
    aliases[key[:62]] = value
    return _dump(payload)


def _corrupt_digest_invalid_hex(payload: dict[str, object]) -> bytes:
    """64 位但含非法 hex 字符：长度对不代表形态对。"""
    aliases, key = _only_alias(payload)
    value = aliases.pop(key)
    aliases["g" * 64] = value
    return _dump(payload)


def _corrupt_duplicate_alias(payload: dict[str, object]) -> bytes:
    aliases, key = _only_alias(payload)
    value = aliases[key]
    spare = "0" * 64 if "0" * 64 not in aliases else "1" * 64
    aliases[spare] = value
    return _dump(payload)


def _corrupt_kind_prefix_mismatch(payload: dict[str, object]) -> bytes:
    """把人的别名改成群的合法别名：形态没问题，但与人这个 kind 不同域。"""
    aliases, key = _only_alias(payload)
    alias = aliases[key]
    assert isinstance(alias, str)
    aliases[key] = pseudonym.ALIAS_GROUP_PREFIX + alias[
        len(pseudonym.ALIAS_PERSON_PREFIX) :
    ]
    return _dump(payload)


def _corrupt_invalid_utf8(payload: dict[str, object]) -> bytes:
    return b'{"version": 1, "salt": "\xff\xfe", "aliases": {}}'


@pytest.mark.parametrize(
    "corrupt",
    [
        _corrupt_json,
        _corrupt_version_number,
        _corrupt_version_bool,
        _corrupt_missing_salt,
        _corrupt_bad_base64,
        _corrupt_short_salt,
        _corrupt_aliases_not_object,
        _corrupt_aliases_missing,
        _corrupt_aliases_null,
        _corrupt_plaintext_alias,
        _corrupt_digest_uppercase,
        _corrupt_digest_short,
        _corrupt_digest_invalid_hex,
        _corrupt_duplicate_alias,
        _corrupt_kind_prefix_mismatch,
        _corrupt_invalid_utf8,
    ],
)
async def test_corrupt_salt_never_auto_rotates(
    tmp_path: Path, corrupt: Callable[[dict[str, object]], bytes]
) -> None:
    base = tmp_path / "reg"
    pz = _make(base)
    good = await _person(pz, sender_id="member-0001", name="示例甲")
    registry_path = base / pseudonym.REGISTRY_FILE_NAME
    original = _registry_payload(base)

    registry_path.write_bytes(corrupt(copy.deepcopy(original)))
    broken_bytes = registry_path.read_bytes()

    with pytest.raises(pseudonym.PseudonymRegistryError) as excinfo:
        await _person(_make(base), sender_id="member-0001", name="示例甲")
    # 错误文本只描述「哪一项不对」，不回显文件内容（明文姓名同样不能进日志）
    assert "示例甲" not in str(excinfo.value)
    # 没有自动重盐：坏文件原样留在盘上，等人按规程恢复
    assert registry_path.read_bytes() == broken_bytes

    # 恢复同一份注册表后别名仍是原来的（盐没被换掉）
    registry_path.write_text(
        json.dumps(original, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    assert await _person(_make(base), sender_id="member-0001", name="示例甲") == good


def test_read_registry_rejects_non_alias_values(tmp_path: Path) -> None:
    """读取层自己就要拒掉非别名值：不能只靠使用时的前缀复核兜底。"""
    base = tmp_path / "reg"
    pz = _make(base)
    asyncio.run(_person(pz, sender_id="member-0001", name="示例甲"))
    payload = _registry_payload(base)
    aliases, key = _only_alias(payload)
    aliases[key] = "示例甲"
    registry_path = base / pseudonym.REGISTRY_FILE_NAME
    registry_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(pseudonym.PseudonymRegistryError) as excinfo:
        pseudonym._read_registry(registry_path)
    assert "形态异常的别名条目" in str(excinfo.value)
    assert "示例甲" not in str(excinfo.value)


async def test_version_error_does_not_echo_value(tmp_path: Path) -> None:
    """版本字段可能被改写成敏感内容：错误里只能出现「不受支持」与路径。"""
    base = tmp_path / "reg"
    pz = _make(base)
    await _person(pz, sender_id="member-0001", name="示例甲")
    payload = _registry_payload(base)
    marker = "敏感版本标记-DO-NOT-ECHO"
    payload["version"] = marker
    registry_path = base / pseudonym.REGISTRY_FILE_NAME
    registry_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(pseudonym.PseudonymRegistryError) as excinfo:
        await _person(_make(base), sender_id="member-0001", name="示例甲")
    message = str(excinfo.value)
    assert marker not in message
    assert "版本不受支持" in message
    assert str(registry_path) in message


async def test_registry_repr_exposes_only_entry_count(tmp_path: Path) -> None:
    base = tmp_path / "reg"
    pz = _make(base)
    alias = await _person(pz, sender_id="member-0001", name="示例甲")
    registry = pseudonym._read_registry(base / pseudonym.REGISTRY_FILE_NAME)

    text = repr(registry)
    assert text == f"<_Registry entries={len(registry.aliases)}>"
    assert alias not in text
    salt_b64 = base64.b64encode(registry.salt).decode("ascii")
    assert salt_b64 not in text


async def test_prepare_failure_counts_and_never_returns_plaintext(
    tmp_path: Path,
) -> None:
    base = tmp_path / "reg"
    pz = _make(base)
    await _person(pz, sender_id="member-0001", name="示例甲")
    payload = _registry_payload(base)
    payload["salt"] = base64.b64encode(b"\x00" * 8).decode("ascii")
    (base / pseudonym.REGISTRY_FILE_NAME).write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )

    ctx = pseudonym.NameContext(_make(base))
    with pytest.raises(pseudonym.PseudonymRegistryError):
        await ctx.person_alias(
            source="weflow",
            sender_id="member-0002",
            session_id="会话-0001",
            display_name="示例乙",
        )
    assert _counters().get("ai.pseudonym.prepare_failed|") == 1
    assert ctx.restore_failures == 0


async def test_empty_display_name_stays_empty(tmp_path: Path) -> None:
    base = tmp_path / "reg"
    pz = _make(base)
    ctx = pseudonym.NameContext(pz)
    assert (
        await ctx.person_alias(
            source="weflow", sender_id="member-0001", session_id="会话-0001", display_name=""
        )
        == ""
    )
    assert await ctx.group_alias(source="weflow", session_id="会话-0001", group_name="  ") == ""
    assert pz.commit_count == 0
    assert not (base / pseudonym.REGISTRY_FILE_NAME).exists()


# ── 策略与掩码 ──


async def test_policy_auto_on_off_and_mask_first(tmp_path: Path) -> None:
    assert pseudonym.pseudonymize_enabled("auto", remote_endpoint=True) is True
    assert pseudonym.pseudonymize_enabled("auto", remote_endpoint=False) is False
    assert pseudonym.pseudonymize_enabled("", remote_endpoint=True) is True  # 空串回 auto
    assert pseudonym.pseudonymize_enabled("", remote_endpoint=False) is False
    assert pseudonym.pseudonymize_enabled("on", remote_endpoint=False) is True
    assert pseudonym.pseudonymize_enabled("off", remote_endpoint=True) is False
    assert pseudonym.pseudonymize_enabled(" OFF ", remote_endpoint=True) is False
    with pytest.raises(ValueError):
        pseudonym.pseudonymize_enabled("大概是开", remote_endpoint=True)

    # 掩码先行：送进假名化的是已经掩过 PII 的显示名
    masked = mask_content(clean_display_name("示例甲 sample.person@example.invalid"))
    assert "[EMAIL]" in masked and "example.invalid" not in masked

    pz = _make(tmp_path / "reg")
    ctx = pseudonym.NameContext(pz)
    alias = await ctx.person_alias(
        source="weflow",
        sender_id="member-0001",
        session_id="会话-0002",
        display_name=masked,
    )
    assert alias.startswith(pseudonym.ALIAS_PERSON_PREFIX)
    assert "示例甲" not in alias and "example" not in alias
    # 掩码后同名（都被掩成同一占位符）的两个人靠 sender_id 分开
    other = await _person(pz, sender_id="member-0002", name=masked)
    assert other != alias

    # 策略关闭时（如本地端点）别名与还原都原样透传，不做无谓改写
    passthrough = pseudonym.NameContext(pz, enabled=False)
    plain = await passthrough.person_alias(
        source="weflow",
        sender_id="member-0001",
        session_id="会话-0002",
        display_name="示例甲",
    )
    assert plain == "示例甲"
    assert passthrough.restore(f"由{alias}发布", field="subject") == f"由{alias}发布"
    assert passthrough.restore_failures == 0


# ── 还原 ──


async def test_restore_exact_tokens_preserves_quotes_and_protected_fields(
    tmp_path: Path,
) -> None:
    base = tmp_path / "reg"
    pz = _make(base)
    ctx = pseudonym.NameContext(pz)
    person = await ctx.person_alias(
        source="weflow",
        sender_id="member-0001",
        session_id="会话-0001",
        display_name="示例甲",
    )
    group = await ctx.group_alias(
        source="weflow", session_id="会话-0001", group_name="示例群"
    )
    # 上下文对象不能把真名带进日志/调试输出
    assert "示例甲" not in repr(ctx) and "示例群" not in repr(ctx)

    payload = {
        "index": 3,
        "category": "活动",
        "subject": f"{person}在{group}发言",
        "quote": f"「{person}」说：带引号也没问题",
        "confidence": 0.87,
    }
    raw = json.dumps(payload, ensure_ascii=False)
    parsed = json.loads(raw)
    # 只还原白名单自由文本；category/index/置信度原样
    parsed["subject"] = ctx.restore(parsed["subject"], field="subject")
    parsed["quote"] = ctx.restore(parsed["quote"], field="quote")
    assert parsed["subject"] == "示例甲在示例群发言"
    assert parsed["quote"] == "「示例甲」说：带引号也没问题"
    assert parsed["index"] == 3
    assert parsed["category"] == "活动"
    assert parsed["confidence"] == 0.87
    assert ctx.restore_failures == 0
    # 还原发生在解析后：重新序列化仍是合法 JSON，引号结构没被破坏
    assert json.loads(json.dumps(parsed, ensure_ascii=False))["quote"] == parsed["quote"]
    assert json.loads(raw)["subject"] == payload["subject"]  # 原串未被就地改动

    # 最长优先：一个别名是另一个的前缀时各自还原
    collision_pz = _make(tmp_path / "reg-collision", digest_fn=_collision_digest)
    collision_ctx = pseudonym.NameContext(collision_pz)
    first = await collision_ctx.person_alias(
        source="weflow",
        sender_id="member-0001",
        session_id="会话-0001",
        display_name="示例甲",
    )
    second = await collision_ctx.person_alias(
        source="weflow",
        sender_id="member-0002",
        session_id="会话-0001",
        display_name="示例乙",
    )
    assert second == f"{first}-1"
    assert collision_ctx.restore(f"{second} 与 {first}", field="subject") == "示例乙 与 示例甲"
    # 人/群同短码但前缀不同：各自还原，不串域
    group_same_code = await collision_ctx.group_alias(
        source="weflow", session_id="会话-0001", group_name="示例群"
    )
    assert group_same_code == f"{pseudonym.ALIAS_GROUP_PREFIX}{first[2:]}"
    assert (
        collision_ctx.restore(f"{group_same_code}/{first}", field="quote")
        == "示例群/示例甲"
    )
    assert collision_ctx.restore_failures == 0


async def test_unknown_alias_keeps_value_and_counts_failure(tmp_path: Path) -> None:
    pz = _make(tmp_path / "reg")
    ctx = pseudonym.NameContext(pz)
    known = await ctx.person_alias(
        source="weflow",
        sender_id="member-0001",
        session_id="会话-0001",
        display_name="示例甲",
    )
    text = f"{known} 和 同学ZZZZZZ 一起"
    assert ctx.restore(text, field="subject") == "示例甲 和 同学ZZZZZZ 一起"
    assert ctx.restore_failures == 1
    # 同一字段重复兜底不重复计数
    assert ctx.restore(text, field="subject") == "示例甲 和 同学ZZZZZZ 一起"
    assert ctx.restore_failures == 1
    # 另一个字段独立计一次
    assert ctx.restore("群QQQQQQ", field="quote") == "群QQQQQQ"
    assert ctx.restore_failures == 2
    assert _counters().get("ai.pseudonym.restore_failed|") == 2
    # 缺前缀的裸短码也算「被改写」：原样保留并计失败
    code = known[len(pseudonym.ALIAS_PERSON_PREFIX):]
    assert ctx.restore(f"{code} 在说话", field="title") == f"{code} 在说话"
    assert ctx.restore_failures == 3


async def test_last_display_name_wins_for_restore(tmp_path: Path) -> None:
    pz = _make(tmp_path / "reg")
    ctx = pseudonym.NameContext(pz)
    first = await ctx.person_alias(
        source="weflow",
        sender_id="member-0001",
        session_id="会话-0001",
        display_name="示例甲",
    )
    again = await ctx.person_alias(
        source="weflow",
        sender_id="member-0001",
        session_id="会话-0001",
        display_name="示例甲-改名",
    )
    assert again == first
    # 同一身份在请求内出现多个显示名：用最后一个非空显示名做还原目标
    assert ctx.restore(first, field="subject") == "示例甲-改名"
    assert ctx.display_name_for(first, kind=pseudonym.KIND_PERSON) == "示例甲-改名"
    assert ctx.display_name_for(first, kind=pseudonym.KIND_GROUP) is None
    assert ctx.display_name_for("同学ZZZZZZ") is None


async def test_restore_requires_whole_token(tmp_path: Path) -> None:
    """只还原完整 token：被拼长、改小写、带杂后缀或丢前缀的片段原样保留并计失败。"""
    pz = _make(tmp_path / "reg")
    ctx = pseudonym.NameContext(pz)
    known = await ctx.person_alias(
        source="weflow",
        sender_id="member-0001",
        session_id="会话-0001",
        display_name="示例甲",
    )
    code = known[len(pseudonym.ALIAS_PERSON_PREFIX) :]

    # 中文紧邻照常还原：边界只排除 ASCII 词字符，汉字不算词字符
    assert ctx.restore(f"由{known}发布", field="adjacent") == "由示例甲发布"
    assert ctx.restore_failures == 0

    rewritten = {
        "extended": f"{known}G",  # 被拼长：不能只还原前半段
        "lowercase": pseudonym.ALIAS_PERSON_PREFIX + "abcdef",  # 模型改成小写
        "suffix_junk": f"{known}-1x",  # 后缀不是纯数字
        "bare_suffix": f"{code}-1",  # 裸码带后缀
    }
    for field, text in rewritten.items():
        assert ctx.restore(text, field=field) == text
    assert ctx.restore_failures == len(rewritten)
    assert _counters().get("ai.pseudonym.restore_failed|") == len(rewritten)


async def test_restore_preserves_rewritten_alias_shapes(tmp_path: Path) -> None:
    """本请求已知的别名被改写后仍原样保留：不猜、不改写、不静默吞掉。"""
    ctx = pseudonym.NameContext(
        _make(tmp_path / "reg", digest_fn=_forced_digest("ABCDEF"))
    )
    known = await ctx.person_alias(
        source="weflow",
        sender_id="member-0001",
        session_id="会话-0001",
        display_name="示例甲",
    )
    # 先把 ABCDEF 固定成本请求已知别名：不登记的话「原样保留」可能只是碰巧没匹配
    assert known == "同学ABCDEF"
    assert ctx.restore(known, field="exact") == "示例甲"
    assert ctx.restore_failures == 0

    rewritten = {
        "extended": "同学ABCDEFG",  # 已知别名再拼一个字母：不能只还原前半段
        "suffix_junk": "同学ABCDEF-1x",  # 后缀不是纯数字
        "unknown_suffix": "同学ABCDEF-1",  # 形态合法但不是本请求的别名
    }
    for field, text in rewritten.items():
        assert ctx.restore(text, field=field) == text
    assert ctx.restore_failures == len(rewritten)
    assert _counters().get("ai.pseudonym.restore_failed|") == len(rewritten)


async def test_known_suffixed_alias_with_extra_ascii_stays_intact(
    tmp_path: Path,
) -> None:
    """已知带后缀别名后拼一个 ASCII 字母（或再拼后缀）：整体保留，不截断成前缀那个人。"""
    ctx = pseudonym.NameContext(
        _make(tmp_path / "reg", digest_fn=_forced_digest("ABCDEF"))
    )
    first = await ctx.person_alias(
        source="weflow",
        sender_id="member-0001",
        session_id="会话-0001",
        display_name="示例甲",
    )
    second = await ctx.person_alias(
        source="weflow",
        sender_id="member-0002",
        session_id="会话-0001",
        display_name="示例乙",
    )
    assert first == "同学ABCDEF"
    assert second == "同学ABCDEF-1"
    # 完整匹配优先：带后缀的已知别名整体还原成乙，而不是截成甲
    assert ctx.restore(second, field="exact") == "示例乙"
    assert ctx.restore_failures == 0

    rewritten = {
        "letter": f"{second}G",  # 再拼一个 ASCII 字母
        "another_suffix": f"{second}-2",  # 后缀再套一层
    }
    for field, text in rewritten.items():
        assert ctx.restore(text, field=field) == text
    assert ctx.restore_failures == len(rewritten)


@pytest.mark.parametrize(
    "text",
    [
        "同学A",  # 1 个字符的截断
        "同学AB",  # 2 个字符的截断
        "同学ABC",  # 3 个字符的截断
        "同学abcd",  # 4 个小写字母
        "同学abcde",  # 5 个小写字母
    ],
)
async def test_short_or_lowercase_rewrites_are_counted(
    tmp_path: Path, text: str
) -> None:
    """短截断与 4–5 个小写字母同样是被改写的别名，必须计入失败。"""
    ctx = pseudonym.NameContext(_make(tmp_path / "reg"))
    known = await ctx.person_alias(
        source="weflow",
        sender_id="member-0001",
        session_id="会话-0001",
        display_name="示例甲",
    )
    assert known.startswith(pseudonym.ALIAS_PERSON_PREFIX)  # 本请求确实有已知别名
    assert ctx.restore(text, field="subject") == text
    assert ctx.restore_failures == 1
    assert _counters().get("ai.pseudonym.restore_failed|") == 1


async def test_natural_chinese_after_alias_prefix_is_not_a_failure(
    tmp_path: Path,
) -> None:
    """前缀后跟数量词（同学3人/同学2345人）是自然中文，失败计数必须为 0。"""
    ctx = pseudonym.NameContext(_make(tmp_path / "reg"))
    await ctx.person_alias(
        source="weflow",
        sender_id="member-0001",
        session_id="会话-0001",
        display_name="示例甲",
    )
    assert ctx.restore("同学3人", field="subject") == "同学3人"
    assert ctx.restore("同学2345人", field="quote") == "同学2345人"
    assert ctx.restore_failures == 0
    assert not _counters().get("ai.pseudonym.restore_failed|")


async def test_restore_is_idempotent_within_one_request(tmp_path: Path) -> None:
    """同输入复用输出；输入变了就重新判定——已产出的显示名不得二次替换。"""
    pz = _make(tmp_path / "reg")
    ctx = pseudonym.NameContext(pz)
    other = await ctx.person_alias(
        source="weflow",
        sender_id="member-0002",
        session_id="会话-0001",
        display_name="示例乙",
    )
    # 甲的显示名刚好是乙的别名：一次还原后的输出里仍含一个「已知别名」
    one = await ctx.person_alias(
        source="weflow",
        sender_id="member-0001",
        session_id="会话-0001",
        display_name=other,
    )
    assert one != other

    original = f"由{one}发布"
    once = ctx.restore(original, field="subject")
    assert once == f"由{other}发布"
    # 同一字段、同一输入（最初输入，不是上次输出）：直接复用上次输出（真正的幂等）
    assert ctx.restore(original, field="subject") == once
    assert ctx.restore_failures == 0

    # 幂等不只靠输入缓存：清掉本字段的缓存后重放最初输入，结果仍是同一份输出。
    # 这一条钉住「替换本身可重放」——缓存丢失（例如换字段、重建上下文）后不能
    # 把已产出的显示名再拆回别名。
    ctx._last_io.clear()
    assert ctx.restore(original, field="subject") == once
    assert ctx.restore_failures == 0

    # 另一个字段拿到同样文本：输入与它自己的上次输入不同，按新输入处理；
    # 命中的 token 是本请求已产出的显示名（也是乙的别名），原样保留、不计失败
    assert ctx.restore(once, field="quote") == once
    assert ctx.restore_failures == 0

    # 证明它确实走了处理路径而不是整体短路：同一条文本里再拼一个坏别名，
    # 失败计数照常增加，已产出的名字仍原样保留
    fresh = f"{once} 和 同学ZZZZZZ"
    assert ctx.restore(fresh, field="quote") == fresh
    assert ctx.restore_failures == 1


# ── 已产出显示名的保护区间 ──


async def _produced_name_scene(
    tmp_path: Path,
) -> tuple[pseudonym.NameContext, str, str]:
    """构造「一个人的显示名里含另一个人的别名形态」的场景。

    乙先登记，拿到短码 `ABCDEF`；甲因短码碰撞拿到 `ABCDEF-1`，而甲的显示名恰好
    以乙的别名开头。两个别名来自两个不同的摘要，不是同一个别名的两种写法——
    只按匹配到的 token 查成员关系，第二次还原就会把甲的名字拆成「乙 + 残渣」。
    """
    ctx = pseudonym.NameContext(
        _make(tmp_path / "reg", digest_fn=_forced_digest("ABCDEF"))
    )
    other = await ctx.person_alias(
        source="weflow",
        sender_id="member-0002",
        session_id="会话-0001",
        display_name="示例乙",
    )
    target = await ctx.person_alias(
        source="weflow",
        sender_id="member-0001",
        session_id="会话-0001",
        display_name="同学ABCDEF老师",
    )
    assert other == "同学ABCDEF"
    assert target == "同学ABCDEF-1"
    return ctx, other, target


async def test_produced_display_name_is_never_reinterpreted(tmp_path: Path) -> None:
    """写回过的显示名整体受保护：里面的别名形态不能被二次替换成另一个人。"""
    ctx, _other, target = await _produced_name_scene(tmp_path)
    assert ctx.restore(target, field="subject") == "同学ABCDEF老师"
    assert ctx.restore_failures == 0

    # 同字段再还原一次（输入是上次输出，不是输入缓存命中）：`同学ABCDEF` 确实是
    # 乙的别名，但它现在只是甲显示名的一部分，替换它会把甲拆成乙 + 残渣
    assert ctx.restore("同学ABCDEF老师", field="subject") == "同学ABCDEF老师"
    assert ctx.restore_failures == 0
    assert not _counters().get("ai.pseudonym.restore_failed|")


async def test_protected_display_name_survives_inside_sentence(
    tmp_path: Path,
) -> None:
    """保护按文本范围生效：前后夹中文与标点时不把显示名里的片段当别名。"""
    ctx, _other, target = await _produced_name_scene(tmp_path)
    original = f"（前情）据{target}透露，稍后还有更新；"
    once = ctx.restore(original, field="subject")
    assert once == "（前情）据同学ABCDEF老师透露，稍后还有更新；"

    # 同一字段、输入换成上次输出：仍必须整体保留
    assert ctx.restore(once, field="subject") == once
    assert ctx.restore_failures == 0


async def test_protected_display_name_survives_in_another_field(
    tmp_path: Path,
) -> None:
    """保护范围按请求累计而不是按字段：换字段再还原也不得二次替换。"""
    ctx, _other, target = await _produced_name_scene(tmp_path)
    produced = ctx.restore(target, field="subject")
    assert produced == "同学ABCDEF老师"

    text = f"{produced} 在群里发言"
    assert ctx.restore(text, field="summary") == text
    assert ctx.restore_failures == 0


async def test_alias_outside_protected_range_still_restores(tmp_path: Path) -> None:
    """保护只覆盖已产出的显示名：保护范围之外的已知别名照常还原。"""
    ctx, _other, target = await _produced_name_scene(tmp_path)
    third = await ctx.person_alias(
        source="weflow",
        sender_id="member-0003",
        session_id="会话-0001",
        display_name="示例甲",
    )
    assert third == "同学ABCDEF-2"
    assert ctx.restore(target, field="subject") == "同学ABCDEF老师"

    text = f"由{third}转述：同学ABCDEF老师 已确认，另见 同学ZZZZZZ"
    assert (
        ctx.restore(text, field="quote")
        == "由示例甲转述：同学ABCDEF老师 已确认，另见 同学ZZZZZZ"
    )
    # 计数只按真实失败增加：抄错的那个别名计一次，受保护的显示名不计
    assert ctx.restore_failures == 1
    assert _counters().get("ai.pseudonym.restore_failed|") == 1


@pytest.mark.parametrize(
    "text",
    [
        "同学ID",  # 两个大写字母，自然行文里像缩写
        "同学OK",  # 同上
        "同学A座",  # 一个字母 + 中文量词
        "同学234567人",  # 六位数字刚好落在 base32 字母表里
    ],
)
async def test_natural_prefix_phrases_keep_heuristic_failure_count(
    tmp_path: Path, text: str
) -> None:
    """自然行文里的前缀形态：原样保留并计一次失败——这是既定启发式，不是回归。

    这些片段不是模型抄错的别名，但形状与「被改写的别名」一致。这里把「原样保留 +
    每字段计一次」钉住：改判定时若把计数改成 0 或开始改写文本，这条用例会红。
    """
    ctx = pseudonym.NameContext(_make(tmp_path / "reg"))
    await ctx.person_alias(
        source="weflow",
        sender_id="member-0001",
        session_id="会话-0001",
        display_name="示例甲",
    )
    assert ctx.restore(text, field="subject") == text
    assert ctx.restore_failures == 1
    assert _counters().get("ai.pseudonym.restore_failed|") == 1
