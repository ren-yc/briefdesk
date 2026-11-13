"""文档形态守卫：折点、目录、节首结论缺口、编号标题与围栏闭合。

判据与 AGENTS.md「文档排版规范」一致：折点 = 行尾汉字 + 下一物理行以汉字
或全角开括号开头（同一散文段）；句末标点后换行、行尾两空格与引用块续行
不算折点。结构检查覆盖目录覆盖、节首结论缺口（首行即标题）、编号二级标题
与围栏/注释闭合。>60 显示列单元格与段落长度按软口径只报告、不阻断。
"""

from __future__ import annotations

import contextlib
import os
import re
import subprocess
import unicodedata
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def _repo_root() -> Path:
    """以 git 根为准，避免从子目录运行时把扫描面缩成 0 个文件。"""
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git rev-parse --show-toplevel 失败：{result.stderr.strip()}")
    return Path(result.stdout.strip())


ROOT = _repo_root()

HAN = re.compile(r"[\u4e00-\u9fff]")
OPEN_FW = re.compile("[\uff08\uff3b\uff5b\u300c\u300e\u3010\u300a\u3008\u3014\u3016\u201c\u2018]")
COMMON_BEGIN = "<!-- common:begin -->"
COMMON_END = "<!-- common:end -->"

_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_LIST = re.compile(r"^(\s*)([-*+]|\d{1,3}[.)])\s+")
_QUOTE = re.compile(r"^\s*>")
_TABLE = re.compile(r"^\s*\|")
_HR = re.compile(r"^\s{0,3}(-{3,}|\*{3,}|_{3,})\s*$")
_INDENT_CODE = re.compile(r"^(    |\t)")
_LINK = re.compile(r"\]\(#([^)]+)\)")


def _slug(text: str) -> str:
    """GitHub 锚点规则：小写 → 去标点 → 空格转连字符。"""
    value = text.strip().lower()
    value = re.sub(r"[^\w \-]", "", value, flags=re.UNICODE)
    return value.replace(" ", "-")


def _fence_step(line: str, open_fence: str | None) -> str | None:
    """CommonMark 围栏状态转移：返回新的开启围栏标记（None = 不在围栏内）。

    开启行可带信息串；闭合行须与开启行同字符、长度不小于开启长度、
    且标记后只允许空白——```` 围栏内的 ``` 与带信息串的行都不再翻转状态。
    """
    match = _FENCE.match(line)
    if not match:
        return open_fence
    marker, rest = match.group(1), match.group(2)
    if open_fence is None:
        return marker
    if marker[0] == open_fence[0] and len(marker) >= len(open_fence) and not rest.strip():
        return None
    return open_fence


def classify(lines: list[str]) -> list[str]:
    """单一行分类器：先注释、后围栏，避免多份状态机分歧导致失败开放。"""
    kinds: list[str] = []
    in_fence: str | None = None
    in_comment = False
    prev_blank = True
    in_indent = False
    for raw in lines:
        line = raw.rstrip("\r")
        if in_comment:
            kinds.append("c")
            if "-->" in line:
                in_comment = False
            prev_blank = False
            continue
        if _FENCE.match(line):
            next_fence = _fence_step(line, in_fence)
            if next_fence != in_fence:
                # 围栏边界行（开启或真正闭合）：本行按代码计
                in_fence = next_fence
                kinds.append("code")
                prev_blank = False
                in_indent = False
                continue
            # 匹配围栏正则但未翻转（同字符短围栏 / 错字符）：按围栏内代码计
            if in_fence is not None:
                kinds.append("code")
                prev_blank = False
                continue
            # 不在围栏内的「伪闭合」形态（如缩进 4 格的 ```` 行越过开启围栏）
            # 落到后续分类，不吞行
        if in_fence is not None:
            # 围栏内、非围栏标记行：一律按代码计（等价旧 in_fence 兜底分支）
            kinds.append("code")
            prev_blank = False
            continue
        if line.lstrip().startswith("<!--"):
            kinds.append("c")
            if "-->" not in line:
                in_comment = True
            prev_blank = False
            continue
        if in_indent:
            if not line.strip() or not _INDENT_CODE.match(line):
                in_indent = False
            else:
                kinds.append("code")
                prev_blank = False
                continue
        if not line.strip():
            kinds.append("b")
            prev_blank = True
            continue
        if prev_blank and _INDENT_CODE.match(line) and not _LIST.match(line):
            kinds.append("code")
            prev_blank = False
            in_indent = True
            continue
        if _HEADING.match(line):
            kinds.append("h")
        elif _HR.match(line):
            kinds.append("hr")
        elif _TABLE.match(line):
            kinds.append("t")
        elif _QUOTE.match(line):
            kinds.append("q")
        elif _LIST.match(line):
            kinds.append("l")
        else:
            kinds.append("p")
        prev_blank = False
    return kinds


