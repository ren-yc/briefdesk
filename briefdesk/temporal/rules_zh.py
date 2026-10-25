"""zh 规则包 — 时间解析器的中文词法与解析规则（影子运行期定稿前为最小集）。

本模块是「语言相关的一切都放外层包」契约的第一个实例：词法模式、相对
表达、词表、触发线索全部住这里；内核（briefdesk/temporal/__init__.py 与
shadow.py）不感知语言。影子运行用真实分布统计各模式命中率后扩写词表——
首期只收录高频强线索与裸数字日期弱线索，覆盖率以影子 diff 报告为准。
"""

from __future__ import annotations

import datetime as _dt
import re
import time
from dataclasses import dataclass

from briefdesk.temporal import (
    LexicalHint,
    ParsedTime,
    ParseResult,
    anchor_from_epoch,
)

# ── 强线索词表（明确时间词；漏检 = 永久丢时间）──
# 每条一个正则（finditer 扫描）；扩表只改这里。模式顺序即 id（影子 diff
# 按 pattern_id 归因漏检）。
_STRONG_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\d{4}[-/年]\d{1,2}[-/月]\d{1,2}[日号]?"),      # 0 绝对日期
    re.compile(r"\d{1,2}月\d{1,2}[日号]"),                      # 1 X月X日
    re.compile(r"(下|本|这)?周[一二三四五六日天]"),              # 2 星期
    re.compile(r"(明天|后天|今天|今晚|明晚|大后天)"),            # 3 相对日
    re.compile(r"(上午|下午|晚上|中午|清晨|凌晨)\d{1,2}[点时](\d{1,2})?分?"),  # 4 时段+时刻
    re.compile(r"\d{1,2}[:点时]\d{2}分?"),                      # 5 时刻（冒号/点分）
    re.compile(r"(截止|截止到|截至| deadline|Deadline)"),        # 6 截止词
)

# ── 弱线索：裸数字日期模式（误检代价 = 一次 LLM 调用，门向误检倾斜）──
# 4.15 / 05/01 类裸数字形态；与强线索重叠的不产出（强模式已覆盖其语义）。
_WEAK_PATTERN = re.compile(r"(?<![\d./-])\d{1,2}[./]\d{1,2}(?![\d./-])")

# ── 排除词表（确定性空转：sysc 按规则本就跳过，入触发表只产生空转调用）──
_EXCLUDE_PATTERN = re.compile(r"(尽早|尽快|近期|最近|随时)")

# 影子期不做「缺省时刻」推断，产出与线索粒度一致（date-only 或精确到分钟
# ——下游 benchmark 按 (type, time) 串精确比对，粒度不一致即判错）。
_MAX_HINT_TEXT = 32


@dataclass(frozen=True)
class _HintSpan:
    kind: str
    start: int
    end: int
    text: str
    pattern_id: int  # 强线索的模式序号（弱线索恒 -1）


def _overlaps(a: tuple[int, int], b: tuple[int, int]) -> bool:
    """半开区间相交判定。"""
    return a[0] < b[1] and b[0] < a[1]


def _scan_hints(text: str) -> list[_HintSpan]:
    """词法扫描：强线索（含强-强嵌套合并，留最长/最先）+ 弱线索（与强
    重叠不产出）。"""
    strong: list[_HintSpan] = []
    for idx, pattern in enumerate(_STRONG_PATTERNS):
        for m in pattern.finditer(text):
            span = _HintSpan("strong", m.start(), m.end(), m.group(0), idx)
            if any(
                _overlaps((s.start, s.end), (span.start, span.end)) for s in strong
            ):
                # 强-强嵌套（如「5.1」嵌在「2026年5月1日」内）：留先命中的
                # 更长 span，避免同一日期被统计两次
                continue
            strong.append(span)
    spans = list(strong)
    for m in _WEAK_PATTERN.finditer(text):
        if any(_overlaps((s.start, s.end), (m.start(), m.end())) for s in strong):
            continue
        spans.append(_HintSpan("weak", m.start(), m.end(), m.group(0), -1))
    return sorted(spans, key=lambda s: s.start)


def lex_hints(text: str) -> list[LexicalHint]:
    """zh 词法触发扫描（协议实现）。

    排除词命中且无硬时间词 → 整条按空转处理（不产出线索，确定性空转不进
    触发表）；排除词与硬时间词共现时仍扫描（排除词只表达「无确定时间」，
    不能吞掉同句的绝对时间词）。hint 文本截断保存，完整归因交给 diff 报告。
    """
    if not text:
        return []
    if _EXCLUDE_PATTERN.search(text) and not _has_hard_time(text):
        return []
    spans = _scan_hints(text)
    return [
        LexicalHint(
            kind=s.kind,
            text=s.text[:_MAX_HINT_TEXT],
            position=s.start,
            pattern_id=s.pattern_id,
        )
        for s in spans
    ]


def _has_hard_time(text: str) -> bool:
    """硬时间词判定（排除词门的例外条件）：命中**任意**强模式即算——
    只看前两个模式会让「尽快确认，明天下午3点开」这类排除词+相对时间
    共现的消息整条被吞。"""
    return any(p.search(text) for p in _STRONG_PATTERNS)


