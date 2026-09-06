"""动态核心/插件设置 schema 测试。"""

import os
import unittest
from typing import Annotated
from unittest.mock import patch

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings

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

    def test_rejects_newline_and_inline_comment_in_string_value(self) -> None:
        """【复核 P1-7】字符串值含换行 / ' #' 时拒绝：暂存文件是 KEY=VALUE
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
        # 审查回归：text 型暂存值含 CR/LF 时会向暂存文件注入任意 KEY=VALUE 行
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

    def test_core_settings_expose_vision_fields(self) -> None:
        # vision 路由：新配置项自动进入核心设置 schema（设置页白名单表单），
        # label/hint 经 _CORE_UI 覆盖层按 env key 合入（routes_settings_env.py:95-96）
        from briefdesk.server.routes_settings_env import _CORE_SCHEMA

        by_key = {item["key"]: item for item in _CORE_SCHEMA}
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


if __name__ == "__main__":
    unittest.main()
