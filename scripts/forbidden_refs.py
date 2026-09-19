"""编号引用扫描 — 禁止在注释 / 文档 / 提交信息中夹带仓库外不可访问的编号指针。

背景：审查报告与本地计划产物（计划文件、会话产物目录）等载体不在仓库内，
读者无法据编号还原上下文。注释应当解释「为什么」与失败模式，而不是指向一份
外部文档的条目号；提交信息应当描述行为变化，而不是流水号。

命中规则（详见 _RULES，示例一律写成占位形态，避免规则文本自我命中）：
  1. 组合编号：中文方括号包裹的「序号 · 条目码」形态；
  2. 归因信号词 + 任意编号：复核 / 审查报告 / 审计 / 排期 后面跟编号；
  3. 条目码：单个大写字母前缀（P / F / C / U / B / S 等）+ 数字，可带次级编号；
  4. 单字母码 + 冒号：字母数字码后紧跟中文或英文冒号；
  5. 计划 / 会话产物路径：本地计划文件名、会话产物目录名；
  6. 流水号批次：第 N 批。

豁免（不构成编号引用）：编码名（UTF-8）、RFC 编号、sha256、C0/C1 控制字符、
静态检查码指令及其码表、少量固定技术缩写（见 _EXEMPT）、指标语境下的 F 值名，
可跟踪的 issue 编号（形如「#123」，无归因信号词时本就不会命中）、依赖版本号，
以及行内显式豁免标记 allow-plan-ref（用于确实需要保留编号的场景）。豁免片段
是**剥离后再扫**，同行其它编号照常判定。JS 的行内尾注释与整行注释同样纳入。
提交信息只扫会真正进入提交的部分（`#` 注释行与 scissors 之后的 diff 不算）。

用法：
  python scripts/forbidden_refs.py            # 扫描 staged 新增内容（pre-commit）
  python scripts/forbidden_refs.py --ref origin/master   # CI：相对基线的差异
  python scripts/forbidden_refs.py --tree     # 全量工作区（收口核查）
  python scripts/forbidden_refs.py --message-file <路径> # 提交信息（commit-msg）

退出码：0 = 无命中；1 = 命中；2 = 扫描未执行（git 取差异失败，拒绝放行——
空 diff 不等于干净，与 scripts/secret_scan.py 同口径）。
"""

from __future__ import annotations

import argparse
import ast
import io
import re
import subprocess
import sys
import tokenize
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

# CI Windows runner 默认 stdout 为 cp1252，中文输出会 UnicodeEncodeError
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001, S110
        pass

_CJK = re.compile(r"[\u3400-\u9fff\uf900-\ufaff\u3000-\u303f\uff00-\uffef]")

# 规则 1：组合编号（中文方括号 + 序号 + 分隔符 + 条目码）
_R_COMBO = r"【\s*\d+\s*[·・]\s*P?\d+\s*】"
# 规则 2：归因信号词 + 编号。数字后接中文量词（其次 / 第 3 条）不算编号引用。
_R_ATTRIB = (
    r"(?:复核|审查报告|审计|排期)\s*[【#]?\s*"
    r"(?:P\d+(?:[-\u2013\u2014·]\d+)?|[A-Z]{1,2}-?\d{1,3}|#?\d{1,4})"
    # (?!\d) 防回溯：四位年份不能被截成前三位而绕过后面的量词排除
    r"(?!\d)(?!\s*[次条个轮遍张年月日])"
)
# 规则 3：条目码（1-2 个大写字母 + 数字，可带次级编号）。仅大写：小写 v1 之类',
# 依赖版本号不命中；三字母以上的缩写（RFC8601）因前缀长度限制不命中。
# 数字位限 1-2：静态检查码（F401 / I001 / UP031 / BLE001）为三位数字，不命中。
_R_CODE = r"(?<![A-Za-z0-9_])[A-Z]{1,2}[-\u2013\u2014·]?\d{1,2}(?![A-Za-z0-9_])"
# 规则 4：单字母码 + 冒号（仅用于注释 / 文档语境，见 _looks_like_prose）
_R_COLON = r"(?<![A-Za-z0-9_])[A-Z]\d{1,2}\s*[：:]"
# 规则 5：计划 / 会话产物路径
_R_PATH = r"(?:IMPLEMENTATION-)?PLAN-[A-Z0-9]|TODO-[A-Z0-9]|\.zcode\b|plan-sess_"
# 规则 6：流水号批次
_R_BATCH = r"第\s*[0-9一二三四五六七八九十]+\s*批"

