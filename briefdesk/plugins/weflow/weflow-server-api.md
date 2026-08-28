# weflow-server HTTP API（v2 参考）

> 本文档以源码（`src/server/`）为准，描述当前实现的全部接口。
> 与 WeFlow 安装版 `HTTP-API.md` 契约对齐的部分在原文处标注。

- 服务默认监听 `127.0.0.1:5033`（`--port` 可改）
- 数据目录 = `%LOCALAPPDATA%\weflow-server`；访问 token 存于**系统凭据库**（Windows 凭据管理器 / macOS 钥匙串 / Linux Secret Service；无凭据库平台为会话级 token，随启动日志打印）
- 所有 `/api/v1/*` 端点需要鉴权；`/health` 与 `/api/v1/health` 不需要

## 鉴权

五种传输等价（源码 `server/auth.rs::authorized`），按下表顺序检查：

| 方式 | 示例 |
|---|---|
| HTTP 头 | `Authorization: Bearer <token>` |
| HTTP 头 | `X-Api-Key: <token>` |
| 查询参数 | `?access_token=<token>` |
| 查询参数 | `?token=<token>` |
| POST JSON body | `{"access_token": "<token>"}`（或 `"token"`） |

> query 与 body 合并为同一参数表（body 优先），故后三种在实现上是同一处检查。

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
  "version": "<version>",
  "accounts": [
    { "wxid": "wxid_xxxx_1234", "state": "ready", "message_count": 217272 }
  ]
}
```

- `status`：`ok`（**已注册**账号至少一个且全部 `ready`）或 `starting`（无已注册账号 /
  仍在 indexing / 有 error）。
- `accounts[]` 除已注册账号外，还包含启动扫描发现但**尚未注册**的账号（`awaiting_key`）。
  这些条目**不参与** `status` 判定：本机存在未注册的账号目录属正常状态，若让它们拉低
  就绪判定，`status` 会永远停在 `starting`。已注册账号为空时 `status` 也是 `starting`。
- `accounts[].state` ∈ `awaiting_key | indexing | ready | error`（与账号状态机一致）；
  账号处于 `error` 时附 `error` 字符串（错误原因），`ready` 时不含该键。
- 列表按 `wxid` 升序，便于客户端做稳定 diff。

### POST `/api/v1/accounts` — 注册账号（客户端驱动启动）

请求体（JSON）：

```json
{
  "wxid": "wxid_xxxxxxxxxxxxxxxx_1234",
  "db_path": "D:\\AppData\\xwechat_files\\wxid_xxxx_xxxx",
  "keys": { "session/session.db": "<64-hex enc_key>", "message/message_0.db": "<64-hex>" },
  "img_aes_key": "<16 位 hex>",
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

消息对象键：

```json
{
  "localId": 1,
  "serverId": "8280000000000000001",
  "localType": 1,
  "createTime": 1700000100,
  "sortSeq": 0,
  "isSend": 0,
  "senderUsername": "wxid_friend_a",
  "senderName": "客户张三",
  "content": "你好",
  "rawContent": "你好",
  "parsedContent": "你好",
  "replyToMessageId": null,
  "quote": { "platformMessageId": "...", "sender": "...", "accountName": "...", "content": "...", "type": 1 } | null,
  "media": { "type": "image|voice|video|emoji|file", "fileName": "...", "md5": "...", "url": "", "localPath": "" } | null
}
```

- `serverId` 为**字符串**：i64 超出 JS 安全整数范围，直接出数字会在浏览器端丢精度。
- `isSend` 为数字 `0`（对方/系统）或 `1`（自己），不是布尔。
- `media` 只要消息可解析出媒体就存在（与 WeFlow 形状一致），与 `media=1` 无关；未导出时
  `url` / `localPath` 为**空字符串**而非缺键，客户端可稳定取字段。

响应：

```json
{ "success": true, "talker": "wxid_friend_a", "count": 20, "hasMore": true,
  "media": { "enabled": false, "exportPath": "", "count": 0 },
  "messages": [ ... ] }
```

媒体导出：`media=1`（可再叠加类型开关）时，服务端按需导出本页媒体，并就地补齐每条消息
的 `media.url` / `media.localPath` / `media.exported`；顶层 `media` 为汇总：

```json
{ "enabled": true, "exportPath": "C:\\...\\api-media", "count": 5 }
```

```json
"media": { "type": "image", "fileName": "...", "md5": "...",
           "url": "http://127.0.0.1:5033/api/v1/media/<talker>/images/<file>?access_token=...",
           "localPath": "C:\\...\\api-media\\...", "exported": true }
```

- **`exported: true` 是导出成功的唯一判据**；仅有 `media` 对象不代表字节可取（缺 md5 的
  非语音消息会被跳过），顶层 `media.count` 等于本页 `exported` 为真的条数。
- 单次请求最多导出 200 项以限制延迟，超出部分保持未导出，可缩小 `limit` 分批取。
- `url` 由 `--base-url`（未指定时按 `host:port` 推导）拼出；表情可能返回 CDN 绝对地址。
- `type`→目录映射：`images / voices / videos / emojis`。

> 文件附件（`file`）暂不参与导出：与 WeFlow 官方契约一致，媒体导出仅覆盖图片/语音/视频/表情四类。

### GET/POST `/api/v1/sessions` — 会话列表

参数：`limit`、`offset`、`keyword`、`format/chatlab`。

```json
{ "success": true, "count": 315, "sessions": [
  { "username": "wxid_xxx@chatroom", "displayName": "项目群",
    "type": 1, "sessionType": "group",
    "lastTimestamp": 1700000100, "unreadCount": 2, "messageCount": 4, "summary": "..." }
] }
```

`type` 为数值枚举：`0` 私聊、`1` 群聊、`2` 公众号、`3` 其他；`sessionType` 是同一枚举的
字符串形式（`private` / `group` / `official` / `other`）。**下游建议用 `sessionType`**：
qqflow-server 的 `type` 取值为 `1` 私聊 / `2` 群聊，数值含义与本项目不同，字符串则一致。

按 `lastTimestamp` 降序、`username` 次键（全序稳定，便于 offset 翻页）。
`format=chatlab` / `chatlab=1` 时改为输出
`{ "sessions": [ { "id", "name", "platform", "type", "messageCount", "lastMessageAt" } ] }`，
其中 `type` 为 `group` / `private` 字符串。

### GET `/api/v1/sessions/{id}/messages` — ChatLab 拉取（消息游标）

参数：`since`、`end`、`limit`（默认/上限 5000）、`offset`；`talker` 由路径段 `{id}` 提供。
返回 ChatLab 契约形状（`platformMessageId` 等键），并附带 `sync` 游标块：

```json
{ "chatlab": { "version": "0.0.2", "generator": "weflow-server", "exportedAt": 1700000000 },
  "meta": { "name": "项目群", "platform": "wechat", "type": "group", "groupId": "...@chatroom", "ownerId": "wxid_self" },
  "members": [], "messages": [],
  "sync": { "hasMore": true, "nextSince": 1700000103, "nextOffset": 0, "watermark": 1700000200 } }
```

**游标语义**（与 qqflow-server 一致）：

- `since` **排他**（`create_time > since`），`end` 包含（`<= end`）。因此把上一页的
  `nextSince` 原样传回不会重复取到边界那一条；
- 页面按时间戳整秒组补齐：达到 `limit` 后仍会把当前秒的剩余消息取完，故一页可能
  略多于 `limit`。这保证 `nextSince`（本页最后一条的时间戳）一定能前进，不会因为
  同秒消息被切断而卡住；
- `nextSince` 是**本页**最后一条的时间戳，不是整个会话的最大时间戳；
- `nextOffset` 常为 `0`：`since` 排他 + 整秒组对齐后，重新过滤已经排除了本页全部行，
  下一条未读就在偏移 0。仅当时间戳无法前进的退化情形才返回非 0。**两个游标都应原样
  回传**；若把 `nextOffset` 当成"累计已读条数"再叠加，会二次跳过同一批行；
- `watermark` 是本次拉取的时间上界（`end` 或当前时间），不是最新消息的时间戳；
  排空后（`hasMore=false`）`nextSince` 停在该上界、`nextOffset` 归 0，可作为下次
  增量拉取的起点。

按上述规则循环直到 `hasMore=false`，可完整取回会话全部消息且不重复
（真库验证：3960 条 / 80 页，无丢无重）。

### GET/POST `/api/v1/contacts` — 联系人

参数：`limit`（默认 100，上限 10000）、`offset`、`keyword`。

```json
{ "success": true, "count": 100, "total": 4533, "hasMore": true, "contacts": [
  { "username": "wxid_friend_a", "displayName": "客户张三", "nickname": "...",
    "remark": "客户张三", "alias": null, "avatarUrl": null, "type": "friend" }
] }
```

`displayName` 按 `remark > nickname > username` 解析，恒为字符串；`nickname`、`remark`、
`alias`、`avatarUrl` 源自联系人行，缺值时为 `null`（非空字符串）。

**必须翻页**：`limit` 默认 100，不传就只拿到前 100 条（实测真实账号 4533 条），
截断外的联系人在下游会退化为显示 UID。按 `offset` 递增直到 `hasMore=false`：

- `total` 为过滤后总数，与 `offset` 无关；`count` 是本页条数；
- 排序键为 `(displayName, username)`——显示名不唯一，仅按显示名排序时并列项
  在多次请求间顺序不定，offset 翻页会漏行/重复行；加 username 次键保证全序稳定；
- `offset` 超出末尾返回空页且 `hasMore=false`。

### GET/POST `/api/v1/group-members` — 群成员

参数：`chatroomId`（或 `talker` 别名）、`includeMessageCounts=1`、`forceRefresh=1`
（先跑一次增量同步保证成员集合最新）。

```json
{ "success": true, "chatroomId": "...@chatroom", "count": 81, "refreshed": true, "members": [
  { "wxid": "wxid_member_b", "displayName": "...", "nickname": "...", "remark": "",
    "alias": "", "groupNickname": "", "avatarUrl": "",
    "isOwner": false, "isFriend": true, "messageCount": 0 }
] }
```

成员标识键为 `wxid`（非 `username`）。`messageCount` 仅在 `includeMessageCounts=1`
时为真实值，否则恒为 `0`；`isOwner` 当前始终 `false`（群主信息不在已解析的表中）。

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
  新账号的事件（旧实现每次注册新建总线，订阅者会静默失聪且不断线）；
- 业务端点（`messages`/`sessions`/…）**仍有** 503 门控——索引未建完确实无法查询，
  与此处语义不同；
- `wxid` 查询参数仅作语义提示，不影响订阅内容（总线为进程级，非按账号隔离）。

事件（`event:` 名）：

| 事件 | data 形状 |
|---|---|
| `ready` | `{"status":"ok"}`（连接建立基线） |
| `message.new` | `{"event":"message.new","sessionId":"...","sessionType":"group","rawid":"...","sourceName":"...","groupName":"...","content":"...","timestamp":1700000100,"media":{"type":"image","fileName":"...","md5":"..."}}` |
| `message.revoke` | 同上（`event` 为 `message.revoke`） |
| `sync` | `{"event":"sync","watermarks":[...]}`（水位基线/重基） |

- 帧携带 `id:` 序号；`Last-Event-ID` 头（或查询参数）可回放最近 **1000 条 / 10 分钟**
  （序号为总线级单调值，跨账号注册保持连续）
- 每 25 秒发送 `ping` 注释帧保活
- `message.new` 的 `media` 仅在消息含图片/语音/视频/表情时出现，否则为 `null`。
  它是**元数据**：不含 `url` / `localPath`（字节走 REST `/api/v1/messages?media=1` 导出），
  也绝不含解密用的 `aes_key`。推空占位链接只会让客户端误以为有可取地址。
- 订阅端滞后（broadcast 缓冲被覆盖）时补发一帧 `sync`，携带**当前真实水位**，客户端可据此
  重新增量拉取。该帧不占用总线序号（它只针对这一个滞后订阅者，占号会导致其他客户端跳号）。
- 进程收到退出信号时，服务端主动结束所有 SSE 流（不等宽限期超时），客户端会看到连接正常关闭。

### GET/POST `/api/v1/sync` — 手动增量同步

立即跑一次水位增量同步（正常情况下由文件监视自动触发，此端点用于强制对账）：

```json
{ "success": true, "newMessages": 3, "revokeMessages": 0 }
```

撤回计数键为 `revokeMessages`。新消息同时会通过 SSE 推给已订阅的客户端。

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
