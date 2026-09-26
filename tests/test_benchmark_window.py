"""基准窗口（db_redirect）隔离的单元用例。

覆盖窗口判据、进出取锁点的取消安全、与 DB 耦合的进程内存派生状态挂起
（去重缓存、RAG 向量缓存与维护循环）以及备份的数据库层第二道防线。

背景：隔离边界此前画在 **DB 连接层**，而正确性要求画在**应用状态层**。
窗口内单例指向临时库，但事件发布仍会改生产去重缓存、RAG 维护循环会在
没有 rag 表的临时库上报错退避、备份会把临时库落成「日后恢复即整库替换」
的假备份。这些用例逐条钉住修复，并覆盖取消路径（泄漏的 aiosqlite 非 daemon
worker 线程会让解释器退出挂死）。
"""

from __future__ import annotations

import asyncio
import hashlib
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi import HTTPException

import briefdesk.db as db_mod
from briefdesk.db import (
    BackupDuringRedirectError,
    backup_db_to,
    db_redirect,
    in_redirect,
    insert_item,
)

_QUOTE = "虚构原文：周三下午在活动室开例会，请提前十分钟到场"
_TITLE = "虚构活动通知标题"


class _AcquireRaisesLock:
    """acquire 恒抛指定异常的锁桩（模拟取锁点被取消）。"""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def acquire(self) -> None:
        raise self._exc

    def release(self) -> None:  # pragma: no cover — 未取得的锁不得释放
        raise AssertionError("未取得的锁不得释放")


class _SecondAcquireRaisesLock:
    """首次 acquire 成功、其后每次失败的锁桩（模拟取消落在退出取锁 await 上）。"""

    def __init__(self) -> None:
        self._calls = 0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        self._calls += 1
        if self._calls >= 2:
            raise asyncio.CancelledError()
        await self._lock.acquire()

    def release(self) -> None:
        self._lock.release()

    def locked(self) -> bool:
        return self._lock.locked()


def _spy_init(created: list):
    """包装 _init_connection：记录每条被关闭的连接，供取消路径断言。"""

    real_init = db_mod._init_connection

    async def spy_init(path, **kwargs):
        conn = await real_init(path, **kwargs)
        orig_close = conn.close

        async def spy_close():
            created.append(conn)
            await orig_close()

        conn.close = spy_close
        return conn

    return spy_init


def _sql_free_conn() -> Mock:
    """任何 SQL 都直接失败的连接桩：窗口守卫若失效会立刻炸出来。"""

    conn = Mock()
    conn.execute = AsyncMock(
        side_effect=AssertionError("窗口内不得在临时库上执行 SQL")
    )
    return conn


class TestRedirectPredicate:
    """窗口判据：进出窗口的标志与单例一致，失败路径不留置位。"""

    async def test_flag_follows_window(self, tmp_path):
        assert in_redirect() is False, "窗口外必须为假"
        async with db_redirect(str(tmp_path / "bench.sqlite")) as (main_conn, _e):
            assert in_redirect() is True, "窗口内必须为真"
            cur = await main_conn.execute("SELECT COUNT(*) AS c FROM items")
            assert (await cur.fetchone())["c"] == 0
        assert in_redirect() is False, "退出后必须复位"
        assert not db_mod.storage_lock.locked(), "退出路径必须释放存储锁"

    async def test_second_connection_failure_leaves_no_flag(self, tmp_path):
        calls = {"n": 0}
        real_init = db_mod._init_connection

        async def flaky_init(path, **kwargs):
            calls["n"] += 1
            if calls["n"] >= 2:
                raise RuntimeError("disk full")
            return await real_init(path, **kwargs)

        with (
            patch.object(db_mod, "_init_connection", flaky_init),
            pytest.raises(RuntimeError),
        ):
            async with db_redirect(str(tmp_path / "bench.sqlite")):
                pass  # 不可达：进入即失败

        assert in_redirect() is False, "半程失败不得留下置位"
        assert not db_mod.storage_lock.locked()


