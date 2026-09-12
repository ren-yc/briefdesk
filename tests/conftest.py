"""briefdesk 测试套件共享配置。

存在性守卫：pytest-asyncio 是本套件的硬依赖（asyncio_mode = "auto"，
见 pyproject [tool.pytest.ini_options]）。required_plugins 已能在插件
未加载时报 "Missing required plugins"；本 import 提供第二道保险——
包未安装时收集阶段即 ImportError，不依赖 ini 解析顺序。
"""

import pytest_asyncio  # noqa: F401
