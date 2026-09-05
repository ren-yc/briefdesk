## 改动说明

<!-- 概括做了什么、为什么。PR 标题请用 Conventional Commits 格式（type/scope 英文，subject 中文），
     如 `feat(ai): 支持通过 AI_DISABLE_THINKING 禁用思考模式`。关联 issue 请写 `Fixes #123`。 -->

## 改动类型

- [ ] 代码行为变化（feat / fix / refactor）
- [ ] 测试补充或更新（test）
- [ ] 文档（docs / usage）

## 质量门禁（提交前本地全部通过，与 CI 及 AGENTS.md 对齐）

- [ ] `python -m ruff check briefdesk/ tests/`
- [ ] `python -m mypy briefdesk/ tests/`
- [ ] `python -m pytest tests/`
- [ ] `git diff --check`（无空白错误 / 冲突标记）
- [ ] 敏感信息自查：不含真实密钥、Token、聊天记录、手机号等 PII

## 测试

<!-- 新增/更新了哪些测试、覆盖了什么；纯文档改动写「不适用」。 -->

## 文档同步

<!-- 涉及插件/管道阶段槽位、DB schema、server 路由、环境变量、模块契约时，
     必须回写 docs/architecture.md 对应小节（见 AGENTS.md 同步义务）。 -->

- [ ] 不涉及，或已回写 `docs/architecture.md`

## 自检

- [ ] `git status --short` 中没有 tmp_*、调试脚本、缓存、数据库、本地 env 文件
- [ ] 只包含与本 PR 相关的改动
