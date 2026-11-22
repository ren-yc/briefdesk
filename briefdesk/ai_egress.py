"""出境可观测性 — 本地端点纯判定，以及发送、排队、SDK 重试的计数助手。

为什么单独成模块：`ai_provider` 引擎要在「真正发出请求」的前一刻打点，而
「这个端点算不算出境」是一个可以脱离引擎单测的纯函数；把判定、元数据与指标名
收在一处，读数口径才不会在 chat / rag / embed 三个端口之间漂移。

关键约束与失败模式：

- 本地判定只看 URL 与 IP 字面量，**不解析 DNS**：解析会把一次离线判定变成网络
  调用，还会让「域名现在指向 127.0.0.1」这种临时状态决定是否把真名发出去。认不出
  的一律按远端处理（fail-closed）——判错方向时多假名化一次，远好过多发一次真名。
- 指标名只由固定词表拼成（端口与角色），标签里不出现 URL、Key、人名、群名、
  别名或各类 ID：这些值进指标会变成高基数标签，也会把敏感数据带进导出的读数。
- 计数在「逻辑发送」边界，不在端口调用边界：排队超时、排队取消、替身短路都不
  应该算作出境尝试。因此排队失败单独进 `ai.queue.*`，与发送桶分开。
- 遥测关闭时这些助手什么都不记（`telemetry.record` 自带开关）：报告侧必须把
  「关闭」读成未知，不能把空计数器当成零出境。
- 政策阻断不在这里实现：本模块只保留 `BLOCKED_METRIC_TEMPLATE` 这个指标名形态，
  没有任何递增路径。阻断属于独立决策，凭空加一个非零的 blocked 计数会把「没
  发出去」和「不允许发出去」混成一个数。
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import NamedTuple
from urllib.parse import urlsplit

from briefdesk import telemetry

PORT_CHAT = "chat"
PORT_RAG = "rag"
PORT_EMBED = "embed"
PORTS = frozenset({PORT_CHAT, PORT_RAG, PORT_EMBED})

ROLE_MAIN = "main"
ROLE_JUDGE = "judge"
ROLE_GEN = "gen"
ROLE_CLASSIFY = "classify"
ROLES = frozenset({ROLE_MAIN, ROLE_JUDGE, ROLE_GEN, ROLE_CLASSIFY})

# 本地宿主名白名单：只有这一个域名，且必须是大小写无关的完整匹配。
_LOCAL_HOSTNAME = "localhost"
_LOCAL_SCHEMES = frozenset({"http", "https"})
# 0.0.0.0 不是环回（is_loopback 为假），但本机监听常以它作绑定地址，按本地处理。
_UNSPECIFIED_V4 = ipaddress.IPv4Address("0.0.0.0")
# C0 控制字符与 DEL：`urlsplit` 会把这些字符直接删掉（RFC 3986 的清理要求），
# 于是 `http://local\nhost` 清理后变成另一个 host，判定结果取决于「清理后是
# 什么」而不是「调用方写了什么」。必须在解析前就拒。
_C0_LIMIT = 0x20
_DEL_ORD = 0x7F

# 标签形态：`kind`/`source` 会进遥测键，只接受短小的、无空白的受控词。
# 下划线不在白名单里：`u_12345` 这类「前缀_数字」正是用户 ID 的常见写法，
# 放行它等于把 ID 原样写进遥测键。
_LABEL_RE = re.compile(r"[A-Za-z0-9.:-]{0,64}")
# 形态不符时 `kind` 归到这里，而不是清洗/截断：截断仍会留下可识别片段
# （姓名、ID 前缀），而清洗后恰好等于某个真实标签会把两个不同来源混进一个桶。
_FALLBACK_KIND = "other"

# 政策阻断的指标名形态：本模块不实现阻断，阶段性的策略决策落地之前这里没有
# 递增路径；保留字符串是为了让报告侧按同一个形状展示零值，避免两边各写一份。
BLOCKED_METRIC_TEMPLATE = "ai.egress.blocked.{port}.{role}"


class EgressMetadata(NamedTuple):
    """一次出站调用携带的遥测元数据（由端口层设置，引擎读取）。

    port/role 参与指标名，kind 是任务标签，source 是消息源标签（遥测的维度）。
    两个标签都会过 `_sanitize_label` 收敛到受控词形态：调用方拿到的是业务对象
    时可能把姓名或 ID 直接传进来，原样进键就是高基数标签加敏感数据外泄。
    默认值让「直接调用引擎」这类路径也有安全值，不会因为没设元数据就丢计数。
    """

    port: str = PORT_CHAT
    role: str = ROLE_MAIN
    kind: str = ""
    source: str = ""


_DEFAULT_METADATA = EgressMetadata()
_METADATA: ContextVar[EgressMetadata] = ContextVar(
    "briefdesk_ai_egress_metadata", default=_DEFAULT_METADATA
)


def _validate_port(port: str) -> str:
    """端口标签必须是固定词表之一：拼错会造出一个查不到的指标族。"""
    if port not in PORTS:
        legal = "/".join(sorted(PORTS))
        raise ValueError(f"未知的出境端口: {port!r}（合法值: {legal}）")
    return port


def _validate_role(role: str) -> str:
    """角色标签同上；未知角色在端口层就该拒绝，不能静默归到主通道。"""
    if role not in ROLES:
        legal = "/".join(sorted(ROLES))
        raise ValueError(f"未知的出境角色: {role!r}（合法值: {legal}）")
    return role


def _dimensions(port: str, role: str) -> str:
    """指标名后缀 `<port>.<role>`。"""
    return f"{_validate_port(port)}.{_validate_role(role)}"


def _sanitize_label(value: str, *, fallback: str) -> str:
    """把 `kind`/`source` 收敛成受控词形态：形态不符就整值换成默认桶。

    这是**形态过滤，不是受控词表**：只判定「有没有空白/中文/下划线、长度是否
    超限」，不判断取值是不是某个具体的插件名或任务标签。因此 `member-0001`
    这种全小写、带连字符、形似用户 ID 的值会原样通过；接线的调用方必须自己从
    插件名或固定任务标签取维度，不能把业务对象（人名、群名、ID）直接传进来。
    本函数不承诺挡住所有身份信息，只保证「形态异常的整值不会被截断成可识别
    片段」——截断后的残片仍可能认出是谁。
    """
    if not isinstance(value, str):
        return fallback
    if _LABEL_RE.fullmatch(value):
        return value
    return fallback


def _current_source() -> str:
    """当前上下文的遥测维度（未设元数据时为空串，与既有遥测口径一致）。"""
    return _METADATA.get().source


@contextmanager
def use_metadata(
    *, port: str, role: str, kind: str = "", source: str = ""
) -> Iterator[EgressMetadata]:
    """在 with 块内设置出境元数据，退出时恢复原值（可嵌套）。

    ContextVar 而不是全局变量：并发请求各自携带不同的端口/源标签，共享一个
    全局值会让一个请求的计数记到另一个请求的源上；恢复用 reset(token) 而不是
    重设旧值，才能正确回到外层的原始上下文。

    `kind`/`source` 在这里收敛：调用方偶尔会把姓名、群名或 ID 当标签传进来，
    而它们最终会出现在遥测键里。不合形态的整值归到默认桶（`kind` → `other`、
    `source` → 空串），不做截断或字符替换。
    """
    metadata = EgressMetadata(
        port=_validate_port(port),
        role=_validate_role(role),
        kind=_sanitize_label(kind, fallback=_FALLBACK_KIND),
        source=_sanitize_label(source, fallback=""),
    )
    token = _METADATA.set(metadata)
    try:
        yield metadata
    finally:
        _METADATA.reset(token)


def current_metadata() -> EgressMetadata:
    """读取当前上下文的元数据（未设置时返回安全默认值）。"""
    return _METADATA.get()


# ── 本地端点判定 ──


def _has_control_chars(text: str) -> bool:
    """是否含 C0 控制字符或 DEL（含 `\\n`/`\\r`/`\\t` 与 NUL）。"""
    return any(ord(char) < _C0_LIMIT or ord(char) == _DEL_ORD for char in text)


def _host_from_base(base: str) -> str | None:
    """从 base URL 取出小写 host；不合法的 base 返回 None（由调用方按远端处理）。"""
    raw = base if isinstance(base, str) else ""
    if not raw:
        return None
    if _has_control_chars(raw):
        # 解析前拒：`urlsplit` 会删掉这些字符，`http://local\nhost` 清理后是
        # `localhost`，按清理结果判本地等于让调用方用一个「看得见的畸形串」换
        # 一个「判成本地」的结论。
        return None
    candidate = raw.strip()
    if not candidate:
        return None
    try:
        parts = urlsplit(candidate)
    except ValueError:
        # 畸形方括号 IPv6 等在这里就抛；不能靠宽松解析把它当成本地
        return None
    if parts.scheme.lower() not in _LOCAL_SCHEMES:
        return None
    try:
        # 凭据段一律按远端：`http://user:pass@localhost` 的 host 虽然也是
        # localhost，但这类写法只会是手工构造，不猜它想连哪里；`http://@127.0.0.1`
        # 的空 userinfo 同样可疑，用 `is not None` 连空串一起拒。
        if parts.username is not None or parts.password is not None:
            return None
        # 访问 port 才会真正解析端口：越界或非数字都会抛 ValueError。
        # 非法端口不猜（如 `:99999` 可能被截断成别的端口），一律按远端。
        _ = parts.port
    except ValueError:
        return None
    try:
        host = parts.hostname
    except ValueError:
        return None
    if not host:
        return None
    return host


def _is_local_host(host: str) -> bool:
    """host 是否明确指向本机；域名式 host 一律远端（不做 DNS 推断）。"""
    if host == _LOCAL_HOSTNAME:
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return bool(address.is_loopback or address == _UNSPECIFIED_V4)


def is_local_endpoint(base: str) -> bool:
    """判定 base 是否本机端点（纯 URL/IP 解析，无网络、无 DNS）。

    本地：`localhost`（大小写无关）、127.0.0.0/8、`::1`、`0.0.0.0`，且必须是
    合法 http(s) 且端口合法。其余全部按远端：空串、畸形 URL、含控制字符（解析
    器会把它们删掉，清理后的 host 不能代表调用方写的 URL）、带凭据段或空
    userinfo（`http://user:pass@localhost`、`http://@127.0.0.1`）、非法端口、
    私网地址（10/8、172.16-31、192.168/16 不等于本机）、`localhost.example.invalid`
    这类近似域名、`localhost@remote` 这类把主机藏在凭据段里的写法。
    """
    host = _host_from_base(base)
    if host is None:
        return False
    return _is_local_host(host)


# ── 计数助手 ──


def record_send_attempt(port: str, role: str, base: str) -> bool:
    """在真正发出一次逻辑发送之前计数；返回该端点是否本地。

    调用点必须是「已经拿到许可、即将 create」的位置：排队失败、替身短路、参数
    校验失败都不该走到这里。返回本地判定是为了让调用方复用同一次解析结果，
    避免判定与计数各算一遍而给出不一致的结论。
    """
    dimensions = _dimensions(port, role)
    source = _current_source()
    if is_local_endpoint(base):
        telemetry.record(f"ai.local.attempt.{dimensions}", source)
        return True
    telemetry.record(f"ai.egress.remote_attempt.{dimensions}", source)
    return False


def record_queue_timeout(port: str, role: str) -> None:
    """排队阶段超时（一级/二级都算）：只有排队失败，没有发送尝试。"""
    telemetry.record(f"ai.queue.timeout.{_dimensions(port, role)}", _current_source())


def record_queue_cancelled(port: str, role: str) -> None:
    """排队阶段被取消：同样不计发送尝试（请求根本没发出去）。"""
    telemetry.record(
        f"ai.queue.cancelled.{_dimensions(port, role)}", _current_source()
    )


def record_sdk_retry(port: str, role: str, is_local: bool) -> None:
    """SDK 内部重试一次：与逻辑发送尝试分桶，不改动尝试计数。

    逻辑尝试是「调用方发起了几次 create」，SDK 重试是「SDK 内部又发了几次」；
    两者混在一起会让「一次 create 被重试两次」看起来像三次出境请求。
    """
    prefix = "ai.local.sdk_retry" if is_local else "ai.egress.sdk_retry"
    telemetry.record(f"{prefix}.{_dimensions(port, role)}", _current_source())