class TestRedirectCancellationSafety:
    """取消安全：取消落在新增的取锁 await 上时，还原与关闭都必须必达。"""

    async def test_cancel_on_entry_lock_closes_both_connections(self, tmp_path):
        created: list = []
        saved_main, saved_embed = db_mod._db, db_mod._embed_db
        with (
            patch.object(db_mod, "_init_connection", _spy_init(created)),
            patch.object(
                db_mod, "storage_lock", _AcquireRaisesLock(asyncio.CancelledError())
            ),
            pytest.raises(asyncio.CancelledError),
        ):
            async with db_redirect(str(tmp_path / "bench.sqlite")):
                pass  # 不可达：进入取锁即被取消

        assert len(created) == 2, "取锁被取消也必须关掉刚建好的两条连接"
        assert db_mod._db is saved_main and db_mod._embed_db is saved_embed
        assert in_redirect() is False
        assert not db_mod.storage_lock.locked()

    async def test_cancel_on_exit_lock_still_restores_and_closes(self, tmp_path):
        created: list = []
        saved_main, saved_embed = db_mod._db, db_mod._embed_db
        fake_lock = _SecondAcquireRaisesLock()
        with (
            patch.object(db_mod, "_init_connection", _spy_init(created)),
            patch.object(db_mod, "storage_lock", fake_lock),
            pytest.raises(asyncio.CancelledError),
        ):
            async with db_redirect(str(tmp_path / "bench.sqlite")):
                pass  # 正常进入；退出时取锁被取消

        assert db_mod._db is saved_main, "取消落在退出取锁 await 上仍须还原单例"
        assert db_mod._embed_db is saved_embed
        assert in_redirect() is False, "取消路径同样必须清除窗口标志"
        assert len(created) == 2, "取消路径不得泄漏两条临时连接"
        assert not fake_lock.locked(), "已取得的锁必须释放"
        assert not db_mod.storage_lock.locked()


class TestDedupCacheSuspend:
    """窗口内去重缓存挂起：不得按临时库内容增删生产缓存。"""

    async def test_cache_mutations_are_noop_in_window(self, tmp_path):
        from briefdesk.plugins.dedup.engine import CachedItem, DedupEngine

        engine = DedupEngine()
        engine._cache = [CachedItem(id="keep", title="生产卡片")]
        engine._pending_embeds = [("keep", "m", [1.0])]

        async with db_redirect(str(tmp_path / "bench.sqlite")):
            engine.add_to_cache("ghost", "临时库合成卡")
            engine.remove_items(["keep"])
            assert [it.id for it in engine._cache] == ["keep"], (
                "窗口内不得删生产缓存条目（会让生产卡退出判重、相似新消息重复建卡）"
            )
            assert engine._pending_embeds == [("keep", "m", [1.0])]

        # 窗口外照常生效（挂起不是永久失效）
        engine.add_to_cache("new", "新卡片")
        engine.remove_items(["keep"])
        assert [it.id for it in engine._cache] == ["new"]

    async def test_probe_engine_still_reads_window_db(self, tmp_path):
        """防回归：挂起不得覆盖 _ensure_cache，基准判重用例必须仍能从临时库命中。"""
        from briefdesk.plugins.benchmark.engine import _ProbeDedupEngine

        async with db_redirect(str(tmp_path / "bench.sqlite")):
            item_id = await insert_item(
                {
                    "category": "活动通知",
                    "title": _TITLE,
                    "source": "weflow",
                    "source_msg_id": "m-1",
                    "source_quote": _QUOTE,
                    "source_group": "虚构群",
                    "content_hash": hashlib.sha256(
                        _QUOTE.encode()
                    ).hexdigest()[:16],
                }
            )
            with patch(
                "briefdesk.plugins.dedup.engine.is_embedding_enabled",
                return_value=False,
            ):
                engine = _ProbeDedupEngine()
                await engine._ensure_cache()
                assert [it.id for it in engine._cache] == [item_id], (
                    "窗口内 _ensure_cache 必须仍从临时库加载卡片（否则基准判重全判 False）"
                )
                result = await engine.check_dedup(
                    _TITLE,
                    source_group="虚构群2",
                    source_quote=_QUOTE,
                    source="weflow",
                )
            assert result.is_duplicate is True, "窗口内基准判重仍须命中临时库卡片"


