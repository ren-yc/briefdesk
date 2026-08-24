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
from briefdesk.sources_base import (
    ConnectionStatus,
    MediaError,
    SourceClient,
    SourceError,
    with_connect_retry,
)

# ── qqflow-server API 数据类型 ──


class QqFlowEvent(TypedDict):
    """SSE 推送的原始事件（message.new / message.revoke / sync / ping）。"""

    event: str
    sessionId: str
    sessionType: Literal["group", "private"]
    groupName: str  # 仅群聊；缺失时省略
    rawid: str  # 消息 rowid（字符串），跨路径去重键
    sourceName: str  # 发送者昵称；缺失时省略
    content: str
    timestamp: int  # 秒级 Unix
    media: dict | None  # 图片/语音/视频的结构化媒体元数据；缺失时省略


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


class QqFlowGroupMember(TypedDict):
    """群成员（/api/v1/group-members）。"""

    wxid: str
    displayName: str
    nickname: str
    remark: str
    alias: str
    groupNickname: str


class QqFlowMessage(TypedDict):
    """REST API 返回的消息。"""

    localId: int  # rowid 数字 —— msg_id 来源（与 SSE rawid 同值）
    serverId: str  # seq 字符串（与 rowid 无关，不使用）
    localType: int  # 0文本 / 1其他 / 3图片 / 4语音 / 5视频 / 6撤回 / 7系统
    createTime: int  # 秒级 Unix
    isSend: int  # v1 恒 0，方向不可推导
    senderUsername: str  # 发送者 UID
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


class QqFlowSessionsResponse(TypedDict):
    sessions: list[QqFlowSession]


class QqFlowContactsResponse(TypedDict):
    contacts: list[QqFlowContact]


class QqFlowGroupMembersResponse(TypedDict):
    members: list[QqFlowGroupMember]



logger = logging.getLogger(__name__)

# 媒体下载大小上限：防止异常/恶意上游返回超大文件造成内存放大
_MAX_MEDIA_BYTES = 20 * 1024 * 1024

# 账号引导的良性状态：已受理/引导中，视为可记忆，索引期内不重复注册
_BENIGN_STATES = ("accepted", "in_progress", "already_ready")


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
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_token = api_token
        self._qq = qq
        self._key = key
        self._db_path = db_path
        self._client: httpx.AsyncClient | None = None
        self._ready_checked = False
        # 串行化健康检查 + 引导注册（SSE 强制重检与轮询检查并发竞争时避免重复注册）
        self._ready_lock = asyncio.Lock()
        self.connection_status = "offline"

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
        already_ready / in_progress
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
            accounts = health.get("accounts", [])
            states = [a.get("state") for a in accounts]
            logger.debug("qqflow 健康检查: 账号状态 %s", states or "（无账号）")
            if any(a.get("state") == "ready" for a in accounts):
                self._ready_checked = True
                logger.debug("qqflow 已有就绪账号，跳过引导")
                return
            if any(a.get("state") in _BENIGN_STATES for a in accounts):
                self._ready_checked = True
                logger.debug("qqflow 账号引导中（良性状态），不再重复注册")
                return
            logger.info(
                "qqflow 无就绪账号，尝试引导注册 (qq=%s, db_path=%s)",
                self._qq,
                self._db_path or "<默认>",
            )
            state = await self.register_account(self._qq, self._key, self._db_path)
            self._ready_checked = True
            if state in _BENIGN_STATES:
                logger.info(f"qqflow 账号引导中: {state}")
            else:
                logger.warning(f"qqflow 账号引导被拒: {state}")

    # ── REST API ──

    async def fetch_contacts(self) -> dict[str, str]:
        """获取联系人 → {username: display_name}（displayName 优先，UID 兜底）。

        每级候选经 clean_display_name 净化（上游档案昵称含控制字符/空白等
        脏数据），全部净化后为空才回退 UID。候选选择发生在构造前，必须在此
        显式净化（types.py 的构造净化不参与候选选择）。
        """
        data: QqFlowContactsResponse = await self._get("/api/v1/contacts")
        contacts: dict[str, str] = {}
        for c in data["contacts"]:
            contacts[c["username"]] = (
                clean_display_name(c.get("displayName"))
                or clean_display_name(c.get("nickname"))
                or clean_display_name(c.get("remark"))
                or c["username"]
            )
        return contacts

    async def fetch_group_members(self, chatroom_id: str) -> dict[str, str]:
        """获取群成员 → {uid: 群内显示名}。

        groupNickname 已由上游按「本群群名片 > 备注 > 最新消息昵称 >
        档案昵称 > UID」计算，优先采用；若它只是 UID 回退值则改用
        displayName/nickname/remark。每级候选经 clean_display_name 净化，
        全部为空才不进入映射（由 normalize 回退 contacts / UID）。
        404（群不存在）返回空映射；503 仍走 QqFlowNotReadyError 瞬态语义。
        """
        data: QqFlowGroupMembersResponse | None = await self._get(
            "/api/v1/group-members",
            params={"chatroomId": chatroom_id},
            not_found_ok=True,
        )
        if not data:
            logger.warning(f"群成员接口 404（群可能不存在）: {chatroom_id}")
            return {}

        members: dict[str, str] = {}
        for m in data.get("members", []):
            uid = m.get("wxid") or ""
            if not uid:
                continue
            group_nick = clean_display_name(m.get("groupNickname"))
            # 上游无任何名字来源时会以 UID 兜底，此时应让位于其他候选
            if group_nick == uid:
                group_nick = ""
            name = (
                group_nick
                or clean_display_name(m.get("displayName"))
                or clean_display_name(m.get("nickname"))
                or clean_display_name(m.get("remark"))
            )
            if name:
                members[uid] = name
        logger.debug("群成员拉取: %s → %d 名成员", chatroom_id, len(members))
        return members

    async def fetch_sessions(self) -> list[QqFlowSession]:
        """获取所有会话列表。"""
        data: QqFlowSessionsResponse = await self._get("/api/v1/sessions")
        return data["sessions"]

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
        """
        try:
            resp = await self.fetch_messages(talker, start=max(0, ts - 120), limit=50)
        except Exception as e:
            # 调用方（SSE 监听器）fail-open 处理并记 WARNING，此处仅 DEBUG 免双重日志
            logger.debug(f"回查消息失败: {e}")
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

        # SSE 需要独立的客户端（无限超时）
        async with httpx.AsyncClient(timeout=httpx.Timeout(None)) as sse_client:
            try:
                async with sse_client.stream(
                    "GET",
                    f"{self._base_url}/api/v1/push/messages",
                    headers={
                        **self._auth_headers(),
                        "Accept": "text/event-stream",
                    },
                ) as resp:
                    if not resp.is_success:
                        logger.warning(f"SSE 连接失败: {resp.status_code}")
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
                        logger.warning(f"SSE 就绪自愈检查失败: {e}")

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
                logger.warning(f"SSE 连接错误: {e}")
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
