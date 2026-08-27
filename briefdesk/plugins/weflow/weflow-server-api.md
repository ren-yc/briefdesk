# weflow-server HTTP API（v2 参考）

> 本文档以源码（`src/server/`）为准，描述当前实现的全部接口。
> 与 WeFlow 安装版 `HTTP-API.md` 契约对齐的部分在原文处标注。

- 服务默认监听 `127.0.0.1:5033`（`--port` 可改）
- 数据目录 = `%LOCALAPPDATA%\weflow-server`；访问 token 存于**系统凭据库**（Windows 凭据管理器 / macOS 钥匙串 / Linux Secret Service；无凭据库平台为会话级 token，随启动日志打印）
- 所有 `/api/v1/*` 端点需要鉴权；`/health` 与 `/api/v1/health` 不需要

## 鉴权

三种方式等价（源码 `server/handlers/mod.rs::authorized` 三通道）：

| 方式 | 示例 |
|---|---|
| HTTP 头 | `Authorization: Bearer <token>` |
| HTTP 头 | `X-Api-Key: <token>` |
| 查询参数 | `?access_token=<token>` 或 `?token=<token>` |

> 所有通道的 token 比对均为**常时比较**（constant-time，无提前返回，防时序侧信道；与 qqflow-server 同款）。

## 错误信封

```json
{ "success": false, "code": 400, "message": "..." }
```
- `success` 布尔；`code` 为 HTTP 状态码；`message` 人类可读描述。
- 鉴权失败：`401`；拒绝访问：`403`；参数错误：`400`；未找到：`404`；服务异常：`500`。

## 通用查询参数

| 参数 | 说明 |
|---|---|
| `limit` | 条数上限（默认 100，上限 10000） |
| `offset` | 偏移量（默认 0） |
| `start` / `end` | 时间边界（Unix 秒；可用于 `now-3600` 形式相对时间，见 `parse_time_bound`） |
| `keyword` | 关键词过滤（小写匹配；sessions/contacts/messages 通用） |
| `format=chatlab` / `chatlab=1` | 输出 ChatLab 风格形状 |
| 导出开关 | `media=1`（或 `meiti=1`）开启媒体导出；再按类型 `image=1`/`voice=1`/`video=1`/`emoji=1`（兼容拼音别名 `tupian=1`、`vioce=1`） |

## 端点

### GET/POST `/health`、`/api/v1/health`（免鉴权）

全局就绪状态 + 每账号状态列表（客户端可据此健康检查，无需轮询注册端点）：

```json
{
  "status": "ok",
  "version": "0.2.0",
  "accounts": [
    { "wxid": "wxid_xxxx_1234", "state": "ready", "message_count": 217272 }
  ]
}
```

- `status`：`ok`（至少一个账号且全部 `ready`）或 `starting`（无账号 / 仍在 indexing / 有 error）。
- `accounts[].state` ∈ `awaiting_key | indexing | ready | error`（与账号状态机一致）；账号处于 `error` 时附 `error` 字符串（错误原因）。

### POST `/api/v1/accounts` — 注册账号（客户端驱动启动）

请求体（JSON）：

```json
{
  "wxid": "wxid_xxxxxxxxxxxxxxxx_1234",
  "db_path": "D:\\AppData\\xwechat_files\\wxid_xxxx_xxxx",
  "keys": { "session/session.db": "<64-hex enc_key>", "message/message_0.db": "<64-hex>" },
  "img_aes_key": "ce1ddac1d2ed49fe",
  "img_xor_key": "0x64"
}
```

| 字段 | 说明 |
|---|---|
| `wxid` | 账号标识（必填） |
| `db_path` | 账号根目录（含 `db_storage/`；默认按 wxid 推导） |
| `key` | 可选：每库统一 enc_key（`keys` 缺省时的单一密钥） |
| `keys` | 可选：`db_storage` 相对路径 → 64-hex enc_key 映射（微信 4.x 每库独立密钥） |
| `img_code` | 可选：WeFlow 兼容的图片密钥代号（由服务端派生 aes/xor） |
| `img_aes_key` / `img_xor_key` | 可选：直接指定图片解密密钥（优先于 `img_code`） |

响应：