_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("组合编号", re.compile(_R_COMBO)),
    ("归因词+编号", re.compile(_R_ATTRIB)),
    ("条目码", re.compile(_R_CODE)),
    ("字母码+冒号", re.compile(_R_COLON)),
    ("计划产物路径", re.compile(_R_PATH)),
    ("流水号批次", re.compile(_R_BATCH)),
)

# 规则 4 只在「注释 / 文档」语境下生效：代码里的分支标签（如 case 缩写码）不是编号引用。
_COLON_RULE_ONLY_PROSE = True

# 豁免片段：命中后从文本中**剥离**再套规则，而不是整行放行——整行放行会让
# 「静态检查码指令 —— 见复核 <条目码>」这类同行夹带的真实编号也一起溜过。
_EXEMPT = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"UTF-\d+",
        r"RFC\s*\d+",
        r"\bsha\d+\b",
        r"\bC0\b",
        r"\bC1\b",
        # 静态检查码（ruff / flake8）连同其后的码表一起剥掉
        r"\bnoqa\b:?\s*(?:[A-Z]+\d+(?:\s*,\s*[A-Z]+\d+)*)?",
        # 常见技术词：形态与条目码相同（1-2 个大写字母 + 1-2 位数字），但仓库
        # 读者一望即知不是编号。白名单只收固定写法；季度/芯片型号这类既是
        # 术语又是真实条目码形态的**不**收，需要时用 allow-plan-ref。
        r"\bES\d+\b",  # ES6 / ES2015
        r"\bMD5\b",
        r"\bMP[34]\b",
        r"\bP(?:50|75|90|95|99)\b",  # 延迟分位
        r"\bA4\b",  # 纸张
        r"\bEC2\b",
    )
)
# 领域术语：F1 既是审查条目码，也是分类/判重指标（精确率与召回率的调和平均），
# 出现在指标语境中不是编号引用——只剥离该指标名本身，同行其它编号照常判定。
_DOMAIN_TERM = re.compile(
    r"(?:精确率|召回率|混淆|调和|accuracy|precision|recall)", re.IGNORECASE
)
_DOMAIN_TOKEN = re.compile(r"\bF1\b")
# 行内豁免标记：确实需要保留编号时（如引用仓库内文件里的真实标识符）
_ALLOW_MARKER = "allow-plan-ref"

_TEXT_SUFFIXES = (".py", ".js", ".mjs", ".md", ".yml", ".yaml", ".toml", ".ps1", ".sh")


@dataclass(frozen=True)
class Hit:
    """一条命中：路径 + 行号 + 命中的规则名（可多条，用 + 连接）+ 该行原文。"""

    path: str
    line: int
    rule: str
    text: str


def _looks_like_prose(line: str) -> bool:
    """判断一行是否处于「注释 / 文档」语境：以注释符号开头，或含中日韩文字。

    用于增量（diff）扫描：diff 里拿不到完整语法上下文，代码行（如分支标签）
    不应因规则 4 被误判；而注释与文档行必然以注释符号开头或含中文。
    """
    stripped = line.lstrip()
    if stripped.startswith(("#", "//", "*", "/*", "<!--", ";;")):
        return True
    return bool(_CJK.search(line))


