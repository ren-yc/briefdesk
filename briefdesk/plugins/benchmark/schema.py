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
import re
import time
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from briefdesk.masking import PLACEHOLDER_ONLY_RE
from briefdesk.types import InternalMessage

# 用例来源受控枚举（对齐 ai_reasoning_effort 校验器风格：未知值响亮报错）。
# manual=人工核对（唯一验收分母）/ recorder=观察记录导出 / cards=网页卡片
# 导出 / synthetic=合成生成（永不入验收分母）。
CASE_ORIGINS: tuple[str, ...] = ("manual", "recorder", "cards", "synthetic")

FEATURES: tuple[str, ...] = ("classify", "dedup", "merge", "title")

FeatureName = Literal["classify", "dedup", "merge", "title"]

# 卡片标题缺省来源：消息未提供 title 时取 content 前 50 字
_DEFAULT_CARD_TITLE_CHARS = 50

# mode 的历史载体是 note（外部脚本转换出来的数据集只有 note，形如
# "sample_id=542；mode=fictional；碎片:1,2,3"）；real 样本没有 mode= 前缀，
# 只在 note 里以独立 token 出现（"sample_id=0；real；src=..."）。
# 两者都是**约定而非结构**，故显式 mode 字段优先，解析只作兼容回退。
_MODE_IN_NOTE_RE = re.compile(r"mode\s*=\s*([A-Za-z0-9_]+)")
_REAL_TOKEN_RE = re.compile(r"(?:^|[；;，,\s])real(?:$|[；;，,\s])")

# mode 的已知取值（切片维度，非受控枚举：数据集自定义的 mode 照样能切片）。
# 存在的用途只有一个——dry-run 时把"看起来像拼错"的值点名出来。历史上就出现过
# 一个数据集只有 5 种 mode、另一个用 oov_custom 的情形；拼错的值会静默多出一个
# 只有一两条用例的切片，读者很难察觉自己按一个假分组在读数。
KNOWN_CLASSIFY_MODES: frozenset[str] = frozenset(
    {"prod", "oov", "mix", "fictional", "single", "real", "unspecified"}
)


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
    origin: str = "manual"  # 用例来源（manual/recorder/cards/synthetic）
    batch_fingerprint: str = ""  # 导出批次指纹（内容派生：同批用例重导同指纹；空=未打）

    @field_validator("origin")
    @classmethod
    def _validate_origin(cls, value: str) -> str:
        if value not in CASE_ORIGINS:
            raise ValueError(
                f"非法 origin {value!r}；可选：{'、'.join(CASE_ORIGINS)}"
            )
        return value


class ClassifyCase(BaseCase):
    """分类基准：一批 InternalMessage + 每条"应被分类"消息的期望。

    expected 之外的 index 视为闲聊/噪声（模型不应输出分类结果）。
    """

    messages: list[MessageIn] = Field(min_length=1)
    expected: list[ClassifyExpected] = Field(default_factory=list)
    mode: str = ""
    """数据集切片维度（prod/oov/mix/fictional/single/real）。

    为什么需要显式字段：类别指标的解释力取决于「这批用例测的是不是生产分布」——
    词表外（oov）样本与生产样本混在一个分母里，总分既不代表能力（非 oov 部分
    九成以上命中）也不代表上限（oov 部分不到一半），验收时无从取信。留空时
    回退解析 note 里的历史写法（外部脚本转换的数据集只有 note），旧数据集零迁移。
    """

    def resolved_mode(self) -> str:
        """显式字段 > note 的 `mode=<值>` > note 里独立的 `real` 标记 > unspecified。"""
        if self.mode:
            return self.mode
        note = self.note
        found = _MODE_IN_NOTE_RE.search(note)
        if found:
            return found.group(1)
        if _REAL_TOKEN_RE.search(note):
            return "real"
        return "unspecified"

    def placeholder_only(self) -> bool:
        """全部消息正文都是附件占位符（`[图片]`/`[链接]` 一类）→ 无正文可分类。

        判定与生产入口过滤共用同一正则（`briefdesk.masking.PLACEHOLDER_ONLY_RE`），
        且比较的是 `to_internal()` 之后的正文——脱敏改写发生在构造期，生产看到的
        就是改写后的文本；拿数据集原文判定会与该过滤口径漂移。这类消息在真实
        管道里进不了分类阶段，把它们的期望算进分母会得到结构性不可能通过的指标。

        命名不叫「全图片」：正则匹配**任意**纯占位符，将来出现 `[链接]` 用例时
        归因仍成立（无正文），而"图片"会说谎。
        """
        return all(
            PLACEHOLDER_ONLY_RE.match(m.to_internal().content.strip())
            for m in self.messages
        )


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


