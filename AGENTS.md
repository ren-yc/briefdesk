# AGENTS.md

This file provides guidance to AI coding agents when working with code in this repository.

## Commands

```bash
# Install dependencies (editable mode)
pip install -e .

# Run the application
python main.py          # root shim → briefdesk/main.py
# or
python -m briefdesk           # via __main__.py
# or
briefdesk                  # pyproject console script

# Opens at http://localhost:3000

# Lint (ruff)
pip install ruff && ruff check briefdesk/ tests/

# Type check (mypy)
pip install mypy && mypy briefdesk/ tests/

# Test (pytest, tests/ 目录)
python -m pytest tests/

# 提交前完整检查
git diff --check

# 可选：安装 pre-commit 密钥扫描钩子（staged 新增内容自动扫描，推荐）
powershell -ExecutionPolicy Bypass -File scripts/install-hooks.ps1
```

## AI 协作规范（所有开发助手必须遵守）

### 提交前质量门禁

- Lint: `python -m ruff check briefdesk/ tests/`
- 类型检查: `python -m mypy briefdesk/ tests/`（tests/ 为签名级检查；函数体深检因测试桩惯用法噪音大暂缓，配置理由见 pyproject `[tool.mypy]` 注释）
- 测试: `python -m pytest tests/`
- 空白/冲突检查: `git diff --check`
- 新增功能必须补充或更新对应测试
- 不要为了“让当前任务快速完成”而跳过上述任何一步；若门禁失败，必须先修复再提交

### 临时文件清理

- 禁止提交调试/临时文件：`tmp_*`、`*.tmp`、`*.bak`、`*_stub*.js`、`debug*.py`、`*.log` 等
- 提交前必须检查：
  ```bash
  git status --short
  git ls-files --others --exclude-standard
  ```
- 如果任务过程中创建了临时文件，必须在提交前删除；不要把根目录调试脚本带入 commit
- 本地生成物（`.mypy_cache/`、`.ruff_cache/`、`.pytest_cache/`、`__pycache__/`、`*.sqlite`、`.env.*.local`）不得出现在 `git status` 中
- 仓库中不应出现已跟踪的 `tmp_*` / `*_stub*.js` 等调试文件；发现时应随清理任务移除
- 协作者或其 agent 创建本地独立计划文件（如 `IMPLEMENTATION-PLAN-*.md`、`PLAN-*.md`、`TODO-*.md` 等）时，必须写入项目目录之外（如系统临时目录或用户主目录），禁止落入仓库工作区；仓库内发现的此类文件应删除，不得提交

### 隐私与敏感数据扫描

- 本项目会处理真实群聊消息，禁止把真实聊天内容、手机号、QQ/微信 ID、地址、Token、Key 写入 commit、测试、文档或示例
- 测试与示例必须使用虚构/脱敏数据
- 不要读取并提交 `.env`、`.env.*.local`、`*.sqlite` 或日志中的真实凭据
- 提交前执行敏感扫描：
  ```bash
  # 已暂存内容中的密钥/Token 形态
  git diff --cached | grep -nE '(sk-[A-Za-z0-9]{16,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----)' || true

  # 真实手机号等 PII（仅用于人工复核，不要把误报直接删除）
  git diff --cached | grep -nE '(^|[^0-9])(1[3-9][0-9]{9})([^0-9]|$)' || true
  ```
- 即使 `.env` 已被 gitignore，也不能在对话/文档/issue 中贴出真实 Key 值；确需演示时使用 `<your-api-key>` 占位
- 若扫描发现疑似真实数据，必须先脱敏再提交，不能直接 `git add .` 绕过

### 完成条件（提交前逐项确认）

- [ ] `python -m ruff check briefdesk/ tests/` 通过
- [ ] `python -m mypy briefdesk/ tests/` 通过
- [ ] `python -m pytest tests/` 通过
- [ ] `git diff --check` 通过
- [ ] `git status --short` 中没有临时文件、缓存、数据库、本地 env 文件
- [ ] `git diff --cached` 中没有真实密钥、Token、聊天记录、手机号等敏感信息
- [ ] 只提交与任务相关的文件，没有 `tmp_*` / 调试脚本 / 无关文件

### 完成后的简要 Review 与 Commit Message

- 完成修改并通过质量门禁后，必须对本次改动做一轮简要 review：检查改动是否最小、是否引入无关文件、是否与源码/文档一致、是否遗漏测试。
- Review 结束后，必须向用户输出一条推荐的、格式合理的 commit message，使用 Conventional Commits 格式（type 和 scope 保持英文，subject 使用中文），例如：
  ```text
  feat(ai): 支持通过 AI_DISABLE_THINKING 禁用思考模式
  ```
  其它示例：
  ```text
  fix(weflow-legacy): 修复空 token 导致请求头非法的问题
  refactor(db): 移除旧数据库兼容迁移逻辑
  docs(agents): 更新协作规范中的 commit message 要求
  ```
- commit message 应概括改动文件、行为变化与测试/文档更新；不要写入真实密钥、Token 或敏感信息。

## 架构指引

简报台是本地网页应用：可插拔消息源采集群聊消息，经统一过滤与阶段化管道（OCR 增强 → AI 分类 → 语义去重 → 同话题合并）写入 SQLite，由 FastAPI 经 SSE 实时推送到原生 JS 前端。

```text
消息源插件(weflow-legacy :5031 / qqflow :5032) → normalize 归一化 → pipeline 入口统一过滤
→ enrich(OCR) → classify(AI) → dedup(判重/入库) → post_insert(合并) → db(SQLite)
→ realtime(pub/sub) → server(FastAPI :3000) → ui/ SPA（SSE 实时刷新）
```

- **完整架构文档**：[docs/architecture.md](docs/architecture.md)——模块职责、插件框架、数据库 schema、server 路由清单、配置项表、设计要点与陷阱。涉及架构的任务先读它。
- **同步更新义务**（元维护规则）：出现下列改动时，必须回写 `docs/architecture.md` 对应小节：
  - 新增/删除插件或管道阶段槽位；
  - DB schema 变更（建表/列/约束）；
  - server 路由增删或中间件行为变化；
  - 新增环境变量或默认值变化；
  - 模块职责或跨模块契约变化；
  - 新发现的跨模块陷阱/gotcha。
- **边界**：本文件只承载协作规则、命令与门禁；架构细节一律写在 `docs/architecture.md`，不要回流到本文件。
