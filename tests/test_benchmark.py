"""基准测试插件单元测试（不调用 AI，不触碰应用库）。

覆盖：测试集 schema 校验、指标计算、dry-run 文件解析、基准环境的
临时数据库隔离（补丁式）与 dedup 预筛跳过路径（离线可测部分）。
"""

import contextlib
import io
import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from briefdesk.config import config
from briefdesk.plugins.benchmark import cli, engine
from briefdesk.plugins.benchmark.metrics import (
    BinaryCaseEval,
    ClassifyCaseEval,
    TitleCaseEval,
    aggregate_binary,
    aggregate_classify,
    aggregate_title,
    evaluate_classify_case,
)
from briefdesk.plugins.benchmark.providers import bench_environment
from briefdesk.plugins.benchmark.schema import (
    FEATURES,
    CategoryDef,
    ClassifyCase,
    ClassifyExpected,
    DatasetError,
    DatasetFile,
    DedupCase,
    MessageIn,
    SameExpected,
    TimePoint,
    TitleExpected,
    card_fields,
    load_dataset_file,
    parse_cases,
)
from briefdesk.types import ClassifyOutcome, ClassifyResult

CASES_DIR = (
    Path(__file__).resolve().parents[1] / "briefdesk" / "plugins" / "benchmark" / "cases"
)


class MessageInTest(unittest.TestCase):
    def test_timestamp_string_converted_to_epoch(self):
        msg = MessageIn(msg_id="m1", content="活动", timestamp="2026-04-05 14:30")
        expected = int(time.mktime(time.strptime("2026-04-05 14:30", "%Y-%m-%d %H:%M")))
        self.assertEqual(msg.to_internal().timestamp, expected)

    def test_timestamp_date_only(self):
        msg = MessageIn(msg_id="m1", content="活动", timestamp="2026-04-05")
        expected = int(time.mktime(time.strptime("2026-04-05", "%Y-%m-%d")))
        self.assertEqual(msg.to_internal().timestamp, expected)

    def test_timestamp_int_passthrough(self):
        msg = MessageIn(msg_id="m1", content="活动", timestamp=12345)
        self.assertEqual(msg.to_internal().timestamp, 12345)

    def test_bad_timestamp_rejected(self):
        with self.assertRaises(ValidationError):
            MessageIn(msg_id="m1", content="活动", timestamp="下周三")

    def test_required_fields(self):
        with self.assertRaises(ValidationError):
            MessageIn(content="没有 msg_id")

    def test_content_masked_on_convert(self):
        # InternalMessage 构造即脱敏：手机号不应出现在转换结果中
        msg = MessageIn(msg_id="m1", content="联系我 13800138000", sender_name="张三")
        internal = msg.to_internal()
        self.assertNotIn("13800138000", internal.content)

    def test_card_fields_fallback_title(self):
        msg = MessageIn(msg_id="m1", content="摄影社招新面试，周三下午3点，体育馆")
        title, desc = card_fields(msg)
        self.assertEqual(title, msg.content)
        self.assertEqual(desc, msg.content)

    def test_card_fields_explicit_title(self):
        msg = MessageIn(msg_id="m1", content="正文", title="摄影社招新面试")
        title, _ = card_fields(msg)
        self.assertEqual(title, "摄影社招新面试")