```json
{ "success": true, "wxid": "wxid_xxxx_1234", "state": "accepted", "status": "indexing", "db_storage": "D:\\AppData\\xwechat_files\\wxid_xxxx_1234\\db_storage" }
```

- `state`：注册结果语义（qqflow-server 风格）——`accepted`（已接受，开始后台构建）/ `already_ready`（重复注册，账号已就绪）/ `in_progress`（重复注册，正在构建中）；
- `status`：账号当前状态机值 ∈ `awaiting_key | indexing | ready | error`；
- `db_storage`：实际使用的库目录。

行为契约：
- **密钥仅存进程内存，不落盘**；服务重启后需重新注册。
- 注册时对目标库做页 1 HMAC 预校验（`wcdb::verify_page1`），错钥立即拒绝（`400`）。
- 成功后启动阻塞式全量构建 + 文件事件监视任务；构建完成前账号状态为 `indexing`。
- **注册幂等**（对齐 qqflow-server）：重复注册已 `ready`/`indexing` 的账号**不会重建索引**、不会中止 watcher，直接返回现有句柄（`state` 为 `already_ready`/`in_progress`）；仅 `error`（或 `awaiting_key`）状态的账号会被替换重建——密钥 / 路径填错后重新注册即可自愈。

### GET/POST `/api/v1/messages` — 消息查询 + 媒体导出

参数：`talker`（会话标识，必填，为空返回空结果）、`limit`、`offset`、`start`、`end`、
`keyword`、`format/chatlab`、`media` 及类型开关（见通用参数）。

消息对象键（节选）：

```json
{
  "localId": 1,
  "serverId": "8280000000000000001",
  "localType": 1,
  "createTime": 1700000100,
  "isSend": false,
  "sender": "wxid_friend_a",
  "senderName": "客户张三",
  "content": "你好",
  "parsed": { "text": "你好", "display": "你好" },
  "quote": { "reversed": false, "senderName": "...", "text": "..." } | null,
  "revoke": { "msgId": "...", "newMsgId": "...", "replaceMsg": "..." } | null,
  "media": { "kind": "image|voice|video|emoji", "md5": "...", "file_name": "...", "width": 0, "height": 0, "duration": 0 } | null
}
```

响应：

```json
{ "count": 20, "hasMore": true, "messages": [ ... ] }
```

媒体导出：当 `media=1` 且类型开关开启，响应附 `media` 数组，每项：

```json
{ "localId": 7, "kind": "image", "file": "images/2025-08/aabb....jpg", "url": "/api/v1/media/<talker>/images/<file>" }
```

`kind`→目录映射：`images / voices / videos / emojis`。

> 文件附件（`file`）暂不参与导出：与 WeFlow 官方契约一致，媒体导出仅覆盖图片/语音/视频/表情四类。

### GET/POST `/api/v1/sessions` — 会话列表

参数：`limit`、`offset`、`keyword`、`format/chatlab`。

```json
{ "count": 315, "sessions": [
  { "sessionId": "wxid_xxx@chatroom", "kind": "group", "displayName": "项目群",
    "unreadCount": 2, "messageCount": 4, "latestText": "...", "latestTime": 1700000100 }
] }
```

### GET `/api/v1/sessions/{id}/messages` — ChatLab 拉取（消息游标）

参数同 `/api/v1/messages`（`talker` 由路径段 `{id}` 提供）；返回 ChatLab 契约形状
（`platformMessageId` 等键在 ChatLab 模式启用时输出）。

### GET/POST `/api/v1/contacts` — 联系人

参数：`limit`（默认 100，上限 10000）、`offset`、`keyword`。

```json
{ "success": true, "count": 100, "total": 4538, "hasMore": true, "contacts": [
  { "username": "wxid_friend_a", "nickname": "...", "remark": "客户张三", "alias": null, "avatar": null }
] }
```

**必须翻页**：`limit` 默认 100，不传只拿到前 100 条（实测真实账号 4538 条），
截断外的发送者显示名在下游退化为 wxid。按 `offset` 递增到 `hasMore=false`：

- `total` 为过滤后总数，与 `offset` 无关；`count` 是本页条数；
- 排序键为 `(displayName, username)`——显示名不唯一，仅按显示名排序时并列项
  在多次请求间顺序不定，offset 翻页会漏行/重复行；
- `offset` 超出末尾返回空页且 `hasMore=false`。

