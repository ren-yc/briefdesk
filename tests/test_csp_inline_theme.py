"""CSP 内联脚本守卫：index.html 的全部内联 <script> 的 sha256 必须出现在
middleware 的 CSP script-src 白名单中（复核 P1-6）。

背景：CSP `script-src 'self'` 会拦截无 src 的内联脚本，令 <head> 内联主题
脚本静默失效（深色用户每次刷新先闪浅色）。本测试把「内联脚本 → hash」与
「CSP 白名单」对齐，未来改动内联脚本内容而忘更新 CSP 时会在此失败。
"""

import base64
import hashlib
import re
import unittest
from pathlib import Path

from starlette.testclient import TestClient

import briefdesk.server as srv

_UI_DIR = Path(__file__).resolve().parents[1] / "ui"


def _inline_script_hashes() -> list[str]:
    """提取 index.html 中所有无 src 的 <script> 内联内容，返回其 CSP sha256 值。"""
    html = (_UI_DIR / "index.html").read_text(encoding="utf-8")
    hashes: list[str] = []
    for body in re.findall(r"<script>(.*?)</script>", html, re.DOTALL):
        digest = hashlib.sha256(body.encode("utf-8")).digest()
        hashes.append(f"'sha256-{base64.b64encode(digest).decode()}'")
    return hashes


class CspInlineThemeGuardTest(unittest.TestCase):
    def test_every_inline_script_hash_is_in_csp(self):
        csp_hashes = _inline_script_hashes()
        self.assertTrue(csp_hashes, "index.html 应至少有一个内联 <script>")

        # 经任意页面响应头取 CSP 值（中间件对 200/404 均设置该头）
        client = TestClient(srv.app, base_url="http://localhost")
        resp = client.get("/api/status")
        client.close()
        csp = resp.headers.get("content-security-policy", "")

        for h in csp_hashes:
            self.assertIn(
                h,
                csp,
                f"内联脚本 hash {h} 未出现在 CSP script-src 白名单中；"
                "改动 index.html 内联脚本后需同步更新 middleware.py 的 CSP",
            )