class ParseCasesTest(unittest.TestCase):
    def test_classify_case_valid(self):
        ds = DatasetFile(
            feature="classify",
            cases=[
                {
                    "id": "c1",
                    "messages": [{"msg_id": "m1", "content": "活动"}],
                    "expected": [{"index": 0, "category": "活动通知"}],
                }
            ],
        )
        cases = parse_cases(ds)
        self.assertEqual(len(cases), 1)
        self.assertIsInstance(cases[0], ClassifyCase)

    def test_duplicate_case_id_rejected(self):
        ds = DatasetFile(
            feature="merge",
            cases=[
                {
                    "id": "c1",
                    "head": {"msg_id": "h", "content": "a"},
                    "tail": {"msg_id": "t", "content": "b"},
                    "expected": {"merge": True},
                },
                {
                    "id": "c1",
                    "head": {"msg_id": "h2", "content": "a"},
                    "tail": {"msg_id": "t2", "content": "b"},
                    "expected": {"merge": False},
                },
            ],
        )
        with self.assertRaises(DatasetError) as ctx:
            parse_cases(ds)
        self.assertIn("id 重复", str(ctx.exception))

    def test_classify_duplicate_expected_index_rejected(self):
        ds = DatasetFile(
            feature="classify",
            cases=[
                {
                    "id": "c1",
                    "messages": [
                        {"msg_id": "m1", "content": "a"},
                        {"msg_id": "m2", "content": "b"},
                    ],
                    "expected": [
                        {"index": 0, "category": "活动通知"},
                        {"index": 0, "category": "学术"},
                    ],
                }
            ],
        )
        with self.assertRaises(DatasetError) as ctx:
            parse_cases(ds)
        self.assertIn("index 重复", str(ctx.exception))

    def test_case_validation_errors_collected(self):
        ds = DatasetFile(
            feature="dedup",
            cases=[
                {"id": "ok", "items": [{"msg_id": "i", "content": "a"}], "query": {"msg_id": "q", "content": "b"}, "expected": {"same": False}},
                {"items": []},  # 缺 id、items 为空、缺 query/expected
                {"id": "bad2", "query": {"msg_id": "q", "content": "b"}, "expected": {"same": False}},  # 缺 items
            ],
        )
        with self.assertRaises(DatasetError) as ctx:
            parse_cases(ds)
        self.assertIn("cases[1]", str(ctx.exception))
        self.assertIn("cases[2]", str(ctx.exception))

    def test_title_expected_needs_title_or_keywords(self):
        with self.assertRaises(ValidationError):
            TitleExpected()
        self.assertIsNotNone(TitleExpected(title="标题"))
        self.assertIsNotNone(TitleExpected(keywords=["a"]))

    def test_categories_only_for_classify(self):
        with self.assertRaises(ValidationError):
            DatasetFile(feature="dedup", cases=[], categories=[CategoryDef(name="x")])

    def test_dedup_query_required(self):
        ds = DatasetFile(
            feature="dedup",
            cases=[{"id": "c1", "items": [{"msg_id": "i", "content": "a"}], "expected": {"same": False}}],
        )
        with self.assertRaises(DatasetError):
            parse_cases(ds)


class DatasetFileTest(unittest.TestCase):
    def test_example_datasets_load(self):
        for feature in FEATURES:
            path = CASES_DIR / f"{feature}.example.json"
            ds = load_dataset_file(path)
            self.assertEqual(ds.feature, feature)
            cases = parse_cases(ds)
            self.assertGreater(len(cases), 0, feature)

    def test_missing_file(self):
        with self.assertRaises(FileNotFoundError):
            load_dataset_file(CASES_DIR / "不存在.json")


class BinaryMetricsTest(unittest.TestCase):
    def test_binary_metrics_math(self):
        evals = [
            BinaryCaseEval(case_id="a", predicted=True, expected=True),   # TP
            BinaryCaseEval(case_id="b", predicted=False, expected=True),  # FN
            BinaryCaseEval(case_id="c", predicted=True, expected=False),  # FP
            BinaryCaseEval(case_id="d", predicted=False, expected=False),  # TN
        ]
        s = aggregate_binary(evals)
        self.assertEqual((s["tp"], s["fp"], s["tn"], s["fn"]), (1, 1, 1, 1))
        self.assertEqual(s["accuracy"], 0.5)
        self.assertEqual(s["precision"], 0.5)
        self.assertEqual(s["recall"], 0.5)
        self.assertEqual(s["f1"], 0.5)

    def test_binary_metrics_all_correct(self):
        s = aggregate_binary(
            [BinaryCaseEval(case_id="a", predicted=True, expected=True),
             BinaryCaseEval(case_id="b", predicted=False, expected=False)]
        )
        self.assertEqual(s["accuracy"], 1.0)
        self.assertEqual(s["precision"], 1.0)

    def test_binary_metrics_empty(self):
        s = aggregate_binary([])
        self.assertEqual(s["accuracy"], 0.0)
        self.assertEqual(s["f1"], 0.0)

    def test_skipped_and_error_counted(self):
        s = aggregate_binary(
            [
                BinaryCaseEval(case_id="a", predicted=False, expected=False, skipped=True),
                BinaryCaseEval(case_id="b", predicted=True, expected=True, error="boom"),
            ]
        )
        self.assertEqual(s["skipped"], 1)
        self.assertEqual(s["error_cases"], 1)
        self.assertEqual(s["cases"], 2)


