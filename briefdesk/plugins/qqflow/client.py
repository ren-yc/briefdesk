"""qqflow-server HTTP API 客户端 — 封装所有与 qqflow-server 的通信细节。

用法:
    client = QqFlowClient(base_url="http://127.0.0.1:5032", api_token="xxx",
                          qq="123", key="...", db_path="...")
    await client.ensure_ready()          # 健康检查 + 可选账号引导注册
    sessions = await client.fetch_sessions()
    resp = await client.fetch_messages(talker="...", start=..., limit=500)
    async for event in client.stream_events():
        ...
"""

import asyncio
import json
import logging
import time as time_module
from collections.abc import AsyncIterator
from typing import Any, Literal, TypedDict

import httpx

from briefdesk.logger import fmt_dur
from briefdesk.masking import clean_display_name
from briefdesk.plugins.qqflow.config import QqFlowSettings
from briefdesk.sources_base import (
    ConnectionStatus,
    MediaError,
    SourceClient,
    SourceError,
    fetch_all_pages,
    make_sse_timeout,
    with_connect_retry,
)

# ── qqflow-server API 数据类型 ──


class QqFlowEvent(TypedDict):
    """SSE 推送的原始事件（ready / message.new / message.revoke / sync / ping）。

    ready 为连接建立基线（上游对齐 WeFlow 契约，v0.3.x 起），载荷实测为
    `{"status":"ok"}`——**不含 event 键**（事件名只在 SSE 的 `event:` 帧头，
    本客户端只解析 data 行故读不到），监听器按空 etype 兜底跳过。
    """

    event: str
    sessionId: str
    sessionType: Literal["group", "private"]
    groupName: str  # 仅群聊；缺失时省略
    rawid: str  # 消息 rowid（字符串），跨路径去重键
    sourceName: str  # 发送者昵称；缺失时省略
    content: str
    timestamp: int  # 秒级 Unix
    media: dict | None  # 图片/语音/视频的结构化媒体元数据（无路径视图，上游
    # 推送不下发 localPath）；缺失时省略
    mediaId: str | None  # 媒体获取键（md5 hex 或 uuid），仅当索引注册了可读取
    # 的本地缓存时提供（与 REST messages.mediaId 同一规则）；缺失时省略


class QqFlowSession(TypedDict):
    """会话（群聊或私聊）。"""

    username: str  # 会话 id：群=群号，私聊=u_ 前缀 UID
    displayName: str
    type: int  # 2 = 群聊, 1 = 私聊
    lastTimestamp: int
    unreadCount: int


class QqFlowContact(TypedDict):
    """联系人（来自消息中出现过的 UID）。"""

    username: str
    displayName: str
    nickname: str
    remark: str
    alias: str
    avatarUrl: str
    type: str


class QqFlowMessage(TypedDict):
    """REST API 返回的消息。"""

    localId: int  # rowid 数字 —— msg_id 来源（与 SSE rawid 同值）
    serverId: str  # seq 字符串（与 rowid 无关，不使用）
    localType: int  # 0文本 / 1其他 / 3图片 / 4语音 / 5视频 / 6撤回 / 7系统
    createTime: int  # 秒级 Unix
    isSend: int  # 方向（上游 40013 列）：1=本人发送；QQ 版本缺列或值非 1/2 时恒 0
    senderUsername: str  # 发送者 UID（稳定去重键）
    senderName: str  # 上游已解析的发送者显示名：本会话群名片(40090) > 备注
    # (20009) > 最新消息昵称(40093) > 档案昵称(20002) > UID。与 SSE sourceName
    # 同值。senderUsername 非空即非空 → 无需再拼 contacts/group-members 映射
    content: str  # 解析后文本（媒体消息为 [image] 等占位符）
    rawContent: str
    parsedContent: str
    mediaType: str  # 仅 image/voice/video 消息
    media: dict | None  # 结构化媒体元数据（同 SSE media 对象形状）；缺失时省略
    mediaId: str | None  # 媒体存储键（md5 hex 小写或 uuid），仅当上游索引注册了
    # 可解析的本地缓存路径时提供（fetch 不 404 的保证）
    mediaFileName: str | None  # media=1 导出字段（本仓库未使用）
    mediaUrl: str | None  # media=1 导出字段（本仓库未使用）
    mediaLocalPath: str | None  # media=1 导出字段（本仓库未使用）


