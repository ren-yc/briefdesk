"""姓名假名化 — 出站显示名换成稳定别名，入站再按请求内逆映射还原。

为什么需要：结构化显示名（人名、群名）此前原样进入远端模型请求，脱敏只覆盖
手机号/邮箱一类 PII，昵称本身仍然是身份信息。本模块只改「出站显示副本」：
原始消息对象、数据库身份列与前端展示始终是真名，别名只是请求内的临时替身。

关键约束与失败模式：

- 别名跨重启稳定：盐与「完整哈希 → 别名」映射写在同一个版本化注册表文件里
  （用户数据目录下的 `pseudonym/mapping.json`）。盐与映射分开存会在两次写入
  之间出现半发布状态——映射已更新而盐还是旧的，重启后同一身份算出另一个别名。
- 新别名必须先落盘再出境：调用方拿到别名时该身份已经原子提交，否则进程崩溃
  后模型见过的别名再也对不上号。合并提交只减少写盘次数，不减少这个顺序保证。
- 任何一步失败都停（注册表损坏、版本不支持、盐缺失、加锁失败、写盘失败）：
  不自动重盐、不退回明文、不把真名重新发给远端。自动重盐会让历史别名永久
  失效；退回明文则把本模块要挡的东西直接放出去。
- 注册表文件里只有盐与哈希：不写人名、群名、sender_id、session_id。哈希是
  HMAC-SHA256，但盐与哈希同文件，拿到文件即可穷举常见昵称——它是「不出明文」
  的下限，不是不可逆的安全边界。
- 逆映射只活在请求内存里：`NameContext` 用完即弃，不落盘、不进日志。
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import math
import os
import re
import sys
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple

from briefdesk import paths, telemetry

KIND_PERSON = "person"
KIND_GROUP = "group"
_KINDS = frozenset({KIND_PERSON, KIND_GROUP})

# 人/群用不同前缀：同一段短码在两个域里各代表一个人和一个群，还原时必须能
# 从 token 本身看出域，不能只靠短码反查（`test_group_identity_preserves_same_name_groups`）。
ALIAS_PERSON_PREFIX = "同学"
ALIAS_GROUP_PREFIX = "群"
_ALIAS_PREFIXES = {KIND_PERSON: ALIAS_PERSON_PREFIX, KIND_GROUP: ALIAS_GROUP_PREFIX}

# 短码长度：base32 每字符 5 bit，6 字符 30 bit。太短会频繁碰撞，太长模型抄错
# 的概率上升；碰撞本身由稳定后缀兜住，所以取短。
ALIAS_CODE_LENGTH = 6
_SALT_SIZE = 32
REGISTRY_VERSION = 1

# base32 字母表（RFC 4648，去掉 0/1/8/9）：短码与别名 token 只由这些字符构成。
_BASE32_CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
_CODE_PATTERN = rf"[{_BASE32_CHARS}]{{{ALIAS_CODE_LENGTH}}}"
_ALIAS_SHAPE_PATTERN = (
    rf"(?:{ALIAS_PERSON_PREFIX}|{ALIAS_GROUP_PREFIX}){_CODE_PATTERN}(?:-\d+)?"
)
# 注册表里的别名必须整体符合这个形态：文件被改写（塞进明文姓名、半截短码或
# 任意字符串）时在读取阶段就停，绝不把文件里的内容当别名发给远端。判定一律用
# `fullmatch`：带 `$` 的 `match` 会放过末尾换行。
_ALIAS_SHAPE_RE = re.compile(_ALIAS_SHAPE_PATTERN)
# 注册表键是完整 HMAC 摘要：只接受 64 位小写十六进制，长度或大小写不对即损坏。
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")

# 新身份默认合并窗口（秒）：窗口内的新身份并进同一次落盘；首次初始化立即提交。
DEFAULT_FLUSH_WINDOW = 5.0
# 合并窗口上限（秒）：窗口过长会让新别名迟迟不能出境，等待的调用方跟着排队；
# 非有限值（nan/inf）会让 `asyncio.sleep` 永远醒不来。
MAX_FLUSH_WINDOW = 30.0
# 跨进程锁等待上限（秒）。超时即失败，不无限等（等不到锁说明另一个写者卡住）。
LOCK_TIMEOUT_SECONDS = 10.0
_LOCK_RETRY_INTERVAL = 0.02
# 提交轮次上限：只为防「分配总落在快照之后」这类异常把请求拖成死循环。
_MAX_FLUSH_ROUNDS = 3

REGISTRY_FILE_NAME = "mapping.json"
LOCK_FILE_NAME = "mapping.lock"

POLICY_AUTO = "auto"
POLICY_ON = "on"
POLICY_OFF = "off"


class PseudonymError(Exception):
    """假名化失败的基类：调用方据此走「准备失败」路径，绝不回退明文。"""


class PseudonymRegistryError(PseudonymError):
    """注册表不可用：缺失、损坏、版本不支持、盐异常或与进程内盐不一致。"""


class _RegistryMissing(PseudonymRegistryError):
    """注册表文件不存在——唯一允许在锁内新建初始表的状态。

    读取失败、解码失败、内容损坏都是别的异常类型：把它们也当「不存在」会直接
    覆盖掉既有映射，等于一次静默重盐。对外仍按注册表错误处理（fail-closed）。
    """


class PseudonymLockError(PseudonymError):
    """跨进程注册锁获取失败（超时或底层锁调用报错）。"""


class PseudonymWriteError(PseudonymError):
    """注册表原子写失败；旧文件保持不变，新别名不发布。"""


# ── 路径与策略 ──


def registry_dir() -> Path:
    """注册表目录（每次调用求值，跟随 `paths.user_data_dir()` 的运行期覆盖）。"""
    return paths.user_data_dir() / "pseudonym"


def pseudonymize_enabled(mode: str, *, remote_endpoint: bool) -> bool:
    """按策略判定本次出站是否假名化。

    auto（含空串）只对远端启用——本地端点不出机器，改名会让本地输入无谓漂移；
    on/off 是强制值，用于运维显式选择。未知取值直接抛错而不是当 auto 处理：
    配置拼错时静默退到 auto 会把「我以为强制开了」变成远端明文。
    """
    normalized = (mode or "").strip().lower()
    if normalized in ("", POLICY_AUTO):
        return remote_endpoint
    if normalized == POLICY_ON:
        return True
    if normalized == POLICY_OFF:
        return False
    raise ValueError(f"未知的姓名假名化策略: {mode!r}（合法值: auto/on/off）")


# ── 身份键 ──


def _canonical_json(value: object) -> str:
    """规范 JSON：键排序、无多余空白，保证同一输入永远得到同一字节串。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def person_identity_key(
    *, source: str, sender_id: str, session_id: str, display_name: str
) -> str:
    """人的身份键。

    有 sender_id 时只用 `(person, source, sender_id)`：同一个人改名、换会话
    仍是同一别名。没有 ID 时退回 `(person, source, session_id, 已掩码姓名)`，
    这是 best-effort——同名者可能被合并，跨数据源不承诺一致（`source` 参与
    身份键正是为了把不同源的 ID 空间隔开）。
    """
    src = (source or "").strip()
    sid = (sender_id or "").strip()
    if sid:
        return _canonical_json([KIND_PERSON, src, "sender_id", sid])
    return _canonical_json(
        [
            KIND_PERSON,
            src,
            "session",
            (session_id or "").strip(),
            "name",
            (display_name or "").strip(),
        ]
    )


