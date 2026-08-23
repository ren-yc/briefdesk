"""内部事件总线单元测试（briefdesk/events.py）。"""

import unittest

from briefdesk.events import EventBus


class PublishTest(unittest.IsolatedAsyncioTestCase):
    async def test_sync_handler_receives_payload(self):
        bus = EventBus()
        received: list = []
        bus.subscribe("e", lambda p: received.append(p))
        await bus.publish("e", {"x": 1})
        self.assertEqual(received, [{"x": 1}])

    async def test_async_handler_awaited(self):
        bus = EventBus()
        received: list = []

        async def handler(payload):
            received.append(payload)

        bus.subscribe("e", handler)
        await bus.publish("e", "ok")
        self.assertEqual(received, ["ok"])

    async def test_sync_handler_returning_coroutine_awaited(self):
        bus = EventBus()
        received: list = []

        async def inner(payload):
            received.append(payload)

        def wrapper(payload):
            return inner(payload)

        bus.subscribe("e", wrapper)
        await bus.publish("e", "wrapped")
        self.assertEqual(received, ["wrapped"])

    async def test_handler_exception_does_not_block_others(self):
        bus = EventBus()
        received: list = []

        def bad(payload):
            raise RuntimeError("boom")

        bus.subscribe("e", bad)
        bus.subscribe("e", received.append)
        await bus.publish("e", "still-delivered")
        self.assertEqual(received, ["still-delivered"])

    async def test_unsubscribe_stops_delivery(self):
        bus = EventBus()
        received: list = []
        handler = received.append
        bus.subscribe("e", handler)
        bus.unsubscribe("e", handler)
        await bus.publish("e", 1)
        self.assertEqual(received, [])

    async def test_unsubscribe_unknown_handler_noop(self):
        bus = EventBus()
        received: list = []
        bus.subscribe("e", received.append)
        bus.unsubscribe("e", lambda p: None)
        await bus.publish("e", 1)
        self.assertEqual(received, [1])

    async def test_duplicate_subscribe_calls_once(self):
        bus = EventBus()
        received: list = []
        handler = received.append
        bus.subscribe("e", handler)
        bus.subscribe("e", handler)
        await bus.publish("e", 1)
        self.assertEqual(received, [1])

    async def test_unrelated_events_isolated(self):
        bus = EventBus()
        received: list = []
        bus.subscribe("a", received.append)
        await bus.publish("b", 1)
        self.assertEqual(received, [])
