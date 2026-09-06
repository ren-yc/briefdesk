"""validate_required_config 直接单测：值形态判定口径与缺失聚合行为。"""

import unittest
from types import SimpleNamespace

from pydantic import SecretStr

from briefdesk.plugin.base import PluginDisabledError
from briefdesk.plugin.config_helpers import validate_required_config


class ValidateRequiredConfigTest(unittest.TestCase):
    """helper 直接覆盖（插件 setup 测试为间接覆盖；本类钉死判定口径）。"""

    def test_all_present_no_raise(self):
        settings = SimpleNamespace(
            api_token=SecretStr("t"), qq="123", extra_map={"k": "v"}
        )
        validate_required_config(
            settings,
            {"api_token": "A_TOKEN", "qq": "A_QQ", "extra_map": "A_MAP"},
        )  # 不抛即通过

    def test_missing_field_name_counts_as_missing(self):
        """getattr 未命中（字段不存在）→ None → 计入缺失。"""
        settings = SimpleNamespace(api_token=SecretStr("t"))
        with self.assertRaises(PluginDisabledError) as cm:
            validate_required_config(settings, {"api_token": "A_TOKEN", "ghost": "A_GHOST"})
        message = str(cm.exception)
        self.assertIn("A_GHOST", message)
        self.assertNotIn("A_TOKEN", message)

    def test_empty_values_count_as_missing(self):
        """空 SecretStr / 空串均判缺失（与重构前插件代码的 `not value` 口径一致）。"""
        for value in (SecretStr(""), ""):
            with self.subTest(value=repr(value)):
                settings = SimpleNamespace(api_token=value)
                with self.assertRaises(PluginDisabledError):
                    validate_required_config(settings, {"api_token": "A_TOKEN"})

    def test_whitespace_string_counts_as_present(self):
        """纯空白串为真值 → 判已配置（历史口径，与 `not value` 一致；如需
        收紧为 strip 后判空应先改 helper 再改此处）。"""
        settings = SimpleNamespace(api_token="   ")
        validate_required_config(settings, {"api_token": "A_TOKEN"})  # 不抛即通过

    def test_empty_dict_counts_as_missing(self):
        """非 str 标量（如 property 返回的空 dict）按真值判定 → 缺失。"""
        settings = SimpleNamespace(db_keys_map={})
        with self.assertRaises(PluginDisabledError) as cm:
            validate_required_config(settings, {"db_keys_map": "A_KEYS"})
        self.assertIn("A_KEYS", str(cm.exception))

    def test_missing_all_lists_everything_in_declaration_order(self):
        """多项缺失时聚合在一条错误里一次报全，顺序与声明一致。"""
        settings = SimpleNamespace(api_token=SecretStr(""), wxid="", db_keys_map={})
        with self.assertRaises(PluginDisabledError) as cm:
            validate_required_config(
                settings,
                {
                    "api_token": "A_TOKEN",
                    "wxid": "A_WXID",
                    "db_keys_map": "A_KEYS",
                },
            )
        message = str(cm.exception)
        self.assertIn("A_TOKEN", message)
        self.assertIn("A_WXID", message)
        self.assertIn("A_KEYS", message)
        self.assertLess(
            message.index("A_TOKEN"),
            message.index("A_WXID"),
            "缺失项按声明顺序列出",
        )


if __name__ == "__main__":
    unittest.main()
