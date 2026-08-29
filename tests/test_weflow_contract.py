"""weflow 就绪链路测试（健康检查驱动的账号引导注册）。

覆盖 weflow-server v0.5.0 的契约：/health 只下发标量 account 阶段
（unregistered / indexing / ready / error，刻意不列账号——该接口免鉴权）、
POST /api/v1/accounts 强制单账号且注册幂等、账号明细改由需鉴权的
GET /api/v1/accounts 提供。断言下游据此避免重复注册、被拒态与占用冲突
不记忆化（下轮自愈）、error 阶段仍能从明细接口打出根因、业务 503 复位
记忆化标志，以及会话类型判定的数字 type 兜底。

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


def _health(phase: str = "unregistered", version: str = "0.5.0") -> dict:
    """构造 /health 响应（标量 account 阶段，不含账号清单）。"""
    return {
        "status": "ok" if phase == "ready" else "starting",
        "version": version,
        "account": phase,
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
        """error 阶段：重新注册（error 不释放绑定但可原地重试）；被拒态不记忆化。"""
        client = _client()
        with (
            patch.object(
                client, "fetch_health", AsyncMock(return_value=_health("error"))
            ),
            patch.object(client, "fetch_accounts", AsyncMock(return_value=[])),
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

    async def test_error_phase_logs_root_cause_from_detail_endpoint(self):
        """error 阶段：/health 只给标量，根因须从需鉴权的明细接口捞出并告警。

        回归防护：标量化 /health 之前，根因来自 accounts[].error；若只是删掉
        那段告警，用户就只能看到业务接口持续 503，看不到「密钥错误」。
        """
        client = _client()
        detail = [
            {
                "wxid": "wxid_test_0001",
                "state": "error",
                "error": "页 1 HMAC 校验失败",
            }
        ]
        with (
            patch.object(
                client, "fetch_health", AsyncMock(return_value=_health("error"))
            ),
            patch.object(
                client, "fetch_accounts", AsyncMock(return_value=detail)
            ) as accounts,
            patch.object(
                client,
                "register_account",
                AsyncMock(return_value=("accepted", "indexing")),
            ) as register,
            self.assertLogs("briefdesk.plugins.weflow.client", "WARNING") as logs,
        ):
            await client.ensure_ready()
        accounts.assert_awaited_once()
        register.assert_awaited_once()  # 诊断不得挡住注册重试
        self.assertTrue(any("页 1 HMAC 校验失败" in m for m in logs.output))

    async def test_detail_endpoint_failure_does_not_block_registration(self):
        """明细接口不可用（鉴权/网络）：仅降级，注册照常发起。"""
        client = _client()
        with (
            patch.object(
                client, "fetch_health", AsyncMock(return_value=_health("error"))
            ),
            patch.object(
                client, "fetch_accounts", AsyncMock(side_effect=RuntimeError("401"))
            ),
            patch.object(
                client,
                "register_account",
                AsyncMock(return_value=("accepted", "indexing")),
            ) as register,
        ):
            await client.ensure_ready()
        register.assert_awaited_once()
        self.assertTrue(client._ready_checked)

    async def test_account_conflict_is_not_memoized(self):
        """服务端已绑定另一个 wxid：不记忆化（人工注销/改配置后自愈），按 ERROR 报。"""
        client = _client()
        with (
            patch.object(client, "fetch_health", AsyncMock(return_value=_health())),
            patch.object(
                client,
                "register_account",
                AsyncMock(return_value=("account_conflict", "unknown")),
            ) as register,
            self.assertLogs("briefdesk.plugins.weflow.client", "ERROR") as logs,
        ):
            await client.ensure_ready()
            self.assertFalse(client._ready_checked)
            await client.ensure_ready()  # 未记忆化 → 下轮重试
        self.assertEqual(register.await_count, 2)
        self.assertTrue(any("已绑定另一个账号" in m for m in logs.output))

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


class AccountDetailTest(unittest.IsolatedAsyncioTestCase):
    """GET /api/v1/accounts —— 需鉴权的账号明细（/health 标量化后的去处）。"""

    async def test_fetch_accounts_sends_token_and_unwraps_list(self):
        """走 /api/v1/accounts、带鉴权头、返回 accounts 数组本身。"""
        client = _client()
        captured: dict = {}

        class _Resp:
            status_code = 200
            is_success = True
            text = ""
            url = httpx.URL("http://127.0.0.1:5033/api/v1/accounts")

            def json(self) -> dict:
                return {
                    "success": True,
                    "accounts": [
                        {
                            "wxid": "wxid_test_0001",
                            "state": "ready",
                            "message_count": 42,
                            "db_storage": "C:\\x\\db_storage",
                        }
                    ],
                }

        real_client = client._get_client()

        async def fake_get(path, **kwargs):
            captured["path"] = path
            captured["headers"] = kwargs.get("headers")
            return _Resp()

        with patch.object(real_client, "get", fake_get):
            accounts = await client.fetch_accounts()
        self.assertEqual(captured["path"], "/api/v1/accounts")
        self.assertIn("Authorization", captured["headers"])
        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0]["message_count"], 42)

    async def test_fetch_accounts_raises_on_http_error(self):
        """非 2xx（如 401）上抛 RuntimeError，由调用方决定是否降级。"""
        client = _client()

        class _Resp:
            status_code = 401
            is_success = False
            text = '{"success":false,"code":401,"message":"unauthorized"}'
            url = httpx.URL("http://127.0.0.1:5033/api/v1/accounts")

            def json(self) -> dict:
                return {}

        with (
            patch(
                "briefdesk.plugins.weflow.client.with_connect_retry",
                AsyncMock(return_value=_Resp()),
            ),
            self.assertRaises(RuntimeError),
        ):
            await client.fetch_accounts()


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


class ListPaginationTest(unittest.IsolatedAsyncioTestCase):
    """列表端点分页：contacts 与 sessions 均按 offset 翻页取全量。"""

    async def test_contacts_paginate_until_exhausted(self):
        """contacts 按 offset 翻页直到 hasMore=false，合并所有页。

        上游 limit 默认 100，不翻页只能拿到前 100 条且无错误提示——截断外的
        发送者显示名会退化成 wxid。
        """
        client = _client()
        page1 = {
            "contacts": [
                {"username": f"wxid_{i:04d}", "displayName": f"联系人{i}"}
                for i in range(1000)
            ],
            "total": 1500,
            "hasMore": True,
        }
        page2 = {
            "contacts": [
                {"username": f"wxid_{i:04d}", "displayName": f"联系人{i}"}
                for i in range(1000, 1500)
            ],
            "total": 1500,
            "hasMore": False,
        }
        with patch.object(
            client, "_get", AsyncMock(side_effect=[page1, page2])
        ) as get:
            contacts = await client.fetch_contacts()
        self.assertEqual(len(contacts), 1500)
        offsets = [c.kwargs["params"]["offset"] for c in get.await_args_list]
        self.assertEqual(offsets, [0, 1000])
        self.assertEqual(contacts["wxid_1499"], "联系人1499")

    async def test_sessions_first_page_uses_max_page_size(self):
        """sessions 按 offset 翻页，page_size=10000 = 上游 limit 硬上限。

        上游 sessions 实际信封不含 hasMore：短页（无 hasMore 且条数 <
        page_size）即末页终止——典型规模一个请求取尽。
        """
        client = _client()
        page = {
            "sessions": [
                {"username": f"wxid_{i:04d}", "displayName": f"会话{i}"}
                for i in range(3)
            ]
        }
        with patch.object(
            client, "_get", AsyncMock(return_value=page)
        ) as get:
            sessions = await client.fetch_sessions()
        self.assertEqual(len(sessions), 3)
        self.assertEqual(get.await_count, 1)
        self.assertEqual(get.await_args.args[0], "/api/v1/sessions")
        self.assertEqual(get.await_args.kwargs["params"], {"limit": 10000, "offset": 0})

    async def test_sessions_paginate_until_exhausted(self):
        """sessions 按 offset 翻页取尽：多页合并、username 去重、hasMore=false 终止。

        实际 sessions 信封虽不含 hasMore，fetch_all_pages 在 hasMore 存在时
        优先以它为准（共享分页契约，与 contacts 一致）。
        """
        client = _client()
        page1 = {
            "sessions": [
                {"username": f"wxid_{i:04d}", "displayName": f"会话{i}"}
                for i in range(1000)
            ],
            "hasMore": True,
        }
        page2 = {
            "sessions": [
                {"username": f"wxid_{i:04d}", "displayName": f"会话{i}"}
                for i in range(1000, 1200)
            ],
            "hasMore": False,
        }
        with patch.object(
            client, "_get", AsyncMock(side_effect=[page1, page2])
        ) as get:
            sessions = await client.fetch_sessions()
        self.assertEqual(len(sessions), 1200)
        offsets = [c.kwargs["params"]["offset"] for c in get.await_args_list]
        self.assertEqual(offsets, [0, 1000])
        self.assertEqual(sessions[1199]["username"], "wxid_1199")


class ControlEventStatsTest(unittest.IsolatedAsyncioTestCase):
    """控制事件（ready / sync / ping）不进管道、也不计入监听统计。"""

    async def test_control_events_skipped_without_inflating_stats(self):
        """上游无就绪门控后每次重连都会带 ready（+可能 sync）基线帧。

        它们不是消息：既不该进管道，也不该计入「事件」或「预过滤丢弃」——
        否则「无消息静默」统计失效（与 qqflow 监听器一致）。
        ready 帧的载荷实测为 {"status":"ok"}，不含 event 键。

        ping 一并断言：上游保活为注释行 `:ping`，而 stream_events 只解析
        `data: ` 行，故心跳当前永不成为事件——这里喂的是手工构造帧，守的是
        「上游改用 data 帧时不污染统计」这条前瞻契约（与 qqflow 监听器同形，
        见 test_source_robustness.QqFlowControlEventStatsTest）。
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
        await listener._handle_event({"event": "ping"})  # type: ignore[arg-type]
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
