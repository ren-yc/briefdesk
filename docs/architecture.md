# 架构（Architecture）

> 本文档是 briefdesk 架构细节的单一汇总处，面向需要理解或修改代码的开发者与 AI agent。
> **同步更新义务**：修改模块职责、插件体系、DB schema、server 路由或配置项时，必须同步更新本文对应小节。
> 分工：`README.md` 面向使用者的快速上手；`AGENTS.md` 只承载协作规则与质量门禁；本文承载全部架构细节。

## 定位

简报台（Brief Desk）is a local web app that monitors group chat messages via pluggable message sources (default: WeFlow HTTP API), classifies them with AI, deduplicates across groups, optionally OCRs attached images, and displays structured, deduplicated information briefs (default categories: 活动通知、社团招新、学术、交易、实习；可在设置中自定义). Multiple sources can run concurrently (each source is a plugin, enabled via `PLUGINS` config).

## 总览与数据流

```
WeFlow :5031                              qqflow-server :5032
  ├─ SSE /api/v1/push/messages             ├─ SSE /api/v1/push/messages
  └─ REST /api/v1/messages                 └─ REST /api/v1/messages
                                           └─ REST /api/v1/media/{id}（图片字节）
              │                                          │
              └────────────────┬─────────────────────────┘
                               ↓
        briefdesk/plugins/weflow|qqflow/
        （sse.py 实时监听 / poller.py 回填 / normalize.py 归一化为 InternalMessage 并预滤噪音）
                               ↓
        pipeline 入口统一过滤（IGNORE_SELF 自消息 / 启用会话 / 已处理 /
        OCR 未启用时纯占位符图片屏蔽）+ raw_messages 批量落库
                               ↓
        阶段槽位 enrich：plugins/ocr（源客户端下载图片字节 → RapidOCR 文字脱敏后替换 content）
                               ↓
        阶段槽位 classify：plugins/classify（并行分批 → AI 经 ai_ports → plugins/ai_provider）
                               ↓
        阶段槽位 dedup：plugins/dedup（嵌入余弦预筛（可选）+ AI 语义判重 → 入库/缓存）
                               ↓
        阶段槽位 post_insert：plugins/merge（会话内同话题片段合并判官）
                               ↓
        briefdesk/db.py（SQLite via aiosqlite）
                               ↓
        briefdesk/realtime.py（进程内 pub/sub）→ server `/api/stream` SSE
                               ↓
        briefdesk/server/（FastAPI :3000 —— 中间件/路由/媒体代理/静态托管/插件注入）
                               ↓
        ui/（原生 HTML/CSS/JS SPA，无框架）
```

## 核心模块

