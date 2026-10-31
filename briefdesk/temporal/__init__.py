"""时间解析器 — 语言无关内核 + 按语言规则包（i18n 分层架构）。

层职责（i18n 契约，详见 docs/architecture.md 的 temporal 小节）：
- 协议与数据类型（本模块）：语言无关。ParsedTime 的 (type, time, label)
  形状与 sysc 现产出对齐（type ∈ start/end；time 为 date-only 或
  "YYYY-MM-DD HH:MM"；label 可选），下游 `_apply_times_to_results` 的
  min/dedup 归一语义直接复用。
- 规则包（`briefdesk/temporal/rules_zh.py` 等）：词法模式、相对表达、
  词表与锚点策略——全部语言相关物，只住规则包；新语言 = 新规则包注册。
- 影子运行（`briefdesk/temporal/shadow.py`）：解析器只记录不生效，与
  sysc 现产出逐字段 diff——用真实分布定词表与覆盖率，再切生效。

解析产出即终态：影子期不参与判定，生效后不与 LLM 结果比较（同消息
start/end 稳定率入遥测的口径由 shadow diff 提供）。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Protocol

# time 字段的两种合法形态（与 classify.engine._clean_time 同一口径）：
# date-only "YYYY-MM-DD" 或精确到分钟 "YYYY-MM-DD HH:MM"
TIME_FORMAT_DATE = "%Y-%m-%d"
TIME_FORMAT_MINUTE = "%Y-%m-%d %H:%M"

# 线索文本的数字段（覆盖判定的最小匹配单位；分隔符形状不参与——
# 「这是时刻还是日期」是语言判定，由规则包经 LexicalHint.clock 声明）
_DIGIT_RUN = re.compile(r"\d+")


@dataclass
class ParsedTime:
    """单条时间点（与 sysc 产出同构；label 规则包首期恒空）。"""

    type: str  # "start" | "end"
    time: str  # "YYYY-MM-DD" 或 "YYYY-MM-DD HH:MM"
    label: str = ""


@dataclass
class LexicalHint:
    """词法触发线索（影子运行统计漏检/误检的计数单位）。

    hint 级别：strong=明确时间词（漏检代价=永久丢时间，升级 sysc）；
    weak=裸数字日期模式（误检代价=一次 LLM 调用，门向误检倾斜）。
    pattern_id：强线索的词表序号（弱线索恒 -1）——线索级漏检归因到模式，
    影子数据定词表时按它定向扩表。
    clock：时刻类线索（如「下午3点」「12:30」），由规则包判定并声明
    ——「这段词是时刻还是日期」是语言知识，内核不做字符猜测。覆盖判定
    据此分流：时刻线索只与产出的 HH:MM 分量对撞（date-only 产出不算
    覆盖），日期线索只与产出的 (年,月,日) 数字窗口对撞。
    """

    kind: str  # "strong" | "weak"
    text: str  # 命中的词法片段（截断保存）
    position: int
    pattern_id: int = -1
    clock: bool = False


@dataclass
class ParseResult:
    """单条消息的解析产出（影子 diff 的对拍单位）。

    times：解析产出（可能为空）；hints：词法触发线索（产出为空但存在
    未被覆盖的强线索 = 升级 sysc 的条件，门条件与误检代价的权衡见
    rules_zh 词表注释）。覆盖判定按线索数字段与产出分量的**整窗相等**
    （日期对 (月,日)/(年,月,日)，时刻对 HH:MM，见 hint_resolved）——
    线索级漏检（某条强线索没有对应产出）由 shadow 汇总按 pattern_id
    归因。
    """

    times: list[ParsedTime] = field(default_factory=list)
    hints: list[LexicalHint] = field(default_factory=list)

    def hint_resolved(self, hint: LexicalHint) -> bool:
        """单条线索是否被解析产出覆盖（数字 token 精确窗口匹配）。

        日期线索：线索数字段必须与产出的 (月,日) 或 (年,月,日) **整窗
        相等**（容忍年份缺失与前导零，不容忍子串蹭中——旧「双向包含」
        口径下「下午3点」的数字 3 会命中 2026-05-13 的 "0513"、"12:30"
        会命中 2026-12-30 的 "1230"，把未覆盖线索记成覆盖，影子漏检
        统计被系统性低估）。时刻线索：只与产出的 HH:MM 分量对撞，
        12 小时表述容忍 ±12h 归一（「下午3点」可命中 15:00），date-only
        产出对时刻线索恒不覆盖。
        """
        if not self.times:
            return False
        tokens = [int(part) for part in _DIGIT_RUN.findall(hint.text)]
        if not tokens:
            return False
        for parsed in self.times:
            date_part, _, clock_part = parsed.time.partition(" ")
            if hint.clock:
                if _clock_window_matches(tokens, clock_part):
                    return True
            elif _date_window_matches(tokens, date_part):
                return True
        return False


def _date_window_matches(tokens: list[int], date_part: str) -> bool:
    """日期线索对撞产出日期：数字段整窗等于 (月,日) 或 (年,月,日)。"""
    parts = date_part.split("-")
    if len(parts) != 3:
        return False
    if not all(part.isdigit() for part in parts):
        return False
    year, month, day = (int(part) for part in parts)
    if len(tokens) == 2:
        return tokens == [month, day]
    if len(tokens) == 3:
        return tokens == [year, month, day]
    return False


def _clock_window_matches(tokens: list[int], clock_part: str) -> bool:
    """时刻线索对撞产出 HH:MM：小时容忍 12 小时制 +12 形态，分钟须全同。"""
    if not clock_part:
        return False
    parts = clock_part.split(":")
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        return False
    hour, minute = (int(part) for part in parts)
    if not tokens:
        return False
    first = tokens[0]
    hour_hit = first == hour or (first <= 12 and first + 12 == hour)
    if len(tokens) == 1:
        return hour_hit
    if len(tokens) == 2:
        return hour_hit and tokens[1] == minute
    return False


class TemporalParser(Protocol):
    """时间解析器协议（语言无关内核消费的规则包接口）。"""

    name: str

    def lex_hints(self, text: str) -> list[LexicalHint]:
        """词法触发扫描：返回强/弱线索命中（不做解析）。"""
        ...

    def parse(
        self, text: str, anchor_epoch: int | None
    ) -> list[ParsedTime]:
        """解析时间点。

        anchor_epoch：相对时间的锚点（消息发送时刻 UNIX 秒）；None 或 0
        表示无合法锚点（对齐 `_local_datetime` 回退语义——非法/0 时间戳
        的消息跳过解析，仅保留词法线索供升级判定）。
        返回ParsedTime 列表（可能为空）。label 首期恒空（规则抽 label
        质量不可控、无自动验收门，保留给 LLM 兜底产出）。
        """
        ...


def anchor_from_epoch(anchor_epoch: int | None) -> time.struct_time | None:
    """锚点换算：UNIX 秒 → 本地墙钟 struct_time（naive 本地时间域，与
    全链路一致）；None/0/非法回退 None（调用方跳过相对时间解析）。"""
    if not anchor_epoch:
        return None
    try:
        return time.localtime(int(anchor_epoch))
    except (OSError, OverflowError, TypeError, ValueError):
        return None
