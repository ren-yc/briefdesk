"""benchmark 测量基础设施回归（离线，不调真实 AI）。

覆盖判重测量链路的四个修复点，以及报告口径调整后的自证恒等式：

- 判重用例必须**镜像生产**拿到 query 向量（历史上从不传 q_emb，整轮其实在
  字符重叠通道上跑，报告却显示"嵌入开启"）；
- 每例的候选缓存必须**只看得到自己的 items**（历史上加载全表，前面的用例把
  卡片留给后面的用例当"历史"）；
- 实际判定通道与 AI 调用成本必须逐例落进报告（否则阈值放宽的代价看不见）；
- classify 的 mode 切片 / 纯占位符剔除、time 的回声点剔除、title 的字符相似
  替代精确匹配，都是"总分不可解释"的修法。

历史报告（payload 里没有本轮新增键）必须仍能渲染——报告 JSON 长期落盘。
"""

import json
import logging
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from briefdesk.plugins.benchmark import engine as bench_engine
from briefdesk.plugins.benchmark import metrics as bench_metrics
from briefdesk.plugins.benchmark.metrics import (
    BinaryCaseEval,
    ClassifyCaseEval,
    aggregate_classify,
    aggregate_dedup,
    evaluate_classify_case,
)
from briefdesk.plugins.benchmark.schema import (
    ClassifyCase,
    ClassifyExpected,
    DatasetError,
    DatasetFile,
    DedupCase,
    MessageIn,
    SameExpected,
    TimePoint,
    conflicting_dedup_case_ids,
    dedup_item_key_conflicts,
    parse_cases,
    shared_dedup_item_keys,
)
from briefdesk.plugins.dedup.engine import DedupEngine
from briefdesk.types import ClassifyOutcome, ClassifyResult
from tests._helpers import _scratch_db


def _resp(same: bool) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content='{"same": true}' if same else '{"same": false}'
                ),
                finish_reason="stop",
            )
        ]
    )


def _dedup_case(
    cid: str, item_title: str, query_title: str, *, same: bool, item_content: str = "", query_content: str = ""
) -> DedupCase:
    """构造判重用例。正文缺省取标题——那会让原文哈希短路先命中（channel=exact），
    需要测余弦/重叠通道时必须给两边不同的正文。"""
    item_content = item_content or item_title
    query_content = query_content or query_title
    return DedupCase(
        id=cid,
        items=[MessageIn(msg_id=f"{cid}-a", content=item_content, title=item_title)],
        query=MessageIn(msg_id=f"{cid}-q", content=query_content, title=query_title),
        expected=SameExpected(same=same),
    )


class TestDedupChannelRecording:
    """判重用例必须记录实际走的通道。"""

    async def test_embedding_path_records_cosine_channel(self):
        """配置了嵌入 → 真的拿 query 向量走余弦，并把通道与 cosine_ran 记进评估。

        这是修复前的失效形态：`_run_dedup` 从不传 q_emb，`_select_candidates`
        在 q_emb 缺失时降级字符重叠（锁内禁远程嵌入），于是配不配 EMBED_API_BASE
        测出来的都是同一件事。
        """
        case = _dedup_case(
            "dd-c1",
            "篮球社招新",
            "篮球社招新",
            same=True,
            item_content="篮球社本周招新，欢迎加入",
            query_content="篮球社周三在体育馆纳新",
        )
        async with _scratch_db():
            with (
                patch(
                    "briefdesk.plugins.dedup.engine.is_embedding_enabled",
                    return_value=True,
                ),
                patch(
                    "briefdesk.plugins.dedup.engine.embed_model_name",
                    return_value="test-model",
                ),
                patch(
                    "briefdesk.plugins.dedup.engine.embed_texts",
                    new=AsyncMock(return_value=[[1.0, 0.0]]),
                ),
                patch(
                    "briefdesk.plugins.dedup.engine.chat",
                    new=AsyncMock(return_value=_resp(True)),
                ),
            ):
                ev = await bench_engine._run_dedup(case)
        assert ev.channel == "cosine", ev.channel
        assert ev.cosine_ran is True
        assert ev.predicted is True
        assert not ev.skipped

    async def test_identical_quote_records_exact_channel(self):
        """原文逐字相同走哈希短路（零 AI）：通道记 exact，不能被算进判官质量。"""
        case = _dedup_case("dd-c0", "篮球社招新", "篮球社招新", same=True)
        async with _scratch_db():
            ev = await bench_engine._run_dedup(case)
        assert ev.channel == "exact"
        assert ev.predicted is True
        assert ev.skipped  # 短路不经判官

    async def test_no_embedding_records_overlap_channel(self):
        """未配置嵌入时如实记 overlap/none，而不是假装走了余弦。"""
        case = _dedup_case("dd-c2", "摄影社招新面试", "摄影社招新面试安排", same=True)
        async with _scratch_db():
            ev = await bench_engine._run_dedup(case)
        assert ev.cosine_ran is False
        assert ev.channel == "overlap"
        # 标题重叠达标 → 送判官；本用例没注册 AI 供应商，判定异常按不重复收口
        assert not ev.skipped

    async def test_zero_overlap_records_none_channel(self):
        case = _dedup_case("dd-c3", "食堂三楼新窗口开业", "求购考研数学复习全书", same=False)
        async with _scratch_db():
            ev = await bench_engine._run_dedup(case)
        assert ev.channel == "none"
        assert ev.skipped