def _collect_hits(path: str, line: int, text: str, *, prose: bool = True) -> list[Hit]:
    """对单个「注释 / 文档片段」套用全部规则。"""
    if _ALLOW_MARKER in text:
        return []
    stripped = text
    for rx in _EXEMPT:
        stripped = rx.sub(" ", stripped)
    if _DOMAIN_TERM.search(stripped):
        stripped = _DOMAIN_TOKEN.sub(" ", stripped)
    matched: list[str] = []
    for rule, rx in _RULES:
        if rule == "字母码+冒号" and (_COLON_RULE_ONLY_PROSE and not prose):
            continue
        if rx.search(stripped):
            matched.append(rule)
    if not matched:
        return []
    # 同一行只报一条（规则名合并），避免基线计数与报告重复膨胀
    return [Hit(path=path, line=line, rule="+".join(matched), text=text.strip())]


def _python_segments(source: str) -> list[tuple[int, str]]:
    """提取 Python 源码中的注释与 docstring 片段（不含普通字符串字面量）。"""
    segments: list[tuple[int, str]] = []
    try:
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type == tokenize.COMMENT:
                segments.append((token.start[0], token.string))
    except (tokenize.TokenError, IndentationError):
        # 语法不完整（如 diff 片段）：退化为逐行注释识别
        for lineno, line in enumerate(source.splitlines(), 1):
            if line.lstrip().startswith("#"):
                segments.append((lineno, line))
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return segments
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            continue
        doc = ast.get_docstring(node, clean=False)
        if not doc:
            continue
        start = node.body[0].lineno
        for offset, text in enumerate(doc.splitlines()):
            segments.append((start + offset, text))
    return segments


def _line_comment_segments(source: str, markers: tuple[str, ...]) -> list[tuple[int, str]]:
    """按行注释符号提取片段（JS / shell / yml / toml / ps1）。"""
    return [
        (lineno, line)
        for lineno, line in enumerate(source.splitlines(), 1)
        if line.lstrip().startswith(markers)
    ]


_JS_LINE_START = ("//", "*", "/*")


def _js_trailing_comment_segments(source: str) -> list[tuple[int, str]]:
    """提取 JS 代码行末尾的 `// ...` 注释（整行注释由 _line_comment_segments 负责）。

    `//` 之前的代码里三种引号都成对时才视为注释起点，排除字符串/模板串内的
    `//`（如 URL）；`://` 视为 URL 不切。正则字面量里的 `//` 极少见，不处理。
    此前只扫整行注释，`let a = 1; // <条目码>` 在三种模式下都放行。
    """
    segments: list[tuple[int, str]] = []
    for lineno, line in enumerate(source.splitlines(), 1):
        if line.lstrip().startswith(_JS_LINE_START):
            continue
        start = 0
        while True:
            idx = line.find("//", start)
            if idx < 0:
                break
            prefix = line[:idx]
            if idx > 0 and line[idx - 1] == ":":
                start = idx + 2
                continue
            if all(prefix.count(q) % 2 == 0 for q in ('"', "'", "`")):
                segments.append((lineno, line[idx:]))
                break
            start = idx + 2
    return segments


def _block_comment_segments(
    source: str, opener: str, closer: str
) -> list[tuple[int, str]]:
    """提取块注释片段（JS 的 /* */）。行内的末尾注释由调用方另行处理。"""
    segments: list[tuple[int, str]] = []
    inside = False
    for lineno, line in enumerate(source.splitlines(), 1):
        if inside or opener in line:
            segments.append((lineno, line))
            if closer in line and not inside:
                continue
        if opener in line and closer not in line.split(opener, 1)[1]:
            inside = True
        elif inside and closer in line:
            inside = False
    return segments


def _fenced_lines(source: str) -> list[tuple[int, str]]:
    """Markdown：跳过 ``` 围栏内的代码块，其余行视为正文。"""
    segments: list[tuple[int, str]] = []
    fence = False
    for lineno, line in enumerate(source.splitlines(), 1):
        if line.lstrip().startswith("```"):
            fence = not fence
            continue
        if not fence:
            segments.append((lineno, line))
    return segments


