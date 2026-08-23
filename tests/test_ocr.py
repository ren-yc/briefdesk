"""OCR 模块单元测试（mock 引擎，不加载真实 ONNX 模型）。

rapidocr/onnxruntime 为可选依赖（ocr extra）：未安装时引擎相关测试跳过，
插件自禁用逻辑（PluginDisabledError）不依赖真实环境、始终可测。
"""

import builtins
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

try:
    from rapidocr.main import RapidOCRError

    from briefdesk.plugins.ocr.engine import _extract_text, ocr_image_bytes

    _OCR_DEPS_AVAILABLE = True
except ImportError:  # pragma: no cover — 未安装 OCR extra 时引擎测试跳过
    _OCR_DEPS_AVAILABLE = False

from briefdesk.config import Settings
from briefdesk.plugin.base import PluginContext, PluginDisabledError
from briefdesk.plugins.ocr.plugin import OcrPlugin


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


if __name__ == "__main__":
    unittest.main()