class TestDedupCacheScope:
    """每例只能看见自己的 items（历史上加载全表 → 跨用例污染）。"""

    async def test_previous_case_item_invisible_to_next_case(self):
        """前一例的历史卡片与后一例的 query 逐字相同，后一例也不得命中它。

        这是隔离修复的正面判据：修复前 B 的缓存里有 A 的卡片（同标题、重叠 1.0），
        会把它当成"历史重复卡"送判官；修复后 B 只看得见自己的卡片，
        零重叠 → channel=none、预筛跳过。
        """
        case_a = _dedup_case("dd-A", "篮球社招新", "篮球社周三招新", same=True)
        case_b = _dedup_case("dd-B", "篮球社招新", "食堂三楼新窗口开业", same=False)
        async with _scratch_db():
            ev_a = await bench_engine._run_dedup(case_a)
            ev_b = await bench_engine._run_dedup(case_b)
        assert ev_a.channel == "overlap"
        assert ev_b.channel == "none", "后一例看见了前一例的卡片（缓存作用域失效）"
        assert ev_b.skipped

    async def test_items_recycled_after_case(self):
        """跑完即回收：scratch 库不留残行，否则 load_embeddings 随用例线性变慢。"""
        from briefdesk import db

        case = _dedup_case("dd-rec", "篮球社招新", "篮球社招新", same=True)
        async with _scratch_db():
            with patch(
                "briefdesk.plugins.dedup.engine.chat",
                new=AsyncMock(return_value=_resp(True)),
            ):
                await bench_engine._run_dedup(case)
            conn = await db.get_db()
            cursor = await conn.execute("SELECT COUNT(*) AS n FROM items")
            row = await cursor.fetchone()
            await cursor.close()
        assert row["n"] == 0

    async def test_failed_case_still_recycles(self):
        """判定抛异常也必须回收：删在 finally，否则失败用例把卡片留给下一例。"""
        from briefdesk import db

        case = _dedup_case("dd-boom", "篮球社招新", "篮球社招新", same=True)
        async with _scratch_db():
            # 异常要照常向上抛（由 _run_feature 收成 error 用例），不能被回收逻辑
            # 吞掉；库断言留在这组 with 之外、scratch 仍在的时候
            with (
                patch.object(
                    DedupEngine,
                    "check_dedup",
                    new=AsyncMock(side_effect=RuntimeError("boom")),
                ),
                pytest.raises(RuntimeError),
            ):
                await bench_engine._run_dedup(case)
            conn = await db.get_db()
            cursor = await conn.execute("SELECT COUNT(*) AS n FROM items")
            row = await cursor.fetchone()
            await cursor.close()
        assert row["n"] == 0

    async def test_shared_card_keeps_rows_and_still_isolates(self):
        """两条用例共享同一张历史卡片时：不回收，且双方都看得见它。

        为什么必须有这条：`insert_item` 的唯一键是 (source, source_msg_id)，同键同卡
        是 recorder 导出的常见形态（多条判重记录引用同一张候选卡）。若照旧"跑完就删"，
        先跑完的那条会把两边共用的行删掉，后跑的那条按 id 取回空表、静默判成「无候选」
        ——作用域没错，是被同伴拆了台，正是这次要消灭的那类静默错。
        """
        from briefdesk import db

        owner = _dedup_case("dd-sh-a", "篮球社招新", "篮球社周三招新", same=True)
        borrower = DedupCase(
            id="dd-sh-b",
            items=[
                # 与 owner 的 item 同键同卡
                MessageIn(msg_id="dd-sh-a-a", content="篮球社招新", title="篮球社招新")
            ],
            query=MessageIn(
                msg_id="dd-sh-b-q", content="篮球社周三在体育馆纳新", title="篮球社周三招新"
            ),
            expected=SameExpected(same=True),
        )
        async with _scratch_db():
            # 走整批入口而不是直调 _run_dedup：回收开关由 _run_feature 按整批算，
            # 直调会用参数默认值（True），测不到这条保护
            evals = await bench_engine._run_feature("dedup", [owner, borrower], 1)
            conn = await db.get_db()
            cursor = await conn.execute("SELECT COUNT(*) AS n FROM items")
            row = await cursor.fetchone()
            await cursor.close()
        # 同键同卡 → 库里只有一行，且必须仍在（回收被整批判定关掉了）
        assert row["n"] == 1, "共享卡片被先跑完的一例删掉了"
        assert [e.channel for e in evals] == ["overlap", "overlap"], [
            e.channel for e in evals
        ]

    async def test_run_feature_decides_recycle_for_the_whole_batch(self):
        """回收开关按整批算并下发到每条用例：单条用例看不出自己是不是共享方。"""
        flags: list[bool] = []

        async def _spy(case: DedupCase, *, recycle: bool = True) -> BinaryCaseEval:
            flags.append(recycle)
            return BinaryCaseEval(case_id=case.id, predicted=False, expected=False)

        owner = _dedup_case("wd-a", "篮球社招新", "篮球社周三招新", same=True)
        borrower = DedupCase(
            id="wd-b",
            items=[MessageIn(msg_id="wd-a-a", content="篮球社招新", title="篮球社招新")],
            query=MessageIn(msg_id="wd-b-q", content="求购考研数学书", title="求购考研数学书"),
            expected=SameExpected(same=False),
        )
        unique = _dedup_case("wd-c", "摄影社招新", "摄影社招新面试", same=True)
        with patch.object(bench_engine, "_RUNNERS", {"dedup": _spy}):
            await bench_engine._run_feature("dedup", [owner, borrower], 1)
            assert flags == [False, False]
            flags.clear()
            await bench_engine._run_feature("dedup", [owner, unique], 1)
            assert flags == [True, True]

    def test_shared_dedup_item_keys_scopes_to_cross_case(self):
        keys = shared_dedup_item_keys(
            [
                _dedup_case("x", "篮球社招新", "篮球社周三招新", same=True),
                _dedup_case("y", "摄影社招新", "摄影社招新面试", same=True),
            ]
        )
        assert keys == set()

    def test_conflicting_case_ids_names_both_sides(self):
        """同键不同卡：双方 id 都要列出（网页导出路径按这个集合剔除，不能只点后者）。"""
        owner = _dedup_case("c-own", "篮球社招新", "篮球社周三招新", same=True)
        squatter = DedupCase(
            id="c-squat",
            items=[MessageIn(msg_id="c-own-a", content="别的卡片", title="别的卡片")],
            query=MessageIn(msg_id="c-squat-q", content="查询", title="查询"),
            expected=SameExpected(same=False),
        )
        assert conflicting_dedup_case_ids([owner, squatter]) == {"c-own", "c-squat"}

    async def test_web_export_drops_conflicting_cases(self, tmp_path, caplog):
        """网页导出是逐条容错路径：冲突用例剔除并点名，而不是整轮失败或带错跑。

        带错跑的后果比跳过更坏——冲突例的 channel 照样正常记 overlap/cosine，
        报告里看不出那条结果其实来自别人的卡片。
        """
        cases = [
            {
                "id": "w-1",
                "items": [{"msg_id": "shared", "content": "篮球社招新", "title": "篮球社招新"}],
                "query": {"msg_id": "w-1-q", "content": "篮球社周三招新", "title": "篮球社周三招新"},
                "expected": {"same": True},
            },
            {
                "id": "w-2",
                # 同 (source, msg_id) 却声明了另一张卡片
                "items": [{"msg_id": "shared", "content": "摄影社招新", "title": "摄影社招新"}],
                "query": {"msg_id": "w-2-q", "content": "别的查询", "title": "别的查询"},
                "expected": {"same": False},
            },
            {
                "id": "w-3",
                "items": [{"msg_id": "solo", "content": "二手自行车", "title": "二手自行车"}],
                "query": {"msg_id": "w-3-q", "content": "自行车转让", "title": "自行车转让"},
                "expected": {"same": True},
            },
        ]
        path = tmp_path / "dedup.fromweb.json"
        path.write_text(
            json.dumps({"feature": "dedup", "cases": cases}, ensure_ascii=False),
            encoding="utf-8",
        )
        caplog.set_level(logging.WARNING, logger="briefdesk.plugins.benchmark.engine")
        loaded = await bench_engine.load_web_cases("dedup", tmp_path)
        assert [c.id for c in loaded] == ["w-3"], "冲突用例应被整对剔除，只留无冲突的 w-3"
        joined = "\n".join(r.message for r in caplog.records)
        assert "同键不同卡" in joined and "w-1" in joined and "w-2" in joined


    async def test_scope_hides_other_rows_already_in_db(self):
        """残行仍在库里时，可见集合也只由构造期传入的 id 决定。

        与上一条的分工要说清：上一条测「跑完即回收」，串行场景里回收本身就把
        污染消除了——所以它**测不到作用域**（把接缝改成返回全量，那条照样绿）。
        这条直接对着"库里已有别人的卡片"构造引擎，钉的是并发窗口或上一例异常
        未清干净时仍然成立的那半条保证。
        """
        from briefdesk import db

        def _row(title: str) -> dict:
            return {
                "category": "活动通知",
                "title": title,
                "key_info": None,
                "sender_name": None,
                "source_quote": title,
                "source_group": "g",
                "subject": None,
                "source": "bench",
                "source_msg_id": title,
                "session_id": "s",
                "msg_time": 0,
                "is_verified": 0,
                "content_hash": None,
                "image_urls": "",
            }

        async with _scratch_db():
            foreign = await db.insert_item(_row("篮球社招新"))
            mine = await db.insert_item(_row("摄影社周五招新面试"))
            engine = bench_engine._ProbeDedupEngine([mine])
            await engine.ensure_cache()
            assert [it.id for it in engine._cache] == [mine]
            assert foreign != mine
            # 反向自证：不加作用域时两条都看得见（说明库里确实有那行残卡）
            unscoped = DedupEngine()
            await unscoped.ensure_cache()
            assert {it.id for it in unscoped._cache} == {mine, foreign}


