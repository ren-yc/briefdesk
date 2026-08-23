"""OCR 增强阶段插件（slot=enrich）— 图片文字识别后以 [OCR] 前缀替换 content。

图片下载归消息源客户端（SourceClient.download_media），本插件只做
下载 → 识别 → 脱敏 → 替换。单条失败（MediaError / 识别异常）只跳过
该条 OCR，不拖垮整批。

OCR 依赖（rapidocr / onnxruntime）为可选安装（`pip install briefdesk[ocr]`）：
未安装时 setup 抛 PluginDisabledError 自禁用，不影响其余插件与核心。
"""

import logging
import time as time_module
from collections.abc import Awaitable, Callable

from briefdesk.logger import fmt_dur
from briefdesk.masking import mask_content
from briefdesk.plugin.base import PluginContext, PluginDisabledError, StagePlugin
from briefdesk.sources_base import MediaError
from briefdesk.types import BatchContext

logger = logging.getLogger(__name__)


class OcrPlugin(StagePlugin):
    """OCR 阶段插件（显式实现 StagePlugin；入口见模块底部 `plugin` 实例）。"""

    name = "ocr"
    version = "1.0.0"
    dependencies: tuple[str, ...] = ()
    slot = "enrich"
    priority = 0

    def __init__(self) -> None:
        self._ocr_image_bytes: Callable[[list[bytes]], Awaitable[str]] | None = None

    async def setup(self, ctx: PluginContext) -> None:
        # 延迟导入：仅加载本插件依赖，且便于测试替换。rapidocr / onnxruntime
        # 为可选依赖（ocr extra），缺失时自禁用而非 failed——与 qqflow 缺必填
        # 配置的自禁用语义一致（非致命，其余插件照常启动）。
        try:
            from briefdesk.plugins.ocr import engine as ocr_engine
        except ImportError as e:
            raise PluginDisabledError(
                "未安装 OCR 依赖（rapidocr/onnxruntime），OCR 已禁用；"
                "可执行 `pip install briefdesk[ocr]` 安装后启用"
            ) from e
        self._ocr_image_bytes = ocr_engine.ocr_images_bytes
        ctx.register_stage(self)

    async def activate(self, ctx: PluginContext) -> None: ...

    async def teardown(self) -> None: ...

    async def run(self, batch: BatchContext, ctx: PluginContext) -> None:
        if self._ocr_image_bytes is None:
            return
        for msg in batch.messages:
            if not msg.image_urls:
                continue
            ocr_start = time_module.perf_counter()
            try:
                contents = [
                    await batch.client.download_media(url) for url in msg.image_urls
                ]
            except MediaError as e:
                # 单条图片下载失败不应拖垮整批（图片过期/源离线时跳过 OCR，
                # 卡片仍以原文内容入库并保留 image_urls 供前端代理显示）
                logger.warning(f"图片下载失败，跳过 OCR（消息 {msg.msg_id}）: {e}")
                continue
            try:
                ocr_text = await self._ocr_image_bytes(contents)
            except Exception as e:  # noqa: BLE001 — OCR 失败不应拖垮整批
                # 引擎故障等非预期异常：跳过 OCR，卡片仍以原文内容入库
                logger.warning(f"OCR 识别失败，跳过（消息 {msg.msg_id}）: {e}")
                continue
            if ocr_text:
                # OCR 文本是构造后替换进 content 的，需在替换前单独脱敏
                # （原文部分已在 InternalMessage 构造时脱敏）
                ocr_text = mask_content(ocr_text)
                msg.content = "[OCR]\n" + ocr_text
            logger.debug(
                "OCR 完成: msg_id=%s, %d 图, %d bytes, 识别 %d 字 (%s)",
                msg.msg_id,
                len(msg.image_urls),
                sum(len(c) for c in contents),
                len(ocr_text or ""),
                fmt_dur(time_module.perf_counter() - ocr_start),
            )


plugin = OcrPlugin()
