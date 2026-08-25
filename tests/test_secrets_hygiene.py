"""密钥卫生守卫：Settings 的 repr/str/model_dump 不得泄露密钥明文。

覆盖 app 级配置（briefdesk/config.py）与两个消息源专属配置
（weflow/qqflow 的 config.py）：字段以 SecretStr 持有后，任何
repr/str/序列化输出都必须只剩掩码，明文只能经 get_secret_value() 取用。
"""

import unittest

from briefdesk.config import Settings

_DUMMY_KEY = "sk-hygiene-test-0123456789abcdef"


class AppSettingsHygieneTest(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings(ai_api_key=_DUMMY_KEY, embed_api_key=_DUMMY_KEY)

    def test_repr_hides_secret_values(self) -> None:
        self.assertNotIn(_DUMMY_KEY, repr(self.settings))
        self.assertNotIn(_DUMMY_KEY, str(self.settings))

    def test_model_dump_masks_secret_values(self) -> None:
        dump = self.settings.model_dump()
        self.assertNotIn(_DUMMY_KEY, str(dump["ai_api_key"]))
        self.assertNotIn(_DUMMY_KEY, str(dump["embed_api_key"]))

    def test_get_secret_value_returns_plaintext(self) -> None:
        self.assertEqual(self.settings.ai_api_key.get_secret_value(), _DUMMY_KEY)


class PluginSettingsHygieneTest(unittest.TestCase):
    def test_weflow_settings_mask_token(self) -> None:
        from briefdesk.plugins.weflow.config import WeFlowSettings

        settings = WeFlowSettings(api_token="w-" + _DUMMY_KEY)
        self.assertNotIn("w-" + _DUMMY_KEY, str(settings))
        self.assertEqual(settings.api_token.get_secret_value(), "w-" + _DUMMY_KEY)

    def test_qqflow_settings_mask_token_and_key(self) -> None:
        from briefdesk.plugins.qqflow.config import QqFlowSettings

        settings = QqFlowSettings(
            api_token="q-" + _DUMMY_KEY, qq="12345678", key="k-" + _DUMMY_KEY
        )
        self.assertNotIn("q-" + _DUMMY_KEY, str(settings))
        self.assertNotIn("k-" + _DUMMY_KEY, str(settings))


if __name__ == "__main__":
    unittest.main()