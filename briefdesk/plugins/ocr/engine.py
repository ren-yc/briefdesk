"""OCR 图片文字识别模块。

只负责图片识别：给定图片字节，提取文字。
图片下载逻辑在消息源客户端（`SourceClient.download_media`），
本模块不接触 URL、HTTP 或鉴权。
"""

import asyncio
import logging

from rapidocr import RapidOCR

try:
    # rapidocr 未在包顶层导出该异常（位于 .main），按内部路径导入；
    # ImportError 兜底仅为防御 rapidocr 布局变更，兜底类实际不会被抛出。
    from rapidocr.main import RapidOCRError as _NoTextError
except ImportError:  # pragma: no cover — rapidocr 版本兼容兜底
    class _NoTextError(Exception):  # type: ignore[no-redef]
        """rapidocr 未导出 RapidOCRError 时的兜底（仅用于 except 分支）。"""

logger = logging.getLogger(__name__)
_ocr_engine: RapidOCR | None = None
_ocr_engine_lock = asyncio.Lock()


async def _get_ocr_engine() -> RapidOCR:
    global _ocr_engine
    if _ocr_engine is not None:
        return _ocr_engine

    async with _ocr_engine_lock:
        if _ocr_engine is None:
            # RapidOCR 构造时按 cfg.Global.log_level 设置其 logger 级别
            # （main.py: logger.setLevel(cfg.Global.log_level.upper())），
            # 默认 info 会输出 "Using model_path" 等与本项目格式不符的 INFO 日志。
            # 与 briefdesk/logger.py 对 httpx 等第三方库的处理一致，压制到 WARNING。
            _ocr_engine = await asyncio.to_thread(
                RapidOCR, params={"Global.log_level": "warning"}
            )
    return _ocr_engine


def _extract_text(engine: RapidOCR, content: bytes) -> str:
    """将图片字节交给 RapidOCR 识别，返回用换行拼接的文字。

    用换行符（而非 <br />）连接各行：内容最终会进入数据库并被前端
    转义渲染（esc），HTML 标记会以字面量显示。换行由前端的
    white-space: pre-line 呈现。

    RapidOCR 接受 bytes 输入并自行解码；引擎返回的联合类型中
    txts 仅存在于 RapidOCROutput（默认 det+cls+rec 全开时返回该类型），
    用 getattr 兜底即可。
    """
    result = engine(content)
    txts = getattr(result, "txts", None) or ()
    lines = [t.strip() for t in txts if t.strip()]
    return "\n".join(lines)


async def ocr_image_bytes(content: bytes) -> str:
    """识别单张图片字节，返回提取的文字。

    图片字节由调用方提供（一般来自消息源客户端的 download_media），
    本函数只调用 RapidOCR（ONNX Runtime）识别。

        Args:
            content: 图片文件的原始字节

        Returns:
            提取的文字，无文字时返回空字符串。
    """
    try:
        ocr = await _get_ocr_engine()
        return await asyncio.to_thread(_extract_text, ocr, content)
    except _NoTextError:
        # 图片上本就没有文字（表情包/照片等）：RapidOCR 会主动抛
        # RapidOCRError，属正常现象——视为"未识别到文字"，返回空串
        # 而非向调用方抛错（无文字不是失败）。
        logger.debug("图片无文字，返回空结果 (%d bytes)", len(content))
        return ""
    except Exception as e:
        # 仅 DEBUG：唯一调用方（ocr 插件 enrich）按"跳过 OCR、卡片仍以原文入库"
        # 记 WARNING。此处再打 ERROR 既重复又抬高了严重度——OCR 失败不阻断管道。
        logger.debug("OCR 识别失败 (%d bytes): %s", len(content), e)
        raise


async def ocr_images_bytes(contents: list[bytes]) -> str:
    """批量识别多张图片字节，合并结果。

    每张图片的结果用换行分隔，标注图片序号。

        Args:
            contents: 图片字节列表

        Returns:
            合并后的文字块，全部无文字时返回空字符串。
    """
    if not contents:
        return ""

    texts: list[str] = []
    for i, content in enumerate(contents, 1):
        text = await ocr_image_bytes(content)
        if text.strip():
            texts.append(f"[图片 {i} OCR 结果]\n{text}")

    return "\n".join(texts) if texts else ""
