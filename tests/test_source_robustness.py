"""消息源健壮性修复测试（审查报告 P1/P2/P3 项）。

覆盖：
- qqflow fetch_sessions 大 limit（会话发现截断坑，对齐 weflow-legacy 同接口）
- 两源 SSE 读超时（config 字段 + 客户端 Timeout 构造）
- qqflow ensure_ready 被拒态不记忆化（下轮重试引导注册）
- 两源回查链路 _LOOKUP_LIMIT=200 且显式 retry_on_empty=False
- weflow-legacy SSE (event, rawid) FIFO 去重缓存
- 两源 stop()/aclose() 冲刷批缓冲并等待 in-flight 批任务收尾
- qqflow sync/ping 心跳不计入消息统计
- qqflow SSE 地址 URL.join 防拼接坏、weflow-legacy 错误体安全解码

全部用内存构造（mock 方法/假回调），不触碰网络与文件系统。
"""

import asyncio
import os
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from briefdesk.config import config
from briefdesk.plugins.qqflow.client import QqFlowClient
from briefdesk.plugins.qqflow.config import QqFlowSettings
from briefdesk.plugins.qqflow.sse import QqFlowSseClient
from briefdesk.plugins.weflow.config import WeFlowSettings
from briefdesk.plugins.weflow_legacy.client import WeFlowLegacyClient
from briefdesk.plugins.weflow_legacy.config import WeFlowLegacySettings
from briefdesk.plugins.weflow_legacy.sse import WeFlowLegacySseClient


def _wf_event(rawid: str = "r1", content: str = "今天下午三点开会讨论") -> dict:
    """构造可通过 weflow-legacy pre_filter 的 SSE 事件（非图片、内容 >= 5 字）。"""
    return {
        "event": "message.new",
        "sessionType": "group",
        "sessionId": "12345@chatroom",
        "rawid": rawid,
        "sourceName": "张三",
        "groupName": "测试群",
        "content": content,
        "timestamp": 1760000123,
    }


def _qq_message_new_event(rawid: str = "r1") -> dict:
    """构造可通过 qqflow pre_filter 的 message.new 事件。"""
    return {
        "event": "message.new",
        "sessionId": "10001",
        "sessionType": "group",
        "groupName": "测试群",
        "rawid": rawid,
        "sourceName": "张三",
        "content": "今天下午三点开会讨论",
        "timestamp": 1760000123,
    }


class QqFlowFetchSessionsLimitTest(unittest.IsolatedAsyncioTestCase):
    """【1·P1】fetch_sessions 必须按大页大小翻页取尽，否则被上游默认 100 截断。

    page_size=10000 = 上游 limit 硬上限：典型规模一个请求即取尽（与旧
    「显式大 limit」实现请求数相同）；旧上游若忽略 offset，共享「本页无
    新增」防御停在 10000 条——零回退。
    """

    async def test_fetch_sessions_pages_with_max_page_size(self):
        client = QqFlowClient("http://127.0.0.1:5032", "tok")
        captured: dict = {}

        async def fake_get(path, *, params=None, not_found_ok=False):
            captured["path"] = path
            captured["params"] = params
            return {"sessions": []}

        client._get = fake_get  # type: ignore[method-assign]
        await client.fetch_sessions()
        self.assertEqual(captured["path"], "/api/v1/sessions")
        self.assertEqual(captured["params"], {"limit": 10000, "offset": 0})


