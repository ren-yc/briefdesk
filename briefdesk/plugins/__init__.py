"""内置插件实现层 — 与核心同发行包，经 pyproject 的 entry points 注册。

依赖方向（由 tests/test_no_core_imports_plugins.py 强制）：
本包可 import 核心与 briefdesk/plugin/*；核心不得静态 import 本包。

新增消息源：在本包建 <name>/ 子包（沿用 client/config/sse/poller/normalize/
runtime 分层，实现 SourceRuntime），加 plugin.py 装配类（显式继承
SourcePlugin，setup 里 ctx.register_source(runtime)），并在 pyproject
声明 [project.entry-points."briefdesk.plugins"] 的 <name> 入口指向模块内
`plugin` 实例。
"""