class QqFlowMessagesResponse(TypedDict):
    """GET /api/v1/messages 的完整信封（含翻页信息）。"""

    success: bool
    talker: str
    count: int
    hasMore: bool
    messages: list[QqFlowMessage]


class QqFlowContactsResponse(TypedDict):
    contacts: list[QqFlowContact]


logger = logging.getLogger(__name__)

# 媒体下载大小上限：防止异常/恶意上游返回超大文件造成内存放大
_MAX_MEDIA_BYTES = 20 * 1024 * 1024

# 注册接口（POST /api/v1/accounts）返回的良性 state：已受理/引导中，视为可
# 记忆，索引期内不重复注册。这是**注册结果**的词表，与 /health 的 account
# 阶段值（unregistered/indexing/ready/error）是两套不相交的枚举 —— 曾经把它
# 拿去比对健康状态，那个分支恒为假，"索引中不重复注册"的优化从未生效。
_BENIGN_STATES = ("accepted", "in_progress", "already_ready")

# /health 的 account 阶段值中，代表"服务端已在引导、无需再注册"的那些。
_BOOTSTRAPPING_PHASES = ("indexing",)

# 按消息回查 REST（IGNORE_SELF 方向判定）的单页条数：倒序响应下目标消息
# 之后 120s 窗口内的新消息会把它挤出首页，50 条在刷屏场景不够，
# 放宽到 200（审查报告【5·P2】）
_LOOKUP_LIMIT = 200


class QqFlowNotReadyError(SourceError):
    """qqflow-server 正在建立索引（503 就绪门控），业务接口暂不可用（瞬态）。

    调用方（poller/runtime）捕获后应静默跳过，不视为错误。
    """


