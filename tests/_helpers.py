"""跨测试文件共享的 pipeline 脚手架（模块 _ 前缀：pytest 不收集）。

只收敛跨文件真重复的构造函数；构造上下文差异大的 fixture（各测试文件
的 _ctx 家族：ocr/stage_plugins/rag/source_plugins）保留在各自文件内。
"""

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