class TestCacheRowsSeam:
    """生产取数接缝的默认实现必须与接缝引入前完全一致。"""

    async def test_default_seam_loads_all_rows(self):
        from briefdesk import db

        async with _scratch_db():
            for title in ("甲卡片", "乙卡片"):
                await db.insert_item(
                    {
                        "category": "活动通知",
                        "title": title,
                        "key_info": None,
                        "sender_name": None,
                        "source_quote": title,
                        "source_group": "g",
                        "subject": None,
                        "source": "bench",
                        "source_msg_id": title,
                        "session_id": "s",
                        "msg_time": 0,
                        "is_verified": 0,
                        "content_hash": None,
                        "image_urls": "",
                    }
                )
            engine = DedupEngine()
            rows = await engine._load_cache_rows()
            assert [r["title"] for r in rows] == ["甲卡片", "乙卡片"]


class TestDedupAggregate:
    def test_channel_and_cost_counts(self):
        evals = [
            BinaryCaseEval(case_id="a", predicted=True, expected=True, channel="cosine", cosine_ran=True),
            BinaryCaseEval(case_id="b", predicted=False, expected=False, skipped=True, channel="none"),
            BinaryCaseEval(case_id="c", predicted=False, expected=True, skipped=True, channel="overlap"),
            BinaryCaseEval(case_id="d", predicted=False, expected=False, error="boom"),
            BinaryCaseEval(case_id="e", predicted=True, expected=False, skipped=True, channel="exact"),
        ]
        s = aggregate_dedup(evals)
        assert s["channel_cosine"] == 1
        assert s["channel_none"] == 1
        assert s["channel_overlap"] == 1
        # 出错用例（d）不进通道/成本统计：它的 channel 是默认空串，不代表任何判定
        assert s["channel_exact"] == 1
        assert s["cosine_cases"] == 1
        # AI 判定占比只数真正经判官裁决的用例：exact 短路（e）与两个预筛跳过
        # （b/c）都不调用判官。把短路算进成本会把阈值代价读高。
        assert s["ai_judged_cases"] == 1
        assert s["ai_judged_rate"] == 1 / 4

    def test_merge_summary_has_no_channel_keys(self):
        """merge 无通道概念，不能被 dedup 的聚合污染出一堆恒为 0 的键。"""
        s = bench_engine._aggregate(
            "merge", [BinaryCaseEval(case_id="m", predicted=True, expected=True)]
        )
        assert "channel_cosine" not in s
        assert "ai_judged_cases" not in s


