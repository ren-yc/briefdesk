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
pip install ruff && python -m ruff check briefdesk/ tests/

# Type check (mypy)
pip install mypy && python -m mypy briefdesk/ tests/

# Test (pytest, tests/ 目录)
python -m pytest tests/

# 提交前完整检查
git diff --check
# 范围级空白检查（空树口径，与 CI 一致；提交后仍可跑）
git diff --check 4b825dc642cb6eb9a060e54bf8d69288fbee4904..HEAD

# 可选：安装本地 git 钩子（推荐）——pre-commit 扫 staged 新增内容（密钥 + 编号引用），
# commit-msg 检查提交信息；见 scripts/install-hooks.ps1 头部说明
powershell -ExecutionPolicy Bypass -File scripts/install-hooks.ps1
```

## AI 协作规范（所有开发助手必须遵守）

### 提交前质量门禁

- Lint: `python -m ruff check briefdesk/ tests/`
- 类型检查: `python -m mypy briefdesk/ tests/`（tests/ 为签名级检查；函数体深检因测试桩惯用法噪音大暂缓，配置理由见 pyproject `[tool.mypy]` 注释。CI 中 mypy 仅查 `briefdesk/` 包级——tests/ 的签名级检查由本地门禁覆盖，两处口径差异为有意为之）
- 测试: `python -m pytest tests/`
- 空白/冲突检查: `git diff --check`
- 构建与元数据: `python -m build --wheel` + `python -m twine check dist/*`
  （构建前先清空 `dist/` **与 `build/`**，避免校验到历史 artifact；`build/lib` 是 setuptools
  的增量暂存目录，只清 `dist/` 时它会用**上一次构建的文件副本**填新 wheel——实测把
  `vendor/qqflow_sdk/__init__.py` 改名后门禁仍绿，清掉 `build/` 才按预期变红；完整 wheel
  冒烟——临时 venv 安装、真进程启动、写入隔离——在独立 CI job 跑，本地不跑）
- sdist 产出与断言: `python scripts/sdist_check.py`
  （构建到唯一临时目录，断言 sdist 成员集合与 wheel 的运行时资源一一对应、从 sdist
  重建的 wheel 与直接构建的 wheel 数据成员相同、twine check 通过；不进 pytest——断言
  依赖真实构建。脚本自带构建与 twine check，不要再单独跑 `python -m build --sdist`）
- 版本号与试发: `pyproject.toml` 的 `version` 是静态值，索引不允许覆盖同版本——试发用
  `python scripts/release_check.py`（查 TestPyPI 算下一个空闲 `0.1.0.devN` → 构建期间瞬态写入
  版本 → 逐字节还原 → `twine check`），加 `--upload` 才真的上传；正式发布才用 `0.1.0`。
  端到端验证两条安装路径都要跑：`python scripts/wheel_smoke.py --install-from testpypi --version 0.1.0.devN`
  （索引上的 wheel）与同一条命令加 `--from-sdist`（`--no-binary` 强制 sdist + 本地构建，构建后端
  先从 PyPI 预装）——pip 对纯 Python 包默认只选 wheel，不强制就永远测不到源码分发那条路，而
  「sdist 少带运行时资源」只在它上面暴露。正式索引把 `testpypi` 换成 `pypi` 即可。
  凭据由 twine 从系统钥匙串取（`python -m keyring set https://test.pypi.org/legacy/ __token__`），
  不进环境变量、不入仓库、不写 `.env`
- **装 TestPyPI 的包不要用 `--extra-index-url pypi.org` 一把梭**：pip 跨索引取最高版本，
  实测被 TestPyPI 上别人试传的 `fastapi 1.0`（正式 PyPI 是 0.142.x）影子化、依赖构建失败。
  冒烟因此改成「依赖走 PyPI、本包走 TestPyPI」两段式，文档与 README/USAGE 同口径。
  本机的 `release_check.py --upload` 只是试发便利入口；正式发布走下面的 CI workflow
- **发布走 CI 的 `publish.yml`**（`workflow_dispatch` + environment 审批 + OIDC 可信发布，
  本机不持有任何上传凭据）：`publish` job 在 **ubuntu** 上跑 `release_check.py`（版本唯一性预检、
  构建、产物成员断言）再用 `pypa/gh-action-pypi-publish` 上传（带 PEP 740 构建证明）——上传
  action 是 Docker 容器动作，只在 Linux runner 上能跑；`verify` job 在 **windows** 上按
  「索引 + 版本」跑两条安装路径的冒烟（主平台 + 进程清理契约），因此不需要在 job 之间传产物。
  冒烟取索引元数据时会先等该版本挂出来（上传到可见有秒级延迟，立刻失败会把「索引延迟」报成
  「版本不存在」）。
  只允许从 `master` 触发；正式索引的版本必须等于 `pyproject.toml` 的静态值（脚本强校验），
  试发版本（`0.1.0.devN` / `0.1.0rc1`）只在构建期写入并逐字节还原，仓库里永不落地。
  一次性配置：GitHub environments `testpypi` / `pypi`（后者配 Required reviewers），
  两个索引各登记 trusted publisher（Owner `ren-yc`、Repository `briefdesk`、Workflow `publish.yml`）
- **写入路径覆盖分工**（只发 GET 证明不了写入隔离，两侧都要看：冒烟在 CI 的
  `wheel-smoke` job，隔离回归在 `isolation-smoke` job）：

  | 写入路径 | 覆盖位置 |
  |---|---|
  | 配置保存（`PUT /api/settings/env`，带与端口一致的同源 `Origin`） | `scripts/wheel_smoke.py` |
  | Benchmark 用例导出（`store.export_fromweb`，辅助进程直调 + 虚构用例） | 同上 |
  | 基准执行与报告（`runner --ai-provider stub --source file`） | 同上 |
  | supervisor 运行目录 / CLI `--out` 默认目录 | 同上（各自单独断言） |
  | 运行中失败也能收尾（Web 基准故意用不可达端点 + 缓存 run 目录） | 同上 |
  | 命令与所属进程全部回收（Job / 进程组所有权，含孙进程） | 同上（`scripts/smoke_process.py`；协议单测在 `tests/test_smoke_process.py`） |
  | 基准终态按 `run_id` 核对（HTTP 失败不得当作「已收尾」） | 同上 |
  | 子进程对生产库的隔离（真 spawn + 逐表内容摘要不变） | `tests/test_benchmark_isolation.py` |

  冒烟跑在全新 venv 与空库上；**本机真实数据下的写入行为不在 CI 覆盖内**。
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
- 锁文件应以 **requires-python 下限（3.12）** 解析生成：CI 矩阵含 3.12/3.13/3.14 安装同一份锁文件，用高版本解析可能引入 `Requires-Python` 排除低版本的 pin（失败模式：低版本 CI 解析锁文件即失败）。当前文件即由 3.12 生成——以文件头部那行 `pip-compile with Python …` 为准，重生成时不要换高版本解释器。

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

### 文本文件编码

- 仓库内文本类文件一律 **UTF-8**、**不带 BOM**；违约由 `tests/test_text_encoding.py` 守卫，pytest 直接红
- 「文本类」＝ 跟踪文件里 git **未判为二进制**的那些（`git ls-files --eol` 工作区侧 `w/` ≠ `-text`，含空文件与工作区缺失）；不按后缀白名单，新增扩展名自动覆盖
- 已知边界（有意不覆盖）：git 判为二进制的文件一律豁免——含 NUL 的编码（UTF-16 等），以及内容为二进制却挂文本后缀的文件（如把 PNG 存成 `.md`）
- 守卫只读工作区：工作区干净而索引里是坏内容的情形，由 CI 的全新检出兜底
- 编辑器侧约定见根目录 `.editorconfig`（`charset = utf-8`）

### 注释、文档与提交信息规范（禁止编号引用）

- **禁止编号引用**：代码注释、docstring、测试的文档串、仓库文档与提交信息中，不得写入指向仓库之外一次性材料的条目号。典型形态：
  - 审查报告 / 计划的条目码（形如「字母码 + 数字」或「数字 + 字母码」，或「中文方括号 + 序号 + 条目码」；同一条目拆子项时的「字母码 + 数字 + 小写字母」也算）。数字在前的形态只在「见 / 决策 / 条目 / 序号 / 表」一类指针词邻近时判定——孤立的「数字+字母」与 3D、5G、2FA 这类技术名词同形，按形状硬判会把正常注释一起拦下；
  - 用「复核 / 审查报告 / 审计 / 排期 / 决策」等词做归因、后面跟一个编号（含带圈数字）；后面跟中文量词（「复核 3 处」「决策 3 项」）属于自然行文，不算；
  - 裸归因短语（「审查」紧接「修复 / 回归」且处在标签位——后随冒号、括号、编号或行尾）；
  - 章节指针（`§N` / `§N.M`——仓库内没有带章节号的文档，出现即指向仓外）；
  - 计划 / 会话产物路径（本地计划文件名、会话产物目录名）；
  - **点名了仓库里不存在的文档**（注释写「见某某文档」，而仓库里根本没有这个文件）——与编号同性质：读者按名字找不到，就只能猜；
  - 流水号批次（「第 N 批」之类）。
- **为什么**：这些编号指向仓库外的审查报告或计划文件，仓库读者无法据此还原上下文，报告改版后编号还会失效。注释要说明**为什么这样做**与**失败模式**；提交信息要说明**行为变化**。
- **替代写法**：把编号换成「原因 + 失败模式 + 仓库内的回归位置」。

  ```text
  反例：复核 <字母码><数字>：atomic_transaction 必须捕 CancelledError
  正例：CancelledError 是 BaseException 子类必须显式捕获——否则 conn.commit() 永不执行，
        连接带未提交事务被归还（回归见 tests/test_db.py 的 atomic_transaction 用例）
  ```

- **豁免**（不视为编号引用）：可跟踪的 issue / PR 编号（如 `Fixes #123`，便于外部读者回查）、编码名与标准编号（UTF-8、RFC 5987）、公开标准的章节号（如 RFC 5987 的 3.2 节——与标准编号同属可回查引用，但孤立章节指针不豁免）、静态检查码（`noqa: F401` 一类）、依赖版本号（小写 `v1.2.3`）、控制字符名（C0 / C1）、领域指标名（如 F 值类指标）、与条目码同形但后缀字母表封闭的量纲（容量单位 MB/GB、千次记法 K、HTTP 状态类通配 1XX）、十六进制颜色字面量（要求含 A-F，纯数字的 `#` 编号照常判定）、少量固定技术缩写（ES6、MD5、延迟分位等，名单见 `scripts/forbidden_refs.py` 的 `_EXEMPT`）。豁免是**剥离片段后再扫**：同一行夹带的其它编号照常判定。JS 代码行末尾的 `//` 注释与整行注释同口径。提交信息只扫真正入库的部分（`#` 注释行与 `git commit -v` 的 diff 不算）。
- **例外**：确实需要在注释里保留某个编号时，在同一行写 `allow-plan-ref` 并说明理由。
- **扫描面**（决定「漏写会不会被拦」，改动本节时须同步 `iter_segments`）：
  - **在面内**：`.py .js .mjs .md .yml .yaml .toml .ps1 .sh .css .html .example` 的注释与文档正文，以及提交信息（subject + body，`#` 注释行与 scissors 之后的 diff 不算）。JS 走逐字符状态机，整行注释、行尾注释、块注释一并收，字符串与模板串里的 `//` `/*` 不算注释起点；CSS 的 `/* */` 与 HTML 的 `<!-- -->` 块注释同口径。Python 的采集面是**所有**字符串表达式语句——不止各节点首位的 docstring，类体中部的说明串同样入扫。`.example` 模板（`.env.example`）按 `#` 行注释扫。
  - **不在面内**：无此类片段的文件（`.svg .json` 等；`.svg` 全是上游图标库的逐字节副本，有意不纳面）——扫描器对它们返回空片段。往这些文件写注释时不享受门禁兜底，需人工复核。
  - **「点名的 `.md` 必须存在」只在全量扫描生效**（`--tree` 与 pytest 守卫）：增量模式手里没有稳定的树快照，当场判「不存在」会误伤同时新增的文件。写注释时若要指向仓内文档，用真实存在的路径。
  - `_EXEMPT` 白名单按**实际命中**增补（当前含少量仓库内暂未出现的防御项）；新增条目要写明它为什么与条目码同形却不是编号。
