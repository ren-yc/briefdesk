"""briefdesk/ai_egress.py 的单元测试：本地判定矩阵与发送边界计数。

不接 SDK、不发网络请求：这里验收的是「判定纯函数」与「计数助手的指标形状」，
真实发送边界由引擎侧用例覆盖。样本域名一律 `example.invalid`。
"""

from __future__ import annotations

import asyncio

import pytest

from briefdesk import ai_egress, telemetry
from briefdesk.config import config

REMOTE_BASE = "https://ai.example.invalid/v1"
LOCAL_BASE = "http://localhost:11434/v1"


@pytest.fixture(autouse=True)
def _telemetry_isolation(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """遥测隔离且固定开启：本文件的断言都基于「确实记了」。"""
    telemetry.reset_for_tests()
    monkeypatch.setattr(telemetry, "telemetry_dir", lambda: tmp_path / "tel")
    monkeypatch.setattr(config, "telemetry_enabled", True)
    yield
    telemetry.reset_for_tests()


def _counters() -> dict[str, int]:
    return telemetry.snapshot()["counters"]


LOCAL_BASES = [
    "http://localhost",
    "http://localhost/",
    "http://localhost:11434",
    "http://localhost:11434/v1",
    "https://LOCALHOST:8080/v1",
    "http://127.0.0.1",
    "http://127.0.0.1:8000/v1",
    "http://127.255.255.254",
    "http://[::1]",
    "http://[::1]:8080/v1",
    "http://0.0.0.0:3000",
    "  http://localhost:11434/v1  ",
]

REMOTE_BASES = [
    "",
    "   ",
    "localhost:11434",  # 没有 scheme
    "ftp://localhost",
    "http://localhost@remote.example.invalid",  # 主机藏在凭据段
    "http://user:pass@localhost",  # 带凭据段：不猜它想连哪里
    "http://@127.0.0.1",  # 空 userinfo 同样可疑（username 是空串而不是 None）
    "http://local\nhost",  # 控制字符被解析器删掉后才是 localhost
    "http://localhost\r/v1",  # 同上：CR 会让解析结果与字面不一致
    "http://localhost.example.invalid",  # 近似域名
    "http://localhost.",  # 带点写法不猜，按远端
    "http://10.0.0.5",
    "http://172.16.0.1:8080",
    "http://192.168.1.1",
    "http://[::]",  # 未指定地址只有 IPv4 的 0.0.0.0 才算本地
    "http://localhost:99999",  # 端口越界
    "http://localhost:abc",  # 端口非数字
    "http://:8080",
    "http://",
    "not a url",
    "http://[::1",  # 畸形 IPv6
    REMOTE_BASE,
]


@pytest.mark.parametrize("base", LOCAL_BASES)
def test_local_endpoint_matrix_fail_closed(base: str) -> None:
    assert ai_egress.is_local_endpoint(base) is True


@pytest.mark.parametrize("base", REMOTE_BASES)
def test_remote_endpoint_matrix_fail_closed(base: str) -> None:
    # 认不出的写法一律按远端：判错方向时最多多假名化一次
    assert ai_egress.is_local_endpoint(base) is False


def test_remote_and_local_attempts_are_separate() -> None:
    assert ai_egress.record_send_attempt(ai_egress.PORT_CHAT, ai_egress.ROLE_MAIN, REMOTE_BASE) is False
    assert ai_egress.record_send_attempt(ai_egress.PORT_CHAT, ai_egress.ROLE_MAIN, LOCAL_BASE) is True
    assert _counters() == {
        "ai.egress.remote_attempt.chat.main|": 1,
        "ai.local.attempt.chat.main|": 1,
    }


@pytest.mark.parametrize(
    "port",
    [ai_egress.PORT_CHAT, ai_egress.PORT_RAG, ai_egress.PORT_EMBED],
)
def test_each_port_has_its_own_series(port: str) -> None:
    for _ in range(2):
        ai_egress.record_send_attempt(port, ai_egress.ROLE_MAIN, REMOTE_BASE)
    counters = _counters()
    assert counters == {f"ai.egress.remote_attempt.{port}.main|": 2}


def test_queue_failures_never_count_remote_attempt() -> None:
    ai_egress.record_queue_timeout(ai_egress.PORT_RAG, ai_egress.ROLE_JUDGE)
    ai_egress.record_queue_cancelled(ai_egress.PORT_CHAT, ai_egress.ROLE_GEN)
    counters = _counters()
    assert counters == {
        "ai.queue.timeout.rag.judge|": 1,
        "ai.queue.cancelled.chat.gen|": 1,
    }
    assert not any(".attempt." in key for key in counters)


def test_sdk_retry_separate_from_logical_attempt() -> None:
    is_local = ai_egress.record_send_attempt(
        ai_egress.PORT_CHAT, ai_egress.ROLE_MAIN, REMOTE_BASE
    )
    assert is_local is False
    ai_egress.record_sdk_retry(ai_egress.PORT_CHAT, ai_egress.ROLE_MAIN, is_local)
    counters = _counters()
    assert counters["ai.egress.remote_attempt.chat.main|"] == 1
    assert counters["ai.egress.sdk_retry.chat.main|"] == 1

    # 本地端点同一套分桶，但不写远端键
    local_flag = ai_egress.record_send_attempt(
        ai_egress.PORT_EMBED, ai_egress.ROLE_MAIN, LOCAL_BASE
    )
    assert local_flag is True
    ai_egress.record_sdk_retry(ai_egress.PORT_EMBED, ai_egress.ROLE_MAIN, local_flag)
    counters = _counters()
    assert counters["ai.local.attempt.embed.main|"] == 1
    assert counters["ai.local.sdk_retry.embed.main|"] == 1
    assert "ai.egress.sdk_retry.embed.main|" not in counters


async def test_contextvar_metadata_is_isolated_between_tasks() -> None:
    async def _worker(source: str) -> None:
        with ai_egress.use_metadata(
            port=ai_egress.PORT_RAG,
            role=ai_egress.ROLE_JUDGE,
            kind="judge",
            source=source,
        ):
            # 让出控制权后再读：并发任务之间不能串味
            await asyncio.sleep(0)
            assert ai_egress.current_metadata().source == source
            ai_egress.record_send_attempt(
                ai_egress.PORT_RAG, ai_egress.ROLE_JUDGE, REMOTE_BASE
            )

    await asyncio.gather(_worker("weflow"), _worker("qqflow"))
    counters = _counters()
    assert counters == {
        "ai.egress.remote_attempt.rag.judge|weflow": 1,
        "ai.egress.remote_attempt.rag.judge|qqflow": 1,
    }
    # with 退出后回到默认元数据（reset 而不是重设，嵌套时才能回到外层值）
    assert ai_egress.current_metadata() == ai_egress.EgressMetadata()

    with ai_egress.use_metadata(
        port=ai_egress.PORT_CHAT, role=ai_egress.ROLE_MAIN, source="outer"
    ):
        with ai_egress.use_metadata(
            port=ai_egress.PORT_EMBED, role=ai_egress.ROLE_MAIN, source="inner"
        ):
            assert ai_egress.current_metadata().source == "inner"
            assert ai_egress.current_metadata().port == ai_egress.PORT_EMBED
        assert ai_egress.current_metadata().source == "outer"
        assert ai_egress.current_metadata().port == ai_egress.PORT_CHAT
    assert ai_egress.current_metadata() == ai_egress.EgressMetadata()


def test_source_label_stays_out_of_metric_name() -> None:
    with ai_egress.use_metadata(
        port=ai_egress.PORT_CHAT,
        role=ai_egress.ROLE_CLASSIFY,
        kind="sysb",
        source="weflow",
    ):
        ai_egress.record_send_attempt(
            ai_egress.PORT_CHAT, ai_egress.ROLE_CLASSIFY, REMOTE_BASE
        )
    # 指标名只由固定词表拼成；源标签只进遥测维度
    assert _counters() == {"ai.egress.remote_attempt.chat.classify|weflow": 1}


def test_metadata_labels_converge_to_controlled_vocabulary() -> None:
    """陌生人名与 ID 不能原样进遥测键：形态不符整值归到默认桶。"""
    with ai_egress.use_metadata(
        port=ai_egress.PORT_CHAT,
        role=ai_egress.ROLE_MAIN,
        kind="张三",
        source="u_12345",
    ):
        metadata = ai_egress.current_metadata()
        assert metadata.kind == "other"
        assert metadata.source == ""
        ai_egress.record_send_attempt(
            ai_egress.PORT_CHAT, ai_egress.ROLE_MAIN, REMOTE_BASE
        )

    counters = _counters()
    assert counters == {"ai.egress.remote_attempt.chat.main|": 1}
    for key in counters:
        assert "张三" not in key
        assert "u_12345" not in key


def test_metadata_labels_keep_well_formed_values() -> None:
    with ai_egress.use_metadata(
        port=ai_egress.PORT_CHAT,
        role=ai_egress.ROLE_CLASSIFY,
        kind="judge",
        source="weflow",
    ):
        metadata = ai_egress.current_metadata()
        assert metadata.kind == "judge"
        assert metadata.source == "weflow"

    # 空标签是默认值，不算「形态不符」：不能被改写成 other
    with ai_egress.use_metadata(port=ai_egress.PORT_CHAT, role=ai_egress.ROLE_MAIN):
        assert ai_egress.current_metadata().kind == ""
        assert ai_egress.current_metadata().source == ""

    # 超长标签同样按形态不符处理（受控词不含 64 字符以上的值）
    with ai_egress.use_metadata(
        port=ai_egress.PORT_CHAT,
        role=ai_egress.ROLE_MAIN,
        kind="k" * 65,
        source="s" * 65,
    ):
        assert ai_egress.current_metadata().kind == "other"
        assert ai_egress.current_metadata().source == ""


def test_shape_filter_does_not_catch_id_shaped_labels() -> None:
    """形态过滤不是受控词表：全小写、带连字符的 `member-0001` 会原样通过。

    这是已知局限：`_sanitize_label` 只看形态，认不出「长得像 ID」的取值。接线
    的调用方必须从插件名或固定任务标签取维度，不能把业务 ID 直接传进来。钉住
    这条行为是为了让「以为已经挡住身份信息」的改动先在这里失败，而不是留给人
    凭印象判断。
    """
    with ai_egress.use_metadata(
        port=ai_egress.PORT_CHAT,
        role=ai_egress.ROLE_MAIN,
        kind="member-0001",
        source="member-0002",
    ):
        metadata = ai_egress.current_metadata()
        assert metadata.kind == "member-0001"
        assert metadata.source == "member-0002"


def test_unknown_port_or_role_is_rejected() -> None:
    with pytest.raises(ValueError, match="chat"):
        ai_egress.record_send_attempt("chatting", ai_egress.ROLE_MAIN, REMOTE_BASE)
    with pytest.raises(ValueError, match="main"):
        ai_egress.record_queue_timeout(ai_egress.PORT_CHAT, "primary")
    with pytest.raises(ValueError, match="classify"):
        ai_egress.record_sdk_retry(ai_egress.PORT_EMBED, "角色不明", False)
    with pytest.raises(ValueError), ai_egress.use_metadata(
        port="nope", role=ai_egress.ROLE_MAIN
    ):
        pass
    with pytest.raises(ValueError), ai_egress.use_metadata(
        port=ai_egress.PORT_CHAT, role="nope"
    ):
        pass


def test_blocked_metric_is_reserved_only() -> None:
    assert (
        ai_egress.BLOCKED_METRIC_TEMPLATE.format(
            port=ai_egress.PORT_CHAT, role=ai_egress.ROLE_MAIN
        )
        == "ai.egress.blocked.chat.main"
    )
    # 本模块没有阻断路径：所有助手跑一遍也不能产出 blocked 非零
    ai_egress.record_send_attempt(ai_egress.PORT_CHAT, ai_egress.ROLE_MAIN, REMOTE_BASE)
    ai_egress.record_queue_timeout(ai_egress.PORT_CHAT, ai_egress.ROLE_MAIN)
    ai_egress.record_queue_cancelled(ai_egress.PORT_CHAT, ai_egress.ROLE_MAIN)
    ai_egress.record_sdk_retry(ai_egress.PORT_CHAT, ai_egress.ROLE_MAIN, False)
    assert not any("blocked" in key for key in _counters())


def test_disabled_telemetry_is_unknown_not_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    ai_egress.record_send_attempt(ai_egress.PORT_CHAT, ai_egress.ROLE_MAIN, REMOTE_BASE)
    monkeypatch.setattr(config, "telemetry_enabled", False)
    ai_egress.record_send_attempt(ai_egress.PORT_CHAT, ai_egress.ROLE_MAIN, REMOTE_BASE)
    ai_egress.record_send_attempt(ai_egress.PORT_EMBED, ai_egress.ROLE_MAIN, LOCAL_BASE)
    ai_egress.record_queue_cancelled(ai_egress.PORT_CHAT, ai_egress.ROLE_MAIN)
    # 关闭期间既不累加也不落零值：读数只能表达「未知」，不能伪装成零出境
    assert _counters() == {"ai.egress.remote_attempt.chat.main|": 1}
    assert all(value > 0 for value in _counters().values())
