"""benchmark 基准环境门闸与清理边界测试。

- bench_environment 进入时暂停生产管道（set_processing_paused(True)）、
  经 db.db_redirect 重定向主/向量连接，退出先还原连接再复位标志；
- 清理只删除本次运行的 uuid 子目录，共享 .tmp 根目录内的其它内容不受影响。

连接走真实 _init_connection 落在插件 .tmp 临时目录（运行结束即清理）；
连接创建失败的半程防护由 db.db_redirect 内部保证（见
ProviderResourceAcquisitionFailureTest）。
"""

import unittest
from unittest.mock import patch

import briefdesk.db as briefdesk_db
from briefdesk.plugins.benchmark import providers


class BenchmarkEnvPauseGateTest(unittest.IsolatedAsyncioTestCase):
    """进入基准环境暂停管道，退出先还原 DB 连接再复位标志。"""

    async def test_pause_flag_toggled_and_db_restored(self):
        old_main, old_embed = briefdesk_db._db, briefdesk_db._embed_db
        with patch("briefdesk.pipeline.set_processing_paused") as paused:
            async with providers.bench_environment(register_ai=False):
                paused.assert_called_once_with(True)
                self.assertIsNot(briefdesk_db._db, old_main)  # 已重定向
                self.assertIsNot(briefdesk_db._embed_db, old_embed)
            self.assertEqual(
                [c.args for c in paused.call_args_list], [(True,), (False,)]
            )
            self.assertIs(briefdesk_db._db, old_main)  # 已还原
            self.assertIs(briefdesk_db._embed_db, old_embed)


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
            self.assertRaises(RuntimeError),
        ):
            async with providers.bench_environment():
                pass  # 不可达：进入即失败
        # 已获取的 main_conn 被缝关闭（不残留非 daemon worker 线程）；
        # 本次子目录被清理（其余目录不动）
        self.assertEqual(len(created), 1)
        self.assertTrue(closed["v"], "半程失败的 main_conn 未被关闭")
        self.assertEqual(set(root.glob("bench-*")), before)
        # 失败路径经 finally 复位，管道标志不得残留置位
        from briefdesk import pipeline as _pipeline

        self.assertFalse(_pipeline._processing_paused)


if __name__ == "__main__":
    unittest.main()


class DrainWaitTest(unittest.IsolatedAsyncioTestCase):
    """【复核 P2-22】重定向前等待在途批次排空：pendingCount 归零即通过，
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
