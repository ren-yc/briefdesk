"""编号引用扫描 — 禁止在注释 / 文档 / 提交信息中夹带仓库外不可访问的编号指针。

背景：审查报告与本地计划产物（计划文件、会话产物目录）等载体不在仓库内，
读者无法据编号还原上下文。注释应当解释「为什么」与失败模式，而不是指向一份
外部文档的条目号；提交信息应当描述行为变化，而不是流水号。

命中规则（详见 _RULES，示例一律写成占位形态，避免规则文本自我命中）：
  1. 组合编号：中文方括号包裹的「序号 · 条目码」形态；
  2. 归因信号词 + 任意编号：复核 / 审查报告 / 审计 / 排期 / 决策 后面跟编号
     （编号含带圈数字）；
  3. 条目码：单个大写字母前缀（P / F / C / U / B / S 等）+ 数字，可带次级编号，
     也可带一个分辨子项的小写后缀（「字母 + 数字 + 小写字母」是审查报告里把同一
     条目拆成子项的常见写法）；
  3b. 反置条目码：数字在前、大写字母在后（外部决策表序号的常见形态），
      只在「见 / 决策 / 条目 / 序号 / 表」一类指针词邻近时判定——裸的「数字+字母」
      与 3D / 5G / 2FA 这类技术名词同形，按形状硬判会连正常行文一起拦下；
  4. 单字母码 + 冒号：字母数字码后紧跟中文或英文冒号；
  5. 计划 / 会话产物路径：本地计划文件名、会话产物目录名；
  6. 流水号批次：第 N 批；
  7. 带圈数字 + 等号 + 码：把就地圈码绑到外部条目表的组合；
  8. 章节指针：§N / §N.M（仓库内没有带章节号的文档；公开标准的章节号豁免）；
  9. 裸归因短语：「审查」紧接「修复 / 回归」且处在标签位（后随冒号 / 括号 / 编号 /
     行尾），无需编号即指向仓外报告；
  10. 声称的仓库文档不存在：注释里点名的 `.md` 文件在本次扫描的树里找不到。
      这一条不是猜文件名，而是可证伪的事实——读者按名字去仓库里找不到，注释就
      等于指向仓外材料（真实发生过：注释引用已被删除的提示词目录与外部缺陷清单）。
      只在全量扫描生效：增量模式手里没有稳定的树快照，判「不存在」会误伤同时
      新增的文件；全量扫描随 pytest 与 --tree 必跑，足以兜住存量。

豁免（不构成编号引用）：编码名（UTF-8）、RFC 编号与公开标准的章节号（RFC 5987 §3.2）、
sha256、C0/C1 控制字符、静态检查码指令及其码表、少量固定技术缩写（见 _EXEMPT）、
容量单位（数字+MB/GB 一类封闭后缀）、千次记法（数字+K）、HTTP 状态类通配（数字+XX）、
十六进制颜色字面量（#RRGGBB，至少含一位 A-F），指标语境下的 F 值名，可跟踪的 issue
编号（形如「#123」，无归因信号词时本就不会命中）、依赖版本号，以及行内显式豁免标记
allow-plan-ref（用于确实需要保留编号的场景）。豁免片段是**剥离后再扫**，同行其它编号
照常判定。采集面：JS 的行内尾注释、整行注释与块注释（逐字符状态机，字符串 / 模板串
里的 `//` `/*` 不算注释开头），Python 的**所有**字符串表达式语句（不止各节点首位的
docstring，类体中部的说明串同样扫），CSS / HTML 的块注释（`/* */`、`<!-- -->`），
以及 `.example` 模板文件的 `#` 行注释。
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
# 规则 2：归因信号词 + 编号。数字后接中文量词（其次 / 第 3 条 / 复核 3 处）不算编号
# 引用——量词表要把常用的一并收进来，漏一个就让「决策 3 项待定」「复核 2 处调用点」
# 这类正常行文被拦下，作者只能改写或加 allow-plan-ref，反而稀释这个机制。
# 「决策」入词表：后接 ① / 编号时是「外部决策表的第 N 条」这一指针形态；
# 单独短语（「注册决策顺序」「设计决策」）不命中——规则要求词后紧跟编号。
# 带圈数字入编号侧：决策表把圈码当条目序号的存量即此形态。
_R_ATTRIB = (
    r"(?:复核|审查报告|审计|排期|决策)\s*[【#]?\s*"
    r"(?:P\d+(?:[-\u2013\u2014·]\d+)?|[A-Z]{1,2}-?\d{1,3}|[\u2460-\u2469]|#?\d{1,4})"
    # (?!\d) 防回溯：四位年份不能被截成前三位而绕过后面的量词排除
    r"(?!\d)(?!\s*[次条个轮遍张年月日项处步类种版页周位人篇段行倍秒分千层档件套组家所章节句字])"
)
# 规则 3：条目码（1-2 个大写字母 + 数字，可带次级编号）。仅大写：小写 v1 之类',
# 依赖版本号不命中；三字母以上的缩写（RFC8601）因前缀长度限制不命中。
# 数字位限 1-2：静态检查码（F401 / I001 / UP031 / BLE001）为三位数字，不命中。
# 允许一个尾随小写字母：审查报告常把同一条目拆成子项（字母 + 数字 + 小写字母），
# 不放行时这类子项码整体"隐身"——存量里就有人照着它写注释而门禁毫无反应。
# 误伤面已实测：放开后全库仅命中真实子项码，ES6 / MP3 / F401 一类各自被前缀长度
# 或既有豁免挡住。
_R_CODE = r"(?<![A-Za-z0-9_])[A-Z]{1,2}[-\u2013\u2014·]?\d{1,2}[a-z]?(?![A-Za-z0-9_])"
# 规则 3b：反置条目码（1-2 位数字 + 1-2 个大写字母）——外部决策表的序号形态
# 是数字在前（「1」+字母），规则 3 结构上看不见它。
# 判定要求**指针语境**：码前面紧跟「见 / 决策 / 条目 / 序号 / 表」一类词。裸的
# 「数字+1-2 字母」与常见技术量纲同形且无法区分——3D / 5G / 2FA / 1M 行在技术
# 注释里很常见，按形状硬判会把它们一起拦下（合法提交被迫改写或加 allow-plan-ref，
# 反而稀释这个机制）。代价是孤立的「1B」不再命中：那种写法本身没有上下文，
# 读者也还原不出它指什么。量纲豁免（MB/GB、K、XX）仍在 _EXEMPT 里剥离。
_R_CODE_REV = (
    r"(?:参见|见|依据|按照|决策表|决策|条目|序号|表格|表)\s*"
    r"\d{1,2}[A-Z]{1,2}(?![A-Za-z0-9_])"
)
# 规则 7：带圈数字 + 等号 + 码——把就地圈码绑到外部条目表的组合才是指针。
# 单独出现的圈码是仓库文档的正当编号手段（architecture/dedup 大量使用），
# 不命中：等号右侧限 1-2 位数字（可带字母后缀），「①=24px」这类不会误伤。
_R_CIRCLED = r"[\u2460-\u2469]\s*[=＝]\s*\d{1,2}[A-Z]?(?![A-Za-z0-9_])"
# 规则 8：章节指针——仓库内没有带章节号的文档，出现即指向仓外材料。
# 两个例外：上游版本钉定的 API 文档（行内 allow-plan-ref 保留）；公开标准的
# 章节号（在 _EXEMPT 里连同标准名一起剥离，与「RFC 编号」同等对待）。
_R_SECTION = r"§\s*\d+(?:\.\d+)?"
# 规则 9：裸归因短语——「审查」与「修复 / 回归」的连写形态，即使没跟编号也指向
# 仓外审查报告。判定要求它处在**标签位**：后随冒号 / 右括号 / 顿号 / 编号 / 行尾。
# 「代码审查修复了竞态」这类动词连写没有标签形态，不命中（早期版本按子串硬判，
# 把正常行文也拦下了）。
_R_ATTRIB_PHRASE = r"(?:审查修复|审查回归)(?=\s*(?:[：:）)、,，]|[#【]|$))"
# 规则 4：单字母码 + 冒号（仅用于注释 / 文档语境，见 _looks_like_prose）
_R_COLON = r"(?<![A-Za-z0-9_])[A-Z]\d{1,2}\s*[：:]"
# 规则 5：计划 / 会话产物路径
_R_PATH = r"(?:IMPLEMENTATION-)?PLAN-[A-Z0-9]|TODO-[A-Z0-9]|\.zcode\b|plan-sess_"
# 规则 6：流水号批次
_R_BATCH = r"第\s*[0-9一二三四五六七八九十]+\s*批"
# 规则 10 的取材模式：注释里点名的 `.md`（可带目录前缀）。首个字符必须是字母数字，
# 否则 `*-api.md` 这类通配写法会被截成 `-api.md` 当成一个具体文件。
_R_MD_REF = re.compile(r"(?<![\w./-])(?:[\w-]+/)*\w[\w-]*\.md\b")
# 带 URL 的行不套规则 10：上游文档镜像的正文不在仓内，但链接指向可公开回查的地址
# （这也是本仓对上游 API 文档的唯一引用方式），点名的文件名属于链接标签而非仓内路径。
_URL_LIKE = re.compile(r"https?://|github\.com/|\bblob/")

_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("组合编号", re.compile(_R_COMBO)),
    ("归因词+编号", re.compile(_R_ATTRIB)),
    ("条目码", re.compile(_R_CODE)),
    ("反置条目码", re.compile(_R_CODE_REV)),
    ("带圈数字=码", re.compile(_R_CIRCLED)),
    ("章节指针", re.compile(_R_SECTION)),
    ("裸归因短语", re.compile(_R_ATTRIB_PHRASE)),
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
        # 公开标准的章节号（RFC 5987 里的 3.2 节一类）：标准名与章节号都可公开
        # 回查，与 RFC 编号本身同等对待。必须排在下面的「RFC 编号」之前——剥离
        # 是顺序执行的，先剥掉标准名就再也认不出后面那个章节指针。
        # 剥离范围限「标准名 + 章节号」整段，无标准名的孤立章节指针照常命中。
        r"(?:RFC|ISO|IEC|IEEE|ECMA|ANSI|POSIX|Unicode|W3C)\s*[\d.]*\s*§\s*\d+(?:\.\d+)?",
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
        # 容量单位（1.6MB / 16MB/回填批 / 1GB 上限）：数字+大写字母与反置条目码
        # 同形，但单位字母表封闭（KB/MB/GB/TB），读者一望即知不是编号。
        r"\d+(?:\.\d+)?(?:KB|MB|GB|TB)\b",
        # 千次记法 / K 作变量（「3K 次」= 3×键数 K 的构建次数）：同为
        # 数字+单字母，但后随量词「次」或独立成词，不是码。
        r"(?<![A-Za-z0-9_])\d+K(?![A-Za-z0-9_])",
        # HTTP 状态类通配（1XX/2XX/…/5XX，vendor 生成代码的注释）：
        # 数字+双字母形态与反置条目码相同，但 X 不是条目码的数字后缀。
        r"(?<![A-Za-z0-9_])[1-5]XX(?![A-Za-z0-9_])",
        # CSS / 十六进制颜色字面量（#F59E0B / #fff）：进入 .css 扫描面后与
        # 「#数字+字母」形态混淆，按颜色语法整体剥离。
        # 要求 3-8 位里**至少含一个 A-F**：纯数字的 3 位或 4 位写法不是颜色语法
        # 的必要形态，却正好是「归因词 + #号数字」这类 issue 编号——早期版本按
        # `#[0-9A-F]{3,8}` 整段吞掉（IGNORECASE 下纯数字也算），把旧版能拦的
        # 归因编号一起吃掉了。「#数字+字母」的三色位仍会被放过：它确实是合法 CSS
        # 颜色写法，宁可漏一个可疑条目码，也不误伤样式表。
        r"#(?=[0-9A-F]{3,8}\b)[0-9A-F]*[A-F][0-9A-F]*",
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

# 纳入扫描面的文本后缀。`.example` 与 `.html` 此前在面外，`.env.example` 里的
# 「见问题报告 §N」与 `index.html` 的 HTML 注释因此对门禁失明（前者是真实漏拦）。
# `.svg` 有意不在内：全部是上游图标库的逐字节副本，注释里没有本仓要守的东西。
_TEXT_SUFFIXES = (
    ".py",
    ".js",
    ".mjs",
    ".md",
    ".yml",
    ".yaml",
    ".toml",
    ".ps1",
    ".sh",
    ".css",
    ".html",
    ".example",
)


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


def _collect_hits(
    path: str,
    line: int,
    text: str,
    *,
    prose: bool = True,
    known_paths: frozenset[str] | None = None,
) -> list[Hit]:
    """对单个「注释 / 文档片段」套用全部规则。

    `known_paths` 非空时额外套用规则 10（注释声称的 `.md` 必须在树内）：这一条
    需要文件树，只有全量扫描能给；增量模式传 None，规则自动休眠。
    """
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
    if known_paths is not None and not _URL_LIKE.search(text):
        for ref in _R_MD_REF.finditer(stripped):
            token = ref.group(0)
            if token in known_paths or Path(token).name in known_paths:
                continue
            matched.append("缺失文档")
            break
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
    # 采集面 = **所有**作为表达式语句出现的字符串，而不是只取各节点 body[0]
    # 的真 docstring。失败模式（真实发生过）：测试类把说明写成类体里的裸字符串，
    # 位置在 autouse fixture 之后——ast.get_docstring 取不到它，整段文字对门禁
    # 完全失明，违规编号就这样留在仓库里（回归见 tests/test_no_plan_refs.py 的
    # test_non_leading_string_expression_is_collected）。
    # 代价是普通多行字符串字面量（如 HTML 夹具）也进扫描面：豁免表与「非文档
    # 语境」的判断因此必须一起扩，宁可多报由人工确认，不做静默放过。
    for node in ast.walk(tree):
        if not isinstance(node, ast.Expr):
            continue
        value = node.value
        if not (isinstance(value, ast.Constant) and isinstance(value.value, str)):
            continue
        for offset, text in enumerate(value.value.splitlines()):
            segments.append((node.lineno + offset, text))
    return segments


def _line_comment_segments(source: str, markers: tuple[str, ...]) -> list[tuple[int, str]]:
    """按行注释符号提取片段（shell / yml / toml / ps1 / .example）。"""
    return [
        (lineno, line)
        for lineno, line in enumerate(source.splitlines(), 1)
        if line.lstrip().startswith(markers)
    ]


_JS_STRINGS = {"'": "single", '"': "double", "`": "template"}


def _js_comment_segments(source: str) -> list[tuple[int, str]]:
    """JS / MJS：逐字符状态机提取注释内容（整行、行尾、块注释一并处理）。

    为什么要逐字符走而不是按行找标记：注释开头与字符串里的 `/` `*` 同形，
    按行判断必然要把「这一行的引号成对了吗」当成启发式。此前的实现就栽在这里
    ——`ui.js` 里 `cases/*.fromweb.json` 的 `/*` 落在**字符串内**，朴素状态机
    把它当成块注释开口，之后再也没有 `*/`，于是该文件第 44 行起两百多行代码
    全被当成"注释正文"扫描（`--tree` 恰好零命中才没炸，但代码行一旦出现形似
    条目码的标识符就会误报）。

    覆盖：`//` 行注释（含行尾）、`/* */` 块注释（含跨行）、单/双引号串与模板串
    的转义、模板串跨行。不覆盖：正则字面量（`/a\\/b/`）与 JSX 文本——仓库不用。
    """
    segments: list[tuple[int, str]] = []
    buf: list[str] = []
    line = 1
    state = "code"
    i = 0
    total = len(source)

    def flush() -> None:
        if buf:
            segments.append((line, "".join(buf)))
            buf.clear()

    while i < total:
        ch = source[i]
        pair = source[i : i + 2]
        if state == "code":
            if pair == "//":
                state = "line"
                i += 2
                continue
            if pair == "/*":
                state = "block"
                i += 2
                continue
            if ch in _JS_STRINGS:
                state = _JS_STRINGS[ch]
            elif ch == "\n":
                line += 1
        elif state == "line":
            if ch == "\n":
                flush()
                state = "code"
                line += 1
            else:
                buf.append(ch)
        elif state == "block":
            if pair == "*/":
                flush()
                state = "code"
                i += 2
                continue
            if ch == "\n":
                flush()
                line += 1
            else:
                buf.append(ch)
        else:  # 处于字符串内：注释标记一律不算
            if ch == "\\":
                # 反斜杠转义的下一个字符不参与状态判定；但「反斜杠 + 换行」是
                # 字符串续行，行号仍要前进，否则其后所有行号整体偏移。
                if source[i + 1 : i + 2] == "\n":
                    line += 1
                i += 2
                continue
            if ch == "\n":
                line += 1
                # 单/双引号串不跨行（未闭合视为该行结束），模板串继续
                if state != "template":
                    state = "code"
            elif (state == "single" and ch == "'") or (
                state == "double" and ch == '"'
            ) or (state == "template" and ch == "`"):
                state = "code"
        i += 1
    flush()
    return segments


def _markup_comment_segments(
    source: str, opener: str, closer: str
) -> list[tuple[int, str]]:
    """CSS / HTML：提取块注释的内容行（`/* */` 与 `<!-- -->`）。

    CSS 的块注释此前不在扫描面，`style.css` 里的流水号批次措辞一路漏到人工审计
    才现形。标记本身在 CSS / HTML 里是字面量（没有字符串转义注释的惯用法），
    逐行找开口与收口即可；误切只会多扫几行属性 / 标签，由编号形态门槛兜住。
    """
    segments: list[tuple[int, str]] = []
    inside = False
    for lineno, line in enumerate(source.splitlines(), 1):
        buf = line
        if inside:
            if closer in buf:
                segments.append((lineno, buf[: buf.index(closer)]))
                inside = False
                buf = buf[buf.index(closer) + len(closer) :]
            else:
                segments.append((lineno, buf))
                continue
        while opener in buf:
            start = buf.index(opener)
            rest = buf[start + len(opener) :]
            if closer in rest:
                segments.append((lineno, rest[: rest.index(closer)]))
                buf = rest[rest.index(closer) + len(closer) :]
            else:
                segments.append((lineno, rest))
                inside = True
                break
    return segments


def _css_comment_segments(source: str) -> list[tuple[int, str]]:
    """CSS：`/* */` 块注释。"""
    return _markup_comment_segments(source, "/*", "*/")


def _html_comment_segments(source: str) -> list[tuple[int, str]]:
    """HTML：`<!-- -->` 注释。

    此前 `.html` 整体不在扫描面（`briefdesk/ui/index.html` 的 HTML 注释对门禁
    失明），与 `.example` 同属"往这些文件写注释不享受兜底"的盲区。
    """
    return _markup_comment_segments(source, "<!--", "-->")


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
        return _js_comment_segments(source)
    if suffix == ".md":
        return _fenced_lines(source)
    if suffix == ".css":
        return _css_comment_segments(source)
    if suffix == ".html":
        return _html_comment_segments(source)
    if suffix in (".yml", ".yaml", ".toml", ".ps1", ".sh", ".example"):
        return _line_comment_segments(source, ("#",))
    return []


def scan_source(
    path: str, source: str, *, known_paths: frozenset[str] | None = None
) -> list[Hit]:
    """扫描单个文件内容的注释 / 文档片段。"""
    hits: list[Hit] = []
    seen: set[tuple[int, int]] = set()
    for lineno, text in iter_segments(path, source):
        key = (lineno, hash(text))
        if key in seen:
            continue
        seen.add(key)
        hits.extend(_collect_hits(path, lineno, text, known_paths=known_paths))
    return hits


def scan_text(path: str, text: str) -> list[Hit]:
    """扫描「片段文本」（提交信息 / diff 新增行），按语境宽松判定。"""
    hits: list[Hit] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        hits.extend(_collect_hits(path, lineno, line, prose=_looks_like_prose(line)))
    return hits


def _git(args: list[str], *, cwd: Path | None = None) -> str:
    # 统一关掉 core.quotePath：默认开启时 git 会给含非 ASCII 的路径加引号并转义，
    # ls-files 的名单会因此在后缀过滤处失配（静默漏扫），diff 头行的路径也拼不出
    # 真实文件（误判「读不到」退出 2）。路径原样输出才是扫描器要的形态。
    proc = subprocess.run(
        ["git", "-c", "core.quotePath=false", *args],
        cwd=str(cwd) if cwd is not None else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败：{proc.stderr.strip()}")
    return proc.stdout


def _tracked_paths(root: Path | None = None) -> list[str]:
    """全部跟踪文件路径（不按后缀过滤）。规则 10 要拿它判「这个 .md 在不在」。"""
    out = _git(["ls-files"], cwd=root)
    return [line for line in out.splitlines() if line]


def tracked_files(root: Path | None = None) -> list[str]:
    """列出纳入扫描的跟踪文件（排除未跟踪的本地计划产物）。

    在 root 下执行 git：ls-files 输出相对**当前目录**的路径，若从子目录
    （如 tests/）跑 pytest 而 git 仍在 cwd 执行，拼到 root 后文件不存在，
    读取失败被静默跳过，全量扫描零命中「通过」——等于没扫。
    """
    return [
        path for path in _tracked_paths(root) if path.lower().endswith(_TEXT_SUFFIXES)
    ]


def scan_tree(root: Path | None = None) -> list[Hit]:
    """全量扫描跟踪文件（收口核查用；基线机制见 tests/test_no_plan_refs.py）。

    文本后缀的跟踪文件读不出来（非 UTF-8、工作区缺该文件、权限拒绝）时按**命中**
    报出，而不是跳过：跳过等于「跑过了但没扫」——scan_tree 返回 0 命中、入口退出 0，
    门禁看起来是绿的，而该文件的注释与文档正文从未被读过。报成命中后调用方（入口的
    退出码 1、pytest 守卫的断言）都看得见。
    """
    base = root or Path.cwd()
    paths = _tracked_paths(base)
    # 同时收全体路径与裸文件名：注释里两种写法都有（docs/architecture.md /
    # architecture.md），只要能在树里找到就算可回查。
    known_paths = frozenset(paths) | frozenset(Path(p).name for p in paths)
    hits: list[Hit] = []
    for rel in tracked_files(base):
        path = base / rel
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            hits.append(Hit(rel, 0, "读取失败", f"{type(error).__name__}：该文件未纳入扫描"))
            continue
        hits.extend(scan_source(rel, source, known_paths=known_paths))
    return hits


_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def _added_lines(diff_text: str) -> dict[str, set[int]]:
    """从 unified diff 解析「新增行的新文件行号」：{路径: {行号}}。

    已知限制：git 的 core.quotePath（默认开）会给含非 ASCII 的路径加引号并
    转义，`b/` 前缀的剥除随之错位，该路径被记成一个读不到的名字，扫描按
    未执行退出 2——方向正确（宁可阻断不放过），彻底修法（按 `-z` 或
    `--no-prefix` 解析）另立跟进。仓库当前没有非 ASCII 路径。
    """
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
    """取某版本下的文件内容；读不到（新增文件、git 异常）返回 None。

    None 只表示「这次没读到」，不表示文件不存在——调用方不得据此跳过
    （见 scan_diff）。纯删除的新端在 diff 里是 `+++ /dev/null`，根本不会
    进入新增行表，走不到这里。
    """
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


def scan_diff(
    diff_text: str, *, rev: str | None, staged: bool, new_side: str | None = None
) -> list[Hit]:
    """扫描增量变更：只报新增行上的命中。

    定位方式：先解析新增行行号，再取该文件的**完整版本**做语法分段，
    只保留落在新增行上的命中——这样 docstring 内的行号依然精确，
    且规则 4 仍能按「注释 / 文档语境」判定。

    版本选择：行号是**新文件**（新端 / 工作区）的行号，因此内容必须取
    「新增侧」的版本——staged 取索引（:0），双端模式取调用方给定的新端 OID，
    旧 --ref 模式取 HEAD。取基线 ref 会把新行号套到旧内容上，报出一堆
    「本轮已删除」的假命中。

    有新增行就要求新端读得到：能走到读取这一步的路径都带着新增行，说明新端
    **该有**这个文件，读不到（git 异常、对象损坏、索引被改写）必须抛错。曾用
    `git cat-file -e` 的退出码与 stderr 英文文案分流「纯删除可以跳过」，但纯删除
    的 `+++ /dev/null` 根本不入新增行表，那个分支只会掩盖「有新增行却读不到」，
    文案判定还让结论随 git 的 locale 漂移。静默跳过等于漏扫，还会照样打印
    「未发现编号引用」，让门禁看起来跑过。抛错由 main 的既有异常路径转成退出码 2
    （扫描未执行，拒绝放行）。
    """
    hits: list[Hit] = []
    new_rev = ":0" if staged else (new_side or "HEAD")
    for path, lines in _added_lines(diff_text).items():
        if not lines:
            continue
        source = _file_at_rev(new_rev, path)
        if source is None:
            raise RuntimeError(f"新增行所在文件读不到：{new_rev}:{path}")
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


def _commits_between(base: str | None, head: str, *, cwd: Path | None = None) -> list[str]:
    """提交集合：base 为 None 时取 head 的全部祖先（首次推送的已知空边界）。"""
    args = ["rev-list", "--reverse", head] if base is None else ["rev-list", "--reverse", head, "--not", base]
    out = _git(args, cwd=cwd)
    return [line.strip() for line in out.splitlines() if line.strip()]


def _commit_message(sha: str, *, cwd: Path | None = None) -> str:
    """单条提交信息（subject + body）；读取失败抛错由调用方转 2。"""
    return _git(["show", "-s", "--format=%B", sha], cwd=cwd)


def scan_commit_range(
    base: str | None, head: str, *, cwd: Path | None = None
) -> tuple[list[Hit], int]:
    """扫描一段提交的全部信息；返回 (命中, 提交数)。合法空集合返回 ([], 0)。"""
    hits: list[Hit] = []
    shas = _commits_between(base, head, cwd=cwd)
    for sha in shas:
        hits.extend(scan_commit_message(_commit_message(sha, cwd=cwd)))
    return hits, len(shas)


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
    parser.add_argument("--base", default=None, help="双端模式基线（事件固定 SHA 或空树对象）")
    parser.add_argument("--head", default=None, help="双端模式新端（事件固定 SHA）")
    parser.add_argument(
        "--commits",
        action="store_true",
        help="扫描提交信息集合（配合 --base/--head；无 --base 时取 head 全部祖先）",
    )
    args = parser.parse_args(argv)

    # git 取不到差异时必须拒绝放行：当作空 diff 会「扫描没跑却通过」，
    # 与 scripts/secret_scan.py 同口径（退出码 2）。CI 里 fetch 失败或基线
    # 引用无效都会走到这里，静默通过等于门禁失守。
    if (args.base is None) != (args.head is None) and not args.commits:
        print("[forbidden-refs] --base 与 --head 必须成对给出；拒绝放行", file=sys.stderr)
        return 2
    try:
        if args.message_file:
            text = Path(args.message_file).read_text(encoding="utf-8", errors="replace")
            hits = scan_commit_message(text)
            target = f"提交信息 {args.message_file}"
        elif args.commits:
            if not args.head:
                print("[forbidden-refs] --commits 需要 --head；拒绝放行", file=sys.stderr)
                return 2
            hits, count = scan_commit_range(args.base, args.head)
            scope = "全部祖先" if args.base is None else f"{args.base[:12]}..{args.head[:12]}"
            target = f"提交信息集合（{scope}，{count} 条）"
        elif args.tree:
            hits = scan_tree()
            target = "全量跟踪文件"
        elif args.base and args.head:
            hits = scan_diff(
                _git(["diff", "--unified=0", args.base, args.head]),
                rev=None,
                staged=False,
                new_side=args.head,
            )
            target = f"{args.base[:12]}..{args.head[:12]} 的新增行"
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
