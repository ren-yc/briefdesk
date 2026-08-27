"""Web 插件注入点（server 子包）：路由/静态资源挂载与装配摘要。

- `/api/plugins`：插件发现/装配摘要（main 经 set_plugins_info_callback 注入）
- `/plugin-assets/{name}/{path}`：插件静态资源（浏览器直连）
- `include_plugin_router`：把插件路由展开插到 SPA mount 之前

从原 server.py 拆出（P5 子包化）：本模块导入即向 `briefdesk.server.app`
注册插件相关路由。
"""

from collections.abc import Callable
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from starlette.routing import Mount

from briefdesk.server.app import app
from briefdesk.server.media import _is_safe_media_path

_plugins_info_callback: Callable[[], list[dict]] | None = None
_settings_schema_callback: Callable[[], list[dict]] | None = None


def set_plugins_info_callback(cb: Callable[[], list[dict]] | None) -> None:
    """注入插件装配摘要回调（main 注册 manager.infos）。"""
    global _plugins_info_callback
    _plugins_info_callback = cb


def set_settings_schema_callback(cb: Callable[[], list[dict]] | None) -> None:
    """注入启用插件设置 schema 回调（main 注册 manager.settings_schema）。"""
    global _settings_schema_callback
    _settings_schema_callback = cb


def get_plugins_info() -> list[dict]:
    """读取插件装配摘要（未注入回调时返回空列表）。"""
    cb = _plugins_info_callback
    return cb() if cb is not None else []


def get_settings_schema() -> list[dict]:
    """读取启用插件设置 schema（未注入时返回空列表）。"""
    cb = _settings_schema_callback
    return cb() if cb is not None else []


def has_settings_schema_callback() -> bool:
    """返回是否已接入插件管理器的设置 schema 回调。"""
    return _settings_schema_callback is not None


@app.get("/api/plugins")
async def api_plugins():
    """插件发现/装配摘要（名称/版本/状态/原因），前端据此渲染插件区。

    响应带 no-store：装配状态只反映当前进程，禁止浏览器陈旧快照
    （插件顺序/状态变化直接决定前端加载行为）。
    """

    response = JSONResponse({"plugins": get_plugins_info()})
    response.headers["Cache-Control"] = "no-store"
    return response


_plugin_assets: dict[str, str] = {}


def register_plugin_assets(name: str, directory: str) -> None:
    """注册插件静态资源目录（服务路径 /plugin-assets/<name>/）。"""
    _plugin_assets[name] = directory


_included_router_ids: set[int] = set()


def include_plugin_router(router: APIRouter) -> None:
    """挂载 Web 插件路由：展开 APIRoute 并插到静态 SPA mount 之前。

    新版 Starlette 的 include_router 生成惰性 _IncludedRouter（path 为空、
    挂在 routes 尾部），会被 SPA 兜底 mount 截胡——这里直接展开路由并
    前移到 mount 之前（main 装配与测试共用）。
    同一 router 重复调用幂等跳过：按 id() 记录（APIRouter 定义了 __eq__
    不可哈希、无法用 WeakSet；router 均为插件模块级长生命周期实例）。
    """
    key = id(router)
    if key in _included_router_ids:
        return
    moved = list(router.routes)
    idx = len(app.routes)
    for i, r in enumerate(app.routes):
        if isinstance(r, Mount):
            idx = i
            break
    app.routes[idx:idx] = moved
    _included_router_ids.add(key)


@app.get("/plugin-assets/{name}/{path:path}")
async def plugin_assets(name: str, path: str):
    """服务插件静态资源（浏览器直连；目录穿越/非法路径一律 404）。

    404 返回纯文本而非 JSON：本端点服务 <link>/<script> 等资源请求，
    JSON 404 会触发浏览器严格 MIME 检查告警（application/json 不是
    可用的样式/脚本类型）。
    """
    directory = _plugin_assets.get(name)
    if directory is None or not _is_safe_media_path(path):
        return PlainTextResponse("Asset not found", status_code=404)
    root = Path(directory).resolve()
    file = (root / path).resolve()
    if file != root and root not in file.parents:
        return PlainTextResponse("Asset not found", status_code=404)
    if not file.is_file():
        return PlainTextResponse("Asset not found", status_code=404)
    return _no_cache_file(file)


def _no_cache_file(file):
    """插件资源随插随改：响应带 no-cache，浏览器每次向服务端校验证书。"""

    response = FileResponse(file)
    response.headers["Cache-Control"] = "no-cache"
    return response
