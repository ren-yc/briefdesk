"""图片字节归一化 — 视觉路由（AI_VISION_ENABLED）的图片预处理工具。

enrich（OCR）阶段下载图片字节后，vision 开启时经 downscale_image 归一为
受限尺寸的 JPEG 随批暂存（BatchContext.vision_images），classify 构建
多模态请求时直接转 base64 data URL。输出恒为 JPEG，调用方无需再做格式
嗅探；任何解码/处理失败返回 None，由调用方逐图跳过（不影响批次其它图片）。
"""

import io
import logging

from PIL import Image, ImageOps

logger = logging.getLogger(__name__)

# 归一化目标：1568px 为主流视觉模型的最佳输入边长量级；JPEG 85 对截图/
# 海报类图片的文字可读性足够。max_bytes 控制单图请求体上界（base64 后约
# 1.6MB），防多图请求打爆端点请求限制。
MAX_SIDE = 1568
JPEG_QUALITY = 85
MAX_BYTES = 1_200_000


def _encode_jpeg(img: Image.Image, quality: int) -> bytes | None:
    try:
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=quality)
        return buf.getvalue()
    except Exception:  # noqa: BLE001 — 编码失败与解码失败同语义：逐图降级
        return None


def downscale_image(
    data: bytes,
    *,
    max_side: int = MAX_SIDE,
    quality: int = JPEG_QUALITY,
    max_bytes: int = MAX_BYTES,
) -> bytes | None:
    """把任意可解码图片归一为受限尺寸的 JPEG 字节；失败返回 None。

    处理链：解码 → EXIF 方向校正 → RGB 转换（透明通道平铺白底，防透明区
    落黑底干扰识别）→ 超边长等比缩放（LANCZOS）→ JPEG 编码；超 max_bytes
    依次降质量（85→60→40→30）试档，仍超则边长对半缩再试各档，直到缩至
    max_side/4 仍超才返回 None（逐图降级，与解码失败同语义——单图失败
    好过超限字节堆满多图请求触发端点 413，令整批 vision_fallback 降级）。
    """
    if not data:
        return None
    try:
        with Image.open(io.BytesIO(data)) as src:
            img = ImageOps.exif_transpose(src)
            if img.mode in ("RGBA", "LA", "P"):
                img = img.convert("RGBA")
                opaque = Image.new("RGB", img.size, (255, 255, 255))
                opaque.paste(img, mask=img.split()[3])
                img = opaque
            elif img.mode != "RGB":
                img = img.convert("RGB")
            width, height = img.size
            side = max(width, height)
            if side > max_side:
                scale = max_side / side
                img = img.resize(
                    (max(1, round(width * scale)), max(1, round(height * scale))),
                    Image.Resampling.LANCZOS,
                )

            def _try_qualities(image: Image.Image) -> bytes | None:
                for q in (quality, 60, 40, 30):
                    encoded = _encode_jpeg(image, q)
                    if encoded is not None and len(encoded) <= max_bytes:
                        return encoded
                return None

            fitted = _try_qualities(img)
            if fitted is not None:
                return fitted
            # 质量阶梯耗尽仍超预算：边长对半缩（高噪声/高细节图的主要
            # 超限因素），每档再试质量阶梯；缩至 max_side/4 仍超则放弃
            side = max(img.size)
            while side >= max_side // 4:
                img = img.resize(
                    (max(1, img.width // 2), max(1, img.height // 2)),
                    Image.Resampling.LANCZOS,
                )
                fitted = _try_qualities(img)
                if fitted is not None:
                    return fitted
                side //= 2
            return None
    except Exception:  # noqa: BLE001 — 任何解码/处理失败一律降级为 None
        logger.debug("图片归一化失败，跳过该图（%d bytes）", len(data))
        return None