def common_lines(lines: list[str], kinds: list[str]) -> set[int]:
    """公共段标记之间的行号；只认成对标记，孤立 begin 不得借后续 end。"""
    out: set[int] = set()
    stack: list[int] = []
    for index, kind in enumerate(kinds):
        if kind != "c":
            continue
        text = lines[index].strip()
        if text == COMMON_BEGIN:
            stack.append(index)
        elif text == COMMON_END and stack:
            start = stack.pop()
            out.update(range(start, index + 1))
    return out


def closure_state(lines: list[str]) -> tuple[bool, bool]:
    """返回 (未闭合围栏, 未闭合注释)；注释里的围栏字面量不翻转围栏状态。

    注释识别须以「不在围栏内」为前提（与 classify 先注释后围栏的顺序不同，
    此处围栏内行一律归围栏，围栏内的 <!-- 是代码内容）——否则 ````html
    示例块内以 <!-- 开头且无 --> 的行会开启注释状态，吞掉后续闭合围栏，
    误报「未闭合围栏 + 未闭合注释」双未闭合。
    """
    in_fence: str | None = None
    in_comment = False
    for raw in lines:
        line = raw.rstrip("\r")
        if in_comment:
            if "-->" in line:
                in_comment = False
            continue
        if in_fence is None and line.lstrip().startswith("<!--"):
            if "-->" not in line:
                in_comment = True
            continue
        if _FENCE.match(line):
            in_fence = _fence_step(line, in_fence)
            continue
    return in_fence is not None, in_comment


def is_break(lines: list[str], kinds: list[str], index: int) -> bool:
    """严格折点：只认散文段/列表项到散文续行，引用块续行不计。"""
    if index + 1 >= len(lines):
        return False
    if kinds[index] not in ("p", "l") or kinds[index + 1] != "p":
        return False
    head = lines[index].rstrip()
    tail = lines[index + 1].lstrip()
    if lines[index].endswith("  ") or head.endswith("\\") or not head or not tail:
        return False
    return bool(HAN.match(head[-1]) and (HAN.match(tail[0]) or OPEN_FW.match(tail[0])))


def display_width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(char) in ("W", "F") else 1 for char in text)


def split_cells(line: str) -> list[str]:
    """按未转义竖线切分；`\\|` 是内容不是分隔符。"""
    value = line.strip()
    cells: list[str] = []
    current: list[str] = []
    index = 0
    while index < len(value):
        char = value[index]
        if char == "\\" and index + 1 < len(value) and value[index + 1] == "|":
            current.append("\\|")
            index += 2
            continue
        if char == "|":
            cells.append("".join(current))
            current = []
            index += 1
            continue
        current.append(char)
        index += 1
    cells.append("".join(current))
    return cells


def table_cells(line: str) -> list[str]:
    """返回表格单元格；`> |` 引用块内表格同样识别。"""
    body: str | None = None
    if line.lstrip().startswith(">"):
        value = line
        while True:
            stripped = re.sub(r"^\s*>\s?", "", value)
            if stripped == value:
                break
            value = stripped
        if value.lstrip().startswith("|"):
            body = value
    elif _TABLE.match(line):
        body = line
    if body is None:
        return []
    cells = [cell.strip() for cell in split_cells(body)]
    if cells and cells[0] == "":
        cells = cells[1:]
    if cells and cells[-1] == "":
        cells = cells[:-1]
    return cells


def paragraph_widths(lines: list[str], kinds: list[str]) -> list[int]:
    """返回每个散文段/列表项的显示宽度（软口径报告用）。"""
    widths: list[int] = []
    index = 0
    while index < len(lines):
        kind = kinds[index]
        if kind not in ("p", "l"):
            index += 1
            continue
        width = display_width(lines[index].rstrip("\r"))
        index += 1
        while index < len(lines) and kinds[index] == "p":
            width += display_width(lines[index].rstrip("\r"))
            index += 1
        widths.append(width)
    return widths


