# ruff: noqa: I001 — 子模块导入顺序即组装顺序（web_plugins 必须先于 static 的 SPA mount）
"""FastAPI HTTP 服务子包。组装顺序即下方导入顺序；子模块清单与完整路由见
docs/architecture.md「核心模块」。`import briefdesk.server as srv` 的既有
用法（main/tests）经 re-export 保持不变。
"""

from briefdesk.server.app import app as app

# 子模块导入顺序即组装顺序：
#   中间件 → Web 插件注入点（必须先于 static，否则被 SPA mount 兜底截胡）
#   → 核心路由 → 类别路由 → 启动配置路由 → 媒体代理 → SPA 静态托管（mount 恒在最后，
#   include_plugin_router 按首个 Mount 前插）。
from briefdesk.server import middleware  # noqa: F401 — 导入即注册中间件
from briefdesk.server import web_plugins  # noqa: F401 — 导入即注册插件路由
from briefdesk.server import routes_items  # noqa: F401 — 导入即注册核心路由
from briefdesk.server import routes_categories  # noqa: F401 — 导入即注册类别路由
from briefdesk.server import routes_settings_env  # noqa: F401 — 导入即注册启动配置路由
from briefdesk.server import media  # noqa: F401 — 导入即注册媒体代理
from briefdesk.server import static  # noqa: F401 — 导入即挂载 SPA

# ── Re-export（保持 `import briefdesk.server` 的既有引用面）──
from briefdesk.server.callbacks import set_refresh_sessions_callback as set_refresh_sessions_callback
from briefdesk.server.media import _is_safe_media_path as _is_safe_media_path
from briefdesk.server.middleware import _local_security_guard as _local_security_guard
from briefdesk.server.middleware import _same_origin as _same_origin
from briefdesk.server.routes_categories import _parse_flag as _parse_flag
from briefdesk.server.routes_items import _FILTER_NOW_RE as _FILTER_NOW_RE
from briefdesk.server.static import _SpaStaticFiles as _SpaStaticFiles
from briefdesk.server.static import _UI_DIR as _UI_DIR
from briefdesk.server.web_plugins import _plugin_assets as _plugin_assets
from briefdesk.server.web_plugins import include_plugin_router as include_plugin_router
from briefdesk.server.web_plugins import has_settings_schema_callback as has_settings_schema_callback
from briefdesk.server.web_plugins import register_plugin_assets as register_plugin_assets
from briefdesk.server.web_plugins import set_plugins_info_callback as set_plugins_info_callback
from briefdesk.server.web_plugins import set_settings_schema_callback as set_settings_schema_callback
