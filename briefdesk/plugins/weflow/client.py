"""weflow-server HTTP API 客户端（微信 4.x，默认 :5033）— 封装所有通信细节。

参考 qqflow（引导注册 + 503 就绪门控）与 weflow-legacy（WeFlow API 契约形状）。

用法:
    client = WeFlowClient(base_url="http://127.0.0.1:5033", api_token="xxx",
                          wxid="wxid_...", db_path="...", db_keys={...},
                          img_aes_key="...", img_xor_key="...")
    await client.ensure_ready()          # 健康检查 + 账号注册（客户端驱动）
    sessions = await client.fetch_sessions()
    resp = await client.fetch_messages(talker="...", start_ts=..., limit=500)
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
    SourceError,
    fetch_all_pages,
    make_sse_timeout,
    with_connect_retry,
)

# ── weflow-server API 数据类型 ──


class WeFlowEvent(TypedDict):
    """SSE 推送的原始事件（data 行的 JSON 载荷）。

    只有 message.new 参与管道（见 normalize.pre_filter_sse），其余全部丢弃：
    - ready：连接基线，载荷实测为 `{"status":"ok"}`——**不含 event 键**
      （事件名只在 SSE 的 `event:` 帧头，本客户端只解析 data 行故读不到），
      因此在 pre_filter_sse 的事件类型分支被丢弃；
    - sync：水位基线/重基通知（`{event, watermarks[]}`），无消息体；订阅端
      滞后（broadcast 缓冲被覆盖）时服务端补发一帧携带**当前真实水位**的
      sync（不占用总线序号，避免其他客户端跳号），客户端可据此重新增量拉取；
    - message.revoke：撤回，显式丢弃。

    v0.3.0 起 message.new 另携带 `media` 元数据（见下），本客户端只取消息
    字段，媒体字节仍走 REST `media=1` 导出回查（normalize_sse 按
    `media.type` 预检跳过非图片类型的无效回查）。
    """

    event: Literal["ready", "message.new", "message.revoke", "sync"]
    sessionId: str
    # 与 REST 的 sessionType 同源（store::SessionKind::as_str）；会话不在索引时
    # 上游兜底为 "other"。normalize 只判 group，其余一律按非群处理
    sessionType: Literal["group", "private", "official", "other"]
    groupName: str  # 仅群聊；缺失时省略
    rawid: str  # 消息 serverId（字符串），跨路径去重键
    sourceName: str  # 发送者昵称；缺失时省略
    content: str
    timestamp: int  # 秒级 Unix
    media: dict | None  # v0.3.0 起：媒体元数据 {type, fileName, md5}，仅图片/语音/
    # 视频/表情消息携带，否则 null 或缺键。**纯元数据**——不含 url/localPath
    # （字节走 REST `/api/v1/messages?media=1` 导出），也绝不含解密密钥；
    # normalize_sse 用它预检 [图片] 回查（type != "image" 时跳过）


class WeFlowSession(TypedDict):
    """会话（原生格式；sessionType 权威，chatlab 的 type 实测不可靠）。"""

    username: str  # 会话 id：群=群号/群 id，私聊/公众号=wxid 或 gh_
    displayName: str
    sessionType: str  # "group" / "private" / "official" / "other"（权威）
    type: int  # 数字类型（兜底，SessionKind 枚举序：private=0/group=1/official=2/other=3）
    messageCount: int
    summary: str
    unreadCount: int
    lastTimestamp: int


class WeFlowContact(TypedDict):
    """联系人。"""

    username: str
    displayName: str
    nickname: str
    remark: str
    alias: str
    avatarUrl: str
    type: str


class WeFlowMedia(TypedDict):
    """消息媒体元数据（可解析媒体的消息恒有；导出字段仅 media=1 时填充）。

    上游 `message_json` 恒下发 type/fileName/md5 与空串 url/localPath；
    media=1 且该条导出成功时，导出流水线回填 url/localPath 并补 exported=true
    （未导出的消息无 exported 键，故用 total=False 语义按 get 访问）。
    """

    type: Literal["image", "voice", "video", "emoji", "file"]
    fileName: str
    md5: str
    url: str  # 完整下载 URL（含 ?access_token= 查询参数；未导出时为空串）
    localPath: str  # 导出后的本地绝对路径；未导出时为空串
    exported: bool  # 仅导出成功时出现


class WeFlowMessage(TypedDict):
    """REST API 返回的消息。"""

    serverId: str  # 消息唯一 id（字符串，SSE rawid 同值）—— msg_id 来源
    localId: int  # rowid 数字
    localType: int  # 1 = 文本, 3 = 图片, 34 = 语音, 0x500000031 = 文章卡片
    createTime: int  # 秒级 Unix
    sortSeq: int  # 上游水位排序键之一（本仓库不使用）
    isSend: int  # 0 = 收到, 1 = 自己发送
    senderUsername: str  # 发送者 wxid（稳定去重键）
    senderName: str  # 上游已解析的发送者显示名：备注 > 昵称 > wxid（index
    # 期由全局 contacts 算出，无群名片 —— 上游 group_cards 从未被写入）。
    # 与 SSE sourceName 同值；退化为 wxid 时由下游回退 contacts
    content: str  # 解析后文本（媒体消息为 [图片]/[语音] 等占位符）
    rawContent: str  # 原始内容（文章卡片为 <msg><appmsg> XML）
    parsedContent: str
    replyToMessageId: str | None  # 引用目标消息 id（本仓库不使用）
    quote: dict | None  # {platformMessageId, sender, accountName, content, type}
    media: WeFlowMedia | None  # 可解析媒体时携带（导出字段见 WeFlowMedia）


class WeFlowMessagesResponse(TypedDict):
    """GET /api/v1/messages 的完整信封。"""

    success: bool
    talker: str
    count: int
    hasMore: bool
    messages: list[WeFlowMessage]
    media: dict | None  # {count, enabled, exportPath}


class WeFlowContactsResponse(TypedDict):
    contacts: list[WeFlowContact]


logger = logging.getLogger(__name__)

# 媒体下载大小上限：防止异常/恶意上游返回超大文件造成内存放大
_MAX_MEDIA_BYTES = 20 * 1024 * 1024

# 按消息回查 REST（SSE 图片 mediaUrl）的单页条数：倒序响应下目标消息之后
# 120s 窗口内的新消息会把它挤出首页，50 条在刷屏场景不够，放宽到 200
_LOOKUP_LIMIT = 200

# 注册响应的两套词表（weflow-server v0.3.0 起分离，勿混用）：
# - state：本次注册的结果语义（qqflow-server 风格）
# - status：账号状态机当前值（awaiting_key / indexing / ready / error）
# 良性 = 已受理或已就绪，可记忆化，索引期内不重复注册。
_BENIGN_REGISTER_STATES = ("accepted", "already_ready", "in_progress")

# /health 的标量 account 阶段中「引导中」的取值（v0.5.0 起）。ready 单独判定，
# unregistered / error 落到注册分支。注意与上面的 status 词表不是一回事：
# awaiting_key 不会出现在 account 阶段里（未注册即 unregistered）。
_BOOTSTRAPPING_PHASES = ("indexing",)


class WeFlowNotReadyError(SourceError):
    """weflow-server 尚未就绪（503 就绪门控：无账号或正在建索引），瞬态。

    调用方（poller/runtime）捕获后应静默跳过，不视为错误。
    """


class WeFlowClient(SourceClient):
    """封装所有 weflow-server API HTTP 通信。"""

    name = "weflow"  # 源标识，SourceClient 契约成员
    connection_status: ConnectionStatus = "offline"

    def __init__(
        self,
        base_url: str,
        api_token: str,
        wxid: str = "",
        db_path: str = "",
        db_keys: dict[str, str] | None = None,
        img_aes_key: str = "",
        img_xor_key: str = "",
        *,
        sse_read_timeout_ms: int | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_token = api_token
        self._wxid = wxid
        self._db_path = db_path
        self._db_keys = db_keys or {}
        self._img_aes_key = img_aes_key
        self._img_xor_key = img_xor_key
        # SSE 读超时（毫秒）：缺省读 WEFLOW_SSE_READ_TIMEOUT_MS。
        # 上游每 25s 发 ping 保活；半开连接下若无读超时，stream_events 的
        # aiter_lines 会永久阻塞，重连循环永远得不到控制权（监听静默死亡）
        if sse_read_timeout_ms is None:
            sse_read_timeout_ms = WeFlowSettings().sse_read_timeout_ms
        self._sse_read_timeout_s = sse_read_timeout_ms / 1000
        self._client: httpx.AsyncClient | None = None
        self._ready_checked = False
        # 串行化健康检查 + 引导注册（SSE 强制重检与轮询检查并发竞争时避免重复注册）
        self._ready_lock = asyncio.Lock()
        # 上游版本号（/health 的 version）：首次就绪时记一条日志，便于排查
        # 「下游按新契约调用、上游还是旧二进制」的错配
        self._logged_version: str | None = None
        self.connection_status = "offline"

    def sse_timeout(self) -> httpx.Timeout:
        return make_sse_timeout(self._sse_read_timeout_s)

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
            WeFlowNotReadyError: 服务端未就绪（503 就绪门控，瞬态）
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
        if resp.status_code == 503:
            # 就绪门控（瞬态）：失效记忆化标志——服务端重启（内存态注册表丢失）
            # 后，下一轮 ensure_ready 会重新健康检查 + 引导注册（自愈）。
            # 复位不会引发注册风暴：下一轮 ensure_ready 先查 /health，索引期
            # 会看到 indexing 良性态而直接返回，且上游注册已幂等（v0.3.0 起
            # 重复注册 ready/indexing 账号不重建索引）。
            logger.debug("GET %s → 503（服务端索引期，瞬态）", path)
            self._ready_checked = False
            raise WeFlowNotReadyError(
                f"weflow-server 尚未就绪（503）: {resp.text[:200]}"
            )
        if not resp.is_success:
            if not_found_ok and resp.status_code == 404:
                return None
            raise RuntimeError(
                f"WeFlow API error: {resp.status_code} on {path} — {resp.text[:200]}"
            )
        return resp.json()

    # ── 账号引导 ──

    async def fetch_health(self) -> dict[str, Any]:
        """健康检查（免鉴权）。

        返回 {status, version, account}：
        - status：ok（已绑定账号且 ready）/ starting（未注册 / 建索引中 / error）；
        - account：标量阶段 unregistered / indexing / ready / error。
        未注册时也返回 200（status=starting，account=unregistered）。

        本接口**不列出账号**（v0.5.0 起）：它免鉴权，账号清单会向任意调用方泄露
        本机存在哪些账号。明细在需鉴权的 GET /api/v1/accounts（见 fetch_accounts）。
        """
        client = self._get_client()
        resp = await with_connect_retry(lambda: client.get("/health"))
        if not resp.is_success:
            raise RuntimeError(
                f"WeFlow API error: {resp.status_code} on /health — {resp.text[:200]}"
            )
        return resp.json()

    async def register_account(self) -> tuple[str, str]:
        """POST /api/v1/accounts 注册账号（客户端驱动启动），返回 (state, status)。

        - state：本次注册结果 —— accepted（已受理，开始后台构建）/
          already_ready（账号已就绪）/ in_progress（正在构建中）/
          account_conflict（服务端已绑定另一个 wxid，被拒）；
        - status：账号状态机值 —— awaiting_key / indexing / ready / error。
          account_conflict 时上游不下发 status，此处为 "unknown"。

        注册幂等（上游 v0.3.0 起）：重复注册**同一** wxid 且已 ready/indexing 时
        不会重建索引、不中止 watcher，直接返回现有句柄；仅 error/awaiting_key 会
        被替换重建（密钥或路径填错后重新注册即可自愈）。上游 v0.5.0 起强制单账号：
        换 wxid 必须先注销，否则回 account_conflict（冲突判定在密钥校验之前）。
        """
        client = self._get_client()
        payload: dict[str, Any] = {"wxid": self._wxid}
        if self._db_path:
            payload["db_path"] = self._db_path
        if self._db_keys:
            payload["keys"] = self._db_keys
        if self._img_aes_key:
            payload["img_aes_key"] = self._img_aes_key
        if self._img_xor_key:
            payload["img_xor_key"] = self._img_xor_key
        resp = await with_connect_retry(
            lambda: client.post(
                "/api/v1/accounts",
                json=payload,
                headers=self._auth_headers(),
            )
        )
        if not resp.is_success:
            raise RuntimeError(
                f"WeFlow API error: {resp.status_code} on /api/v1/accounts — "
                f"{resp.text[:200]}"
            )
        data = resp.json()
        return data.get("state", "unknown"), data.get("status", "unknown")

    async def ensure_ready(self, force: bool = False) -> None:
        """确保服务端有就绪账号（健康检查驱动，记忆化，可强制重检）。

        先查 /health 的标量 account 阶段（上游 v0.5.0 起）：ready 与 indexing
        即记忆化返回，**不重复注册**；unregistered / error 才注册。注册后进入
        索引期，业务接口的 503 由 WeFlowNotReadyError 瞬态处理兜底，不在此
        阻塞等待。

        force=True 时忽略记忆化标志重新健康检查（SSE 重连后服务端可能已重启，
        内存态账号注册表丢失，需重新注册）。良性态（_BENIGN_REGISTER_STATES /
        _BOOTSTRAPPING_PHASES）记忆；被拒态与网络失败不记忆，下轮重试（自愈）。

        单账号（上游 v0.5.0 起为强制）：阶段判定不按 wxid 过滤——本仓库只配一个
        WEFLOW_WXID，而服务端同时只绑定一个账号，故「服务端就绪」与「我们的账号
        就绪」等价。若配错 wxid，注册会回 account_conflict 而不是静默顶掉在位账号。
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
                logger.info("[weflow] weflow-server 版本: %s", version)
                self._logged_version = str(version)
            phase = health.get("account", "unregistered")
            logger.debug("[weflow] 健康检查: 账号阶段 %s", phase)
            if phase == "ready":
                self._ready_checked = True
                logger.debug("[weflow] 已有就绪账号，跳过注册")
                return
            if phase in _BOOTSTRAPPING_PHASES:
                self._ready_checked = True
                logger.debug("[weflow] 账号建索引中（%s），不再重复注册", phase)
                return
            if phase == "error":
                # /health 只给标量，根因（密钥/路径填错的 error 字符串）只在需
                # 鉴权的明细接口里。仍显式告警——否则用户只能看到业务接口持续
                # 503，看不到原因。诊断失败不能挡住下面的注册重试。
                await self._log_account_errors()
            # `error` 落到注册分支是有意的：服务端的 error 状态不释放绑定，但同一
            # 账号可以直接重试注册来恢复（密钥修正后即生效）。
            logger.info(
                "[weflow] 无就绪账号（阶段 %s），注册账号 wxid=%s "
                "(db_path=%s, keys=%d 个库)",
                phase,
                self._wxid,
                self._db_path or "<默认>",
                len(self._db_keys),
            )
            state, status = await self.register_account()
            if state in _BENIGN_REGISTER_STATES or status in _BOOTSTRAPPING_PHASES:
                self._ready_checked = True
                logger.info("[weflow] 账号注册: state=%s, status=%s", state, status)
            elif state == "account_conflict":
                # 服务端已被**另一个** wxid 占用（v0.5.0 起强制单账号）。重试不会
                # 自愈：得由人去注销那个账号或改配置，所以按 ERROR 报而非 warning，
                # 且不记忆化以便配置修正后自愈。
                logger.error(
                    "[weflow] 注册被拒：服务端已绑定另一个账号（本地配置 wxid=%s）。"
                    "需先注销：DELETE /api/v1/accounts/{占用方wxid}",
                    self._wxid,
                )
            else:
                # 被拒态（error / awaiting_key 等）不记忆化：保持未检查标志让
                # 下一轮重试；否则零账号部署下没有业务 503 兜底复位标志，
                # 注册失败后永不自愈
                logger.warning(
                    "[weflow] 账号注册被拒: state=%s, status=%s（下轮重试）",
                    state,
                    status,
                )

    async def _log_account_errors(self) -> None:
        """拉明细接口把 error 根因打出来（诊断专用，失败仅降级为 debug）。"""
        try:
            accounts = await self.fetch_accounts()
        except Exception as exc:  # noqa: BLE001 —— 诊断路径不得影响注册重试
            logger.debug("[weflow] 账号明细拉取失败（跳过根因诊断）: %s", exc)
            return
        for a in accounts:
            if a.get("state") == "error" and a.get("error"):
                logger.warning(
                    "[weflow] 账号 %s 初始化失败: %s",
                    a.get("wxid") or "<未知>",
                    a["error"],
                )

    async def fetch_accounts(self) -> list[dict]:
        """GET /api/v1/accounts —— 账号明细（诊断用，需鉴权）。

        /health 只报一个标量阶段；出错的具体原因（error 字段）、消息数与服务端
        实际读取的 db_storage 只在这里。除绑定账号外还含启动扫描发现但未注册的
        账号（awaiting_key）。不参与就绪判定，仅供排查。
        """
        client = self._get_client()
        resp = await with_connect_retry(
            lambda: client.get("/api/v1/accounts", headers=self._auth_headers())
        )
        if not resp.is_success:
            raise RuntimeError(
                f"WeFlow API error: {resp.status_code} on /api/v1/accounts — "
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
        不翻页只能拿到前 100 条且无任何错误提示（实测真实账号 4533 条），
        截断外的发送者显示名会退化成 wxid。传大 limit 只是把天花板抬到上游
        硬上限 10000，仍是猜值；翻页才是取尽。
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

    # 不再有 fetch_group_members：该接口的 groupNickname 出自 store.group_cards，
    # 而上游全仓（含测试）没有任何一处写入该字段 —— 两个 Store 构造点都是
    # Default::default() 的恒空 HashMap。实测最近活跃 5 群 208 名成员，
    # groupNickname 非空 0 条；其 displayName 走 sender_display()，群名片分支
    # 恒落空后掉到 contacts.display_name()，与消息自带 senderName 在
    # index.rs 的算法逐字相同。实测 974 条消息两者 974 条同值，纯冗余。

    async def fetch_sessions(self) -> list[WeFlowSession]:
        """获取所有会话列表（原生格式，sessionType 权威，按 offset 翻页取尽）。

        上游 limit 默认 100，不翻页只会发现最近活跃的 100 个会话；上游
        v0.3.0 提供 offset 与 (lastTimestamp, username) 全序稳定排序，故经
        fetch_all_pages 翻页取尽（与 fetch_contacts 同模式）。
        page_size=10000 = 上游 limit 硬上限：典型规模（<10000 会话）一个
        请求即取尽，与旧「显式大 limit」实现请求数相同；旧上游若忽略
        offset，共享「本页无新增」防御立即告警终止，停在 10000 条——
        即旧实现行为，零回退。
        chatlab 格式的 type 实测不可靠（official 会话也返回 private），
        故用原生格式的 sessionType 判定会话类型。
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
        start_ts: int | None,
        limit: int = 500,
        offset: int = 0,
        media: bool = False,
        retry_on_empty: bool = True,
        not_found_ok: bool = False,
    ) -> WeFlowMessagesResponse:
        """获取指定会话的历史消息（返回完整信封，含 hasMore）。

        上游按 createTime 过滤，start 为闭区间下界（createTime >= start 的消息
        均返回，实测含边界）；响应按时间倒序。翻页用 offset 递增至 hasMore=False。

        Args:
            talker: 会话 ID
            start_ts: 起始时间（秒级 Unix 时间戳，含边界）；None 不过滤
            limit: 单页返回条数
            offset: 分页偏移
            media: 是否导出媒体（图片等），为 True 时附加 media=1&image=1，
                消息对象 media 字段携带 url
            retry_on_empty: 空结果是否 500ms 后重试一次（回查链路应传 False；
                轮询首页保留默认 True 以维持既有「刚入库查不到」竞态兜底）
            not_found_ok: 会话不存在（404，如 brandsessionholder 等无消息的
                系统会话）时返回空信封而非抛错；轮询路径应传 True
        """
        params: dict[str, Any] = {"talker": talker, "limit": limit, "offset": offset}
        if start_ts is not None:
            params["start"] = start_ts
        if media:
            params["media"] = 1
            params["image"] = 1
        data = await self._get(
            "/api/v1/messages", params=params, not_found_ok=not_found_ok
        )
        if data is None:
            return {
                "success": True,
                "talker": talker,
                "count": 0,
                "hasMore": False,
                "messages": [],
                "media": None,
            }
        return data

    async def _lookup_message(
        self, talker: str, rawid: str, ts: int, media: bool
    ) -> WeFlowMessage | None:
        """按 serverId 回查 REST 获取消息原始对象。

        用 timestamp 缩小查询范围（start=ts-120 起，客户端再按 ±120s 过滤），
        在返回的消息中匹配 serverId == rawid。回查显式 retry_on_empty=False：
        miss 是常见路径，不应在监听/回填热路径上为空结果白付 500ms 重试。
        """
        start_ts = int((datetime.fromtimestamp(ts, tz=UTC) - timedelta(seconds=120)).timestamp())

        try:
            resp = await self.fetch_messages(
                talker, start_ts, limit=_LOOKUP_LIMIT, media=media,
                retry_on_empty=False,
            )
        except Exception as e:
            logger.warning(f"回查消息失败: {e}")
            raise

        window = 120
        for m in resp.get("messages", []):
            if str(m.get("serverId", "")) != rawid:
                continue
            ct = m.get("createTime", 0)
            if abs(ct - ts) > window:
                continue
            return m
        return None

    async def fetch_message_media(self, talker: str, rawid: str, ts: int) -> str | None:
        """通过 rawid 回查 REST（media=1），获取图片消息的媒体相对路径。

        SSE 事件不含媒体信息，需要回查 REST。用 timestamp 缩小查询范围
        （前后各 120s），在返回的消息中匹配 serverId == rawid。

        Returns:
            媒体相对路径（如 "{talker}/images/xxx.jpg"）或 None
        """
        m = await self._lookup_message(talker, rawid, ts, media=True)
        if m is None:
            return None
        media = m.get("media")
        if media is not None and media.get("type") == "image" and media.get("url"):
            return self._extract_media_path(str(media["url"]))
        logger.debug(
            "SSE 消息 %s media=%s 无图片 URL",
            rawid,
            media.get("type") if media else None,
        )
        return None

    # ── 媒体 ──

    @staticmethod
    def _extract_media_path(url: str) -> str | None:
        """从完整媒体 URL 提取相对路径（去掉 /api/v1/media/ 前缀与查询串）。

        "http://127.0.0.1:5033/api/v1/media/{talker}/images/abc.jpg?access_token=..."
        → "{talker}/images/abc.jpg"
        """
        idx = url.find("/api/v1/media/")
        if idx < 0:
            return None
        path = url[idx + len("/api/v1/media/"):]
        # 去掉查询串
        return path.split("?", 1)[0]

    def _build_media_url(self, path: str) -> str:
        """将媒体相对路径规范化为 weflow-server 完整 URL。

        用 RFC 3986 的 URL join 拼接：绝对路径引用会替换掉 base 自带的
        路径与查询串，_base_url 即使误配成
        "http://127.0.0.1:5033/api/v1/push/messages?access_token=..."
        也不会把媒体路径拼进查询串（朴素字符串拼接会打出坏 URL 导致图片挂死）。
        """
        return str(
            httpx.URL(self._base_url).join(f"/api/v1/media/{path.lstrip('/')}")
        )

    async def download_media(self, path: str) -> bytes:
        """下载媒体文件原始字节（GET /api/v1/media/{path}，带鉴权）。

        404（媒体未导出/缓存被清理）/ 503（就绪门控）等非成功状态统一映射为
        MediaError，由 pipeline 跳过 OCR、server 代理映射为 404 兜底。

        Args:
            path: 媒体相对路径（normalize 从 media.url 提取）

        Returns:
            媒体文件原始内容

        Raises:
            MediaError: 网络错误或 weflow-server 返回非成功状态（cause 保留原异常）
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

    # ── SSE 流 ──

    def _push_url(self) -> str:
        """SSE 推送地址（RFC 3986 join）：base_url 误带路径/查询串时不会拼坏。"""
        return str(httpx.URL(self._base_url).join("/api/v1/push/messages"))

    async def stream_events(self) -> AsyncIterator[WeFlowEvent]:
        """SSE 实时消息流 — 异步迭代器，持续产出解析后的 JSON 事件。

        用法:
            async for event in client.stream_events():
                process(event)

        连接失败（非 2xx，含 503）时置 offline 并正常返回，
        由监听器的退避重连循环处理。
        连接成功（HTTP 200）后会强制重做一次就绪检查/引导注册，自愈
        服务端重启导致的注册表丢失（注册幂等 + 先查 /health，已就绪账号
        只会命中健康检查短路，不会触发索引重建）。
        """
        logger.debug("SSE 连接中...")
        self.connection_status = "reconnecting"

        # SSE 需要独立的客户端：写/池不限，但保留可配置的读超时
        # （上游 25s ping 保活）以自愈半开连接
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
                        logger.warning(f"SSE 连接失败: {resp.status_code}")
                        self.connection_status = "offline"
                        return

                    logger.info("SSE 已连接")
                    self.connection_status = "online"

                    # 服务端重启后账号注册表（内存态）丢失；SSE HTTP 200 是服务端
                    # 已恢复的可靠信号，强制重做一次就绪检查 + 引导注册。
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

    async def close(self) -> None:
        """关闭 HTTP 客户端。"""
        if self._client is not None:
            await self._client.aclose()
            self._client = None