- **工具与门禁**：
  - 本地提交：`scripts/install-hooks.ps1` 安装的 pre-commit（密钥 + 编号）与 commit-msg（提交信息）钩子；
  - 命令行：`python scripts/forbidden_refs.py`（扫描 staged）、`--ref origin/master`（扫描差异）、`--tree`（全量）、`--message-file <路径>`（提交信息）。退出码：0 = 无命中、1 = 命中、**2 = 扫描未执行（git 取差异失败，拒绝放行——空 diff 不等于干净）**；
  - CI 侧统一入口：`python scripts/ci_scan.py`（读 `GITHUB_EVENT_NAME` / `GITHUB_EVENT_PATH`，范围只认事件载荷里的固定 SHA：PR 用 `base.sha`/`head.sha` 的 merge-base 到事件 head，push 用 `before`/`after`）。非零 `before` 本地缺失时先精确 fetch 该 SHA，仍取不到返回 2 阻断；全零 `before` 是首次推送的已知空边界（空树 → after，提交集合取全部祖先）；`before == after` 是合法空范围；删除分支事件明确无操作。
  - 两个扫描器另有成对的双端参数：`python scripts/secret_scan.py --base <commit|空树> --head <commit>`、`python scripts/forbidden_refs.py --base ... --head ...`（直接比较两端树，不构造三点范围）、`python scripts/forbidden_refs.py --commits [--base ...] --head ...`（提交信息集合 = reachable 差集；无 `--base` 时取 head 全部祖先）。任一环节读取失败一律退出 2。
  - 门禁：`tests/test_no_plan_refs.py` 随 pytest 一起跑，对全库注释 / 文档 **0 容忍**（存量已清理完毕），并覆盖本地领先 `origin/master` 的提交信息（无基线时 skip）；`tests/test_ci_scan.py` 用临时仓库构造真实 DAG 覆盖事件范围与退出码承诺；CI 的扫描步骤已合并为一次 `ci_scan` 调用，覆盖直接推 master 的路径。

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
- [ ] `python -m build --wheel` 与 `python -m twine check dist/*` 通过（先清空 `dist/` 与 `build/`，见质量门禁）
- [ ] `python scripts/sdist_check.py` 通过（sdist 成员与 wheel 运行时资源一一对应）
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

简报台是本地网页应用：可插拔消息源采集群聊消息，经统一过滤与阶段化管道（OCR 增强 → AI 分类 → 语义去重 → 同话题合并）写入 SQLite，由 FastAPI 经 SSE 实时推送到原生 JS 前端。**数据流、模块职责与槽位契约一律以架构文档为准，此处不复制**——本文件与架构文档各留一份就会出现「改了一处、另一处仍是旧口径」的漂移。

- **完整架构文档**：[docs/architecture.md](docs/architecture.md)——模块职责、插件框架、数据库 schema、server 路由清单、配置项表、设计要点与陷阱。涉及架构的任务先读它。
- **同步更新义务**（元维护规则）：出现下列改动时，必须回写 `docs/architecture.md` 对应小节：
  - 新增/删除插件或管道阶段槽位；
  - DB schema 变更（建表/列/约束）；
  - server 路由增删或中间件行为变化；
  - 新增环境变量或默认值变化；
  - 模块职责或跨模块契约变化；
  - 新发现的跨模块陷阱/gotcha。
- **边界**：本文件只承载协作规则、命令与门禁；架构细节一律写在 `docs/architecture.md`，不要回流到本文件。
