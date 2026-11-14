# 使用指南（Usage）

> 这份指南覆盖安装、首次使用向导、日常操作、设置、常见问题与数据隐私；门面见 `README.md`，架构与插件契约见 `docs/architecture.md` 与 `docs/plugin-dev.md`。

## 安装与启动

装好 Python 3.12 与任一消息源后，复制配置模板、填好 `AI_API_KEY` 即可启动；发布版与试发版各有独立安装命令。

### 前置条件

- Python >= 3.12
- 任一消息源已运行：weflow-server（微信 4.x，`http://127.0.0.1:5033`）、WeFlow（`:5031`）或 qqflow-server（`:5032`）
- AI API Key（`AI_API_KEY`）

### 步骤

```bash
cd briefdesk
pip install -e .                 # 可选图片 OCR：pip install -e ".[ocr]"
cp .env.example .env             # Windows: copy .env.example .env
# 编辑 .env 填入必填项（见下方配置）
python main.py                   # 或 python -m briefdesk / briefdesk
```

浏览器打开 `http://localhost:3000`。

**从索引安装（发布版）**：`pip install briefdesk`（可选图片 OCR：`pip install "briefdesk[ocr]"`），用 `briefdesk` 启动。**发布版不读项目根或当前目录的 `.env`**（`paths.project_dotenv_path()` 在 wheel 模式返回 None）：配置走进程环境变量，或在设置页「启动配置」与「密钥区」填写；数据目录位置与源码安装一致。

### 试用预发布版（TestPyPI）

试发版本发在 TestPyPI（形如 `0.1.0.dev1`），装法与正式包**不同**——两段式，每段只对一个索引：

```bash
# 1) 本包：只对 TestPyPI，且先不解析依赖
pip install --no-deps --index-url https://test.pypi.org/simple/ briefdesk==0.1.0.dev1
# 2) 依赖：只对正式 PyPI（清单以包元数据为准：pip show briefdesk 的 Requires，
#    或 https://test.pypi.org/pypi/briefdesk/json）
pip install fastapi "uvicorn[standard]" httpx aiosqlite pydantic pydantic-settings \
    openai python-dotenv Pillow numpy json-repair python-multipart keyring platformdirs \
    python-dateutil typing-extensions
```

**不要**改成 `--index-url https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple/` 一步装：
pip 会把两个索引合并后取最高版本，而 TestPyPI 上存在别人试传的同名依赖（`fastapi 1.0` 稳定版，正式 PyPI 是 `0.142.x`），依赖会被影子化成残缺包、构建直接失败。

## 首次使用向导

首次打开页面会弹出向导（仅当浏览器从未完成过向导且尚未启用任何群聊时出现；后端不可用时不弹），三步完成环境检查、群聊选择与首次同步；跳过或走完后可在设置里手动操作。

1. **环境检查**：展示各消息源连接状态；若后端有错误/警告（如 `AI_API_KEY` / 所选消息源的必填密钥缺失、消息源未启动）会给出提示与常见原因；
2. **选择群聊**：勾选要监控的会话——**群聊默认勾选，私聊 / 公众号默认不勾**（避免无意监控私聊）。会话列表为空时可先到 设置 → 群聊筛选 点「发现新群聊」拉取最新会话列表，再回来重试；
3. **开始同步**：首次会按 `BACKFILL_HOURS`（默认 24 小时）回填一次已启用会话的历史消息，此后按会话增量轮询 + SSE 实时监听（默认无周期轮询，见「同步状态」说明）；同步完成后回到主页面，AI 分类后的信息卡片会陆续出现。

随时可点右上角「跳过」（或按 `Esc`）退出向导，之后再手动操作：**设置 → 群聊筛选** 勾选群聊 → 顶部 **同步消息**。跳过或走完向导后不再弹出（浏览器 localStorage 记忆）。

> 未启用任何群聊时，前端空态会给出可操作的下一步提示：「去『群聊筛选』发现会话」/「去设置启用群聊」/「立即同步」，点击即可跳转；未启用会话不会拉取任何消息，这不是故障。

## 日常使用

主列表、卡片详情、状态与同步是日常操作的三个入口，筛选与手动同步都在这里完成。

### 主列表

