"""去重判官负向缓存单元测试（纯内存，不触 DB / AI）。

覆盖批次契约点：
- LRU 语义（容量淘汰最久未用、lookup 刷新位置、重复 record 不重复淘汰）；
- 反向索引失效（invalidate_items 使该 item 全部对子失效；重复失效幂等）；
- query_key 稳定性与分隔（标题/原文拼接防碰撞、输入变化即换键）。
"""

import unittest

from briefdesk import telemetry
from briefdesk.negcache import (
    DEFAULT_NEGCACHE_CAPACITY,
    NegativeCache,
    query_key,
    reset_negcache_for_tests,
)


class QueryKeyTest(unittest.TestCase):
    def test_stable_and_distinct(self):
        assert query_key("t", "q") == query_key("t", "q")
        assert query_key("t", "q") != query_key("t", "q2")
        assert query_key("t", "q") != query_key("t2", "q")

    def test_separator_prevents_boundary_collision(self):
        # \0 分隔：("a", "bc") 与 ("ab", "c") 的朴素拼接会撞车
        assert query_key("a", "bc") != query_key("ab", "c")

    def test_short_hash_format(self):
        k = query_key("标题", "原文")
        self.assertEqual(len(k), 16)
        int(k, 16)  # 十六进制形态


class NegativeCacheTest(unittest.TestCase):
    def setUp(self):
        telemetry.reset_for_tests()

    def tearDown(self):
        telemetry.reset_for_tests()

    def test_record_and_lookup(self):
        c = NegativeCache()
        c.record("q1", "i1")
        assert c.lookup("q1", "i1") is True
        assert c.lookup("q1", "i2") is False
        assert c.lookup("q2", "i1") is False
        assert len(c) == 1

    def test_lru_eviction_order(self):
        c = NegativeCache(capacity=2)
        c.record("q1", "i1")
        c.record("q1", "i2")
        c.lookup("q1", "i1")  # 刷新 i1 → i2 成为最久未用
        c.record("q1", "i3")  # 淘汰 i2
        assert c.lookup("q1", "i2") is False
        assert c.lookup("q1", "i1") is True
        assert c.lookup("q1", "i3") is True

    def test_rerecord_does_not_evict(self):
        c = NegativeCache(capacity=2)
        c.record("q1", "i1")
        c.record("q1", "i2")
        c.record("q1", "i1")  # 已存在：移动位置，不得触发淘汰
        assert c.lookup("q1", "i1") is True
        assert c.lookup("q1", "i2") is True
        assert len(c) == 2

    def test_invalidate_items_removes_all_pairs_of_item(self):
        c = NegativeCache()
        c.record("q1", "i1")
        c.record("q2", "i1")
        c.record("q1", "i2")  # 不涉及 i1，须保留
        removed = c.invalidate_items(["i1"])
        assert removed == 2
        assert c.lookup("q1", "i1") is False
        assert c.lookup("q2", "i1") is False
        assert c.lookup("q1", "i2") is True
        assert len(c) == 1

    def test_invalidate_unknown_item_is_noop(self):
        c = NegativeCache()
        assert c.invalidate_items(["ghost"]) == 0

    def test_invalidate_is_idempotent(self):
        c = NegativeCache()
        c.record("q1", "i1")
        assert c.invalidate_items(["i1"]) == 1
        assert c.invalidate_items(["i1"]) == 0

    def test_invalidate_keeps_index_consistent_after_eviction(self):
        # 淘汰过的对子从反向索引一并摘除：失效计数只数活着的对子
        c = NegativeCache(capacity=1)
        c.record("q1", "i1")
        c.record("q2", "i2")  # 淘汰 (q1,i1)，i1 的索引应清空
        assert c.invalidate_items(["i1"]) == 0
        assert c.invalidate_items(["i2"]) == 1

    def test_clear(self):
        c = NegativeCache()
        c.record("q1", "i1")
        c.clear()
        assert len(c) == 0
        assert c.lookup("q1", "i1") is False

    def test_capacity_floor(self):
        # 容量下限 1：0/负数不会退化成无界或立刻抛错
        c = NegativeCache(capacity=0)
        c.record("q1", "i1")
        c.record("q1", "i2")
        assert len(c) == 1

    def test_default_capacity_value(self):
        assert DEFAULT_NEGCACHE_CAPACITY >= 50_000


class ResetForTestsTest(unittest.TestCase):
    def test_reset_replaces_singleton(self):
        from briefdesk import negcache

        # 模块门面只经测试隔离入口写（生产写路径在引擎实例上）
        negcache._module_instance.record("q", "i")
        assert negcache.negcache_size() == 1
        reset_negcache_for_tests()
        assert negcache.negcache_size() == 0
        # 容量可换
        reset_negcache_for_tests(capacity=3)
        negcache._module_instance.record("q", "i")
        negcache._module_instance.record("q", "j")
        negcache._module_instance.record("q", "k")
        negcache._module_instance.record("q", "l")
        assert negcache.negcache_size() == 3


class TelemetryTest(unittest.TestCase):
    def test_evict_and_invalidate_counted(self):
        telemetry.reset_for_tests()
        c = NegativeCache(capacity=1)
        c.record("q1", "i1")
        c.record("q1", "i2")  # 触发一次 evict
        c.invalidate_items(["i2"])  # 触发一次 invalidate
        snap = telemetry.snapshot()
        assert snap["counters"]["negcache.evict|"] == 1
        assert snap["counters"]["negcache.invalidate|"] == 1
        telemetry.reset_for_tests()


if __name__ == "__main__":
    unittest.main()
