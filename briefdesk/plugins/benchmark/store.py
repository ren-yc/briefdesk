"""基准用例文件存储（benchmark WebPlugin 专属，随插件包分发）。

用例不再写入数据库（原 benchmark_cases 表已移除；旧库遗留的同名空表无害），
网页「导出当前列表为基准用例」直接把构造好的四类用例**导出**为
用户用例目录下的 <feature>.fromweb.json（合并写，每功能一个文件），
Web 运行与 CLI 均从该目录加载（见 engine.load_web_cases）。

- 文件为 DatasetFile 顶层结构（feature/description/cases），与手写数据集同构，
  导出前逐条经 schema 模型校验（非法整批拒绝，不落盘）；
- 原子写（同目录 .tmp + os.replace）；*.fromweb.json 已 gitignore
  （含真实群聊内容，禁止入 commit）；
- 列举/清空只动**用户用例目录**，既不触碰应用数据库，也不写包里那份只读示例。

四类用例的期望推导（build_*_cases_from_rows，期望=卡片当前状态这一
「人工确认后」的事实）：
- classify：期望 = 卡片当前类别/主体/时间字段（按批上限拆分多例）；
  **已忽略卡片（is_verified=-1）作为噪声样本进入 messages、不写期望**
  （AI 误分类上来的闲聊，人工标记应丢弃，模型不应输出分类结果）；
- title：每卡一例，期望 = 卡片关键词包含（key_info 拆分；无 key_info 回退
  期望标题=卡片当前标题）；已忽略卡片排除（噪声不作标题期望）；
- dedup：same=true = 同 content_hash 却共存的卡片对（原文一致=同一信息，
  去重应命中而未命中的场景）；same=false = 时间相邻且类别不同的共存卡片对；
  已忽略卡片排除；
- merge：同会话同类别、时间窗（生产 MERGE_WINDOW_MINUTES）内的相邻卡片对，
  同主体同发送者 → merge=true（同话题前后片段），主体均非空且不同 →
  merge=false；同 content_hash 或主体缺失的歧义对跳过；已忽略卡片排除。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import uuid
from collections.abc import Callable
from itertools import pairwise
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from briefdesk import paths
from briefdesk.config import config
from briefdesk.plugins.benchmark.schema import (
    ClassifyCase,
    DedupCase,
    MergeCase,
    TitleCase,
)

logger = logging.getLogger(__name__)

# 插件包内 cases/ 目录：**只读**示例与夹具（随 wheel 分发）。
# 网页导出的用例不写这里——安装包目录可能只读，示例也不该被用户数据覆盖。
PACKAGE_CASES_DIR = Path(__file__).resolve().parent / "cases"
FROMWEB_SUFFIX = ".fromweb.json"


def user_cases_dir() -> Path:
    """用户用例目录（网页导出 *.fromweb.json 与手写数据集）；每次调用求值。"""
    return paths.benchmark_cases_dir()


_CASE_MODELS: dict[str, Any] = {
    "classify": ClassifyCase,
    "dedup": DedupCase,
    "merge": MergeCase,
    "title": TitleCase,
}


def fromweb_path(feature: str, cases_dir: Path | None = None) -> Path:
    """某功能的网页导出文件路径：<cases_dir>/<feature>.fromweb.json。

    缺省目录是**用户用例目录**而不是包内示例目录：导出与清空都只吃模块全局，
    写回安装包目录在 wheel 下会失败，在源码树里则会用真实聊天内容覆盖只读示例。
    cases_dir 显式传入时覆盖缺省：基准子进程读父进程给的用例快照，测试喂夹具
    而不是覆盖用户导出的真实用例。
    """
    base = user_cases_dir() if cases_dir is None else Path(cases_dir)
    return base / f"{feature}{FROMWEB_SUFFIX}"


def new_case_id() -> str:
    """随机用例 id（仅手工构造用例的场景使用；导出路径用 stable_case_id）。"""
    return f"bench-{uuid.uuid4().hex[:10]}"


def stable_case_id(feature: str, identity: object) -> str:
    """内容派生的稳定用例 id：sha256(feature|identity) 截 16 位。

    导出路径统一用它替代随机 id：同一批数据重复导出（同内容）→ 同 id →
    合并写「新覆盖旧」真正命中，重复导出不再堆积内容相同的副本；卡片
    成员集合变化 → 新 id 追加；成员不变而卡片状态变化 → 同 id 就地更新
    （旧期望被新状态覆盖，不会残留过期副本）。
    """
    material = f"{feature}|{json.dumps(identity, ensure_ascii=False, sort_keys=True, default=str)}"
    return "case-" + hashlib.sha256(material.encode()).hexdigest()[:16]


# ── 校验 ──


def validate_case(feature: str, case: dict[str, Any]) -> dict[str, Any]:
    """按功能模型校验用例主体；非法抛 DatasetError（含字段明细）。

    model_dump(exclude_none=True)：导出的 JSON 不携带缺省的 None 字段
    （如 title 期望未提供的 keywords），文件保持精简、便于人工复核。
    """
    from briefdesk.plugins.benchmark.schema import DatasetError

    try:
        model = _CASE_MODELS[feature]
        parsed = model.model_validate(case)
    except ValidationError as e:
        raise DatasetError(f"用例校验失败:\n{e}") from e
    return parsed.model_dump(exclude_none=True)


def parse_cases_with_errors(
    feature: str, raw_cases: list[dict[str, Any]]
) -> tuple[list[Any], list[str]]:
    """文件用例逐条校验：返回 (合法用例, 错误明细)；非法项跳过不影响整轮。"""
    errors: list[str] = []
    ok: list[Any] = []
    model = _CASE_MODELS[feature]
    for i, raw in enumerate(raw_cases):
        raw_id = raw.get("id") if isinstance(raw, dict) else "<非对象>"
        try:
            ok.append(model.model_validate(raw))
        except ValidationError as e:
            errors.append(f"cases[{i}] {raw_id}: {e}")
    return ok, errors


# ── 网页导出（写 cases/<feature>.fromweb.json）──


def batch_fingerprint(source: str = "fromweb") -> str:
    """导出批次指纹：sha256(来源标识 + uuid4) 截 16 位十六进制。

    每次**调用**生成一个新指纹（uuid4，时间戳只进 description 不进指纹
    ——秒级时间戳会把同秒多次导出合并成同指纹，粒度错配）；同一批数据
    若会经两条路径导出（先 recorder 入训、再 cards 进评测），调用方必须
    显式传同一 source 并自行做内容级互斥（指纹是调用级、不是内容级）。
    训练数据构建侧按指纹做批次级互斥（与入训记录同指纹的用例出验收分母）。
    """
    material = f"{source}|{uuid.uuid4()}"
    return hashlib.sha256(material.encode()).hexdigest()[:16]


async def export_fromweb(
    feature: str,
    cases: list[dict[str, Any]],
    description: str | None = None,
    *,
    origin: str = "cards",
    fingerprint: str | None = None,
) -> Path:
    """把用例列表**合并**写入 cases/<feature>.fromweb.json（原子写）。

    合并语义（替代旧覆盖式）：读现有文件 → 按 case id 合并（新覆盖旧、
    文件独有的保留）→ 原子写回。导出路径的用例 id 内容派生（stable_case_id
    ——同批数据重复导出同 id 就地更新，不堆积副本）；要删除用例请走
    delete_all_fromweb 或手工编辑文件。
    逐条经 schema 模型校验，任一非法即抛 DatasetError（整批不落盘）；
    origin 受控枚举在写入前校验（单点写入处同样拦非法值）；空列表拒绝
    导出（不改动已有文件）。本函数是用例来源字段（origin/batch_fingerprint）
    的**单点写入处**：默认 origin=cards + 本调用新指纹；recorder 等其它
    来源复用本函数时显式传 origin 与自己的指纹（不传则每调用生成）。
    既有文件里的用例不再重新校验（历史数据原样保留，新入参为准）。
    """
    from briefdesk.plugins.benchmark.schema import CASE_ORIGINS, DatasetError

    if origin not in CASE_ORIGINS:
        # 受控枚举在写入前校验：绕开它会让非法 origin 落盘、再被 parse 层
        # 静默丢弃（文件数据集路径才报错）——契约（见 architecture.md）是
        # 单点写入处同样拦非法值
        raise DatasetError(
            f"非法 origin {origin!r}；可选：{'、'.join(CASE_ORIGINS)}"
        )
    if not cases:
        raise DatasetError("用例列表为空，未导出（不改动已有文件）")
    stamped_fingerprint = fingerprint or batch_fingerprint(origin)
    stamped: list[dict[str, Any]] = []
    for case in cases:
        validated = validate_case(feature, case)
        validated["origin"] = origin
        validated["batch_fingerprint"] = stamped_fingerprint
        stamped.append(validated)
    path = fromweb_path(feature)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing_cases: list[dict[str, Any]] = []
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
            # 旧文件不可读（语法坏/编码坏）时拒绝整批覆盖：合并写的前提
            # 是「不丢既有用例」，静默按空合并会换一种方式重犯覆盖式丢
            # 数据的缺陷——宁可让用户手工处理该文件
            raise DatasetError(
                f"既有导出文件不可读，拒绝合并覆盖（请手工修复或删除后重试）："
                f"{path.name}（{e}）"
            ) from e
        if not isinstance(raw, dict) or not isinstance(raw.get("cases"), list):
            raise DatasetError(
                f"既有导出文件结构非法（缺 cases 数组），拒绝合并覆盖：{path.name}"
            )
        dropped = 0
        for entry in raw["cases"]:
            if isinstance(entry, dict) and entry.get("id"):
                existing_cases.append(entry)
            else:
                dropped += 1
        if dropped:
            logger.warning(
                "既有导出文件中 %d 条无法识别的条目被丢弃（缺 id 或非对象）：%s",
                dropped, path.name,
            )
    by_id = {c.get("id"): c for c in existing_cases}
    for case in stamped:
        by_id[case["id"]] = case
    merged = list(by_id.values())
    payload = {
        "feature": feature,
        "description": description
        or f"网页「导出当前列表为基准用例」于 {time.strftime('%Y-%m-%d %H:%M:%S')}"
        "导出（合并写，期望=卡片当前分类）",
        "cases": merged,
    }
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return path


def _read_fromweb(
    feature: str, cases_dir: Path | None = None
) -> list[dict[str, Any]]:
    """读取某功能的 fromweb 用例（文件缺失/非法 → 空列表 + WARNING）。

    编码兜底与写入侧同口径（utf-8-sig + UnicodeDecodeError）：坏编码文件
    按空列表跳过而非抛穿——读取点在 Web 运行与 CLI 回落的公共路径上，
    一个坏文件不该打崩整轮基准。
    """
    path = fromweb_path(feature, cases_dir)
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
        logger.warning("网页导出用例文件不可读 %s: %s", path.name, e)
        return []
    cases = raw.get("cases") if isinstance(raw, dict) else None
    if not isinstance(cases, list):
        logger.warning("网页导出用例文件结构非法 %s（缺 cases 数组）", path.name)
        return []
    out: list[dict[str, Any]] = []
    for case in cases:
        if isinstance(case, dict):
            entry = dict(case)
            entry["feature"] = feature
            out.append(entry)
    return out


async def list_fromweb(
    feature: str | None = None, cases_dir: Path | None = None
) -> list[dict[str, Any]]:
    """列出 cases/*.fromweb.json 中的用例（每项含 feature 字段；缺失/非法文件跳过）。"""
    features = [feature] if feature else list(_CASE_MODELS)
    out: list[dict[str, Any]] = []
    for f in features:
        out.extend(_read_fromweb(f, cases_dir))
    return out


async def delete_all_fromweb(feature: str | None = None) -> int:
    """删除 cases/*.fromweb.json（?feature= 可只删某功能）；返回删除文件数。"""
    features = [feature] if feature else list(_CASE_MODELS)
    deleted = 0
    for f in features:
        path = fromweb_path(f)
        if path.exists():
            try:
                path.unlink()
                deleted += 1
            except OSError as e:
                logger.warning("删除 %s 失败: %s", path.name, e)
    return deleted


# ── 设置导出（当前筛选列表 → classify 用例批）──

# 单用例消息数/字符上限（对齐 classify 引擎的批上限，防截断）
_MAX_BATCH_MESSAGES = 100
_MAX_BATCH_CHARS = 30000


def _card_to_message(row: dict[str, Any]) -> dict[str, Any]:
    """items 表行 → InternalMessage 形状；classify 判官看原文（source_quote）。"""
    content = (
        row.get("source_quote")
        or row.get("description")
        or row.get("title")
        or ""
    )
    image_urls: list[str] = []
    raw_urls = row.get("image_urls")
    if isinstance(raw_urls, str) and raw_urls:
        try:
            parsed = json.loads(raw_urls)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, list):
            image_urls = [str(u) for u in parsed if isinstance(u, str)]
    return {
        "msg_id": row.get("source_msg_id") or str(row.get("id") or ""),
        "content": content,
        "sender_name": row.get("sender_name") or "",
        "sender_id": "",
        "session_id": row.get("session_id") or "",
        "group_name": row.get("source_group") or "",
        "timestamp": row.get("msg_time") or 0,
        "source": row.get("source") or "bench",
        "image_urls": image_urls,
        "article_url": row.get("article_url") or "",
        "title": row.get("title") or None,
        "key_info": row.get("key_info") or "",
    }


def _card_expected_classify(row: dict[str, Any], index: int) -> dict[str, Any]:
    """分类期望：类别 = 卡片当前类别，时间 = 卡片已提取字段。"""
    extra_times: list[dict[str, Any]] = []
    raw_times = row.get("extra_times")
    if isinstance(raw_times, str) and raw_times:
        try:
            parsed = json.loads(raw_times)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, list):
            extra_times = [
                {
                    "type": str(t.get("type", "")),
                    "time": str(t.get("time", "")),
                    "label": str(t.get("label") or ""),
                }
                for t in parsed
                if isinstance(t, dict)
                and t.get("type") in ("start", "end")
                and t.get("time")
            ]
    return {
        "index": index,
        "category": row.get("category") or "",
        "subject": row.get("subject") or "",
        "start": row.get("start") or "",
        "end": row.get("end") or "",
        "times": extra_times,
    }


def build_classify_cases_from_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把卡片列表构造成 classify 用例（≤100 条/批，期望=卡片当前分类）。

    期望分类为空的卡片跳过（无有效 ground truth）；**已忽略卡片**
    （is_verified=-1）作为噪声样本进入 messages、不写期望——它们是 AI
    误分类上来的闲聊/噪声（人工标记应丢弃），模型不应输出分类结果
    （输出即计为误报）。返回用例主体列表，由调用方导出到用户用例目录。
    """
    cases: list[dict[str, Any]] = []
    batch_msgs: list[dict[str, Any]] = []
    batch_expected: list[dict[str, Any]] = []
    batch_chars = 0
    batch_noise = 0

    def _flush(prefix: str) -> None:
        nonlocal batch_msgs, batch_expected, batch_chars, batch_noise
        if not batch_msgs:
            return
        note = f"{prefix}（{len(batch_msgs)} 条）"
        if batch_noise:
            note += f"，含 {batch_noise} 条噪声（已忽略）"
        cases.append(
            {
                # 批级用例的稳定身份 = 成员消息 id 集合：同批重复导出同 id
                # （就地更新期望），成员变化（卡片状态变/筛选变）→ 新批
                "id": stable_case_id(
                    "classify", sorted(m["msg_id"] for m in batch_msgs)
                ),
                "note": note,
                "messages": batch_msgs,
                "expected": batch_expected,
            }
        )
        batch_msgs = []
        batch_expected = []
        batch_chars = 0
        batch_noise = 0

    for row in rows:
        noise = _is_noise(row)
        if not noise:
            category = (row.get("category") or "").strip()
            if not category:
                continue  # 无类别卡片不是有效分类期望
        msg = _card_to_message(row)
        chars = len(msg.get("content") or "")
        if len(batch_msgs) >= _MAX_BATCH_MESSAGES or (
            batch_msgs and batch_chars + chars > _MAX_BATCH_CHARS
        ):
            _flush("由设置导出")
        if not noise:
            batch_expected.append(_card_expected_classify(row, len(batch_msgs)))
        else:
            batch_noise += 1
        batch_msgs.append(msg)
        batch_chars += chars
    _flush("由设置导出")
    return cases


# ── title / dedup / merge 用例推导 ──


def _is_noise(row: dict[str, Any]) -> bool:
    """已忽略卡片（is_verified=-1）= 噪声样本：AI 误分类上来的闲聊，
    人工标记应丢弃。classify 中作为 messages 负样本（无期望）；title/
    dedup/merge 中排除（噪声不是有效卡片状态）。"""
    return (row.get("is_verified") or 0) == -1


def _split_keywords(text: str) -> list[str]:
    """key_info 关键词拆分（逗号/中文逗号/顿号分隔）。"""
    parts = text.replace("，", ",").replace("、", ",").split(",")
    return [p.strip() for p in parts if p.strip()]


def _categorized(row: dict[str, Any]) -> bool:
    """有效分类卡片：有类别且未被人工忽略（忽略 = 噪声，不作期望）。"""
    return bool((row.get("category") or "").strip()) and not _is_noise(row)


def _time_key(row: dict[str, Any]) -> tuple[int, str]:
    return (row.get("msg_time") or 0, str(row.get("id") or ""))


def build_title_cases_from_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把卡片列表构造成 title 用例（每卡一例）。

    判官以卡片当前标题为 old_title、key_info 为关键信息重拟标题；
    期望 = 重拟结果仍包含卡片核心关键词（key_info 拆分），无 key_info 的
    卡片回退为期望标题 = 卡片当前标题（精确）。无类别/无标题卡片跳过。
    """
    cases: list[dict[str, Any]] = []
    for row in rows:
        if not _categorized(row):
            continue
        title = (row.get("title") or "").strip()
        keywords = _split_keywords(row.get("key_info") or "")
        if keywords:
            expected: dict[str, Any] = {"keywords": keywords}
            note = "由设置导出：期望=卡片关键词包含"
        elif title:
            expected = {"title": title}
            note = "由设置导出：期望=卡片当前标题（无 key_info）"
        else:
            continue
        msg = _card_to_message(row)
        cases.append(
            {
                "id": stable_case_id("title", msg["msg_id"]),
                "note": note,
                "message": msg,
                "old_title": title,
                "key_info": row.get("key_info") or "",
                "expected": expected,
            }
        )
    return cases


def build_dedup_cases_from_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把卡片列表构造成 dedup 用例（两类共存状态即可推导的 ground truth）：

    - same=true：content_hash 相同却共存的卡片对（原文完全一致 = 同一
      信息，属去重应命中而未命中的场景）；
    - same=false：按时间相邻、类别不同且 content_hash 不同的共存卡片对
      （人工复核后共存的卡片 = 不同信息）。
    """
    cases: list[dict[str, Any]] = []

    def _case(item_row: dict[str, Any], query_row: dict[str, Any], same: bool, note: str) -> None:
        item_msg = _card_to_message(item_row)
        query_msg = _card_to_message(query_row)
        cases.append(
            {
                "id": stable_case_id(
                    "dedup", [item_msg["msg_id"], query_msg["msg_id"]]
                ),
                "note": note,
                "items": [item_msg],
                "query": query_msg,
                "expected": {"same": same},
            }
        )

    # 1) 同 content_hash 共存 → same=true（首条为历史 items，其余逐条为 query）
    by_hash: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if _is_noise(row):
            continue  # 忽略卡片 = 噪声，不作去重期望
        h = (row.get("content_hash") or "").strip()
        if h:
            by_hash.setdefault(h, []).append(row)
    for h, group in by_hash.items():
        for other in group[1:]:
            _case(group[0], other, True, f"由设置导出：同 content_hash 共存（{h[:8]}）→ 同一信息")

    # 2) 时间相邻且类别不同 → same=false（同 hash 对已在上面按 true 导出）
    ordered = sorted([r for r in rows if _categorized(r)], key=_time_key)
    for prev, cur in pairwise(ordered):
        if (prev.get("category") or "").strip() == (cur.get("category") or "").strip():
            continue
        ph = (prev.get("content_hash") or "").strip()
        if ph and ph == (cur.get("content_hash") or "").strip():
            continue
        _case(prev, cur, False, "由设置导出：相邻不同类别共存卡片 → 不同信息")
    return cases


def _merge_window_seconds() -> int:
    """合并配对时间窗：对齐生产 MERGE_WINDOW_MINUTES（0=禁用时按默认 10 分钟）。"""
    return max(1, config.merge_window_minutes or 10) * 60


def build_merge_cases_from_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把卡片列表构造成 merge 用例（同会话同类别、时间窗内的相邻卡片对）：

    - merge=true：主体相同且发送者相同 → 同一话题的前后片段；
    - merge=false：主体均非空且不同 → 不同话题卡片；
    - 同 content_hash 对（去重场景）与主体缺失的歧义对跳过。
    """
    cases: list[dict[str, Any]] = []
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        if _is_noise(row):
            continue  # 忽略卡片 = 噪声，不作合并期望
        category = (row.get("category") or "").strip()
        if not category:
            continue
        session = (row.get("session_id") or "").strip()
        if session:
            groups.setdefault((session, category), []).append(row)
    window = _merge_window_seconds()
    for group in groups.values():
        group.sort(key=_time_key)
        for head, tail in pairwise(group):
            dt = (tail.get("msg_time") or 0) - (head.get("msg_time") or 0)
            if not 0 <= dt <= window:
                continue
            h_hash = (head.get("content_hash") or "").strip()
            if h_hash and h_hash == (tail.get("content_hash") or "").strip():
                continue  # 同信息对是去重场景，不是合并场景
            h_subj = (head.get("subject") or "").strip()
            t_subj = (tail.get("subject") or "").strip()
            if h_subj and t_subj and h_subj == t_subj:
                if (head.get("sender_name") or "") != (tail.get("sender_name") or ""):
                    continue  # 同主体不同发送者：可能一买一卖，歧义跳过
                head_msg = _card_to_message(head)
                tail_msg = _card_to_message(tail)
                cases.append(
                    {
                        "id": stable_case_id(
                            "merge", [head_msg["msg_id"], tail_msg["msg_id"]]
                        ),
                        "note": "由设置导出：同会话同主体同发送者的相邻片段 → 应合并",
                        "head": head_msg,
                        "tail": tail_msg,
                        "expected": {"merge": True},
                    }
                )
            elif h_subj and t_subj:
                head_msg = _card_to_message(head)
                tail_msg = _card_to_message(tail)
                cases.append(
                    {
                        "id": stable_case_id(
                            "merge", [head_msg["msg_id"], tail_msg["msg_id"]]
                        ),
                        "note": "由设置导出：同会话不同主体的相邻卡片 → 不应合并",
                        "head": head_msg,
                        "tail": tail_msg,
                        "expected": {"merge": False},
                    }
                )
    return cases


#: 各功能的卡片列表 → 用例列表构造器（网页导出入口按序调用）
CASE_BUILDERS: dict[str, Callable[[list[dict[str, Any]]], list[dict[str, Any]]]] = {
    "classify": build_classify_cases_from_rows,
    "dedup": build_dedup_cases_from_rows,
    "merge": build_merge_cases_from_rows,
    "title": build_title_cases_from_rows,
}
