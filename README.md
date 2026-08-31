# 简报台

> 本地运行的网页应用，通过可插拔消息源（weflow-server 或 WeFlow 监听微信群，qqflow-server 监听 QQ 群）获取群消息，AI 自动分类筛选有价值的信息并结构化展示。

## 功能

- **消息获取**：启动时后台自动回填一次（仅已启用会话参与；会话启用/从未轮询时按 `BACKFILL_HOURS` 默认 24h 窗口回填一次，设为 -1 拉取全部历史——每次同步都会全量扫描，慎用），此后每轮**按会话增量轮询**（各会话独立水位，`POLL_OVERLAP_SECONDS` 控制重叠容错），并辅以 SSE 实时监听（weflow-server / WeFlow 微信群、qqflow-server QQ 群可多源共存）
- **AI 分类**：AI 自动将消息分类，内置 13 类、出厂默认启用 5 类（活动通知、社团招新、学术、交易、实习）；可在设置中自定义类别（名称/提示词/颜色/启停）
- **图片 OCR**：聊天图片自动 OCR 识别文字，识别结果纳入分类与展示
- **视觉分类（可选）**：AI_MODEL 支持图片输入（多模态）时开启 `AI_VISION_ENABLED`，含图消息把 OCR 文本连同图片一并送入分类；端点不支持图片时自动降级纯文本重试
- **语义去重**：同一条信息在多个群转发时自动合并，保留多来源标注
- **原文上下文**：展开信息卡片可查看原文及附近消息
- **人工审核**：卡片可标记加入备忘录 / 已忽略，侧边栏按状态查看
- **同主体折叠**：同一信息主体的卡片自动折叠成组，可展开
- **群聊筛选**：设置中自由选择监控哪些群聊
- **分类过滤**：设置中可选隐藏不需要的信息类型

## 快速开始

### 前置条件

- Python >= 3.12
- 任一消息源运行中：weflow-server（微信 4.x，默认 `http://127.0.0.1:5033`）、WeFlow（HTTP API 已开启，默认 `:5031`）或 qqflow-server（默认 `http://127.0.0.1:5032`）
- AI API Key（环境变量 `AI_API_KEY`）

### 安装运行

```bash
cd briefdesk
# 必须以 editable 方式安装：ui/ 静态资源与 .env.example 在源码目录，
# 非 editable 安装（pip install .）不会打包它们，页面会 404 无法使用。
pip install -e .
# 可选：启用图片 OCR 需额外安装 OCR 依赖（rapidocr/onnxruntime，
# 体积较大；不安装时 OCR 插件自动禁用，其余功能不受影响）
pip install -e ".[ocr]"
```

