"""briefdesk/pseudonym.py 的单元测试：身份键、落盘、还原与失败红线。

全部用例用 `tmp_path` 隔离注册表，绝不触碰真实用户数据目录；样本一律虚构
（人名、群名、ID 都是示例值）。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import threading
import time
from collections.abc import Callable
from pathlib import Path

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
    """强制短码碰撞的摘要：前 4 字节固定 → 前 6 位 base32 一定相同。"""
    tail = hashlib.sha256(f"{kind}:{identity_key}".encode()).hexdigest()
    return "00000000" + tail


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
        # 旧表原样保留、没留下临时文件、旧别名照常可用
        assert registry_path.read_bytes() == before_bytes
        assert not list(base.glob("*.tmp"))
        assert await _person(pz, sender_id="member-0001", name="示例甲") == first

    # 写路径恢复后可以继续登记，且不污染已提交的旧别名
    second = await _person(pz, sender_id="member-0002", name="示例乙")
    assert second != first
    assert {first, second} <= set(_aliases(base).values())


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


def _corrupt_json(payload: dict[str, object]) -> str:
    return "{not-valid-json"


def _corrupt_version(payload: dict[str, object]) -> str:
    payload["version"] = 99
    return json.dumps(payload, ensure_ascii=False)


def _corrupt_missing_salt(payload: dict[str, object]) -> str:
    payload.pop("salt", None)
    return json.dumps(payload, ensure_ascii=False)


def _corrupt_bad_base64(payload: dict[str, object]) -> str:
    payload["salt"] = "!!!不是合法 base64!!!"
    return json.dumps(payload, ensure_ascii=False)


def _corrupt_short_salt(payload: dict[str, object]) -> str:
    payload["salt"] = base64.b64encode(b"too-short").decode("ascii")
    return json.dumps(payload, ensure_ascii=False)


def _corrupt_aliases(payload: dict[str, object]) -> str:
    payload["aliases"] = ["不是对象"]
    return json.dumps(payload, ensure_ascii=False)


@pytest.mark.parametrize(
    "corrupt",
    [
        _corrupt_json,
        _corrupt_version,
        _corrupt_missing_salt,
        _corrupt_bad_base64,
        _corrupt_short_salt,
        _corrupt_aliases,
    ],
)
async def test_corrupt_salt_never_auto_rotates(
    tmp_path: Path, corrupt: Callable[[dict[str, object]], str]
) -> None:
    base = tmp_path / "reg"
    pz = _make(base)
    good = await _person(pz, sender_id="member-0001", name="示例甲")
    registry_path = base / pseudonym.REGISTRY_FILE_NAME
    original = _registry_payload(base)

    registry_path.write_text(corrupt(dict(original)), encoding="utf-8")
    broken_bytes = registry_path.read_bytes()

    with pytest.raises(pseudonym.PseudonymRegistryError):
        await _person(_make(base), sender_id="member-0001", name="示例甲")
    # 没有自动重盐：坏文件原样留在盘上，等人按规程恢复
    assert registry_path.read_bytes() == broken_bytes

    # 恢复同一份注册表后别名仍是原来的（盐没被换掉）
    registry_path.write_text(
        json.dumps(original, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    assert await _person(_make(base), sender_id="member-0001", name="示例甲") == good


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
