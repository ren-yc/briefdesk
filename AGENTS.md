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
# 范围级空白检查（空树口径，与 CI 一致；提交后仍可跑）
git diff --check 4b825dc642cb6eb9a060e54bf8d69288fbee4904..HEAD

# 可选：安装 pre-commit 密钥扫描钩子（staged 新增内容自动扫描，推荐）
powershell -ExecutionPolicy Bypass -File scripts/install-hooks.ps1
```

## AI 协作规范（所有开发助手必须遵守）

### 提交前质量门禁

- Lint: `python -m ruff check briefdesk/ tests/`
- 类型检查: `python -m mypy briefdesk/ tests/`（tests/ 为签名级检查；函数体深检因测试桩惯用法噪音大暂缓，配置理由见 pyproject `[tool.mypy]` 注释。CI 中 mypy 仅查 `briefdesk/` 包级——tests/ 的签名级检查由本地门禁覆盖，两处口径差异为有意为之）
- 测试: `python -m pytest tests/`
- 空白/冲突检查: `git diff --check`
- 范围级空白检查（对齐 CI 的空树口径，覆盖全部跟踪文件；上一条只查工作区，
  对已入库的空白问题失明——rag/config.py 末尾空行曾因此逃逸到 CI 才拦下）:
  `git diff --check 4b825dc642cb6eb9a060e54bf8d69288fbee4904..HEAD`
- 新增功能必须补充或更新对应测试
- 不要为了“让当前任务快速完成”而跳过上述任何一步；若门禁失败，必须先修复再提交

### 依赖锁文件（`requirements-dev.txt`）

- 该文件是 `pyproject.toml` 的 `[dev,ocr]` **依赖闭包**，由 `pip-compile` 生成，**不是** `pip freeze` 的整环境快照。重新生成用文件头部记录的那条命令。
- 不用 `pip freeze`：整环境快照会把项目从不 import 的包一并钉死（曾因此钉入 yank 版本的 `polars` 与整套 ML 栈），任何无关包的 yank 或平台轮子缺失都会弄红 CI。
- 升级依赖：`pip-compile --upgrade`（全量）或 `-P <包名>`（单包），随后必须在本地重跑全部门禁；不加 `--upgrade` 时既有 pin 会被复用，仅做闭包收敛。
- 该文件只服务 CI 的可复现安装，不参与任何测试断言；修改后应在干净虚拟环境中实测 `pip install -r requirements-dev.txt` + `pip install -e . --no-deps` 后跑一遍 pytest，确认闭包足够。
- 锁文件应以 **requires-python 下限（3.12）** 解析生成：CI 矩阵含 3.12/3.13/3.14 安装同一份锁文件，用高版本解析可能引入 `Requires-Python` 排除低版本的 pin。当前文件由 3.14 生成、实测三版本可装；下次重生成时改用 3.12 环境。

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

### 注释、文档与提交信息规范（禁止编号引用）

- **禁止编号引用**：代码注释、docstring、测试的文档串、仓库文档与提交信息中，不得写入指向仓库之外一次性材料的条目号。典型形态：
  - 审查报告 / 计划的条目码（形如「字母码 + 数字」，或「中文方括号 + 序号 + 条目码」）；
  - 用「复核 / 审查报告 / 审计 / 排期」等词做归因、后面跟一个编号；
  - 计划 / 会话产物路径（本地计划文件名、会话产物目录名）；
  - 流水号批次（「第 N 批」之类）。
- **为什么**：这些编号指向仓库外的审查报告或计划文件，仓库读者无法据此还原上下文，报告改版后编号还会失效。注释要说明**为什么这样做**与**失败模式**；提交信息要说明**行为变化**。
- **替代写法**：把编号换成「原因 + 失败模式 + 仓库内的回归位置」。

  ```text
  反例：复核 <字母码><数字>：atomic_transaction 必须捕 CancelledError
  正例：CancelledError 是 BaseException 子类必须显式捕获——否则 conn.commit() 永不执行，
        连接带未提交事务被归还（回归见 tests/test_db.py 的 atomic_transaction 用例）
  ```

- **豁免**（不视为编号引用）：可跟踪的 issue / PR 编号（如 `Fixes #123`，便于外部读者回查）、编码名与标准编号（UTF-8、RFC 5987）、静态检查码（`noqa: F401` 一类）、依赖版本号（小写 `v1.2.3`）、控制字符名（C0 / C1）、领域指标名（如 F 值类指标）、少量固定技术缩写（ES6、MD5、延迟分位等，名单见 `scripts/forbidden_refs.py` 的 `_EXEMPT`）。豁免是**剥离片段后再扫**：同一行夹带的其它编号照常判定。JS 代码行末尾的 `//` 注释与整行注释同口径。提交信息只扫真正入库的部分（`#` 注释行与 `git commit -v` 的 diff 不算）。
- **例外**：确实需要在注释里保留某个编号时，在同一行写 `allow-plan-ref` 并说明理由。
- **扫描面**（决定「漏写会不会被拦」，改动本节时须同步 `iter_segments`）：
  - **在面内**：`.py .js .mjs .md .yml .yaml .toml .ps1 .sh` 的注释与文档正文，以及提交信息（subject + body，`#` 注释行与 scissors 之后的 diff 不算）。JS 的行内尾注释与整行注释同口径。
  - **不在面内**：无此类片段的文件（`.html .css .svg .json .example` 等，含 `ui/index.html` 的 HTML 注释与 `ui/style.css` 的 `/* */`）——扫描器对它们返回空片段。往这些文件写注释时不享受门禁兜底，需人工复核。
  - `_EXEMPT` 白名单按**实际命中**增补（当前含少量仓库内暂未出现的防御项）；新增条目要写明它为什么与条目码同形却不是编号。