首次使用需要创建配置文件（填入密钥，见下方 [配置](#配置)）：

```bash
# Linux / macOS
cp .env.example .env
# Windows
copy .env.example .env
```

```bash
python main.py
```

浏览器打开 `http://localhost:3000`

### 首次使用（关键步骤）

首次打开页面会弹出**首次使用向导**（仅当浏览器从未完成过向导且尚未启用任何群聊时出现；后端不可用时不弹），分三步：

1. **环境检查**：展示各消息源连接状态；若后端有错误/警告（如 `AI_API_KEY` / 所选消息源的必填密钥缺失、消息源未启动）会给出提示与常见原因；
2. **选择群聊**：勾选要监控的会话——**群聊默认勾选，私聊 / 公众号默认不勾**（避免无意监控私聊）。会话列表为空时可先到 设置 → 群聊筛选 点「发现新群聊」拉取最新会话列表，再回来重试；
3. **开始同步**：首次会按 `BACKFILL_HOURS`（默认 24 小时）回填一次已启用会话的历史消息，此后按会话增量轮询 + SSE 实时监听；同步完成后回到主页面，AI 分类后的信息卡片会陆续出现。

随时可点右上角「跳过」（或按 Esc）退出向导，之后再手动操作：**设置 → 群聊筛选** 勾选群聊 → 顶部 **同步消息**。跳过或走完向导后不再弹出（浏览器 localStorage 记忆）。

> 未启用任何群聊时，前端空态会给出可操作的下一步提示：「去『群聊筛选』发现会话」/「去设置启用群聊」/「立即同步」，点击即可跳转；未启用会话不会拉取任何消息，这不是故障。

## 依赖

项目核心依赖（分类、去重、服务端、数据库）如下：

- `fastapi`
- `uvicorn[standard]`
- `httpx`
- `aiosqlite`
- `pydantic`
- `pydantic-settings`
- `openai`
- `python-dotenv`
- `numpy`
- `Pillow`
- `json-repair`（AI 输出的 JSON 修复解析兜底）
- `python-multipart`（表单解析）

OCR 依赖为**可选**（`pip install -e ".[ocr]"`）：

- `rapidocr`（RapidOCR，基于 ONNX 的跨平台 OCR 框架）
- `onnxruntime`（ONNX Runtime CPU 推理后端，无需 GPU / CUDA）

未安装 OCR 依赖时，OCR 阶段插件在启动时自禁用（`PluginDisabledError`，非致命），图片消息仍以原文入库展示，仅不做文字识别。

## 配置

所有配置通过项目根目录的 `.env` 文件读取，不配置时使用默认值。**直接复制模板即可开始**：

1. 复制 `.env.example` 为 `.env`：

   ```bash
   # Linux / macOS
   cp .env.example .env
   # Windows
   copy .env.example .env
   ```

2. 编辑 `.env`，填入必填项 `AI_API_KEY`；消息源为 `weflow` 时需 `WEFLOW_API_TOKEN`/`WEFLOW_WXID`/`WEFLOW_DB_KEYS`（微信每库独立密钥的 JSON 映射，过长时拆 `WEFLOW_DB_KEYS_2` 第二段；密钥项走系统钥匙串而非 `.env`），为 `weflow-legacy` 时需 `WEFLOW_LEGACY_API_TOKEN`，为 `qqflow` 时需 `QQFLOW_API_TOKEN`/`QQFLOW_QQ`/`QQFLOW_KEY`（weflow/qqflow 缺失任一必填项 → 该插件自禁用）。消息源启用走 `PLUGINS` / `PLUGINS_DISABLED`（weflow/weflow-legacy/qqflow 均为内置插件），其余按需修改。
3. `AI_MODEL` 默认 `deepseek-v4-flash`：若你对接的 OpenAI 兼容服务没有该模型名，请改为实际模型名（如 `deepseek-chat`、`qwen-turbo` 等），否则首次分类会报模型不存在。

常用可调项（完整清单与逐项注释见 `.env.example`）：

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `PLUGINS` / `PLUGINS_DISABLED` | `["*"]` / `[]` | 插件启用/禁用（JSON 数组），消息源启用的唯一开关（不再使用 SOURCES） |
| `POLL_OVERLAP_SECONDS` | `300` | 增量轮询窗口与水位间的重叠秒数（吸收边界秒/时钟偏差/翻页偏移；重叠部分由已处理表去重，无 AI 开销） |
| `POLL_INTERVAL_SECONDS` | `0`（禁用） | 周期同步间隔（秒）：SSE 断连窗口的消息补齐兜底，>0 时按周期自动触发与「同步消息」同路径的同步（进行中互斥） |
| `IGNORE_SELF` | `true` | 过滤本账号自己发送的消息（SSE 实时 + REST 回填） |
| `MAX_CLASSIFY_TOKENS` | `8192` | 单次 AI 分类最大输出 token（触顶截断会破坏 JSON） |
| `AI_MAX_CONCURRENCY` | `4` | AI 请求最大并发，`0` = 不限制（本地模型建议设 1） |
| `AI_DISABLE_THINKING` | `false` | 关闭思考模式（Qwen3/Qwen3.5 等；DeepSeek 等思考系模型强烈建议开启，思考输出挤占 max_tokens 会截断丢批） |
| `AI_VISION_ENABLED` | `false` | 视觉模型开关：AI_MODEL 支持图片输入时开启，含图消息将 OCR 文本连同图片一并送入分类（需启用 ocr 插件；端点不支持图片时自动降级纯文本重试并公告提示） |
| `AI_VISION_MAX_IMAGES` | `4` | 单条消息随分类请求附图上限（1–20；多图超出部分只发 OCR 文本） |
| `REALTIME_BATCH_MAX_COUNT` / `REALTIME_BATCH_TIMEOUT_MS` | `1` / `180000` | 实时批缓冲（攒够条数 / 超时毫秒触发处理） |
| `BACKFILL_BATCH_MAX_COUNT` | `10` | 回填时单批 AI 分类的消息条数（默认已由 20 调为 10：批越大 AI 返回 index 漂移的绝对误差面越大） |
| `IGNORED_EXPIRY_HOURS` | `0`（禁用） | 启动时清理超过该时长的已忽略条目 |
| `MERGE_WINDOW_MINUTES` / `MERGE_MAX_CANDIDATES` | `10` / `3` | 会话内同话题片段合并窗口（分钟）/ 候选上限；`0` = 禁用合并 |
| `EMBED_API_BASE` / `EMBED_MODEL` / `EMBED_API_KEY` | 空（禁用） | 嵌入向量去重：填入嵌入服务地址后启用余弦预筛（留空回退字符重叠预过滤） |
| `DEDUP_SIMILARITY_THRESHOLD` / `DEDUP_EMBED_THRESHOLD` / `DEDUP_EMBED_TOP_K` / `DEDUP_EMBED_FALLBACK_THRESHOLD` / `DEDUP_STRONG_THRESHOLD` | `0.3` / `0.80` / `3` / `0.65` / `0.99` | 去重预筛与 AI 判重门禁阈值 |
| `LOG_LEVEL` | `INFO` | 日志级别（DEBUG 开逐条细节，并放出 uvicorn 的 HTTP 请求日志——默认静默以免逐请求刷屏） |
| `DB_PATH` / `SERVER_PORT` | `briefdesk.sqlite` / `3000` | SQLite 路径 / Web 端口 |

> 配置说明：`.env` 与默认数据库路径均以项目根目录为基准解析，从任意目录启动（`python main.py` / `python -m briefdesk` / `briefdesk`）都会读取同一份配置（显式配置的相对 `DB_PATH` 仍按当前工作目录解析）。
> `PLUGINS` 未启用任何消息源插件时进入**降级启动**：应用照常运行（UI/设置/向导可用），状态栏明示「无消息源」，消息采集不可用——配置消息源后重启生效。

## 项目结构

```
briefdesk/
├── main.py                 # 入口 shim，转发至 briefdesk/main.py（DB → 插件装配（含源注册/去重缓存预热）→ HTTP 服务器 → 插件激活 + 源监听启动 → 首轮回填）
├── .env.example            # 环境配置模板（复制为 .env 后填写）
├── .env                    # 密钥配置（本地生成，不入库）
├── briefdesk/
│   ├── __main__.py         # python -m briefdesk 入口
│   ├── config.py           # pydantic Settings 读 .env（.env 与默认 DB 路径以项目根目录为基准）
│   ├── logger.py           # 日志配置（格式/级别/uvicorn 统一）
│   ├── masking.py          # 消息内容脱敏（mask_content）
│   ├── events.py           # 内部事件总线（核心 ↔ 插件解耦）
│   ├── plugin/             # 插件框架：Plugin 协议 + PluginManager（entry points / PLUGIN_PATH 发现）
│   │   ├── base.py             # Plugin 协议、PluginContext、加载约定
│   │   └── manager.py          # 发现、过滤、拓扑排序、生命周期编排
│   ├── db.py               # aiosqlite 数据库操作（主连接 + 向量连接，WAL + 外键）
│   ├── pipeline.py         # 管道骨架（入口统一过滤 → 并行 enrich/classify → 锁内 dedup/跳过标记/post_insert → 通知）
│   ├── poll_cycle.py       # 轮询周期业务编排（按会话增量窗口）
│   ├── realtime.py         # 进程内发布/订阅（前端 SSE）
│   ├── status.py           # 运行时状态 + 消息源注册表（/api/status 数据源）
│   ├── sync.py             # 同步服务（trigger_sync：启动首轮回填与 /api/sync 共用）
│   ├── stages.py           # 管道阶段注册表（槽位 enrich → classify → dedup → post_insert）
│   ├── ai_ports.py         # AI 供应商端口（引擎经端口函数调用，核心不依赖具体供应商）
│   ├── sources_base.py     # 消息源核心契约（SourceClient / RealtimeListener / SourceRuntime）
│   ├── types.py            # 跨模块基础类型（含管道跨插件契约）
│   ├── server/             # FastAPI 服务子包（app/中间件/核心路由/类别路由/媒体代理/静态托管/插件注入/回调）
│   └── plugins/            # 内置插件（消息源 + AI 供应商 + 管道阶段 + Web 插件）
│       ├── weflow/             # weflow 消息源（微信 4.x，weflow-server :5033；同构六文件分层 + weflow-server-api.md）
│       ├── weflow_legacy/      # WeFlow Legacy 消息源（plugin.py + client/config/normalize/poller/runtime/sse + weflow-legacy-api.md）
│       ├── qqflow/             # qqflow 消息源（plugin.py + 同构六文件分层 + qqflow-server-api.md）
│       ├── ai_provider/        # AI 供应商插件（plugin.py 注册端口 / engine.py OpenAI 兼容 chat + 嵌入）
│       ├── ocr/                # OCR 阶段插件（plugin.py 槽位 enrich / engine.py RapidOCR；依赖可选，未安装时自禁用）
│       ├── classify/           # AI 分类阶段插件（plugin.py 槽位 classify / engine.py 提示词与解析）
│       ├── dedup/              # 语义去重阶段插件（plugin.py 槽位 dedup / engine.py DedupEngine）
│       ├── merge/              # 同话题合并阶段插件（plugin.py 槽位 post_insert / engine.py 判官）
│       ├── calendar/           # 日历 Web 插件（plugin.py + router.py + db.py：/api/calendar；ui/ 含完整前端）
│       ├── reminders/          # 提醒 Web 插件（plugin.py + router.py：提醒设置 + 到期轮询；ui/ 含完整前端）
│       ├── rag/                # 检索问答 Web 插件（plugin.py + router.py + db.py + engine.py + prompts.py；ui/ 含完整前端）
│       └── benchmark/           # 实验基准（case 样例 + runner + 报告生成，见 benchmark/README.md；默认禁用，PLUGINS 显式列名启用）
├── ui/
│   ├── index.html          # 桌面端页面
│   ├── app.js              # 前端逻辑
│   ├── style.css           # 样式
│   └── icons/              # 图标资源（Lucide SVG 子集，见 ui/icons/README.md）
└── tests/
```

## 插件化

本体只保留核心（存储、管道骨架、HTTP、状态总线），扩展功能以插件挂载：

- 打包插件经 `[project.entry-points."briefdesk.plugins"]` 声明；开发期插件放
  `PLUGIN_PATH` 目录（每个 `.py` 暴露 `plugin` 实例）即可被加载
- `briefdesk/plugin/manager.py` 的 `PluginManager`：发现 → `PLUGINS` /
  `PLUGINS_DISABLED` 过滤 → 依赖拓扑排序 → setup（HTTP 启动前）→
  activate（服务器就绪后）→ teardown（逆序幂等）
- 单插件失败只禁用该插件；`PLUGINS_REQUIRED` 名单内的失败则中止启动
- 依赖方向由 `tests/test_no_core_imports_plugins.py` 守卫：核心永不 import
  `briefdesk.plugins.*`
- 消息源为内置插件（weflow/weflow-legacy/qqflow），启用走 `PLUGINS` /
  `PLUGINS_DISABLED`（不再使用 SOURCES）
- 声明 `default_disabled = True` 的插件（如实验性 benchmark）默认不随
  `PLUGINS=["*"]` 加载，需显式列名（`PLUGINS=["*", "benchmark"]`）才启用；
  `PLUGINS_DISABLED` 仍为最高优先级
- 管道阶段化：OCR / AI 分类 / 语义去重 / 同话题合并各是一个阶段插件
  （`briefdesk/plugins/{ocr,classify,dedup,merge}/`，槽位
  enrich → classify → dedup → post_insert），`briefdesk/pipeline.py` 只做
  编排（入口过滤 → 并行分类 → 锁内入库/合并 → 状态/通知）；核心删除
  卡片经事件总线通知去重插件清理缓存；OCR 依赖（rapidocr/onnxruntime）
  为可选安装，未安装时 OCR 插件自禁用、纯图片消息（无文字）在入口被
  屏蔽（不落库且水位照常推进，重新启用 OCR 后**不会自动恢复**——要找回
  需重新停用/启用会话触发 `BACKFILL_HOURS` 回填，或临时设为 -1 全量回填），
  图片+文字混合消息不受影响
- AI 供应商插件（`briefdesk/plugins/ai_provider/`）：chat + 嵌入能力注册到
  `briefdesk/ai_ports.py` 端口，引擎经端口函数调用、核心不依赖具体供应商；
  分类/去重/合并声明依赖 ai_provider（被禁用时随依赖降级，管道对
  "阶段缺失"整批保留不标记，防消息丢失）
- Web 插件（`WebPlugin` 协议：router + 静态资源）：日历/提醒/检索问答路由为
  示范插件（`briefdesk/plugins/{calendar,reminders,rag}/`）；核心提供 `/api/plugins`
  元数据与 `/plugin-assets/<name>/` 静态资源；**插件的完整前端随插件包
  分发**——calendar 的按钮/视图容器/浮层/样式全在 `calendar/ui/`
  （`ui.js` 自建 DOM 并注册核心 `registerPluginView` 视图钩子接入 hash
  路由与刷新联动，`ui.css` 随注入）；reminders 的提醒按钮/菜单、设置
  面板「自动提醒」控件与到期轮询全在 `reminders/ui/`（`ui.js` 注册核心
  `registerItemRowExtension` 行内扩展接入卡片动作区与 `handleRowAction`，
  经 `data-plugin-slot` 注入设置面板）；核心 `ui/` 只留通用加载器与两类
  扩展钩子（`CoreFrontendBoundaryTest` 守卫），不写死任何插件功能入口

当前阶段（P5）：消息源、管道四阶段、AI 供应商与 Web 插件全部插件化，
插件化改造完成（本体只保留存储、管道骨架、HTTP 与状态总线等核心）。

## 架构

简报台采用「核心骨架 + 插件」架构：可插拔消息源（weflow / weflow-legacy / qqflow）产出归一化消息，经 pipeline 入口统一过滤后进入阶段插件流水线（OCR 增强 → AI 分类 → 语义去重 → 同话题合并），结果写入 SQLite，由 FastAPI 服务端经 SSE 实时推送到原生 JS 前端；本体只保留存储、管道骨架、HTTP 与状态总线等核心。

完整的数据流图、模块职责、插件框架、数据库 Schema、配置项与设计陷阱详见 [docs/architecture.md](docs/architecture.md)；日常使用（启动、配置、常见操作）详见 [docs/USAGE.md](docs/USAGE.md)。

## 技术栈

- **运行时**: Python + uvicorn
- **Web 框架**: FastAPI
- **数据库**: SQLite (aiosqlite 异步)
- **AI**: AI Chat API (openai SDK 兼容)
- **HTTP 客户端**: httpx (异步)
- **前端**: 原生 HTML + CSS + JavaScript（无框架）
- **消息源**: 可插拔多源（weflow-server :5033 微信 4.x、WeFlow :5031、qqflow-server :5032；消息源为内置插件，通过 `PLUGINS` / `PLUGINS_DISABLED` 启用，JSON 数组格式）
