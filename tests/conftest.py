"""briefdesk 测试套件共享配置。

路径隔离（为什么在 import 期做，而不是 autouse 夹具）：`briefdesk/config.py` 在
import 时构造全局 `config` 单例，其 `db_path` 默认值经 `paths.database_path()`
求值——只有**在 import 之前**把三个 `BRIEFDESK_*` 覆盖变量指向临时目录，才能保证
任何用例都不触碰真实用户数据目录。夹具跑得太晚：收集期已完成 import，那时
config 早已冻结成真实路径。会话级临时目录在解释器退出时清理。

存在性守卫：pytest-asyncio 是本套件的硬依赖（asyncio_mode = "auto"，
见 pyproject [tool.pytest.ini_options]）。required_plugins 已能在插件
未加载时报 "Missing required plugins"；本 import 提供第二道保险——
包未安装时收集阶段即 ImportError，不依赖 ini 解析顺序。
"""

import atexit
import os
import shutil
import tempfile
from pathlib import Path
from unittest.mock import Mock

_SESSION_TMP = Path(tempfile.mkdtemp(prefix="briefdesk-tests-"))
atexit.register(shutil.rmtree, _SESSION_TMP, True)

# 必须先于 briefdesk.* 的 import：config 单例与 settings_base 的 env_file 列表
# 都在 import 期求值，之后再设环境变量不会改变它们。
os.environ["BRIEFDESK_DATA_DIR"] = str(_SESSION_TMP / "data")
os.environ["BRIEFDESK_CACHE_DIR"] = str(_SESSION_TMP / "cache")
os.environ["BRIEFDESK_SETTINGS_FILE"] = str(_SESSION_TMP / "settings.env")

import aiosqlite
import pytest
import pytest_asyncio  # noqa: F401

from briefdesk import paths, settings_env
from briefdesk.config import Settings
from briefdesk.db import init_schema


@pytest.fixture(autouse=True)
def _without_project_dotenv(monkeypatch):
    """项目根 .env 不是测试输入：默认按 wheel 模式（不读隐式 .env）。

    为什么必须全局：`.env` 只在开发机存在（CI 没有），一旦它参与解析，用例就会
    随开发机内容变红或变绿——典型的「只在开发机失败」。需要验证源码模式行为的
    用例自行 patch `paths.project_dotenv_path()` 到临时文件即可覆盖本夹具。
    """
    monkeypatch.setattr(paths, "project_dotenv_path", lambda: None)
    # 核心启动快照在 import config 时就捕获了（早于任何夹具），这里按「无项目 .env」
    # 重算一次——否则用例会读到开发机真实 .env 里的来源，CI 上又变成另一套结论。
    settings_env.capture_startup_sources(Settings)


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
