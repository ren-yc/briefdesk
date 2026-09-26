"""基准运行与生产库的隔离回归：跑完一轮，生产库的逐表内容摘要必须完全不变。

为什么不用文件 SHA256：库是 WAL 模式，父进程持连接时对生产库的误写会停在 -wal
文件里，主库文件哈希不变、断言漏报；而最危险的泄漏恰恰是「DELETE FROM categories
后重建」这类**不改 items 行数**的写。这里的摘要经连接读取（WAL 中已提交的数据照样
可见），逐表逐行取内容，因此「行数不变但内容被改写」也能抓到。

为什么必须真 spawn：进程内直调时 runner 已经把 config.db_path 改到自己的 scratch
上，「生产库不变」从构造上就恒真，验证不了任何东西。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import aiosqlite

from briefdesk.db import init_schema

ROOT = Path(__file__).resolve().parent.parent

_OFFLINE_DATASET = {
    "feature": "dedup",
    "description": "隔离测试夹具（虚构数据）",
    "cases": [
        {
            "id": "iso-1",
            "items": [
                {
                    "msg_id": "i1",
                    "content": "出二手自行车，八成新，150元",
                    "sender_name": "虚构甲",
                    "sender_id": "u1",
                    "session_id": "s1",
                    "group_name": "虚构群",
                    "timestamp": "2026-04-01 10:00",
                    "source": "bench",
                    "title": "出二手自行车",
                }
            ],
            "query": {
                "msg_id": "q1",
                "content": "求购考研数学复习全书，价格可议",
                "sender_name": "虚构乙",
                "sender_id": "u2",
                "session_id": "s1",
                "group_name": "虚构群",
                "timestamp": "2026-04-02 09:30",
                "source": "bench",
                "title": "求购考研数学复习全书",
            },
            "expected": {"same": False},
        }
    ],
}


async def _build_production_db(path: Path) -> None:
    """造一个「像生产库」的夹具：建全表 + 一条类别 + 一张卡片。

    类别与卡片是**故意放的**：把它们换成摘要无关的空库，断言就检测不到
    「类目被重建」「合成卡片被插进生产库」这两类最危险的泄漏。
    """
    conn = await aiosqlite.connect(str(path))
    conn.row_factory = aiosqlite.Row  # init_schema 的查询按列名取值，同 _init_connection
    try:
        await init_schema(conn)
        await conn.execute(
            "INSERT INTO categories (name, prompt, color, enabled, created_at) "
            "VALUES (?, ?, ?, 1, datetime('now'))",
            ("生产类别", "生产提示词（虚构）", "#2563EB"),
        )
        await conn.execute(
            "INSERT INTO items (id, category, title, source_quote, source_group, "
            "source, source_msg_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, "
            "datetime('now'))",
            ("prod-1", "生产类别", "生产库卡片", "原文（虚构）", "虚构群", "weflow", "m-1"),
        )
        await conn.commit()
    finally:
        await conn.close()


def _digest(path: Path) -> str:
    """生产库全量逻辑摘要：逐表列名 + 全行内容 + user_version。

    经连接读取，天然绕开 WAL 的落盘时机问题。
    """
    h = hashlib.sha256()
    # 必须显式 close：sqlite3 的上下文管理器只提交事务、不关闭连接，
    # 连接开着会让 Windows 上临时目录清理报 WinError 32（文件被占用）。
    conn = sqlite3.connect(str(path))
    try:
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        for table in tables:
            cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]
            h.update(f"{table}{cols}".encode())
            rows = sorted(
                repr(tuple(r)) for r in conn.execute(f'SELECT * FROM "{table}"')
            )
            for row in rows:
                h.update(row.encode())
        version = conn.execute("PRAGMA user_version").fetchone()
        h.update(f"user_version={version[0]}".encode())
    finally:
        conn.close()
    return h.hexdigest()


class BenchmarkIsolationTest(unittest.TestCase):
    def test_production_db_unchanged_and_scratch_gets_the_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            production = base / "production.sqlite"
            asyncio.run(_build_production_db(production))
            before = _digest(production)

            cases_dir = base / "cases"
            cases_dir.mkdir()
            (cases_dir / "dedup.json").write_text(
                json.dumps(_OFFLINE_DATASET, ensure_ascii=False), encoding="utf-8"
            )
            run_dir = base / "run"
            scratch = base / "scratch.sqlite"
            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "briefdesk.plugins.benchmark.runner",
                    "--run-dir",
                    str(run_dir),
                    "--db",
                    str(scratch),
                    "--features",
                    "dedup",
                    "--source",
                    "file",
                    "--cases-dir",
                    str(cases_dir),
                ],
                check=False,
                cwd=str(ROOT),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=300,
                # 子进程的「生产库」由 DB_PATH 指定为夹具，这样断言检查的是它
                # 而不是本机真实库；runner 应在任何取连接之前就把路径改到 scratch。
                env={
                    **os.environ,
                    "DB_PATH": str(production),
                    "PYTHONIOENCODING": "utf-8",
                    # 嵌入关掉：否则判重缓存会按开发机配置去建向量，测试不再离线
                    "EMBED_API_BASE": "",
                },
            )
            self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
            self.assertEqual(
                _digest(production), before, "基准运行改动了生产库（隔离失效）"
            )

            # 反向确认这次运行真的写了东西：夹具卡片被合成进 scratch 库，
            # 否则「生产库没变」可能只是因为整个运行什么都没做。
            self.assertTrue(scratch.exists())
            conn = sqlite3.connect(str(scratch))
            try:
                written = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
                cats = [
                    r[0] for r in conn.execute("SELECT name FROM categories ORDER BY id")
                ]
            finally:
                conn.close()
            self.assertGreaterEqual(written, 1, "scratch 库里没有合成卡片，运行未生效")
            # 反向的隔离证据：scratch 库拿的是 init_schema 播种的默认类别，
            # **不含**生产库那条自建类别——它若出现就说明生产库被读了。
            self.assertNotIn("生产类别", cats, "scratch 库读到了生产库的类别（隔离失效）")
            self.assertIn("活动通知", cats, "scratch 库没有播种默认类别")


if __name__ == "__main__":
    unittest.main()
