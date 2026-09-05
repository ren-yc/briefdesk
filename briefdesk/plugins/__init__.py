"""内置插件实现层 — 与核心同发行包，经 pyproject 的 entry points 注册。

依赖方向（由 tests/test_no_core_imports_plugins.py 强制）：
本包可 import 核心与 briefdesk/plugin/*；核心不得静态 import 本包。

新增消息源：在本包建 <name>/ 子包（沿用 client/config/sse/poller/normalize/
runtime 分层，实现 SourceRuntime），加 plugin.py 装配类（显式继承
SourcePlugin，setup 里 ctx.register_source(runtime)），并在 pyproject
声明 [project.entry-points."briefdesk.plugins"] 的 <name> 入口指向模块内
`plugin` 实例。装配类须声明 `core = False`（消息源为可选插件，默认禁用，
经 PLUGINS 显式列名或设置页「插件」面板启用）与 `conflicts`（无互斥则
`()`；与其他插件互斥时对称声明，如 weflow ↔ weflow-legacy）。
"""
