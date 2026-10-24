"""动态核心/插件设置 schema 测试。"""

import os
import unittest
from pathlib import Path
from typing import Annotated
from unittest.mock import patch

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings

from briefdesk.plugins.benchmark.plugin import BenchmarkPlugin
from briefdesk.plugins.qqflow.plugin import QqFlowPlugin
from briefdesk.plugins.rag.plugin import RagPlugin
from briefdesk.plugins.weflow_legacy.plugin import WeFlowLegacyPlugin
from briefdesk.settings_schema import build_settings_schema, normalize_setting


class ExampleSettings(BaseSettings):
    limit: int = Field(default=3, ge=1, le=10)
    score: float = Field(default=0.5, gt=0, le=1)
    enabled: bool = True
    names: list[str] = []
    token: SecretStr = SecretStr("")

    model_config = {"env_prefix": "EXAMPLE_"}


class AdvancedSettings(BaseSettings):
    optional_limit: Annotated[int | None, Field(ge=1)] = None
    optional_names: list[str] | None = None
    optional_token: SecretStr | None = None
    ratio: float = Field(default=0.1, multiple_of=0.1)

    model_config = {"env_prefix": "ADVANCED_"}


class InvalidSettings(BaseSettings):
    required_name: str
    required_token: SecretStr
    fallback: int = 4

    model_config = {"env_prefix": "INVALID_"}


class MalformedSettings(BaseSettings):
    threshold: int = Field(default=4, ge=1)

    model_config = {"env_prefix": "MALFORMED_"}


class ValidatorBoomSettings(BaseSettings):
    """模型级 validator 抛错的最小形态：投影实例化的另一类异常成因。

    守卫分支（_expected_instance 的 except Exception）被既有用例覆盖的只有
    缺必填字段与坏环境变量两类成因；validator 抛错若被 pydantic 换成别的
    通道（不再从 model() 冒泡），本用例先红而字段校验用例不受影响。
    """

    threshold: int = 4

    model_config = {"env_prefix": "BOOM_"}

    @model_validator(mode="after")
    def _boom(self) -> "ValidatorBoomSettings":
        # 错误文本夹带 pydantic 会回显的 input_value 片段：断言脱敏摘要必须
        # 把它剥掉（守卫依赖 _sanitize_error 的截断口径）。
        raise ValueError("validator boom [input_value=threshold=4]")


def _raise_factory_error() -> str:
    raise RuntimeError("factory boom")


class FactoryDefaultSettings(BaseSettings):
    """default_factory 字段（DB_PATH 就是这种字段）的默认值展示。"""

    db_path: str = Field(default_factory=lambda: "D:/data/briefdesk.sqlite", alias="DB_PATH")
    broken: str = Field(default_factory=_raise_factory_error, alias="BROKEN")
    none_value: str = Field(default_factory=lambda: None, alias="NONE_VALUE")  # type: ignore[assignment]
    secret_value: SecretStr = Field(default_factory=lambda: SecretStr("s"), alias="SECRET_VALUE")


