"""跨测试文件共享的 pipeline 与基准脚手架（模块 _ 前缀：pytest 不收集）。

只收敛跨文件真重复的构造函数；构造上下文差异大的 fixture（各测试文件
的 _ctx 家族：ocr/stage_plugins/rag/source_plugins）保留在各自文件内。
"""

import contextlib
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from briefdesk.types import InternalMessage


def _pipeline_msg(mid, content="c", session_id="s", ts=1, is_self=False):
    return InternalMessage(
        msg_id=mid,
        content=content,
        sender_name="A",
        sender_id="u",
        session_id=session_id,
        group_name="g",
        timestamp=ts,
        source="weflow-legacy",
        is_self=is_self,
    )


def _pipeline_client(name="weflow-legacy"):
    c = Mock()
    c.name = name
    c.download_media = AsyncMock(return_value=b"x")
    return c


@contextlib.asynccontextmanager
async def _scratch_db(categories=None):
    """临时 scratch 基准库：config.db_path 指到临时文件、建表、按需换类别，退出还原。

    进程内运行时的隔离缝（providers.bench_environment：暂停管道 + 排空 + 库重定向）
    已随进程级隔离删除，测试改为自建，与 CLI 手工运行同口径（见 cli._run_isolated）：
    换连接前先把模块级单例清空，退出时**先关连接再删目录**（Windows 上文件仍被占用
    会让 TemporaryDirectory 清理报 WinError 32）。

    收敛到本模块的原因：判重测量的隔离用例（tests/test_benchmark_measurement.py）
    与基准指标用例（tests/test_benchmark.py）都要「真库 + 真 DedupEngine」跑通，
    两处各写一份清理顺序就会漂移出「某个文件漏关向量连接、把 pytest 卡在退出阶段」
    这类只在特定收集顺序下复现的问题。
    """
    from briefdesk.config import config

    old_path = config.db_path
    with tempfile.TemporaryDirectory() as d:
        import briefdesk.db as briefdesk_db
        from briefdesk.plugins.benchmark.providers import prepare_scratch

        old_main, old_embed = briefdesk_db._db, briefdesk_db._embed_db
        briefdesk_db._db = briefdesk_db._embed_db = None
        config.db_path = str(Path(d) / "bench.sqlite")
        try:
            await prepare_scratch(categories)
            yield
        finally:
            # 两条连接都要关：只用主连接（get_db）时向量连接可能已被 dedup 惰性打开，
            # 漏关会让 aiosqlite 的非 daemon 线程把 pytest 卡在退出阶段
            for conn in (briefdesk_db._db, briefdesk_db._embed_db):
                if conn is not None:
                    await conn.close()
            briefdesk_db._db = old_main
            briefdesk_db._embed_db = old_embed
            config.db_path = old_path