- **工具与门禁**：
  - 本地提交：`scripts/install-hooks.ps1` 安装的 pre-commit（密钥 + 编号）与 commit-msg（提交信息）钩子；
  - 命令行：`python scripts/forbidden_refs.py`（扫描 staged）、`--ref origin/master`（扫描差异）、`--tree`（全量）、`--message-file <路径>`（提交信息）。退出码：0 = 无命中、1 = 命中、**2 = 扫描未执行（git 取差异失败，拒绝放行——空 diff 不等于干净）**；
  - 门禁：`tests/test_no_plan_refs.py` 随 pytest 一起跑，对全库注释 / 文档 **0 容忍**（存量已清理完毕），并覆盖本地领先 `origin/master` 的提交信息（无基线时 skip）；CI 另有独立的提交信息扫描 step，覆盖直接推 master 的路径。

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
- [ ] `git diff --check 4b825dc642cb6eb9a060e54bf8d69288fbee4904..HEAD` 通过（范围级，见质量门禁）
- [ ] `git status --short` 中没有临时文件、缓存、数据库、本地 env 文件
- [ ] `git diff --cached` 中没有真实密钥、Token、聊天记录、手机号等敏感信息
- [ ] 只提交与任务相关的文件，没有 `tmp_*` / 调试脚本 / 无关文件
- [ ] 新增的注释 / 文档 / 提交信息中没有编号引用（审查报告条目号、计划产物编号、流水号批次），或已按规范改写

### 完成后的简要 Review 与 Commit Message

- 完成修改并通过质量门禁后，必须对本次改动做一轮简要 review：检查改动是否最小、是否引入无关文件、是否与源码/文档一致、是否遗漏测试。
- Review 结束后，必须向用户输出一条推荐的、格式合理的 commit message，使用 Conventional Commits 格式（type 和 scope 保持英文，subject 使用中文），例如：
  ```text
  feat(ai): 支持通过 AI_REASONING_EFFORT 选择推理强度
  ```
  其它示例：
  ```text
  fix(weflow-legacy): 修复空 token 导致请求头非法的问题
  refactor(db): 移除旧数据库兼容迁移逻辑
  docs(agents): 更新协作规范中的 commit message 要求
  ```
- commit message 应概括改动文件、行为变化与测试/文档更新；不要写入真实密钥、Token 或敏感信息。
- commit message（subject 与 body）不得包含编号引用与流水号批次（详见上文「注释、文档与提交信息规范」）；用行为变化描述代替。

## 架构指引

简报台是本地网页应用：可插拔消息源采集群聊消息，经统一过滤与阶段化管道（OCR 增强 → AI 分类 → 语义去重 → 同话题合并）写入 SQLite，由 FastAPI 经 SSE 实时推送到原生 JS 前端。

```text
消息源插件(weflow :5033 / weflow-legacy :5031 / qqflow :5032) → normalize 归一化
→ pipeline 入口统一过滤 → enrich(OCR) → classify(AI) → dedup(判重/入库)
→ post_insert(合并) → db(SQLite) → realtime(pub/sub) → server(FastAPI :3000)
→ ui/ SPA（SSE 实时刷新）
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