def group_identity_key(*, source: str, session_id: str, group_name: str) -> str:
    """群的身份键：有会话 ID 时不以群名为主键（改群名不换别名，同名不同群不合并）。"""
    src = (source or "").strip()
    sid = (session_id or "").strip()
    if sid:
        return _canonical_json([KIND_GROUP, src, "session_id", sid])
    return _canonical_json(
        [KIND_GROUP, src, "name", (group_name or "").strip()]
    )


# ── 注册表结构 ──


@dataclass(frozen=True)
class _Registry:
    """注册表快照：安装级盐 + 完整哈希到别名的映射。"""

    salt: bytes
    aliases: dict[str, str]

    def __repr__(self) -> str:
        """只暴露条目数量：盐与别名同样敏感，不能随异常栈或调试输出扩散。"""
        return f"<_Registry entries={len(self.aliases)}>"


class _AliasCode(NamedTuple):
    """待分配别名的形态：前缀随 kind 固定，短码由完整哈希派生。"""

    prefix: str
    code: str


def _short_code(digest: str) -> str:
    """完整哈希 → 前 6 位 base32 短码（30 bit）。"""
    return base64.b32encode(bytes.fromhex(digest)).decode("ascii")[
        :ALIAS_CODE_LENGTH
    ]


def _default_digest(salt: bytes, kind: str, identity_key: str) -> str:
    """`(版本, kind, 身份键)` 的规范 JSON 做 HMAC-SHA256，返回十六进制摘要。"""
    payload = _canonical_json([REGISTRY_VERSION, kind, identity_key]).encode("utf-8")
    return hmac.new(salt, payload, hashlib.sha256).hexdigest()


def _serialize_registry(registry: _Registry) -> bytes:
    """注册表 → UTF-8 字节；键排序只为让同一内容写出稳定字节。"""
    payload = {
        "version": REGISTRY_VERSION,
        "salt": base64.b64encode(registry.salt).decode("ascii"),
        "aliases": dict(sorted(registry.aliases.items())),
    }
    return json.dumps(payload, ensure_ascii=False, indent=1).encode("utf-8")


