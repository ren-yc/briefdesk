"""benchmark 基准环境门闸与清理边界测试。

- bench_environment 进入时暂停生产管道（set_processing_paused(True)）、
  经 db.db_redirect 重定向主/向量连接，退出先还原连接再复位标志；
- 清理只删除本次运行的 uuid 子目录，共享 .tmp 根目录内的其它内容不受影响。

连接走真实 _init_connection 落在插件 .tmp 临时目录（运行结束即清理）；
连接创建失败的半程防护由 db.db_redirect 内部保证（见
ProviderResourceAcquisitionFailureTest）。
"""

import asyncio
import shutil
import unittest
from unittest.mock import AsyncMock, patch

import pytest

import briefdesk.db as briefdesk_db
from briefdesk.plugins.benchmark import providers


class TestBenchmarkEnvPauseGate:
    """进入基准环境暂停管道，退出先还原 DB 连接再复位标志。"""

    async def test_pause_flag_toggled_and_db_restored(self):
        old_main, old_embed = briefdesk_db._db, briefdesk_db._embed_db
        with patch("briefdesk.pipeline.set_processing_paused") as paused:
            async with providers.bench_environment(register_ai=False):
                paused.assert_called_once_with(True)
                assert briefdesk_db._db is not old_main  # 已重定向
                assert briefdesk_db._embed_db is not old_embed
            assert [c.args for c in paused.call_args_list] == [(True,), (False,)]
            assert briefdesk_db._db is old_main  # 已还原
            assert briefdesk_db._embed_db is old_embed


class TestBenchmarkEnvCleanupScope:
    """清理只删本次运行子目录；根目录内既有内容必须保留。"""

    async def test_cleanup_keeps_root_sentinel(self):
        providers._TMP_ROOT.mkdir(parents=True, exist_ok=True)
        sentinel = providers._TMP_ROOT / "sentinel-keep.txt"
        sentinel.write_text("keep", encoding="utf-8")
        # 相对断言：忽略其它测试/进程遗留的 bench-*，只看本次运行的增减
        pre_existing = set(providers._TMP_ROOT.glob("bench-*"))
        try:
            with (
                # 本测试只关注清理边界：门闸函数打桩（管道侧实现已合并）
                patch("briefdesk.pipeline.set_processing_paused"),
            ):
                async with providers.bench_environment(register_ai=False):
                    current = set(providers._TMP_ROOT.glob("bench-*")) - pre_existing
                    assert len(current) == 1
                assert sentinel.exists(), ".tmp 根目录被整体删除"
                leftovers = set(providers._TMP_ROOT.glob("bench-*")) - pre_existing
                assert leftovers == set(), "本次运行的子目录未被清理"
        finally:
            if sentinel.exists():
                sentinel.unlink()


class TestProviderResourceAcquisitionFailure:
    """连接创建失败也必须回收已获取资源与本次子目录。

    半程防护已下沉到 db.db_redirect 内部：第二条连接创建失败时由缝关闭
    第一条并上抛；本测试注入 _init_connection 第二次调用失败，验证该
    防护与子目录清理、管道标志复位联动。"""

    async def test_second_connection_failure_cleans_up(self):
        real_init = briefdesk_db._init_connection
        created: list = []
        closed = {"v": False}
        calls = {"n": 0}

        async def flaky_init(path, **kwargs):
            calls["n"] += 1
            if calls["n"] >= 2:
                raise RuntimeError("disk full")
            conn = await real_init(path, **kwargs)
            orig_close = conn.close

            async def spy_close():
                closed["v"] = True
                await orig_close()

            conn.close = spy_close
            created.append(conn)
            return conn

        root = providers._TMP_ROOT
        before = set(root.glob("bench-*"))
        with (
            patch.object(briefdesk_db, "_init_connection", new=flaky_init),
            pytest.raises(RuntimeError),
        ):
            async with providers.bench_environment():
                pass  # 不可达：进入即失败
        # 已获取的 main_conn 被缝关闭（不残留非 daemon worker 线程）；
        # 本次子目录被清理（其余目录不动）
        assert len(created) == 1
        assert closed["v"], "半程失败的 main_conn 未被关闭"
        assert set(root.glob("bench-*")) == before
        # 失败路径经 finally 复位，管道标志不得残留置位
        from briefdesk import pipeline as _pipeline

        assert not _pipeline._processing_paused