- **侧边栏**：按分类（全部/活动通知/社团招新…）与状态（未处理/备忘录/已忽略）过滤。
- **卡片操作**：加入备忘录 / 忽略 / 复制 / 改分类（`⋯` 菜单内）。
- **同主体折叠**：同一信息主体的卡片自动折叠成组，可展开查看全部。
- **显示模式**：顶部「按主体 / 按群聊 / 逐条」切换列表聚合方式。
- **搜索与时间**：顶部搜索框与时间范围筛选叠加生效。

### 卡片详情

展开卡片可查看：

- **原文引用**（结构化提取的标题/关键信息/原文）与 **上下文**（附近消息，点击加载）。
- **多时间点**：含多个截止时间时逐点展示徽章；日历按每个时间点计入日期格子。
- **文章卡片**：末尾有可点原文链接（新窗口打开）。

### 状态与同步

- 顶部状态胶囊显示消息源连接状态与同步进度（新增条数、处理中/已完成）。
- 手动触发：顶栏 **同步消息** 按钮，或向导中的 **开始同步**。
- 周期同步：默认关闭（`POLL_INTERVAL_SECONDS=0`），SSE 断连窗口的消息不会自动补齐——手动点「同步消息」或把该变量设为正值（如 `600`）后按间隔自动同步。

## 设置

群聊筛选、分类过滤与启动配置构成设置的全部内容；启动配置的改动需重启生效。

### 群聊筛选

- 支持按类型（全部/群聊/私聊/公众号）、按消息源、按名称搜索四维筛选；与时间过滤叠加仅影响显示，不改变已保存的启用会话。
- 点 **发现新群聊** 拉取最新会话列表；勾选后点 **保存** 生效（启用会话触发一次性回填）。

### 分类过滤

在 **设置 → 信息分类** 中自定义类别：名称 / 提示词 / 颜色 / 启用开关。改后即时生效于后续分类；已入库卡片可经 **改分类** 纠正。

### 启动配置（高级）

**设置 → 启动配置** 为 `.env` 的 UI 覆写层（白名单字段）：

- 表单显示当前值与来源（暂存/环境变量/`.env`/默认）；环境变量优先时暂存不生效会有提示。
- 点 **保存** 写入暂存文件，**重启生效**；点 **恢复默认** 清除暂存。
- **密钥区**（AI/嵌入/消息源 Key）只显示是否已配置，明文不回传；输入后写入系统钥匙串，可清除。

常用可调项见下方环境变量逐字清单与 `.env.example` 逐项注释。

#### 环境变量逐字清单

