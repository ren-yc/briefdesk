"""基准运行监管（supervisor）与双轨语义的回归。

覆盖：判定→登记原子性（重复启动只起一个运行）、inproc 与 subprocess 的状态与取消、
run_dir 生命周期（meta 先落盘、回收补终态记录、轮转保底）、gc_orphans 的 best-effort，
以及双轨期「结果分类看三态、/report 只认 completed」这两条契约。

子进程模式的用例把 CASES_SRC 指到临时夹具目录（快照就是这么来的），因此跑的是零重叠
的离线用例：不发 chat 请求，也不碰用户导出的真实用例。
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from briefdesk.plugins.benchmark import router as bench_router
from briefdesk.plugins.benchmark import supervisor

_OFFLINE_CASE = {
    "id": "sup-1",
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


def _settings(mode: str = "inproc", **over: object) -> SimpleNamespace:
    base: dict[str, object] = {
        "run_mode": mode,
        "pause_pipeline": False,
        "keep_runs": 2,
        "run_timeout_seconds": 0,
        "run_stall_seconds": 600,
    }
    base.update(over)
    return SimpleNamespace(**base)


def _make_completed(run_dir: Path, started_at: str, run_id: str) -> None:
    """造一个「已完成」的运行目录，供轮转/挑目录用例使用。"""
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "meta.json").write_text(
        json.dumps({"run_id": run_id, "started_at": started_at}), encoding="utf-8"
    )
    (run_dir / "progress.jsonl").write_text(
        json.dumps({"type": "done", "kind": "done"}) + "\n", encoding="utf-8"
    )
    (run_dir / "report.json").write_text(
        json.dumps({"run_id": run_id, "features": {}}), encoding="utf-8"
    )
    (run_dir / "report.html").write_text("<html></html>", encoding="utf-8")


class SupervisorTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.run_root = self.base / "runs"
        self.run_root.mkdir()
        self.cases = self.base / "cases"
        self.cases.mkdir()
        (self.cases / "dedup.fromweb.json").write_text(
            json.dumps({"feature": "dedup", "cases": [_OFFLINE_CASE]}),
            encoding="utf-8",
        )
        self.settings = _settings()
        self._patches: list[Any] = [
            patch.object(supervisor, "RUN_ROOT", self.run_root),
            patch.object(supervisor, "CASES_SRC", self.cases),
            patch.object(supervisor, "_settings", lambda: self.settings),
        ]
        for p in self._patches:
            p.start()
        supervisor._current = None
        supervisor._last = None

    async def asyncTearDown(self) -> None:
        # 任何还在跑的运行都要收掉，否则子进程会活过测试进程
        await supervisor.stop_all()
        for p in self._patches:
            p.stop()
        supervisor._current = None
        supervisor._last = None
        self._tmp.cleanup()

    def _pin_inproc(self, *, slow: bool = True):
        """把 inproc 路径钉在「加载用例为空 + 运行体可控」上。

        真跑 run_benchmark_cases 会经环境缝打开生产库；用例里必须替换掉它，
        否则测试会往真实库里写合成卡片。
        """
        entered = asyncio.Event()

        async def _body(*_a, **_k):
            entered.set()
            if slow:
                await asyncio.sleep(3600)
            return {"run_id": "x", "features": {}, "elapsed_sec": 0.0}, {}

        return entered, [
            patch(
                "briefdesk.plugins.benchmark.engine.load_web_cases",
                new=AsyncMock(return_value=[]),
            ),
            patch("briefdesk.plugins.benchmark.engine.run_benchmark_cases", new=_body),
        ]

    async def test_inproc_start_state_and_idempotent_cancel(self) -> None:
        entered, patchers = self._pin_inproc()
        for p in patchers:
            p.start()
        try:
            info = await supervisor.start(["dedup"])
            await asyncio.wait_for(entered.wait(), timeout=5)
            state = supervisor.state()
            self.assertTrue(state["running"])
            self.assertEqual(state["run_id"], info["run_id"])
            self.assertIsInstance(state["elapsed_sec"], float)

            self.assertEqual(
                await supervisor.cancel(),
                {"cancelled": True, "run_id": info["run_id"]},
            )
            for _ in range(100):
                if not supervisor.is_running():
                    break
                await asyncio.sleep(0.02)
            self.assertFalse(supervisor.is_running())
        finally:
            for p in patchers:
                p.stop()
        self.assertEqual(
            await supervisor.cancel(), {"cancelled": False, "reason": "not_running"}
        )

    async def test_double_start_conflicts_at_router(self) -> None:
        entered, patchers = self._pin_inproc()
        for p in patchers:
            p.start()
        try:
            await supervisor.start(["dedup"])
            await asyncio.wait_for(entered.wait(), timeout=5)
            with self.assertRaises(RuntimeError):
                await supervisor.start(["dedup"])
            with self.assertRaises(HTTPException) as ctx:
                await bench_router.start_run(None)
            self.assertEqual(ctx.exception.status_code, 409)
            self.assertIn("基准正在运行中", str(ctx.exception.detail))
        finally:
            for p in patchers:
                p.stop()

    async def test_subprocess_run_completes_and_serves_report(self) -> None:
        self.settings = _settings(mode="subprocess")
        env = dict(os.environ)
        env["EMBED_API_BASE"] = ""  # 判重缓存不去建向量：夹具要真正离线
        with patch.object(supervisor, "_child_env", lambda: env):
            info = await supervisor.start(["dedup"])
            for _ in range(1200):
                if not supervisor.is_running():
                    break
                await asyncio.sleep(0.1)
        self.assertFalse(supervisor.is_running(), "子进程未在限时内结束")

        dirs = list(self.run_root.iterdir())
        self.assertEqual(len(dirs), 1)
        run_dir = dirs[0]
        self.assertEqual(run_dir.name.startswith("20"), True)
        self.assertTrue((run_dir / "cases" / "dedup.fromweb.json").exists(), "缺用例快照")

        meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["run_id"], info["run_id"])
        self.assertEqual(meta["terminal"]["state"], "completed")
        self.assertEqual(meta["terminal"]["exit_code"], 0)

        payload = supervisor.report_payload()
        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(payload["run_id"], info["run_id"])
        self.assertIn("dedup", payload["features"])
        html = supervisor.report_html()
        self.assertTrue(html and "html" in html.lower())

        state = supervisor.state()
        self.assertFalse(state["running"])
        self.assertEqual(state["run_id"], info["run_id"])
        self.assertIn("summary", state)

    async def test_dual_track_payload_isomorphic(self) -> None:
        """双轨同构：同一批离线用例，inproc 与 subprocess 的 payload 结构必须一致。

        比较口径按契约取「顶层键集合 + features[*].summary 键集合」：报告是由同一段
        聚合代码产出的，模式差异只应体现在运行环境，不应体现在结果形状上。
        """
        import aiosqlite

        from briefdesk import db as briefdesk_db
        from briefdesk.db import init_schema
        from briefdesk.plugins.benchmark import store as bench_store

        cases, _errors = bench_store.parse_cases_with_errors(
            "dedup", [{"feature": "dedup", **_OFFLINE_CASE}]
        )
        self.assertTrue(cases)
        conn = await aiosqlite.connect(":memory:")
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)
        # inproc 这一半必须钉住库与用例来源：真跑会经环境缝打开生产库，
        # 并从包内 cases/ 读到用户导出的真实用例（那会发真实 AI 请求）
        patchers = [
            patch.object(briefdesk_db, "get_db", new=AsyncMock(return_value=conn)),
            patch(
                "briefdesk.plugins.benchmark.engine.load_web_cases",
                new=AsyncMock(return_value=cases),
            ),
            patch(
                "briefdesk.plugins.dedup.engine.is_embedding_enabled",
                return_value=False,
            ),
        ]
        for p in patchers:
            p.start()
        try:
            await supervisor.start(["dedup"])
            for _ in range(300):
                if not supervisor.is_running():
                    break
                await asyncio.sleep(0.02)
            self.assertFalse(supervisor.is_running(), "inproc 运行未结束")
            self.assertIsNotNone(supervisor._last)
            assert supervisor._last is not None
            inproc_summary = supervisor._last["summary"]["dedup"]
        finally:
            for p in patchers:
                p.stop()
            await conn.close()

        self.settings = _settings(mode="subprocess")
        env = dict(os.environ)
        env["EMBED_API_BASE"] = ""
        with patch.object(supervisor, "_child_env", lambda: env):
            await supervisor.start(["dedup"])
            for _ in range(1200):
                if not supervisor.is_running():
                    break
                await asyncio.sleep(0.1)
        payload = supervisor.report_payload()
        assert payload is not None
        sub_summary = payload["features"]["dedup"]["summary"]

        self.assertEqual(set(inproc_summary), set(sub_summary))
        self.assertEqual(inproc_summary["cases"], sub_summary["cases"])
        self.assertTrue(
            {"run_id", "generated_at", "model", "concurrency", "elapsed_sec", "features"}
            <= set(payload)
        )

    async def test_rotation_keeps_latest_completed(self) -> None:
        _make_completed(self.run_root / "20260101-000000-aaaa0001", "2026-01-01 00:00:00", "r1")
        _make_completed(self.run_root / "20260102-000000-bbbb0002", "2026-01-02 00:00:00", "r2")
        latest = self.run_root / "20260103-000000-cccc0003"
        _make_completed(latest, "2026-01-03 00:00:00", "r3")
        # 一个失败目录（没有终态行）同样计入轮转，但不占保底名额
        failed = self.run_root / "20260104-000000-dddd0004"
        failed.mkdir()
        (failed / "meta.json").write_text(
            json.dumps({"run_id": "r4", "started_at": "2026-01-04 00:00:00"}),
            encoding="utf-8",
        )
        await supervisor._rotate()
        remaining = {p.name for p in self.run_root.iterdir()}
        self.assertEqual(len(remaining), 2, remaining)
        self.assertIn(latest.name, remaining, "最新一个 completed 必须留下")
        self.assertIn(failed.name, remaining)

    async def test_gc_orphans_is_best_effort(self) -> None:
        orphan = self.run_root / "20260101-000000-deadbeef"
        orphan.mkdir()
        await supervisor.gc_orphans()
        self.assertFalse(orphan.exists(), "缺 meta.json 的残目录应被清掉")
        with patch.object(supervisor, "_run_dirs", side_effect=OSError("boom")):
            await supervisor.gc_orphans()  # 不得向上抛：抛了会让插件被标记 failed


if __name__ == "__main__":
    unittest.main()