def scan_shape(text: str, rel: str) -> dict[str, Any]:
    """扫描单个文档，返回 problems（阻断项）与 report（软口径报告项）。"""
    lines = text.replace("\r\n", "\n").split("\n")
    kinds = classify(lines)
    common = common_lines(lines, kinds)
    problems: list[str] = []

    folds = [
        index + 1
        for index in range(len(lines) - 1)
        if index not in common and (index + 1) not in common and is_break(lines, kinds, index)
    ]
    if folds:
        problems.append(f"折点 {len(folds)} 处：{folds[:10]}")

    fence_open, in_comment = closure_state(lines)
    if fence_open:
        problems.append("未闭合围栏")
    if in_comment:
        problems.append("未闭合注释")

    headings: list[tuple[int, int, str]] = []
    for index, kind in enumerate(kinds):
        if kind != "h":
            continue
        match = _HEADING.match(lines[index])
        if match:
            headings.append((index + 1, len(match.group(1)), lines[index].strip()[len(match.group(1)):].strip()))

    h2 = [item for item in headings if item[1] == 2]
    numbered = [title for _, _, title in h2 if re.match(r"^\d+\.", title)]
    if numbered:
        problems.append(f"编号 H2：{numbered}")

    toc_titles = [title for _, _, title in h2 if title == "目录"]
    if len(lines) >= 300 and not toc_titles:
        problems.append("长文档缺少 ## 目录")
    if len(lines) >= 300:
        toc_start = next(
            (line_no - 1 for line_no, level, title in headings if level == 2 and title == "目录"),
            None,
        )
        if toc_start is not None:
            toc_end = next(
                (line_no - 1 for line_no, level, _ in headings if level == 2 and line_no - 1 > toc_start),
                len(lines),
            )
            toc_links = {
                target for row in lines[toc_start:toc_end] for target in _LINK.findall(row)
            }
            required = [_slug(title) for _, _, title in h2 if title != "目录"]
            missing = [slug for slug in required if slug not in toc_links]
            if missing:
                problems.append(f"目录未覆盖：{missing}")

    for line_no, level, _ in headings:
        if level != 2:
            continue
        cursor = line_no
        while cursor < len(lines) and not lines[cursor].strip():
            cursor += 1
        if cursor < len(lines) and kinds[cursor] == "h":
            problems.append(f"节首即标题（L{line_no}）")

    wide = 0
    for row, kind in zip(lines, kinds):
        if kind not in ("t", "q"):
            continue
        for cell in table_cells(row):
            if display_width(cell) > 60:
                wide += 1

    return {
        "path": rel,
        "problems": problems,
        "wide_cells": wide,
        "max_paragraph": max(paragraph_widths(lines, kinds), default=0),
    }


def _parse_ls_files(stdout: bytes) -> list[str]:
    """NUL 边界切分 git ls-files -z 的字节输出，逐路径 os.fsdecode。

    不走文本模式：默认 core.quotePath=true 下非 ASCII 路径会被转义成
    "\346\226\207\346\241\243.md" 形态，守卫会拿转义名开文件直接 FileNotFoundError；
    且文本模式的通用换行转换会破坏二进制安全读法。os.fsdecode 按
    sys/filesystem encoding 还原路径（与 pathlib 打开文件的解码口径一致）。
    """
    return [os.fsdecode(path) for path in stdout.split(b"\0") if path]


def tracked_markdown() -> list[str]:
    """列出全部 tracked Markdown 的相对路径（字节安全解析）。"""
    with contextlib.chdir(ROOT):
        result = subprocess.run(
            ["git", "ls-files", "-z", "*.md"],
            capture_output=True,
            check=False,
        )
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"git ls-files 失败：{stderr}")
    return _parse_ls_files(result.stdout)


def ensure_files(files: list[str]) -> list[str]:
    if not files:
        raise AssertionError("git ls-files '*.md' 返回空：拒绝以空扫描放行")
    return files