class TestRagWindowSkip:
    """RAG 窗口内跳过：维护循环安静短轮询，引擎内守卫拦住临时库上的 SQL。"""

    async def test_maintenance_loop_polls_in_window(self, tmp_path):
        from briefdesk.plugins.rag.plugin import (
            _REDIRECT_POLL_SECONDS,
            RagPlugin,
        )

        plugin = RagPlugin()
        engine = Mock()
        engine.backfill_step = AsyncMock(
            side_effect=AssertionError("窗口内不得回填")
        )
        engine.maintenance_gc = AsyncMock(side_effect=AssertionError("窗口内不得跑 GC"))
        engine.warm_vectors = AsyncMock(side_effect=AssertionError("窗口内不得预热"))
        plugin._engine = engine
        sleeps: list[float] = []

        async def fake_sleep(delay: float) -> None:
            sleeps.append(delay)
            plugin._engine = None  # 让 while 条件退出，避免真等一轮

        async with db_redirect(str(tmp_path / "bench.sqlite")):
            with patch("briefdesk.plugins.rag.plugin.asyncio.sleep", fake_sleep):
                await plugin._maintenance_loop()

        assert sleeps == [_REDIRECT_POLL_SECONDS], (
            "窗口内必须以短轮询间隔让出 CPU，不得 sleep(0) 空转"
        )

    async def test_maintenance_loop_runs_outside_window(self, tmp_path):
        from briefdesk.plugins.rag.plugin import RagPlugin

        plugin = RagPlugin()
        calls: list[str] = []

        async def backfill_step(now_ts: int) -> int:
            calls.append("backfill")
            # 用取消终止循环：本用例只验证「窗口外会真正走到回填」，
            # 循环自身的休眠语义由上面那条用例覆盖
            raise asyncio.CancelledError()

        engine = Mock()
        engine.backfill_step = backfill_step
        engine.last_cycle_embed_failed = False
        plugin._engine = engine

        with pytest.raises(asyncio.CancelledError):
            await plugin._maintenance_loop()
        assert calls == ["backfill"], "窗口外维护循环必须照常推进回填"

    async def test_run_gc_skips_and_keeps_dirty_flag(self, tmp_path):
        from briefdesk.plugins.rag.plugin import RagPlugin

        plugin = RagPlugin()
        engine = Mock()
        engine.maintenance_gc = AsyncMock(side_effect=AssertionError("窗口内不得跑 GC"))
        plugin._engine = engine
        plugin._gc_dirty = True

        async with db_redirect(str(tmp_path / "bench.sqlite")):
            await plugin._run_gc()

        assert plugin._gc_dirty is True, "窗口内跳过不得清脏标志（否则这次删除永久漏账）"

    async def test_maintenance_gc_returns_zero_in_window(self, tmp_path):
        from briefdesk.plugins.rag.config import RagSettings
        from briefdesk.plugins.rag.engine import RagEngine

        async with db_redirect(str(tmp_path / "bench.sqlite")):
            conn = _sql_free_conn()

            async def factory():
                return conn

            engine = RagEngine(RagSettings(), db_factory=factory, embed_factory=factory)
            assert await engine.maintenance_gc() == 0

    async def test_warm_vectors_skips_and_keeps_watermark(self, tmp_path):
        from briefdesk import ai_ports
        from briefdesk.plugins.rag.config import RagSettings
        from briefdesk.plugins.rag.engine import RagEngine

        async with db_redirect(str(tmp_path / "bench.sqlite")):
            conn = _sql_free_conn()

            async def factory():
                return conn

            engine = RagEngine(RagSettings(), db_factory=factory, embed_factory=factory)
            # 模型名对齐，避免触发「模型切换 → 重建缓存」这条与窗口无关的分支
            engine._vec_model = ai_ports.embed_model_name()
            engine._vec_watermark = "wm-1"
            engine._vec_count_seen = 7

            await engine.warm_vectors(force_full=True)

        assert engine._vec_watermark == "wm-1", (
            "窗口内不得归零水位：归零会让删除检测恒为假，已删内容窗口结束后仍可被引用"
        )
        assert engine._vec_count_seen == 7


class TestBackupWindowGuard:
    """备份的数据库层第二道防线：窗口内拒绝产出假备份。"""

    async def test_backup_db_to_raises_in_window(self, tmp_path):
        async with db_redirect(str(tmp_path / "bench.sqlite")):
            out = tmp_path / "out.sqlite"
            with pytest.raises(BackupDuringRedirectError):
                await backup_db_to(str(out))
            assert not out.exists(), "拒绝路径不得产出任何文件"

    async def test_api_backup_maps_to_409_without_temp_file(self, tmp_path):
        from briefdesk.server import routes_items

        db_path = tmp_path / "briefdesk.sqlite"
        before = set(routes_items._BACKUP_TMP_PATHS)
        with patch.object(routes_items.config, "db_path", str(db_path)):
            async with db_redirect(str(tmp_path / "bench.sqlite")):
                with pytest.raises(HTTPException) as excinfo:
                    await routes_items.api_backup()

        assert excinfo.value.status_code == 409
        assert excinfo.value.detail["code"] == "benchmark_running"
        assert set(routes_items._BACKUP_TMP_PATHS) == before, "409 路径不得残留临时文件登记"
        assert [n for n in tmp_path.iterdir() if n.name.startswith("briefdesk-backup-")] == []