if __name__ == "__main__":
    unittest.main()


class TestDrainWait:
    """重定向前等待在途批次排空：计数归零即通过，无进展达阈值才中止。

    判据是**无进展**阈值而非总时长上限：进展信号只在批边界变化，而单批
    内部的 AI 调用可能比任何固定总超时都慢（分类最坏 360s，其后还有一段
    并行双调用），总超时会误杀正常推进的慢批。
    """

    async def test_returns_true_when_drained(self):
        with patch.object(
            providers, "get_sync_progress", return_value={"pendingCount": 0}
        ):
            assert await providers._wait_pipelines_drained(0.1)

    async def test_returns_false_when_stalled(self):
        with patch.object(
            providers, "get_sync_progress", return_value={"pendingCount": 3}
        ):
            assert not await providers._wait_pipelines_drained(0.1)

    async def test_keeps_waiting_while_progress_continues(self):
        """计数持续变化时不得中止——即使累计耗时已远超阈值。"""
        scripted = [3, 2, 1, 0]
        calls = {"n": 0}

        def progress():
            value = scripted[min(calls["n"], len(scripted) - 1)]
            calls["n"] += 1
            return {"pendingCount": value}

        # 阈值取到与单次轮询同量级：按总时长实现的版本会在中途就返回 False
        with patch.object(providers, "get_sync_progress", side_effect=progress):
            assert await providers._wait_pipelines_drained(0.05)


class TestDrainTimeoutAborts:
    """排空超时必须直接中止基准，不得带警告继续重定向。

    继续重定向会让在途批次的后续写落进临时基准库，并在生产去重缓存留下
    幽灵条目（去重缓存是进程级内存态，切库不会清）。
    """

    async def test_drain_timeout_aborts_before_redirect(self):
        root = providers._TMP_ROOT
        before = set(root.glob("bench-*"))
        with (
            patch.object(
                providers, "_wait_pipelines_drained",
                new=AsyncMock(return_value=False),
            ),
            patch.object(providers, "db_redirect") as redirect,
            patch("briefdesk.pipeline.set_processing_paused") as paused,
            pytest.raises(RuntimeError, match="无进展"),
        ):
            async with providers.bench_environment(register_ai=False):
                pass  # 不可达：排空前即中止
        redirect.assert_not_called()
        assert [c.args for c in paused.call_args_list] == [(True,), (False,)]
        assert set(root.glob("bench-*")) == before, "中止路径必须清理运行目录"
        from briefdesk import pipeline as _pipeline

        assert not _pipeline._processing_paused