- 环境变量：`PLUGINS` — 默认：`[]`；说明：可选插件（消息源/ocr/benchmark）显式启用列表（JSON 数组，无通配）；核心插件恒装配，亦可在设置页「插件」面板逐个开关
- 环境变量：`POLL_OVERLAP_SECONDS` — 默认：`300`；说明：增量轮询窗口与水位间的重叠秒数（吸收边界秒/时钟偏差/翻页偏移；重叠部分由已处理表去重，无 AI 开销）
- 环境变量：`POLL_INTERVAL_SECONDS` — 默认：`0`（禁用）；说明：周期同步间隔（秒）：SSE 断连窗口的消息补齐兜底，>0 时按周期自动触发与「同步消息」同路径的同步（进行中互斥）
- 环境变量：`IGNORE_SELF` — 默认：`true`；说明：过滤本账号自己发送的消息（SSE 实时 + REST 回填）
- 环境变量：`MAX_CLASSIFY_TOKENS` — 默认：`8192`；说明：单次 AI 分类最大输出 token（触顶截断会破坏 JSON）
- 环境变量：`AI_MAX_CONCURRENCY` — 默认：`4`；说明：AI 请求最大并发，`0` = 不限制（本地模型建议设 1）
- 环境变量：`AI_REASONING_EFFORT` — 默认：`auto`；说明：推理强度：`auto` 不干预 / `off` 尽力关闭思考（发送 `reasoning_effort="none"`，端点明确拒收时自动改为不发送）/ `omit` 永不发送 / `minimal`…`max` 固定强度。DeepSeek 等思考系模型建议 `off`：思考输出挤占 max_tokens 会截断丢批（取值必须全小写）
- 环境变量：`AI_JSON_MODE` — 默认：`auto`；说明：JSON 严格输出（`response_format=json_object`）：`auto` 启发式（ollama 端点或 deepseek-v4 系列）/ `on` 强制开 / `off` 强制关
- 环境变量：`AI_VISION_ENABLED` — 默认：`false`；说明：视觉模型开关：AI_MODEL 支持图片输入时开启，含图消息将 OCR 文本连同图片一并送入分类（需启用 ocr 插件；端点不支持图片时自动降级纯文本重试并公告提示）
- 环境变量：`AI_VISION_MAX_IMAGES` — 默认：`4`；说明：单条消息随分类请求附图上限（1–20；多图超出部分只发 OCR 文本）
- 环境变量：`REALTIME_BATCH_MAX_COUNT` / `REALTIME_BATCH_TIMEOUT_MS` — 默认：`1` / `180000`；说明：实时批缓冲（攒够条数 / 超时毫秒触发处理）
- 环境变量：`BACKFILL_BATCH_MAX_COUNT` — 默认：`10`；说明：回填时单批 AI 分类的消息条数（默认已由 20 调为 10：批越大 AI 返回 index 漂移的绝对误差面越大）
- 环境变量：`IGNORED_EXPIRY_HOURS` — 默认：`0`（禁用）；说明：启动时清理超过该时长的已忽略条目
- 环境变量：`MERGE_WINDOW_MINUTES` / `MERGE_MAX_CANDIDATES` / `MERGE_PREFILTER_THRESHOLD` — 默认：`10` / `3` / `-1`（关闭）；说明：会话内同话题片段合并窗口（分钟）/ 候选上限 / 合并判官余弦预筛门（`-1` 关闭；门限须由余弦-真值数据标定产生，未标定前保持关闭）；`0` = 禁用合并
- 环境变量：`BENCHMARK_PAUSE_PIPELINE` / `BENCHMARK_KEEP_RUNS` / `BENCHMARK_RUN_TIMEOUT_SECONDS` / `BENCHMARK_RUN_STALL_SECONDS` — 默认：`true` / `5` / `0`（不限）/ `600`；说明：benchmark 插件专属（需启用 benchmark 插件）：运行期暂停管道 / 保留运行产物数 / 单次运行总时长上限 / 无进展终止阈值
- 环境变量：`EMBED_API_BASE` / `EMBED_MODEL` / `EMBED_API_KEY` — 默认：空（禁用）；说明：嵌入向量去重：填入嵌入服务地址后启用余弦预筛（留空回退字符重叠预过滤）
- 环境变量：`EMBED_BATCH_SIZE` — 默认：`20`；说明：单次嵌入请求的批量条数（历史缓存加载 + 逐批预嵌入共用）
- 环境变量：`DEDUP_SIMILARITY_THRESHOLD` / `DEDUP_EMBED_THRESHOLD` / `DEDUP_EMBED_TOP_K` / `DEDUP_EMBED_FALLBACK_THRESHOLD` / `DEDUP_STRONG_THRESHOLD` — 默认：`0.07` / `0.80` / `3` / `0.65` / `0.99`；说明：去重预筛与 AI 判重门禁阈值（`DEDUP_SIMILARITY_THRESHOLD` 为标题 bigram Jaccard 口径，默认值已按该口径重标定；换度量或换数据集后重新标定用 `scripts/recalibrate_similarity.py`）
- 环境变量：`LOG_LEVEL` — 默认：`INFO`；说明：日志级别（DEBUG 开逐条细节，并放出 uvicorn 的 HTTP 请求日志——默认静默以免逐请求刷屏）
- 环境变量：`TELEMETRY_ENABLED` — 默认：`true`；说明：本地运行指标开关（只记计数与耗时，不含消息正文；写入用户数据目录 `telemetry/`，不外发）。关闭后不再计数也不写入，已生成的文件保留；重启生效，只约束本进程
- 环境变量：`DB_PATH` / `SERVER_PORT` — 默认：用户数据目录 `data/briefdesk.sqlite` / `3000`；说明：SQLite 路径 / Web 端口

> 配置说明：`.env` 以项目根目录为基准解析，从任意目录启动（`python main.py` / `python -m briefdesk` / `briefdesk`）都会读到同一份配置——**仅限源码 / editable 安装**；发布版（wheel）不读任何隐式 `.env`，配置走进程环境变量或设置页。默认数据库落在平台用户数据目录（Windows `%LOCALAPPDATA%\briefdesk\data\briefdesk.sqlite`），可用 `DB_PATH` 覆盖（显式相对路径按当前工作目录解析，`~` 不展开）。
> `PLUGINS` 未启用任何消息源插件时进入**降级启动**：应用照常运行（UI/设置/向导可用），状态栏明示「无消息源」，消息采集不可用——配置消息源后重启生效。

