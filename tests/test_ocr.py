"""OCR 模块单元测试（mock 引擎，不加载真实 ONNX 模型）。

rapidocr/onnxruntime 为可选依赖（ocr extra）：未安装时引擎相关测试跳过，
插件自禁用逻辑（PluginDisabledError）不依赖真实环境、始终可测。
"""

import builtins
import io
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from PIL import Image

try:
    from rapidocr.main import RapidOCRError

    from briefdesk.plugins.ocr.engine import _extract_text, ocr_image_bytes

    _OCR_DEPS_AVAILABLE = True
except ImportError:  # pragma: no cover — 未安装 OCR extra 时引擎测试跳过
    _OCR_DEPS_AVAILABLE = False

from briefdesk.config import Settings, config
from briefdesk.plugin.base import PluginContext, PluginDisabledError
from briefdesk.plugins.ocr.plugin import OcrPlugin
from briefdesk.sources_base import MediaError
from briefdesk.types import BatchContext, InternalMessage


async def _noop_async(*args, **kwargs):
    return None


def _ctx(register_stage=None):
    """最小 PluginContext（复用 test_stage_plugins 的构造模式）。"""
    return PluginContext(
        config=Settings(
            plugins=["*"], plugins_disabled=[], plugins_required=[], plugin_path=""
        ),
        publish_event=_noop_async,
        subscribe_event=lambda event, handler: None,
        register_source=lambda runtime: None,
        register_stage=register_stage or (lambda stage: None),
    )


class _Txts:
    """模拟 RapidOCROutput：仅提供 txts 属性。"""

    def __init__(self, txts):
        self.txts = txts


class _FakeEngine:
    """可调用的假 OCR 引擎：按配置抛异常或返回结果。"""

    def __init__(self, result=None, error: Exception | None = None):
        self._result = result
        self._error = error

    def __call__(self, content):
        if self._error is not None:
            raise self._error
        return self._result


@unittest.skipUnless(_OCR_DEPS_AVAILABLE, "OCR 依赖未安装（pip install briefdesk[ocr]）")
class ExtractTextTest(unittest.TestCase):
    def test_joins_nonempty_lines(self):
        engine = _FakeEngine(result=_Txts(["  a  ", "", "b"]))
        self.assertEqual(_extract_text(engine, b"x"), "a\nb")

    def test_empty_txts_returns_empty(self):
        engine = _FakeEngine(result=_Txts([]))
        self.assertEqual(_extract_text(engine, b"x"), "")


@unittest.skipUnless(_OCR_DEPS_AVAILABLE, "OCR 依赖未安装（pip install briefdesk[ocr]）")
class OcrImageBytesTest(unittest.IsolatedAsyncioTestCase):
    async def test_no_text_returns_empty_string(self):
        # rapidocr 对无文字图片主动抛 RapidOCRError：应视为"未识别到文字"
        # 返回空串，而不是向调用方抛错（无文字不是失败）。
        engine = _FakeEngine(error=RapidOCRError("The text detection result is empty"))
        with patch("briefdesk.plugins.ocr.engine._get_ocr_engine", new=AsyncMock(return_value=engine)):
            self.assertEqual(await ocr_image_bytes(b"img"), "")

    async def test_engine_failure_propagates(self):
        # 真正的引擎故障（模型加载失败等）仍应向上抛，由调用方决定降级。
        engine = _FakeEngine(error=RuntimeError("model broken"))
        with (
            patch("briefdesk.plugins.ocr.engine._get_ocr_engine", new=AsyncMock(return_value=engine)),
            self.assertRaises(RuntimeError),
        ):
            await ocr_image_bytes(b"img")


