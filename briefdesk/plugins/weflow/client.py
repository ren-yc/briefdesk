"""WeFlow HTTP API 客户端 — 封装所有与 WeFlow 的通信细节。

用法:
    client = WeFlowClient(base_url="http://127.0.0.1:5031", api_token="xxx")
    contacts = await client.fetch_contacts()
    sessions = await client.fetch_sessions()
    resp = await client.fetch_messages(talker="...", start_ts=1750000000, limit=500)
    messages = resp["messages"]
    async for event in client.stream_events():
        ...
"""

import asyncio
import json
import logging
import time as time_module
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, TypedDict

import httpx

from briefdesk.logger import fmt_dur
from briefdesk.masking import clean_display_name
from briefdesk.plugins.weflow.config import WeFlowSettings
from briefdesk.sources_base import (
    ConnectionStatus,
    MediaError,
    SourceClient,
    with_connect_retry,
)

# ── WeFlow API 数据类型 ──


class WeFlowContact(TypedDict):
    """联系人（/api/v1/contacts）。"""

    username: str
    displayName: str
    remark: str
    nickname: str
    alias: str
    avatarUrl: str
    type: str


class WeFlowGroupMember(TypedDict):
    """群成员（/api/v1/group-members）。"""

    wxid: str
    displayName: str
    nickname: str
    remark: str
    alias: str
    groupNickname: str


class ChatLabSession(TypedDict):
    """ChatLab 格式会话（GET /api/v1/sessions?format=chatlab）。

    type 为权威会话类型：group（群聊）/ private（私聊）/ channel（公众号），
    经实测与微信会话真实形态一致（489 会话 0 异常）。
    """

    id: str
    name: str
    platform: str
    type: Literal["group", "private", "channel"]
    messageCount: int
    lastMessageAt: int


def session_kind(session: ChatLabSession) -> str:
    """ChatLab 会话类型 → 应用会话类型（channel→official，即公众号）。"""
    raw = session.get("type")
    if raw == "channel":
        return "official"
    if raw in ("group", "private"):
        return raw
    return "private"  # 未知类型兜底按私聊处理，避免误判为群聊拉群成员


def is_group_session(session: ChatLabSession) -> bool:
    """判断会话是否为群聊（chatlab type=group）。"""
    return session_kind(session) == "group"


def is_private_session(session: ChatLabSession) -> bool:
    """判断会话是否为私聊（chatlab type=private）。"""
    return session_kind(session) == "private"


def is_official_session(session: ChatLabSession) -> bool:
    """判断会话是否为公众号（chatlab type=channel）。"""
    return session_kind(session) == "official"


class WeFlowMessage(TypedDict):
    """REST API 返回的消息。"""

    serverId: str
    content: str
    createTime: int
    localType: int  # 1 = 文本, 3 = 图片
    senderUsername: str
    isSend: int  # 0 = 收到, 1 = 自己发送（微信 DB 原义；SSE 推送端据此外排自消息）
    is_group: bool
    mediaType: str  # "image" 等
    mediaUrl: str  # 可访问的 HTTP 图片地址
    mediaFileName: str
    mediaLocalPath: str


class WeFlowEvent(TypedDict):
    """SSE 推送的原始事件。"""

    event: Literal["message.new", "message.revoke"]
    rawid: str
    content: str
    sessionId: str
    sourceName: str
    groupName: str
    timestamp: int
    sessionType: Literal["group", "private"]


class WeFlowContactsResponse(TypedDict):
    contacts: list[WeFlowContact]


class WeFlowGroupMembersResponse(TypedDict):
    members: list[WeFlowGroupMember]


class WeFlowMessagesResponse(TypedDict):
    messages: list[WeFlowMessage]
    hasMore: bool


class ChatLabSessionsResponse(TypedDict):
    sessions: list[ChatLabSession]



logger = logging.getLogger(__name__)

# 媒体下载大小上限：防止异常/恶意上游返回超大文件造成内存放大
_MAX_MEDIA_BYTES = 20 * 1024 * 1024

# 按消息回查 REST（SSE 图片 mediaUrl / 文章卡片原始 XML）的单页条数：
# 倒序响应下目标消息之后 120s 窗口内的新消息会把它挤出首页，
# 50 条在刷屏场景不够，放宽到 200（审查报告【5·P2】）
_LOOKUP_LIMIT = 200