class SettingsSchemaTest(unittest.TestCase):
    def test_builds_env_keys_values_and_constraints(self) -> None:
        schema = build_settings_schema(ExampleSettings, plugin="example")
        by_key = {item["key"]: item for item in schema}
        self.assertEqual(by_key["EXAMPLE_LIMIT"]["type"], "number")
        self.assertEqual(by_key["EXAMPLE_LIMIT"]["numberKind"], "integer")
        self.assertEqual(by_key["EXAMPLE_LIMIT"]["min"], 1)
        self.assertEqual(by_key["EXAMPLE_LIMIT"]["max"], 10)
        self.assertEqual(by_key["EXAMPLE_ENABLED"]["current"], True)
        self.assertEqual(by_key["EXAMPLE_NAMES"]["type"], "multi")
        self.assertNotIn("current", by_key["EXAMPLE_TOKEN"])
        self.assertTrue(by_key["EXAMPLE_TOKEN"]["secret"])

    def test_normalizes_and_validates_dynamic_values(self) -> None:
        schema = build_settings_schema(ExampleSettings)
        by_key = {item["key"]: item for item in schema}
        self.assertEqual(normalize_setting(by_key["EXAMPLE_LIMIT"], "10"), "10")
        self.assertEqual(
            normalize_setting(by_key["EXAMPLE_NAMES"], '["one","two"]'),
            '["one","two"]',
        )
        self.assertEqual(normalize_setting(by_key["EXAMPLE_ENABLED"], "false"), "false")
        with self.assertRaises(ValueError):
            normalize_setting(by_key["EXAMPLE_LIMIT"], "0")
        with self.assertRaises(ValueError):
            normalize_setting(by_key["EXAMPLE_ENABLED"], "maybe")

    def test_empty_number_value_error_names_the_reset_path(self) -> None:
        """数字项收到空串的报错必须点明「留空不等于恢复默认」。

        前端对「没有运行值」的条目渲染空控件，归一写错时会把空串当改动提交上来；
        报错若只说「须为数字」，用户看到的只是笼统的保存失败，无从知道该走哪里。
        """
        by_key = {item["key"]: item for item in build_settings_schema(ExampleSettings)}
        with self.assertRaises(ValueError) as ctx:
            normalize_setting(by_key["EXAMPLE_LIMIT"], "")
        self.assertIn("留空不等于恢复默认", str(ctx.exception))

    def test_rejects_newline_and_inline_comment_in_string_value(self) -> None:
        """字符串值含换行 / ' #' 时拒绝：暂存文件是 KEY=VALUE
        行格式，换行可注入任意配置行（含密钥名——路由白名单只过滤键名），
        ' #' 会被 dotenv 当行内注释截断，写入/读回不保真。"""

        class PlainSettings(BaseSettings):
            greeting: str = "hi"

            model_config = {"env_prefix": "PLAIN_"}

        by_key = {item["key"]: item for item in build_settings_schema(PlainSettings)}
        self.assertEqual(normalize_setting(by_key["PLAIN_GREETING"], "hello"), "hello")
        with self.assertRaises(ValueError):
            normalize_setting(by_key["PLAIN_GREETING"], "hello\nAI_API_KEY=sk-evil")
        with self.assertRaises(ValueError):
            normalize_setting(by_key["PLAIN_GREETING"], "hello # comment")

    def test_text_value_rejects_newlines_and_inline_comment(self) -> None:
        # 回归防线：text 型暂存值含 CR/LF 时会向暂存文件注入任意 KEY=VALUE 行
        # （绕过键白名单与「密钥只走 keyring」分层）；「 #」是 dotenv 行内
        # 注释起点，回读被截断，一并拒绝
        meta = {"type": "text"}
        self.assertEqual(
            normalize_setting(meta, "http://127.0.0.1:5033"),
            "http://127.0.0.1:5033",
        )
        with self.assertRaises(ValueError):
            normalize_setting(meta, "x\nAI_API_KEY=sk-attacker")
        with self.assertRaises(ValueError):
            normalize_setting(meta, "x\r\nAI_API_KEY=sk-attacker")
        with self.assertRaises(ValueError):
            normalize_setting(meta, "value # inline comment")

    def test_unwraps_optional_and_annotated_field_types(self) -> None:
        schema = build_settings_schema(AdvancedSettings)
        by_key = {item["key"]: item for item in schema}
        self.assertEqual(by_key["ADVANCED_OPTIONAL_LIMIT"]["type"], "number")
        self.assertEqual(by_key["ADVANCED_OPTIONAL_LIMIT"]["numberKind"], "integer")
        self.assertEqual(by_key["ADVANCED_OPTIONAL_LIMIT"]["min"], 1)
        self.assertEqual(by_key["ADVANCED_OPTIONAL_NAMES"]["type"], "multi")
        self.assertTrue(by_key["ADVANCED_OPTIONAL_TOKEN"]["secret"])
        self.assertEqual(
            normalize_setting(by_key["ADVANCED_OPTIONAL_LIMIT"], "2"), "2"
        )
        self.assertEqual(
            normalize_setting(by_key["ADVANCED_OPTIONAL_NAMES"], '["a"]'), '["a"]'
        )

    def test_schema_survives_missing_required_configuration(self) -> None:
        schema = build_settings_schema(InvalidSettings)
        by_key = {item["key"]: item for item in schema}
        self.assertEqual(set(by_key), {
            "INVALID_REQUIRED_NAME",
            "INVALID_REQUIRED_TOKEN",
            "INVALID_FALLBACK",
        })
        self.assertIsNone(by_key["INVALID_REQUIRED_NAME"]["current"])
        self.assertIsNone(by_key["INVALID_REQUIRED_TOKEN"]["configured"])
        self.assertEqual(by_key["INVALID_FALLBACK"]["default"], 4)
        self.assertIsNone(by_key["INVALID_FALLBACK"]["current"])

    def test_schema_survives_malformed_environment_value(self) -> None:
        with patch.dict(os.environ, {"MALFORMED_THRESHOLD": "not-a-number"}):
            schema = build_settings_schema(MalformedSettings)
        threshold = next(
            item for item in schema if item["key"] == "MALFORMED_THRESHOLD"
        )
        self.assertEqual(threshold["type"], "number")
        self.assertIsNone(threshold["current"])
        self.assertEqual(threshold["default"], 4)

    def test_schema_survives_model_validator_failure(self) -> None:
        """模型级 validator 抛错不得 500：字段元数据照常下发、expected 降级。

        与缺必填字段用例覆盖同一守卫分支，但触发通道不同（validator 异常 vs
        字段校验异常）——pydantic 改变其中一条通道时另一用例仍有判别力。
        """
        schema = build_settings_schema(ValidatorBoomSettings)
        by_key = {item["key"]: item for item in schema}
        self.assertEqual(set(by_key), {"BOOM_THRESHOLD"})
        item = by_key["BOOM_THRESHOLD"]
        self.assertEqual(item["type"], "number")
        self.assertEqual(item["default"], 4)
        # expected 路降级而非 500：坏配置只影响「下次启动值」，修复所需的
        # 字段元数据必须完整
        self.assertFalse(item.get("expectedAvailable", True))
        self.assertTrue(item.get("expectedError"))
        self.assertNotIn("input_value=", item.get("expectedError", ""), "脱敏摘要不得回显 input_value")
        self.assertIsNone(item.get("current"))

    def test_decimal_step_accepts_exact_decimal_multiples(self) -> None:
        schema = build_settings_schema(AdvancedSettings)
        ratio = next(item for item in schema if item["key"] == "ADVANCED_RATIO")
        for value in ("0.3", "0.7", "1.0"):
            self.assertEqual(normalize_setting(ratio, value), value)
        with self.assertRaises(ValueError):
            normalize_setting(ratio, "0.35")

    def test_builtin_plugin_schemas_expose_their_settings(self) -> None:
        weflow = {item["key"]: item for item in WeFlowLegacyPlugin().settings_schema()}
        qqflow = {item["key"]: item for item in QqFlowPlugin().settings_schema()}
        rag = {item["key"]: item for item in RagPlugin().settings_schema()}
        self.assertIn("WEFLOW_LEGACY_API_BASE", weflow)
        self.assertTrue(weflow["WEFLOW_LEGACY_API_TOKEN"]["secret"])
        self.assertIn("QQFLOW_QQ", qqflow)
        self.assertTrue(qqflow["QQFLOW_KEY"]["secret"])
        self.assertEqual(rag["RAG_GROUP_ONLY"]["type"], "boolean")
        self.assertEqual(rag["RAG_TOP_K"]["min"], 1)

    def test_apply_when_defaults_to_restart_and_is_declared_per_field(self) -> None:
        """采用时点元数据：未声明回落 restart；benchmark 四字段按其采用时点
        契约声明（三个运行参数 next_run、keep_runs next_cleanup）。前端按
        applyWhen 显示采用时点文案，缺省措辞「重启生效」对运行参数是误导。"""
        core = {item["key"]: item for item in build_settings_schema(ExampleSettings)}
        self.assertEqual(core["EXAMPLE_LIMIT"]["applyWhen"], "restart")
        self.assertNotIn("restart", core["EXAMPLE_LIMIT"], "旧静态 restart 字段应被 applyWhen 取代")

        benchmark = {
            item["key"]: item
            for item in BenchmarkPlugin().settings_schema()
        }
        self.assertEqual(
            benchmark["BENCHMARK_PAUSE_PIPELINE"]["applyWhen"], "next_run"
        )
        self.assertEqual(
            benchmark["BENCHMARK_RUN_TIMEOUT_SECONDS"]["applyWhen"], "next_run"
        )
        self.assertEqual(
            benchmark["BENCHMARK_RUN_STALL_SECONDS"]["applyWhen"], "next_run"
        )
        self.assertEqual(
            benchmark["BENCHMARK_KEEP_RUNS"]["applyWhen"], "next_cleanup"
        )

    def test_source_api_base_carries_url_contract_hint(self) -> None:
        """三源 base_url 契约：设置页必须提示「根地址/反代子路径前缀、
        不带查询串、不填完整端点」——新实现按 base 路径前缀拼接，误配完整
        端点会产出坏 URL（旧实现的绝对路径 join 会自动纠偏，已移除）。"""
        from briefdesk.plugins.qqflow.plugin import QqFlowPlugin
        from briefdesk.plugins.weflow.plugin import WeFlowPlugin
        from briefdesk.plugins.weflow_legacy.plugin import WeFlowLegacyPlugin

        cases = (
            (WeFlowPlugin(), "WEFLOW_API_BASE"),
            (QqFlowPlugin(), "QQFLOW_API_BASE"),
            (WeFlowLegacyPlugin(), "WEFLOW_LEGACY_API_BASE"),
        )
        for plugin, key in cases:
            item = next(
                i for i in plugin.settings_schema() if i["key"] == key
            )
            # 循环内多断言（不引入 subTest，保持套件 subtests 计数稳定）
            self.assertIn("基址", item["label"], f"{key} label 未写明「基址」")
            hint = item["hint"]
            self.assertIn("反代", hint, f"{key} hint 未提示反代子路径")
            self.assertIn("查询串", hint, f"{key} hint 未提示禁止查询串")
            self.assertIn("完整端点", hint, f"{key} hint 未提示禁止完整端点")

    def test_core_settings_expose_vision_fields(self) -> None:
        # vision 路由：新配置项自动进入核心设置 schema（设置页白名单表单），
        # label/hint 经 _CORE_UI 覆盖层按 env key 合入（routes_settings_env 的
        # _core_schema 覆盖循环）
        from briefdesk.server.routes_settings_env import _core_schema

        by_key = {item["key"]: item for item in _core_schema()}
        vision = by_key["AI_VISION_ENABLED"]
        self.assertEqual(vision["type"], "boolean")
        self.assertEqual(vision["default"], False)
        self.assertEqual(vision["label"], "AI 支持图片输入（视觉模型）")
        self.assertIn("ocr 插件", vision["hint"])
        max_images = by_key["AI_VISION_MAX_IMAGES"]
        self.assertEqual(max_images["type"], "number")
        self.assertEqual(max_images["numberKind"], "integer")
        self.assertEqual(max_images["min"], 1)
        self.assertEqual(max_images["max"], 20)

    def test_every_core_field_has_curated_copy(self) -> None:
        """核心字段必须有中文文案，且 _CORE_UI 不得含未知键。

        失败模式（真实发生过）：新增 Settings 字段时忘了在 `_CORE_UI` 补条目，
        设置页照常渲染、接口照常返回，只是标题回落成 `_label_from_name` 的下划线
        拆词英文（`POLL_INTERVAL_SECONDS` → `Poll Interval Seconds`）——没有守卫
        就只能靠肉眼在设置页发现。反向也要查：键名拼错时覆盖静默失效。
        """
        from briefdesk.config import Settings
        from briefdesk.server.routes_settings_env import _CORE_UI
        from briefdesk.settings_schema import _field_key

        keys = {_field_key(Settings, name) for name in Settings.model_fields}
        self.assertEqual(
            sorted(keys - set(_CORE_UI)),
            [],
            "这些核心设置缺中文文案（未收录进 _CORE_UI，设置页会显示英文标签）",
        )
        self.assertEqual(
            sorted(set(_CORE_UI) - keys),
            [],
            "_CORE_UI 里有 Settings 不认识的键（键名拼写错误时覆盖静默失效）",
        )
        for key, copy in _CORE_UI.items():
            self.assertTrue(copy.get("label"), f"{key} 的 label 不能为空")