class TestClassifyModeAndPlaceholder:
    def test_mode_resolution_priority(self):
        def _case(**kw):
            return ClassifyCase(
                id="c1",
                messages=[MessageIn(msg_id="m1", content="周三下午3点讲座")],
                **kw,
            )

        assert _case(mode="prod", note="mode=oov").resolved_mode() == "prod"
        assert _case(note="sample_id=542；mode=fictional；碎片:1").resolved_mode() == "fictional"
        assert _case(note="sample_id=0；real；src=123").resolved_mode() == "real"
        assert _case(note="随手写的说明").resolved_mode() == "unspecified"
        # real 必须是独立 token：含 "real" 子串的别的词（如 realistically）不得误判
        assert _case(note="mode=realistic 生成").resolved_mode() == "realistic"

    def test_placeholder_only_detection(self):
        assert ClassifyCase(
            id="c", messages=[MessageIn(msg_id="m1", content="[图片]")]
        ).placeholder_only()
        # 混合批不算：还有一条有正文，分类该做还是得做
        assert not ClassifyCase(
            id="c",
            messages=[
                MessageIn(msg_id="m1", content="[图片]"),
                MessageIn(msg_id="m2", content="周三下午3点讲座"),
            ],
        ).placeholder_only()

    def test_aggregate_excludes_placeholder_and_keeps_identity(self):
        scored = ClassifyCaseEval(
            case_id="ok",
            messages_total=1,
            mode="prod",
            expected_count=2,
            hits=1,
            model_count=2,
            false_positives=1,
        )
        placeholder = ClassifyCaseEval(
            case_id="ph",
            messages_total=1,
            mode="real",
            placeholder_only=True,
            expected_count=1,
            hits=0,
            model_count=0,
        )
        broken = ClassifyCaseEval(case_id="err", messages_total=1, error="boom", expected_count=9)
        s = aggregate_classify([scored, placeholder, broken])
        assert s["expected"] == 2  # 占位符与出错用例都不进主分母
        assert s["category_recall"] == 0.5
        assert s["placeholder_only_cases"] == 1
        assert s["placeholder_only_expected"] == 1
        assert s["category_recall_incl_placeholder"] == 1 / 3
        # 自证恒等式：切片覆盖全部主分母用例，占位符单独补齐
        assert sum(v["expected"] for v in s["by_mode"].values()) == s["expected"]
        assert (
            s["expected"] + s["placeholder_only_expected"]
            == s["expected_incl_placeholder"]
        )

    def test_false_positives_split_by_case_kind(self):
        """误报按用例形态拆两半：全噪声用例里的误报与部分期望用例里的不同因。"""
        noise = ClassifyCaseEval(
            case_id="noise", expected_count=0, hits=0, model_count=3, false_positives=3
        )
        partial = ClassifyCaseEval(
            case_id="partial", expected_count=1, hits=1, model_count=2, false_positives=1
        )
        s = aggregate_classify([noise, partial])
        assert s["false_positives"] == 4
        assert s["false_positives_noise"] == 3
        assert s["false_positives_partial"] == 1


