"""calibrate_allocator 样本装载口径单测（sqlite 临时库，不触网络）。

失败模式：校准脚本的 recat 样本若直接取 recat_log.content（裸 quote），与
items 侧 `title + 空格 + quote` 文本必然不同——覆盖层 weight=2 的去重
胜出永不命中，「计入新类、从原类剔除同源」在校准里退化为摆设，校准测的
就不是生产分配器的分布（阈值建议随之失真）。断言点：同源卡片在装载后
产出**同一 content 字符串**，且 _dedupe 按生产语义让 recat 胜出。
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location(
    "calibrate_allocator", _REPO / "scripts" / "calibrate_allocator.py"
)
if _SPEC is None or _SPEC.loader is None:  # pragma: no cover — 文件必在
    raise RuntimeError("无法加载 calibrate_allocator.py")
_mod = importlib.util.module_from_spec(_SPEC)
sys.modules.setdefault("calibrate_allocator", _mod)
_SPEC.loader.exec_module(_mod)


def _make_db(tmp_path: Path) -> Path:
    """最小表结构（只含装载查询涉及的列），返回库路径。"""
    db_path = tmp_path / "calib.sqlite"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE items (
            id TEXT PRIMARY KEY,
            category TEXT,
            title TEXT,
            source_quote TEXT,
            is_verified INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE recat_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id TEXT,
            category_before TEXT,
            category_after TEXT,
            content TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
        """
    )
    conn.execute(
        "INSERT INTO items (id, category, title, source_quote) "
        "VALUES ('i1', '活动通知', '团购通知', '周六下午3点集合')"
    )
    # recat_log.content 只存 quote（历史落库形态）——装载必须回查现行 title
    conn.execute(
        "INSERT INTO recat_log (item_id, category_before, category_after, content) "
        "VALUES ('i1', '普通消息', '活动通知', '周六下午3点集合')"
    )
    # 卡片已删的纠错样本：items 无行 → 回落存量 quote（与生产分支一致）
    conn.execute(
        "INSERT INTO recat_log (item_id, category_before, category_after, content) "
        "VALUES ('gone', '普通消息', '提醒', '旧引用文本')"
    )
    conn.commit()
    conn.close()
    return db_path


def test_recat_content_matches_production_overlay_text(tmp_path: Path) -> None:
    items, recats = _mod._load_samples(_make_db(tmp_path))
    assert len(items) == 1
    assert len(recats) == 2
    live = next(r for r in recats if r["category_after"] == "活动通知")
    # 生产口径：title + 空格 + 现行 quote，而非 recat_log.content 裸 quote
    assert live["content"] == "团购通知 周六下午3点集合"
    # 同源卡片两侧文本一致 → 去重键相同（覆盖层剔除才可能命中）
    assert live["content"] == items[0]["content"]


def test_deleted_card_falls_back_to_stored_quote(tmp_path: Path) -> None:
    _, recats = _mod._load_samples(_make_db(tmp_path))
    gone = next(r for r in recats if r["category_after"] == "提醒")
    assert gone["content"] == "旧引用文本"


def test_dedupe_overlay_wins_on_same_content(tmp_path: Path) -> None:
    """同源冲突：recat（weight=2）必须顶掉 items（weight=1）的原类锚。"""
    items, recats = _mod._load_samples(_make_db(tmp_path))
    all_samples = [
        {"category": s["category"], "content": s["content"], "weight": 1}
        for s in items
    ] + [
        {"category": r["category_after"], "content": r["content"], "weight": 2}
        for r in recats
    ]
    deduped, keys = _mod._dedupe(all_samples)
    by_key = dict(zip(keys, deduped))
    shared = next(s for s in deduped if s["content"] == "团购通知 周六下午3点集合")
    assert shared["category"] == "活动通知"
    assert shared["weight"] == 2
    # 同一 content 只保留一条（原类锚不残留，双锚震荡情形被消除）
    same_content = [s for s in deduped if s["content"] == shared["content"]]
    assert len(same_content) == 1
    assert by_key is not None  # 键表与样本对齐（留一剔除按它定位）