def iter_segments(path: str, source: str) -> list[tuple[int, str]]:
    """按文件类型提取需要检查的「注释 / 文档」片段。"""
    suffix = Path(path).suffix.lower()
    if suffix == ".py":
        return _python_segments(source)
    if suffix in (".js", ".mjs"):
        return (
            _line_comment_segments(source, _JS_LINE_START)
            + _js_trailing_comment_segments(source)
            + _block_comment_segments(source, "/*", "*/")
        )
    if suffix == ".md":
        return _fenced_lines(source)
    if suffix in (".yml", ".yaml", ".toml", ".ps1", ".sh"):
        return _line_comment_segments(source, ("#",))
    return []


def scan_source(path: str, source: str) -> list[Hit]:
    """扫描单个文件内容的注释 / 文档片段。"""
    hits: list[Hit] = []
    seen: set[tuple[int, int]] = set()
    for lineno, text in iter_segments(path, source):
        key = (lineno, hash(text))
        if key in seen:
            continue
        seen.add(key)
        hits.extend(_collect_hits(path, lineno, text))
    return hits


def scan_text(path: str, text: str) -> list[Hit]:
    """扫描「片段文本」（提交信息 / diff 新增行），按语境宽松判定。"""
    hits: list[Hit] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        hits.extend(_collect_hits(path, lineno, line, prose=_looks_like_prose(line)))
    return hits


def _git(args: list[str], *, cwd: Path | None = None) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败：{proc.stderr.strip()}")
    return proc.stdout


def tracked_files(root: Path | None = None) -> list[str]:
    """列出纳入扫描的跟踪文件（排除未跟踪的本地计划产物）。

    在 root 下执行 git：ls-files 输出相对**当前目录**的路径，若从子目录
    （如 tests/）跑 pytest 而 git 仍在 cwd 执行，拼到 root 后文件不存在，
    读取失败被静默跳过，全量扫描零命中「通过」——等于没扫。
    """
    out = _git(["ls-files"], cwd=root)
    return [
        line
        for line in out.splitlines()
        if line and line.lower().endswith(_TEXT_SUFFIXES)
    ]


def scan_tree(root: Path | None = None) -> list[Hit]:
    """全量扫描跟踪文件（收口核查用；基线机制见 tests/test_no_plan_refs.py）。"""
    base = root or Path.cwd()
    hits: list[Hit] = []
    for rel in tracked_files(base):
        path = base / rel
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        hits.extend(scan_source(rel, source))
    return hits


_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def _added_lines(diff_text: str) -> dict[str, set[int]]:
    """从 unified diff 解析「新增行的新文件行号」：{路径: {行号}}。"""
    added: dict[str, set[int]] = {}
    current: str | None = None
    lineno = 0
    for line in diff_text.splitlines():
        if line.startswith("+++ "):
            raw = line[4:].strip()
            current = None if raw == "/dev/null" else raw.removeprefix("b/")
            if current is not None:
                added.setdefault(current, set())
            continue
        if line.startswith("@@"):
            match = _HUNK_RE.match(line)
            lineno = int(match.group(1)) if match else 0
            continue
        if current is None or lineno == 0:
            continue
        if line.startswith("+"):
            added[current].add(lineno)
        if line.startswith(("+", " ")):
            lineno += 1
    return added


def _file_at_rev(rev: str, path: str) -> str | None:
    """取某版本下的文件内容；失败（新增/删除文件）返回 None。"""
    try:
        return _git(["show", f"{rev}:{path}"])
    except RuntimeError:
        return None


def scan_lines(path: str, source: str, lines: set[int]) -> list[Hit]:
    """在给定的行号集合上扫描该文件内容（增量定位的纯函数形态）。

    与 scan_diff 的分工：这里只做「内容 → 命中 → 按行号过滤」，不碰 git；
    这样增量定位逻辑可以被无副作用地单测，不需要改动索引或工作区。
    """
    return [hit for hit in scan_source(path, source) if hit.line in lines]