下游走 `sources_base.fetch_all_pages`（`page_size=5000`）取尽。

### GET/POST `/api/v1/group-members` — 群成员

参数：`chatroomId`（或 `talker` 别名）、`includeMessageCounts=1`、`forceRefresh=1`
（先跑一次增量同步保证成员集合最新）。

```json
{ "count": 81, "refreshed": true, "members": [
  { "username": "wxid_member_b", "nickname": "..." }
] }
```

### GET/POST `/api/v1/media/{talker}/{media_type}/{file}` — 导出媒体直服

- `media_type` ∈ `images|voices|videos|emojis`
- 双重防穿越：路径段拒绝 `..`/`./`/`\`，且 `canonicalize` 后必须落在导出目录内
- 404 信封返回 `{ "error": "Media not found" }`；内容按扩展名推断 MIME 输出

### GET/POST `/api/v1/push/messages` — SSE 事件流（免轮询推送）

**无就绪门控**（对齐 qqflow-server）：事件总线与重放历史挂在进程级状态上，不属于
任何单个账号。因此——

- **零账号时连接返回 200**（不是 503），先收到 `ready` 基线；账号注册并建索引完成后
  事件自然流入同一条连接，客户端无需在冷启动期退避重连；
- **替换 `error` 态账号不会孤儿化订阅者**：改正密钥后重注册，已连接的客户端继续收到
  新账号的事件；
- 业务端点（`messages`/`sessions`/…）**仍有** 503 门控——索引未建完确实无法查询，
  与此处语义不同；
- `wxid` 查询参数仅作语义提示，不影响订阅内容（总线为进程级，非按账号隔离）。

事件（`event:` 名）：

| 事件 | data 形状 |
|---|---|
| `ready` | `{"status":"ok"}`（连接建立基线） |
| `message.new` | `{"event":"message.new","sessionId":"...","sessionType":"group","rawid":"...","sourceName":"...","groupName":"...","content":"...","timestamp":1700000100}` |
| `message.revoke` | 同上（`event` 为 `message.revoke`） |
| `sync` | `{"event":"sync","watermarks":[...]}`（水位基线/重基）；订阅端滞后时为 `{"rebased":true}` |

- 帧携带 `id:` 序号；`Last-Event-ID` 头（或查询参数）可回放最近 **1000 条 / 10 分钟**
  （序号为总线级单调值，跨账号注册保持连续）
- 每 25 秒发送 `ping` 注释帧保活

### GET/POST `/api/v1/sync` — 手动增量同步

```json
{ "newMessages": 3, "revoked": 0 }
```

### SNS（朋友圈，本地缓存只读）

| 端点 | 参数 | 响应键 |
|---|---|---|
| `/api/v1/sns/timeline` | `limit`、`offset`、`username`、`start`、`end` | `{count,total,feeds:[...]}` |
| `/api/v1/sns/usernames` | — | `{success,count,usernames:[...]}` |
| `/api/v1/sns/stats` | — | `{feeds, ...}` |
| `/api/v1/sns/export` | `format=json\|html`、`username` | `{count, entries/...}` |
| `/api/v1/sns/export/stats` | — | `{data:{totalPosts: N, ...}}` |
| `/api/v1/sns/media/proxy` | `url`、`referer`、`user_agent` | 媒体字节流（CDN 鉴权墙时返回明确错误） |

feeds 条目字段（以源码 `sns.rs` 为准）：`tid/userName/content(明文XML 解析后)/likes/comments/mediaList/location/rawXml` 的等效 JSON 键
（`mediaList` 每项含 `url/thumb/md5/width/height`）。

## 数据获取与安全模型

- **活库直读**：对微信加密库持只读长连接（`db/live.rs`，qqflow 式），`PRAGMA query_only`，
  全程不写源库、**不在磁盘生成明文镜像/快照**。
- **变更检测**：轮询以 (主文件 mtime/size, wal mtime/size) 双指纹判断变化，仅对变化库做
  水位增量查询 `(create_time, sort_seq, local_id) > watermark`。
- **密钥策略**：注册入内存、重启失效需重注册；日志、导出、响应均不打码密钥值。
- **媒体**：图片 dat(V1/V2/XOR) 解密、语音 silk 合并、视频明文直通、wxgf(HEVC)→PNG 需
  ffmpeg（环境变量 `WEFLOW_SERVER_FFMPEG` → WeFlow 内置 ffmpeg → PATH）。

## 启动示例

```powershell
weflow-server.exe --port 5033 --watch-fallback-ms 5000 --log info
```

参数：`--port`、`--host`、`--data-dir`、`--log`、`--watch-debounce-ms`、`--watch-fallback-ms`、
`--media-export-dir`、`--base-url`（全部仅命令行，无配置文件）。

---

## 下游实测补注（briefdesk 侧，本文档其余部分与上游 `docs/` 逐字同步）

以下为对接时实测（weflow-server v0.3.0 源码 + 真库运行）与上文描述不一致之处，
**以实测为准**；上游修正文档后本节可删。

1. **`/api/v1/sessions` 响应键与上文示例不符。** 实际（`sessions.rs`）为
   `username / displayName / type / sessionType / lastTimestamp / unreadCount /
   messageCount / summary`，而非上文的 `sessionId / kind / latestText / latestTime`。
   `sessionType` ∈ `group | private | official | other`（`store::SessionKind`），
   `type` 为该枚举序号（private=0 / group=1 / official=2 / other=3）。
   下游以 `sessionType` 为权威、`type` 为兜底。
2. **消息内 `media` 对象键为驼峰，且导出字段原地回填。** 实际（`messages.rs::message_json`）
   恒下发 `type / fileName / md5` 与空串 `url / localPath`；`media=1` 且该条导出成功时，
   导出流水线回填 `url`（完整 URL，含 `?access_token=`）与 `localPath`，并补 `exported: true`
   （未导出的消息无 `exported` 键）。响应顶层的 `media` 是导出汇总
   `{enabled, exportPath, count}`，**不是**上文所述的每项 `{localId, kind, file, url}` 数组。
3. **`sync` 事件常态形状为 `{event, watermarks[]}`**（`push_events.rs::serialize_event`），
   `{"rebased":true}` 仅在订阅端滞后（broadcast lagged）时发出。账号建索引完成时也会
   发一帧 `sync` 作为水位基线（26 库账号的 `watermarks` 数组很长，调试时注意刷屏）。
4. **`ready` 基线帧的 data 为 `{"status":"ok"}`，不含 `event` 键**——事件名只在 SSE 的
   `event:` 帧头里。只解析 `data:` 行的客户端读不到事件名，需按此判别。
5. **`/api/v1/contacts` 的默认 `limit=100` 会静默截断通讯录**（实测 100/4538）。
   已在上游补 `offset` + `(displayName, username)` 唯一排序 + `total`/`hasMore`，
   下游经 `fetch_all_pages` 翻页取尽。`/api/v1/sessions` **仍无 `offset`**，
   只能显式传 `limit=10000`（天花板，非取尽）；`/api/v1/group-members` 无 limit，
   不受影响。
6. **`/api/v1/group-members` 的 `groupNickname` 恒空，该端点对显示名无增量。**
   实际响应键为 `wxid / displayName / nickname / remark / alias / groupNickname /
   avatarUrl / isOwner / isFriend / messageCount`（`group_members.rs`），非上文示例的
   `username / nickname`。其中 `groupNickname` 取自 `store.group_cards`，而该字段
   **在上游全仓（含测试）没有任何一处写入**——两个 `Store` 构造点（`index.rs`、
   `sync/mod.rs` 测试）都是 `..Default::default()` 的恒空 map。因此 `displayName`
   走的 `sender_display()` 群名片分支恒落空，掉到 `contacts.display_name()`，
   与消息自带 `senderName` 在 `index.rs::row_to_record` 的算法逐字相同。
   实测最近活跃 5 群 208 名成员 `groupNickname` 非空 0 条；974 条消息两链 974 条
   同值。**下游据此不再调该端点**，显示名直取 `senderName`，退化为 wxid 时回退
   contacts。上游若日后补上 `group_cards` 填充，需重新评估。
7. **已知上游缺陷（未修，下游已兜底）**：`sessions` 端点会列出无消息表的聚合会话
   （如 `brandsessionholder`，`messageCount=0`），但 `messages?talker=` 对其返回 404，
   两侧口径不一致。详见仓库外的缺陷报告；下游以 `fetch_messages(not_found_ok=True)`
   降级为空信封，回填时静默跳过。