class SseReadTimeoutTest(unittest.TestCase):
    """【2·P1】两源 SSE 读超时：config 字段默认值 + 客户端 Timeout 构造。

    默认值断言与进程环境隔离：patch.dict 下 pop 两个 READ_TIMEOUT 环境变量
    并以 `_env_file=None` 跳过 .env，只验证字段默认值；客户端级换算改为
    显式传参验证（不隐式依赖进程环境/.env 的当前值）。
    """

    def test_weflow_legacy_config_default(self):
        # weflow-legacy 上游无心跳：只能靠 5 分钟兜住半开连接
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("WEFLOW_LEGACY_SSE_READ_TIMEOUT_MS", None)
            os.environ.pop("QQFLOW_SSE_READ_TIMEOUT_MS", None)
            settings = WeFlowLegacySettings(_env_file=None)
        self.assertEqual(settings.sse_read_timeout_ms, 300000)

    def test_weflow_config_default_matches_heartbeat(self):
        """weflow-server 每 25s 发 ping → 60s（≈2.4 周期），与 qqflow 同口径。

        这里曾经是 300000：从无心跳的 weflow-legacy 抄来的默认值，在有心跳
        的 weflow 上等于白等 4 分半才发现连接已死。
        """
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("WEFLOW_SSE_READ_TIMEOUT_MS", None)
            settings = WeFlowSettings(_env_file=None)
        self.assertEqual(settings.sse_read_timeout_ms, 60000)

    def test_qqflow_config_default(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("WEFLOW_LEGACY_SSE_READ_TIMEOUT_MS", None)
            os.environ.pop("QQFLOW_SSE_READ_TIMEOUT_MS", None)
            settings = QqFlowSettings(_env_file=None)
        self.assertEqual(settings.sse_read_timeout_ms, 60000)

    def test_weflow_client_builds_timeout_with_read(self):
        client = WeFlowLegacyClient(
            "http://127.0.0.1:5031", "tok", sse_read_timeout_ms=300000
        )
        t = client.sse_timeout()
        self.assertEqual(t.read, 300.0)
        self.assertEqual(t.connect, 10.0)
        self.assertIsNone(t.write)
        self.assertIsNone(t.pool)

    def test_weflow_client_explicit_override(self):
        client = WeFlowLegacyClient("http://127.0.0.1:5031", "tok", sse_read_timeout_ms=1500)
        self.assertEqual(client.sse_timeout().read, 1.5)

    def test_qqflow_client_builds_timeout_with_read(self):
        client = QqFlowClient(
            "http://127.0.0.1:5032",
            "tok",
            qq="1",
            key="k",
            sse_read_timeout_ms=60000,
        )
        t = client.sse_timeout()
        self.assertEqual(t.read, 60.0)
        self.assertEqual(t.connect, 10.0)
        self.assertIsNone(t.write)
        self.assertIsNone(t.pool)


class EnsureReadyRejectedStateTest(unittest.IsolatedAsyncioTestCase):
    """【4·P2】ensure_ready 注册被拒（invalid_key 等）不得记忆化，下轮须重试。"""

    def _make_client(
        self,
        state: str,
        calls: list,
        phase: str = "unregistered",
        accounts: list | None = None,
    ):
        client = QqFlowClient(
            "http://127.0.0.1:5032", "tok", qq="123", key="bad-key"
        )

        async def fake_health():
            # 标量形状：/health 只给一个 account 阶段，不再列账号。
            return {"status": "starting", "version": "0.5.0", "account": phase}

        async def fake_register(qq, key, db_path):
            calls.append(state)
            return state

        async def fake_accounts():
            # error 阶段会走根因诊断；不打桩就会真发一次 HTTP 请求。
            return accounts or []

        client.fetch_health = fake_health  # type: ignore[method-assign]
        client.register_account = fake_register  # type: ignore[method-assign]
        client.fetch_accounts = fake_accounts  # type: ignore[method-assign]
        return client

    async def test_rejected_state_is_not_memoized(self):
        calls: list = []
        client = self._make_client("invalid_key", calls)
        await client.ensure_ready()
        await client.ensure_ready()
        self.assertEqual(len(calls), 2, "被拒后必须再次尝试引导注册")

    async def test_benign_state_is_memoized(self):
        calls: list = []
        client = self._make_client("accepted", calls)
        await client.ensure_ready()
        await client.ensure_ready()
        self.assertEqual(len(calls), 1, "良性状态应记忆化避免重复注册")

    async def test_account_conflict_is_not_memoized(self):
        """服务端被另一个账号占用：不记忆化，配置修正/对方注销后可自愈。"""
        calls: list = []
        client = self._make_client("account_conflict", calls)
        await client.ensure_ready()
        await client.ensure_ready()
        self.assertEqual(len(calls), 2, "占用冲突后必须再次尝试")

    async def test_indexing_phase_skips_registration(self):
        """服务端已在建索引：跳过注册并记忆化。

        回归防护：这条优化此前从未生效 —— 代码拿**注册结果**的词表
        (accepted/in_progress/...) 去比对 /health 的账号状态
        (awaiting_key/indexing/...)，两套枚举不相交，分支恒为假，
        于是索引期内每一轮 poll 都会重复注册一次。
        """
        calls: list = []
        client = self._make_client("accepted", calls, phase="indexing")
        await client.ensure_ready()
        await client.ensure_ready()
        self.assertEqual(len(calls), 0, "索引期不应发起注册")

    async def test_ready_phase_skips_registration(self):
        calls: list = []
        client = self._make_client("accepted", calls, phase="ready")
        await client.ensure_ready()
        self.assertEqual(len(calls), 0, "已就绪不应发起注册")

    async def test_error_phase_retries_registration(self):
        """error 阶段仍要注册：服务端的 error 不释放绑定，但同一账号可重试恢复。"""
        calls: list = []
        client = self._make_client("accepted", calls, phase="error")
        await client.ensure_ready()
        self.assertEqual(len(calls), 1, "error 阶段应尝试重新注册以恢复")

    async def test_error_phase_logs_root_cause_from_detail_endpoint(self):
        """error 阶段：/health 只给标量，根因须从需鉴权的明细接口捞出并告警。

        与 weflow 客户端同形（test_weflow_contract 有对应用例）：没有这一条，
        用户只能看到业务接口持续 503，看不到「密钥错误」这类真正的原因。
        """
        calls: list = []
        detail = [{"qq": "123", "state": "error", "error": "密钥校验失败"}]
        client = self._make_client(
            "accepted", calls, phase="error", accounts=detail
        )
        with self.assertLogs("briefdesk.plugins.qqflow.client", "WARNING") as logs:
            await client.ensure_ready()
        self.assertTrue(any("密钥校验失败" in m for m in logs.output))
        self.assertTrue(any("123" in m for m in logs.output))
        self.assertEqual(len(calls), 1, "诊断不得挡住注册重试")

    async def test_detail_endpoint_failure_does_not_block_registration(self):
        """明细接口不可用（鉴权/网络）：仅降级为 debug，注册照常发起。"""
        calls: list = []
        client = self._make_client("accepted", calls, phase="error")

        async def boom():
            raise RuntimeError("401")

        client.fetch_accounts = boom  # type: ignore[method-assign]
        await client.ensure_ready()
        self.assertEqual(len(calls), 1, "诊断失败不得挡住注册重试")

    async def test_version_logged_once_per_change(self):
        """/health 的 version 记入 _logged_version 并只在变化时打印。

        它有第二个用途：fetch_all_pages 的「上游可能不支持 offset」告警要靠
        upstream_version 补版本号（sources_base.py），而 qqflow 的两处调用
        此前都没传 —— 最需要版本号定位的场景反而拿不到。与 weflow 客户端同形。
        """
        calls: list = []
        client = self._make_client("accepted", calls, phase="ready")

        with self.assertLogs("briefdesk.plugins.qqflow.client", "INFO") as cm:
            await client.ensure_ready()
        self.assertEqual(client._logged_version, "0.5.0")
        version_lines = [ln for ln in cm.output if "qqflow-server 版本" in ln]
        self.assertEqual(len(version_lines), 1, f"版本应恰好打印一行: {cm.output}")
        self.assertIn("0.5.0", version_lines[0])

        # 同版本重检（force 绕过记忆化）不重复打印
        with self.assertNoLogs("briefdesk.plugins.qqflow.client", "INFO"):
            await client.ensure_ready(force=True)
        self.assertEqual(client._logged_version, "0.5.0")


class LookupLimitTest(unittest.IsolatedAsyncioTestCase):
    """【5·P2】回查链路 limit 提升为 _LOOKUP_LIMIT=200 且显式关闭空结果重试。"""

    async def test_weflow_lookup_uses_limit_and_no_retry(self):
        client = WeFlowLegacyClient("http://127.0.0.1:5031", "tok")
        captured: dict = {}

        async def fake_fetch(talker, start_ts, limit=500, offset=0, media=False,
                             retry_on_empty=True):
            captured["limit"] = limit
            captured["retry_on_empty"] = retry_on_empty
            return {"messages": []}

        client.fetch_messages = fake_fetch  # type: ignore[method-assign]
        result = await client._lookup_message("wxid_x", "raw1", 1760000123, False)
        self.assertIsNone(result)
        self.assertEqual(captured["limit"], 200)
        self.assertIs(captured["retry_on_empty"], False)

    async def test_qqflow_lookup_uses_limit(self):
        client = QqFlowClient("http://127.0.0.1:5032", "tok", qq="1", key="k")
        captured: dict = {}

        async def fake_fetch(talker, start=None, limit=500, offset=0):
            captured["limit"] = limit
            return {"messages": []}

        client.fetch_messages = fake_fetch  # type: ignore[method-assign]
        result = await client.lookup_message("u_x", "42", 1760000123)
        self.assertIsNone(result)
        self.assertEqual(captured["limit"], 200)


class WeflowSseDedupTest(unittest.IsolatedAsyncioTestCase):
    """【6·P2】weflow-legacy 监听器按 (event, rawid) 去重：同一事件重投只消费一次。"""

    async def test_duplicate_event_consumed_once(self):
        received: list = []

        async def on_batch(batch):
            received.extend(batch)

        listener = WeFlowLegacySseClient(
            WeFlowLegacyClient("http://127.0.0.1:5031", "tok"), on_batch,
            settings=WeFlowLegacySettings(),
        )
        ev = _wf_event()
        await listener._handle_event(ev)
        await listener._handle_event(ev)
        await asyncio.sleep(0)  # 让 fire-and-forget 的批刷新任务跑完
        self.assertEqual(len(received), 1, "重复投递的事件只允许进管道一次")
        self.assertEqual(listener._stats_deduped, 1)

    async def test_distinct_events_both_consumed(self):
        received: list = []

        async def on_batch(batch):
            received.extend(batch)

        listener = WeFlowLegacySseClient(
            WeFlowLegacyClient("http://127.0.0.1:5031", "tok"), on_batch,
            settings=WeFlowLegacySettings(),
        )
        await listener._handle_event(_wf_event(rawid="a"))
        await listener._handle_event(_wf_event(rawid="b"))
        await asyncio.sleep(0)
        self.assertEqual(len(received), 2)


class StopDrainsBufferTest(unittest.IsolatedAsyncioTestCase):
    """【8·P3】stop() 后冲刷批缓冲残余消息，aclose() 等待 in-flight 收尾。"""

    async def test_stop_flushes_buffered_message(self):
        received: list = []

        async def on_batch(batch):
            received.extend(batch)

        client = WeFlowLegacyClient("http://127.0.0.1:5031", "tok")
        listener = WeFlowLegacySseClient(client, on_batch, settings=WeFlowLegacySettings())
        with patch.object(config, "realtime_batch_max_count", 5):
            await listener._handle_event(_wf_event())  # 攒在缓冲区不触发刷新
        self.assertEqual(len(received), 0)
        listener.stop()
        await listener.aclose()
        self.assertEqual(len(received), 1, "停止时缓冲区内消息必须被冲刷")

    async def test_aclose_waits_inflight_flush(self):
        received: list = []
        release = asyncio.Event()

        async def slow_on_batch(batch):
            await release.wait()
            received.extend(batch)

        client = WeFlowLegacyClient("http://127.0.0.1:5031", "tok")
        listener = WeFlowLegacySseClient(client, slow_on_batch, settings=WeFlowLegacySettings())
        await listener._handle_event(_wf_event())  # 默认 max_count=1 → in-flight
        self.assertEqual(len(received), 0)
        listener.stop()
        release.set()
        await listener.aclose()
        self.assertEqual(len(received), 1, "aclose 必须等待 in-flight 批处理收尾")

    async def test_qqflow_stop_flushes_buffered_message(self):
        received: list = []

        async def on_batch(batch):
            received.extend(batch)

        client = QqFlowClient("http://127.0.0.1:5032", "tok", qq="1", key="k")
        listener = QqFlowSseClient(client, on_batch, settings=QqFlowSettings())
        with patch.object(config, "realtime_batch_max_count", 5):
            await listener._handle_event(_qq_message_new_event())
        self.assertEqual(len(received), 0)
        listener.stop()
        await listener.aclose()
        self.assertEqual(len(received), 1)


class QqFlowControlEventStatsTest(unittest.IsolatedAsyncioTestCase):
    """【9·P3】sync/ping 心跳不计入事件数与预过滤丢弃数。"""

    async def test_sync_and_ping_not_counted_as_events(self):
        """ping 喂的是手工构造帧：上游保活为注释行 `:ping`，而
        client.stream_events 只解析 `data: ` 行，故心跳当前永不成为事件。
        这里守的是「上游改用 data 帧时不污染统计」这条前瞻契约（与 weflow
        监听器同形，见 test_weflow_contract.ControlEventStatsTest）。
        """
        client = QqFlowClient("http://127.0.0.1:5032", "tok", qq="1", key="k")
        client.lookup_message = AsyncMock(return_value=None)  # type: ignore[method-assign]
        received: list = []

        async def on_batch(batch):
            received.extend(batch)

        listener = QqFlowSseClient(client, on_batch, settings=QqFlowSettings())
        sync_ev = {
            "event": "sync", "sessionId": "", "sessionType": "",
            "rawid": "", "content": "", "timestamp": 1,
        }
        await listener._handle_event(sync_ev)
        await listener._handle_event({"event": "ping"})
        self.assertEqual(listener._stats_events, 0)
        self.assertEqual(listener._stats_filtered, 0)

        await listener._handle_event(_qq_message_new_event())
        await asyncio.sleep(0)
        self.assertEqual(listener._stats_events, 1)
        self.assertEqual(len(received), 1)


class QqFlowPushUrlJoinTest(unittest.TestCase):
    """【10·P3】SSE 地址经 RFC 3986 join，base_url 误带查询串时不拼坏。"""

    def test_push_url_joined_against_query_base(self):
        client = QqFlowClient("http://127.0.0.1:5032/api/v1/push?x=1", "tok")
        url = client._push_url()
        self.assertEqual(url, "http://127.0.0.1:5032/api/v1/push/messages")


class WeflowErrorBodySafeDecodeTest(unittest.IsolatedAsyncioTestCase):
    """【10·P3】非 UTF-8 错误体不应让 UnicodeDecodeError 掩盖原始 API 错误。"""

    async def test_non_utf8_error_body_raises_runtime_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, content=b"\xff\xfe\xff binary")

        client = WeFlowLegacyClient("http://127.0.0.1:5031", "tok")
        client._client = httpx.AsyncClient(
            base_url="http://127.0.0.1:5031", transport=httpx.MockTransport(handler)
        )
        with self.assertRaises(RuntimeError):
            await client._get("/api/v1/contacts")