class ClassifyMetricsTest(unittest.TestCase):
    def _case(self) -> ClassifyCase:
        return ClassifyCase(
            id="c1",
            messages=[
                MessageIn(msg_id="m0", content="活动"),
                MessageIn(msg_id="m1", content="闲聊"),
                MessageIn(msg_id="m2", content="讲座"),
            ],
            expected=[
                ClassifyExpected(index=0, category="活动通知", start="2026-04-20 19:00"),
                ClassifyExpected(
                    index=2,
                    category="学术",
                    end="2026-04-15",
                    times=[TimePoint(type="end", time="2026-04-16", label="补交")],
                ),
            ],
        )

    def test_evaluate_classify_case(self):
        case = self._case()
        outcome = ClassifyOutcome(
            results=[
                ClassifyResult(msg_index=0, category="活动通知", start="2026-04-20 19:00", summary="十佳歌手"),
                ClassifyResult(
                    msg_index=2,
                    category="学术",
                    end="2026-04-15",
                    extra_times=[{"type": "end", "time": "2026-04-16", "label": "补交"}],
                    summary="高数考试",
                ),
                ClassifyResult(msg_index=1, category="活动通知"),  # 误报：闲聊被分类
            ],
            failed=[3],
            time_indexes=[0, 2],
        )
        ev = evaluate_classify_case(case, outcome)
        self.assertEqual(ev.hits, 2)
        self.assertEqual(ev.expected_count, 2)
        self.assertEqual(ev.model_count, 3)
        self.assertEqual(ev.false_positives, 1)
        self.assertEqual(ev.time_msgs_expected, 2)
        self.assertEqual(ev.time_msgs_ok, 2)
        self.assertEqual(ev.time_points_expected, 3)
        self.assertEqual(ev.time_points_ok, 3)
        self.assertEqual(ev.time_points_model, 3)
        self.assertEqual(ev.failed, 1)
        self.assertEqual(ev.summary_filled, 2)

    def test_evaluate_miss_and_time_mismatch(self):
        case = self._case()
        outcome = ClassifyOutcome(
            results=[
                # index 0 缺分类（漏检）；index 2 类别错 + 时间点缺失
                ClassifyResult(msg_index=2, category="活动通知", end="2026-04-15"),
            ],
            failed=[],
        )
        ev = evaluate_classify_case(case, outcome)
        self.assertEqual(ev.hits, 0)
        self.assertEqual(ev.model_count, 1)
        self.assertEqual(ev.false_positives, 0)  # index 2 在期望集合内，只是类别错
        self.assertEqual(ev.time_msgs_ok, 0)
        self.assertEqual(ev.time_points_ok, 1)  # 只有 end=04-15 命中
        self.assertEqual(ev.time_points_expected, 3)

    def test_aggregate_classify_math(self):
        ev1 = ClassifyCaseEval(
            case_id="a", messages_total=3, expected_count=2, hits=2, model_count=2,
            time_msgs_expected=2, time_msgs_ok=2, time_points_expected=3,
            time_points_ok=3, time_points_model=3, summary_filled=2, summary_total=2,
        )
        ev2 = ClassifyCaseEval(
            case_id="b", messages_total=1, expected_count=1, hits=0, model_count=0,
            time_msgs_expected=1, time_msgs_ok=0, time_points_expected=1,
            time_points_ok=0, time_points_model=0, summary_filled=0, summary_total=0,
            failed=1,
        )
        s = aggregate_classify([ev1, ev2])
        self.assertEqual(s["category_accuracy"], 2 / 3)
        self.assertEqual(s["category_precision"], 1.0)
        self.assertEqual(s["category_recall"], 2 / 3)
        self.assertEqual(s["time_msg_accuracy"], 2 / 3)
        self.assertEqual(s["failure_rate"], 0.25)
        self.assertEqual(s["summary_fill_rate"], 1.0)

    def test_aggregate_classify_error_case_excluded(self):
        bad = ClassifyCaseEval(case_id="x", messages_total=2, error="boom")
        s = aggregate_classify([bad])
        self.assertEqual(s["error_cases"], 1)
        self.assertEqual(s["category_accuracy"], 0.0)