def _classify_case(cid: str, ts: str, expected_start: str, *, mode_note: str = "") -> ClassifyCase:
    return ClassifyCase(
        id=cid,
        note=mode_note,
        messages=[MessageIn(msg_id=f"{cid}-1", content="周三下午3点讲座", timestamp=ts)],
        expected=[ClassifyExpected(index=0, category="活动通知", start=expected_start)],
    )


class TestTimeEchoMetrics:
    def test_echo_by_granularity(self):
        ts = int(time.mktime(time.strptime("2026-04-20 19:00", "%Y-%m-%d %H:%M")))
        assert bench_metrics._is_echo("2026-04-20 19:00", ts)
        assert bench_metrics._is_echo("2026-04-20", ts)  # date-only 比到日
        assert not bench_metrics._is_echo("2026-04-20 20:00", ts)
        assert not bench_metrics._is_echo("2026-04-21", ts)

    def test_zero_timestamp_is_not_echo(self):
        """非法/缺省时间戳不得当锚点：否则 1970-01-01 会把真抽取点误剔成回声。"""
        assert not bench_metrics._is_echo("1970-01-01", 0)
        assert not bench_metrics._is_echo("2026-04-20", -1)

    def test_seconds_bearing_point_is_not_echo(self):
        """带秒的期望点必须比到秒：截到分钟会把「19:00:30」当成发送时间误剔。

        带秒的期望是一次真实的精确抽取（正文里写了秒），把它算成回声等于奖励
        标注粗糙——精度越高的标注反而越容易被静默剔除分母。
        """
        ts = int(time.mktime(time.strptime("2026-04-20 19:00:00", "%Y-%m-%d %H:%M:%S")))
        assert not bench_metrics._is_echo("2026-04-20 19:00:30", ts)
        # 同一时刻不带秒 → 仍是回声
        assert bench_metrics._is_echo("2026-04-20 19:00", ts)

    def test_non_standard_shape_is_not_echo(self):
        """三种标准形态之外的写法不算回声（宁可留在分母当漏检，也不静默剔除）。"""
        ts = int(time.mktime(time.strptime("2026-04-20 19:00", "%Y-%m-%d %H:%M")))
        assert not bench_metrics._is_echo("2026/04/20 19:00", ts)
        assert not bench_metrics._is_echo("20260420", ts)
        assert not bench_metrics._is_echo("", ts)

    def test_date_only_point_not_echo_when_only_clock_matches(self):
        """跨零点的消息：按本地日期比较，不能按 UTC（差 8 小时会判反）。"""
        # 2026-04-20 00:30 本地 → UTC 仍是 04-19，date-only 期望必须按本地判
        ts = int(time.mktime(time.strptime("2026-04-20 00:30", "%Y-%m-%d %H:%M")))
        assert bench_metrics._is_echo("2026-04-20", ts)

    def test_eval_counts_echo_ex_echo_and_granularity(self):
        ts = int(time.mktime(time.strptime("2026-04-20 19:00", "%Y-%m-%d %H:%M")))
        case = ClassifyCase(
            id="c",
            messages=[
                MessageIn(msg_id="m1", content="讲座", timestamp=ts),
                MessageIn(msg_id="m2", content="讲座", timestamp=ts),
            ],
            expected=[
                # 回声点（date-only 与带时刻各一个）
                ClassifyExpected(index=0, category="活动通知", start="2026-04-20"),
                ClassifyExpected(index=1, category="活动通知", start="2026-04-20 19:00"),
            ],
        )
        outcome = ClassifyOutcome(
            results=[
                ClassifyResult(msg_index=0, category="活动通知", start="2026-04-20"),
                ClassifyResult(msg_index=1, category="活动通知", start="2026-04-20 19:00"),
            ],
            failed=[],
        )
        ev = evaluate_classify_case(case, outcome)
        assert ev.time_points_expected == 2
        assert ev.time_points_echo == 2
        assert ev.time_points_ex_echo == 0

    def test_non_echo_points_survive_exclusion(self):
        case = ClassifyCase(
            id="c",
            messages=[MessageIn(msg_id="m1", content="讲座", timestamp="2026-04-20 19:00")],
            expected=[
                ClassifyExpected(
                    index=0,
                    category="活动通知",
                    start="2026-05-01 10:00",
                    times=[TimePoint(type="end", time="2026-05-02")],
                )
            ],
        )
        outcome = ClassifyOutcome(
            results=[
                ClassifyResult(
                    msg_index=0,
                    category="活动通知",
                    start="2026-05-01 10:00",
                    extra_times=[{"type": "end", "time": "2026-05-02", "label": ""}],
                )
            ],
            failed=[],
        )
        s = aggregate_classify([evaluate_classify_case(case, outcome)])
        assert s["time_points_echo"] == 0
        assert s["time_point_recall_ex_echo"] == 1.0
        # 带时刻与 date-only 分开：两者难度不同，混在一起看不出解析器弱在哪
        assert s["time_point_recall_clock_ex_echo"] == 1.0
        assert s["time_point_recall_date_ex_echo"] == 1.0


