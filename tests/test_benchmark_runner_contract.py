"""基准子进程入口的对外契约：参数 / 产物 / 终态语义。

契约先于实现冻结在这里：参数集合、run-dir 产物清单、progress.jsonl 的行 schema、
终态行的 kind、退出码粒度。**结果分类一律看终态行**，退出码只断言粒度（0 成功 /
2 用法错误 / 其余非 0）——1 被逃逸异常、Windows 上父进程的 terminate 与 db.py 的
SchemaMismatch 共用，用它分类必然误判。

夹具分两种：零重叠的 dedup 用例（预筛直接跳过 chat）与有重叠、必然触发判定的
用例（走 --ai-provider stub）。两种都显式把 EMBED_API_BASE 置空——只跳过 chat
并不等于离线，判重缓存加载还会按开发机 .env 去建向量。
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from briefdesk import ai_ports
from briefdesk.config import config
from briefdesk.plugins.benchmark import runner as bench_runner

ROOT = Path(__file__).resolve().parent.parent

# 零重叠的两条用例：预筛判无候选 → 不触发 AI，判定恒为 False
_OFFLINE_DATASET = {
    "feature": "dedup",
    "description": "契约测试夹具（虚构数据）",
    "cases": [
        {
            "id": "c1",
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
        },
        {
            "id": "c2",
            "items": [
                {
                    "msg_id": "i2",
                    "content": "食堂三楼新增窗口，本周试营业",
                    "sender_name": "虚构丙",
                    "sender_id": "u3",
                    "session_id": "s2",
                    "group_name": "虚构群",
                    "timestamp": "2026-04-03 11:00",
                    "source": "bench",
                    "title": "食堂三楼新增窗口",
                }
            ],
            "query": {
                "msg_id": "q2",
                "content": "图书馆闭馆时间调整为二十二点",
                "sender_name": "虚构丁",
                "sender_id": "u4",
                "session_id": "s2",
                "group_name": "虚构群",
                "timestamp": "2026-04-04 08:00",
                "source": "bench",
                "title": "图书馆闭馆时间调整",
            },
            "expected": {"same": False},
        },
    ],
}


async def _release_db_singletons() -> None:
    """关掉并复位 runner 在进程内留下的数据库单例。

    这里**不能**用 close_db()：它置位的 _db_closed 没有复位通道，会让同进程里之后
    所有用真实 get_db() 的用例抛「数据库已关闭」；但只把 close_db patch 掉又会让
    aiosqlite 的非 daemon worker 线程活着，pytest 退出时 join 它会挂住——所以直接
    关闭连接并清空模块级引用，两个问题一起避开。
    """
    import briefdesk.db as db_mod

    for name in ("_db", "_embed_db"):
        conn = getattr(db_mod, name, None)
        if conn is not None:
            await conn.close()
            setattr(db_mod, name, None)


# 标题有明显重叠 → 预筛给候选，必然触发 AI 判定（走 --ai-provider stub）
_AI_DATASET = {
    "feature": "dedup",
    "description": "契约测试夹具（虚构数据，触发 AI 判定）",
    "cases": [
        {
            "id": "ai-1",
            "items": [
                {
                    "msg_id": "i1",
                    "content": "摄影社招新面试，周三下午三点，体育馆",
                    "sender_name": "虚构甲",
                    "sender_id": "u1",
                    "session_id": "s1",
                    "group_name": "虚构群",
                    "timestamp": "2026-04-01 10:00",
                    "source": "bench",
                    "title": "摄影社招新面试",
                }
            ],
            "query": {
                "msg_id": "q1",
                "content": "摄影社周三下午三点在体育馆招新面试",
                "sender_name": "虚构乙",
                "sender_id": "u2",
                "session_id": "s1",
                "group_name": "虚构群",
                "timestamp": "2026-04-02 09:30",
                "source": "bench",
                "title": "摄影社周三下午三点在体育馆招新",
            },
            "expected": {"same": True},
        }
    ],
}


def _read_lines(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


class RunnerContractTest(unittest.TestCase):
    """进程内直调 runner.main：覆盖参数、产物与终态行。

    close_db 会被替换掉——它置位的 _db_closed 没有复位通道，真跑一次就会毒化
    整个 pytest 进程；「完整运行含关闭」由下面的真实 spawn 用例承担。
    """

    def setUp(self) -> None:
        self._old_db_path = config.db_path
        self._old_ai = ai_ports.get_ai()
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.cases_dir = self.tmp / "cases"
        self.cases_dir.mkdir()
        (self.cases_dir / "dedup.json").write_text(
            json.dumps(_OFFLINE_DATASET, ensure_ascii=False), encoding="utf-8"
        )
        self.run_dir = self.tmp / "run"
        self.scratch = self.tmp / "scratch.sqlite"

    def tearDown(self) -> None:
        config.db_path = self._old_db_path
        ai_ports.set_ai(self._old_ai)
        asyncio.run(_release_db_singletons())
        self._tmp.cleanup()

    def _argv(self, *extra: str) -> list[str]:
        return [
            "--run-dir",
            str(self.run_dir),
            "--db",
            str(self.scratch),
            "--features",
            "dedup",
            "--source",
            "file",
            "--cases-dir",
            str(self.cases_dir),
            "--run-id",
            "test-run-1",
            *extra,
        ]

    def test_offline_run_writes_contract_artifacts(self) -> None:
        with patch.object(bench_runner, "close_db", new=AsyncMock()):
            rc = bench_runner.main(self._argv())
        self.assertEqual(rc, 0)

        for name in ("meta.json", "progress.jsonl", "report.json", "report.html"):
            self.assertTrue((self.run_dir / name).exists(), f"缺少产物 {name}")

        meta = json.loads((self.run_dir / "meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["run_id"], "test-run-1")
        self.assertEqual(meta["db_path"], str(self.scratch))
        self.assertEqual(meta["terminal"]["kind"], "done")
        self.assertEqual(meta["terminal"]["exit_code"], 0)

        lines = _read_lines(self.run_dir / "progress.jsonl")
        self.assertEqual(lines[0]["type"], "start")
        self.assertEqual(lines[-1]["type"], "done")
        self.assertEqual(lines[-1]["kind"], "done")
        progress = [ln for ln in lines if ln["type"] == "progress"]
        self.assertTrue(progress, "至少要有一条进度行")
        for line in progress:
            self.assertEqual(
                set(line) >= {"feature", "done", "total", "failed"}, True, line
            )
            self.assertEqual(line["total"], 2)

        payload = json.loads((self.run_dir / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["run_id"], "test-run-1")
        self.assertIn("dedup", payload["features"])
        self.assertIn("summary", payload["features"]["dedup"])

        # 隔离是真的：scratch 库确实被创建并建好了表，而不是「哪儿都没写」
        self.assertTrue(self.scratch.exists())
        import sqlite3

        # 显式 close：sqlite3 上下文管理器不关连接，留着会让 Windows 上
        # 临时目录清理报 WinError 32。
        conn = sqlite3.connect(self.scratch)
        try:
            names = {
                r[0]
                for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
        finally:
            conn.close()
        self.assertIn("items", names)
        self.assertIn("categories", names)

    def test_usage_exit_when_db_is_production(self) -> None:
        rc = bench_runner.main(
            [
                "--run-dir",
                str(self.run_dir),
                "--db",
                str(config.db_path),
                "--features",
                "dedup",
            ]
        )
        self.assertEqual(rc, 2)
        self.assertFalse((self.run_dir / "meta.json").exists())

    def test_usage_exit_for_unknown_feature(self) -> None:
        rc = bench_runner.main(self._argv("--features", "nope"))
        self.assertEqual(rc, 2)

    def test_dataset_error_records_terminal_kind(self) -> None:
        empty = self.tmp / "empty"
        empty.mkdir()
        with patch.object(bench_runner, "close_db", new=AsyncMock()):
            rc = bench_runner.main(
                [
                    "--run-dir",
                    str(self.run_dir),
                    "--db",
                    str(self.scratch),
                    "--features",
                    "dedup",
                    "--source",
                    "file",
                    "--cases-dir",
                    str(empty),
                ]
            )
        self.assertNotIn(rc, (0, 2))
        lines = _read_lines(self.run_dir / "progress.jsonl")
        self.assertEqual(lines[-1]["type"], "error")
        self.assertEqual(lines[-1]["kind"], "dataset")
        meta = json.loads((self.run_dir / "meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["terminal"]["kind"], "dataset")


class RunnerSpawnTest(unittest.TestCase):
    """真实 spawn：既验证产物，也验证「退出前关库」这条义务（进程必须自己退出）。"""

    def test_spawned_run_exits_and_writes_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
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
                    "--run-id",
                    "spawn-1",
                ],
                check=False,
                cwd=str(ROOT),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=300,
                # EMBED_API_BASE 置空：嵌入去重关掉，判重缓存才不会去建向量——
                # 只跳过 chat 并不等于离线，开发机的 .env 往往配了嵌入端点。
                env={
                    **os.environ,
                    "PYTHONIOENCODING": "utf-8",
                    "EMBED_API_BASE": "",
                },
            )
            self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
            lines = _read_lines(run_dir / "progress.jsonl")
            self.assertEqual(lines[-1]["type"], "done")
            self.assertTrue((run_dir / "report.html").exists())
            self.assertTrue(scratch.exists())


class RunnerStubSpawnTest(unittest.TestCase):
    """真实 spawn + AI 替身：验证替身入口可达，且替身不吃网络也能跑完。

    与上面的离线用例互补——那条路径根本不调 AI，这条一定调。
    """

    def test_spawned_run_with_ai_stub(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            cases_dir = base / "cases"
            cases_dir.mkdir()
            (cases_dir / "dedup.json").write_text(
                json.dumps(_AI_DATASET, ensure_ascii=False), encoding="utf-8"
            )
            run_dir = base / "run"
            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "briefdesk.plugins.benchmark.runner",
                    "--run-dir",
                    str(run_dir),
                    "--db",
                    str(base / "scratch.sqlite"),
                    "--features",
                    "dedup",
                    "--source",
                    "file",
                    "--cases-dir",
                    str(cases_dir),
                    "--ai-provider",
                    "stub",
                ],
                check=False,
                cwd=str(ROOT),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=300,
                # EMBED_API_BASE 置空：嵌入去重关掉，判重缓存才不会去建向量——
                # 只跳过 chat 并不等于离线，开发机的 .env 往往配了嵌入端点。
                env={
                    **os.environ,
                    "PYTHONIOENCODING": "utf-8",
                    "EMBED_API_BASE": "",
                },
            )
            self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
            lines = _read_lines(run_dir / "progress.jsonl")
            self.assertEqual(lines[-1]["kind"], "done")
            payload = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
            summary = payload["features"]["dedup"]["summary"]
            self.assertEqual(summary["cases"], 1)
            self.assertEqual(summary["error_cases"], 0)
            # skipped=0 证明预筛给了候选、AI 替身确实被调用（走跳过路径时 skipped=1）；
            # 替身恒判「不重复」而用例期望 same=true → 记一个假阴性。
            self.assertEqual(summary["skipped"], 0)
            self.assertEqual((summary["tp"], summary["fn"]), (0, 1))


if __name__ == "__main__":
    unittest.main()