class TitleMetricsTest(unittest.TestCase):
    def test_aggregate_title(self):
        evals = [
            TitleCaseEval(
                case_id="a", output="塔卡沙a6方格40页团购（5本45元）",
                expected_title="塔卡沙a6方格40页团购（5本45元）",
                expected_keywords=["塔卡沙", "团购"],
            ),
            TitleCaseEval(
                case_id="b", output="摄影社招新",
                expected_keywords=["摄影社", "面试"],
            ),
            TitleCaseEval(case_id="c", output=None, expected_keywords=["a"]),
        ]
        s = aggregate_title(evals)
        self.assertEqual(s["exact_match_rate"], 1.0)
        self.assertEqual(s["exact_match_cases"], 1)
        self.assertEqual(s["keyword_hit_rate"], 1 / 3)
        self.assertEqual(s["keyword_hit_cases"], 3)
        self.assertEqual(s["avg_len"], (len("塔卡沙a6方格40页团购（5本45元）") + len("摄影社招新")) / 2)
        self.assertEqual(s["fallback_count"], 1)
        self.assertEqual(s["too_long_count"], 0)

    def test_too_long(self):
        output = "这是一个非常非常非常长的标题已经远远超过了三十个字的限制要求长度"
        self.assertGreater(len(output), 30)
        ev = TitleCaseEval(case_id="a", output=output, expected_keywords=["标题"])
        self.assertTrue(ev.too_long)


class DryRunTest(unittest.TestCase):
    def test_dry_run_example_datasets(self):
        # 隔离到只含示例文件的临时目录，避免本机网页导出的 *.fromweb.json 干扰
        with tempfile.TemporaryDirectory() as tmp:
            cases_dir = Path(tmp)
            for f in FEATURES:
                shutil.copyfile(
                    CASES_DIR / f"{f}.example.json", cases_dir / f"{f}.example.json"
                )
            payload = cli.run_dry_run(list(FEATURES), cases_dir, None)
            self.assertEqual(set(payload["features"]), set(FEATURES))
            for f in FEATURES:
                self.assertIn("cases", payload["features"][f])

    def test_resolve_dataset_falls_back_to_example(self):
        with tempfile.TemporaryDirectory() as tmp:
            cases_dir = Path(tmp)
            shutil.copyfile(
                CASES_DIR / "classify.example.json", cases_dir / "classify.example.json"
            )
            path = cli._resolve_dataset("classify", cases_dir, None)
            self.assertEqual(path.name, "classify.example.json")

    def test_resolve_dataset_falls_back_to_fromweb(self):
        with tempfile.TemporaryDirectory() as tmp:
            cases_dir = Path(tmp)
            (cases_dir / "classify.fromweb.json").write_text(
                json.dumps({"feature": "classify", "cases": []}), encoding="utf-8"
            )
            path = cli._resolve_dataset("classify", cases_dir, None)
            self.assertEqual(path.name, "classify.fromweb.json")
            # fromweb 优先于示例数据集
            (cases_dir / "classify.example.json").write_text(
                json.dumps({"feature": "classify", "cases": []}), encoding="utf-8"
            )
            path = cli._resolve_dataset("classify", cases_dir, None)
            self.assertEqual(path.name, "classify.fromweb.json")