class TestProviderLabel:
    def test_enum_labels(self):
        f = bench_engine.provider_label
        assert f("http://localhost:11434/v1/") == "ollama"
        assert f("http://192.168.1.9:11434/v1") == "ollama"
        assert f("https://ollama.example.com/v1") == "ollama"
        assert f("https://api.deepseek.com/v1") == "deepseek"
        assert f("https://dashscope.aliyuncs.com/compatible-mode/v1") == "custom"
        assert f("") == ""

    def test_unparseable_base_degrades_to_custom(self):
        """畸形地址归 custom 并继续跑，绝不让整轮基准因为一行配置失败。"""
        f = bench_engine.provider_label
        # 无 scheme：urlsplit 把整串当 scheme、port 取不到 → 不猜、归 custom
        assert f("localhost:11434") == "custom"
        assert f("http://host:99999/v1") == "custom"

    def test_snapshot_has_no_endpoint_leak(self):
        """快照只落枚举：完整 URL 含主机名，报告 JSON 会长期落盘、也可能被贴进 issue。"""
        with (
            patch("briefdesk.plugins.benchmark.engine.config") as cfg,
        ):
            cfg.ai_api_base = "https://api.deepseek.com"
            cfg.embed_api_base = "http://localhost:11434/v1/"
            cfg.ai_max_concurrency = 1
            cfg.ai_reasoning_effort = "off"
            cfg.ai_json_mode = "auto"
            cfg.max_classify_tokens = 4096
            cfg.ai_vision_enabled = False
            cfg.ai_vision_max_images = 4
            cfg.embed_model = "bge-m3"
            cfg.embed_batch_size = 10
            cfg.dedup_similarity_threshold = 0.07
            cfg.dedup_embed_threshold = 0.8
            cfg.dedup_embed_top_k = 3
            cfg.dedup_embed_fallback_threshold = 0.65
            cfg.dedup_strong_threshold = 0.99
            cfg.merge_prefilter_threshold = -1.0
            snap = bench_engine.benchmark_config_snapshot()
        assert snap["chat_provider"] == "deepseek"
        assert snap["embed_provider"] == "ollama"
        assert "://" not in json.dumps(snap, ensure_ascii=False)


class TestDedupItemKeyConflicts:
    """同键不同卡会让缓存作用域指向别人的卡片，装载期就得拒绝。"""

    def _dataset(self, cases: list[dict]) -> DatasetFile:
        return DatasetFile(feature="dedup", cases=cases)

    def _raw(self, cid: str, msg_id: str, title: str) -> dict:
        return {
            "id": cid,
            "items": [
                {
                    "msg_id": msg_id,
                    "content": title,
                    "title": title,
                    "source": "bench",
                }
            ],
            "query": {"msg_id": f"{cid}-q", "content": "查询", "title": "查询"},
            "expected": {"same": False},
        }

    def test_cross_case_same_key_different_card_rejected(self):
        with pytest.raises(DatasetError) as ctx:
            parse_cases(self._dataset([self._raw("a", "i1", "篮球社招新"), self._raw("b", "i1", "摄影社招新")]))
        assert "同键不同卡" in str(ctx.value)

    def test_cross_case_same_key_same_card_allowed(self):
        """共享同一张历史卡片是合法设计（多例引用同一候选），不得误报。"""
        cases = parse_cases(
            self._dataset([self._raw("a", "i1", "篮球社招新"), self._raw("b", "i1", "篮球社招新")])
        )
        assert dedup_item_key_conflicts(cases) == []

    def test_within_case_duplicate_key_rejected(self):
        raw = self._raw("a", "i1", "篮球社招新")
        raw["items"].append({"msg_id": "i1", "content": "摄影社招新", "title": "摄影社招新", "source": "bench"})
        with pytest.raises(DatasetError):
            parse_cases(self._dataset([raw]))


