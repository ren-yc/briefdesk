## 改动说明

<!-- 概括做了什么、为什么。PR 标题请用 Conventional Commits 格式（type/scope 英文，subject 中文），
     如 `feat(ai): 支持通过 AI_REASONING_EFFORT 选择推理强度`。关联 issue 请写 `Fixes #123`。
     标题与描述都不要夹带审查报告条目号、计划产物编号或「第 N 批」流水号——仓库读者无法据此回查。 -->

## 改动类型

- [ ] 代码行为变化（feat / fix / refactor）
- [ ] 测试补充或更新（test）
- [ ] 文档（docs / usage）

## 质量门禁（提交前本地全部通过，与 CI 及 AGENTS.md 对齐）

- [ ] `python -m ruff check briefdesk/ tests/`
- [ ] `python -m mypy briefdesk/ tests/`
- [ ] `python -m pytest tests/`
- [ ] `git diff --check`（无空白错误 / 冲突标记）
- [ ] `git diff --check 4b825dc642cb6eb9a060e54bf8d69288fbee4904..HEAD`（空树口径：覆盖全部跟踪文件）
- [ ] `python -m build --wheel` 与 `python -m twine check dist/*`（构建前清空 `dist/`）
- [ ] `python scripts/sdist_check.py`（sdist 成员与 wheel 运行时资源一一对应）
- [ ] 敏感信息自查：不含真实密钥、Token、聊天记录、手机号等 PII
- [ ] 注释 / 文档 / 提交信息中没有编号引用（`python scripts/forbidden_refs.py --tree` 无新增命中）

## 测试

<!-- 新增/更新了哪些测试、覆盖了什么；纯文档改动写「不适用」。 -->

## 文档同步

<!-- 涉及插件/管道阶段槽位、DB schema、server 路由、环境变量、模块契约时，
     必须回写 docs/architecture.md 对应小节（见 AGENTS.md 同步义务）。 -->

- [ ] 不涉及，或已回写 `docs/architecture.md`

## 自检

- [ ] `git status --short` 中没有 tmp_*、调试脚本、缓存、数据库、本地 env 文件
- [ ] 只包含与本 PR 相关的改动
