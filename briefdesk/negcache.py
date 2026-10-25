"""去重判官负向缓存 — 有界 LRU 记录「判过 DIFFERENT 的候选对」。

目的：判官减负。语义去重管线上，绝大多数候选对经判官判定为 DIFFERENT；
重复消息场景（同一条消息被上游重复投递、相似消息连续到达）会让同一查询
与同一缓存条目反复触发完整 AI 判定。本缓存记录确定性 DIFFERENT 结论，
命中时判官直接返回 False（语义 = 上次对相同输入已判 DIFFERENT）。

键形态与失效模型：
- 对子键 = (查询侧键, item_id)。查询侧键取**判官输入文本哈希**
  （query_key(title, quote)）而非消息 id：判官判定是两段文本的函数，文本
  相同则判定相同；且查询消息在判定时点还没有 item_id（它可能判重命中、
  也可能成为新卡），引擎拿不到稳定的消息身份——文本哈希自包含且天然覆盖
  「同一消息重试时标题概括变化」的场景（哈希不同 = 输入确实变了 = 该重判）。
- 「结论仍成立」假设由失效钩子保证：查询侧文本不可变，失效全部按 item 侧
  归约——反向索引 item_id → 对子键集合，统一「使该 item 全部对子失效」。
  键里不含 title/quote 文本：缓存条目文本变更时（合并重写/标题补写）旧
  结论对新文本不再成立，同样按 item 侧失效。

失效钩子完备清单（引擎侧集成点，回归见 tests/test_negcache.py 与
tests/test_dedup.py）：
1. EVENT_ITEMS_DELETED / EVENT_ITEMS_PURGED → DedupEngine.remove_items；
2. merge 重写 → merge 插件 run 内 remove_items（两张卡都失效）；
3. recat 改类 → routes_items 显式调 DedupEngine.invalidate_negcache（文本
   未变、判定仍成立，失效只为满足「任一侧 category 变更即失效」的保守
   口径——过度失效只损失缓存，不损正确性）；
4. 重拟标题的批尾补写（锁外 after_run 的第二失效点）→ add_to_cache 全
   字段覆盖时自动失效（同 id 重加即失效，勿只挂 merge 时刻）；
5. add_to_cache / remove_items 两条引擎内部路径已覆盖 1/2/4，无需新增
   调用点；事件处理器与路由只处理引擎外触发源。

容量与遥测：LRU 上限按「活跃卡数 × 平均候选数 × 安全系数」约 5 万对量级
（DEFAULT_NEGCACHE_CAPACITY）；evict/invalidate 在类内记遥测，hit/miss 由
引擎在判定入口记（negcache.hit / negcache.miss），供验收读数。
"""

from __future__ import annotations

import hashlib
import logging
from collections import OrderedDict
from collections.abc import Iterable

from briefdesk import telemetry

logger = logging.getLogger(__name__)

# 默认容量（对子数）。5 万对 × 每对键约百字节 ≈ 数 MB 内存上限；活跃
# 对子数低于容量时 LRU 不淘汰，语义退化为普通字典。
DEFAULT_NEGCACHE_CAPACITY = 50_000


def query_key(title: str, quote: str) -> str:
    """查询侧键：判官输入文本的 sha256 短哈希（与 content_hash 同长度口径）。

    \\0 分隔标题与原文，防「A|BC」与「AB|C」拼出同一串；不剥空白/不做
    归一化——判官看到什么就哈希什么，保持与判定输入严格同口径。
    """
    return hashlib.sha256(f"{title}\x00{quote}".encode()).hexdigest()[:16]


class NegativeCache:
    """判官负向缓存：OrderedDict LRU + item 侧反向索引。

    全部方法同步、无 await：调用点在存储锁内或事件处理器的同步上下文，
    与去重缓存同样的并发模型（事件循环单线程消费，无跨线程访问）。
    实例归 DedupEngine 所有（引擎局部而非模块级单例）：基准引擎每用例
    新建实例即天然隔离，生产引擎实例与进程同生命周期。
    """

    def __init__(self, capacity: int = DEFAULT_NEGCACHE_CAPACITY) -> None:
        self._capacity = max(1, capacity)
        # OrderedDict 尾部 = 最近使用；容量满淘汰队首（最久未用对子）
        self._pairs: OrderedDict[tuple[str, str], None] = OrderedDict()
        # item 侧反向索引：item_id → 该 item 参与的对子键集合
        self._by_item: dict[str, set[tuple[str, str]]] = {}

    def _remember(self, key: tuple[str, str], item_id: str) -> None:
        existed = key in self._pairs
        self._pairs[key] = None
        self._pairs.move_to_end(key)
        self._by_item.setdefault(item_id, set()).add(key)
        if not existed:
            while len(self._pairs) > self._capacity:
                old_key, _ = self._pairs.popitem(last=False)
                members = self._by_item.get(old_key[1])
                if members is not None:
                    members.discard(old_key)
                    if not members:
                        self._by_item.pop(old_key[1], None)
                telemetry.record("negcache.evict")

    def lookup(self, qkey: str, item_id: str) -> bool:
        """单候选查询：(qkey, item_id) 上次已判 DIFFERENT → True。

        命中把对子移到 LRU 尾部（最近使用语义）。调用方按候选粒度逐对
        调用并记 hit/miss 遥测（本方法只管缓存语义，不管计数口径）。
        """
        key = (qkey, item_id)
        if key in self._pairs:
            self._pairs.move_to_end(key)
            return True
        return False

    def record(self, qkey: str, item_id: str) -> None:
        """记录一次确定性 DIFFERENT 判定（只记 False；None/True 不入缓存）。"""
        self._remember((qkey, item_id), item_id)

    def invalidate_items(self, item_ids: Iterable[str]) -> int:
        """使 item 侧涉及的全部对子失效（全部失效钩子的统一入口）。

        返回失效的对子数。缺失 id 静默跳过——失效幂等，重复触发
        （删除路径事件与 merge 内部清理先后到达）无副作用。
        """
        count = 0
        for item_id in set(item_ids):
            keys = self._by_item.pop(item_id, None)
            if not keys:
                continue
            for key in keys:
                # 值恒为 None（OrderedDict 只用作有序键集），不能以返回值
                # 判存在——须先查键再删
                if key in self._pairs:
                    del self._pairs[key]
                    count += 1
        if count:
            telemetry.record("negcache.invalidate")
        return count

    def clear(self) -> None:
        """全清（测试隔离用；生产进程内随重启自然为空）。"""
        self._pairs.clear()
        self._by_item.clear()

    def __len__(self) -> int:
        return len(self._pairs)


# ── 模块级门面：仅供测试隔离与诊断（引擎走实例属性 self._negcache） ──
# 生产代码不得经此写缓存：模块实例与引擎实例是两套，误调不会影响引擎判定。

_module_instance = NegativeCache()


def reset_negcache_for_tests(capacity: int | None = None) -> None:
    """整体替换模块级实例（测试隔离；生产不调用）。

    引擎持有的是自己的实例，不受本替换影响——这里只服务直接用模块门面
    写的测试。
    """
    global _module_instance
    _module_instance = NegativeCache(
        capacity=capacity if capacity is not None else DEFAULT_NEGCACHE_CAPACITY
    )


def negcache_size() -> int:
    """当前模块级实例的对子数（诊断用）。"""
    return len(_module_instance)
