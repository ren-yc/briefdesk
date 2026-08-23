"""媒体代理（server 子包）：/api/media/{source}/{path} 经源客户端下载后转发。

浏览器无法携带消息源 token，由服务端经 `SourceClient.download_media`
下载字节后转发；路径安全校验与图片魔数嗅探也在此。
"""

import mimetypes

from fastapi import HTTPException
from fastapi.responses import Response

from briefdesk.server.app import app
from briefdesk.sources_base import MediaError
from briefdesk.status import get_source_client


def _is_safe_media_path(path: str) -> bool:
    """路由层媒体路径安全校验（不依赖各源客户端实现）。

    拦截已被解码的穿越载荷（../）、绝对路径、反斜杠、控制字符，
    以及任何残留百分号（防 %252e%252e%252f 双重编码绕过）。
    合法文件名中的普通点号（如 abc.jpg）不受影响。
    """
    if not path or path.startswith("/"):
        return False
    if "\\" in path or "%" in path:
        return False
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in path):
        return False
    # 拒绝空段（// 或尾部 /）、"." 与 ".."
    return all(seg not in ("", ".", "..") for seg in path.split("/"))


# 图片魔数嗅探（仅猜不出扩展名时兜底，如 qqflow 的 mediaId 无扩展名）
_IMAGE_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"BM", "image/bmp"),
)


def _sniff_content_type(content: bytes, fallback: str) -> str:
    """按魔数嗅探图片类型；识别失败返回 fallback。"""
    if content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        return "image/webp"
    for magic, ctype in _IMAGE_MAGIC:
        if content.startswith(magic):
            return ctype
    return fallback


@app.get("/api/media/{source}/{path:path}")
async def api_media_proxy(source: str, path: str):
    """代理消息源媒体文件，解决跨域与鉴权问题。

    媒体归属具体消息源（path 为该源自身约定的媒体路径），浏览器无法
    携带源 token，故由服务端经 SourceClient.download_media 下载后转发。
    """
    if not _is_safe_media_path(path):
        raise HTTPException(404, "Media not found")

    client = get_source_client(source)
    if client is None:
        raise HTTPException(404, f"Unknown source: {source}")

    try:
        content = await client.download_media(path)
    except MediaError as e:
        raise HTTPException(404, f"Media unavailable: {e}") from e

    content_type = mimetypes.guess_type(path)[0] or _sniff_content_type(
        content, "application/octet-stream"
    )
    return Response(content=content, media_type=content_type)
