"""benchmark 基准环境门闸与清理边界测试。

- bench_environment 进入时暂停生产管道（set_processing_paused(True)）、
  退出时先复位标志再还原 get_db 补丁；
- 清理只删除本次运行的 uuid 子目录，共享 .tmp 根目录内的其它内容不受影响。

连接工厂替换为内存 SQLite：不落盘、不依赖系统临时目录（受限环境下
tempfile 写入可能被拒绝，属环境限制而非代码问题）。
"""

import unittest
from unittest.mock import patch

import aiosqlite

import briefdesk.db as briefdesk_db
from briefdesk.plugins.benchmark import providers


async def _fake_new_connection(path: str) -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    return conn


class BenchmarkEnvPauseGateTest(unittest.IsolatedAsyncioTestCase):
    """进入基准环境暂停管道，退出先复位标志再还原 DB 补丁。"""

    async def test_pause_flag_toggled_and_patch_restored(self):
        old_get_db = briefdesk_db.get_db
        old_get_embed_db = briefdesk_db.get_embed_db
        with (
            patch.object(providers, "_new_connection", _fake_new_connection),
            patch("briefdesk.pipeline.set_processing_paused") as paused,
        ):
            async with providers.bench_environment(register_ai=False):
                paused.assert_called_once_with(True)
                self.assertIsNot(briefdesk_db.get_db, old_get_db)
                self.assertIsNot(briefdesk_db.get_embed_db, old_get_embed_db)
            self.assertEqual(
                [c.args for c in paused.call_args_list], [(True,), (False,)]
            )
            self.assertIs(briefdesk_db.get_db, old_get_db)
            self.assertIs(briefdesk_db.get_embed_db, old_get_embed_db)


class BenchmarkEnvCleanupScopeTest(unittest.IsolatedAsyncioTestCase):
    """清理只删本次运行子目录；根目录内既有内容必须保留。"""

    async def test_cleanup_keeps_root_sentinel(self):
        providers._TMP_ROOT.mkdir(parents=True, exist_ok=True)
        sentinel = providers._TMP_ROOT / "sentinel-keep.txt"
        sentinel.write_text("keep", encoding="utf-8")
        # 相对断言：忽略其它测试/进程遗留的 bench-*，只看本次运行的增减
        pre_existing = set(providers._TMP_ROOT.glob("bench-*"))
        try:
            with (
                patch.object(providers, "_new_connection", _fake_new_connection),
                # 本测试只关注清理边界：门闸函数打桩（管道侧实现已合并）
                patch("briefdesk.pipeline.set_processing_paused"),
            ):
                async with providers.bench_environment(register_ai=False):
                    current = set(providers._TMP_ROOT.glob("bench-*")) - pre_existing
                    self.assertEqual(len(current), 1)
                self.assertTrue(sentinel.exists(), ".tmp 根目录被整体删除")
                leftovers = set(providers._TMP_ROOT.glob("bench-*")) - pre_existing
                self.assertEqual(leftovers, set(), "本次运行的子目录未被清理")
        finally:
            if sentinel.exists():
                sentinel.unlink()




class ProviderResourceAcquisitionFailureTest(unittest.IsolatedAsyncioTestCase):
    """连接创建失败也必须回收已获取资源与本次子目录（审查 A3）。

    旧实现把 _new_connection/pragma 放在 try 之外：第二个连接失败会泄漏
    第一个连接并残留 run_dir。现资源获取纳入 try，finally 判空防护。"""

    async def test_second_connection_failure_cleans_up(self):
        class FakeConn:
            def __init__(self):
                self.closed = False

            async def close(self):
                self.closed = True

        fake = FakeConn()
        calls = {"n": 0}

        async def flaky_new_connection(_path):
            calls["n"] += 1
            if calls["n"] >= 2:
                raise RuntimeError("disk full")
            return fake

        root = providers._TMP_ROOT
        before = set(root.glob("bench-*"))
        with (
            patch.object(providers, "_new_connection", new=flaky_new_connection),
            self.assertRaises(RuntimeError),
        ):
            async with providers.bench_environment():
                pass  # 不可达：进入即失败
        # 已获取的 main_conn 被关闭；本次子目录被清理（其余目录不动）
        self.assertTrue(fake.closed)
        self.assertEqual(set(root.glob("bench-*")), before)
        # 失败发生在暂停置位之前，管道标志不得被置位
        from briefdesk import pipeline as _pipeline

        self.assertFalse(_pipeline._processing_paused)


if __name__ == "__main__":
    unittest.main()


class DrainWaitTest(unittest.IsolatedAsyncioTestCase):
    """【复核 P2-22】补丁前等待在途批次排空：pendingCount 归零即通过，
    超时返回 False（调用方告警后放弃等待，行为可观测）。"""

    async def test_returns_true_when_drained(self):
        with patch.object(
            providers, "get_sync_progress", return_value={"pendingCount": 0}
        ):
            self.assertTrue(await providers._wait_pipelines_drained(timeout_s=0.1))

    async def test_returns_false_on_timeout(self):
        with patch.object(
            providers, "get_sync_progress", return_value={"pendingCount": 3}
        ):
            self.assertFalse(await providers._wait_pipelines_drained(timeout_s=0.1))
