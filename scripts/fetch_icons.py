"""Lucide 图标拉取脚本（ui/icons/ vendored 子集的唯一来源通道）。

- 钉定 lucide-static 版本（``LUCIDE_STATIC_VERSION``），禁止整库拷贝、
  禁止引入第二图标库（约定见 ui/icons/README.md）；
- ``add`` 逐个拉取并自动登记 ui/icon-manifest.txt（单一事实来源）；
- ``check`` 校验清单全部图标在钉定版本可拉取且格式能被前端内联管线接受；
- ``show`` 打印当前钉定版本。

用法::

    python scripts/fetch_icons.py add <name> [<name> ...]   # 拉取并登记
    python scripts/fetch_icons.py add --overwrite <name>    # 覆盖已存在文件
    python scripts/fetch_icons.py check                     # 校验钉定版本可用性
    python scripts/fetch_icons.py show                      # 显示钉定版本
"""

from __future__ import annotations

import argparse
import re
import sys
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ICONS_DIR = REPO_ROOT / "ui" / "icons"
MANIFEST = REPO_ROOT / "ui" / "icon-manifest.txt"

# 钉定的 lucide-static 版本。升级流程：先 `python scripts/fetch_icons.py check`
# 确认清单全部图标在新版本仍可拉取（未被改名/移除），再改本常量并同步更新
# ui/icons/README.md 的「版本记录」，最后跑 tests/test_icon_manifest.py 与
# tests/test_fetch_icons_script.py。
LUCIDE_STATIC_VERSION = "1.34.0"

# 与 ui/app.js 的 _SVG_CONTENT_RE / tests/test_icon_manifest.py 保持一致：
# 可选前置 XML 注释 + <svg> 根标签；不满足的文件会被前端内联管线静默拒绝，
# 退回 <img> 形态、深色模式恒黑。
_SVG_CONTENT_RE = re.compile(r"^\s*(?:<!--[\s\S]*?-->\s*)*<svg[\s>]")
_ICON_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_FETCH_URL = "https://unpkg.com/lucide-static@{version}/icons/{name}.svg"


def _default_fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=30) as resp:
        return resp.read()


def _manifest_entries(lines: list[str]) -> set[str]:
    return {ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")}


def _display_path(dest: Path) -> str:
    """仓库内路径显示为相对路径（测试等仓库外目录兜底为全路径）。"""
    try:
        return str(dest.relative_to(REPO_ROOT))
    except ValueError:
        return str(dest)


def add_icon(
    name: str,
    *,
    version: str = LUCIDE_STATIC_VERSION,
    fetch=_default_fetch,
    icons_dir: Path = ICONS_DIR,
    manifest: Path = MANIFEST,
    overwrite: bool = False,
) -> str:
    """拉取单个 Lucide 图标、写入 icons_dir 并登记 manifest。

    任一步骤失败抛 ValueError（不写文件、不改清单）；成功返回人读摘要。
    """
    if not _ICON_NAME_RE.fullmatch(name):
        raise ValueError(f"图标名须为 kebab-case（a-z/0-9/连字符）: {name!r}")
    dest = icons_dir / f"{name}.svg"
    if dest.exists() and not overwrite:
        raise ValueError(f"图标文件已存在: {dest.name}（--overwrite 可覆盖）")
    raw = fetch(_FETCH_URL.format(version=version, name=name))
    text = raw.decode("utf-8")
    if not _SVG_CONTENT_RE.match(text):
        raise ValueError(f"{name}: 内容不被内联管线接受（须为可选注释 + <svg> 根标签）")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text, encoding="utf-8")
    entry = f"/icons/{name}.svg"
    content = manifest.read_text(encoding="utf-8")
    lines = content.splitlines()
    if entry in _manifest_entries(lines):
        return f"{name}: 已写入 {_display_path(dest)}（已在清单中）"
    if content and not content.endswith("\n"):
        content += "\n"
    manifest.write_text(content + entry + "\n", encoding="utf-8")
    return f"{name}: 已写入 {_display_path(dest)} 并登记清单"


def check_icons(
    *,
    version: str = LUCIDE_STATIC_VERSION,
    fetch=_default_fetch,
    manifest: Path = MANIFEST,
) -> list[tuple[str, str]]:
    """校验清单全部图标在钉定版本可拉取且格式可接受。返回 [(名称, 结论)]。"""
    results: list[tuple[str, str]] = []
    lines = manifest.read_text(encoding="utf-8").splitlines()
    for entry in sorted(_manifest_entries(lines)):
        name = entry.removeprefix("/icons/").removesuffix(".svg")
        try:
            raw = fetch(_FETCH_URL.format(version=version, name=name))
        except Exception as exc:  # noqa: BLE001 —— 命令行检查工具，汇总后由退出码判定
            results.append((name, f"拉取失败: {exc}"))
            continue
        text = raw.decode("utf-8", errors="replace")
        if _SVG_CONTENT_RE.match(text):
            results.append((name, "可用"))
        else:
            results.append((name, "内容格式不可接受"))
    return results


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="从钉定版本的 lucide-static 拉取单个图标并登记清单。"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    add_p = sub.add_parser("add", help="拉取并登记图标（逐个拉取，禁止整库拷贝）")
    add_p.add_argument("names", nargs="+", metavar="NAME")
    add_p.add_argument("--overwrite", action="store_true", help="覆盖已存在文件")
    sub.add_parser("check", help="校验清单全部图标在钉定版本可用")
    sub.add_parser("show", help="显示钉定版本")
    args = parser.parse_args(argv)

    if args.command == "show":
        print(f"lucide-static@{LUCIDE_STATIC_VERSION}")
        return 0
    if args.command == "check":
        results = check_icons()
        ok = True
        for name, verdict in results:
            print(f"{name}: {verdict}")
            ok = ok and verdict == "可用"
        return 0 if ok else 1
    rc = 0
    for name in args.names:
        try:
            print(add_icon(name, overwrite=args.overwrite))
        except ValueError as exc:
            print(f"错误: {exc}", file=sys.stderr)
            rc = 1
    return rc


if __name__ == "__main__":
    raise SystemExit(_main())