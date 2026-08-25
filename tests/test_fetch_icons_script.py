"""scripts/fetch_icons.py 的纯函数级单元测试（不触网，注入假拉取器）。

约定（ui/icons/README.md）：图标只允许从钉定的 lucide-static 版本逐个拉取，
自动登记 ui/icon-manifest.txt；内容必须能被前端内联管线接受（可选注释 + <svg>）。
"""

from pathlib import Path

import pytest

from scripts.fetch_icons import (
    LUCIDE_STATIC_VERSION,
    add_icon,
    check_icons,
)

GOOD_SVG = '<!-- license --><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path d="M0 0"/></svg>'
BAD_SVG = "<html><body>SPA fallback</body></html>"
NOT_FOUND = b"404: Not Found"


def _manifest_file(tmp_path: Path, names: list[str]) -> Path:
    manifest = tmp_path / "icon-manifest.txt"
    manifest.write_text(
        "# 注释行不计入条目\n" + "\n".join(f"/icons/{n}.svg" for n in names) + "\n",
        encoding="utf-8",
    )
    return manifest


def _fake_fetch(available: dict[str, bytes]):
    def fetch(url: str) -> bytes:
        return available.get(url, NOT_FOUND)

    return fetch


def _good_fetch(url: str) -> bytes:
    return GOOD_SVG.encode()


def test_add_icon_writes_file_and_registers_manifest(tmp_path: Path) -> None:
    icons_dir = tmp_path / "icons"
    manifest = _manifest_file(tmp_path, ["bell"])
    summary = add_icon(
        "rocket",
        fetch=_good_fetch,
        icons_dir=icons_dir,
        manifest=manifest,
    )
    assert (icons_dir / "rocket.svg").read_text(encoding="utf-8") == GOOD_SVG
    assert "/icons/rocket.svg" in manifest.read_text(encoding="utf-8")
    assert "登记清单" in summary
    # 原有条目与注释保持不变
    assert "/icons/bell.svg" in manifest.read_text(encoding="utf-8")


def test_add_icon_hits_pinned_version_url(tmp_path: Path) -> None:
    seen: list[str] = []

    def fetch(url: str) -> bytes:
        seen.append(url)
        return GOOD_SVG.encode()

    add_icon("rocket", fetch=fetch, icons_dir=tmp_path / "icons", manifest=_manifest_file(tmp_path, []))
    expected = f"https://unpkg.com/lucide-static@{LUCIDE_STATIC_VERSION}/icons/rocket.svg"
    assert seen == [expected]


def test_add_icon_existing_file_needs_overwrite(tmp_path: Path) -> None:
    icons_dir = tmp_path / "icons"
    icons_dir.mkdir()
    (icons_dir / "bell.svg").write_text(GOOD_SVG, encoding="utf-8")
    manifest = _manifest_file(tmp_path, ["bell"])

    with pytest.raises(ValueError, match="已存在"):
        add_icon("bell", fetch=_good_fetch, icons_dir=icons_dir, manifest=manifest)

    summary = add_icon(
        "bell",
        fetch=_good_fetch,
        icons_dir=icons_dir,
        manifest=manifest,
        overwrite=True,
    )
    assert "已在清单中" in summary
    assert (icons_dir / "bell.svg").read_text(encoding="utf-8") == GOOD_SVG


def test_add_icon_rejects_bad_name(tmp_path: Path) -> None:
    icons_dir = tmp_path / "icons"
    manifest = _manifest_file(tmp_path, [])
    for bad in ("Rocket", "rocket..svg", "rocket/svg"):
        with pytest.raises(ValueError, match="kebab-case"):
            add_icon(bad, fetch=_fake_fetch({}), icons_dir=icons_dir, manifest=manifest)
    assert not icons_dir.exists()


def test_add_icon_rejects_unacceptable_content(tmp_path: Path) -> None:
    icons_dir = tmp_path / "icons"
    manifest = _manifest_file(tmp_path, [])
    with pytest.raises(ValueError, match="内联管线"):
        add_icon("rocket", fetch=_fake_fetch({}), icons_dir=icons_dir, manifest=manifest)
    assert not icons_dir.exists()
    assert "/icons/rocket.svg" not in manifest.read_text(encoding="utf-8")


def test_check_icons_reports_availability_per_icon(tmp_path: Path) -> None:
    manifest = _manifest_file(tmp_path, ["a", "b"])

    def fetch(url: str) -> bytes:
        if url.endswith("/icons/a.svg"):
            return GOOD_SVG.encode()
        raise ConnectionError("network down")

    results = dict(check_icons(fetch=fetch, manifest=manifest))
    assert results == {"a": "可用", "b": "拉取失败: network down"}


def test_check_icons_flags_unacceptable_format(tmp_path: Path) -> None:
    manifest = _manifest_file(tmp_path, ["a"])

    def fetch(url: str) -> bytes:
        return BAD_SVG.encode()

    assert check_icons(fetch=fetch, manifest=manifest) == [("a", "内容格式不可接受")]