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

import time
from dataclasses import dataclass, field
from typing import Protocol

# time 字段的两种合法形态（与 classify.engine._clean_time 同一口径）：
# date-only "YYYY-MM-DD" 或精确到分钟 "YYYY-MM-DD HH:MM"
TIME_FORMAT_DATE = "%Y-%m-%d"
TIME_FORMAT_MINUTE = "%Y-%m-%d %H:%M"


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
    阶段 2 按它定向扩表。
    """

    kind: str  # "strong" | "weak"
    text: str  # 命中的词法片段（截断保存）
    position: int
    pattern_id: int = -1


@dataclass
class ParseResult:
    """单条消息的解析产出（影子 diff 的对拍单位）。

    times：解析产出（可能为空）；hints：词法触发线索（产出为空但存在
    未被覆盖的强线索 = 升级 sysc 的条件，门条件与误检代价的权衡见
    rules_zh 词表注释）。覆盖判定按线索的数字成分与产出 time 值的
    (month, day) 匹配——线索级漏检（某条强线索没有对应产出）由 shadow
    汇总按 pattern_id 归因。
    """

    times: list[ParsedTime] = field(default_factory=list)
    hints: list[LexicalHint] = field(default_factory=list)

    def hint_resolved(self, hint: LexicalHint) -> bool:
        """单条线索是否被解析产出覆盖（月日数值匹配：容忍粒度/年份差异）。"""
        if not self.times:
            return False
        digits = "".join(ch for ch in hint.text if ch.isdigit())
        if not digits:
            return False
        for parsed in self.times:
            try:
                day_part = parsed.time.split(" ")[0]
                _, month, day = day_part.split("-")
                # 线索数字串与 (月,日) 的拼接双向包含即算覆盖（4.15 ↔ 05-15）
                if digits in (month + day) or (month.lstrip("0") + day.lstrip("0")) in digits:
                    return True
            except ValueError:
                continue
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
        质量不可控且无自动验收门，见计划裁决 R）。
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
