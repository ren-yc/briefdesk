"""两个消息源插件共享的身份状态机（就绪门控）。

**与 vendor SDK 的关系**：`weflow_sdk` / `qqflow_sdk` 目前**仅随包分发**
（wheel/sdist 内含、装完可导入、形状有门禁断言），插件运行期**尚未消费**
它们——本模块自己用 httpx 直连上游，只复用 vendor 定下的**语义口径**
（wait-only 就绪轮询、200 拒绝态快速失败、SSE 单连接多帧等）。运行期
切换到 vendor SDK 是下一批的事，别把「随包分发」读成「在跑它的代码」。

本模块把两个插件各自的「身份状态机」收敛为一份实现：健康检查驱动的
记忆化、绑定账号身份闸门、良性态/被拒态的注册分诊，全部逻辑两个平台
同构，只有「怎么调 HTTP」不同——那部分以协作回调注入，本模块不持有
HTTP 客户端。

身份闸门的时点约定（两平台一致，**不可弱化**）：每次 SSE 实际建连成功、
交付消息之前必须强制重检（`force=True`）；周期轮询默认关闭
（POLL_INTERVAL_SECONDS=0），不能依赖「poller 每轮会检查」来兜底身份。
绑定账号与本地配置不符时抛专属 Mismatch 异常并置 offline，**禁止继续
读流**——继续读就是在收别人的消息。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# 注册端点 200 业务态里的「受理」词表（两平台一致）。
BENIGN_REGISTER_STATES = ("accepted", "in_progress", "already_ready")

# /health 的 account 阶段值中，代表"服务端已在引导、无需再注册"的那些。
BOOTSTRAPPING_PHASES = ("indexing",)

# /health 的 account 阶段值中，代表"服务端已被某个账号占用"的那些。
# weflow 与 qqflow 的 AccountPhase 都是四值（unregistered/indexing/ready/
# error），且刻意没有 awaiting_key 变体（启动扫描发现但无人注册的账号
# 折叠为 unregistered），所以这三个就是"有绑定"的全部取值。
BOUND_PHASES = ("indexing", "ready", "error")


@dataclass
class ReadinessGate:
    """健康检查驱动的就绪门控状态机（两平台共享，HTTP 细节注入）。

    协作回调（由各插件客户端注入）：

    - ``fetch_health() -> dict``：/health 响应（免鉴权）。
    - ``fetch_accounts() -> list[dict]``：账号明细（需鉴权）。
    - ``register() -> tuple[str, str | None]``：自持 POST，返回
      ``(state, status)``；conflict 由其就地抛 Mismatch（此时它持有
      occupied_by，错误消息更有诊断价值）。
    - ``log_account_errors(accounts) -> None``：error 根因诊断（仅日志）。
    - ``identity_matches(accounts) -> bool``：绑定账号比对；不符时抛
      Mismatch（原样冒泡），返回 False 表示无法确认（明细为空），调用方
      继续走注册由上游 conflict 守卫定夺。

    记忆化语义（契约测试钉死）：良性态记忆；被拒态与网络失败不记忆
    （下一轮重试，自愈）；503 由 ``reset()`` 复位（业务接口的自愈路径
    调用）。
    """

    # 协作者在调用时解析（owner.attr），而非构造时绑定：测试以
    # patch.object(client, "fetch_health", ...) 实例级打桩，绑定时机
    # 若在 __init__，桩对门控不可见。attr 名即接缝契约。
    owner: Any
    health_attr: str
    accounts_attr: str
    register_attr: str
    log_errors_attr: str
    identity_attr: str
    # 注册分诊日志的平台描述（owner.<attr> 指向无参 callable，返回
    # 一行描述文本，例如 weflow 的「注册账号 wxid=… (db_path=…, keys=N 个库)」
    # 与 qqflow 的「注册账号 qq=… (db_path=…)」）。迁移到本门控时这条
    # 「注册前 INFO」曾被整体丢失：它带的是平台各自的身份上下文，门控拿不到，
    # 只能由 owner 以回调供词——缺省 None 时回落到通用文案（测试桩可用）。
    register_desc_attr: str | None = None
    # 版本日志的平台前缀（weflow-server / qqflow-server），与既有日志口径一致
    version_label: str = "服务端"
    log: logging.Logger = field(default_factory=lambda: logger)

    def __post_init__(self) -> None:
        self._ready_checked = False
        self._lock = asyncio.Lock()
        self._logged_version: str | None = None

    def _coll(self, attr: str):
        return getattr(self.owner, attr)

    def reset(self) -> None:
        """失效记忆化（503 自愈路径 / SSE 断连时调用）。"""
        self._ready_checked = False

    @property
    def checked(self) -> bool:
        """当前记忆化标志（诊断与测试观测/白盒兼容用）。"""
        return self._ready_checked

    @checked.setter
    def checked(self, value: bool) -> None:
        self._ready_checked = value

    @property
    def logged_version(self) -> str | None:
        """已记录的服务端版本（版本变化才打 INFO；测试观测用）。"""
        return self._logged_version

    async def ensure_ready(self, force: bool = False) -> None:
        """确保服务端有就绪账号（健康检查驱动，记忆化，可强制重检）。

        先查 /health 的标量 account 阶段：ready 与 indexing 即记忆化返回，
        **不重复注册**；unregistered / error 才注册。注册后进入索引期，
        业务接口的 503 由各插件的 NotReady 瞬态处理兜底，不在此阻塞等待
        ——「索引期是否阻塞等待」是已声明行为：本门控**不等待**，需要
        等待语义的调用方应改用 vendor SDK 的 ``wait_ready``。

        force=True 时忽略记忆化标志重新健康检查（SSE 重连后服务端可能已
        重启，内存态账号注册表丢失，需重新注册）。良性态记忆；被拒态与
        网络失败不记忆，下轮重试（自愈）。

        每客户端一把锁＋锁内双检：并发调用（SSE 强制检查 vs 轮询检查）
        先到者注册并置位，后到者短路，避免重复注册。取消/超时会正常释放
        锁且不留成功标记。
        """
        if self._ready_checked and not force:
            return
        async with self._lock:
            if self._ready_checked and not force:
                return
            # force=True must invalidate the memoized success *before* the
            # first await: otherwise a force call that fails (registration
            # refusal, network error, Mismatch, cancellation) leaves the
            # stale success flag set and every later non-force call short-
            # circuits without ever re-checking - the exact opposite of
            # 'force means re-verify'. Successful force re-checks re-set
            # the flag below on the normal paths.
            if force:
                self._ready_checked = False
            try:
                health = await self._coll(self.health_attr)()
            except Exception:
                self._ready_checked = False
                raise
            version = health.get("version")
            if version and version != self._logged_version:
                self.log.info("%s 版本: %s", self.version_label, version)
                self._logged_version = str(version)
            phase = health.get("account", "unregistered")
            self.log.debug("健康检查: 账号阶段 %s", phase)

            # 身份闸门：阶段说「有绑定」时，先确认绑的是不是自己的账号。
            accounts: list[dict] = []
            identity_ok = False
            if phase in BOUND_PHASES:
                try:
                    accounts = await self._coll(self.accounts_attr)()
                except Exception as e:  # noqa: BLE001 —— 取不到身份不硬失败
                    self.log.warning("账号明细不可用（%s），改由注册结果判定身份", e)
                else:
                    # 比对留在 else：不符的 Mismatch 才能原样冒泡；挪进 try
                    # 会被宽 except 吞成一行 warning。
                    identity_ok = self._coll(self.identity_attr)(accounts)

            if identity_ok and phase == "ready":
                self._ready_checked = True
                self.log.debug("已确认自有账号就绪，跳过注册")
                return
            if identity_ok and phase in BOOTSTRAPPING_PHASES:
                self._ready_checked = True
                self.log.debug("自有账号建索引中（%s），不再重复注册", phase)
                return
            if phase == "error":
                # /health 只给标量，根因只在需鉴权的明细接口里。
                await self._coll(self.log_errors_attr)(accounts)
            # error 落到注册分支是有意的：error 不释放绑定，同一账号可直接
            # 重试注册恢复（密钥修正后即生效）。
            # 注册前 INFO（平台化描述）：两个插件迁移到本门控前各打一行
            # 「无就绪账号（阶段 X），注册账号 …」——账号身份上下文（wxid/qq/
            # db_path/keys 数）只有插件自己有，门控只能要回调供词。丢了这行，
            # 「服务端拒绝引导」在日志里就只剩一行阶段 debug，排查面回退。
            desc = None
            if self.register_desc_attr is not None:
                try:
                    desc = self._coll(self.register_desc_attr)()
                except Exception:  # noqa: BLE001 —— 供词失败绝不阻断注册
                    desc = None
            if desc:
                self.log.info("无就绪账号（阶段 %s），%s", phase, desc)
            else:
                self.log.info("无就绪账号（阶段 %s），注册账号", phase)
            state, status = await self._coll(self.register_attr)()
            # status 只在「有」时打：qqflow 面的 register 回调不带 status
            # （其健康分诊由 phase 承担），此前恒打 status 会让那侧的日志
            # 永远多一个 None——按有无 status 两种文案，恢复各平台迁移前
            # 的口径（weflow: state+status；qqflow: 仅 state）。
            detail = (f"state={state}, status={status}" if status is not None
                      else f"state={state}")
            if state in BENIGN_REGISTER_STATES or (
                status is not None and status in BOOTSTRAPPING_PHASES
            ):
                self._ready_checked = True
                self.log.info("账号注册: %s", detail)
            else:
                # 被拒态不记忆化：保持未检查标志让下一轮重试；否则零账号
                # 部署下没有业务 503 兜底复位标志，引导失败后永不自愈。
                self.log.warning("账号注册被拒: %s（下轮重试）", detail)
