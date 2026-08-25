"""图标清单守卫测试。

约定（见 ui/icons/README.md 与 ui/icon-manifest.txt）：
- ``ui/icons/*.svg`` 文件集合必须与清单完全一致（多、少都算失败）；
- 代码中引用的 ``/icons/<name>.svg`` 必须已登记且文件真实存在；
- 旧中文图标库路径 ``/图标/`` 不得回流。

扫描范围：核心前端（index.html / app.js / style.css）与全部插件前端
（briefdesk/plugins/*/ui/*.js）——插件引用核心图标路径，一并约束。
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ICONS_DIR = REPO / "ui" / "icons"
MANIFEST = REPO / "ui" / "icon-manifest.txt"

_SOURCES = [
    REPO / "ui" / "index.html",
    REPO / "ui" / "app.js",
    REPO / "ui" / "style.css",
    *sorted((REPO / "briefdesk" / "plugins").glob("*/ui/*.js")),
]

_ICON_REF = re.compile(r"/icons/[\w.-]+\.svg")

# 与 ui/app.js 的 _SVG_CONTENT_RE 保持一致：可选前置 XML 注释 + <svg> 根标签。
# 不满足该格式的文件会被内联管线静默拒绝（退回 <img> 形态，深色模式恒黑）
_SVG_CONTENT = re.compile(r"^\s*(?:<!--[\s\S]*?-->\s*)*<svg[\s>]")


def _manifest_paths() -> set[str]:
    lines = MANIFEST.read_text(encoding="utf-8").splitlines()
    return {
        line.strip()
        for line in lines
        if line.strip() and not line.strip().startswith("#")
    }


def _all_references() -> set[str]:
    refs: set[str] = set()
    for src in _SOURCES:
        assert src.exists(), f"守卫扫描目标不存在（路径变更须同步本测试）: {src}"
        refs |= set(_ICON_REF.findall(src.read_text(encoding="utf-8")))
    return refs


def test_manifest_matches_disk() -> None:
    """清单与 ui/icons/ 磁盘文件集合双向一致。"""
    listed = _manifest_paths()
    on_disk = {f"/icons/{p.name}" for p in ICONS_DIR.glob("*.svg")}
    assert listed == on_disk, (
        f"清单与 ui/icons/ 不一致："
        f"仅清单有={sorted(listed - on_disk)} 仅磁盘有={sorted(on_disk - listed)}"
    )


def test_referenced_icons_listed_and_exist() -> None:
    """代码引用的图标必须已登记且文件存在。"""
    listed = _manifest_paths()
    refs = _all_references()
    assert refs, "未扫描到任何 /icons/ 引用（扫描范围配置可能失效）"
    missing = {
        r for r in refs if not (REPO / "ui" / r.lstrip("/")).exists()
    }
    assert not missing, f"引用的图标文件不存在: {sorted(missing)}"
    unlisted = refs - listed
    assert not unlisted, f"引用未登记进 ui/icon-manifest.txt: {sorted(unlisted)}"


def test_svg_files_acceptable_by_inline_pipeline() -> None:
    """每个图标文件必须是内联管线可接受的格式（可选注释 + <svg> 根标签）。"""
    for p in sorted(ICONS_DIR.glob("*.svg")):
        text = p.read_text(encoding="utf-8")
        assert _SVG_CONTENT.match(text), (
            f"{p.name} 不被内联管线接受（须为可选前置注释 + <svg> 根标签），"
            "否则该图标将退回 <img> 形态且深色模式下恒黑"
        )


def test_no_legacy_icon_path_references() -> None:
    """旧中文图标库路径不得回流。"""
    for src in _SOURCES:
        text = src.read_text(encoding="utf-8")
        assert "/图标/" not in text, f"残留旧图标路径引用: {src}"