class OcrPluginSetupTest(unittest.IsolatedAsyncioTestCase):
    async def test_setup_registers_enrich_stage_when_deps_present(self):
        # 依赖可用（模拟 engine 模块可导入）→ 注册 enrich 槽位
        registered = []
        with patch.dict(
            sys.modules,
            {"briefdesk.plugins.ocr.engine": SimpleNamespace(ocr_images_bytes=AsyncMock())},
        ):
            await OcrPlugin().setup(_ctx(register_stage=registered.append))
        self.assertEqual([s.slot for s in registered], ["enrich"])

    async def test_setup_self_disables_without_deps(self):
        # rapidocr/onnxruntime 未安装（engine 导入失败）→ 抛 PluginDisabledError
        # 自禁用（非致命），与 qqflow 缺必填配置的自禁用语义一致。
        # 用 __import__ 拦截模拟：engine 可能已被真实导入并挂在父包属性上，
        # patch sys.modules 会被属性查找绕过，__import__ 拦截与导入状态无关。
        real_import = builtins.__import__

        def _fake_import(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "briefdesk.plugins.ocr" and "engine" in (fromlist or ()):
                raise ImportError("No module named 'rapidocr'")
            return real_import(name, globals, locals, fromlist, level)

        with (
            patch("builtins.__import__", side_effect=_fake_import),
            self.assertRaises(PluginDisabledError),
        ):
            await OcrPlugin().setup(_ctx())


class _FakeMediaClient:
    """可配置的假媒体客户端：按 URL 返回字节或抛 MediaError。"""

    def __init__(self, payloads=None, error=None):
        self._payloads = payloads or {}
        self._error = error

    async def download_media(self, url):
        if self._error is not None:
            raise MediaError(self._error)
        return self._payloads.get(url, b"raw-bytes")


def _png_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (20, 10), (10, 10, 10)).save(buf, "PNG")
    return buf.getvalue()


def _vision_msg(msg_id="m1", source="weflow-legacy") -> InternalMessage:
    return InternalMessage(
        msg_id=msg_id,
        content="[图片]",
        sender_name="张三",
        sender_id="u1",
        session_id="s1",
        group_name="社团群",
        timestamp=1,
        source=source,
        image_urls=["p1"],
    )


class OcrPluginVisionStashTest(unittest.IsolatedAsyncioTestCase):
    """vision 路由：下载成功后把归一化图片字节随批暂存（独立于 OCR 结果）。"""

    async def _run(self, *, vision_enabled, ocr_result="识别文本", ocr_error=None, media_error=None):
        plugin = OcrPlugin()
        if ocr_error is not None:
            plugin._ocr_image_bytes = AsyncMock(side_effect=ocr_error)
        else:
            plugin._ocr_image_bytes = AsyncMock(return_value=ocr_result)
        msg = _vision_msg()
        batch = BatchContext(
            messages=[msg],
            client=_FakeMediaClient(payloads={"p1": _png_bytes()}, error=media_error),
        )
        with patch.object(config, "ai_vision_enabled", vision_enabled):
            await plugin.run(batch, None)
        return batch, msg

    async def test_vision_on_stashes_normalized_jpeg(self):
        batch, msg = await self._run(vision_enabled=True)
        stashed = batch.vision_images[("weflow-legacy", "m1")]
        self.assertEqual(len(stashed), 1)
        self.assertTrue(stashed[0].startswith(b"\xff\xd8"))  # 归一化输出为 JPEG
        self.assertEqual(msg.content, "[OCR]\n识别文本")  # OCR 替换契约不变

    async def test_vision_on_ocr_failure_still_stashes(self):
        # 暂存独立于 OCR 结果：引擎故障时图片仍可送模型，content 保持原文
        batch, msg = await self._run(vision_enabled=True, ocr_error=RuntimeError("broken"))
        self.assertIn(("weflow-legacy", "m1"), batch.vision_images)
        self.assertEqual(msg.content, "[图片]")

    async def test_vision_off_no_stash(self):
        batch, _msg = await self._run(vision_enabled=False)
        self.assertEqual(batch.vision_images, {})

    async def test_media_error_no_stash(self):
        batch, _msg = await self._run(vision_enabled=True, media_error="404")
        self.assertEqual(batch.vision_images, {})


if __name__ == "__main__":
    unittest.main()