def _read_registry(path: Path) -> _Registry:
    """读取并校验注册表；任何异常形态都转成 PseudonymRegistryError。

    错误信息只带路径与「哪一项不对」，绝不带文件里的取值：异常信息会进日志，
    把盐、别名片段或版本原文写进日志等于扩散敏感数据（版本字段已被改写成敏感
    内容的场景同样按此处理）。

    校验是逐层收紧的：顶层对象 → 版本 → 盐 → aliases 是否存在 → 每个键值的
    形态 → 别名唯一性。缺一层就会出现「文件被改写成明文也能当别名发出去」的
    缝，本功能尚无历史文件，因此不为任何缺字段形态留向后兼容。
    """
    try:
        raw_bytes = path.read_bytes()
    except FileNotFoundError:
        raise _RegistryMissing(f"别名注册表不存在: {path}") from None
    except OSError as error:
        raise PseudonymRegistryError(
            f"别名注册表读取失败: {path}（{type(error).__name__}）"
        ) from error
    try:
        raw = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        # 非法 UTF-8 与「内容损坏」同性质：不能当成空表，也不能重写覆盖
        raise PseudonymRegistryError(f"别名注册表不是合法 UTF-8: {path}") from None
    try:
        decoded: object = json.loads(raw)
    except json.JSONDecodeError as error:
        raise PseudonymRegistryError(
            f"别名注册表不是合法 JSON: {path}（{error.msg}）"
        ) from error
    if not isinstance(decoded, dict):
        raise PseudonymRegistryError(f"别名注册表顶层不是对象: {path}")
    data: dict[object, object] = decoded
    version = data.get("version")
    # bool 是 int 的子类：`version: true` 用 `!=` 比较会刚好等于 1 而混过去；
    # 消息里不回显原值——版本字段可能被改写成敏感内容。
    if isinstance(version, bool) or version != REGISTRY_VERSION:
        raise PseudonymRegistryError(
            f"别名注册表版本不受支持（期望 {REGISTRY_VERSION}）: {path}"
        )
    salt_b64 = data.get("salt")
    if not isinstance(salt_b64, str) or not salt_b64:
        raise PseudonymRegistryError(f"别名注册表缺少盐: {path}")
    try:
        salt = base64.b64decode(salt_b64, validate=True)
    except (binascii.Error, ValueError) as error:
        raise PseudonymRegistryError(
            f"别名注册表的盐不是合法 base64: {path}（{type(error).__name__}）"
        ) from error
    if len(salt) != _SALT_SIZE:
        raise PseudonymRegistryError(
            f"别名注册表的盐长度异常: {len(salt)} 字节（期望 {_SALT_SIZE}）: {path}"
        )
    if "aliases" not in data:
        # 缺字段按空表处理会让「被清空的注册表」静默通过：已发出的别名全部失效
        raise PseudonymRegistryError(f"别名注册表缺少 aliases 映射: {path}")
    aliases_raw = data["aliases"]
    if not isinstance(aliases_raw, dict):
        raise PseudonymRegistryError(f"别名注册表的 aliases 不是对象: {path}")
    aliases: dict[str, str] = {}
    alias_owners: dict[str, str] = {}
    entries: dict[object, object] = aliases_raw
    for key, value in entries.items():
        if not isinstance(key, str) or not _DIGEST_RE.fullmatch(key):
            raise PseudonymRegistryError(f"别名注册表含形态异常的摘要键: {path}")
        if not isinstance(value, str) or not _ALIAS_SHAPE_RE.fullmatch(value):
            raise PseudonymRegistryError(f"别名注册表含形态异常的别名条目: {path}")
        if value in alias_owners:
            # 一个别名指向两个身份：还原时无法判断该写回哪个名字，必须停
            raise PseudonymRegistryError(
                f"别名注册表同一个别名对应多个摘要: {path}"
            )
        alias_owners[value] = key
        aliases[key] = value
    return _Registry(salt=salt, aliases=aliases)


def _replace_file(tmp: str, path: Path) -> None:
    """临时文件 → 注册表；独立成函数便于测试注入替换失败。"""
    os.replace(tmp, path)