def dedup_item_key_conflicts(cases: list[BaseCase]) -> list[str]:
    """找出同键不同卡的 dedup items：`(source, msg_id)` 相同但卡片内容不同。

    为什么要单独校验：items 表的唯一键是 `(source, source_msg_id)`，而判重的缓存
    作用域是按 `insert_item` 返回的行 id 收窄的。两条用例（或同一用例的两条 item）
    用同一个键承载不同内容时，后插入的一方会命中冲突分支、拿到**前一条**的行 id
    ——它声明的那张历史卡片根本没进库，缓存里躺着的是别人的标题与正文。表现为
    「命中的相似卡在用例里找不到」，且没有任何报错，只能靠装载期校验拦住。

    跨用例复用为何也要查（而不是只查用例内）：每例跑完会回收自己的 items，串行时
    后一例插入同键并不会撞车；但 `--concurrency>1` 下前一例的行还活着，冲突就真的
    发生。隔离设计的前提是「并发与串行同结果」，故校验按整个数据集做。

    同键同卡不算冲突：那是多条用例引用同一张历史卡片（网页导出多例共享候选正是
    这种形态），插入返回的是同一行——内容本就相同，作用域不会指错。比较口径用
    `card_fields`（标题 + 正文）：标题参与重叠与嵌入，只比正文会漏掉「同文异标题」
    这种同样会改变判重结果的复用。
    """
    seen: dict[tuple[str, str], tuple[str, tuple[str, str]]] = {}
    errors: list[str] = []
    for case in cases:
        if not isinstance(case, DedupCase):
            continue
        for item in case.items:
            key = (item.source, item.msg_id)
            owner, card = seen.setdefault(key, (case.id, card_fields(item)))
            if card != card_fields(item):
                errors.append(
                    f"用例 {case.id}: item (source={item.source!r}, "
                    f"msg_id={item.msg_id!r}) 与用例 {owner!r} 同键不同卡"
                    "（items 唯一键撞车：本条声明的卡片不会入库，"
                    "判重的缓存作用域会指向别人的卡片）"
                )
    return errors


def shared_dedup_item_keys(cases: list[BaseCase]) -> set[tuple[str, str]]:
    """跨用例共享的 `(source, msg_id)` 键集合（同键同卡——合法但影响回收策略）。

    与 `dedup_item_key_conflicts` 的分工：那条查「同键不同卡」（必须拒绝），这条查
    「同键同卡」（允许——多条用例引用同一张历史卡片是 recorder 导出的常见形态）。

    为什么要单独暴露：允许共享卡之后，「跑完就把自己的 items 删掉」不再安全——
    A 例先跑完删掉共享行，还在排队的 B 例按 id 取数会取到空，静默判成「无候选」。
    调用方据此决定本轮要不要回收（键全唯一才回收；有共享就一律保留）。
    """
    seen: dict[tuple[str, str], str] = {}
    shared: set[tuple[str, str]] = set()
    for case in cases:
        if not isinstance(case, DedupCase):
            continue
        for item in case.items:
            key = (item.source, item.msg_id)
            owner = seen.setdefault(key, case.id)
            if owner != case.id:
                shared.add(key)
    return shared


def conflicting_dedup_case_ids(cases: list[BaseCase]) -> set[str]:
    """参与「同键不同卡」复用的**双方**用例 id（与 `dedup_item_key_conflicts` 同判据）。

    为什么单独要一份 id 而不是只用错误文案：文件数据集走 `parse_cases`，可以整体拒绝
    （用户改文件重跑）；网页导出是**逐条容错**的（非法项跳过不影响整轮），整轮拒绝会让
    用户界面上一次运行直接失败。但"照跑只留一行 WARNING"更糟——冲突时后插入的一方经
    `insert_item` 的冲突分支拿到前一条的行 id，判重命中的是别人的卡片，而逐例 `channel`
    照样正常记 overlap/cosine，报告里**看不出这条结果是错的**。两侧期望在这种数据下都
    不可信，所以把**双方**都剔出本轮：用例数会明显减少、WARNING 点名 id，比留一批
    看起来正常、实际错判的结果诚实。
    """
    seen: dict[tuple[str, str], dict[str, tuple[str, str]]] = {}
    bad: set[str] = set()
    for case in cases:
        if not isinstance(case, DedupCase):
            continue
        for item in case.items:
            key = (item.source, item.msg_id)
            card = card_fields(item)
            holders = seen.setdefault(key, {})
            for other_id, other_card in holders.items():
                if other_card != card:
                    bad.update({other_id, case.id})
            mine = holders.get(case.id)
            if mine is not None and mine != card:
                # 同一用例内部同键不同卡：声明了两张历史卡、库里只会有一行，本例整体不可信
                bad.add(case.id)
            holders[case.id] = card
    return bad


def load_dataset_file(path: str | Path) -> DatasetFile:
    """加载并校验测试集文件顶层结构（feature/description/categories/cases）。

    编码兜底与 store 读取侧同口径：非 UTF-8 文件抛 DatasetError（含原因）
    而非裸 UnicodeDecodeError——CLI 面对坏文件应收到可操作的错误信息。
    """
    try:
        with open(path, encoding="utf-8-sig") as f:
            raw = json.load(f)
    except UnicodeDecodeError as e:
        raise DatasetError(f"数据集 {path} 不是合法 UTF-8 编码：{e}") from e
    except json.JSONDecodeError as e:
        raise DatasetError(f"数据集 {path} 不是合法 JSON：{e}") from e
    try:
        return DatasetFile.model_validate(raw)
    except ValidationError as e:
        raise DatasetError(f"数据集 {path} 顶层结构校验失败:\n{e}") from e


def parse_cases(dataset: DatasetFile) -> list[BaseCase]:
    """按 feature 校验全部用例；任何一条非法即整体报错（错误明细全列出）。

    额外规则：
    - 用例 id 必须唯一；
    - classify 期望中的 index 不得重复（同一消息只允许一条期望）；
    - dedup 的 items 不得出现「同 `(source, msg_id)` 键、不同卡片」的复用
      （items 唯一键撞车会让判重的缓存作用域指向别人的卡片；同键同卡允许）。
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
    if dataset.feature == "dedup":
        errors += dedup_item_key_conflicts(cases)
    if errors:
        raise DatasetError(f"数据集 {dataset.feature} 用例校验失败:\n" + "\n".join(errors))
    return cases