def parse(text: str, anchor_epoch: int | None) -> list[ParsedTime]:
    """zh 规则解析（协议实现；影子期最小集，覆盖率以影子 diff 报告为准）。

    首期实现只覆盖三类高置信形态（影子期先用 diff 数据验证精度，再扩表）：
    1. 绝对日期（2026-05-01 / 2026年5月1日）→ date-only；
    2. X月X日（无年份）→ 以锚点年份补全（取与消息时刻最近的一次的年份，
       与 sysc「无年份日期推断」同语义）→ date-only；
    3. 星期（下X → 下一周的匹配日；本/这 X → 本周内，含今天）→ date-only。
    span 按位置消费：绝对日期命中的区间不再被「X月X日」二次消费（否则
    「2027年5月1日」会凭空多出一条按锚点补年的假日期）。
    时刻/区间/label 均留待影子数据定稿后扩展；解析产出为空不构造占位。
    """
    if not text:
        return []
    anchor = anchor_from_epoch(anchor_epoch)
    out: list[ParsedTime] = []
    seen: set[str] = set()
    consumed: list[tuple[int, int]] = []

    # 1) 绝对日期（span 消费：登记区间，供后续规则跳过重叠命中）
    for m in re.finditer(
        r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})[日号]?", text
    ):
        value = _format_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if value and value not in seen:
            seen.add(value)
            out.append(ParsedTime(type="start", time=value))
        consumed.append((m.start(), m.end()))

    def _consumed(start: int, end: int) -> bool:
        return any(_overlaps((start, end), span) for span in consumed)

    if anchor is not None:
        # 2) X月X日（无年份）：取与锚点最近的年份；跳过被绝对日期消费的 span
        for m in re.finditer(r"(\d{1,2})月(\d{1,2})[日号]", text):
            if _consumed(m.start(), m.end()):
                continue
            month, day = int(m.group(1)), int(m.group(2))
            value = _nearest_year_date(month, day, anchor)
            if value and value not in seen:
                seen.add(value)
                out.append(ParsedTime(type="start", time=value))

        # 3) 星期：下X = 下一周的匹配日；本/这 X = 本周内（含今天）
        for m in re.finditer(r"(下|本|这)?周([一二三四五六日天])", text):
            target = _WEEKDAY_MAP.get(m.group(2))
            if target is None:
                continue
            if m.group(1) == "下":
                value = _next_weekday(target, anchor)
            else:
                value = _this_weekday(target, anchor)
            if value and value not in seen:
                seen.add(value)
                out.append(ParsedTime(type="start", time=value))

    return out


_WEEKDAY_MAP = {
    "一": 0, "二": 1, "三": 2, "四": 3,
    "五": 4, "六": 5, "日": 6, "天": 6,
}


def _format_date(year: int, month: int, day: int) -> str:
    """构造并校验 date-only 串；非法日期返回空串（对齐 _clean_time 语义）。"""
    value = f"{year:04d}-{month:02d}-{day:02d}"
    try:
        time.strptime(value, "%Y-%m-%d")
    except ValueError:
        return ""
    return value


def _date_days(value: str) -> int | None:
    """date-only 串 → epoch 天序（非法返回 None）。"""
    try:
        return int(time.mktime(time.strptime(value, "%Y-%m-%d")))
    except (ValueError, OSError, OverflowError):
        return None


def _nearest_year_date(month: int, day: int, anchor: time.struct_time) -> str:
    """无年份日期补年：取与锚点最接近的那一次（无论已过还是未来；
    严禁默认滚动次年——与 sysc 无年份推断同语义）。"""
    candidates: list[tuple[int, str]] = []
    for year in (anchor.tm_year - 1, anchor.tm_year, anchor.tm_year + 1):
        value = _format_date(year, month, day)
        if value:
            days = _date_days(value)
            if days is not None:
                candidates.append((days, value))
    if not candidates:
        return ""
    anchor_days = _date_days(time.strftime("%Y-%m-%d", anchor))
    if anchor_days is None:
        return candidates[0][1]
    best = min(candidates, key=lambda pair: abs(pair[0] - anchor_days))
    return best[1]


def _next_weekday(weekday: int, anchor: time.struct_time) -> str:
    """「下周X」= 下一周的匹配日。

    周首约定：周一为一周之始（中文习惯，与 _WEEKDAY_MAP 周日=6 一致）。
    目标日 = 本周周一 + 7 + weekday：无论锚点在周内哪天，语义恒为
    「下一自然周的同名日」（锚点周三问「下周三」= 下周三，不是 +7 天后
    的错误推算）。
    """
    anchor_date = _dt.date(anchor.tm_year, anchor.tm_mon, anchor.tm_mday)
    monday = anchor_date - _dt.timedelta(days=anchor_date.weekday())
    target = monday + _dt.timedelta(days=7 + weekday)
    return target.strftime("%Y-%m-%d")


def _this_weekday(weekday: int, anchor: time.struct_time) -> str:
    """「本周X/这周X/X」（无前缀）= 本周内的匹配日（周一为周首，含今天）。

    无前缀的「周五」按本周算：锚点周六问「周五」= 昨天（本周五），不是
    下周五——与「下周X」显式前缀形成语义分界。
    """
    anchor_date = _dt.date(anchor.tm_year, anchor.tm_mon, anchor.tm_mday)
    monday = anchor_date - _dt.timedelta(days=anchor_date.weekday())
    target = monday + _dt.timedelta(days=weekday)
    return target.strftime("%Y-%m-%d")


def make_result(text: str, anchor_epoch: int | None) -> ParseResult:
    """协议便捷入口：一次调用同时产出 hints 与 times（影子 diff 用）。"""
    return ParseResult(times=parse(text, anchor_epoch), hints=lex_hints(text))