def _atomic_write(path: Path, payload: bytes) -> None:
    """同目录临时文件 + flush + fsync + 关闭后 `os.replace` 发布。

    失败模式：任何一步失败都清理临时文件并抛 PseudonymWriteError，原文件保持
    原样。`os.replace` 之前写入的内容对新读者不可见——半个文件不会成为新注册表，
    于是「写失败」与「没开始写」对调用方是同一种结果：旧别名继续有效。
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise PseudonymWriteError(
            f"别名注册表目录创建失败: {path.parent}（{error}）"
        ) from error
    tmp_name: str | None = None
    try:
        fd, tmp_name = tempfile.mkstemp(
            prefix=f"{path.name}.", suffix=".tmp", dir=path.parent
        )
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        _replace_file(tmp_name, path)
        tmp_name = None
    except OSError as error:
        raise PseudonymWriteError(
            f"别名注册表写入失败: {path}（{error}）"
        ) from error
    finally:
        if tmp_name is not None:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


# ── 跨进程注册锁 ──


def _acquire_lock(fd: int, timeout: float) -> None:
    """阻塞直到取得排他锁或超时；等待用短睡眠轮询，不忙等占满 CPU。"""
    if sys.platform == "win32":  # pragma: no cover — 平台分支
        _acquire_windows_lock(fd, timeout)
    else:  # pragma: no cover — 平台分支
        _acquire_posix_lock(fd, timeout)


def _acquire_windows_lock(fd: int, timeout: float) -> None:  # pragma: no cover
    """Windows：独立锁文件上的字节锁（msvcrt 需要文件至少 1 字节可锁）。

    依赖在函数内导入：平台专属模块在另一平台上导入即失败，模块级条件导入又会
    让类型检查按当前平台把另一分支判成未定义。
    """
    import msvcrt

    try:
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"\0")
    except OSError as error:
        # 初始化锁文件失败与「等待超时」同性质：都拿不到跨进程互斥，调用方必须
        # 走准备失败，而不是在无锁状态下继续写注册表。
        raise PseudonymLockError(
            f"别名注册锁文件初始化失败（{type(error).__name__}）"
        ) from error
    # 类型忽略：typeshed 在 POSIX 平台下把 msvcrt 当空模块，只有 Windows 才有这些成员
    lock_nb = msvcrt.LK_NBLCK  # type: ignore[attr-defined]
    deadline = time.monotonic() + timeout
    while True:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, lock_nb, 1)  # type: ignore[attr-defined]
            return
        except OSError as error:
            if time.monotonic() >= deadline:
                raise PseudonymLockError(
                    f"别名注册锁等待超时（{timeout:.1f}s）"
                ) from error
            time.sleep(_LOCK_RETRY_INTERVAL)


def _acquire_posix_lock(fd: int, timeout: float) -> None:  # pragma: no cover
    """POSIX：flock 排他锁，非阻塞轮询 + 超时。"""
    import fcntl  # 平台专属模块：函数内导入，避免另一平台上导入即失败

    # 类型忽略：typeshed 在 Windows 平台下把 fcntl 当空模块，只有 POSIX 才有这些成员
    lock_ex = fcntl.LOCK_EX  # type: ignore[attr-defined]
    lock_nb = fcntl.LOCK_NB  # type: ignore[attr-defined]
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(fd, lock_ex | lock_nb)  # type: ignore[attr-defined]
            return
        except OSError as error:
            if time.monotonic() >= deadline:
                raise PseudonymLockError(
                    f"别名注册锁等待超时（{timeout:.1f}s）"
                ) from error
            time.sleep(_LOCK_RETRY_INTERVAL)


def _release_lock(fd: int) -> None:
    """释放排他锁；失败只影响本进程后续加锁，不能顶替调用方已经拿到的结果。"""
    try:
        if sys.platform == "win32":  # pragma: no cover — 平台分支
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:  # pragma: no cover — 平台分支
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass


@contextmanager
def _registry_lock(lock_path: Path, *, timeout: float) -> Iterator[None]:
    """跨进程注册锁；等待期间不持任何发送许可，取消也走 finally 释放。

    锁文件与注册表分开：注册表每次整文件替换，锁必须挂在不会被替换的 inode
    上；挂了锁的文件被 `os.replace` 换掉后，第二个写者会在新 inode 上另取一把
    锁，互斥当场失效。
    """
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as error:
        raise PseudonymLockError(
            f"别名注册锁文件不可用: {lock_path}（{error}）"
        ) from error
    try:
        _acquire_lock(fd, timeout)
        try:
            yield
        finally:
            _release_lock(fd)
    finally:
        os.close(fd)


def _allocate_alias(code: _AliasCode, owner: Mapping[str, str]) -> str:
    """短码未被占用则直接用，否则按 `-1/-2/...` 顺序取第一个空闲后缀。

    只追加、不改已有别名：旧别名已经发给过模型，改动会让历史回答对不上号。
    """
    base = f"{code.prefix}{code.code}"
    if base not in owner:
        return base
    index = 1
    while f"{base}-{index}" in owner:
        index += 1
    return f"{base}-{index}"


# ── 假名化器 ──


class Pseudonymizer:
    """安装级别名注册表：生成、缓存与原子提交都在这里。

    缓存语义：已提交的别名命中零 I/O（不加锁、不读盘）；新身份登记进 dirty 批，
    首次初始化立即提交，之后在合并窗口内合并提交。窗口结束前登记的身份共享同
    一个 flush future，`alias_for` 在拿到别名前 await 它——先发后写会让进程崩溃
    后模型见过的别名成为孤儿。
    """

    def __init__(
        self,
        directory: Path | None = None,
        *,
        flush_window: float = DEFAULT_FLUSH_WINDOW,
        lock_timeout: float = LOCK_TIMEOUT_SECONDS,
        digest_fn: Callable[[bytes, str, str], str] | None = None,
    ) -> None:
        # 目录在构造期定稿（而不是每次调用求值）：同一实例的读与写必须落在
        # 同一处，运行期改 `BRIEFDESK_DATA_DIR` 只影响此后新建的实例。
        self._directory = Path(directory) if directory is not None else registry_dir()
        window = float(flush_window)
        # nan/inf 会让合并窗口的 sleep 永不醒来，超过上限则让新别名迟迟不能出境；
        # 两种都当配置错误直接抛，而不是夹到边界值上悄悄放行。
        if not math.isfinite(window) or not 0.0 <= window <= MAX_FLUSH_WINDOW:
            raise ValueError(
                f"合并窗口必须是 0 到 {MAX_FLUSH_WINDOW:g} 秒之间的有限值: {flush_window!r}"
            )
        self._flush_window = window
        self._lock_timeout = max(0.0, float(lock_timeout))
        self._digest_fn = digest_fn
        self._registry: _Registry | None = None
        self._committed: dict[str, str] = {}
        self._pending: dict[str, _AliasCode] = {}
        self._init_future: asyncio.Future[_Registry] | None = None
        self._init_task: asyncio.Task[None] | None = None
        self._flush_future: asyncio.Future[None] | None = None
        self._flush_task: asyncio.Task[None] | None = None
        self._commit_count = 0

    @property
    def registry_file(self) -> Path:
        """注册表路径（用户数据目录下，不入仓库、不进导出）。"""
        return self._directory / REGISTRY_FILE_NAME

    @property
    def lock_file(self) -> Path:
        """跨进程锁文件路径（与注册表分离，见 `_registry_lock` 的说明）。"""
        return self._directory / LOCK_FILE_NAME

    @property
    def commit_count(self) -> int:
        """已成功落盘的别名批次数（诊断与测试观测用）。"""
        return self._commit_count

    async def alias_for(self, kind: str, identity_key: str, display_name: str) -> str:
        """取身份对应的别名；空显示名原样返回空串（不登记）。

        新身份返回前一定已经提交成功：失败抛 PseudonymError，调用方必须把它当
        准备失败处理（不发送真名、不重试明文）。

        注册表加载走 `_ensure_registry`（写线程 + 共享初始化 future），不在协程
        线程里读盘或抢锁；已注册身份命中 `_committed` 时零 I/O。
        """
        if not display_name:
            return ""
        if kind not in _KINDS:
            raise ValueError(f"未知的假名化实体类型: {kind!r}（合法值: person/group）")
        key = identity_key.strip()
        if not key:
            raise ValueError("身份键为空：调用方必须先生成身份键再取别名")
        registry = await self._ensure_registry()
        digest = self._digest(kind, key, registry.salt)
        cached = self._committed.get(digest)
        if cached is not None:
            self._verify_alias_kind(cached, _ALIAS_PREFIXES[kind])
            return cached
        prefix = _ALIAS_PREFIXES[kind]
        code = _AliasCode(prefix, _short_code(digest))
        rounds = 0
        while digest not in self._committed:
            if rounds >= _MAX_FLUSH_ROUNDS:
                raise PseudonymError("别名提交未在预期轮次内完成，已停止出站")
            self._pending.setdefault(digest, code)
            # shield：本调用被取消不应该连带取消别人共享的 flush future。
            await asyncio.shield(self._start_flush(immediate=rounds > 0))
            rounds += 1
        return self._committed[digest]

    # ── 注册表加载 ──

    async def _ensure_registry(self) -> _Registry:
        """取注册表；首次加载在写线程里跑，并发调用共享同一个初始化 future。

        为什么不能直接在协程里 `_load()`：它会读盘，首次启动还要抢跨进程锁
        （最长等 10 秒）。同步执行会把整个事件循环连同其它请求一起卡住，而
        `alias_for` 正处在出境前的准备路径上，卡住的代价是整条管道停摆。
        共享 future 是为了让并发首启只建一次表：各自建表会互相覆盖盐。
        """
        if self._registry is not None:
            return self._registry
        loop = asyncio.get_running_loop()
        future = self._init_future
        if future is None or future.get_loop() is not loop or future.done():
            # 没有在飞的初始化，或上一轮绑在已关闭的 loop 上（客户端缓存被清、
            # 测试换 loop）：按当前 loop 重新发起。失败不会被缓存，故可重试。
            future = loop.create_future()
            # 所有等待者都被取消时没人取异常，事件循环回收时会告警；先消费掉。
            future.add_done_callback(_consume_future_exception)
            self._init_future = future
            self._init_task = loop.create_task(self._init_registry(future))
        # shield：本调用被取消不应该连带取消别人共享的初始化。
        return await asyncio.shield(future)

    async def _init_registry(self, future: asyncio.Future[_Registry]) -> None:
        """在写线程里加载注册表，把结果或异常交给所有等待者。

        失败只落到 future 上，不写进 `_registry` 也不留半成品：半成品（有盐、
        缺映射）会让后续请求拿着不完整的表算出与历史不一致的别名。
        """
        try:
            registry = await asyncio.to_thread(self._load)
        except asyncio.CancelledError:
            if not future.done():
                future.cancel()
            raise
        except PseudonymError as error:
            if not future.done():
                future.set_exception(error)
        except Exception as error:  # noqa: BLE001 — 等待者必须都收到失败
            if not future.done():
                future.set_exception(
                    PseudonymError(f"别名注册表初始化失败（{type(error).__name__}）")
                )
        else:
            if not future.done():
                future.set_result(registry)
        finally:
            # 只清自己那一轮：新一轮已经换上新 future 时不能把人家清掉
            if self._init_future is future:
                self._init_future = None
                self._init_task = None

    def _load(self) -> _Registry:
        """读取注册表（每进程一次）；文件不存在时在锁内创建含新盐的初始表。

        盐必须在任何哈希计算之前落定。两个进程同时首次启动时若各自生成盐再各自
        提交，两边的映射会互相覆盖——先提交的一方别名在重启后全部失效。因此
        「创建初始表」也在锁内完成，且创建后立刻原子写盘。

        区分「文件不存在」与「读取失败」靠异常类型，不用 `Path.exists()` 预判：
        预判与真正的读取之间有窗口，文件在这期间被写坏会被当成「不存在」而直接
        被新表覆盖，等于一次静默重盐。读到的既有映射同时填进内存缓存。
        """
        if self._registry is not None:
            return self._registry
        path = self.registry_file
        try:
            return self._adopt(_read_registry(path))
        except _RegistryMissing:
            pass
        with _registry_lock(self.lock_file, timeout=self._lock_timeout):
            try:
                return self._adopt(_read_registry(path))
            except _RegistryMissing:
                registry = _Registry(salt=os.urandom(_SALT_SIZE), aliases={})
                _atomic_write(path, _serialize_registry(registry))
                return self._adopt(registry)

    def _adopt(self, registry: _Registry) -> _Registry:
        """把读到的注册表定为进程内快照，并把已有映射填进内存缓存。

        已注册身份（盘上已有 digest）此后命中 `_committed` 纯内存，不再读盘、
        不再抢锁；不填这一步，重启后每个已知身份都要走一遍完整提交路径。
        """
        self._registry = registry
        for digest, alias in registry.aliases.items():
            self._committed.setdefault(digest, alias)
        return registry

    # ── 批量提交 ──

    def _start_flush(self, *, immediate: bool) -> asyncio.Future[None]:
        """取得/创建当前批次的 flush future；同一批次只落盘一次。"""
        loop = asyncio.get_running_loop()
        current = self._flush_future
        if current is not None:
            if not current.done() and current.get_loop() is loop:
                return current
            # 事件循环被重建（关闭客户端缓存、测试换 loop）后，旧 future 绑在已
            # 关闭的 loop 上，复用它会让 await 直接抛 RuntimeError。丢弃旧的、
            # 按当前 loop 重新开始；旧批次的身份仍在 dirty 里，会随新批次落盘。
            self._flush_future = None
            self._flush_task = None
        future: asyncio.Future[None] = loop.create_future()
        # 所有等待者都被取消时没人取异常，事件循环会在回收时报
        # "Future exception was never retrieved"；这里先消费掉。
        future.add_done_callback(_consume_future_exception)
        self._flush_future = future
        self._flush_task = loop.create_task(self._flush_batch(immediate=immediate))
        return future

    async def _flush_batch(self, *, immediate: bool) -> None:
        """等合并窗口 → 在写线程里加锁重读、分配、原子写 → 唤醒所有等待者。

        顺序不能颠倒：只要有一个等待者拿到别名，这个别名就必须已经在盘上。
        """
        future = self._flush_future
        if future is None:  # 理论上不可达：任务只由 _start_flush 创建
            return
        try:
            if not immediate and self._commit_count > 0 and self._flush_window > 0:
                # 合并窗口：把这段时间内新登记的身份并进同一次落盘。首次初始化
                # （_commit_count == 0）跳过等待，让本批别名立刻可用。
                await asyncio.sleep(self._flush_window)
            batch = dict(self._pending)
            if batch:
                produced = await asyncio.to_thread(self._commit_sync, batch)
                for digest, alias in produced.items():
                    self._committed[digest] = alias
                    self._pending.pop(digest, None)
                self._commit_count += 1
            if not future.done():
                future.set_result(None)
        except asyncio.CancelledError:
            if not future.done():
                future.cancel()
            raise
        except PseudonymError as error:
            # 失败不发布：dirty 批整体丢弃，下一次登记重新分配并重试提交。
            self._pending.clear()
            if not future.done():
                future.set_exception(error)
        except Exception as error:  # noqa: BLE001 — 任何异常都必须落到 future 上
            self._pending.clear()
            if not future.done():
                # 只带异常类型：原始错误文本可能夹带真名、路径或 SDK 请求片段
                future.set_exception(
                    PseudonymError(f"别名提交失败（{type(error).__name__}）")
                )
        finally:
            # 只清自己那一轮：窗口内新批次可能已经换上新的 future，旧批次结束
            # 时无条件清空会把新批次的 future 丢掉，等待它的调用方永远等不到。
            if self._flush_future is future:
                self._flush_future = None
                self._flush_task = None

    def _commit_sync(self, batch: dict[str, _AliasCode]) -> dict[str, str]:
        """在写线程里：持锁重读磁盘 → 合并分配 → 原子写 → 返回本批别名。"""
        if not batch:
            return {}
        registry = self._load()
        path = self.registry_file
        with _registry_lock(self.lock_file, timeout=self._lock_timeout):
            disk = _read_registry(path)
            if disk.salt != registry.salt:
                # 盐被换掉（外部替换或恢复到另一份注册表）：继续用进程内盐写会
                # 把两份不兼容的映射混进一个文件，只能停。
                raise PseudonymRegistryError(
                    "别名注册表的盐与进程内已用盐不一致，已停止出站"
                )
            merged = dict(disk.aliases)
            owner = {alias: digest for digest, alias in merged.items()}
            produced: dict[str, str] = {}
            changed = False
            for digest, code in batch.items():
                known = merged.get(digest)
                if known is not None:
                    # 既有别名必须与本次请求的实体类型同前缀，否则是篡改或串域
                    self._verify_alias_kind(known, code.prefix)
                    produced[digest] = known
                    continue
                alias = self._committed.get(digest)
                if alias is not None:
                    self._verify_alias_kind(alias, code.prefix)
                if alias is None or owner.get(alias, digest) != digest:
                    alias = _allocate_alias(code, owner)
                merged[digest] = alias
                owner[alias] = digest
                produced[digest] = alias
                changed = True
            if changed:
                _atomic_write(
                    path,
                    _serialize_registry(_Registry(salt=disk.salt, aliases=merged)),
                )
        return produced

    def _verify_alias_kind(self, alias: str, prefix: str) -> None:
        """既有别名必须与本次请求的实体类型同前缀，否则按注册表损坏处理。

        注册表只存 digest → 别名，kind 不在文件里；不复核前缀的话，被篡改或
        串域的条目会把「群」别名当成某个人的名字发出去，还原时还会互相串域。
        """
        if not _ALIAS_SHAPE_RE.fullmatch(alias) or not alias.startswith(prefix):
            raise PseudonymRegistryError(
                f"别名注册表含与实体类型不符的别名条目: {self.registry_file}"
            )

    def _digest(self, kind: str, identity_key: str, salt: bytes) -> str:
        """摘要：默认 HMAC-SHA256；测试可注入以强制短码碰撞。

        注入的摘要也要复核形态：注册表键按 64 位小写十六进制校验，形态不对的
        摘要一旦写盘，下次启动就读不进来（等于把整个安装锁死）。
        """
        if self._digest_fn is not None:
            digest = self._digest_fn(salt, kind, identity_key)
        else:
            digest = _default_digest(salt, kind, identity_key)
        if not _DIGEST_RE.fullmatch(digest):
            raise PseudonymError("别名摘要形态不合法，已停止出站")
        return digest


def _consume_future_exception(future: asyncio.Future[Any]) -> None:
    """消费 future 的异常（等待者全被取消时没人取，会打未取回告警）。"""
    if not future.cancelled():
        future.exception()


# ── 进程级单例 ──

_instances: dict[Path, Pseudonymizer] = {}


def get_pseudonymizer() -> Pseudonymizer:
    """取当前数据目录对应的进程级实例（按路径缓存，路径变了就新建一个）。

    缓存实例而不是模块级常量：注册表路径要跟随运行期的数据目录覆盖，常量会在
    import 期把路径钉死。
    """
    directory = registry_dir()
    instance = _instances.get(directory)
    if instance is None:
        instance = Pseudonymizer(directory)
        _instances[directory] = instance
    return instance


def reset_for_tests() -> None:
    """清空进程级实例缓存（测试隔离用；生产不调用）。"""
    _instances.clear()


# ── 请求级上下文 ──

# 完整 token 的两侧必须是「非 ASCII 词字符」边界。被拼长的 `同学ABCDEFG` 若只在
# 中间匹配，会把 `同学ABCDEF` 单独还原、留下孤零零的 `G`，既改了模型输出又
# 不计失败。不能用 `\w`：它把汉字也算单词字符，中文紧邻时（`由同学ABCDEF发布`）
# 就再也匹配不到。
_TOKEN_LEFT_BOUNDARY = r"(?<![A-Za-z0-9_-])"
_TOKEN_RIGHT_BOUNDARY = r"(?![A-Za-z0-9_-])"
_ALIAS_TOKEN_RE = re.compile(
    rf"{_TOKEN_LEFT_BOUNDARY}{_ALIAS_SHAPE_PATTERN}{_TOKEN_RIGHT_BOUNDARY}"
)
# 缺前缀的裸短码：模型有时会把「同学ABCDEF」抄成「ABCDEF」或「ABCDEF-1」。只用来
# 判「被改写过」并计数，不参与替换——凭一段裸码反查身份会把同码的另一个人也认进来。
# 右侧允许 `-`：带后缀的裸码同样是被改写，旧口径的 `(?![A-Za-z0-9_-])` 会把它漏掉。
_BARE_CODE_RE = re.compile(
    rf"(?<!{ALIAS_PERSON_PREFIX})(?<!{ALIAS_GROUP_PREFIX})"
    rf"(?<![A-Za-z0-9_-])({_CODE_PATTERN})(?![A-Za-z0-9_])"
)
# 前缀出现位置：用来逐个判断「前缀后面跟的是什么」，比一条大正则更容易说明
# 每一种被改写的方式。
_ALIAS_PREFIX_SEARCH_RE = re.compile(rf"{ALIAS_PERSON_PREFIX}|{ALIAS_GROUP_PREFIX}")
# 前缀后紧跟的 ASCII 串：用于判断「长得像别名但不合法」。4 是下限——`同学3人`
# 这类自然行文只有 1 个字符，按候选计会把遥测淹没在普通中文里。
_CANDIDATE_TAIL_RE = re.compile(r"[A-Za-z0-9_-]{4,}")
# 纯 base32 的 4~5 位串：像被截断的短码（`同学ABCD`）。含小写或 0/1/8/9 的串由
# 「长度 ≥ 短码长度才算候选」那条覆盖，不必在这里逐类穷举。
_TRUNCATED_CODE_RE = re.compile(rf"[{_BASE32_CHARS}]{{4,5}}")
_ASCII_TOKEN_CHAR_RE = re.compile(r"[A-Za-z0-9_-]")


class NameContext:
    """请求级姓名上下文：准备别名、查逆映射、标记还原失败。

    生命周期只有一次请求：不落盘、不进日志、不跨请求复用。`__repr__` 只暴露
    计数，避免异常栈或调试输出把真名带出去。
    """

    def __init__(
        self, pseudonymizer: Pseudonymizer | None = None, *, enabled: bool = True
    ) -> None:
        self._pseudonymizer = pseudonymizer
        self._enabled = enabled
        # 别名 → 显示名 / 别名 → kind：逆映射只在请求内存里
        self._by_alias: dict[str, str] = {}
        self._alias_kind: dict[str, str] = {}
        self._codes: set[str] = set()
        # 身份键 → 别名：同一身份在本请求内重复出现不再走身份键计算
        self._person_cache: dict[str, str] = {}
        self._group_cache: dict[str, str] = {}
        self._failed_fields: set[str] = set()
        # 字段 → 上次还原输出：同一字段重复调用时据此短路，避免二次替换
        self._last_output: dict[str, str] = {}
        self._restore_failures = 0

    def __repr__(self) -> str:
        return (
            f"<NameContext aliases={len(self._by_alias)} "
            f"enabled={self._enabled} restore_failures={self._restore_failures}>"
        )

    @property
    def enabled(self) -> bool:
        """本请求是否真的在做假名化（关闭时别名与还原都原样透传）。"""
        return self._enabled

    @property
    def restore_failures(self) -> int:
        """本请求累计的还原失败次数（按字段去重后）。"""
        return self._restore_failures

    async def person_alias(
        self,
        *,
        source: str,
        sender_id: str,
        session_id: str,
        display_name: str,
    ) -> str:
        """准备一个人的出站显示名；空显示名原样返回空串。"""
        if not self._enabled:
            return display_name
        name = (display_name or "").strip()
        if not name:
            return ""
        key = person_identity_key(
            source=source,
            sender_id=sender_id,
            session_id=session_id,
            display_name=name,
        )
        cached = self._person_cache.get(key)
        if cached is not None:
            # 同一身份出现多个显示名时，用请求内最后一个非空显示名做还原目标。
            self._remember(cached, name, KIND_PERSON)
            return cached
        alias = await self._alias(KIND_PERSON, key, name)
        self._person_cache[key] = alias
        return alias

    async def group_alias(
        self, *, source: str, session_id: str, group_name: str
    ) -> str:
        """准备一个群的出站显示名；空群名原样返回空串。"""
        if not self._enabled:
            return group_name
        name = (group_name or "").strip()
        if not name:
            return ""
        key = group_identity_key(
            source=source, session_id=session_id, group_name=name
        )
        cached = self._group_cache.get(key)
        if cached is not None:
            self._remember(cached, name, KIND_GROUP)
            return cached
        alias = await self._alias(KIND_GROUP, key, name)
        self._group_cache[key] = alias
        return alias

    def display_name_for(self, alias: str, *, kind: str | None = None) -> str | None:
        """查别名对应的显示名；kind 不匹配视为未知（人/群别名分域）。"""
        name = self._by_alias.get(alias)
        if name is None:
            return None
        if kind is not None and self._alias_kind.get(alias) != kind:
            return None
        return name

    def mark_restore_failed(self, field: str) -> None:
        """标记一次还原失败并计数；同一字段重复兜底不重复计数。

        每请求每字段只记一次：同一次还原失败会被多个兜底步骤看到，按次计会把
        「一个字段没还原出来」放大成多条遥测，掩盖真实规模。
        """
        if field in self._failed_fields:
            return
        self._failed_fields.add(field)
        self._restore_failures += 1
        telemetry.record("ai.pseudonym.restore_failed")

    def restore(self, text: str, *, field: str) -> str:
        """把自由文本中的已知别名替换回显示名（单次请求内幂等）。

        只做完整 token 的精确替换：token 两侧必须是 ASCII 词字符之外的边界，
        中文紧邻照常匹配；出现「长得像别名但不合法」的片段（被拼长、被改成
        小写、后缀不是纯数字、缺前缀的裸码）时原样保留并计一次失败。不模糊
        猜测——凭猜测替换会把无关文本改成某个人名，而原样保留至少看得见问题。

        幂等只在单次请求内成立（`restore(restore(x)) == restore(x)`）：每字段
        记住上次输出，输入与它相同就直接返回。真实显示名可能本身形似别名（有人
        就叫「同学ABCDEF」），没有这层记忆时第二次还原会把刚写回去的真名当成
        别名再换一次，字段在多次兜底之间就漂了。失败计数按字段去重，重复调用
        不会把一次失败放大成多条遥测。
        """
        if not text or not self._enabled or not self._by_alias:
            return text
        if self._last_output.get(field) == text:
            return text

        def _substitute(match: re.Match[str]) -> str:
            token = match.group(0)
            name = self._by_alias.get(token)
            if name is None:
                self.mark_restore_failed(field)
                return token
            return name

        restored = _ALIAS_TOKEN_RE.sub(_substitute, text)
        if field not in self._failed_fields and self._looks_like_broken_alias(text):
            self.mark_restore_failed(field)
        self._last_output[field] = restored
        return restored

    def _looks_like_broken_alias(self, text: str) -> bool:
        """文本里是否存在「形似别名但不合法」的片段（只判定，不改写）。

        逐项列出被改写的方式：前缀后的 ASCII 串长度不对（被拼长或截断）、含有
        小写或 base32 之外的字符、带非数字后缀，以及丢了前缀但在本请求里已知的
        裸短码。任何一项命中都说明模型抄错了别名，调用方按「没还原出来」处理。
        """
        for match in _ALIAS_PREFIX_SEARCH_RE.finditer(text):
            start = match.start()
            if start > 0 and _ASCII_TOKEN_CHAR_RE.match(text[start - 1]):
                # 前缀被粘在 ASCII 词中间（`X同学...`）：不是别名起点
                continue
            tail = _CANDIDATE_TAIL_RE.match(text, match.end())
            if tail is None:
                continue
            span = text[start : tail.end()]
            if span in self._by_alias:
                # 完整且已知的别名由替换路径处理，不算失败
                continue
            run = tail.group(0)
            if len(run) >= ALIAS_CODE_LENGTH or _TRUNCATED_CODE_RE.fullmatch(run):
                return True
        for match in _BARE_CODE_RE.finditer(text):
            if match.group(1) in self._codes:
                # 化了名却丢了前缀：同样是没还原出来，原样保留
                return True
        return False

    # ── 内部 ──

    async def _alias(self, kind: str, identity_key: str, name: str) -> str:
        """取别名；失败计一次准备失败再抛出（调用方据此走失败路径）。"""
        pseudonymizer = self._pseudonymizer or get_pseudonymizer()
        try:
            alias = await pseudonymizer.alias_for(kind, identity_key, name)
        except PseudonymError:
            telemetry.record("ai.pseudonym.prepare_failed")
            raise
        if alias:
            self._remember(alias, name, kind)
        return alias

    def _remember(self, alias: str, name: str, kind: str) -> None:
        """记下逆映射（同名重复出现时用最后一个非空显示名覆盖）。"""
        if not alias or not name:
            return
        self._by_alias[alias] = name
        self._alias_kind[alias] = kind
        prefix = _ALIAS_PREFIXES[kind]
        if alias.startswith(prefix):
            code = alias[len(prefix):].split("-", 1)[0]
            if len(code) == ALIAS_CODE_LENGTH:
                self._codes.add(code)
