# 简报台

> 本地运行的网页应用：通过可插拔消息源（weflow-server 或 WeFlow 监听微信群，qqflow-server 监听 QQ 群）获取群消息，AI 自动分类筛选有价值的信息并结构化展示。

## Highlights

简报台的核心能力集中在多消息源采集、四阶段处理链、可审核卡片与快速启动四件事。

- **多消息源可共存**：安装版 WeFlow、weflow-server 或 qqflow-server 任选其一，启动回填 + 增量轮询 + SSE 实时监听
- **一条处理链**：图片 OCR → AI 分类 → 语义去重 → 同话题合并，结果写入 SQLite 并经 SSE 推送到原生 JS 前端
- **可审核的卡片**：按类别 / 群聊 / 状态筛选，支持备忘录、忽略、原文上下文与同主体折叠
- **5 分钟可装可跑**：Python 3.12 + 任一消息源 + `AI_API_KEY`，启动后浏览器打开 `http://localhost:3000`

## 功能

下面十条是开箱可用的功能，机制细节与默认值统一收在使用指南。

- **消息获取**：启动回填 + 按会话增量轮询，并辅以 SSE 实时监听，多消息源可共存；会话启用或从未轮询时按 `BACKFILL_HOURS` 默认 24h 窗口回填一次，设为 -1 拉取全部历史（每次同步都会全量扫描，慎用），各会话独立水位，`POLL_OVERLAP_SECONDS` 控制重叠容错
- **AI 分类**：AI 自动将消息分类，内置 13 类、出厂默认启用 5 类（活动通知、社团招新、学术、交易、实习）；可在设置中自定义类别（名称/提示词/颜色/启停）
- **图片 OCR**：聊天图片自动识别文字并纳入分类与展示
- **视觉分类（可选）**：多模态模型下把 OCR 文本连同图片送入分类，端点不支持时自动降级纯文本重试
- **语义去重**：跨群转发的同一条信息自动合并并保留多来源标注
- **原文上下文**：展开卡片可查看原文及附近消息
- **人工审核**：卡片可加入备忘录 / 已忽略，侧边栏按状态查看
- **同主体折叠**：同一信息主体的卡片自动折叠成组
- **群聊筛选**：设置中自由选择监控哪些群聊
- **分类过滤**：设置中可选隐藏不需要的信息类型

> 功能细节、回填窗口、水位与重叠容错、类别默认值见 [USAGE.md](USAGE.md)。

## 快速开始

装好 Python 3.12 与任一消息源、填好 `AI_API_KEY` 后即可启动，浏览器打开 `http://localhost:3000`。

### 前置条件

- Python >= 3.12
- 任一消息源运行中：安装版 WeFlow（桌面客户端开启 HTTP API，默认 `:5031`，对应本仓 `weflow-legacy` 插件）、weflow-server（微信 4.x 无头服务，默认 `http://127.0.0.1:5033`，接口形态参考安装版 WeFlow）或 qqflow-server（QQ NT 版无头服务，默认 `http://127.0.0.1:5032`）
- AI API Key（环境变量 `AI_API_KEY`）

### 安装运行

```bash
cd briefdesk
# 安装（editable 便于改代码；普通安装 pip install . 同样可用——核心 SPA 与
# 插件前端随包分发，见 pyproject 的 [tool.setuptools.package-data]）。
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

#### 从索引安装（发布版）

不改代码时直接装发布版：

```bash
pip install briefdesk               # 可选图片 OCR：pip install "briefdesk[ocr]"
briefdesk                           # 或 python -m briefdesk
```

配置与数据目录口径同上（只有源码/editable 模式才读项目根 `.env`）。

## 配置

所有配置通过项目根目录的 `.env` 文件读取，不配置时使用默认值。**直接复制模板即可开始**：

1. 复制 `.env.example` 为 `.env`：

   ```bash
   # Linux / macOS
   cp .env.example .env
   # Windows
   copy .env.example .env
   ```

2. 编辑 `.env`，填入必填项 `AI_API_KEY`；消息源为 `weflow` 时需 `WEFLOW_API_TOKEN`/`WEFLOW_WXID`/`WEFLOW_DB_KEYS`（微信每库独立密钥的 JSON 映射，一份完整 JSON 即可，超长时由系统自动分片存储；密钥项走系统钥匙串而非 `.env`），为 `weflow-legacy` 时需 `WEFLOW_LEGACY_API_TOKEN`，为 `qqflow` 时需 `QQFLOW_API_TOKEN`/`QQFLOW_QQ`/`QQFLOW_KEY`（weflow/qqflow 缺失任一必填项 → 该插件自禁用）。消息源为可选插件，启用走 `PLUGINS` 显式列表（如 `PLUGINS=["weflow","qqflow"]`）或应用内「设置 → 插件」面板逐个开关，其余按需修改。
3. `AI_MODEL` 默认 `deepseek-v4-flash`：若你对接的 OpenAI 兼容服务没有该模型名，请改为实际模型名（如 `deepseek-chat`、`qwen-turbo` 等），否则首次分类会报模型不存在。

> 完整可调项、环境变量表与逐项注释见 [USAGE.md](USAGE.md) 与 [.env.example](.env.example)；
> 依赖清单以 [pyproject.toml](pyproject.toml) 为事实源，架构职责见 [docs/architecture.md](docs/architecture.md)。

## 插件化

本体只保留核心（存储、管道骨架、HTTP、状态总线），扩展功能以插件挂载：打包插件经 entry points 声明、开发期插件放 `PLUGIN_PATH` 目录，
`PluginManager` 负责发现、分层过滤、依赖拓扑排序与生命周期编排；核心插件恒装配，可选插件经 `PLUGINS` 显式列表或设置页「插件」面板启用，
可声明 `conflicts` 互斥；单插件失败只禁用该插件，`PLUGINS_REQUIRED` 名单内的失败则中止启动；核心永不 import `briefdesk.plugins.*`，由 `tests/test_no_core_imports_plugins.py` 守卫。

> 完整插件机制、槽位契约与前端扩展钩子见 [docs/architecture.md](docs/architecture.md)；第三方插件开发见 [第三方插件开发指南](docs/plugin-dev.md)。

## 架构

简报台采用「核心骨架 + 插件」架构：可插拔消息源（weflow / weflow-legacy / qqflow）产出归一化消息，经 pipeline 入口统一过滤后进入阶段插件流水线（OCR 增强 → AI 分类 → 语义去重 → 同话题合并），结果写入 SQLite，由 FastAPI 服务端经 SSE 实时推送到原生 JS 前端；本体只保留存储、管道骨架、HTTP 与状态总线等核心。

完整的数据流图、模块职责、插件框架、数据库 Schema、配置项与设计陷阱详见 [docs/architecture.md](docs/architecture.md)；日常使用（启动、配置、常见操作）详见 [USAGE.md](USAGE.md)；想给简报台写插件（消息源/阶段/Web 扩展）请阅读[第三方插件开发指南](docs/plugin-dev.md)。

## 技术栈

运行时是 Python + FastAPI，前端是无构建的原生 JS，数据落 SQLite。

- **运行时与 Web**：Python + uvicorn + FastAPI，前端为原生 HTML + CSS + JavaScript（无框架）
- **数据与 AI**：SQLite (aiosqlite 异步) + AI Chat API (openai SDK 兼容)，HTTP 客户端 httpx (异步)
- **消息源**：可插拔多源（weflow-server :5033 微信 4.x、WeFlow :5031、qqflow-server :5032）；消息源为可选插件，通过 `PLUGINS` 显式列表或设置页「插件」面板开关启用，JSON 数组格式