class WeFlowClient(SourceClient):
    """封装所有 WeFlow API HTTP 通信。"""

    name = "weflow"  # 源标识，SourceClient 契约成员
    connection_status: ConnectionStatus = "offline"

    def __init__(
        self,
        base_url: str,
        api_token: str,
        *,
        sse_read_timeout_ms: int | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_token = api_token
        # SSE 读超时（毫秒）：缺省读 WEFLOW_SSE_READ_TIMEOUT_MS。
        # 半开连接（对端假死/断网无 FIN）下若无读超时，stream_events 的
        # aiter_lines 会永久阻塞，重连循环永远得不到控制权（监听静默死亡）
        if sse_read_timeout_ms is None:
            sse_read_timeout_ms = WeFlowSettings().sse_read_timeout_ms
        self._sse_read_timeout_s = sse_read_timeout_ms / 1000
        self._client: httpx.AsyncClient | None = None
        self.connection_status = "offline"

    def sse_timeout(self) -> httpx.Timeout:
        """SSE 长连接超时：连接 10s、读超时可配置、写/连接池不限。

        ReadTimeout 是 httpx.RequestError 的子类，会被 stream_events 的
        既有 except 捕获置 offline 并正常结束生成器，由监听器的退避重连
        循环接管——这是半开连接自愈的关键路径。
        """
        return httpx.Timeout(
            connect=10.0,
            read=self._sse_read_timeout_s,
            write=None,
            pool=None,
        )

    # ── 内部 ──

    def _get_client(self) -> httpx.AsyncClient:
        """获取共享的 httpx 客户端（懒初始化）。"""
        if self._client is None:
            # base_url 原样使用 _base_url：httpx 的 base_url 合并规则会正确处理
            # 绝对路径请求；媒体/SSE 的 URL 统一走 URL.join（见 _build_media_url
            # 与 stream_events），不再依赖 base 的形态。
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
        retry_on_empty: bool = False,
        params: dict[str, Any] | None = None,
        not_found_ok: bool = False,
    ) -> Any:
        """通用 GET 请求，带错误处理。

        Args:
            path: API 路径，如 "/api/v1/sessions"
            retry_on_empty: 为 True 时，返回空列表会在 500ms 后重试一次
            params: 查询参数（httpx 负责编码）
            not_found_ok: 为 True 时，404 返回 None（由调用方降级处理）

        Returns:
            JSON 响应；not_found_ok 且上游返回 404 时为 None
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
        if not resp.is_success:
            if not_found_ok and resp.status_code == 404:
                return None
            # resp.text 按响应声明解码并容错，避免非 UTF-8 错误体让
            # UnicodeDecodeError 掩盖原始 API 错误（与 qqflow 对齐）
            raise RuntimeError(
                f"WeFlow API error: {resp.status_code} on {path} — {resp.text[:200]}"
            )
        data = resp.json()

        if retry_on_empty:
            # 检查返回的数据中是否包含空列表
            list_key = "messages"
            items = data.get(list_key)
            if isinstance(items, list) and not items:
                logger.debug("GET %s 返回空列表，500ms 后重试一次", path)
                await asyncio.sleep(0.5)
                start = time_module.perf_counter()
                resp = await with_connect_retry(
                    lambda: client.get(
                        path, params=params, headers=self._auth_headers()
                    )
                )
                logger.debug(
                    "GET %s (重试) → %s (%s)",
                    path,
                    resp.status_code,
                    fmt_dur(time_module.perf_counter() - start),
                )
                if resp.is_success:
                    data = resp.json()

        return data

    async def _lookup_message(
        self, talker: str, rawid: str, ts: int, media: bool
    ) -> WeFlowMessage | None:
        """按 serverId 回查 REST 获取消息原始对象。

        用 timestamp 缩小查询范围（start=ts-120 起，客户端再按 ±120s 过滤），
        在返回的消息中匹配 serverId == rawid。media 控制是否携带媒体导出参数
        ——注意 WeFlow 在 media=True 时会把文章卡片 XML 渲染成占位符
        （如 "[视频号] 标题"），需要原始 XML 时必须用 media=False 回查。
        回查显式 retry_on_empty=False：miss 是常见路径，不应在监听/回填
        热路径上为空结果白付 500ms 重试（审查报告【7·P2】）。
        """
        start_ts = int((datetime.fromtimestamp(ts, tz=UTC) - timedelta(seconds=120)).timestamp())

        try:
            resp = await self.fetch_messages(
                talker, start_ts, limit=_LOOKUP_LIMIT, media=media,
                retry_on_empty=False,
            )
        except Exception as e:
            logger.error(f"回查消息失败: {e}")
            raise

        # 时间窗口过滤 + 匹配 serverId
        window = 120
        for m in resp.get("messages", []):
            if m.get("serverId") != rawid:
                continue
            ct = m.get("createTime", 0)
            if abs(ct - ts) > window:
                continue
            return m
        return None

    async def fetch_message_media(self, talker: str, rawid: str, ts: int) -> str | None:
        """通过 rawid 回查 REST，获取消息的 mediaUrl。

        SSE 事件不含 mediaUrl，需要用 rawid 回查 REST。用 timestamp
        缩小查询范围（前后各 120s），在返回的消息中匹配 serverId == rawid。

        Args:
            talker: 会话 ID
            rawid: SSE 事件中的 rawid，对应 REST 的 serverId
            ts: 消息时间戳（秒级 Unix）

        Returns:
            mediaUrl 或 None
        """
        m = await self._lookup_message(talker, rawid, ts, media=True)
        if m is None:
            return None
        if m.get("mediaType") == "image" and m.get("mediaUrl"):
            logger.debug("SSE 回查成功: %s → %s", rawid, m["mediaUrl"])
            return m["mediaUrl"]
        # 可能没有 mediaUrl（media 导出未命中），记录但不算错误
        logger.debug(
            "SSE 消息 %s mediaType=%s 无 mediaUrl",
            rawid,
            m.get("mediaType"),
        )
        return None

    async def fetch_message_raw(self, talker: str, rawid: str, ts: int) -> WeFlowMessage | None:
        """按 rawid 回查 REST（media=False）获取消息原始内容。

        回填以 media=True 拉取时，WeFlow 会把文章卡片 XML 渲染成占位符
        （如 "[视频号] 标题"）；用本方法回查拿原始 XML 供 appmsg 解析。
        """
        m = await self._lookup_message(talker, rawid, ts, media=False)
        if m is None:
            logger.debug("回查原始消息未命中: %s", rawid)
            return None
        return m

    def _build_media_url(self, path: str) -> str:
        """将媒体路径规范化为 WeFlow 完整 URL。

        支持的输入格式:
            - 完整 URL（http://.../api/v1/media/...）→ 提取路径部分
            - 绝对路径（/api/v1/media/... 或 /api/media/...）→ 提取路径部分
            - 纯相对路径（xxx@chatroom/images/abc.jpg）→ 直接使用
        """
        for prefix in ("/api/v1/media/", "/api/media/"):
            idx = path.find(prefix)
            if idx >= 0:
                path = path[idx + len(prefix) :]
                break
        # 用 RFC 3986 的 URL join 拼接：绝对路径引用会替换掉 base 自带的
        # 路径与查询串，_base_url 即使误配成
        # "http://127.0.0.1:5031/api/v1/push/messages?access_token=..."
        # 也不会把媒体路径拼进查询串（朴素字符串拼接会打出坏 URL 导致图片挂死）。
        return str(httpx.URL(self._base_url).join(f"/api/v1/media/{path.lstrip('/')}"))

    async def download_media(self, path: str) -> bytes:
        """下载媒体文件（如图片），带鉴权返回原始字节。

        媒体文件归属 WeFlow，鉴权与 URL 拼接均在此处处理，
        调用方（OCR 等）只拿到字节，无需接触 WeFlow 细节。

        Args:
            path: 图片路径（normalize 提取的相对路径，或完整 URL），
                见 _build_media_url

        Returns:
            媒体文件原始内容

        Raises:
            MediaError: 网络错误或 WeFlow 返回非成功状态（cause 保留原异常）
        """
        client = self._get_client()
        start = time_module.perf_counter()
        try:
            async with client.stream(
                "GET", self._build_media_url(path), headers=self._auth_headers()
            ) as resp:
                if not resp.is_success:
                    raise MediaError(
                        f"WeFlow media error: {resp.status_code} on {path}"
                    )
                chunks: list[bytes] = []
                total = 0
                async for chunk in resp.aiter_bytes():
                    total += len(chunk)
                    if total > _MAX_MEDIA_BYTES:
                        raise MediaError(f"WeFlow media too large: {path}")
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

    # ── REST API ──

    async def fetch_contacts(self) -> dict[str, str]:
        """获取联系人 → {username: display_name}（displayName 优先，username 兜底）。

        每级候选经 clean_display_name 净化（上游昵称可能携带控制字符/空白等
        脏数据），全部净化后为空才回退 username。候选选择发生在构造前，必须
        在此显式净化（types.py 的构造净化不参与候选选择）。
        """
        data: WeFlowContactsResponse = await self._get("/api/v1/contacts")
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
        """获取群成员 → {wxid: 群内显示名}。

        候选顺序（每级经 clean_display_name 净化，净化后为空才回退）：
        groupNickname → displayName → nickname → remark；全部为空则该成员
        不进入映射，由 normalize 回退到全局 contacts / wxid。
        群不存在（404）返回空映射（群可能已解散，降级处理）；其他错误
        照常抛出，由 poller 中止本轮回填，避免 u_/wxid 名永久入库。
        """
        data: WeFlowGroupMembersResponse | None = await self._get(
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
            # 群昵称缺失/仅回退为 wxid 时不应压住 displayName 等有效候选
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
        logger.debug(
            "群成员拉取: %s → %d 名成员", chatroom_id, len(members)
        )
        return members

    async def fetch_sessions(self) -> list[ChatLabSession]:
        """获取所有会话列表。

        使用 chatlab 格式：其 `type`（group/private/channel）经实测与微信
        会话真实形态一致，而 JSON 格式的 `type` 恒为 0 不可靠。默认 limit=100
        会截断会话发现（实测 489 会话只回 100），故显式传大 limit。
        """
        data: ChatLabSessionsResponse = await self._get(
            "/api/v1/sessions",
            params={"format": "chatlab", "limit": 10000},
        )
        return data["sessions"]

    async def fetch_messages(
        self,
        talker: str,
        start_ts: int | None,
        limit: int = 500,
        offset: int = 0,
        media: bool = False,
        retry_on_empty: bool = True,
    ) -> WeFlowMessagesResponse:
        """获取指定会话的历史消息（返回完整信封，含 hasMore）。

        上游按 createTime 过滤，start 为闭区间下界（createTime >= start 的消息
        均返回，实测含边界）；响应按时间倒序。翻页用 offset 递增至 hasMore=False。

        Args:
            talker: 会话 ID
            start_ts: 起始时间（秒级 Unix 时间戳，含边界）；None 不过滤
            limit: 单页返回条数
            offset: 分页偏移
            media: 是否导出媒体（图片等），为 True 时附加 media=1&image=1
            retry_on_empty: 空结果是否 500ms 后重试一次（回查链路应传 False；
                轮询首页保留默认 True 以维持既有「刚入库查不到」竞态兜底）
        """
        qs = f"talker={talker}&limit={limit}&offset={offset}"
        if start_ts is not None:
            qs += f"&start={start_ts}"
        if media:
            qs += "&media=1&image=1"
        data: WeFlowMessagesResponse = await self._get(
            f"/api/v1/messages?{qs}", retry_on_empty=retry_on_empty
        )
        return data

    # ── SSE 流 ──

    async def stream_events(self) -> AsyncIterator[WeFlowEvent]:
        """SSE 实时消息流 — 异步迭代器，持续产出解析后的 JSON 事件。

        用法:
            async for event in client.stream_events():
                process(event)
        """
        logger.debug("SSE 连接中...")
        self.connection_status = "reconnecting"

        # SSE 需要独立的客户端：写/池不限，但保留可配置的读超时以自愈半开连接
        async with httpx.AsyncClient(timeout=self.sse_timeout()) as sse_client:
            try:
                # 同样用 URL join 构建 SSE 地址，避免 _base_url 带路径/查询时拼坏；
                # WeFlow 文档推荐 SSE 长连接用 ?access_token= 查询参数（与 Bearer 头
                # 同时携带）。该令牌会出现在请求 URL 中：本进程侧由 uvicorn access
                # log 的查询参数掩码（logger.redact_query_string）兜底，httpx 调试
                # 日志已压制在 WARNING 之下，不输出 URL
                url = str(httpx.URL(self._base_url).join("/api/v1/push/messages"))
                params = {"access_token": self._api_token} if self._api_token else None
                async with sse_client.stream(
                    "GET",
                    url,
                    params=params,
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