class TestBenchmarkAnnouncements:
    """双公告：准备中先于等待发布且不声称拒绝写操作；撤销失败不阻断还原。"""

    async def test_preparing_announced_before_wait_without_block_claim(self):
        from briefdesk import ai_ports, announcements

        announcements.reset_announcements()
        preparing_seen: list[str] = []
        ai_sentinel = object()

        async def fake_wait(stall_seconds):
            # 等待开始时「准备中」必须已发布：这一段最长可达无进展阈值
            snapshot = {
                a["code"]: a["message"] for a in announcements.get_announcements()
            }
            preparing_seen.append(snapshot.get("benchmark_preparing", ""))
            return True

        try:
            with (
                patch.object(providers, "_wait_pipelines_drained", new=fake_wait),
                patch("briefdesk.pipeline.set_processing_paused"),
                patch.object(providers.ai_ports, "get_ai", return_value=ai_sentinel),
                patch.object(providers.ai_ports, "set_ai") as set_ai,
            ):
                async with providers.bench_environment(register_ai=False):
                    inside = {a["code"] for a in announcements.get_announcements()}
                    assert "benchmark_running" in inside
            assert preparing_seen == [
                "基准准备中：消息处理已暂停，正在等待在途批次排空"
            ]
            # 排空阶段 DB 仍是生产库，写操作自洽且安全——公告不得声称被拒绝
            for forbidden in ("不可用", "拒绝", "暂不", "请勿"):
                assert forbidden not in preparing_seen[0], (
                    "准备中公告不得暗示写操作被拒绝（该阶段写是允许的）"
                )
            assert announcements.get_announcements() == [], "退出必须撤销两条公告"
            assert set_ai.call_args_list[-1].args == (ai_sentinel,)
        finally:
            announcements.reset_announcements()
            ai_ports.set_ai(None)

    async def test_revoke_failure_still_restores_pause_flag_and_ai(self):
        from briefdesk import ai_ports

        ai_sentinel = object()
        with (
            patch.object(
                providers,
                "_wait_pipelines_drained",
                new=AsyncMock(return_value=True),
            ),
            patch("briefdesk.pipeline.set_processing_paused") as paused,
            patch.object(providers.ai_ports, "get_ai", return_value=ai_sentinel),
            patch.object(providers.ai_ports, "set_ai") as set_ai,
            patch.object(providers.announcements, "announce", new=AsyncMock()),
            patch.object(
                providers.announcements,
                "revoke",
                new=AsyncMock(side_effect=RuntimeError("公告通道故障")),
            ),
        ):
            async with providers.bench_environment(register_ai=False):
                pass

        assert [c.args for c in paused.call_args_list] == [(True,), (False,)], (
            "撤销公告抛错时管道暂停标志仍必须复位（否则管道一直停着）"
        )
        assert set_ai.call_args_list[-1].args == (ai_sentinel,), (
            "撤销公告抛错时 AI 端口仍必须还原"
        )
        ai_ports.set_ai(None)

    async def test_cancel_during_revoke_still_restores_pause_flag_and_ai(self):
        """取消落在 revoke 的 await 上时，暂停标志与 AI 端口仍必须复位。

        revoke 是收尾段里唯一的可取消点，而 CancelledError 是 BaseException
        子类——内层 except Exception 拦不住，取消落在它上面会让紧随其后的
        两条复位一起被跳过：管道永久停在暂停态、AI 端口仍指向基准供应商。
        插件 teardown 会取消运行中的基准任务，该路径因此可达。

        用例走**真实取消机制**：让 revoke 挂起，确认 task 已停在该挂起点后
        再 cancel，而不是在桩里直接抛 CancelledError。
        """
        from briefdesk import ai_ports

        ai_sentinel = object()
        entered = asyncio.Event()

        async def blocking_revoke(code: str) -> None:
            entered.set()
            await asyncio.sleep(3600)  # 挂起点：取消将落在这里

        async def _enter_env() -> None:
            async with providers.bench_environment(register_ai=False):
                pass  # 正常走完 body，收尾段随即进入 revoke

        root = providers._TMP_ROOT
        before = set(root.glob("bench-*"))
        try:
            with (
                patch.object(
                    providers,
                    "_wait_pipelines_drained",
                    new=AsyncMock(return_value=True),
                ),
                patch("briefdesk.pipeline.set_processing_paused") as paused,
                patch.object(providers.ai_ports, "get_ai", return_value=ai_sentinel),
                patch.object(providers.ai_ports, "set_ai") as set_ai,
                patch.object(providers.announcements, "announce", new=AsyncMock()),
                patch.object(providers.announcements, "revoke", new=blocking_revoke),
            ):
                task = asyncio.create_task(_enter_env())
                await asyncio.wait_for(entered.wait(), timeout=5)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task

            assert [c.args for c in paused.call_args_list] == [(True,), (False,)], (
                "取消落在 revoke 上时暂停标志仍必须复位（否则管道一直停着）"
            )
            assert set_ai.call_args_list[-1].args == (ai_sentinel,), (
                "取消落在 revoke 上时 AI 端口仍必须还原"
            )
        finally:
            # 收尾段的运行目录清理不在保护范围内，这条路径会遗留本次目录
            # （接受项）：用例自行回收，避免反复运行堆积
            for leftover in set(root.glob("bench-*")) - before:
                shutil.rmtree(leftover, ignore_errors=True)
            ai_ports.set_ai(None)
