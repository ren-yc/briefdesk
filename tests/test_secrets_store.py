"""keyring 密钥环存储与解析链测试（fake backend 隔离，绝不触碰真实凭据管理器）。

- 优先级链：keyring > 环境变量 > .env > 默认值（app 级与插件级 Settings）
- secrets_store 读写/幂等删除/禁用开关
- `briefdesk secrets` CLI 子命令（不打印明文、拒绝白名单外键名）
"""

import io
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import cast
from unittest.mock import patch

import keyring
from keyring.backend import KeyringBackend

from briefdesk.config import Settings
from briefdesk.secrets_cli import secrets_cli_main
from briefdesk.secrets_store import (
    SECRET_NAMES,
    SecretsStoreError,
    configured_names,
    delete_secret,
    get_secret,
    is_keyring_available,
    set_secret,
)


class FakeKeyringBackend(KeyringBackend):
    """内存版 keyring backend（keyring.set_keyring 校验须为 KeyringBackend 实例）。"""

    priority = 10

    def __init__(self) -> None:
        super().__init__()
        self._store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self._store.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self._store[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        if (service, username) not in self._store:
            raise keyring.errors.PasswordDeleteError(
                f"No password for {service}/{username}"
            )
        del self._store[(service, username)]


class KeyringTestCase(unittest.TestCase):
    """基类：注入 fake backend 并在用例结束后恢复原 backend。"""

    _previous: KeyringBackend | None = None

    def setUp(self) -> None:
        try:
            self._previous = keyring.get_keyring()
        except keyring.errors.NoKeyringError:
            self._previous = None
        keyring.set_keyring(FakeKeyringBackend())

    def tearDown(self) -> None:
        if self._previous is not None:
            keyring.set_keyring(self._previous)
        else:
            keyring.set_keyring(keyring.backends.fail.Keyring())

    def _fake(self) -> FakeKeyringBackend:
        return cast(FakeKeyringBackend, keyring.get_keyring())

    def _seed(self, name: str, value: str) -> None:
        self._fake().set_password("briefdesk", name, value)


def _env_file(tmp_parent: str, content: str) -> Path:
    """在临时目录写一个 .env 文件（隔离开发机真实 .env）。"""
    path = Path(tmp_parent) / ".env"
    path.write_text(content, encoding="utf-8")
    return path


class SecretStoreTest(KeyringTestCase):
    def test_set_get_delete_roundtrip(self) -> None:
        self.assertIsNone(get_secret("AI_API_KEY"))
        set_secret("AI_API_KEY", "ring-value")
        self.assertEqual(get_secret("AI_API_KEY"), "ring-value")
        delete_secret("AI_API_KEY")
        self.assertIsNone(get_secret("AI_API_KEY"))

    def test_delete_missing_is_idempotent(self) -> None:
        delete_secret("AI_API_KEY")  # 未设置：不抛错

    def test_configured_names(self) -> None:
        self.assertEqual(configured_names(), [])
        self._seed("AI_API_KEY", "v")
        self.assertEqual(configured_names(), ["AI_API_KEY"])

    def test_keyring_available_with_fake_backend(self) -> None:
        self.assertTrue(is_keyring_available())

    def test_disabled_flag(self) -> None:
        with patch.dict(os.environ, {"BRIEFDESK_KEYRING": "0"}):
            self.assertFalse(is_keyring_available())
            self.assertIsNone(get_secret("AI_API_KEY"))
            with self.assertRaises(SecretsStoreError):
                set_secret("AI_API_KEY", "v")
        # 恢复后（fake backend 仍注入）可用
        self.assertTrue(is_keyring_available())


class KeyringPriorityChainTest(KeyringTestCase):
    def test_keyring_wins_over_env_and_dotenv(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            env_file = _env_file(d, "AI_API_KEY=dotenv-value\n")
            self._seed("AI_API_KEY", "ring-value")
            with patch.dict(os.environ, {"AI_API_KEY": "env-value"}):
                settings = Settings(_env_file=env_file)
            self.assertEqual(
                settings.ai_api_key.get_secret_value(), "ring-value"
            )

    def test_env_wins_over_dotenv(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            env_file = _env_file(d, "AI_API_KEY=dotenv-value\n")
            with patch.dict(os.environ, {"AI_API_KEY": "env-value"}):
                settings = Settings(_env_file=env_file)
            self.assertEqual(settings.ai_api_key.get_secret_value(), "env-value")

    def test_dotenv_when_no_env_and_empty_keyring(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            env_file = _env_file(d, "AI_API_KEY=dotenv-value\n")
            with patch.dict(os.environ, {}, clear=True):
                settings = Settings(_env_file=env_file)
            self.assertEqual(settings.ai_api_key.get_secret_value(), "dotenv-value")

    def test_default_when_all_layers_empty(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            env_file = _env_file(d, "")
            with patch.dict(os.environ, {}, clear=True):
                settings = Settings(_env_file=env_file)
            self.assertEqual(settings.ai_api_key.get_secret_value(), "")

    def test_plugin_settings_read_keyring(self) -> None:
        from briefdesk.plugins.qqflow.config import QqFlowSettings
        from briefdesk.plugins.weflow.config import WeFlowSettings

        self._seed("WEFLOW_API_TOKEN", "weflow-ring-token")
        self._seed("QQFLOW_API_TOKEN", "qqflow-ring-token")
        self._seed("QQFLOW_KEY", "qqflow-ring-key")
        weflow = WeFlowSettings()
        qqflow = QqFlowSettings()
        self.assertEqual(weflow.api_token.get_secret_value(), "weflow-ring-token")
        self.assertEqual(qqflow.api_token.get_secret_value(), "qqflow-ring-token")
        self.assertEqual(qqflow.key.get_secret_value(), "qqflow-ring-key")

    def test_plugin_settings_env_wins_when_keyring_empty(self) -> None:
        from briefdesk.plugins.weflow.config import WeFlowSettings

        with patch.dict(os.environ, {"WEFLOW_API_TOKEN": "env-token"}):
            settings = WeFlowSettings()
        self.assertEqual(settings.api_token.get_secret_value(), "env-token")


class SecretsCliTest(KeyringTestCase):
    def _run(self, argv: list[str]) -> tuple[str, str, int]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = secrets_cli_main(argv)
        return out.getvalue(), err.getvalue(), code

    def test_set_and_get_status_without_reveal(self) -> None:
        out, _, code = self._run(["set", "AI_API_KEY", "cli-value"])
        self.assertEqual(code, 0)
        self.assertIn("已写入系统密钥环", out)
        self.assertEqual(get_secret("AI_API_KEY"), "cli-value")

        out, _, code = self._run(["get", "AI_API_KEY"])
        self.assertEqual(code, 0)
        self.assertNotIn("cli-value", out)  # 默认不打印明文
        self.assertIn("已配置", out)

    def test_get_reveal_prints_plaintext(self) -> None:
        self._seed("AI_API_KEY", "cli-value")
        out, _, code = self._run(["get", "AI_API_KEY", "--reveal"])
        self.assertEqual(code, 0)
        self.assertIn("cli-value", out)

    def test_get_missing_returns_nonzero(self) -> None:
        out, _, code = self._run(["get", "AI_API_KEY"])
        self.assertEqual(code, 1)
        self.assertIn("未配置", out)

    def test_rm(self) -> None:
        self._seed("AI_API_KEY", "v")
        out, _, code = self._run(["rm", "AI_API_KEY"])
        self.assertEqual(code, 0)
        self.assertIsNone(get_secret("AI_API_KEY"))
        self.assertIn("已从系统密钥环删除", out)

    def test_list_reports_names(self) -> None:
        self._seed("AI_API_KEY", "v")
        out, _, code = self._run(["list"])
        self.assertEqual(code, 0)
        for name in SECRET_NAMES:
            self.assertIn(name, out)
        self.assertIn("AI_API_KEY: 已配置", out)

    def test_unknown_name_rejected(self) -> None:
        with self.assertRaises(SystemExit):
            secrets_cli_main(["set", "OTHER_KEY", "v"])

    def test_set_when_disabled_errors(self) -> None:
        with patch.dict(os.environ, {"BRIEFDESK_KEYRING": "0"}):
            _, err, code = self._run(["set", "AI_API_KEY", "v"])
        self.assertEqual(code, 1)
        self.assertIn("错误", err)


if __name__ == "__main__":
    unittest.main()