class BenchEnvironmentTest(unittest.IsolatedAsyncioTestCase):
    async def test_env_patches_get_db_and_restores(self):
        """补丁式临时库：不改 config.db_path、不动应用连接，退出即恢复。"""
        import briefdesk.db as briefdesk_db

        old_get_db = briefdesk_db.get_db
        old_path = config.db_path
        async with bench_environment(
            [CategoryDef(name="自定义类别", prompt="测试说明")], register_ai=False
        ):
            self.assertIsNot(briefdesk_db.get_db, old_get_db)  # 已替换
            conn = await briefdesk_db.get_db()
            cursor = await conn.execute("SELECT name FROM categories ORDER BY id")
            rows = await cursor.fetchall()
            await cursor.close()
            self.assertEqual([r["name"] for r in rows], ["自定义类别"])
        self.assertIs(briefdesk_db.get_db, old_get_db)  # 已恢复
        self.assertEqual(config.db_path, old_path)

    async def test_dedup_disjoint_pair_skips_ai_offline(self):
        """预筛跳过路径离线可测：标题无重叠 → 不触发 AI，保守判 false。"""
        case = DedupCase(
            id="d1",
            items=[MessageIn(msg_id="i1", content="出二手自行车", title="出二手自行车")],
            query=MessageIn(msg_id="q1", content="求购考研数学书", title="求购考研数学书"),
            expected=SameExpected(same=False),
        )
        async with bench_environment(register_ai=False):
            ev = await engine._run_dedup(case)
        self.assertFalse(ev.predicted)
        self.assertTrue(ev.skipped)

    async def test_run_benchmark_cases_empty_records_elapsed(self):
        """零用例运行不触发 AI，但 payload 仍带测试用时（顶层 + 逐功能）。"""
        payload, evals = await engine.run_benchmark_cases({})
        self.assertEqual(payload["features"], {})
        self.assertEqual(evals, {})
        self.assertIsInstance(payload["elapsed_sec"], float)
        self.assertGreaterEqual(payload["elapsed_sec"], 0)


class ProgressTest(unittest.IsolatedAsyncioTestCase):
    """评估进度回调：逐条事件序列 + run_benchmark_cases 透传（离线可测）。"""

    def _dedup_case(self, cid: str) -> DedupCase:
        return DedupCase(
            id=cid,
            items=[MessageIn(msg_id="i", content="a")],
            query=MessageIn(msg_id="q", content="b"),
            expected=SameExpected(same=False),
        )

    async def test_run_feature_progress_events_sequence(self):
        """每条用例 settle 回调一次；done 严格递增、failed 累计正确。"""
        cases = [self._dedup_case(f"d{i}") for i in range(6)]
        events: list[engine.CaseProgress] = []

        async def _flaky(case: DedupCase) -> BinaryCaseEval:
            if case.id in {"d1", "d4"}:
                raise RuntimeError("模拟失败")
            return BinaryCaseEval(case_id=case.id, predicted=True, expected=True)

        with patch.object(engine, "_RUNNERS", {"dedup": _flaky}):
            evals = await engine._run_feature("dedup", cases, 2, progress=events.append)
        self.assertEqual(len(events), 6)
        self.assertEqual([e.done for e in events], [1, 2, 3, 4, 5, 6])
        self.assertEqual([e.total for e in events], [6] * 6)
        self.assertEqual([e.failed for e in events], [0, 1, 1, 1, 2, 2])
        self.assertEqual(events[-1].feature, "dedup")
        self.assertEqual([e.case_id for e in events], [f"d{i}" for i in range(6)])
        # 失败用例以 error eval 落位，不拖垮整轮
        self.assertEqual(len(evals), 6)
        self.assertEqual(sum(1 for e in evals if e.error), 2)

    async def test_run_benchmark_cases_forwards_progress(self):
        """run_benchmark_cases 透传进度回调（离线判重用例：预筛跳过，不触发 AI）。"""
        case = DedupCase(
            id="d1",
            items=[MessageIn(msg_id="i1", content="出二手自行车", title="出二手自行车")],
            query=MessageIn(msg_id="q1", content="求购考研数学书", title="求购考研数学书"),
            expected=SameExpected(same=False),
        )
        events: list[engine.CaseProgress] = []
        payload, evals = await engine.run_benchmark_cases(
            {"dedup": [case]}, progress=events.append
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].feature, "dedup")
        self.assertEqual((events[0].done, events[0].total, events[0].failed), (1, 1, 0))
        self.assertIn("dedup", payload["features"])
        self.assertTrue(evals["dedup"][0].skipped)  # 预筛跳过路径仍生效