class _RecordingListener:
    """最小 RealtimeListener 形状：记录 stop/aclose 调用顺序（供关停顺序断言）。"""

    def __init__(self, events: list):
        self._events = events

    def start(self) -> None:
        return None

    def stop(self) -> None:
        self._events.append("stop")

    async def aclose(self) -> None:
        self._events.append("aclose")

    def invalidate_session_cache(self) -> None:
        return None


class RuntimeCloseOrderingTest(unittest.IsolatedAsyncioTestCase):
    """【增量·P3】runtime.close()：stop → await listener.aclose → client.close。"""

    async def test_weflow_close_ordering_and_idempotent(self):
        from briefdesk.plugins.weflow_legacy.runtime import WeFlowLegacySource

        events: list = []
        source = WeFlowLegacySource(base_url="http://127.0.0.1:5031", api_token="tok")
        closed = {"done": False}

        async def recording_close():  # 镜像真实 client.close 的幂等守卫
            if closed["done"]:
                return
            closed["done"] = True
            events.append("client_closed")

        source.client.close = recording_close  # type: ignore[method-assign]
        source.listener = _RecordingListener(events)
        await source.close()
        self.assertEqual(events, ["stop", "aclose", "client_closed"])
        self.assertIsNone(source.listener)
        await source.close()  # 幂等：第二次 close 无副作用
        self.assertEqual(events, ["stop", "aclose", "client_closed"])

    async def test_qqflow_close_ordering_and_idempotent(self):
        from briefdesk.plugins.qqflow.runtime import QqFlowSource

        events: list = []
        source = QqFlowSource(base_url="http://127.0.0.1:5032", api_token="tok")
        closed = {"done": False}

        async def recording_close():  # 镜像真实 client.close 的幂等守卫
            if closed["done"]:
                return
            closed["done"] = True
            events.append("client_closed")

        source.client.close = recording_close  # type: ignore[method-assign]
        source.listener = _RecordingListener(events)
        await source.close()
        self.assertEqual(events, ["stop", "aclose", "client_closed"])
        self.assertIsNone(source.listener)
        await source.close()
        self.assertEqual(events, ["stop", "aclose", "client_closed"])

    async def test_weflow_close_flushes_pending_batch_before_returning(self):
        """真实监听器接线：close 返回前必须完成缓冲消息冲刷（先于 client.close）。"""
        from briefdesk.plugins.weflow_legacy.runtime import WeFlowLegacySource

        received: list = []

        async def on_batch(batch):
            received.extend(batch)

        source = WeFlowLegacySource(base_url="http://127.0.0.1:5031", api_token="tok")
        source.start(on_batch)
        with patch.object(config, "realtime_batch_max_count", 5):
            await source.listener._handle_event(_wf_event())  # 攒在缓冲区
        self.assertEqual(len(received), 0)
        await source.close()
        self.assertEqual(
            len(received), 1, "close 返回前缓冲区内消息必须已冲刷交付"
        )

    async def test_qqflow_close_flushes_pending_batch_before_returning(self):
        from briefdesk.plugins.qqflow.runtime import QqFlowSource

        received: list = []

        async def on_batch(batch):
            received.extend(batch)

        source = QqFlowSource(base_url="http://127.0.0.1:5032", api_token="tok")
        source.start(on_batch)
        with patch.object(config, "realtime_batch_max_count", 5), patch.object(
            config, "ignore_self", False
        ):
            await source.listener._handle_event(_qq_message_new_event())
        self.assertEqual(len(received), 0)
        await source.close()
        self.assertEqual(len(received), 1)