class DocStyleGuardTest(unittest.TestCase):
    """全库 tracked Markdown 的形态守卫（0 容忍）。"""

    def test_all_tracked_markdown(self) -> None:
        files = ensure_files(tracked_markdown())
        problems: list[str] = []
        for rel in files:
            text = (ROOT / rel).read_text(encoding="utf-8")
            result = scan_shape(text, rel)
            for item in result["problems"]:
                problems.append(f"{rel}: {item}")
        self.assertEqual(problems, [], "文档形态守卫命中")

    def test_injection_samples_are_caught(self) -> None:
        samples = {
            "fold-han": ("# 标题\n\n不做密钥\n提取\n", "折点"),
            "fold-fullwidth": ("# 标题\n\n仅内存保存\n（不落盘）\n", "折点"),
            "fence": ("# 标题\n\n```python\nx = 1\n", "未闭合围栏"),
            "comment": ("# 标题\n\n<!-- 说明\n", "未闭合注释"),
            "numbered": ("# 标题\n\n## 1. 安装\n\n正文。\n", "编号 H2"),
            "section-gap": ("# 标题\n\n## 节\n\n### 子节\n\n正文。\n", "节首即标题"),
            "missing-toc": ("# 标题\n\n## 节\n\n" + "正文。\n" * 310, "缺少 ## 目录"),
            # 四反引号围栏内的三反引号行是内容不是闭合；围栏在尾部四反引号处才闭合
            "nested-fence": (
                "# 标题\n\n````\n内部 ``` 示例\n```\n内部行\n````\n\n中文甲\n乙中文\n",
                "折点",
            ),
            # 闭合标记短于开启标记（``` 开、`` 后）不闭合
            "short-close": ("# 标题\n\n````\n代码\n``\n\n中文甲\n乙中文\n", "未闭合围栏"),
            # 带信息串的行不是闭合行
            "close-with-info": ("# 标题\n\n```\n代码\n```python\n\n中文甲\n乙中文\n", "未闭合围栏"),
        }
        for name, (text, expected) in samples.items():
            with self.subTest(name=name):
                problems = scan_shape(text, name)["problems"]
                self.assertTrue(
                    any(expected in item for item in problems),
                    f"{name} 未被拦住：{problems}",
                )

    def test_clean_sample_passes(self) -> None:
        text = "# 标题\n\n## 目录\n\n- [节](#节)\n\n## 节\n\n一句话结论。\n"
        self.assertEqual(scan_shape(text, "clean")["problems"], [])

    def test_parse_ls_files_bytes(self) -> None:
        # 非 ASCII 路径与结尾 NUL：文本模式会把前者转义成 "\346..." 形态，
        # 守卫拿转义名开文件直接 FileNotFoundError；字节解析必须原样还原
        name = "文档.md"
        raw = name.encode("utf-8") + b"\0" + b"README.md\0"
        self.assertEqual(_parse_ls_files(raw), [name, "README.md"])
        self.assertEqual(_parse_ls_files(b""), [])

    def test_fence_interior_comment_not_misclassified(self) -> None:
        # 围栏内的 <!-- 是代码内容：不得开启注释状态吞掉闭合围栏，
        # 否则围栏内以 <!-- 开头且块内无 --> 的合法文档被误报
        # 「未闭合围栏 + 未闭合注释」双未闭合
        text = (
            "# 标题\n\n## 目录\n\n- [节](#节)\n\n## 节\n\n一句话结论。\n\n"
            "```html\n<!-- 多行注释第一行\n第二行无闭合标记\n```\n\n结尾。\n"
        )
        result = scan_shape(text, "fence-comment")
        self.assertEqual(result["problems"], [])

    def test_common_segment_pairing(self) -> None:
        lines = [
            COMMON_BEGIN,
            "中文甲",
            "乙中文",
            COMMON_BEGIN,
            "中文丙",
            "丁中文",
            COMMON_END,
        ]
        exempt = common_lines(lines, classify(lines))
        self.assertEqual(exempt, {3, 4, 5, 6})

    def test_report_metrics(self) -> None:
        text = (
            "# 标题\n\n## 节\n\n结论。\n\n"
            "| 列一 | 列二 |\n|---|---|\n| 值 | " + "长" * 40 + " |\n\n" + "长" * 200 + "\n"
        )
        result = scan_shape(text, "report")
        self.assertGreaterEqual(result["wide_cells"], 1)
        self.assertGreaterEqual(result["max_paragraph"], 400)


if __name__ == "__main__":
    unittest.main()