class TestReportBackwardCompatibility:
    """旧 payload（无本轮新增键）必须仍能渲染：历史报告长期可读。"""

    def _legacy_payload(self) -> dict:
        return {
            "run_id": "20260822-100657",
            "generated_at": "2026-08-22 10:06:57",
            "model": "old-model",
            "concurrency": 1,
            "config": {"embedding_enabled": False, "dedup_similarity_threshold": 0.3},
            "elapsed_sec": 10.0,
            "features": {
                "classify": {
                    "dataset": "cases/classify.json",
                    "elapsed_sec": 5.0,
                    "summary": {
                        "cases": 2,
                        "error_cases": 0,
                        "expected": 3,
                        "model_classified": 3,
                        "category_accuracy": 0.5,
                        "category_precision": 0.5,
                        "category_recall": 0.5,
                        "category_f1": 0.5,
                        "false_positives": 1,
                        "time_msg_accuracy": 0.0,
                        "time_point_recall": 0.0,
                        "time_point_precision": 0.0,
                        "failure_rate": 0.0,
                        "summary_fill_rate": 1.0,
                    },
                    "cases": [
                        {
                            "case_id": "cls-1",
                            "messages_total": 1,
                            "expected_count": 1,
                            "hits": 1,
                            "model_count": 1,
                            "false_positives": 0,
                            "time_msgs_expected": 0,
                            "time_msgs_ok": 0,
                            "failed": 0,
                            "error": None,
                        }
                    ],
                },
                "dedup": {
                    "dataset": "cases/dedup.json",
                    "elapsed_sec": 2.0,
                    "summary": {
                        "cases": 1,
                        "error_cases": 0,
                        "accuracy": 1.0,
                        "precision": 1.0,
                        "recall": 1.0,
                        "f1": 1.0,
                        "tp": 1,
                        "fp": 0,
                        "tn": 0,
                        "fn": 0,
                        "skipped": 0,
                    },
                    "cases": [
                        {"case_id": "dd-1", "predicted": True, "expected": True, "skipped": False, "error": None}
                    ],
                },
                "title": {
                    "dataset": "cases/title.json",
                    "elapsed_sec": 1.0,
                    "summary": {
                        "cases": 1,
                        "error_cases": 0,
                        "keyword_hit_rate": 1.0,
                        "keyword_hit_cases": 1,
                        "avg_len": 8.0,
                        "too_long_count": 0,
                        "fallback_count": 0,
                    },
                    "cases": [
                        {
                            "case_id": "tt-1",
                            "output": "摄影社招新面试",
                            "expected_title": None,
                            "expected_keywords": ["摄影社"],
                            "error": None,
                        }
                    ],
                },
            },
        }

    def test_html_report_renders_legacy_payload(self):
        from briefdesk.plugins.benchmark.html_report import build_html_report

        text = build_html_report(self._legacy_payload())
        assert "</html>" in text
        # 缺字段显示占位符而不是抛错，也不得把旧报告渲染成新口径
        assert "对话来源" in text
        assert "判定通道" not in text

    def test_terminal_metrics_render_legacy_summary(self):
        from briefdesk.plugins.benchmark.report import render_metrics

        payload = self._legacy_payload()
        for feature in ("classify", "dedup", "title"):
            lines = render_metrics(feature, payload["features"][feature]["summary"])
            assert lines, feature

    def test_terminal_missing_metric_renders_dash_not_zero(self):
        """旧 payload 缺的指标显示「-」，不能被渲染成 0.0%。

        为什么必须区分：`time_point_recall_ex_echo` 是本轮新增键，旧报告里根本
        没有这一列。补 0 会把它显示成"剔回声召回 0.0%"，读者据此得出"旧版本
        时间抽取完全失败"的错误结论——而真实原因是那一轮没测这个口径。
        """
        from briefdesk.plugins.benchmark.report import render_metrics

        payload = self._legacy_payload()
        text = "\n".join(render_metrics("classify", payload["features"]["classify"]["summary"]))
        assert "时间点召回率(剔回声)" in text
        for line in text.splitlines():
            if "时间点召回率(剔回声)" in line:
                assert line.rstrip().endswith("-"), line
                assert "0.0%" not in line, line
        # 有键且真的是 0 的指标仍显示 0.0%：占位符不能吃掉真实零值
        for line in text.splitlines():
            if "时间点完全一致率" in line:
                assert line.rstrip().endswith("0.0%"), line

    def test_html_missing_metric_renders_dash_not_zero(self):
        """HTML 主表与终端同口径：缺键显示「-」，真实零值仍是 0.0%。"""
        from briefdesk.plugins.benchmark.html_report import _metric_table

        payload = self._legacy_payload()
        table = _metric_table("classify", payload["features"]["classify"]["summary"])
        assert "<td>-</td>" in table
        rows = table.split("<tr>")
        echo_row = next(r for r in rows if "时间点召回率(剔回声)" in r)
        assert "<td>-</td>" in echo_row, echo_row
        assert "0.0%" not in echo_row, echo_row
        exact_row = next(r for r in rows if "时间点完全一致率" in r)
        assert "0.0%" in exact_row, exact_row

    def test_format_metric_none_is_dash_for_rate_keys(self):
        """比例键的 None 也必须走「-」：比例分支里的 `value or 0` 会吞掉 None。"""
        from briefdesk.plugins.benchmark.report import format_metric

        for key in ("time_point_recall_ex_echo", "char_similarity_mean", "accuracy", "ai_judged_rate"):
            assert format_metric(key, None) == "-", key
        # 真实零值照旧是 0.0%，不被当成缺失
        assert format_metric("accuracy", 0.0) == "0.0%"
        assert format_metric("char_similarity_mean", 0) == "0.0%"

    def test_terminal_extra_line_shows_dash_for_missing_split_key(self):
        """补充行里的拆分键缺失时也给「-」：父键存在不代表拆分键存在。

        场景：`time_points_echo` 是更早的键，旧 payload 有它但没有本轮的
        `time_point_recall_date_ex_echo` / `_clock_ex_echo`。补 0 会把两个
        口径都印成 0.0%，读起来像"两类时点全错"，实际是没记录。
        """
        from briefdesk.plugins.benchmark.report import render_metrics

        summary = {
            "cases": 1,
            "error_cases": 0,
            "time_points_echo": 3,
            "time_point_recall_ex_echo": 0.5,
        }
        text = "\n".join(render_metrics("classify", summary))
        line = next(ln for ln in text.splitlines() if "时间点回声" in ln)
        assert "剔回声召回 50.0%" in line, line
        assert "date-only -" in line, line
        assert "带时刻 -" in line, line

    def test_html_extra_line_shows_dash_for_missing_split_key(self):
        """HTML 补充行与终端同口径：缺拆分键显示「-」。"""
        from briefdesk.plugins.benchmark.html_report import _metric_table

        summary = {"cases": 1, "error_cases": 0, "time_points_echo": 3, "time_point_recall_ex_echo": 0.5}
        table = _metric_table("classify", summary)
        assert "date-only -" in table, table

    def test_html_chart_skips_missing_metric_instead_of_zero_bar(self):
        """图表里缺的指标直接不画：补 0 会得到一根标着 0.0% 的条。"""
        from briefdesk.plugins.benchmark.html_report import _feature_charts

        svg = _feature_charts("classify", {"category_accuracy": 0.5, "cases": 1})
        assert "类别准确率" in svg
        assert "时间点召回率(剔回声)" not in svg

    def test_title_verdict_keeps_legacy_exact_match_as_soft_pass(self):
        """旧 payload 没有 char_similarity：逐字相同不能被重渲染成失败。

        `char_similarity` 是本轮新增的逐例派生分，历史报告里没有这个键。旧口径
        下「仅标题期望 + 逐字相同」是通过的；新代码若只看相似度就会把它判成 ✘，
        等于用新标准改写历史结论。回退只对缺键的历史 payload 生效。
        """
        from briefdesk.plugins.benchmark.html_report import _title_ok, _verdict

        legacy_exact = {
            "case_id": "tt-legacy",
            "output": "摄影社招新面试",
            "expected_title": "摄影社招新面试",
            "expected_keywords": None,
            "error": None,
        }
        hard, soft = _title_ok(legacy_exact)
        assert hard is False
        assert soft is True
        assert 'class="warn"' in _verdict("title", legacy_exact)

        # 新 payload 同一个用例带相似度键：走相似度判定，不被旧回退短路
        new_form = dict(legacy_exact, char_similarity=0.95)
        assert _title_ok(new_form) == (False, True)
        low_sim = dict(legacy_exact, char_similarity=0.10)
        assert _title_ok(low_sim) == (False, False)
        # 旧 payload 但逐字不同：仍判失败，回退不能放宽成无条件通过
        mismatch = dict(legacy_exact, output="篮球社招新")
        assert _title_ok(mismatch) == (False, False)

    def test_html_chart_treats_null_value_as_missing(self):
        """键存在但值为 null 与缺键同义：主表显示「-」，图表也不能画 0.0%。

        JSON 里的 `null` 反序列化成 None，是旧报告表达"未记录"的另一种形态。
        只判 `key in summary` 会让同一份报告里主表写「-」、图表画零长条。
        """
        from briefdesk.plugins.benchmark.html_report import (
            _feature_charts,
            _metric_table,
        )

        summary = {"accuracy": 0.5, "precision": 0.5, "recall": 0.5, "f1": 0.5, "ai_judged_rate": None}
        svg = _feature_charts("dedup", summary)
        # 只看条形图那一段：混淆矩阵在同一段 HTML 里，它的百分比与本题无关
        bar = svg.split("</svg>")[0]
        assert "判官裁决用例占比" not in bar, bar
        # 精确匹配独立出现的 ">0.0%<"：50.0% 里也含 "0.0%" 子串，不能用 in
        assert ">0.0%<" not in bar, bar
        # 主表同输入显示「-」：两处口径一致
        table = _metric_table("dedup", summary)
        assert "<td>-</td>" in table, table

    def test_terminal_case_lines_render_legacy_evals(self):
        """旧 payload 的逐用例 dict 走 HTML 渲染路径不得抛 KeyError。"""
        from briefdesk.plugins.benchmark.html_report import _case_rows

        payload = self._legacy_payload()
        rows = _case_rows("dedup", payload["features"]["dedup"]["cases"])
        assert "dd-1" in rows
        assert "通道" not in rows  # 无通道数据时不加空列
        rows = _case_rows("title", payload["features"]["title"]["cases"])
        assert "tt-1" in rows
