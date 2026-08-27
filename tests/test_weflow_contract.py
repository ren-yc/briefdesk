"""weflow 就绪链路测试（健康检查驱动的账号引导注册）。

覆盖 weflow-server v0.3.0 的契约：/health 下发每账号状态列表、
POST /api/v1/accounts 注册幂等且返回真实 state/status。断言下游据此
避免重复注册、被拒态不记忆化（下轮自愈）、业务 503 复位记忆化标志，
以及会话类型判定的数字 type 兜底。

全部使用虚构 wxid / token / 密钥，不含任何真实凭据或聊天内容。
"""

import unittest
from unittest.mock import AsyncMock, patch

import httpx

from briefdesk.plugins.weflow.client import WeFlowClient, WeFlowNotReadyError
from briefdesk.plugins.weflow.config import WeFlowSettings
from briefdesk.plugins.weflow.poller import _session_kind


def _client() -> WeFlowClient:
    """构造测试用客户端（虚构参数，不发起真实请求）。"""
    return WeFlowClient(
        base_url="http://127.0.0.1:5033",
        api_token="test-token",
        wxid="wxid_test_0001",
        db_path="",
        db_keys={"session/session.db": "00" * 32},
        sse_read_timeout_ms=1000,
    )


def _health(*states: str, version: str = "0.3.0") -> dict:
    """构造 /health 响应（每账号一个 state）。"""
    return {
        "status": "ok" if states and all(s == "ready" for s in states) else "starting",
        "version": version,
        "accounts": [
            {"wxid": f"wxid_test_{i:04d}", "state": s, "message_count": 100}
            for i, s in enumerate(states, start=1)
        ],
    }


class EnsureReadyTest(unittest.IsolatedAsyncioTestCase):
    """ensure_ready 的健康检查短路与注册决策。"""

    async def test_ready_account_skips_registration(self):
        """已有 ready 账号：不注册，且记忆化（后续调用不再查 health）。"""
        client = _client()
        with (
            patch.object(
                client, "fetch_health", AsyncMock(return_value=_health("ready"))
            ) as health,
            patch.object(client, "register_account", AsyncMock()) as register,
        ):
            await client.ensure_ready()
            await client.ensure_ready()  # 记忆化：不应再查 health
        register.assert_not_called()
        health.assert_awaited_once()

    async def test_indexing_account_skips_registration(self):
        """账号建索引中（indexing）：良性态，不重复注册（防注册风暴）。"""
        client = _client()
        with (
            patch.object(
                client, "fetch_health", AsyncMock(return_value=_health("indexing"))
            ),
            patch.object(client, "register_account", AsyncMock()) as register,
        ):
            await client.ensure_ready()
        register.assert_not_called()

    async def test_zero_accounts_registers_once(self):
        """零账号：注册一次；accepted 为良性态并记忆化。"""
        client = _client()
        with (
            patch.object(client, "fetch_health", AsyncMock(return_value=_health())),
            patch.object(
                client,
                "register_account",
                AsyncMock(return_value=("accepted", "indexing")),
            ) as register,
        ):
            await client.ensure_ready()
            await client.ensure_ready()  # 记忆化：不应再注册
        register.assert_awaited_once()

    async def test_already_ready_state_is_benign(self):
        """幂等注册返回 already_ready/ready：记忆化，不视为失败。"""
        client = _client()
        with (
            patch.object(client, "fetch_health", AsyncMock(return_value=_health())),
            patch.object(
                client,
                "register_account",
                AsyncMock(return_value=("already_ready", "ready")),
            ),
        ):
            await client.ensure_ready()
        self.assertTrue(client._ready_checked)

    async def test_error_account_registers_and_rejection_not_memoized(self):
        """error 态账号：重新注册（上游允许替换重建）；被拒态不记忆化。"""
        client = _client()
        with (
            patch.object(
                client, "fetch_health", AsyncMock(return_value=_health("error"))
            ),
            patch.object(
                client,
                "register_account",
                AsyncMock(return_value=("unknown", "error")),
            ) as register,
        ):
            await client.ensure_ready()
            self.assertFalse(client._ready_checked)
            await client.ensure_ready()  # 未记忆化 → 下轮重试
        self.assertEqual(register.await_count, 2)

    async def test_awaiting_key_account_triggers_registration(self):
        """awaiting_key 态：非良性，应尝试注册（密钥补齐后自愈）。"""
        client = _client()
        with (
            patch.object(
                client, "fetch_health", AsyncMock(return_value=_health("awaiting_key"))
            ),
            patch.object(
                client,
                "register_account",
                AsyncMock(return_value=("accepted", "indexing")),
            ) as register,
        ):
            await client.ensure_ready()
        register.assert_awaited_once()

    async def test_health_failure_not_memoized(self):
        """健康检查失败（服务未起）：上抛且不记忆化，下轮重试。"""
        client = _client()
        with (
            patch.object(
                client, "fetch_health", AsyncMock(side_effect=RuntimeError("boom"))
            ),
            patch.object(client, "register_account", AsyncMock()) as register,
            self.assertRaises(RuntimeError),
        ):
            await client.ensure_ready()
        self.assertFalse(client._ready_checked)
        register.assert_not_called()

    async def test_force_rechecks_health(self):
        """force=True：忽略记忆化标志重查 health（自愈服务端重启）。"""
        client = _client()
        with (
            patch.object(
                client, "fetch_health", AsyncMock(return_value=_health("ready"))
            ) as health,
            patch.object(client, "register_account", AsyncMock()),
        ):
            await client.ensure_ready()
            await client.ensure_ready(force=True)
        self.assertEqual(health.await_count, 2)

    async def test_force_after_restart_registers_again(self):
        """服务端重启（注册表清空）：force 重检发现零账号 → 重新注册。"""
        client = _client()
        with (
            patch.object(
                client,
                "fetch_health",
                AsyncMock(side_effect=[_health("ready"), _health()]),
            ),
            patch.object(
                client,
                "register_account",
                AsyncMock(return_value=("accepted", "indexing")),
            ) as register,
        ):
            await client.ensure_ready()
            register.assert_not_called()
            await client.ensure_ready(force=True)
        register.assert_awaited_once()