class DefaultFactorySchemaTest(unittest.TestCase):
    """default_factory 字段的默认值：工厂是唯一取值来源（field.default 恒为
    PydanticUndefined），漏读会让设置页丢掉该键的默认值展示。"""

    def test_factory_value_is_exposed_and_failures_are_skipped(self) -> None:
        items = {item["key"]: item for item in build_settings_schema(FactoryDefaultSettings)}
        self.assertEqual(items["DB_PATH"]["default"], "D:/data/briefdesk.sqlite")
        # 工厂抛错 / 返回 None / 密钥字段：省略 default，但不阻断整份 schema
        self.assertNotIn("default", items["BROKEN"])
        self.assertNotIn("default", items["NONE_VALUE"])
        self.assertNotIn("default", items["SECRET_VALUE"])

    def test_core_db_path_default_follows_data_dir(self) -> None:
        """核心 Settings 的 DB_PATH 默认值随 BRIEFDESK_DATA_DIR 走（实例化时求值）。"""
        from briefdesk.config import Settings

        data_dir = os.path.join(os.sep, "tmp", "data-dir")
        with patch.dict(os.environ, {"BRIEFDESK_DATA_DIR": data_dir}):
            items = {item["key"]: item for item in build_settings_schema(Settings)}
        expected = str(Path(data_dir) / "data" / "briefdesk.sqlite")
        self.assertEqual(items["DB_PATH"]["default"], expected)

    def test_explicit_db_path_overrides_data_dir_default(self) -> None:
        """显式 DB_PATH 完全覆盖默认路径：默认值只在该键缺省时参与。

        环境变量与构造参数是两条独立的显式通道，任一存在都不应再回落到
        「用户数据目录/data/briefdesk.sqlite」。
        """
        from briefdesk.config import Settings

        data_dir = os.path.join(os.sep, "tmp", "data-dir")
        explicit = os.path.join(os.sep, "custom", "briefdesk.sqlite")
        with patch.dict(
            os.environ, {"BRIEFDESK_DATA_DIR": data_dir, "DB_PATH": explicit}
        ):
            self.assertEqual(Settings(_env_file=None).db_path, explicit)
        self.assertEqual(Settings(_env_file=None, DB_PATH=explicit).db_path, explicit)


