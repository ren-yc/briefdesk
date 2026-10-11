"""基准运行监管（supervisor）的回归。

覆盖：判定→登记原子性（重复启动只起一个运行）、运行态的状态与取消、run_dir 生命周期
（meta 先落盘、回收补终态记录、轮转保底）、gc_orphans 的 best-effort，以及「结果分类
看三态、/report 只认 completed」这两条契约。

子进程模式的用例把 `_cases_sources` 指到临时夹具目录（快照就是这么来的），因此跑的是零重叠
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
from unittest.mock import patch

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


class _FakeProc:
    """假子进程句柄：只提供监控与取消会用到的成员。

    terminate 后立刻置退出码，取消用例因此不必真等一个真进程；Windows 上
    terminate 就是硬杀，退出码同样不承诺具体值。
    """

    def __init__(self) -> None:
        self.returncode: int | None = None
        self.terminated = False

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 1

    def kill(self) -> None:
        self.terminate()

    async def wait(self) -> int | None:
        return self.returncode


def _settings(**over: object) -> SimpleNamespace:
    base: dict[str, object] = {
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
            patch.object(supervisor, "_runs_root", return_value=self.run_root),
            patch.object(supervisor, "_cases_sources", return_value=(self.cases,)),
            patch.object(supervisor, "_settings", lambda: self.settings),
        ]
        for p in self._patches:
            p.start()
        supervisor._current = None
        supervisor._last = None
        supervisor._last_cache = None

    async def asyncTearDown(self) -> None:
        # 任何还在跑的运行都要收掉，否则子进程会活过测试进程
        await supervisor.stop_all()
        for p in self._patches:
            p.stop()
        supervisor._current = None
        supervisor._last = None
        supervisor._last_cache = None
        self._tmp.cleanup()

    def _pin_spawn(self):
        """把运行钉在「子进程已起、尚未退出」上：只替换 spawn 一步。

        真等一轮子进程既慢又与这两条断言无关（要测的是运行态的取消与重复启动），
        所以句柄交给 _FakeProc 顶替；监控、取消与收尾仍走生产代码。
        """

        async def _spawn(run) -> None:
            run.proc = _FakeProc()

        return [patch.object(supervisor, "_spawn", new=_spawn)]

    async def test_start_state_and_idempotent_cancel(self) -> None:
        patchers = self._pin_spawn()
        for p in patchers:
            p.start()
        try:
            info = await supervisor.start(["dedup"])
            state = supervisor.state()
            self.assertTrue(state["running"])
            self.assertEqual(state["run_id"], info["run_id"])
            self.assertIsInstance(state["elapsed_sec"], float)

            self.assertEqual(
                await supervisor.cancel(),
                {"cancelled": True, "run_id": info["run_id"]},
            )
            for _ in range(400):
                if not supervisor.is_running():
                    break
                await asyncio.sleep(0.05)
            self.assertFalse(supervisor.is_running())
        finally:
            for p in patchers:
                p.stop()
        self.assertEqual(
            await supervisor.cancel(), {"cancelled": False, "reason": "not_running"}
        )

    async def test_double_start_conflicts_at_router(self) -> None:
        patchers = self._pin_spawn()
        for p in patchers:
            p.start()
        try:
            await supervisor.start(["dedup"])
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

    def test_child_env_carries_effective_benchmark_config(self) -> None:
        """子进程环境必须带生效的 benchmark 行为配置。

        直接调真实实现——既有的子进程用例会把 _child_env mock 掉，用它断言等于
        什么都没测（下传键名改错也不会红）。
        """
        from briefdesk.config import config

        with (
            patch.object(config, "ai_reasoning_effort", "off"),
            patch.object(config, "ai_model", "m1"),
            patch.object(config, "ai_json_mode", "on"),
            patch.object(config, "max_classify_tokens", 1234),
            patch.object(config, "ai_vision_enabled", True),
            patch.object(config, "ai_vision_max_images", 7),
            patch.object(config, "embed_api_base", ""),
            patch.object(config, "embed_model", ""),
            patch.object(config, "embed_batch_size", 9),
            patch.object(config, "dedup_similarity_threshold", 0.11),
            patch.object(config, "dedup_embed_threshold", 0.77),
            patch.object(config, "dedup_embed_top_k", 5),
            patch.object(config, "dedup_embed_fallback_threshold", 0.44),
            patch.object(config, "dedup_strong_threshold", 0.98),
        ):
            env = supervisor._child_env()
        self.assertEqual(env.get("AI_REASONING_EFFORT"), "off")
        self.assertEqual(env.get("AI_MODEL"), "m1")
        self.assertEqual(env.get("AI_JSON_MODE"), "on")
        self.assertEqual(env.get("MAX_CLASSIFY_TOKENS"), "1234")
        self.assertEqual(env.get("AI_VISION_ENABLED"), "true")
        self.assertEqual(env.get("AI_VISION_MAX_IMAGES"), "7")
        self.assertEqual(env.get("EMBED_API_BASE"), "")
        self.assertEqual(env.get("EMBED_MODEL"), "")
        self.assertEqual(env.get("EMBED_BATCH_SIZE"), "9")
        self.assertEqual(env.get("DEDUP_SIMILARITY_THRESHOLD"), "0.11")
        self.assertEqual(env.get("DEDUP_EMBED_THRESHOLD"), "0.77")
        self.assertEqual(env.get("DEDUP_EMBED_TOP_K"), "5")
        self.assertEqual(env.get("DEDUP_EMBED_FALLBACK_THRESHOLD"), "0.44")
        self.assertEqual(env.get("DEDUP_STRONG_THRESHOLD"), "0.98")
        for secret_name in ("AI_API_KEY", "EMBED_API_KEY", "RAG_API_KEY"):
            self.assertNotIn(secret_name, env)

    async def test_state_does_not_scan_disk_while_running(self) -> None:
        """运行期间 state() 不扫盘：这是 3s 轮询的热路径，而扫盘是同步 I/O。

        前端在运行中只读 running/progress；上一次的摘要等运行结束（_finish 把
        _last 写进内存）自然会给出。
        """
        patchers = self._pin_spawn()
        for p in patchers:
            p.start()
        try:
            await supervisor.start(["dedup"])
            with patch.object(supervisor, "latest_report_dir") as scan:
                st = supervisor.state()
            self.assertTrue(st["running"])
            scan.assert_not_called()
        finally:
            for p in patchers:
                p.stop()

    async def test_last_from_disk_is_cached(self) -> None:
        """同一份报告不重复解析：state() 在轮询路径上，解析结果应按戳命中缓存。"""
        run_dir = self.run_root / "20260101-000000-aaaa"
        _make_completed(run_dir, "2026-01-01 00:00:00", "aaaa")
        (run_dir / "report.json").write_text(
            json.dumps(
                {
                    "run_id": "aaaa",
                    "elapsed_sec": 1.5,
                    "features": {"dedup": {"summary": {"cases": 3}}},
                }
            ),
            encoding="utf-8",
        )
        with patch.object(supervisor, "_reports", wraps=supervisor._reports) as parse:
            first = supervisor.state()
            second = supervisor.state()
        self.assertEqual(parse.call_count, 1, "第二次 state() 应命中缓存")
        self.assertEqual(first["run_id"], "aaaa")
        self.assertEqual(first["summary"], {"dedup": {"cases": 3}})
        self.assertEqual(first, second)

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


class RuntimeSettingsSnapshotTest(SupervisorTest):
    """运行参数采用时点：start 冻结进 _Run，运行中改配置不影响活动运行。

    旧实现把 settings 对象直接交给监控协程，每次读的是解析时的命名空间——
    运行中换掉 _settings 的返回值就会改变活动运行的阈值（页面值与真实运行
    各说各话）。快照必须冻结在 _Run 上。
    """

    async def test_start_freezes_runtime_settings_into_run(self) -> None:
        self.settings = _settings(run_timeout_seconds=999, run_stall_seconds=888)
        patchers = self._pin_spawn()
        for p in patchers:
            p.start()
        try:
            await supervisor.start(["dedup"])
            run = supervisor._current
            assert run is not None
            self.assertEqual(run.run_timeout_seconds, 999)
            self.assertEqual(run.run_stall_seconds, 888)
            self.assertEqual(run.pause_pipeline, False)
            # 运行中改配置：活动快照不变（「下次运行采用」才是 expected 的事）
            self.settings = _settings(run_timeout_seconds=1, run_stall_seconds=1)
            self.assertEqual(run.run_timeout_seconds, 999)
            self.assertEqual(supervisor.runtime_settings_snapshot()["run_timeout_seconds"], 999)
        finally:
            for p in patchers:
                p.stop()
        await supervisor.stop_all()

    async def test_snapshot_revoked_after_finish(self) -> None:
        patchers = self._pin_spawn()
        for p in patchers:
            p.start()
        try:
            await supervisor.start(["dedup"])
            self.assertNotEqual(supervisor.runtime_settings_snapshot(), {})
        finally:
            for p in patchers:
                p.stop()
        await supervisor.stop_all()
        self.assertEqual(
            supervisor.runtime_settings_snapshot(),
            {},
            "运行收尾后不得继续露出旧 current 的快照"
        )

    async def test_keep_runs_read_fresh_at_each_rotate(self) -> None:
        """keep_runs 每次清理重读：start 之后的改动要影响下一次轮转。"""
        _make_completed(self.run_root / "a", "2026-01-01 00:00:00", "a")
        _make_completed(self.run_root / "b", "2026-01-02 00:00:00", "b")
        self.settings = _settings(keep_runs=5)
        await supervisor._rotate()
        self.assertEqual(len(list(self.run_root.iterdir())), 2)
        self.settings = _settings(keep_runs=1)
        await supervisor._rotate()
        names = {p.name for p in self.run_root.iterdir()}
        self.assertEqual(names, {"b"}, "keep_runs=1 只留最新的 completed")


class FinishRevokeOrderTest(SupervisorTest):
    """撤销顺序：_finish 先标 finished 并撤销 _current，之后才轮转。

    旧缺陷是「先轮转、后撤销」：轮转里 _settings()/删除抛错时 _current 还
    指向已完成的运行，设置页继续显示一份本该消失的「本次基准运行采用」。
    只在收尾结束后读一次快照看不到这个窗口，所以断言点在 _rotate 内部。
    """

    async def test_snapshot_revoked_before_rotate_raises(self) -> None:
        patchers = self._pin_spawn()
        for p in patchers:
            p.start()
        observed: dict[str, object] = {}

        async def _rotate_boom() -> None:
            # 关键断言点：轮转抛错的那一刻，撤销必须已经发生。
            # 快照与 running 都不足以证明：_finish 先置 finished，快照遇 finished 即返回 {}、
            # is_running 也随之变 False——把「清 _current」挪到 _rotate 之后，这两条仍然成立。
            observed["snapshot_during_rotate"] = supervisor.runtime_settings_snapshot()
            observed["running_during_rotate"] = supervisor.state()["running"]
            observed["current_during_rotate"] = supervisor._current
            raise RuntimeError("轮转炸掉（模拟 _settings/删除失败）")

        try:
            await supervisor.start(["dedup"])
            run = supervisor._current
            assert run is not None
            self.assertNotEqual(supervisor.runtime_settings_snapshot(), {})
            with patch.object(supervisor, "_rotate", new=_rotate_boom):
                await supervisor.cancel()
                for _ in range(400):
                    if run.monitor is not None and run.monitor.done():
                        break
                    await asyncio.sleep(0.05)
                # 取回监控任务的异常（顺带断言轮转错误确实向上传播）
                monitor = run.monitor
                assert monitor is not None
                with self.assertRaises(RuntimeError):
                    await monitor
        finally:
            for p in patchers:
                p.stop()
        self.assertEqual(observed["snapshot_during_rotate"], {}, "轮转前必须已撤销快照")
        self.assertIs(observed["running_during_rotate"], False)
        self.assertIsNone(
            observed["current_during_rotate"], "轮转前运行指针必须已清空"
        )
        self.assertEqual(supervisor.runtime_settings_snapshot(), {})
        self.assertFalse(supervisor.state()["running"])
        self.assertIsNone(supervisor._current, "收尾后运行指针必须已清空")

    async def test_old_run_finish_does_not_clear_new_current(self) -> None:
        """旧 run 的收尾只清自己的字段：_finish 按身份判定撤销。

        若退回无条件 _current = None，第二个运行的快照会被第一个运行的收尾
        误清。这里对一个已退役的 run 手动补一次 _finish，断言当前运行不受影响。
        """
        patchers = self._pin_spawn()
        for p in patchers:
            p.start()
        try:
            await supervisor.start(["dedup"])
            run_a = supervisor._current
            assert run_a is not None
            stale_dir = self.run_root / "stale"
            stale_dir.mkdir()
            stale = supervisor._Run(
                run_id="stale-id", run_dir=stale_dir, features=["dedup"],
                started_at="2026-01-01 00:00:00", t0=0.0, pause=False,
            )
            await supervisor._finish(stale)
            self.assertIs(supervisor._current, run_a)
            self.assertNotEqual(supervisor.runtime_settings_snapshot(), {})
            self.assertEqual(supervisor.state()["run_id"], run_a.run_id)
        finally:
            for p in patchers:
                p.stop()


if __name__ == "__main__":
    unittest.main()