def scan_diff(diff_text: str, *, rev: str | None, staged: bool) -> list[Hit]:
    """扫描增量变更：只报新增行上的命中。

    定位方式：先解析新增行行号，再取该文件的**完整版本**做语法分段，
    只保留落在新增行上的命中——这样 docstring 内的行号依然精确，
    且规则 4 仍能按「注释 / 文档语境」判定。

    版本选择：行号是**新文件**（HEAD / 工作区）的行号，因此内容必须取
    「新增侧」的版本——staged 取索引（:0），CI 比较基线...HEAD 时取 HEAD。
    取基线 ref 会把新行号套到旧内容上，报出一堆「本轮已删除」的假命中。
    取不到（文件为新增或删除）时跳过该文件。
    """
    hits: list[Hit] = []
    for path, lines in _added_lines(diff_text).items():
        if not lines:
            continue
        source = _file_at_rev(":0" if staged else "HEAD", path)
        if source is None:
            continue
        hits.extend(scan_lines(path, source, lines))
    return hits


_SCISSORS = "------------------------ >8 ------------------------"


def strip_git_commentary(message: str) -> str:
    """去掉 COMMIT_EDITMSG 里不会进入提交的部分：`#` 注释行与 scissors 之后的 diff。

    `git commit -v` 会把 diff 附在 scissors 线之后、模板注释以 `#` 开头，git 生成
    提交时全部剥掉；钩子若照扫，diff 里代码的技术缩写会拦下一个根本不会入库
    的内容。与 git 默认 commit.cleanup=strip 同口径（自定义 commentChar 不处理）。
    """
    kept: list[str] = []
    for line in message.splitlines():
        if line.startswith("#"):
            if _SCISSORS in line:
                break
            continue
        kept.append(line)
    return "\n".join(kept)


def scan_commit_message(message: str) -> list[Hit]:
    """扫描提交信息（subject + body；git 注释行与 scissors 之后不算）。"""
    return scan_text("<commit-message>", strip_git_commentary(message))


def _format(hits: Iterable[Hit]) -> str:
    return "\n".join(
        f"  {hit.path}:{hit.line} [{hit.rule}] {hit.text[:120]}" for hit in hits
    )


def _staged_diff() -> str:
    return _git(["diff", "--cached", "--unified=0"])


def _range_diff(ref: str) -> str:
    return _git(["diff", "--unified=0", f"{ref}...HEAD"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="禁止编号引用扫描")
    parser.add_argument("--ref", default=None, help="扫描相对该基线的差异（CI 用）")
    parser.add_argument("--tree", action="store_true", help="全量扫描跟踪文件")
    parser.add_argument("--message-file", default=None, help="扫描提交信息文件")
    args = parser.parse_args(argv)

    # git 取不到差异时必须拒绝放行：当作空 diff 会「扫描没跑却通过」，
    # 与 scripts/secret_scan.py 同口径（退出码 2）。CI 里 fetch 失败或基线
    # 引用无效都会走到这里，静默通过等于门禁失守。
    try:
        if args.message_file:
            text = Path(args.message_file).read_text(encoding="utf-8", errors="replace")
            hits = scan_commit_message(text)
            target = f"提交信息 {args.message_file}"
        elif args.tree:
            hits = scan_tree()
            target = "全量跟踪文件"
        elif args.ref:
            hits = scan_diff(_range_diff(args.ref), rev=args.ref, staged=False)
            target = f"相对 {args.ref} 的差异"
        else:
            hits = scan_diff(_staged_diff(), rev=None, staged=True)
            target = "staged 新增内容"
    except (RuntimeError, OSError) as e:
        # OSError：提交信息文件读不到（钩子参数错误等）——同样属于「扫描没跑」
        print(
            f"[forbidden-refs] 扫描未执行，拒绝放行：{e}\n"
            "请检查 git 环境/基线引用/提交信息文件后重试",
            file=sys.stderr,
        )
        return 2

    if hits:
        print(f"检测到编号引用（{target}），请改为说明「为什么」与失败模式：")
        print(_format(hits))
        print(
            "\n编号指向仓库外不可访问的文档，读者无法据此还原上下文。"
            "\n确需保留时，请在同行加注释 allow-plan-ref（并在 review 中说明理由）。"
        )
        return 1
    print("未发现编号引用")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
