"""SPA 静态托管（server 子包）：/ 与 ui/ 静态文件 + SPA 兜底。

从原 server.py 拆出（P5 子包化）：本模块导入即向 `briefdesk.server.app`
注册 / 路由并挂载兜底 mount（必须位于插件路由之后——include_plugin_router
会把插件路由插到本 mount 之前）。
"""

import os

from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from briefdesk.server.app import app

_UI_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "ui")


@app.get("/")
async def index():
    return FileResponse(os.path.join(_UI_DIR, "index.html"))


# Mount static files (CSS, JS)
class _SpaStaticFiles(StaticFiles):
    """Serve ui/ static files, fall back to index.html for SPA routes."""

    async def get_response(self, path: str, scope) -> Response:
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException:
            # 必须捕 starlette 基类：StaticFiles raise 的是
            # starlette.exceptions.HTTPException，fastapi.HTTPException 是其
            # 子类，isinstance(父类实例, 子类) 恒 False——此前 except 永远
            # 不匹配，SPA fallback 实际从未生效（hash 路由掩盖了这点）
            # starlette>=1.3 在 Windows 上会给 mount 传入反斜杠形式的 path
            # （"api\x"），先归一化再判断，防止未知 /api/* 被回退成首页
            normalized = path.replace("\\", "/")
            # 未知 /api/* 保持 404 JSON，不伪装成 SPA 首页
            if normalized == "api" or normalized.startswith("api/"):
                raise
            # 带扩展名的资源请求（.js/.css/图片…）404 即 404：回退 index.html
            # 会以 200 + text/html 应答脚本/样式请求，触发浏览器严格 MIME
            # 检查告警并掩盖真实错误；SPA hash 路由不会发到服务器，
            # 无扩展名路径才回退首页
            if os.path.splitext(normalized)[1]:
                raise
            return FileResponse(os.path.join(_UI_DIR, "index.html"))


app.mount("/", _SpaStaticFiles(directory=_UI_DIR, html=True), name="static")