#### 依赖说明

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
- `keyring`（系统密钥环：密钥型配置的第一解析层）
- `platformdirs`（用户配置目录定位）
- `python-dateutil`、`typing-extensions`（vendor SDK 生成层 import 期需要；插件装配对每个 entry point 无条件 `load()`，缺项时该源被记 `failed` 并打 ERROR，向导/插件面板可见）

OCR 依赖为**可选**（`pip install -e ".[ocr]"`）：

- `rapidocr`（RapidOCR，基于 ONNX 的跨平台 OCR 框架）
- `onnxruntime`（ONNX Runtime CPU 推理后端，无需 GPU / CUDA）

未安装 OCR 依赖时，OCR 阶段插件在启动时自禁用（`PluginDisabledError`，非致命）；**纯图片消息（消息内容只有图片占位符）在管道入口被过滤，不会入库**——重新启用 OCR 和会话后可回填窗口内的消息，窗口默认为 24 小时；要找回更早的消息，需先扩大 `BACKFILL_HOURS` 再重新启用会话，或临时设置 `BACKFILL_HOURS=-1` 后同步。图片+文字混合消息不受影响。

## 插件功能

日历、提醒与问一问（RAG）是三个可选插件，各自有独立入口与配置要求。

- **日历**：侧边栏入口，按日期查看带截止时间的卡片；当日浮层可展开详情。
- **提醒**：卡片 `⋯` 菜单设提醒，到期轮询提示；设置面板有自动提醒开关。
- **问一问（RAG）**：右侧问答侧边栏，基于已入库群聊消息检索作答，附引用来源。需配置嵌入与 RAG 模型（`RAG_*`）。

> 以上功能由插件提供，未启用时对应入口不显示，不影响核心使用。

## 常见问题

下面是消息源、图片识别、设置生效、发送者显示与视觉分类的排障问答。

**Q: 页面一直显示“无消息源”？**
A: 未在 `PLUGINS` 中启用任何消息源插件。检查 `.env` 或启动配置中的 `PLUGINS`，并确认对应消息源服务已启动，重试后重启应用。

**Q: 图片消息没有被识别？**
A: 检查是否安装 OCR 依赖且 `ocr` 插件已启用——源码 / editable 安装用 `pip install -e ".[ocr]"`，从索引安装的发布版用 `pip install "briefdesk[ocr]"`。未启用时纯图片消息不会入库（入口过滤）；重新启用 OCR 和会话后可回填窗口内的消息，窗口默认为 24 小时，要找回更早的消息需先扩大 `BACKFILL_HOURS` 再重新启用会话，或临时设置 `BACKFILL_HOURS=-1` 后同步。

**Q: 设置保存后不生效？**
A: 启动配置为重启生效。保存后重启 `python main.py`。

**Q: 消息显示为“未知”发送者？**
A: 上游未提供发送者名时的兜底显示，不影响分类与去重。QQ 群名片等显示名在下游按优先级解析，群名片与备注差异属正常。

**Q: 视觉分类不工作？**
A: 需同时满足：`AI_VISION_ENABLED=true`、`ocr` 插件已启用、模型支持图片输入。不满足时自动降级纯文本分类；端点不支持图片会在顶部公告提示。

## 数据与隐私

所有数据留在本地 SQLite；使用外部 AI 端点时，消息文本会随分类请求外发给所选供应商，图片外发与旧数据边界是另两处需要单独确认的风险。