class QqFlowClient(SourceClient):
    """封装所有 qqflow-server API HTTP 通信。"""

    name = "qqflow"  # 源标识，SourceClient 契约成员
    connection_status: ConnectionStatus = "offline"

    def __init__(
        self,
        base_url: str,
        api_token: str,
        qq: str = "",
        key: str = "",
        db_path: str = "",
        *,
        sse_read_timeout_ms: int | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_token = api_token
        self._qq = qq
        self._key = key
        self._db_path = db_path
        # SSE 读超时（毫秒）：缺省读 QQFLOW_SSE_READ_TIMEOUT_MS。
        # 上游每 25s 发 KeepAlive；半开连接下若无读超时，stream_events 的
        # aiter_lines 会永久阻塞，重连循环永远得不到控制权（监听静默死亡）
        if sse_read_timeout_ms is None:
            sse_read_timeout_ms = QqFlowSettings().sse_read_timeout_ms
        self._sse_read_timeout_s = sse_read_timeout_ms / 1000
        self._client: httpx.AsyncClient | None = None
        self._ready_checked = False
        # 首次见到的上游版本号（/health 的 version 字段）：仅用于版本日志与
        # 「上游可能不支持 offset」告警的文案（sources_base 的 upstream_version），
        # 不参与任何行为判定
        self._logged_version: str | None = None
        # 串行化健康检查 + 引导注册（SSE 强制重检与轮询检查并发竞争时避免重复注册）
        self._ready_lock = asyncio.Lock()
        self.connection_status = "offline"

    def sse_timeout(self) -> httpx.Timeout:
        return make_sse_timeout(self._sse_read_timeout_s)

    @property
    def self_uid(self) -> str:
        """本账号自身 UID（QQ NT UID 约定：u_<QQ号>），用于识别自己发送的消息。

        QQFLOW_QQ 为必填配置（缺失时 qqflow 插件在 setup 阶段自禁用），空串仅防御。
        """
        return f"u_{self._qq}" if self._qq else ""

    # ── 内部 ──

    def _get_client(self) -> httpx.AsyncClient:
        """获取共享的 httpx 客户端（懒初始化）。"""
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=httpx.Timeout(30.0),
            )
        return self._client

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_token}"}

    async def _get(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        not_found_ok: bool = False,
    ) -> Any:
        """通用 GET 请求，带错误处理。

        Args:
            path: API 路径，如 "/api/v1/sessions"
            params: 查询参数
            not_found_ok: 为 True 时，404 返回 None（由调用方降级处理）

        Returns:
            JSON 响应；not_found_ok 且上游返回 404 时为 None

        Raises:
            QqFlowNotReadyError: 服务端正在建索引（503 就绪门控，瞬态）
            RuntimeError: 其他非成功状态
        """
        client = self._get_client()
        start = time_module.perf_counter()
        resp = await with_connect_retry(
            lambda: client.get(path, params=params, headers=self._auth_headers())
        )
        logger.debug(
            "GET %s%s → %s (%s)",
            path,
            f"?{resp.url.query.decode()}" if resp.url.query else "",
            resp.status_code,
            fmt_dur(time_module.perf_counter() - start),
        )
        # 先判状态码再解析 JSON：503 信封是合法 JSON，但需走瞬态语义
        if resp.status_code == 503:
            # 就绪门控（瞬态）：失效记忆化标志——服务端重启（内存态注册表丢失）
            # 后，下一轮 ensure_ready 会重新健康检查 + 引导注册（自愈）
            logger.debug("GET %s → 503（服务端索引期，瞬态）", path)
            self._ready_checked = False
            raise QqFlowNotReadyError(
                f"qqflow-server 尚未就绪（503）: {resp.text[:200]}"
            )
        if not resp.is_success:
            if not_found_ok and resp.status_code == 404:
                return None
            raise RuntimeError(
                f"QqFlow API error: {resp.status_code} on {path} — {resp.text[:200]}"
            )
        return resp.json()

    # ── 账号引导 ──

    async def fetch_health(self) -> dict[str, Any]:
        """健康检查（免鉴权）。"""
        client = self._get_client()
        resp = await with_connect_retry(lambda: client.get("/health"))
        if not resp.is_success:
            raise RuntimeError(
                f"QqFlow API error: {resp.status_code} on /health — {resp.text[:200]}"
            )
        return resp.json()

    async def register_account(self, qq: str, key: str, db_path: str) -> str:
        """POST /api/v1/accounts 注册账号，返回 state。

        state: accepted / invalid_key / invalid_db_path / unknown_qq /
        already_ready / in_progress / account_conflict
        """
        client = self._get_client()
        resp = await with_connect_retry(
            lambda: client.post(
                "/api/v1/accounts",
                json={"qq": qq, "key": key, "db_path": db_path},
                headers=self._auth_headers(),
            )
        )
        if not resp.is_success:
            raise RuntimeError(
                f"QqFlow API error: {resp.status_code} on /api/v1/accounts — "
                f"{resp.text[:200]}"
            )
        return resp.json().get("state", "unknown")

    async def ensure_ready(self, force: bool = False) -> None:
        """确保服务端有就绪账号（记忆化，可强制重检）。

        force=True 时忽略记忆化标志重新健康检查（SSE 重连后服务端可能已重启，
        内存态账号注册表丢失，需重新引导注册）。
        无 ready/引导中账号时用配置的 qq/key/db_path 自动注册；注册后进入
        索引期，业务接口的 503 由 QqFlowNotReadyError 瞬态处理兜底，不在此
        阻塞等待。引导中（_BENIGN_STATES）视为良性状态并记忆，索引期内不
        重复注册；网络失败不记忆，下轮重试。

        `/health` 只给一个标量 `account` 阶段（`unregistered` / `indexing` /
        `ready` / `error`），不再列出账号 —— 该接口免鉴权，账号清单会向任何
        调用方泄露本机存在哪些账号。明细在需鉴权的 `GET /api/v1/accounts`
        （见 fetch_accounts）。
        """
        if self._ready_checked and not force:
            return
        async with self._ready_lock:
            # 锁内双检：并发调用（SSE 强制检查 vs 轮询检查）先到者注册并置位，
            # 后到者在此短路，避免重复注册
            if self._ready_checked and not force:
                return
            try:
                health = await self.fetch_health()
            except Exception:
                self._ready_checked = False
                raise
            version = health.get("version")
            if version and version != self._logged_version:
                logger.info("qqflow-server 版本: %s", version)
                self._logged_version = str(version)
            phase = health.get("account", "unregistered")
            logger.debug("健康检查: 账号阶段 %s", phase)
            if phase == "ready":
                self._ready_checked = True
                logger.debug("已有就绪账号，跳过注册")
                return
            if phase in _BOOTSTRAPPING_PHASES:
                self._ready_checked = True
                logger.debug("账号建索引中（%s），不再重复注册", phase)
                return
            if phase == "error":
                # /health 只给标量，根因（密钥/路径填错的 error 字符串）只在需
                # 鉴权的明细接口里。仍显式告警——否则用户只能看到业务接口持续
                # 503，看不到原因。诊断失败不能挡住下面的注册重试。
                await self._log_account_errors()
            # `error` 落到这里是有意的：服务端的 error 状态不释放绑定，但同一
            # 账号可以直接重试注册来恢复（密钥修正后即生效）。
            logger.info(
                "无就绪账号（阶段 %s），注册账号 qq=%s (db_path=%s)",
                phase,
                self._qq,
                self._db_path or "<默认>",
            )
            state = await self.register_account(self._qq, self._key, self._db_path)
            if state in _BENIGN_STATES:
                self._ready_checked = True
                logger.info("账号注册: state=%s", state)
            elif state == "account_conflict":
                # 服务端已被**另一个** QQ 占用（内存索引没有账号维度，同时只能
                # 绑定一个账号）。重试不会自愈：得由人去注销那个账号或改配置，
                # 所以按 ERROR 报而不是 warning，且不记忆化以便配置修正后自愈。
                logger.error(
                    "注册被拒：服务端已绑定另一个账号（本地配置 qq=%s）。"
                    "需先注销：DELETE /api/v1/accounts/{占用方qq}",
                    self._qq,
                )
            else:
                # 被拒态（invalid_key/invalid_db_path/unknown_qq 等）不记忆化：
                # 保持未检查标志让下一轮 poll 的 ensure_ready 再次尝试注册；
                # 否则零账号部署下没有业务 503 兜底复位标志，引导失败后永不自愈
                logger.warning("账号注册被拒: state=%s（下轮重试）", state)

    async def _log_account_errors(self) -> None:
        """拉明细接口把 error 根因打出来（诊断专用，失败仅降级为 debug）。"""
        try:
            accounts = await self.fetch_accounts()
        except Exception as exc:  # noqa: BLE001 —— 诊断路径不得影响注册重试
            logger.debug("账号明细拉取失败（跳过根因诊断）: %s", exc)
            return
        for a in accounts:
            if a.get("state") == "error" and a.get("error"):
                logger.warning(
                    "账号 %s 初始化失败: %s",
                    a.get("qq") or "<未知>",
                    a["error"],
                )

    async def fetch_accounts(self) -> list[dict]:
        """GET /api/v1/accounts —— 账号明细（诊断用，需鉴权）。

        `/health` 只报一个标量阶段；出错时的具体原因（`error` 字段）、消息数
        与服务端实际读取的 `db_path` 只在这里。不参与就绪判定，仅供排查。
        """
        client = self._get_client()
        resp = await with_connect_retry(
            lambda: client.get("/api/v1/accounts", headers=self._auth_headers())
        )
        if not resp.is_success:
            raise RuntimeError(
                f"QqFlow API error: {resp.status_code} on /api/v1/accounts — "
                f"{resp.text[:200]}"
            )
        return resp.json().get("accounts", [])

    # ── REST API ──

    async def fetch_contacts(self) -> dict[str, str]:
        """获取联系人 → {username: display_name}（displayName 优先，UID 兜底）。

        每级候选经 clean_display_name 净化（上游档案昵称含控制字符/空白等
        脏数据），全部净化后为空才回退 UID。候选选择发生在构造前，必须在此
        显式净化（types.py 的构造净化不参与候选选择）。

        **按 offset 翻页取全量**（fetch_all_pages）：上游 `limit` 默认 100，
        不翻页只能拿到前 100 条且无任何错误提示，截断外的发送者显示名会
        退化成 UID。传大 limit 只是把天花板抬到上游硬上限 10000，仍是猜值；
        翻页才是取尽。
        """
        rows = await fetch_all_pages(
            lambda path, params: self._get(path, params=params),
            "/api/v1/contacts",
            key="contacts",
            dedup_key="username",
            upstream_version=self._logged_version,
        )
        contacts: dict[str, str] = {}
        for c in rows:
            contacts[c["username"]] = (
                clean_display_name(c.get("displayName"))
                or clean_display_name(c.get("nickname"))
                or clean_display_name(c.get("remark"))
                or c["username"]
            )
        return contacts

    # 不再有 fetch_group_members：/api/v1/group-members 的 groupNickname 与
    # 消息自带的 senderName 出自上游同一条 display_sender 链（本群群名片 >
    # 备注 > 最新消息昵称 > 档案昵称 > UID），逐群再查一次纯属冗余。

    async def fetch_sessions(self) -> list[QqFlowSession]:
        """获取所有会话列表（按 offset 翻页取尽，与 fetch_contacts 同模式）。

        上游 limit 默认 100 且按最后消息时间倒序（qqflow-server-api.md §4），
        不翻页只会发现最近活跃的 100 个会话，更早的会话将永远无法被
        发现/启用/轮询。page_size=10000 = 上游 limit 硬上限：典型规模一个
        请求即取尽（与旧「显式大 limit」实现请求数相同）；旧上游若忽略
        offset，共享「本页无新增」防御立即告警终止，停在 10000 条——零回退。
        """
        return await fetch_all_pages(
            lambda path, params: self._get(path, params=params),
            "/api/v1/sessions",
            key="sessions",
            dedup_key="username",
            page_size=10000,
            upstream_version=self._logged_version,
        )

    async def fetch_messages(
        self,
        talker: str,
        start: int | None = None,
        limit: int = 500,
        offset: int = 0,
    ) -> QqFlowMessagesResponse:
        """获取指定会话的历史消息（按时间倒序，返回完整信封）。

        Args:
            talker: 会话 ID
            start: 起始时间（秒级 Unix 时间戳，含该时间），None 为不限
            limit: 单页条数（服务端上限 10000）
            offset: 分页偏移

        Returns:
            QqFlowMessagesResponse: 含 messages 与 hasMore，翻页由调用方驱动
        """
        params: dict[str, Any] = {"talker": talker, "limit": limit, "offset": offset}
        if start is not None:
            params["start"] = start
        data: QqFlowMessagesResponse = await self._get(
            "/api/v1/messages", params=params
        )
        return data

    async def lookup_message(
        self, talker: str, local_id: str, ts: int
    ) -> QqFlowMessage | None:
        """按 localId 回查 REST 获取消息原始对象（供 SSE 自消息判定）。

        SSE 事件无发送者标识，IGNORE_SELF 开启时需按 rawid（= REST localId）
        回查方向信息；用 timestamp 缩小查询范围（向前 120s），在返回的
        消息中匹配 localId == rawid。回查失败/未命中返回 None，由调用方
        fail-open 放行。503 走 QqFlowNotReadyError 瞬态语义。
        刷屏场景下目标消息可能被窗口内更新的消息挤出首页，limit 放宽到
        _LOOKUP_LIMIT（200）。
        """
        try:
            resp = await self.fetch_messages(
                talker, start=max(0, ts - 120), limit=_LOOKUP_LIMIT
            )
        except Exception as e:
            # 调用方（SSE 监听器）fail-open 处理并记 WARNING，此处仅 DEBUG 免双重日志
            logger.debug("回查消息失败: %s", e)
            raise
        window = 120
        for m in resp.get("messages", []):
            if str(m.get("localId", "")) != local_id:
                continue
            ct = m.get("createTime", 0)
            if abs(ct - ts) > window:
                continue
            return m
        return None

    # ── SSE 流 ──

    def _push_url(self) -> str:
        """SSE 推送地址（RFC 3986 join）：base_url 误带路径/查询串时不会拼坏，
        与 weflow-legacy client 及本类 _build_media_url 的拼接策略一致。"""
        return str(httpx.URL(self._base_url).join("/api/v1/push/messages"))

    async def stream_events(self) -> AsyncIterator[QqFlowEvent]:
        """SSE 实时消息流 — 异步迭代器，持续产出解析后的 JSON 事件。

        用法:
            async for event in client.stream_events():
                process(event)

        连接失败（非 2xx，含 503）时置 offline 并正常返回，
        由监听器的退避重连循环处理。
        连接成功（HTTP 200）后会强制重做一次就绪检查/引导注册，自愈
        服务端重启导致的注册表丢失。
        """
        logger.debug("SSE 连接中...")
        self.connection_status = "reconnecting"

        # SSE 需要独立的客户端：写/池不限，但保留可配置的读超时
        # （上游 25s ping，默认 60s ≈ 2.4 个心跳周期）以自愈半开连接
        async with httpx.AsyncClient(timeout=self.sse_timeout()) as sse_client:
            try:
                async with sse_client.stream(
                    "GET",
                    self._push_url(),
                    headers={
                        **self._auth_headers(),
                        "Accept": "text/event-stream",
                    },
                ) as resp:
                    if not resp.is_success:
                        logger.warning("SSE 连接失败: %s", resp.status_code)
                        self.connection_status = "offline"
                        return

                    logger.info("SSE 已连接")
                    self.connection_status = "online"

                    # 服务端重启后账号注册表（内存态）丢失；SSE HTTP 200 是服务端
                    # 已恢复的可靠信号，强制重做一次就绪检查 + 引导注册（幂等）。
                    # 失败仅告警不阻断流：业务 503 由 _get 自愈、重连由监听器
                    # 退避循环兜底。
                    try:
                        await self.ensure_ready(force=True)
                    except Exception as e:  # noqa: BLE001 — 自愈尽力而为，失败不阻断流
                        logger.warning("SSE 就绪自愈检查失败: %s", e)

                    buffer = ""
                    async for line in resp.aiter_lines():
                        buffer += line + "\n"
                        while "\n\n" in buffer:
                            event_text, buffer = buffer.split("\n\n", 1)
                            for event_line in event_text.split("\n"):
                                if event_line.startswith("data: "):
                                    try:
                                        event = json.loads(event_line[6:])
                                        logger.debug(
                                            "SSE 事件: %s rawid=%s",
                                            event.get("event"),
                                            event.get("rawid"),
                                        )
                                        yield event
                                    except json.JSONDecodeError:
                                        logger.debug("SSE 数据行 JSON 解析失败，跳过")

            except httpx.RequestError as e:
                logger.warning("SSE 连接错误: %s", e)
                self.connection_status = "offline"
            except asyncio.CancelledError:
                self.connection_status = "offline"
                raise

        self.connection_status = "offline"
        logger.info("SSE 流结束")

    # ── 媒体 ──

    def _build_media_url(self, media_id: str) -> str:
        """将 mediaId 规范化为 qqflow-server 完整 URL（GET /api/v1/media/{id}）。

        用 RFC 3986 的 URL join 拼接：绝对路径引用会替换掉 base 自带的
        路径与查询串，_base_url 即使误配成
        "http://127.0.0.1:5032/api/v1/push/messages?access_token=..."
        也不会把媒体路径拼进查询串（朴素字符串拼接会打出坏 URL 导致图片挂死）。
        """
        return str(
            httpx.URL(self._base_url).join(f"/api/v1/media/{media_id.lstrip('/')}")
        )

    async def download_media(self, path: str) -> bytes:
        """下载媒体文件原始字节（GET /api/v1/media/{id}，带鉴权）。

        path 为 normalize 提取的 mediaId（md5 hex 小写或 uuid）。
        404（QQ 缓存被清理）/ 503（就绪门控）等非成功状态统一映射为
        MediaError，由 pipeline 跳过 OCR、server 代理映射为 404 兜底。

        Raises:
            MediaError: 网络错误或 qqflow-server 返回非成功状态（cause 保留原异常）
        """
        client = self._get_client()
        start = time_module.perf_counter()
        try:
            async with client.stream(
                "GET", self._build_media_url(path), headers=self._auth_headers()
            ) as resp:
                if not resp.is_success:
                    raise MediaError(
                        f"QqFlow media error: {resp.status_code} on {path}"
                    )
                chunks: list[bytes] = []
                total = 0
                async for chunk in resp.aiter_bytes():
                    total += len(chunk)
                    if total > _MAX_MEDIA_BYTES:
                        raise MediaError(f"QqFlow media too large: {path}")
                    chunks.append(chunk)
                logger.debug(
                    "媒体下载完成: %s (%d bytes, %s)",
                    path,
                    total,
                    fmt_dur(time_module.perf_counter() - start),
                )
                return b"".join(chunks)
        except httpx.RequestError as e:
            raise MediaError(f"media fetch failed: {path}") from e

    async def close(self) -> None:
        """关闭 HTTP 客户端。"""
        if self._client is not None:
            await self._client.aclose()
            self._client = None
