"""briefdesk 测试套件共享配置。

存在性守卫：pytest-asyncio 是本套件的硬依赖（asyncio_mode = "auto"，
见 pyproject [tool.pytest.ini_options]）。required_plugins 已能在插件
未加载时报 "Missing required plugins"；本 import 提供第二道保险——
包未安装时收集阶段即 ImportError，不依赖 ini 解析顺序。
"""

from unittest.mock import Mock

import aiosqlite
import pytest
import pytest_asyncio  # noqa: F401

from briefdesk.db import init_schema


@pytest.fixture
async def memory_db():
    """内存库：:memory: aiosqlite + Row 工厂 + 全量 schema；用例结束关闭。

    供 db/管道相关用例注入 get_db/get_embed_db 打桩（参考
    tests/test_db.py 的 _InMemoryDbTest 基座）。
    """
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await init_schema(db)
    yield db
    await db.close()


@pytest.fixture
def temp_db(tmp_path):
    """临时**库路径**（真实文件场景：走 get_db/get_embed_db 或备份/恢复）。

    返回路径而非连接——需要真实文件库的用例（关闭门闩、备份/恢复、WAL）
    都要求自己按各自口径建连接（部分还需同一文件的两条连接）；生命周期
    由 pytest 的 tmp_path 托管，无需用例关闭连接。
    """
    return str(tmp_path / "briefdesk-test.sqlite")


@pytest.fixture
def fake_embed_provider():
    """可配 enabled 的嵌入 Provider Mock **工厂**（对应 test_rag_plugin 的
    _embed_provider 样板）：调用 fake_embed_provider(True/False) 取实例。"""

    def _make(enabled: bool = False):
        provider = Mock()
        provider.is_embedding_enabled = Mock(return_value=enabled)
        return provider

    return _make
