"""文本类文件的 UTF-8 契约。

面（由 git 的内容判定给出，不维护后缀白名单）：跟踪文件里工作区侧**未被判为二进制**
的那些（`git ls-files --eol` 的 `w/` ≠ `-text`）——含空文件（`w/none`）与工作区缺失的
跟踪文件（`w/` 为空）。判定来自内容，故 `.svg`/`.css`/`.json`/`.txt`/`.html`/`.example`/
`.in` 与无后缀的 `LICENSE`/`.gitignore` 自动纳入，新增扩展名也自动覆盖。

契约：面内文件必须存在、不带 UTF-8 BOM、且能严格 UTF-8 解码。

已知边界（有意不覆盖）：git 判为二进制的文件一律豁免——包括 ① UTF-16 等含 NUL 的编码
（如 PowerShell ISE 另存的 `.ps1`）、② 内容为二进制却挂文本后缀的文件（如把 PNG 存成
`.md`）。若要覆盖，改法＝面并上「文本后缀白名单」。

本守卫只读工作区：工作区干净而索引里是坏内容的情形，要等 CI 的全新检出才会暴露。
"""

from __future__ import annotations

import codecs
import subprocess
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def _worktree_text_files(root: Path = _ROOT) -> list[str]:
    """git 未判为二进制的跟踪文件（含空文件与工作区缺失）。

    独立调用 git（不复用扫描器的 `_git`），避免本守卫被扫描器的行为改动牵动；
    `-c core.quotePath=false` 保证非 ASCII 路径原样输出，`-z` 保证含空格的路径可解析。
    `root` 可注入：负向自证在临时仓库里做，不碰本仓 index。
    """
    proc = subprocess.run(
        ["git", "-c", "core.quotePath=false", "ls-files", "--eol", "-z"],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(f"git ls-files --eol 失败，契约检查未执行：{proc.stderr.strip()}")
    files: list[str] = []
    for record in proc.stdout.split("\0"):
        if not record:
            continue
        head, sep, path = record.partition("\t")
        fields = head.split()
        if not sep or len(fields) < 3:
            raise AssertionError(f"ls-files --eol 输出无法解析：{record!r}")
        if fields[1] != "w/-text":
            files.append(path)
    return files


def _violations(root: Path, files: list[str]) -> list[str]:
    """给定文件清单，逐条给出违约说明（空清单＝全部合规）。"""
    bad: list[str] = []
    for rel in files:
        try:
            raw = (root / rel).read_bytes()
        except OSError as error:
            bad.append(f"{rel}（{type(error).__name__}）")
            continue
        if raw.startswith(codecs.BOM_UTF8):
            bad.append(f"{rel}（带 UTF-8 BOM）")
            continue
        try:
            raw.decode("utf-8")
        except UnicodeDecodeError:
            bad.append(f"{rel}（不是 UTF-8）")
    return bad


def test_text_files_are_utf8() -> None:
    """契约面内每个文件必须存在、不带 BOM、且能严格 UTF-8 解码。"""
    files = _worktree_text_files(_ROOT)
    assert files, "契约面为空，分类逻辑可能失效"
    bad = _violations(_ROOT, files)
    assert not bad, (
        "以下文本类文件不是 UTF-8 / 带 BOM / 读不到，请修正（或确认它不该被跟踪）：\n  "
        + "\n  ".join(bad)
    )
