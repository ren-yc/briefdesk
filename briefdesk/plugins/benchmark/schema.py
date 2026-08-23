"""基准测试集 schema — pydantic 校验 + InternalMessage 转换。

测试集为 JSON 文件（每功能一个文件），统一以 InternalMessage 形状的消息
作为输入（字段与 briefdesk.types.InternalMessage 对齐），另加两个可选扩展
字段 title / key_info 模拟"已分类卡片"（真实管道中由 classify 阶段产出，
dedup / merge / title 三个功能消费的是卡片而非原始消息）。

timestamp 为方便手工录入，除整数 epoch 秒外还接受本地时间字符串
"YYYY-MM-DD[ HH:MM[:SS]]"（构造时换算为 epoch 秒）。

数据文件顶层结构（benchmark/cases/<feature>.json）：
{
  "feature": "classify" | "dedup" | "merge" | "title",
  "description": "可选：数据集说明",
  "categories": [可选，仅 classify：覆盖默认五类的类别定义 {name, prompt?, color?}],
  "cases": [ ... 各功能用例见下方 Case 模型 ... ]
}
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from briefdesk.types import InternalMessage

FEATURES: tuple[str, ...] = ("classify", "dedup", "merge", "title")

FeatureName = Literal["classify", "dedup", "merge", "title"]

# 卡片标题缺省来源：消息未提供 title 时取 content 前 50 字
_DEFAULT_CARD_TITLE_CHARS = 50


class DatasetError(Exception):
    """测试集校验失败（含全部错误明细，便于手工数据排查）。"""


# ── 消息输入（InternalMessage 形状）──


class MessageIn(BaseModel):
    """InternalMessage 形状的测试输入。

    title / key_info 为基准扩展字段（非 InternalMessage 原生字段）：
    - title：可选，模拟该消息已分类成卡片后的标题（缺省取 content 前 50 字）；
    - key_info：可选，模拟卡片的关键词信息（merge/title 判官使用）。
    """

    msg_id: str
    content: str
    sender_name: str = ""
    sender_id: str = ""
    session_id: str = ""
    group_name: str = ""
    timestamp: int = 0
    source: str = "bench"
    is_self: bool = False
    image_urls: list[str] = Field(default_factory=list)
    article_url: str = ""

    # 卡片扩展字段（dedup/merge/title 用）
    title: str | None = None
    key_info: str = ""

    @field_validator("timestamp", mode="before")
    @classmethod
    def _coerce_timestamp(cls, value: object) -> object:
        """整数 epoch 秒原样通过；字符串按本地时间解析（便于手工录入）。"""
        if isinstance(value, str):
            text = value.strip()
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
                try:
                    return int(time.mktime(time.strptime(text, fmt)))
                except (ValueError, OverflowError):
                    continue
            raise ValueError(
                f"无法解析 timestamp={value!r}："
                "支持整数 epoch 秒或 'YYYY-MM-DD[ HH:MM[:SS]]'"
            )
        return value

    def to_internal(self) -> InternalMessage:
        """转换为管道使用的 InternalMessage（content 构造即脱敏）。"""
        return InternalMessage(
            msg_id=self.msg_id,
            content=self.content,
            sender_name=self.sender_name,
            sender_id=self.sender_id,
            session_id=self.session_id,
            group_name=self.group_name,
            timestamp=self.timestamp,
            source=self.source,
            is_self=self.is_self,
            image_urls=list(self.image_urls),
            article_url=self.article_url,
        )


def card_fields(msg: MessageIn) -> tuple[str, str]:
    """取卡片 (标题, 内容)：title 缺省回退 content 前 50 字。

    dedup/merge/title 三个判官消费的是"已分类卡片"（标题+描述），
    与真实管道中 classify 阶段产出、dedup 阶段入缓存的数据形态一致。
    """
    title = (msg.title or "").strip() or msg.content.strip()[:_DEFAULT_CARD_TITLE_CHARS]
    return title, msg.content


# ── 期望标注 ──


class SameExpected(BaseModel):
    """去重期望：两条消息是否同一件事。"""

    same: bool


class MergeExpected(BaseModel):
    """合并判定期望：两张卡片是否同话题片段、应合并。"""

    merge: bool


class TimePoint(BaseModel):
    """单个时间点期望（extra_times 逐项；与 result.extra_times 同构）。"""

    type: Literal["start", "end"]
    time: str
    label: str = ""


class ClassifyExpected(BaseModel):
    """单条消息的分类期望。index 为该消息在 messages 数组中的下标。"""

    index: int = Field(ge=0)
    category: str
    subject: str = ""
    start: str = ""  # 期望开始时间 "YYYY-MM-DD[ HH:MM]"，无则空
    end: str = ""  # 期望截止时间，无则空
    times: list[TimePoint] = Field(default_factory=list)  # 期望 extra_times 时间点


class TitleExpected(BaseModel):
    """标题期望：title 精确匹配 与/或 keywords 包含匹配（至少提供一种）。"""

    title: str | None = None
    keywords: list[str] | None = None

    @model_validator(mode="after")
    def _at_least_one(self) -> TitleExpected:
        if not (self.title or self.keywords):
            raise ValueError("expected 需提供 title 或 keywords（至少一种）")
        return self


# ── 各功能用例模型 ──


class BaseCase(BaseModel):
    id: str
    note: str = ""  # 可选：用例说明/预期理由（便于人工复核）


class ClassifyCase(BaseCase):
    """分类基准：一批 InternalMessage + 每条"应被分类"消息的期望。

    expected 之外的 index 视为闲聊/噪声（模型不应输出分类结果）。
    """

    messages: list[MessageIn] = Field(min_length=1)
    expected: list[ClassifyExpected] = Field(default_factory=list)


class DedupCase(BaseCase):
    """去重基准：已有卡片 items（去重缓存中的历史条目）+ 新消息 query。

    items 先按卡片形态写入基准库，check_dedup 的候选预筛（字符重叠/嵌入
    余弦）与 AI 判重都基于真实缓存路径执行。
    """

    items: list[MessageIn] = Field(min_length=1)
    query: MessageIn
    expected: SameExpected  # {"same": true/false} — 是否同一件事


class MergeCase(BaseCase):
    """合并判定基准：同一会话相邻时间内的两张卡片，是否同话题片段。"""

    head: MessageIn  # 先出现
    tail: MessageIn  # 后出现
    expected: MergeExpected  # {"merge": true/false} — 是否应合并


class TitleCase(BaseCase):
    """标题重拟基准：合并后的卡片内容 + 期望标题。"""

    message: MessageIn
    old_title: str | None = None  # 缺省取 message.title 或 content 前 50 字
    key_info: str = ""  # 缺省取 message.key_info
    expected: TitleExpected


_CASE_MODELS: dict[str, type[BaseCase]] = {
    "classify": ClassifyCase,
    "dedup": DedupCase,
    "merge": MergeCase,
    "title": TitleCase,
}


# ── 数据集文件 ──


class CategoryDef(BaseModel):
    """classify 数据集可声明的类别定义（覆盖默认五类）。"""

    name: str
    prompt: str = ""
    color: str = ""


class DatasetFile(BaseModel):
    """测试集文件顶层结构。"""

    feature: FeatureName
    description: str = ""
    categories: list[CategoryDef] | None = None
    cases: list[dict[str, Any]] = Field(default_factory=list)

    @model_validator(mode="after")
    def _categories_only_for_classify(self) -> DatasetFile:
        if self.categories and self.feature != "classify":
            raise ValueError(
                f"feature={self.feature!r} 不支持 categories（仅 classify 数据集可用）"
            )
        return self


# ── 加载与校验 ──


def load_dataset_file(path: str | Path) -> DatasetFile:
    """加载并校验测试集文件顶层结构（feature/description/categories/cases）。"""
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    try:
        return DatasetFile.model_validate(raw)
    except ValidationError as e:
        raise DatasetError(f"数据集 {path} 顶层结构校验失败:\n{e}") from e


def parse_cases(dataset: DatasetFile) -> list[BaseCase]:
    """按 feature 校验全部用例；任何一条非法即整体报错（错误明细全列出）。

    额外规则：
    - 用例 id 必须唯一；
    - classify 期望中的 index 不得重复（同一消息只允许一条期望）。
    """
    model = _CASE_MODELS[dataset.feature]
    errors: list[str] = []
    cases: list[BaseCase] = []
    seen_ids: set[str] = set()
    for i, raw in enumerate(dataset.cases):
        raw_id = raw.get("id", "<无 id>") if isinstance(raw, dict) else "<非对象>"
        try:
            case = model.model_validate(raw)
        except ValidationError as e:
            errors.append(f"cases[{i}] {raw_id}: {e}")
            continue
        if case.id in seen_ids:
            errors.append(f"cases[{i}] id 重复: {case.id!r}")
        seen_ids.add(case.id)
        cases.append(case)
    if dataset.feature == "classify":
        for case in cases:
            assert isinstance(case, ClassifyCase)
            indexes = [e.index for e in case.expected]
            if len(indexes) != len(set(indexes)):
                dup = sorted({i for i in indexes if indexes.count(i) > 1})
                errors.append(
                    f"用例 {case.id}: 期望 index 重复 {dup}（同一消息只允许一条期望）"
                )
    if errors:
        raise DatasetError(f"数据集 {dataset.feature} 用例校验失败:\n" + "\n".join(errors))
    return cases