class PluginRunningSnapshotTest(unittest.TestCase):
    """插件运行快照：manager 保存运行实例并下发；未装配的插件显式「无运行值」。"""

    def _items(self, loaded: bool) -> dict[str, dict]:
        from briefdesk.plugin.manager import PluginManager
        from briefdesk.plugins.benchmark.plugin import BenchmarkPlugin

        plugin = BenchmarkPlugin()
        manager = PluginManager()
        manager.register(plugin)
        if loaded:
            manager._mark(plugin.name, "loaded")
            manager._capture_settings(plugin.name, plugin)
        fields = manager.settings_schema()
        return {i["key"]: i for i in fields if i.get("plugin") == "benchmark"}

    def test_loaded_dynamic_plugin_idle_has_no_current(self) -> None:
        """动态运行配置（采用时点=运行开始）：无活动运行时不伪造 current。

        旧行为缓存 setup 期实例并把「按当前文件解析的值」冒充运行值——运行中改过
        配置后两者背离。新契约：空闲时只有 expected（下次运行采用），current 等
        活动运行开始才有。
        """
        items = self._items(loaded=True)
        for key in ("BENCHMARK_KEEP_RUNS", "BENCHMARK_PAUSE_PIPELINE"):
            item = items[key]
            self.assertFalse(item["running"])
            self.assertNotIn("current", item, "空闲时不得拿下次运行值冒充运行值")
            self.assertNotIn("source", item)
            self.assertIn("expected_value", item, "下次运行值仍要能预配置")

    def test_loaded_dynamic_plugin_active_run_exposes_adopted_values(self) -> None:
        """活动运行期间：三个运行参数展示 supervisor 冻结的采用值（source=run）。"""
        from briefdesk.plugin.manager import PluginManager
        from briefdesk.plugins.benchmark import supervisor as bench_supervisor
        from briefdesk.plugins.benchmark.plugin import BenchmarkPlugin

        plugin = BenchmarkPlugin()
        manager = PluginManager()
        manager.register(plugin)
        manager._mark(plugin.name, "loaded")
        manager._capture_settings(plugin.name, plugin)
        # 伪造一个活动运行（只设内存快照，不真启动）：timeout 故意取一个
        # 与任何默认值都不同的值，证明展示的是「采用值」而不是重新解析结果
        run = bench_supervisor._Run(
            run_id="test-run", run_dir=Path("."), features=[],
            started_at="2026-01-01 00:00:00", t0=0.0,
            pause_pipeline=False, run_timeout_seconds=12345, run_stall_seconds=777,
        )
        run.finished = False
        old = bench_supervisor._current
        bench_supervisor._current = run
        try:
            fields = manager.settings_schema()
        finally:
            bench_supervisor._current = old
        items = {i["key"]: i for i in fields if i.get("plugin") == "benchmark"}
        timeout_item = items["BENCHMARK_RUN_TIMEOUT_SECONDS"]
        self.assertEqual(timeout_item["current"], 12345)
        self.assertEqual(timeout_item["source"], "run")
        self.assertTrue(timeout_item["running"])
        # keep_runs 不在运行快照里：它的采用时点是「下次清理」，没有运行 current
        self.assertNotIn("current", items["BENCHMARK_KEEP_RUNS"])
        self.assertIn("expected_value", items["BENCHMARK_KEEP_RUNS"])

    def test_unloaded_plugin_has_no_running_value(self) -> None:
        items = self._items(loaded=False)
        item = items["BENCHMARK_KEEP_RUNS"]
        self.assertFalse(item["running"])
        self.assertNotIn("current", item, "无运行值不得拿下次启动值冒充")
        self.assertNotIn("source", item)
        self.assertIn("expected_value", item, "下次启动值仍要能预配置")


if __name__ == "__main__":
    unittest.main()
