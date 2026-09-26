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
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

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


class _FakeProc:
    """假子进程句柄：只提供监控与取消会用到的那几个成员。

    用它把运行「按住」在 running 状态——真跑一轮离线夹具几秒就结束了，
    断言窗口太窄，而在运行态上做断言才是这两条保证的意义所在。
    """

    def __init__(self) -> None:
        self.returncode: int | None = None
        self.terminated = False

    def terminate(self) -> None:
        self.terminated = True
        # Windows 上 terminate 就是硬杀，退出码同样是 1（语义不承诺）
        self.returncode = 1

    def kill(self) -> None:
        self.terminate()

    async def wait(self) -> int | None:
        return self.returncode


async def _export_gate() -> tuple[int | None, bool]:
    """直调访问守卫中间件，返回 (中间件最终返回的状态码, 桩处理器是否被调用)。

    中间件是 `@app.middleware("http")` 注册的**普通协程函数**（Starlette 的装饰器
    原样返回它），因此可以脱离 HTTP 栈直接调用；call_next 用桩代替真实处理器，
    这样导出路由不会经 get_db() 打开真实生产库。

    直调而不是拼两条独立事实，是因为真正要守的是**组合判定**「窗口开着 且 命中
    黑名单」：只断言「黑名单成员为真」与「未重定向」各自成立，中间件即使丢掉
    `in_redirect()` 前置条件（黑名单无条件生效）也照样绿。
    """
    from fastapi.responses import JSONResponse
    from starlette.requests import Request

    from briefdesk.server import middleware

    called = False

    async def _stub(_request: object) -> JSONResponse:
        nonlocal called
        called = True
        return JSONResponse({"ok": True})

    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/export/items",
            "query_string": b"",
            "scheme": "http",
            "server": ("localhost", 80),
            "headers": [(b"host", b"localhost")],
        }
    )
    response = await middleware._local_security_guard(request, _stub)
    return getattr(response, "status_code", None), called


def _subprocess_settings() -> SimpleNamespace:
    return SimpleNamespace(
        pause_pipeline=False,
        keep_runs=5,
        run_timeout_seconds=0,
        run_stall_seconds=600,
    )


class RunAvailabilityTest(unittest.TestCase):
    """运行期间界面照常可用——子进程模式的**机制保证**。

    两条用户可见收益都系在同一个不变量上：父进程**从不重定向**自己的连接，
    因此 `db.in_redirect()` 恒为假，中间件的写闸门与读黑名单整段不生效。这里
    断言的就是这个不变量，以及它带来的可观测后果（写请求会走到处理器而不是被
    409 拦下）。

    读半侧走「直调中间件 + 桩处理器」两态对照（`_export_gate`）：断言的是组合判定
    本身，且不必真打导出路由。

    为什么不在这一层断言「GET /api/items 返回真实卡片」：那条路径会经
    `get_db()` 打开 `config.db_path` 指向的真实生产库，测试进程里留下一条
    aiosqlite 连接（非 daemon 线程）会把 pytest 卡在退出阶段。渲染层面的验收
    留给人工完整验证；这里断言的是让渲染成立的那个机制。
    """

    def test_run_does_not_block_writes_or_reads(self) -> None:
        from starlette.testclient import TestClient

        import briefdesk.server as srv
        from briefdesk import db as briefdesk_db
        from briefdesk.plugins.benchmark import router as bench_router
        from briefdesk.plugins.benchmark import supervisor

        srv.include_plugin_router(bench_router.router)
        with tempfile.TemporaryDirectory() as tmp:
            run_root = Path(tmp) / "runs"
            run_root.mkdir()
            supervisor._current = None
            supervisor._last = None
            try:
                with patch.object(supervisor, "RUN_ROOT", run_root), patch.object(
                    supervisor, "_settings", _subprocess_settings
                ), patch.object(supervisor, "_spawn", new=_fake_spawn):
                    client = TestClient(
                        srv.app,
                        base_url="http://localhost",
                        headers={"Origin": "http://localhost"},
                    )
                    with client:
                        resp = client.post(
                            "/api/benchmark/run", json={"features": ["dedup"]}
                        )
                        self.assertEqual(resp.status_code, 200, resp.text)
                        self.assertTrue(resp.json()["started"])

                        # 运行期间：没有重定向（这就是闸门不生效的原因）
                        self.assertFalse(
                            briefdesk_db.in_redirect(),
                            "子进程模式不得重定向父进程的连接",
                        )
                        # 写请求走到处理器（400 = body 非法）而不是被闸门 409
                        blocked = client.post(
                            "/api/items/batch", json={"ids": [], "action": "ignore"}
                        )
                        self.assertNotEqual(blocked.status_code, 409, blocked.text)
                        self.assertEqual(blocked.status_code, 400, blocked.text)
                        # 读半侧：直调访问守卫中间件，两态对照。正例证明它不误伤
                        # （运行期间导出照常放行），反例证明它没失效（窗口内必须 409，
                        # 且不再往下走）。断言的是中间件的组合判定，不是两条各自
                        # 恒真的独立事实。
                        # 放行时中间件原样返回处理器的响应（桩给 200）
                        code, called = asyncio.run(_export_gate())
                        self.assertEqual(code, 200, "运行期间导出路由不该被拦")
                        self.assertTrue(called, "运行期间导出请求必须走到处理器")
                        with patch.object(briefdesk_db, "_redirect_active", True):
                            code, called = asyncio.run(_export_gate())
                        self.assertEqual(code, 409, "窗口内导出路由必须被拦")
                        self.assertFalse(called, "被拦时不得调用处理器")

                        state = client.get("/api/benchmark/run").json()
                        self.assertTrue(state["running"])

                        # 取消 → 状态回落，且 meta 补记 aborted（这次运行没有终态行）
                        cancelled = client.delete("/api/benchmark/run")
                        self.assertEqual(cancelled.status_code, 200)
                        self.assertTrue(cancelled.json()["cancelled"])
                        for _ in range(100):
                            if not client.get("/api/benchmark/run").json()["running"]:
                                break
                            time.sleep(0.05)
                        self.assertFalse(
                            client.get("/api/benchmark/run").json()["running"],
                            "取消后状态必须回落",
                        )
                    run_dir = next(run_root.iterdir())
                    meta = json.loads(
                        (run_dir / "meta.json").read_text(encoding="utf-8")
                    )
                    self.assertEqual(meta["terminal"]["state"], "aborted")
                    self.assertIn("取消", str(meta["terminal"]["reason"]))
                    # 被取消的运行目录保留（供排查），不被当成残目录清掉
                    self.assertTrue(run_dir.exists())
            finally:
                supervisor._current = None
                supervisor._last = None


async def _fake_spawn(run) -> None:
    run.proc = _FakeProc()


if __name__ == "__main__":
    unittest.main()