| Module | Role |
|---|---|
| `main.py` (root) | 3-line shim for `python main.py` backward compatibility. Delegates to `briefdesk.main:main`. |
| `briefdesk/main.py` | Real entry point — runtime lifecycle only (业务编排见 `poll_cycle.py`). **新增消息源 = 在 `briefdesk/plugins/` 实现 `SourceRuntime` 并以插件发布（entry point 组 `briefdesk.plugins`，启用走 `PLUGINS`/`PLUGINS_DISABLED`）**（其余接线走 `SourceRuntime` 协议）。Startup order: init DB → purge expired ignored（`IGNORED_EXPIRY_HOURS` > 0 时）→ `PluginManager.setup_all()`（发现并装配插件：源插件经 `ctx.register_source` 注册、阶段插件经 `ctx.register_stage` 注册、dedup 插件在 setup 内完成去重缓存预热、`ctx.dedup` 服务端口就绪、Web 插件经 `ctx.register_router`/`ctx.register_plugin_assets` 注册；按名注册到 server，零源报错）→ Web 插件挂载（`include_plugin_router` 展开路由插到 SPA mount 前 + 静态资源注册 + `set_plugins_info_callback(manager.infos)`）→ start uvicorn → wait for `server.started` → `activate_all()` + 逐个 `source.start()`（启动实时监听）→ background initial sync (`trigger_sync`). Registers SIGINT/SIGTERM graceful shutdown via `_install_signal_handlers` (Windows falls back to `signal.signal` + `call_soon_threadsafe`). The Ctrl+C handler only does what must precede `should_exit` — `signal_shutdown()` (otherwise uvicorn's graceful exit waits on the long-lived `/api/stream` ASGI tasks) — then sets `server.should_exit`; all cleanup lives in `_run()`'s single `finally` (covers both graceful and exception paths): initial sync → `PluginManager.teardown_all()` (插件逆序关闭：消息源 listener + client) → DB (`close_db`, stops aiosqlite's non-daemon worker thread) → `_cancel_pending_tasks`. |
| `briefdesk/__main__.py` | Enables `python -m briefdesk`. |
| `briefdesk/poll_cycle.py` | 轮询周期业务编排（应用层控制流，不属源包）。`run_poll_cycle(source: SourceRuntime)` (查启用会话 → `_compute_session_windows` 按会话算增量窗口 → `source.fetch_history(enabled_sessions, window_start_by_session=...)` → 结果 `contacts`/`sessions` 统一写库 → `process_all_batches` → 成功后按会话批量推进水位 `update_session_last_polls`（值为本轮 cycle 开始时刻），guarded by `_poll_lock`)，供 `/api/sync` 与首轮回填共用；周期内错误写入 status。窗口规则：会话水位 `sessions.last_poll_ts`（NULL=待回填 → 按 `BACKFILL_HOURS` 回填一次）与该会话最早未处理消息取 min，再减 `POLL_OVERLAP_SECONDS`；仅含未处理消息的会话被其最久远未处理消息钉住窗口，其余会话水位不受影响。 |
| `briefdesk/types.py` | Cross-module base types: `InternalMessage` dataclass (msg_id, content, sender_name, sender_id, session_id, group_name, timestamp, `source`（源标识，由 pipeline 入口按客户端 name 统一盖章）, `is_self`（是否本账号自己发送，normalize 阶段盖章、pipeline 按 `IGNORE_SELF` 过滤）, `image_urls`, `article_url`（文章卡片原文链接，独立于 content 供前端可点跳转）; content 构造时即经 `mask_content` 脱敏), `PollResult` (单源一次轮询结果，含源无关的 `sessions`/`contacts` 产出数据), `SessionInfo`（`is_group`/`is_official` 双维度：公众号为第三类会话，仅 weflow 产出）/`ContactInfo`（源产出、应用层写库的源无关描述）, and `ContextMsg` TypedDict. **管道跨插件契约**（`ClassifyResult`/`ClassifyOutcome`/`DedupResult`/`InsertedRow`/`BatchContext`）均定义在本模块；`CachedItem` 留在 `plugins/dedup/engine.py`（引擎内部）。 |
| `briefdesk/ai_ports.py` | AI 供应商端口：ai_provider 插件在 setup 阶段把实例注册到 `ctx.ai` 与本模块（`set_ai`），引擎（classify/dedup/merge）经端口函数 `chat`/`embed_texts`/`is_embedding_enabled`/`embed_model_name` 调用，核心不依赖具体供应商。未注册时 chat/embed 抛 RuntimeError（配置错误明示），`embed_model_name` 回退 config、`is_embedding_enabled` 安全返回 False（引擎/实验离线可用）。`loads_json`（JSON 修复解析）与 `top_k_similar`（余弦 Top-K）为供应商无关工具，亦收于本模块。模块级单例，测试用 `set_ai(None)` 复位。 |
| `briefdesk/plugins/ai_provider/engine.py` | OpenAI 兼容供应商实现：共享 `AsyncOpenAI` 客户端（`get_ai_client`）、`chat`（thinking 开关 `AI_DISABLE_THINKING`、**严格 JSON 输出**（`_use_json_object`：ollama api key 或 deepseek-v4-flash/pro 模型名命中时传 `response_format={"type":"json_object"}`，配合各任务对象根输出）、`AI_MAX_CONCURRENCY` 并发预算）与嵌入（`EMBED_API_BASE` 启用、按 `EMBED_BATCH_SIZE` 分批）。`Provider` 类显式实现 AIProvider 端口（薄封装委托）。`loads_json`/`top_k_similar` re-export（实验脚本兼容）。 |
| `briefdesk/plugins/ai_provider/plugin.py` | `AiProviderPlugin`（显式实现 Plugin + AIProvider）：setup 构造 `Provider` 并注册到 `ctx.ai` 与 `briefdesk.ai_ports`；teardown 清除注册（幂等）。classify/dedup/merge 依赖本插件（拓扑序保证其先就绪）。 |
| `briefdesk/plugins/classify/engine.py` | AI 分类引擎。分类类别由 DB `categories` 表驱动（用户可增删改/启用停用，见 db.py），`build_system_prompt` 按启用类别动态构建 system prompt，按群分组构建 prompt，解析统一外壳 `{"task":"classify","data":[...]}` 中的 data 数组 → `ClassifyResult`。`_parse_response` 对结构错误与未知类别（AI 幻觉，不在 allowed 集合）**同路径抛错**：整批不标记 processed、由下一轮回填重试（代价是整批含分类正常的消息随下轮重试，收益是零簿记）。无重试：`finish_reason=length` 截断仅记 WARNING。**多时间点**：prompt 要求含多个时间点的消息把全部时间点列入可选 `times` 数组（{type,time,label}），`_parse_extra_times` 逐项校验（脏项丢弃、与主字段 (type,time) 去重、上限 20 项、label 折叠截断 40 字）；key 中的时间必须写绝对日期（合并后相对时间失去锚点）。**无年份日期推断**：“X月X日/号”取与消息发送时刻**最接近**的那一次（无论已过还是未来）——任务清单里已过的截止日仍取当年（严禁滚动次年），明显更接近次年才取次年。 |
| `briefdesk/plugins/dedup/engine.py` | `DedupEngine`（显式实现 DedupService 服务端口）语义去重，判定管线：**image_urls 精确短路**（图片路径集合完全一致 → 直接判重、零 AI；同图重发是确定性证据，分类标题措辞变体不影响；集合相等而非子集，防多图卡片共享装饰图误判；**源限定 `_IMAGE_SHORTCUT_SOURCES`（当前仅 weflow**：该源图片消息无混合文本、同图必同文；qqflow 实测存在图片+文字混合消息、同图可配不同文字，不得短路——查询与缓存条目双方同属限定源才命中）→ **原文哈希精确短路**（`content_hash` = sha256(source_quote)[:16]，**仅对原文取哈希**：原文非空且非纯占位符时哈希全等 → 直接判重、零 AI，同时覆盖原文逐字节等价与标题+描述哈希等价两类精确判定）→ 候选选取（嵌入余弦 Top-K 以 `DEDUP_EMBED_FALLBACK_THRESHOLD` 召回，或余弦零候选时字符重叠单候选兜底，阈值 `config.dedup_similarity_threshold`）→ 门禁分级（normal ≥ `DEDUP_EMBED_THRESHOLD` 走 strong 短路/加权多数票；weak 区间 [fallback, threshold) 仅当无 normal 候选时参与，全员判 SAME 才判重）→ 同文本短路（≥ `DEDUP_STRONG_THRESHOLD` 候选 AI 判 SAME 即直接判重、判 DIFFERENT 只剔除该候选）→ 其余候选**并行**送 LLM、**加权多数票**（票权 = 候选相似度，SAME 权重和 > 总权重一半才命中；等权时等价原 >K/2 规则，单候选退化为一次判定）；AI 输出裸对象 `{"same": true\|false}`（解析器兼容旧版 `{"task":"dedup","data":{"same":...}}` 外壳）。无候选路径打 WARNING 诊断（含 cosine/overlap top-1 差距）。命中时合并 `source_group`（逗号分隔、精确匹配去重，群名互为子串不误判）。`add_to_cache` 同步内存追加（嵌入由调用方在锁外 `preembed_batch` 预计算后传入，原文随条传入并按原文重算 content_hash）；向量落库由锁外 `flush_pending_embeddings` 批量完成。模块级单例 `dedup_engine` 暴露 `check_dedup`/`add_to_cache`（实验脚本兼容）。 |
| `briefdesk/plugins/merge/engine.py` | 会话内同话题片段合并判官（与去重互补的第二个 AI 判定）：同一会话里一个话题常由前后多条消息拼成（物品名/价格/运费各一句），逐条分类会各成一张卡。`judge_merge` 判断两张卡是否同话题片段（应合并为一张卡），输出裸对象 `{"merge": true\|false}`，失败保守返回 False（不合并）；`summarize_title` 在合并后依据「原标题+关键信息+原文引用」重拟概括性标题（输出裸对象 `{"title":"..."}`，失败/超长回退原标题）；两者解析器均兼容旧版 `{"task":"merge"\|"title","data":{...}}` 外壳。 |
| `briefdesk/plugins/ocr/engine.py` | RapidOCR（基于 ONNX Runtime，CPU 推理）图片文字识别。只接收图片字节（`ocr_image_bytes`/`ocr_images_bytes`），不接触 URL/HTTP/鉴权；下载归消息源客户端（`SourceClient.download_media`）。无文字图片（rapidocr 抛 `RapidOCRError`）视为“未识别到文字”返回空串，不向调用方抛错；引擎故障等其余异常仍向上抛（pipeline 侧对 OCR 调用有 try 兜底，单条失败不拖垮批次）。OCR 文本经 `mask_content` 脱敏后以 `[OCR]` 前缀**替换** content。懒加载单例引擎。**依赖可选**：rapidocr/onnxruntime 为 `ocr` extra（`pip install briefdesk[ocr]`），未安装时本模块不可导入、OCR 插件自禁用。 |
| `briefdesk/realtime.py` | 进程内发布/订阅：`publish_items_updated()`（列表刷新）与 `publish_sync_progress()`（同步进度事件）把事件推给所有订阅队列（队列项为 `(事件名, data JSON)` 二元组），由 server 的 `/api/stream` SSE 按事件名转发给前端。 |
| `briefdesk/status.py` | 应用运行时状态 + 消息源注册表：`set_status`/`get_status_info`/`is_syncing`、`register_source_client`/`get_source_client`、`set_listener`/`get_listener`。pipeline/poll_cycle 与 server 都只依赖本模块（不互相依赖），避免业务层反向依赖 HTTP 层。**相对时间展示在前端**：卡片行 `relativeTime` 与状态面板 `relativeSync` 均由前端按 `msg_time`/`lastSync` 自行计算（`relativeTimeStr`/`itemRelativeTime`/`syncRelativeText`），本模块只下发原始时间数据。**同步进度（新增消息数）**：`SyncProgress` 快照（startedAt/newCount/pendingCount/processedCount/done）+ `note_sync_batch_start`/`note_sync_batch_done` 由 pipeline 入口/出口调用（单事件循环内同步原子，无需加锁），经 `get_status_info().syncProgress` 与 `sync_progress` SSE 事件下发，突发边界按 pending 归零划分。 |
| `briefdesk/sync.py` | 同步服务：`set_sync_callback` + `trigger_sync()`（fire-and-forget 全源轮询任务，syncing 互斥，结束后经 realtime 广播 `synced`）。main 启动与 `/api/sync` 共用。 |
| `briefdesk/config.py` | `pydantic-settings` from `.env`。含 `plugins`（`PLUGINS`，默认 `["*"]`，JSON 数组，**消息源启用的唯一开关**，weflow/qqflow 为内置插件）、`plugins_disabled`/`plugins_required`/`plugin_path`、`db_path`（默认 `briefdesk.sqlite`）、`server_port`（3000）、`backfill_hours`（24）、`log_level`（`LOG_LEVEL`，默认 `"INFO"`，logger.py 读取）、`realtime_batch_max_count`/`realtime_batch_timeout_ms`（实时批缓冲，跨源公共）、`backfill_batch_max_count`（回填切批）、AI 模型等。weflow/qqflow 专属配置（`WEFLOW_*`/`QQFLOW_*`）在各插件包的 `config.py`，不占 app 级配置。 |
| `briefdesk/plugins/weflow/normalize.py` | Two normalization paths: `normalize_sse` and `normalize_rest` both produce **lists** of `InternalMessage`. Pre-filters drop revocations, system messages, short/empty content, and attachment-only messages — but **image messages are kept** (`[图片]` passes SSE filter, `localType=3` + mediaUrl passes REST filter) for OCR. SSE images require REST lookback for `mediaUrl`. **文章卡片**（`localType=0x500000031`，公众号推送与群聊转发同格式的 `<msg><appmsg>` XML）：`parse_appmsg_xml` 提取 `mmreader/category/item[]` 的 title（缺省回退 title_v2）/summary/url（无 item 时退化解析外层 appmsg），`_article_messages` 按篇拆条（msg_id=`{serverId}_1..{serverId}_n` 文档序 1 起），content=「标题：…\n摘要：…」，原文链接存 `article_url`（不进 content）；REST 解析失败返回空列表（维持丢弃语义），SSE 解析失败按原文单条放行。 |
| `briefdesk/pipeline.py` | `process_all_batches()` — 管道**骨架**（不 import 任何 AI/OCR 实现）：入口对每条消息统一盖章 `msg.source = client.name`（源身份单一权威点）→ **统一过滤（自己发送（`IGNORE_SELF`，`msg.is_self` 由各源 normalize 盖章）→ 纯占位符图片（enrich 槽位为空即 OCR 未启用时，`image_urls` 非空且 content 为单个附件占位符如 `[图片]`/`[image]` 的消息直接屏蔽，不标记 processed、重新启用 OCR 后回填窗口内自动重拉；图片+文字混合消息不受影响）→ 启用会话 → 已处理，替代源内查重）→ raw_messages 批量落库（单事务）** → 切批 → 并行跑 enrich + classify 槽位（`asyncio.create_task` + `as_completed`，锁外）→ 串行（`_storage_lock` 内）：dedup 槽位（判重/入库/缓存）→ 跳过标记（未选中且非失败的消息标记 processed，含“无分类结果全批标记”路径）→ post_insert 槽位（会话内同话题合并）→ 锁外 dedup after_run（向量落库）→ 计数/状态/实时通知。零产出（全部失败）不刷新 lastSync。被滤自消息不标记 processed（回填窗口内每轮重滤、关闭 IGNORE_SELF 可恢复）。阶段实现见 `briefdesk/plugins/{ocr,classify,dedup,merge}/`。 |
| `briefdesk/db.py` | All SQLite via `aiosqlite`. **双连接 + WAL mode + foreign keys**：主连接（`get_db` 单例）+ 向量持久化专用连接（`get_embed_db`，`load_embeddings`/`upsert_embeddings` 走它，`_embed_lock` 语句级串行、游标先 close 再 commit、锁竞争指数退避重试 3 次——杜绝活动语句阻断 COMMIT 的 `cannot commit transaction - SQL statements in progress`）。DB path from `config.db_path`. Schema: `items`, `sessions`, `processed_messages`, `raw_messages`, `contacts`, `item_embeddings`, `categories`. 多源命名空间：sessions/contacts/processed_messages/raw_messages 以 `(source, id)` 复合主键，items 以 `UNIQUE(source, source_msg_id)` 区分；所有相关函数签名首参为 `source`。schema 单一来源为 `init_schema` 的 CREATE TABLE；启动时若检测到已有数据库（存在任意应用表），会通过 `validate_schema` 严格校验表/列/类型，不匹配则记录 CRITICAL 并 FATAL 退出，不做自动迁移；`sessions.last_poll_ts` 为按会话增量轮询水位，NULL=待回填；`toggle_session` 启用会话时清空。`get_items_page` 用一条共享 CTE 语句，在同一套类别/状态/搜索/来源群/时间范围/截止状态/启用类别与会话条件及同一 SQLite 快照下返回稳定分页、完整条数/组数、来源群选项与下一偏移（日历区间查询 `get_calendar_items` 随 calendar 插件分发，见 `plugins/calendar/db.py`）。TypedDict row shapes (`ItemRow`, `ItemInput`, etc.). **`categories`**：用户自定义分类类别（name UNIQUE/prompt/color/enabled），空表时启动播种 `DEFAULT_CATEGORIES` 五类；改名时同事务同步 `items.category`；`delete_category(purge_items=True)` 级联删 items/raw_messages/item_embeddings（保留 processed_messages），调用方需同步清 dedup 内存缓存。color 由 `/api/items` 聚合携带，前端侧边栏/卡片/色板数据驱动渲染；类别图标不入库，由前端按 color 从预设色板（`_CAT_PALETTE`，颜色+图标组合）映射派生。**游标纪律**：新增查询一律经 `_fetchone`/`_fetchall`/`_cursor` 三个助手（db.py 内定义，游标 try/finally 自动 close）；流式迭代（async for）用 `_cursor` 作用域，需 rowcount/lastrowid 的 DML 同样用 `_cursor`（须在 with 块内读取），executemany 后必须 `await cursor.close()`——未终结语句会残留连接并可能阻断后续 COMMIT。`close_db` 同时关闭主连接与向量连接（aiosqlite worker 线程非 daemon，漏关解释器退出挂死）。 |
| `briefdesk/server/` | FastAPI HTTP 服务子包（按职责分组的模块）：`app.py`（FastAPI 实例）、`middleware.py`（Host 白名单 + 同源校验 + CSP 头）、`web_plugins.py`（Web 插件注入点）、`routes_items.py`（核心数据路由）、`routes_categories.py`（类别管理）、`media.py`（媒体代理）、`static.py`（SPA 托管）、`callbacks.py`（会话刷新回调）。**组装顺序有语义**：`__init__.py` 按 中间件 → 插件路由 → 核心路由 → 类别路由 → 媒体代理 → SPA mount 依次导入（web_plugins 必须先于 static，否则被 SPA 兜底截胡；`# ruff: noqa: I001` 豁免排序）。Routes: `GET /api/items`, `POST /api/items/:id/verify`, `POST /api/items/:id/recategorize`（手动修正分类，仅允许改到启用类别）, `POST /api/items/batch`（批量 memo/ignore/unverify/delete；delete 在存储锁内删库并发布 `EVENT_ITEMS_DELETED` 清去重内存缓存）, `GET /api/export/items`（CSV 导出，筛选参数与 `/api/items` 一致）, `GET /api/export/recat-samples`（导出人工改类样本 jsonl/csv，内容已脱敏）, `GET /api/backup`（SQLite 在线备份下载，WAL 安全可运行中执行）/ `POST /api/restore`（上传校验后暂存 `{db_path}.restore-pending`，重启应用生效）, `GET /api/sessions`, `POST /api/sessions/:source/:session_id/toggle`, `POST /api/sessions/refresh`, `POST /api/sync`, `GET /api/context`（`source`+`session_id` 查询参数）, `GET /api/status`, `GET /api/stream` (SSE push channel), `GET /api/media/:source/:path` (媒体代理，经对应源的 `SourceClient.download_media` 转发；`MediaError` → 404), `GET /api/subject/items`（主体时间线，核心视图保留）, 类别管理：`GET /api/categories`、`POST /api/categories`、`POST /api/categories/:id/update`、`POST /api/categories/:id/toggle`、`POST /api/categories/:id/delete`（body `purgeItems` 控制级联删除，级联后发布 `EVENT_ITEMS_DELETED` 清 dedup 内存缓存）。**Web 插件注入点**：`GET /api/plugins`（插件装配摘要，前端设置区与入口可见性据此渲染；每项含 `has_frontend`——插件是否声明前端资源，前端加载器据此只对带前端的插件注入 `ui.css`/`ui.js`，避免无资源插件的 404 触发浏览器严格 MIME 检查告警）、`GET /plugin-assets/{name}/{path}`（插件静态资源，目录穿越/非法路径一律 404，且 404 返回纯文本而非 JSON——本端点服务 `<link>`/`<script>` 资源请求）、`register_plugin_assets`/`set_plugins_info_callback`/`include_plugin_router`（展开 APIRoute 插到 SPA mount 之前——新版 Starlette 的惰性 `_IncludedRouter` 会被 SPA 兜底截胡）。日历/提醒路由位于 `plugins/{calendar,reminders}`。`/api/items` 接受 `sourceGroup`/`minMsgTime`/`hideExpired`/`filterNow`，并返回与完整过滤条件一致的 `totalCount`/`groupCount`/`sourceGroups`/`hasMore`/`nextOffset`/`filterNow`；启用隐藏截止时，客户端在同一分页链复用服务端首次返回的 `filterNow` 以固定时间边界。侧边栏计数仍为全局口径。Serves `ui/` with SPA fallback. 仅保留 `set_refresh_sessions_callback` 注入点；状态查询走 `briefdesk.status.get_status_info`，同步走 `briefdesk.sync.trigger_sync`。 |
| `briefdesk/plugins/calendar/plugin.py` + `router.py` + `db.py` | `CalendarPlugin`（显式实现 WebPlugin）：`/api/calendar` 日历视图路由（区间带开始/截止卡片，排除已忽略）；**数据访问随插件分发**——`db.py` 的 `get_calendar_items`（start/end/extra_times 任一命中区间，`_extra_times_in_range` JSON 过滤）属日历专属查询，不在核心 `briefdesk/db.py`；`asset_dir()` 返回插件包内 `ui/`（**日历完整前端**：`ui/ui.js` 自建按钮/视图容器/浮层并注册核心视图钩子、`ui/ui.css` 日历样式，经 `/plugin-assets/calendar/` 由核心加载器注入，核心 `ui/` 无任何日历前端残留——由 tests/test_web_plugins.py 的核心前端边界守卫测试覆盖）；卡片行相对时间由前端计算（后端不再附加）。 |
| `briefdesk/plugins/reminders/plugin.py` + `router.py` | `RemindersPlugin`（显式实现 WebPlugin）：`POST /api/items/:id/reminder`（设置/清除卡片提醒，aware→本地墙钟换算、参数校验）与 `GET /api/reminders/due`（到期提醒轮询）；`asset_dir()` 返回插件包内 `ui/`（**提醒完整前端**：`ui/ui.js` 自建卡片「提醒」按钮/菜单、设置弹窗「通知」面板自动提醒控件与到期轮询定时器，经核心 `registerItemRowExtension` 行内扩展钩子接入 `renderItemRow`/`renderCard` 动作区与 `handleRowAction`，`ui/ui.css` 提醒菜单样式，经 `/plugin-assets/reminders/` 由核心加载器注入；核心 `ui/` 无任何提醒前端残留——由 tests/test_web_plugins.py 的核心前端边界守卫测试覆盖）。 |
| `briefdesk/sources_base.py` | 消息源抽象（核心契约模块，无 sources 包）：`SourceClient` Protocol（`name`/`connection_status`/`download_media`/`close` 客户端能力契约）——pipeline 与 server 只依赖该协议，新消息源实现它即可被消费；`RealtimeListener[S]`（`start`/`stop`/`invalidate_session_cache` 监听器生命周期契约，泛型参数绑定监听器所服务的客户端类型）——server 只依赖它；`SourceRuntime`（`client`/`listener`/`fetch_history(enabled_sessions, is_processed)`/`refresh_sessions() -> list[SessionInfo]`/`start`/`close` 已装配源单元，**源只产出源无关数据、不触碰 DB**，`is_processed` 为应用层注入的已处理查询端口 `ProcessedQuery`）——main 依赖它编排启动/关闭，**新增源 = 实现 `SourceRuntime` 并以插件发布（`briefdesk/plugins/*`，entry point 组 `briefdesk.plugins`，启用走 `PLUGINS`）**。通用类型 `ConnectionStatus`、`BatchHandler`、`ProcessedQuery`、异常 `SourceError`/`MediaError`（`download_media` 失败统一抛 `MediaError`，server 据此映射 404）；**`with_connect_retry`**（连接类失败短退避重试：捕获 `httpx.ConnectError`/`ConnectTimeout`，0.5s/1s/2s 共 3 次，耗尽原样上抛；不重试 HTTP 状态错误与 503 门控、不用于 SSE 流（监听器已有退避重连））。轮询拉取/实时监听等源内控制流留在各插件包内，跨源编排在 `poll_cycle.py`。 |
| `briefdesk/plugins/weflow/plugin.py` | `WeFlowPlugin`（显式实现 SourcePlugin）：setup 构造 `WeFlowSource` 并经 `ctx.register_source` 注册；activate 无副作用（监听启动由应用层编排）；teardown 关闭 runtime。无必填配置校验（缺 WEFLOW_API_TOKEN 时上游调用期报错）。模块底部暴露 `plugin` 实例供 entry point 引用。 |
| `briefdesk/plugins/qqflow/plugin.py` | `QqFlowPlugin`（显式实现 SourcePlugin）：setup 校验 `QQFLOW_API_TOKEN`/`QQFLOW_QQ`/`QQFLOW_KEY` 必填配置，缺失抛 `PluginDisabledError` 自禁用；齐备则构造 `QqFlowSource` 并经 `ctx.register_source` 注册。teardown 关闭 runtime。 |
| `briefdesk/plugins/weflow/runtime.py` | `WeFlowSource`（实现 `SourceRuntime`）— weflow 源装配门面：构造 `WeFlowClient`（参数缺省时读 `WeFlowSettings` 的 `WEFLOW_*`）、`fetch_history`（= `poller.poll`）、`refresh_sessions`（拉会话返回 `list[SessionInfo]`，**不写库**，由应用层 main 的 `_refresh_all` 统一落库 + 失效监听器缓存）、`start(on_batch)`（建 `WeFlowSseClient` 并启动）、`close()`（停监听 + 关客户端）。main 经 PluginManager（WeFlowPlugin）接入，不接触 weflow 具体类型。 |
| `briefdesk/plugins/qqflow/` | qqflow 消息源（实现 `SourceRuntime`，接入 qqflow-server 默认 :5032）。与 weflow 同构六文件分层（config/client/sse/poller/normalize/runtime），差异源于 qqflow-server API：**媒体**（图片消息经 `mediaId` + `GET /api/v1/media/{id}` 获取字节做 OCR 与前端展示；`mediaId` 仅在上游注册可读取的本地缓存时提供——REST 与 SSE 同一规则、同一承诺（出现即保证可取）；SSE 事件直接携带 `mediaId`，`media` 对象为无路径元数据视图（上游推送不下发 `localPath`）；语音/视频无下游消费方仍整体过滤）、**引导注册**（`ensure_ready`：/health 无 ready 账号时自动 `POST /api/v1/accounts`，配置 `QQFLOW_QQ`/`QQFLOW_KEY`/`QQFLOW_DB_PATH`）、**503 就绪门控**（索引期视为瞬态，`QqFlowNotReadyError` 静默跳过不污染 lastError）、msg_id 统一用 rowid（SSE `rawid` = REST `localId`）、监听器按 `event+rawid` 小去重缓存。**IGNORE_SELF 自消息过滤**：REST 按 `senderUsername == self_uid`（`u_<QQFLOW_QQ>`）在 poller 预滤并独立计数（`X 自己`）；SSE 事件无发送者标识，开启后按 `(sessionId, rawid, timestamp)` 回查 REST（`client.lookup_message`）判定，命中在监听器层丢弃并计入 SSE 统计。`QQFLOW_API_TOKEN`/`QQFLOW_QQ`/`QQFLOW_KEY` 必填，缺失任一 → QqFlowPlugin.setup 抛 PluginDisabledError 自禁用。**REST 连接类失败短退避重试**：`_get`/`fetch_health`/`register_account` 经 `with_connect_retry` 包裹（连接拒绝/超时自动重试 3 次，耗尽原样上抛；503 门控语义不变）。 |
| `briefdesk/plugins/weflow/client.py` | `WeFlowClient` class（实现 `SourceClient`）— 封装所有 WeFlow HTTP 通信 + API 数据类型（`WeFlowEvent`, `WeFlowMessage`, `ChatLabSession`, `WeFlowContact`）。含 `stream_events()`（SSE 异步迭代器）、`fetch_sessions()`（**chatlab 格式 + 大 limit**：JSON 格式的 `type` 实测恒为 0 不可靠、默认 limit=100 截断会话发现）、`session_kind()`/`is_group_session`/`is_private_session`/`is_official_session`（channel→公众号）、`fetch_message_media()`（SSE rawid 回查 REST 获取 mediaUrl）、`download_media()`（带鉴权下载媒体文件字节，供 OCR）、`connection_status`、`close()`。**REST 连接类失败短退避重试**：`_get`（含 `retry_on_empty` 二次请求）经 `with_connect_retry` 包裹（连接拒绝/超时自动重试 3 次，耗尽原样上抛）。 |
| `briefdesk/plugins/weflow/sse.py` | SSE 实时监听，指数退避 + 抖动自动重连（退避参数由 `WeFlowSettings` 构造注入）。`BatchBuffer` 按数量/超时刷新。**不触碰 DB**：启用会话过滤、已处理过滤与 raw 落库由 pipeline 入口统一完成；`invalidate_session_cache()` 为 no-op（保留以兼容调用）。 |
| `briefdesk/plugins/weflow/poller.py` | REST 历史回填（**按会话窗口**：`window_start_by_session` 提供各会话增量下界（会话水位-overlap）；缺省/无水位会话回退 `BACKFILL_HOURS`（默认 24h，启用即回填一次）；**-1 = 拉取全部历史**：不传 start、无年龄截止、offset 翻页至 hasMore=False，单会话守卫上限 2000 页，并打 WARNING；所有模式统一翻页，无单页硬顶）。`poll(client, enabled_sessions, is_processed, window_start_by_session=...)` 返回 `PollResult`（messages / sessions / contacts / session_count），**不触碰 DB**：联系人/会话/消息只产出数据，写库由 `poll_cycle` 统一完成；只轮询传入的已启用会话，`is_processed` 为应用层注入的已处理查询端口；每会话必打 INFO 汇总行（含 0 条，标注窗口=增量/回填/全量）。会话类型来自 chatlab 格式（`id`/`name`/`type`，channel→公众号）。**IGNORE_SELF 预滤**：`isSend==1` 的消息在候选循环直接丢弃并独立计数（`X 自己`），不标记 processed；同时检测整轮是否含 isSend 字段，缺失打 WARNING（该 WeFlow 版本过滤未生效）。**文章占位符回查**：回填固定 `media=True`（图片 OCR），而 WeFlow 在 media=True 时把文章卡片 XML 渲染成占位符（如 `[视频号] 标题`）——poller 对 localType=文章卡片且 content 非 XML 的候选调 `client.fetch_message_raw()`（media=False 回查原始 XML）后再拆条解析。 |
| `briefdesk/logger.py` | 标准 logging 配置。`setup_logging()` 安装彩色 `_BriefFormatter`（格式：`时间戳 + LEVEL: + 模块名: message`，级别名补齐对齐），日志级别由 `config.log_level`（`LOG_LEVEL`，默认 INFO，DEBUG 开逐条细节）驱动；uvicorn/FastAPI 日志经 `uvicorn.Config(log_config=None)` + uvicorn 系 logger 清 handler、`propagate=True` 统一走根 handler，访问日志在 `formatMessage` 中还原 HTTP 状态短语（如 `200 OK`）并着色；降低 httpx/httpcore/openai/aiosqlite/PIL（Pillow 解码字节流噪音，压到 WARNING）的日志噪音；`fmt_dur()` 统一耗时格式。 |
| `briefdesk/events.py` | 内部事件总线（topic pub/sub）：核心与插件间的通用解耦通道（realtime 只管前端 SSE）。同步/异步处理器均支持，处理器异常只记日志不向发布方传播。模块级单例 `event_bus` 由 main 注入 PluginContext。核心删除卡片后发布 `EVENT_ITEMS_DELETED`（"items_deleted"），去重插件订阅后同步清理内存缓存。 |
| `briefdesk/stages.py` | 管道阶段注册表与装配期上下文：StagePlugin 经 `ctx.register_stage` 注册（main 把端口接到本模块），pipeline 骨架按槽位（enrich → classify → dedup → post_insert）读取，同槽按 priority 升序；`set_context` 注入装配期 PluginContext（阶段 run(batch, ctx) 经此获得 `ctx.dedup` 等服务端口）。模块级单例，测试用 `reset()` 隔离。 |
| `briefdesk/plugins/ocr/plugin.py` | `OcrPlugin`（显式实现 StagePlugin，slot=enrich）：setup 延迟导入引擎——rapidocr/onnxruntime（可选依赖）缺失时抛 `PluginDisabledError` 自禁用（非致命，与 qqflow 缺配置自禁用同语义），图片消息仍以原文入库；run 下载图片字节（`batch.client.download_media`）→ 引擎识别 → 脱敏后以 `[OCR]` 替换 content；单条失败（MediaError/引擎异常）只跳过该条、不拖垮整批。 |
| `briefdesk/plugins/classify/plugin.py` | `ClassifyPlugin`（显式实现 StagePlugin，slot=classify）：run 调引擎 `classify_batch` 并把 `ClassifyOutcome` 写入 `batch.outcomes`。 |
| `briefdesk/plugins/dedup/plugin.py` | `DedupPlugin`（显式实现 StagePlugin，slot=dedup）：setup 构造 `DedupEngine` + **预热去重缓存**（HTTP 服务启动前、源监听启动前），注册为 `ctx.dedup` 服务端口并订阅 `EVENT_ITEMS_DELETED`（同步处理器，发布方持锁时保持原子）；`before_run`（锁外）行规划 + 批内预嵌入，`run`（锁内）判重/入库/缓存，`after_run`（锁外）向量落库。 |
| `briefdesk/plugins/merge/plugin.py` | `MergePlugin`（显式实现 StagePlugin，slot=post_insert，依赖 dedup）：run（锁内）把 `batch.inserted` 新卡与同会话近期未核实卡经 AI 判官合并（折入最早头卡、多时间点集合化、重拟标题、保留片段 raw 行、已设提醒卡不参与）；去重缓存同步走 `ctx.dedup` 服务端口。合并纯函数（`_merge_quote`/`_merge_key_info`/`_merge_time_points` 等）在 `plugins/merge/engine.py`。 |
| `briefdesk/plugin/__init__.py` | 插件框架包（核心侧）：导出协议与 PluginManager。实现层在 `briefdesk/plugins/`。 |
| `briefdesk/plugin/base.py` | 插件最小契约：`Plugin` Protocol（name/version/dependencies + setup/activate/teardown 生命周期）、`PluginContext`（核心注入给插件的服务端口集合，见下「插件框架」）、异常 `PluginError`（致命）/`PluginDisabledError`（自禁用，非致命）。内置插件类显式继承对应能力协议（mypy 强制实现完整性；第三方插件亦可鸭子实现，manager 只做结构校验）。详见下方「插件框架」。 |
| `briefdesk/plugin/manager.py` | `PluginManager`：发现/过滤/拓扑排序/生命周期编排（setup → activate → teardown）/失败隔离/`infos()`。详见下方「插件框架」。 |

## 插件框架

**依赖方向单向**：核心与 `briefdesk/plugin/*` 永不静态 import `briefdesk.plugins.*`，由 `tests/test_no_core_imports_plugins.py` 的 AST 守卫强制执行；插件实现层可自由依赖核心与 `briefdesk/plugin/*`。

- **发现**：打包插件经 pyproject entry point 组 `briefdesk.plugins` 声明；开发期插件放 `PLUGIN_PATH` 目录（每个 `*.py` 暴露 `plugin` 实例即被加载，免打包）。
- **过滤**：`PLUGINS` / `PLUGINS_DISABLED`（后者最高优先）；另有**默认禁用**层——声明 `default_disabled = True` 的插件（如实验性 benchmark）仅在被 `PLUGINS` 显式列名时启用，`"*"` 通配不包含，被排除时标 disabled + 原因供 `/api/plugins` 展示。
- **排序**：依赖拓扑排序（未知依赖/依赖环降级 disabled）。
- **生命周期**：`setup_all`（HTTP 启动前、DB 就绪后）→ `activate_all`（服务器就绪后）→ `teardown_all`（逆序幂等）。消息源预热（去重缓存等）与监听启动等顺序约束由该阶段划分承载。
- **失败隔离**：单插件失败只降级 disabled/failed 并记日志；`PLUGINS_REQUIRED` 名单内的失败抛 `PluginError` 致命中止启动。
- **能力协议**：`SourcePlugin`（消息源，经 `ctx.register_source` 注册 `SourceRuntime`；零源启动报错）/ `StagePlugin`（管道槽位 enrich → classify → dedup → post_insert，注册表 `briefdesk/stages.py`，骨架 `briefdesk/pipeline.py` 只做编排）/ `DedupService`（`ctx.dedup` 服务端口，供 merge 阶段同步缓存）/ `AIProvider`（`briefdesk/ai_ports.py` 端口；classify/dedup/merge 声明依赖 ai_provider，被禁用时随依赖降级，pipeline 骨架对“阶段缺失”整批保留不标记防消息丢失）/ `WebPlugin`（router + asset_dir；核心提供 `GET /api/plugins` 元数据与 `GET /plugin-assets/{name}/{path}` 静态资源，`include_plugin_router` 展开 APIRoute 插到 SPA mount 之前——新版 Starlette 的惰性 `_IncludedRouter` 会被 SPA 兜底截胡）。
- **PluginContext 服务端口**：config、事件总线（`event_bus`，核心删除卡片发布 `EVENT_ITEMS_DELETED`，去重插件订阅清内存缓存）、`register_source`、`register_stage`、`dedup`、`ai`、`register_router`/`register_plugin_assets`（默认 noop，未注入时静默丢弃）。
- **插件前端随插件包分发**：约定 `ui/ui.js` 以 IIFE 暴露 `window.briefdeskPlugins.<name>.init(api)`，DOM/样式/交互全在插件包内；核心只留通用加载器与两类扩展钩子（详见下方「前端」节）。

## 数据库

SQLite file: `briefdesk.sqlite` (configurable via `DB_PATH` in `.env`). Key tables:
- **`items`**: Classified/deduped information cards with category, title, structured fields, source tracking (`source` + `UNIQUE(source, source_msg_id)`), verification status (`is_verified`: 0 unverified / 1 memo / -1 ignored), `image_urls` (JSON), `article_url`（文章卡片原文链接，前端渲染可点跳转）, `content_hash`, `extra_times`（多时间点 JSON：每项 {"type":"start"|"end", "time":"YYYY-MM-DD[ HH:MM]", "label":"任务名"}——单条消息含多个截止/开始时间时，主字段取最早、其余全部结构化存此列，供卡片徽章与日历逐点渲染）
- **`sessions`**: Discovered conversations, composite PK `(source, session_id)`, each with an `enabled` flag; `is_group`/`is_official` 双维度区分群聊/私聊/公众号（公众号仅 weflow 产出，chatlab 格式的 channel 类型映射而来）；`last_poll_ts` 为按会话增量轮询水位（NULL=待回填，启用会话时由 toggle 清空 → 下轮按 BACKFILL_HOURS 回填一次）
- **`processed_messages`**: Tracks processed message IDs to avoid re-processing; composite PK `(source, msg_id)`
- **`raw_messages`**: Original message content for context lookups (joined with `contacts` for display names); composite PK `(source, msg_id)`; `article_url` 列存文章卡片链接供 `/api/context` 返回；与 processed_messages 的反连接（未处理消息按会话分组）驱动增量轮询的按会话重试钉窗
- **`contacts`**: Display name mappings for sender identifiers; composite PK `(source, sender_id)`
- **`item_embeddings`**: Item embedding vectors (`item_id` PK, `model`, JSON-encoded `embedding`, `created_at`); model change overwrites rows per `item_id` (re-embed on next load)
- **`categories`**: User-defined classification categories (`id` PK autoincrement, `name` UNIQUE, `prompt` (per-category AI hint), `color`, `enabled`); seeded with the 5 defaults when empty; managed via `/api/categories` endpoints and the settings modal. Icons are not stored — the frontend derives them from the preset color palette

## 前端

Vanilla JS SPA in `ui/` (`index.html`, `app.js`, `style.css`, `icons/`). No build step, no framework. Key behaviors:
- Category sidebar with counts + 备忘录 (memo) / 已忽略 (ignored) views (from `/api/items`)
- Item cards with three-state verification: 加入备忘录 (1) / 忽略 (-1) / 未处理 (0)
- Expandable quote section (fetches context via `/api/context`)
- Settings modal: sync button (`/api/sync`), session enable/disable with select-all; 群聊列表支持类型筛选（全部/群聊/私聊/公众号，多选）与消息源筛选（多选，芯片按 `/api/status` 的 `sources` 实际启用源动态渲染，单源部署整行隐藏），两者与名称搜索叠加生效（仅显示层，不影响保存 diff）；行标签「群/私/公」按 is_group/is_official 渲染
- **插件前端随插件包分发**：设置弹窗有「插件」分组（`/api/plugins` 渲染名称/版本/状态/原因）；核心前端只留通用加载器 `loadPluginFrontends`——读取 `/api/plugins` 取 loaded 名单 → 隐藏所有 `[data-plugin-entry="<name>"]` 声明入口 → **仅对声明了前端资源的插件**（`has_frontend`，`asset_dir()` 非 None）注入 `/plugin-assets/<name>/ui.css`（样式）与 `/plugin-assets/<name>/ui.js`（脚本；无前端资源的插件不请求，避免 404 触发浏览器严格 MIME 检查告警）→ 调用 `window.briefdeskPlugins.<name>.init({isLoaded})`（异常仅 console.warn）。**插件的完整前端（DOM/样式/交互/入口）随插件包分发**：calendar 的按钮、视图容器、两个浮层全部由其 `ui/ui.js` 自建，核心 `ui/` 不写死任何插件功能入口（由 tests/test_web_plugins.py 的核心前端边界守卫测试覆盖）；核心提供两类插件扩展钩子——**视图钩子** `registerPluginView`（hash 路由 / fetchData 委派 / Esc 消费 / 侧边栏数据就绪通知，calendar 视图 `#calendar` 据此接入）与**行内扩展** `registerItemRowExtension`（renderItemRow/renderCard 动作区按钮与行末菜单渲染、handleRowAction 委派、文档点击关闭菜单、verifyItem 成功后 onVerify 通知——reminders 的提醒按钮/菜单/自动提醒据此接入），另设 `data-plugin-slot` 设置面板注入点（reminders 注入「自动提醒」控件）
- Article cards: 卡片与浮层的「原文引用」内容末尾、上下文引用的文本行尾渲染可点原文链接（`article_url`，http/https 校验后新窗口打开）
- 多时间点卡片：`parseExtraTimes` 解析 `extra_times`，`timeBadgeHtml` 在主徽章后逐点渲染补充徽章（「截止 8月15日·部门宣传视频」）；卡片头部 `flex-wrap` 换行防多徽章被裁剪；**部分截止状态**：主截止已过但仍有未过期时间点（`allTimePoints`/`nextUpcomingTime` 判定）→ `timeBadgeInfo` 返回「部分截止」（`partial` 类，琥珀色、`expired=false`，不置灰、不被「隐藏已截止」过滤），提醒菜单默认值与自动提醒基准也改用下一个未过期时间点；日历 `calDaySet` 把主时间与 extra_times 的每个日期都计入格子与当日浮层（同卡多日出现），`dayTimeEntry`/`timeExpired` 让每个格子按**该日对应的时间点**渲染时间标签与过期样式（而非整卡主字段）
- 主列表固定每页 100 张卡片；类别/状态/搜索/来源群/时间范围/“隐藏已截止”均由 `/api/items` 统一过滤，头部“共 n 条/组”使用完整查询总数，“加载更多”沿服务端 `nextOffset` 追加下一页并复用当前 `filterNow`。定时刷新与 SSE 刷新会从第一页重拉当前已加载页数，保留用户的分页深度；卡片状态操作后也按该深度刷新，使卡片与条/组数收敛；侧边栏计数不随主列表局部筛选变化
- Real-time updates via `EventSource("/api/stream")` SSE channel (plus poll-based auto-refresh, settings persisted to `localStorage`)
- **图标库**：使用 [Lucide](https://lucide.dev)（ISC 许可）的 vendored 子集，位于 `ui/icons/`（英文 kebab-case 扁平命名，经 `/icons/<name>.svg` 引用；旧中文图标库 `ui/图标/` 已整体移除，历史版本在 git 中可回溯）。单一事实来源为 `ui/icon-manifest.txt`，`tests/test_icon_manifest.py` 双向守卫：代码引用 ⊆ 清单且文件存在、`ui/icons/` 文件集合 == 清单集合、旧路径 `/图标/` 不得回流。**新增图标流程**：拷贝单个 Lucide SVG 到 `ui/icons/` → manifest 登记 → 代码引用（动态渲染的图标须加入 `app.js` 的 `preloadSvgIcons` 预取集合或 `_CAT_ICONS`/`_CAT_PALETTE`/`_STATUS_ICONS` 映射）→ 守卫测试通过。禁止引入第二图标库或整库拷贝；许可归属与流程见 `ui/icons/README.md`。图标由 JS fetch 后内联（`currentColor` 随 CSS color 着色），favicon 由 `layout-grid.svg` 加随机主题色动态生成

## 配置

All via `.env` file. Required: `AI_API_KEY`; `WEFLOW_API_TOKEN` when the `weflow` plugin is enabled (default); `QQFLOW_API_TOKEN`/`QQFLOW_QQ`/`QQFLOW_KEY` when the `qqflow` plugin is enabled (missing any of the latter three → the plugin self-disables via `PluginDisabledError`). Optional (with defaults):

> 默认值的事实来源是 `briefdesk/config.py` 与各插件包的 `config.py`；本表仅为速查，修改代码默认值时须同步本表。

| Env var | Default | Purpose |
|---|---|---|
| `PLUGINS` | `["*"]` | **JSON array** of enabled plugin names; `"*"` = all discovered. 消息源启用的唯一开关（weflow/qqflow 等源插件由本开关控制）。声明 `default_disabled = True` 的插件（如实验性 benchmark）默认不随 `"*"` 加载，需显式列名（`PLUGINS=["*", "benchmark"]`）才启用 |
| `PLUGINS_DISABLED` | `[]` | **JSON array** of disabled plugin names (takes precedence over `PLUGINS`) |
| `PLUGINS_REQUIRED` | `[]` | **JSON array** of plugins whose setup/activate failure is fatal (`PluginError` 中止启动) |
| `PLUGIN_PATH` | `` (disabled) | 开发期插件目录：目录下每个 *.py 暴露 `plugin` 实例即被加载（免打包） |
| `WEFLOW_API_BASE` / `WEFLOW_API_TOKEN` / `WEFLOW_SSE_RECONNECT_INITIAL_MS` / `WEFLOW_SSE_RECONNECT_MAX_MS` | `http://127.0.0.1:5031` / `` / `1000` / `60000` | WeFlow source-specific (read by `briefdesk/plugins/weflow/config.py`, only when the `weflow` plugin is enabled) |
| `QQFLOW_API_BASE` / `QQFLOW_API_TOKEN` / `QQFLOW_QQ` / `QQFLOW_KEY` / `QQFLOW_DB_PATH` / `QQFLOW_SSE_RECONNECT_INITIAL_MS` / `QQFLOW_SSE_RECONNECT_MAX_MS` | `http://127.0.0.1:5032` / `` / `` / `` / `` / `1000` / `60000` | qqflow source-specific (read by `briefdesk/plugins/qqflow/config.py`, only when the `qqflow` plugin is enabled). `API_TOKEN`/`QQ`/`KEY` **required** — missing any → the plugin self-disables (`PluginDisabledError`). `DB_PATH` optional (empty → upstream qqflow-server falls back to platform defaults, e.g. Windows `Documents\Tencent Files`) |
| `AI_API_BASE` | `https://api.deepseek.com` | OpenAI-compatible API base |
| `AI_MODEL` | `deepseek-v4-flash` | Model name for classify/dedup |
| `AI_MAX_CONCURRENCY` | `0` (unlimited) | Max concurrent AI API requests; set `1` for local models with concurrency limit 1 |
| `AI_DISABLE_THINKING` | `false` | When `true`, chat requests pass `reasoning_effort="none"` to disable thinking mode (Qwen3/Qwen3.5); default sends no such parameter |
| `MAX_CLASSIFY_TOKENS` | `8192` | Max output tokens per classify call (DeepSeek cap 8192; truncation breaks JSON) |
| `LOG_LEVEL` | `INFO` | 日志级别（DEBUG / INFO / WARNING / ERROR / CRITICAL）。DEBUG 开启逐条细节（事件/请求/过滤决策），INFO 只保留阶段与汇总；同时驱动 uvicorn 自身 logger 的级别门 |
| `DB_PATH` | `briefdesk.sqlite` | SQLite file path |
| `SERVER_PORT` | `3000` | FastAPI/uvicorn port |
| `BACKFILL_HOURS` | `24` | REST 回填窗口：会话启用/从未轮询时按该窗口回填一次，此后按会话增量；`-1` = pull all history (warns at startup, pages to hasMore end) |
| `IGNORED_EXPIRY_HOURS` | `0` (disabled) | On startup, purge ignored items older than this many hours (deletes from `items` + `raw_messages`, keeps `processed_messages`) |
| `IGNORE_SELF` | `true` | 过滤本账号自己发送的消息（所有消息入口：SSE 实时 + REST 回填）。weflow REST 按 `isSend` 判定（SSE 上游已不推送自消息）；qqflow REST 按自身 UID（`u_<QQFLOW_QQ>`）判定，SSE 事件无发送者标识、开启后按消息回查 REST（每消息 +1 次本机 HTTP） |
| `REALTIME_BATCH_MAX_COUNT` / `REALTIME_BATCH_TIMEOUT_MS` | `1` / `180000` | Realtime batch buffer flush thresholds (跨源公共：监听器攒批 + 实时路径 pipeline 切批) |
| `BACKFILL_BATCH_MAX_COUNT` | `20` | Backfill batch size per AI classify call (independent of SSE batching) |
| `DEDUP_SIMILARITY_THRESHOLD` | `0.3` | Dedup pre-filter: title character-overlap ratio that triggers AI semantic dedup（嵌入整体不可用时为主通道；嵌入模式下余弦零候选时作兜底通道） |
| `EMBED_API_BASE` | `` (empty = disabled) | Embedding API base URL; non-empty enables embedding cosine pre-filter (falls back to `AI_API_BASE` if empty within enabled mode) |
| `EMBED_MODEL` | `` (falls back to `AI_MODEL`) | Embedding model name |
| `EMBED_API_KEY` | `` (falls back to `AI_API_KEY`) | Embedding API key |
| `EMBED_BATCH_SIZE` | `20` | Batch size for all embedding calls (history cache load + per-batch pre-embedding) |
| `DEDUP_EMBED_THRESHOLD` | `0.80` | Cosine similarity threshold that promotes a candidate to AI judgment |
| `DEDUP_EMBED_TOP_K` | `3` | Max candidates (by similarity desc) sent to the AI judge |
| `DEDUP_STRONG_THRESHOLD` | `0.99` | 同文本短路阈值：候选相似度 ≥ 此值时 AI 判 SAME 即直接判重、不参与多数票（防高相似但不同话题的干扰候选稀释成平票漏判） |
| `DEDUP_EMBED_FALLBACK_THRESHOLD` | `0.65` | 低置信复核阈值：余弦候选落在 [fallback, `DEDUP_EMBED_THRESHOLD`) 区间且无 normal 候选时，全员判 SAME 才判重（覆盖中段相似度、低于 normal 门禁但确实重复的情形，如余弦略低于阈值但标题逐字相同） |
| `MERGE_WINDOW_MINUTES` | `10` | 会话内同话题片段合并窗口（分钟）：新卡与同会话同类别、msg_time 相差不超过该值的未核实卡进入 AI 合并判定；`0` = 禁用合并 |
| `MERGE_MAX_CANDIDATES` | `3` | 每张新卡最多送合并判官判定的候选头卡数 |

## 设计要点与陷阱

### 运行时与优雅关闭

- SSE client and uvicorn share a single `asyncio` event loop (no worker processes). `Server(config).serve()` is called directly — note `reload=True` is a no-op there (reload only works through `uvicorn.run()` supervisor path), so it is not configured
- Graceful shutdown: the Ctrl+C handler (via `_install_signal_handlers`) calls `signal_shutdown()` to end all `/api/stream` SSE streams (they are long-lived ASGI tasks that would otherwise block uvicorn's shutdown forever), then sets `server.should_exit`. The handler does nothing else — all cleanup happens in `_run()`'s single `finally`, which covers graceful and exception paths alike. `timeout_graceful_shutdown=5` is a safety net for stuck tasks. DB close must happen while the loop is still alive (aiosqlite's worker thread is non-daemon; an unclosed connection makes interpreter exit hang on thread join — see `close_db` in db.py). **Gotcha:** uvicorn's `serve()` installs its own `handle_exit` via `capture_signals()` at startup, overwriting any handler registered before it — so `_install_signal_handlers` must run **after** `server.started` becomes true (the `_run` startup loop waits for it), otherwise Ctrl+C never calls `signal_shutdown()` and the SSE streams stall until the 5s timeout force-cancels them

### 去重管线

- Dedup in-memory cache is pre-warmed by the dedup plugin's `setup` (inside `PluginManager.setup_all()`, before HTTP server and message sources start — full history load + missing embeddings); the lazy `ensure_cache` inside `check_dedup` remains as a fallback for first use. Wrapped in `DedupEngine` instance owned by `DedupPlugin`
- Embedding dedup (when `EMBED_API_BASE` set): cosine pre-filter (fallback-threshold recall, tiered into normal/weak) is the primary channel; character-overlap remains the fallback when embeddings are entirely unavailable **and** a single-candidate fallback when cosine returns zero candidates（例：余弦略低于阈值但标题逐字相同的真实重复，由重叠兜底兜回）。Vectors persist in `item_embeddings` so restarts don't re-embed history. Degrades gracefully tier by tier: cache-load failure → whole process falls back to overlap; single-query embed failure → that check falls back; batch-level `preembed_batch` failure → whole batch falls back to character-overlap; items without a vector just skip cosine candidacy (re-embedded on next restart by `_ensure_cache`'s missing-vector check). Embedding pre-computation (`ensure_cache` / `preembed_batch`) and vector persistence (`flush_pending_embeddings`) all run **outside** `_storage_lock`; inside the lock only dedup judgment and DB writes remain

### 管道并行与锁

- Batch classification runs in parallel (`asyncio.create_task` + `as_completed`, one task per batch, enrich+classify stages); DB writes are serialized under `_storage_lock` in the pipeline skeleton (dedup stage + skipped marking + merge stage as one atomic section)
- 启用会话过滤、已处理过滤与 raw_messages 落库统一在 pipeline 入口（`process_all_batches`）完成，每批实时查询（无缓存）；`RealtimeListener.invalidate_session_cache()` 保留为 no-op 兼容调用。`SourceRuntime.refresh_sessions()` 返回 `list[SessionInfo]` 由应用层（main 的 `_refresh_all`）统一写库；`fetch_history(enabled_sessions)` 的启用会话由 `poll_cycle` 查询传入（源不触碰 DB）

### 图片与 OCR

- Images flow: SSE `[图片]` / REST `localType=3` messages are kept → `normalize` extracts relative media path → source client `download_media()` fetches bytes (direct auth, no local proxy) → OCR (`ocr_images_bytes`) recognizes text → OCR text is masked (`mask_content`) and replaces content as `[OCR]` block. 无文字图片（`RapidOCRError`）在 `plugins/ocr/engine.py` 视为空结果；下载失败（`MediaError`）与识别异常在 ocr 阶段插件（`plugins/ocr/plugin.py`）只跳过该条 OCR、不拖垮批次（失败消息以原文入库）。`/api/media/:source/:path` proxy remains for the frontend (browsers can't carry source token)
- OCR 未启用（enrich 槽位为空）时，纯占位符图片消息（`image_urls` 非空且 content 为单个附件占位符如 `[图片]`/`[image]`）在 pipeline 入口被直接屏蔽：不落 raw、不进分类、不标记 processed（重新启用 OCR 后回填窗口内自动重拉）。**图片+文字混合消息**（qqflow-server 实测存在：`localType=3` + `mediaId` 非空且 content 为真实文字）不受影响——文字仍有信息价值，照常处理；屏蔽判定用 `_PLACEHOLDER_ONLY_RE`（与源侧 normalize 占位符语义一致）而非 `image_urls` 非空，避免误伤混合消息

### 实时推送与前端联动

- Real-time frontend updates ride an in-process pub/sub (`realtime.py`) → `/api/stream` SSE, one queue per subscriber (maxsize 32, drop-if-full). 事件按名分型：`items_updated`（列表刷新/同步完成）与 `sync_progress`（**同步进度，并入状态指示器胶囊**——新增消息数含处理中：pipe 入口 `note_sync_batch_start` 计数、每批完成 `note_sync_batch_done` 递减、pending 归零置 done；快照同时经 `/api/status` 的 `syncProgress` 下发。前端 `renderSyncProgress` 渲染到 `#status-indicator`：处理中仅显示「＋N 条新消息 · 处理中 M」（源状态文本隐藏、连接圆点保留），收尾「✓ 已同步 N 条」约 3s 后经 `restoreStatusTextAfterProgress` 用 `lastStatusInfo` 恢复源状态文案；`updateStatus` 在进度非空闲（`syncProgressPhase`）时不覆盖进度文本；刷新中途由 `restoreSyncProgress` 恢复）

### 日志与配置

- 数据库路径统一经 `config.db_path` 读取（`db.get_db()` 直读；`experiments/dedup_compare.py` 等实验脚本通过给 `config.db_path` 赋值指向临时库）
- 日志体系：格式/级别/uvicorn 统一见 `briefdesk/logger.py` 行；`uvicorn.Config(log_config=None)` 必须与 `setup_logging()` 的 uvicorn logger 清理配合，否则启动阶段 dictConfig 会重新挂回 uvicorn 自带 handler（无时间戳、propagate=False）

### 启动期上游连接竞态

- briefdesk 启动即发起「首轮回填（REST）+ SSE 首连」，若上游（qqflow-server / WeFlow）TCP 尚未监听，REST 侧由 `with_connect_retry` 短退避自愈（3 次 ≈0.5s/1s/2s，耗尽仍写 lastError）；SSE 侧由监听器退避重连自愈。上游就绪慢于重试窗口时仍会记一次 lastError——彻底消除需「启动回填前置探活」（暂未实施）。