class ReadyGateTest(unittest.IsolatedAsyncioTestCase):
    """业务接口 503 就绪门控的瞬态语义与记忆化复位。"""

    async def test_503_resets_memoized_flag(self):
        """503 → WeFlowNotReadyError 且复位 _ready_checked（服务端重启自愈）。"""
        client = _client()
        client._ready_checked = True

        class _Resp:
            status_code = 503
            is_success = False
            text = '{"success":false,"code":503,"message":"account not ready"}'
            url = httpx.URL("http://127.0.0.1:5033/api/v1/sessions")

            def json(self) -> dict:
                return {}

        with (
            patch(
                "briefdesk.plugins.weflow.client.with_connect_retry",
                AsyncMock(return_value=_Resp()),
            ),
            self.assertRaises(WeFlowNotReadyError),
        ):
            await client._get("/api/v1/sessions")
        self.assertFalse(client._ready_checked)

    async def test_404_with_not_found_ok_returns_none(self):
        """会话不存在（brandsessionholder 等）404 → None，由调用方降级。

        上游 sessions 端点仍会列出无消息表的聚合会话，messages 查询对其
        返回 404（见 bug.md 问题 2，未修），故该兜底必须保留。
        """
        client = _client()

        class _Resp:
            status_code = 404
            is_success = False
            text = '{"success":false,"code":404,"message":"conversation not found"}'
            url = httpx.URL("http://127.0.0.1:5033/api/v1/messages")

            def json(self) -> dict:
                return {}

        with patch(
            "briefdesk.plugins.weflow.client.with_connect_retry",
            AsyncMock(return_value=_Resp()),
        ):
            resp = await client.fetch_messages(
                "brandsessionholder", None, not_found_ok=True
            )
        self.assertEqual(resp["messages"], [])
        self.assertFalse(resp["hasMore"])


