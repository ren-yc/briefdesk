"""imaging 模块单元测试（视觉路由的图片字节归一化，不依赖 OCR extra）。"""

import io
import unittest

from PIL import Image

from briefdesk.imaging import downscale_image


def _png(size, mode="RGB", color=(200, 30, 40)) -> bytes:
    img = Image.new(mode, size, color)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


class DownscaleImageTest(unittest.TestCase):
    def test_small_image_passes_through_as_jpeg(self):
        out = downscale_image(_png((100, 60)))
        self.assertIsNotNone(out)
        with Image.open(io.BytesIO(out)) as img:
            self.assertEqual(img.format, "JPEG")
            self.assertEqual(img.size, (100, 60))

    def test_oversize_scaled_proportionally(self):
        out = downscale_image(_png((3136, 400)))
        self.assertIsNotNone(out)
        with Image.open(io.BytesIO(out)) as img:
            self.assertEqual(img.size, (1568, 200))

    def test_empty_or_corrupt_bytes_return_none(self):
        self.assertIsNone(downscale_image(b""))
        self.assertIsNone(downscale_image(b"not an image"))

    def test_transparent_png_composited_on_white(self):
        # 透明区平铺白底而非落黑底（PNG 海报透明背景是常见形态）
        out = downscale_image(_png((10, 10), mode="RGBA", color=(255, 0, 0, 0)))
        self.assertIsNotNone(out)
        with Image.open(io.BytesIO(out)) as img:
            px = img.convert("RGB").getpixel((0, 0))
        self.assertGreaterEqual(min(px), 250)

    def test_exif_orientation_transposed(self):
        # 带 Orientation=6 的 JPEG：输出应已按 EXIF 转正（存储 100x50 → 显示 50x100）
        img = Image.new("RGB", (100, 50), (10, 200, 10))
        exif = Image.Exif()
        exif[274] = 6
        buf = io.BytesIO()
        img.save(buf, "JPEG", exif=exif)
        out = downscale_image(buf.getvalue())
        self.assertIsNotNone(out)
        with Image.open(io.BytesIO(out)) as rotated:
            self.assertEqual(rotated.size, (50, 100))

    def test_max_bytes_degrades_smaller(self):
        # 超预算降级链（降质量 → 降边长）的产物必须小于未降级编码
        data = _png((800, 800))
        uncapped = downscale_image(data)
        self.assertIsNotNone(uncapped)
        capped = downscale_image(data, max_bytes=max(1, len(uncapped) - 1))
        self.assertIsNotNone(capped)
        self.assertLess(len(capped), len(uncapped))

    def test_unreachable_max_bytes_returns_none(self):
        # 降级链（质量 85→60→40→30 → 边长对半缩至 max_side/4）耗尽仍超预算
        # 时返回 None（逐图降级，与解码失败同语义）——宁可丢一张图，也不让
        # 超限字节堆满多图请求触发端点 413、令整批 vision_fallback 降级
        self.assertIsNone(downscale_image(_png((400, 400)), max_bytes=1))

    def test_noisy_image_capped_to_budget(self):
        # 高噪声图是超预算的主要形态（JPEG 对高频细节压缩率差）：输出必须
        # ≤ max_bytes，或彻底放弃（None）——绝不能返回超限字节
        import random

        rng = random.Random(42)
        noisy = Image.new("RGB", (1200, 1200))
        noisy.putdata([(rng.randrange(256), rng.randrange(256), rng.randrange(256))
                       for _ in range(1200 * 1200)])
        buf = io.BytesIO()
        noisy.save(buf, "PNG")
        out = downscale_image(buf.getvalue(), max_bytes=200_000)
        if out is not None:
            self.assertLessEqual(len(out), 200_000)
        # 预算极紧时宁可放弃也不超限
        out_tight = downscale_image(buf.getvalue(), max_bytes=1_000)
        if out_tight is not None:
            self.assertLessEqual(len(out_tight), 1_000)


if __name__ == "__main__":
    unittest.main()