class CliProgressTest(unittest.TestCase):
    """CLI 进度打印器：每 5 条一行（最后一条恒输出）、失败数非零才标注。"""

    def _printer(self):
        return cli._progress_printer(time.monotonic())

    def test_every_five_and_final_line(self):
        out = io.StringIO()
        printer = self._printer()
        with contextlib.redirect_stdout(out):
            for i in range(1, 13):
                printer(
                    engine.CaseProgress(feature="classify", done=i, total=12, failed=0)
                )
        lines = out.getvalue().splitlines()
        self.assertEqual(len(lines), 3)  # 5/12、10/12、12/12
        self.assertIn("classify 5/12", lines[0])
        self.assertIn("classify 10/12", lines[1])
        self.assertIn("classify 12/12", lines[2])
        self.assertNotIn("失败", out.getvalue())  # 失败 0 不标注

    def test_small_total_still_prints_last(self):
        out = io.StringIO()
        printer = self._printer()
        with contextlib.redirect_stdout(out):
            for i in range(1, 4):
                printer(engine.CaseProgress(feature="title", done=i, total=3, failed=0))
        lines = out.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertIn("title 3/3", lines[0])

    def test_failure_count_annotated(self):
        out = io.StringIO()
        printer = self._printer()
        with contextlib.redirect_stdout(out):
            printer(engine.CaseProgress(feature="dedup", done=5, total=9, failed=2))
        self.assertIn("（失败 2）", out.getvalue())