class SseRawidGuardTest(unittest.TestCase):
    """message.new 缺 rawid 的就地拦截（审查 A5）。

    rawid/serverId 是 msg_id 与 processed 标记的根基：缺失消息放行只会在
    去重键 ("message.new","") 碰撞与回填间反复投递，必须在 pre_filter 拦下。"""

    def test_weflow_message_new_without_rawid_dropped(self):
        from briefdesk.plugins.weflow_legacy.normalize import pre_filter_sse

        ev = {"event": "message.new", "content": "正常内容长度超过五字"}
        self.assertFalse(pre_filter_sse(ev))

    def test_weflow_with_rawid_still_passes_shape(self):
        from briefdesk.plugins.weflow_legacy.normalize import pre_filter_sse

        ev = {"event": "message.new", "rawid": "r1", "content": "正常内容长度超过五字"}
        self.assertTrue(pre_filter_sse(ev))

    def test_qqflow_message_new_without_rawid_dropped(self):
        from briefdesk.plugins.qqflow.normalize import pre_filter_sse

        ev = {"event": "message.new", "sourceName": "张三", "content": "这条内容长度肯定满足过滤阈值"}
        self.assertFalse(pre_filter_sse(ev))

    def test_qqflow_with_rawid_still_passes_shape(self):
        from briefdesk.plugins.qqflow.normalize import pre_filter_sse

        ev = {
            "event": "message.new",
            "rawid": "r1",
            "sourceName": "张三",
            "content": "这条内容长度肯定满足过滤阈值",
        }
        self.assertTrue(pre_filter_sse(ev))


if __name__ == "__main__":
    unittest.main()
