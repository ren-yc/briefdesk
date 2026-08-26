"""本地访问守卫：Host 白名单（防 DNS rebinding）+ 变更接口同源校验 + 安全响应头。

从原 server.py 拆出（P5 子包化）：本模块导入即向 `briefdesk.server.app`
注册 http 中间件。
"""

from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import JSONResponse

from briefdesk.config import config
from briefdesk.server.app import app

_ALLOWED_HOSTNAMES = frozenset({"localhost", "127.0.0.1", "::1"})
_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _same_origin(origin: str, request: Request) -> bool:
    """浏览器 Origin/Referer 是否与当前请求同源（scheme/host/port 一致）。"""
    try:
        parts = urlsplit(origin)
        if parts.scheme not in ("http", "https") or parts.hostname is None:
            return False
        origin_port = parts.port
        if origin_port is None:
            origin_port = 443 if parts.scheme == "https" else 80
        request_port = request.url.port
        if request_port is None:
            request_port = 443 if request.url.scheme == "https" else 80
        return origin_port == request_port and parts.hostname == request.url.hostname
    except ValueError:
        return False


@app.middleware("http")
async def _local_security_guard(request: Request, call_next):
    """仅接受本机 Host，并对 /api 变更请求做同源校验。

    Host 白名单阻断 DNS rebinding（恶意域名解析到 127.0.0.1）；
    Origin/Referer 校验阻断浏览器跨站表单/fetch 对变更接口的 CSRF 调用。
    """
    host = (request.url.hostname or "").lower()
    if host not in _ALLOWED_HOSTNAMES or request.url.port not in (
        None,
        config.server_port,
    ):
        return JSONResponse({"detail": "Invalid Host header"}, status_code=400)

    if request.method in _MUTATING_METHODS and request.url.path.startswith("/api/"):
        origin = request.headers.get("origin")
        referer = request.headers.get("referer")
        # Origin/Referer 双缺失不得静默放行：浏览器 fetch/表单跨站 POST
        # 总会携带 Origin，双缺只出现在剥离 Referer 的旧浏览器/隐私扩展
        # 环境——此时宁可拒绝（前端同源请求不受影响）
        source = origin or referer
        if source is None:
            return JSONResponse(
                {"detail": "Missing Origin/Referer header"}, status_code=403
            )
        if not _same_origin(source, request):
            return JSONResponse(
                {"detail": "Cross-origin request rejected"}, status_code=403
            )

    response = await call_next(request)
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self'; font-src 'self'; "
        "object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
    )
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    return response
