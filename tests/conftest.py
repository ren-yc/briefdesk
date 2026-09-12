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
async def temp_db(tmp_path):
    """临时文件库：真实文件路径场景（备份/恢复、WAL 行为）；用例结束关闭。"""
    db = await aiosqlite.connect(str(tmp_path / "briefdesk-test.sqlite"))
    db.row_factory = aiosqlite.Row
    await init_schema(db)
    yield db
    await db.close()


@pytest.fixture
def fake_embed_provider():
    """可配 enabled 的嵌入 Provider Mock（rag/去重用例的嵌入替身基座）。"""

    provider = Mock()
    provider.is_embedding_enabled = Mock(return_value=False)
    return provider