class ChartsTest(unittest.TestCase):
    """SVG 图表与 HTML 报告（纯函数，无 AI/DB）。"""

    def test_bar_chart_svg_contains_labels_and_values(self):
        from briefdesk.plugins.benchmark.charts import bar_chart_svg

        svg = bar_chart_svg(
            "测试指标", [("类别准确率", 0.875), ("类别 F1", 0.5)]
        )
        self.assertTrue(svg.startswith("<svg"))
        self.assertIn("类别准确率", svg)
        self.assertIn("87.5%", svg)
        self.assertIn("50.0%", svg)
        self.assertIn("</svg>", svg)

    def test_bar_chart_svg_escapes_label(self):
        from briefdesk.plugins.benchmark.charts import bar_chart_svg

        svg = bar_chart_svg("标题", [("<b>注入</b>", 1.0)])
        self.assertIn("&lt;b&gt;注入&lt;/b&gt;", svg)
        self.assertNotIn("<b>注入</b>", svg)

    def test_bar_chart_svg_empty(self):
        from briefdesk.plugins.benchmark.charts import bar_chart_svg

        svg = bar_chart_svg("空", [])
        self.assertIn("<svg", svg)
        self.assertIn("无数据", svg)

    def test_confusion_svg_counts(self):
        from briefdesk.plugins.benchmark.charts import confusion_svg

        svg = confusion_svg("混淆矩阵", tp=2, fp=1, tn=3, fn=0)
        for text in ("TP", "FP", "TN", "FN", "2", "3"):
            self.assertIn(text, svg)
        self.assertIn("期望是", svg)
        self.assertIn("预测否", svg)

    def _payload(self) -> dict:
        return {
            "run_id": "20260821-000000",
            "generated_at": "2026-08-21 00:00:00",
            "model": "test-model",
            "ai_api_base": "http://localhost:11434/v1",
            "concurrency": 1,
            "features": {
                "classify": {
                    "dataset": "cases/classify.json",
                    "summary": {
                        "cases": 1, "error_cases": 0, "expected": 2, "model_classified": 2,
                        "category_accuracy": 1.0, "category_precision": 1.0,
                        "category_recall": 1.0, "category_f1": 1.0,
                        "time_msg_accuracy": 1.0, "time_point_recall": 1.0,
                        "time_point_precision": 1.0, "failure_rate": 0.0,
                        "summary_fill_rate": 1.0, "false_positives": 0,
                    },
                    "cases": [
                        {
                            "case_id": "cls-001",
                            "hits": 2, "expected_count": 2, "model_count": 2,
                            "false_positives": 0, "time_msgs_ok": 1,
                            "time_msgs_expected": 1, "failed": 0,
                            "summary_filled": 2, "summary_total": 2, "error": None,
                        }
                    ],
                },
                "dedup": {
                    "dataset": "cases/dedup.json",
                    "summary": {
                        "cases": 1, "error_cases": 0, "accuracy": 1.0,
                        "precision": 1.0, "recall": 1.0, "f1": 1.0,
                        "tp": 1, "fp": 0, "tn": 0, "fn": 0, "skipped": 0,
                    },
                    "cases": [
                        {"case_id": "dd-001", "predicted": True, "expected": True, "skipped": False, "error": None}
                    ],
                },
            },
        }

    def test_build_html_report(self):
        from briefdesk.plugins.benchmark.html_report import build_html_report

        html_text = build_html_report(self._payload())
        self.assertIn("<!DOCTYPE html>", html_text)
        self.assertIn("分类（classify_batch）", html_text)
        self.assertIn("混淆矩阵（行=预测，列=期望）", html_text)
        self.assertIn("100.0%", html_text)
        self.assertIn("dd-001", html_text)
        self.assertIn("</html>", html_text)
        # 用例 id 注入应被转义
        payload = self._payload()
        payload["features"]["title"] = {
            "dataset": "cases/title.json",
            "summary": {"cases": 1, "error_cases": 0, "exact_match_rate": 0.0,
                        "keyword_hit_rate": 1.0, "avg_len": 6.0,
                        "too_long_count": 0, "fallback_count": 0,
                        "exact_match_cases": 0, "keyword_hit_cases": 1},
            "cases": [{"case_id": "<script>alert(1)</script>", "output": "摄影社招新",
                       "expected_title": None, "expected_keywords": ["摄影社"], "error": None}],
        }
        html_text = build_html_report(payload)
        self.assertIn("&lt;script&gt;", html_text)
        self.assertNotIn("<script>alert(1)</script>", html_text)

    def test_html_report_shows_elapsed(self):
        """HTML 报告头部含总用时、各功能指标表含测试用时。"""
        from briefdesk.plugins.benchmark.html_report import build_html_report

        payload = self._payload()
        payload["elapsed_sec"] = 12.34
        payload["features"]["classify"]["elapsed_sec"] = 5.67
        payload["features"]["dedup"]["elapsed_sec"] = 0.45
        html_text = build_html_report(payload)
        self.assertIn("总用时", html_text)
        self.assertIn("12.3s", html_text)
        self.assertIn("测试用时", html_text)
        self.assertIn("5.7s", html_text)
        self.assertIn("450ms", html_text)
        # 缺省时显示 '-' 而非报错
        html_text = build_html_report(self._payload())
        self.assertIn("总用时", html_text)
        self.assertIn("<b>总用时</b> -", html_text)

    def test_build_payload_includes_elapsed(self):
        """payload 顶层 elapsed_sec + 每功能 elapsed_sec 由 _build_payload 写入。"""
        payload = engine._build_payload(
            {"classify": {"cases": 1}},
            {"classify": [ClassifyCaseEval(case_id="c1")]},
            1,
            "cases/classify.json",
            elapsed={"classify": 1.234},
            total_elapsed=2.5,
        )
        self.assertEqual(payload["elapsed_sec"], 2.5)
        self.assertEqual(payload["features"]["classify"]["elapsed_sec"], 1.234)
        # 缺省参数（旧调用方）不写入用时
        payload = engine._build_payload({}, {}, 1, "x")
        self.assertIsNone(payload["elapsed_sec"])
        self.assertEqual(payload["features"], {})

    def test_render_feature_block_shows_elapsed(self):
        from briefdesk.plugins.benchmark.report import render_feature_block

        summary = aggregate_binary(
            [BinaryCaseEval(case_id="c1", predicted=True, expected=True)]
        )
        lines = render_feature_block("dedup", [], summary, 12.34)
        self.assertTrue(any("测试用时" in ln and "12.3s" in ln for ln in lines))
        # 未传用时（旧调用方）不输出该行
        lines = render_feature_block("dedup", [], summary)
        self.assertFalse(any("测试用时" in ln for ln in lines))

    def test_save_html_report(self):
        from briefdesk.plugins.benchmark.html_report import save_html_report

        out_dir = Path(__file__).resolve().parents[1] / "briefdesk" / "plugins" / "benchmark" / ".tmp"
        out_dir.mkdir(parents=True, exist_ok=True)
        try:
            path = save_html_report(out_dir, "test-run-1", "<html></html>")
            self.assertTrue(path.exists())
            self.assertEqual(path.read_text(encoding="utf-8"), "<html></html>")
        finally:
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
