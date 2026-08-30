"""跨模块共享的基础类型（含管道跨插件契约）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, TypedDict

from briefdesk.masking import clean_display_name, mask_content

if TYPE_CHECKING:
    from briefdesk.sources_base import SourceClient


@dataclass
class InternalMessage:
    """统一内部消息格式，由各数据源的 normalize 函数生成。

    content 构造即脱敏：任何位置创建的本类型，content 都不含明文
    手机号/身份证/邮箱/银行卡（PII 脱敏见 briefdesk/masking.py）。
    sender_name/group_name 构造即净化：统一过滤控制字符与首尾空白
    （多源显示名清洗见 briefdesk/masking.py 的 clean_display_name）。
    """

    msg_id: str
    content: str
    sender_name: str
    sender_id: str
    session_id: str
    group_name: str
    timestamp: int
    source: str = ""  # 源标识，由 pipeline 入口按客户端 name 统一盖章
    is_self: bool = False  # 是否本账号自己发送（normalize 阶段由各源按原始字段
    # 判定；pipeline 入口按 config.ignore_self 统一过滤）
    image_urls: list[str] = field(default_factory=list)
    article_url: str = ""  # 公众号/文章卡片的消息原文链接（独立于 content，供前端可点跳转）

    def __post_init__(self) -> None:
        """构造即脱敏/净化：保证隐私与脏数据在入库、AI、前端展示前已被处理。"""
        self.content = mask_content(self.content)
        self.sender_name = clean_display_name(self.sender_name)
        self.group_name = clean_display_name(self.group_name)


class ContextMsg(TypedDict):
    """上下文消息（raw_messages 与 contacts 联合查询结果）。

    msg_id 由 get_context_messages 一并返回，前端据此定位目标消息高亮。
    """

    sender: str
    content: str
    time: int
    msg_id: str
    article_url: str  # 文章卡片原文链接（无则空串），前端渲染可点跳转


@dataclass
class SessionInfo:
    """源无关的会话描述 — 由源产出，应用层负责写库。"""

    source: str
    session_id: str
    name: str
    is_group: bool
    is_official: bool = False  # 公众号会话（与群聊/私聊并列的第三类，仅 weflow-legacy 产出）
    last_active_at: int = 0  # 会话内最后一条消息时间（秒级 epoch；0 = 未知）

    def __post_init__(self) -> None:
        """构造即净化显示名（控制字符/空白），幂等。"""
        self.name = clean_display_name(self.name)


@dataclass
class ContactInfo:
    """源无关的联系人描述 — 由源产出，应用层负责写库。"""

    source: str
    sender_id: str
    display_name: str

    def __post_init__(self) -> None:
        """构造即净化显示名（控制字符/空白），幂等。"""
        self.display_name = clean_display_name(self.display_name)


@dataclass
class PollResult:
    """单源一次轮询的结果（源无关，写库由应用层完成）。"""

    messages: list[InternalMessage] = field(default_factory=list)
    sessions: list[SessionInfo] = field(
        default_factory=list
    )  # 本轮发现的会话（应用层 upsert）
    contacts: list[ContactInfo] = field(
        default_factory=list
    )  # 本轮发现的联系人（应用层 upsert）
    session_count: int = 0
    failed_sessions: set[str] = field(default_factory=set)
    # 本轮【未成功拉取】的 session_id 集合（源侧瞬态错误静默跳过，如 qqflow
    # 索引期 503）。poll_cycle 对这些会话跳过水位推进：它们的消息未落
    # raw_messages，钉窗机制看不到，若照常推进水位会造成窗口内消息永久漏拉。
    # 单会话拉取失败（非 503）同样走此集合 + session_errors 记原因，不再整轮
    # 上抛——否则一个持续失败的坏会话会饿死同源其它会话。
    session_errors: dict[str, str] = field(default_factory=dict)


# ── 管道跨插件契约（由各阶段插件实现/消费，定义在核心）──


@dataclass
class ClassifyResult:
    """AI 分类结果（classify 阶段产出，dedup/merge 阶段消费）。"""

    msg_index: int
    category: str = ""
    summary: str = ""
    key_info: str = ""
    quote: str = ""
    subject: str = ""  # 信息主体（组织/实体名），用于同主体卡片折叠分组
    start: str = ""  # 活动/讲座/面试等的开始时间 "YYYY-MM-DD HH:MM"，空=未提取
    end: str = ""  # 报名/提交/抢票等的截止时间，空=未提取
    # 一条消息含多个时间点时（如工作提醒列了 4 个截止日），除主字段外的全部
    # 时间点：[{"type": "start"|"end", "time": "...", "label": "..."}]
    extra_times: list[dict] = field(default_factory=list)


@dataclass
class ClassifyOutcome:
    """一次分类调用的结果：成功结果 + 本轮失败（不标记 processed、下轮回填）的全局 index。

    finish_reason=length 截断会按数量对半拆成两个独立请求递归重试；
    每个子请求独立成败：成功部分正常入库，失败部分（单条仍 length /
    深度上限 / 空响应 / 解析失败 / 未知类别 / 网络异常）由管道骨架跳过
    processed 标记，回填窗口内自动重试。
    """

    results: list[ClassifyResult]
    failed: list[int]  # 相对原批的全局 index（已含 offset）
    # 分类阶段标记"含明确时间"的消息 index（相对原批全局 index），供
    # 第二阶段 sysc 时间提取使用（只对 time=true 的消息提取 start/end/times）。
    time_indexes: list[int] = field(default_factory=list)


@dataclass
class DedupCandidate:
    """去重判定时点的对照候选快照（观察用途，与 dedup 缓存条目同源）。

    命中时为被并入的已存在条目；未命中时为参与判定的最高分候选。
    """

    item_id: str
    title: str
    source_quote: str = ""
    source: str = ""
    image_urls: list[str] = field(default_factory=list)


@dataclass
class DedupResult:
    """去重判定结果（dedup 阶段产出）。"""

    is_duplicate: bool
    similar_to_id: str = ""
    candidate: DedupCandidate | None = None  # 判定时点的对照候选（观察插件用）


@dataclass
class InsertedRow:
    """入库后的卡片行（dedup 阶段产出，merge 阶段消费）。"""

    item_id: str
    msg: InternalMessage
    result: ClassifyResult
    title: str


@dataclass
class DedupCheck:
    """单条消息的一次去重判定观察记录（dedup 阶段产出，观察型插件消费）。

    candidate 为 None 表示未发生实际比较（无候选，无判定依据），
    观察方可跳过该条。
    """

    msg: InternalMessage
    title: str  # 判定时使用的卡片标题
    is_duplicate: bool
    candidate: DedupCandidate | None = None


@dataclass
class MergeCard:
    """合并判定一方的卡片快照（观察记录用，与 items 行/新卡字段同源）。"""

    title: str
    desc: str  # 判官看到的完整描述（标题 + key_info + 原文拼接）
    key_info: str = ""
    subject: str = ""
    sender_name: str = ""
    session_id: str = ""
    group_name: str = ""
    msg_time: int = 0
    source: str = ""
    msg_id: str = ""
    source_quote: str = ""
    image_urls: list[str] = field(default_factory=list)


@dataclass
class MergeTitleCheck:
    """合并成功后重拟标题的观察记录（merge 阶段产出）。"""

    old_title: str  # 重拟前的头卡标题（summarize_title 的输入）
    key_info: str  # 合并后的关键信息（期望关键词的 ground truth 来源）
    quote: str  # 合并后的原文


@dataclass
class MergeCheck:
    """会话内合并判定的观察记录（merge 阶段产出）。

    head = 判官视角的卡片A（候选卡），tail = 卡片B（新卡），与
    judge_merge 调用顺序一致；same 为判官结论（None 已在上游跳过，
    不记录——判官失败不构成判定依据）。
    title 在合并成功后填充（重拟标题事件）。
    """

    same: bool
    head: MergeCard
    tail: MergeCard
    title: MergeTitleCheck | None = None


@dataclass
class BatchContext:
    """单批消息在管道阶段链间的共享状态。

    messages 可被 enrich 阶段改写（如 OCR 替换 content）；client 供
    enrich 阶段下载媒体；outcomes/rows/inserted 由各阶段按序填充。
    dedup_checks/merge_checks 为各阶段的判定观察记录（供观察型
    阶段插件如 benchmark 消费），在锁内追加、批结束即弃。
    """

    messages: list[InternalMessage]
    client: SourceClient  # OCR 等需下载媒体的阶段使用（TYPE_CHECKING 引用，避免环）
    outcomes: ClassifyOutcome | None = None  # classify 阶段填充
    rows: list[tuple[InternalMessage, ClassifyResult, str, list[float] | None]] = field(
        default_factory=list
    )  # dedup 阶段锁外规划：（消息/结果/标题/预嵌入向量）；原文取 msg.content，不另存副本
    inserted: list[InsertedRow] = field(default_factory=list)  # dedup 阶段入库行
    dupes: int = 0  # 判重命中数（dedup 阶段累加）
    skipped: int = 0  # 闲聊跳过数（骨架标记 processed 时累加）
    merged: int = 0  # 合并次数（merge 阶段累加）
    dedup_checks: list[DedupCheck] = field(default_factory=list)
    merge_checks: list[MergeCheck] = field(default_factory=list)
    preembeddings: dict[tuple[str, str], list[float]] = field(default_factory=dict)
    # 锁外阶段（before_run）写入的本批预嵌入向量，存储相按 (source, msg_id)
    # 消费。挂批上下文而非引擎实例：BatchContext 每批新建、天然隔离——
    # rag 曾用引擎级共享字典，实时批与回填并发时互相 clear 丢向量（复核 P1-5）
    reembed_queue: list[tuple[str, str, str, list[str] | None, str]] = field(
        default_factory=list
    )
    # 阶段插件锁内登记、锁外 after_run 消化的补嵌请求
    # (item_id, title, quote, image_urls, source)：merge 合并后存活卡文本
    # 已变需补嵌入回归余弦候选（复核 P2-20）；挂批上下文与 preembeddings 同理