class ListLimitTest(unittest.IsolatedAsyncioTestCase):
    """列表端点必须显式传大 limit：上游默认 100 会静默截断。"""

    async def test_contacts_and_sessions_request_large_limit(self):
        """/contacts 与 /sessions 都要带 limit=10000。

        上游两个端点的默认 limit 均为 100（上限 10000）：contacts 不传会把
        通讯录截断到 100 条，截断外的发送者显示名回退成 wxid；sessions 不传
        只会发现最近活跃的 100 个会话。
        """
        client = _client()
        with patch.object(
            client, "_get", AsyncMock(return_value={"contacts": [], "sessions": []})
        ) as get:
            await client.fetch_contacts()
            await client.fetch_sessions()
        paths = [call.args[0] for call in get.await_args_list]
        limits = [call.kwargs["params"].get("limit") for call in get.await_args_list]
        self.assertEqual(paths, ["/api/v1/contacts", "/api/v1/sessions"])
        self.assertEqual(limits, [10000, 10000])


class ControlEventStatsTest(unittest.IsolatedAsyncioTestCase):
    """控制事件（ready / sync）不进管道、也不计入监听统计。"""

    async def test_control_events_skipped_without_inflating_stats(self):
        """上游无就绪门控后每次重连都会带 ready（+可能 sync）基线帧。

        它们不是消息：既不该进管道，也不该计入「事件」或「预过滤丢弃」——
        否则「无消息静默」统计失效（与 qqflow 监听器一致）。
        ready 帧的载荷实测为 {"status":"ok"}，不含 event 键。
        """
        from briefdesk.plugins.weflow.sse import WeFlowSseClient

        batches: list[list] = []

        async def on_batch(msgs: list) -> None:
            batches.append(msgs)

        client = _client()
        listener = WeFlowSseClient(
            client, on_batch, settings=WeFlowSettings(sse_read_timeout_ms=1000)
        )
        await listener._handle_event({"status": "ok"})  # type: ignore[arg-type]
        await listener._handle_event({"event": "sync", "watermarks": []})  # type: ignore[arg-type]
        await listener._handle_event(  # type: ignore[arg-type]
            {"event": "ready", "status": "ok"}
        )
        self.assertEqual(listener._stats_events, 0)
        self.assertEqual(listener._stats_filtered, 0)
        self.assertEqual(batches, [])

        # 真实消息仍计入事件统计（证明跳过逻辑没有误伤 message.new）
        await listener._handle_event(  # type: ignore[arg-type]
            {
                "event": "message.new",
                "sessionId": "test@chatroom",
                "sessionType": "group",
                "rawid": "1001",
                "sourceName": "甲",
                "groupName": "测试群",
                "content": "这是一条足够长的测试消息内容",
                "timestamp": 1700000000,
            }
        )
        self.assertEqual(listener._stats_events, 1)


class SessionKindTest(unittest.TestCase):
    """会话类型判定：sessionType 权威 + 数字 type 兜底（枚举序）。"""

    def test_session_type_authoritative(self):
        self.assertEqual(_session_kind({"sessionType": "group"}), (True, False))
        self.assertEqual(_session_kind({"sessionType": "official"}), (False, True))
        self.assertEqual(_session_kind({"sessionType": "private"}), (False, False))
        self.assertEqual(_session_kind({"sessionType": "other"}), (False, False))

    def test_numeric_type_fallback_matches_enum_order(self):
        """sessionType 缺失时按 SessionKind 枚举序兜底：
        private=0 / group=1 / official=2 / other=3。"""
        self.assertEqual(_session_kind({"type": 0}), (False, False))
        self.assertEqual(_session_kind({"type": 1}), (True, False))
        self.assertEqual(_session_kind({"type": 2}), (False, True))
        self.assertEqual(_session_kind({"type": 3}), (False, False))
        self.assertEqual(_session_kind({}), (False, False))


if __name__ == "__main__":
    unittest.main()