- 所有数据存于本地 SQLite，默认落在平台用户数据目录（Windows `%LOCALAPPDATA%\briefdesk\data\briefdesk.sqlite`，可经 `DB_PATH` 覆盖）。
- **本地运行指标不外发**：应用在本机记录 AI 调用次数、各阶段耗时与合并判定的余弦-真值（**不含消息正文**），写入用户数据目录 `telemetry/`，不上传任何地方。可在设置页关闭（`TELEMETRY_ENABLED`，重启生效）：关闭后不再写入，已生成的文件保留；快照保留 14 天，余弦-真值明细保留 180 天（清理随同步执行，关闭期间不清理）。
- **文本会发送给 AI 供应商**：AI 分类请求包含群名、发送者名与消息文本（正文语义内容原样外发）。文本脱敏（手机号/身份证/邮箱/银行卡替换为占位符）只覆盖常见写法，**不是全面保证**——带括号等不常见分隔符的手机号（如 `+86 (138) 0013-8000`）不会被替换、会原样外发，敏感文本请按此自行评估。不用外部 AI 端点（本地模型）时不存在该外发。
- **文本脱敏不遮盖图片内容**：开启视觉输入（`AI_VISION_ENABLED`，需 `ocr` 插件）时，消息图片会经归一化后 **连同图片像素一并发送给所选模型供应商**——OCR 识别出的文字会被掩码，但图片里的文字、人脸等内容不经过任何脱敏。是否开启视觉输入，请按你与所选供应商之间的数据约定自行决定。
- **密钥存储分两条路**：系统钥匙串（`briefdesk secrets set` / UI 密钥区，Windows 为凭据管理器/DPAPI）中的密钥随用户账号加密、不落明文；写在 `.env` 里的密钥是**明文文件**——含密钥的 `.env` 不要提交版本库（已在 `.gitignore` 排除）、不要随网盘/备份/截图连带分享。
- 暂存配置（UI「启动配置」）按**字面量**保存：值里写 `${VAR}` 不会被展开、保存时直接拒绝（提示改为写在项目 `.env` 或环境变量层）；跨层引用属项目 `.env`（dotenv 语义）的能力。
- 配置文件（UI 暂存）在 `%LOCALAPPDATA%\briefdesk\settings.env`；基准用例/报告在 `…\briefdesk\benchmark\`、运行目录在 `…\briefdesk\Cache\benchmark\runs\`（可用 `BRIEFDESK_DATA_DIR` / `BRIEFDESK_CACHE_DIR` /`BRIEFDESK_SETTINGS_FILE` 重定向，这三个变量只读进程环境变量）。
- **旧版本的数据不会被自动发现或迁移**：默认库路径与配置文件位置都变了，旧位置的文件保持原样、不再被读取；本版本不提供旧库的迁移、导入或兼容转换指引。
- 导出与备份：`GET /api/export/items`（CSV）、`GET /api/backup`（SQLite 备份下载）、`POST /api/restore`（上传恢复，重启生效）。恢复时原库会保留为 `{DB_PATH}.pre-restore`（只留最近一代，含隐私数据，已在 `.gitignore` 中排除），替换失败会自动回滚并保留待恢复文件。

### 数据与配置文件位置

运行期可写的东西都在平台用户目录，安装目录只放代码与只读资源：

- 内容：SQLite 数据库 — 位置（Windows）：`%LOCALAPPDATA%\briefdesk\data\briefdesk.sqlite`；覆盖入口：`DB_PATH`
- 内容：UI 暂存配置 — 位置（Windows）：`%LOCALAPPDATA%\briefdesk\settings.env`；覆盖入口：`BRIEFDESK_SETTINGS_FILE`
- 内容：基准用例与报告 — 位置（Windows）：`%LOCALAPPDATA%\briefdesk\benchmark\{cases,reports}\`；覆盖入口：`BRIEFDESK_DATA_DIR`
- 内容：基准运行目录 — 位置（Windows）：`%LOCALAPPDATA%\briefdesk\Cache\benchmark\runs\`；覆盖入口：`BRIEFDESK_CACHE_DIR`
- 内容：本地运行指标（快照与余弦-真值明细） — 位置（Windows）：`%LOCALAPPDATA%\briefdesk\telemetry\`；覆盖入口：`BRIEFDESK_DATA_DIR`

`BRIEFDESK_DATA_DIR` / `BRIEFDESK_CACHE_DIR` / `BRIEFDESK_SETTINGS_FILE` **只读进程环境变量**
（写进 `.env` 无效）；macOS / Linux 走各自平台约定目录（platformdirs）。

> **旧数据边界**：本版本默认路径与旧版不同，**不会自动发现、复制或迁移旧库**，也不提供旧库的迁移、
> 导入或兼容转换指引；`DB_PATH` 只是通用的数据库文件覆盖入口，旧 schema、WAL 状态与兼容性不在支持范围。
> 配置文件（`settings.env`）的位置同样变化：旧位置的文件不再被读取，其中的插件开关与暂存项回到默认值。
