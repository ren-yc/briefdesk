"""启动配置面板测试：暂存存储层 / 解析优先级 / /api/settings/env 与密钥路由。

- 存储层：BRIEFDESK_SETTINGS_FILE 显式路径、读写/删键/整文件移除、来源判定
- 优先级链：暂存文件 > .env > 默认（pydantic-settings 多文件后加载优先）
- 路由：GET 元数据/暂存/来源；PUT 白名单/类型校验/原子写/null 恢复；
  POST/DELETE 密钥（fake keyring 隔离真实凭据管理器）
- 前端守卫：index.html 面板与 app.js 端点引用不漂移
"""

import json
import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import keyring
from keyring.backend import KeyringBackend
from pydantic import SecretStr
from starlette.testclient import TestClient

import briefdesk.server as srv
from briefdesk import settings_env
from briefdesk.config import Settings
from briefdesk.server import routes_settings_env as settings_routes
from briefdesk.settings_env import (
    get_settings_file,
    read_staged,
    source_of,
    write_staged,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent


@contextmanager
def _env_without(*names: str):
    """临时移除指定环境变量（用例内断言来源判定时排除宿主环境干扰）。"""
    saved = {name: os.environ.pop(name, None) for name in names}
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

_VALID_SECRET = "sk-abcdef" + "1234567890abcdef1234567890"


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


class StagedFileTestCase(unittest.TestCase):
    """基类：staged 文件指向临时目录 + fake keyring（用例级隔离）。"""

    _prev_keyring: KeyringBackend | None = None

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.staged_path = Path(self._tmp.name) / "settings.env"
        self._env_patch = patch.dict(
            os.environ, {"BRIEFDESK_SETTINGS_FILE": str(self.staged_path)}
        )
        self._env_patch.start()
        self.addCleanup(self._env_patch.stop)
        try:
            self._prev_keyring = keyring.get_keyring()
        except keyring.errors.NoKeyringError:
            self._prev_keyring = None
        keyring.set_keyring(FakeKeyringBackend())  # type: ignore[arg-type]
        self.addCleanup(self._restore_keyring)

    def _restore_keyring(self) -> None:
        if self._prev_keyring is not None:
            keyring.set_keyring(self._prev_keyring)
        else:
            keyring.set_keyring(keyring.backends.fail.Keyring())


class SettingsFileTest(StagedFileTestCase):
    def test_explicit_env_override_file_path(self) -> None:
        self.assertEqual(get_settings_file(), self.staged_path)

    def test_write_read_roundtrip_and_delete_key(self) -> None:
        write_staged({"LOG_LEVEL": "DEBUG", "SERVER_PORT": "3001"})
        self.assertEqual(
            read_staged(), {"LOG_LEVEL": "DEBUG", "SERVER_PORT": "3001"}
        )
        write_staged({"LOG_LEVEL": None})
        self.assertEqual(read_staged(), {"SERVER_PORT": "3001"})

    def test_removing_all_keys_deletes_file(self) -> None:
        write_staged({"LOG_LEVEL": "DEBUG"})
        write_staged({"LOG_LEVEL": None})
        self.assertFalse(self.staged_path.exists())

    def test_write_keeps_existing_keys(self) -> None:
        write_staged({"LOG_LEVEL": "DEBUG"})
        write_staged({"SERVER_PORT": "3001"})
        self.assertEqual(
            read_staged(), {"LOG_LEVEL": "DEBUG", "SERVER_PORT": "3001"}
        )

    def test_write_value_containing_equals_sign(self) -> None:
        write_staged({"DB_PATH": r"C:\data\app.db?x=1"})
        self.assertEqual(read_staged()["DB_PATH"], r"C:\data\app.db?x=1")

    def test_source_of_priority(self) -> None:
        # 环境变量 > override（暂存文件）> .env > default
        with tempfile.TemporaryDirectory() as d:
            env_root = Path(d)
            (env_root / ".env").write_text(
                "POLL_OVERLAP_SECONDS=99\n", encoding="utf-8"
            )
            with _env_without("LOG_LEVEL", "SERVER_PORT", "IGNORE_SELF"), patch.object(
                settings_env, "PROJECT_ROOT", env_root
            ):
                self.assertEqual(source_of("LOG_LEVEL"), "default")
                write_staged({"SERVER_PORT": "3001"})
                self.assertEqual(source_of("SERVER_PORT"), "override")
                with patch.dict(os.environ, {"IGNORE_SELF": "false"}):
                    self.assertEqual(source_of("IGNORE_SELF"), "env")
                write_staged({"IGNORE_SELF": "true"})
                with patch.dict(os.environ, {"IGNORE_SELF": "false"}):
                    self.assertEqual(source_of("IGNORE_SELF"), "env")
                self.assertEqual(source_of("POLL_OVERLAP_SECONDS"), "dotenv")
                self.assertEqual(source_of("LOG_LEVEL"), "default")


class PriorityChainTest(unittest.TestCase):
    def test_staged_file_beats_dotenv_and_env_beats_staged(self) -> None:
        # 暂存文件（后加载）优先于 .env；环境变量优先于暂存文件
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            env_a = root / "a.env"
            env_b = root / "b.env"
            env_a.write_text("LOG_LEVEL=INFO\nSERVER_PORT=3001\n", encoding="utf-8")
            env_b.write_text("LOG_LEVEL=DEBUG\n", encoding="utf-8")
            settings = Settings(_env_file=[env_a, env_b])
            self.assertEqual(settings.log_level, "DEBUG")
            self.assertEqual(settings.server_port, 3001)  # 未暂存 → 下层生效
            with patch.dict(os.environ, {"LOG_LEVEL": "WARNING"}):
                settings2 = Settings(_env_file=[env_a, env_b])
                self.assertEqual(settings2.log_level, "WARNING")


class EnvRoutesTest(StagedFileTestCase):
    def setUp(self) -> None:
        super().setUp()
        # 中间件 CSRF 收口后，/api 变更接口要求 Origin/Referer 至少其一；
        # 默认带同源 Origin 模拟浏览器 fetch 行为（与 test_server._client 对齐）
        self.client = TestClient(
            srv.app,
            base_url="http://localhost",
            headers={"Origin": "http://localhost"},
        )

    def test_get_env_returns_schema_and_state(self) -> None:
        res = self.client.get("/api/settings/env")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["filePath"], str(self.staged_path))
        keys = [i["key"] for i in data["items"]]
        self.assertIn("LOG_LEVEL", keys)
        for item in data["items"]:
            self.assertIn(item["source"], ("default", "dotenv", "env", "override"))
            self.assertIn("current", item)
            self.assertIn("staged", item)
        self.assertEqual(len(data["secrets"]), 5)
        self.assertEqual(
            {s["name"] for s in data["secrets"]},
            {
                "AI_API_KEY",
                "EMBED_API_KEY",
                "WEFLOW_API_TOKEN",
                "QQFLOW_API_TOKEN",
                "QQFLOW_KEY",
            },
        )

    def test_put_stages_and_get_reports_override(self) -> None:
        res = self.client.put(
            "/api/settings/env", json={"items": {"LOG_LEVEL": "DEBUG"}}
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(read_staged(), {"LOG_LEVEL": "DEBUG"})
        data = self.client.get("/api/settings/env").json()
        log_level = next(i for i in data["items"] if i["key"] == "LOG_LEVEL")
        self.assertEqual(log_level["staged"], "DEBUG")
        self.assertEqual(log_level["source"], "override")

    def test_put_multi_json_array(self) -> None:
        res = self.client.put(
            "/api/settings/env",
            json={"items": {"PLUGINS": json.dumps(["*", "benchmark"])}},
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(read_staged()["PLUGINS"], '["*","benchmark"]')

    def test_dynamic_plugin_schema_is_rendered_validated_and_saved(self) -> None:
        dynamic = [
            {
                "key": "RAG_TOP_K",
                "type": "number",
                "numberKind": "integer",
                "min": 1,
                "label": "向量召回条数",
                "plugin": "rag",
                "pluginStatus": "loaded",
                "current": 12,
                "secret": False,
            },
            {
                "key": "RAG_API_KEY",
                "type": "text",
                "label": "RAG API Key",
                "plugin": "rag",
                "pluginStatus": "loaded",
                "secret": True,
            },
        ]
        with patch.object(settings_routes, "get_settings_schema", return_value=dynamic):
            data = self.client.get("/api/settings/env").json()
            item = next(i for i in data["items"] if i["key"] == "RAG_TOP_K")
            self.assertEqual(item["plugin"], "rag")
            self.assertEqual(item["current"], 12)
            self.assertIn(
                {
                    "name": "RAG_API_KEY",
                    "label": "RAG API Key",
                    "plugin": "rag",
                    "configured": False,
                    "keyringConfigured": False,
                },
                data["secrets"],
            )
            res = self.client.put(
                "/api/settings/env", json={"items": {"RAG_TOP_K": "20"}}
            )
            self.assertEqual(res.status_code, 200)
            self.assertEqual(read_staged()["RAG_TOP_K"], "20")
            self.assertEqual(
                self.client.put(
                    "/api/settings/env", json={"items": {"RAG_TOP_K": "0"}}
                ).status_code,
                422,
            )
            self.assertEqual(
                self.client.post(
                    "/api/settings/secrets",
                    json={"name": "RAG_API_KEY", "value": "fake-rag-key"},
                ).status_code,
                200,
            )

    def test_empty_manager_schema_hides_plugin_secrets(self) -> None:
        with patch.object(settings_routes, "get_settings_schema", return_value=[]), patch.object(
            settings_routes, "has_settings_schema_callback", return_value=True
        ):
            data = self.client.get("/api/settings/env").json()
        self.assertEqual(
            {secret["name"] for secret in data["secrets"]},
            {"AI_API_KEY", "EMBED_API_KEY"},
        )

    def test_core_schema_is_derived_from_settings_fields(self) -> None:
        keys = {
            item["key"] for item in self.client.get("/api/settings/env").json()["items"]
        }
        expected = {
            str(field.alias)
            for field in Settings.model_fields.values()
            if field.alias and field.annotation is not SecretStr
        }
        self.assertTrue(expected <= keys)

    def test_put_rejects_unknown_key(self) -> None:
        res = self.client.put(
            "/api/settings/env", json={"items": {"NOT_A_REAL_KEY": "x"}}
        )
        self.assertEqual(res.status_code, 422)

    def test_put_rejects_invalid_number(self) -> None:
        res = self.client.put(
            "/api/settings/env", json={"items": {"SERVER_PORT": "99999"}}
        )
        self.assertEqual(res.status_code, 422)
        res = self.client.put(
            "/api/settings/env", json={"items": {"SERVER_PORT": "abc"}}
        )
        self.assertEqual(res.status_code, 422)
        self.assertEqual(read_staged(), {})

    def test_put_rejects_invalid_boolean_and_select(self) -> None:
        res = self.client.put(
            "/api/settings/env", json={"items": {"IGNORE_SELF": "maybe"}}
        )
        self.assertEqual(res.status_code, 422)
        res = self.client.put(
            "/api/settings/env", json={"items": {"LOG_LEVEL": "VERBOSE"}}
        )
        self.assertEqual(res.status_code, 422)

    def test_put_rejects_non_string_value(self) -> None:
        res = self.client.put("/api/settings/env", json={"items": {"LOG_LEVEL": 3}})
        self.assertEqual(res.status_code, 422)

    def test_put_null_restores_default(self) -> None:
        self.client.put("/api/settings/env", json={"items": {"LOG_LEVEL": "DEBUG"}})
        res = self.client.put(
            "/api/settings/env", json={"items": {"LOG_LEVEL": None}}
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(read_staged(), {})

    def test_secret_set_get_delete(self) -> None:
        # 测试只验证 keyring 的写删；宿主项目 .env 可能有真实配置，需排除其
        # 对“删除 keyring 后仍已配置”的有效影响。
        core_schema = [
            {**item, "configured": False}
            for item in settings_routes._CORE_SECRET_SCHEMA
        ]
        with patch.object(settings_routes, "_CORE_SECRET_SCHEMA", core_schema):
            res = self.client.post(
                "/api/settings/secrets",
                json={"name": "AI_API_KEY", "value": _VALID_SECRET},
            )
            self.assertEqual(res.status_code, 200)
            data = self.client.get("/api/settings/env").json()
            ai = next(s for s in data["secrets"] if s["name"] == "AI_API_KEY")
            self.assertTrue(ai["configured"])
            self.assertTrue(ai["keyringConfigured"])
            # 明文永不回传
            self.assertNotIn(_VALID_SECRET, json.dumps(data))
            res = self.client.delete("/api/settings/secrets/AI_API_KEY")
            self.assertEqual(res.status_code, 200)
            ai = next(
                s
                for s in self.client.get("/api/settings/env").json()["secrets"]
                if s["name"] == "AI_API_KEY"
            )
            self.assertFalse(ai["configured"])
            self.assertFalse(ai["keyringConfigured"])
            # 幂等删除
            self.assertEqual(
                self.client.delete("/api/settings/secrets/AI_API_KEY").status_code,
                200,
            )

    def test_secret_separates_effective_and_keyring_configuration(self) -> None:
        core_schema = [
            {**item, "configured": item["key"] == "AI_API_KEY"}
            for item in settings_routes._CORE_SECRET_SCHEMA
        ]
        with patch.object(settings_routes, "_CORE_SECRET_SCHEMA", core_schema), patch.object(
            settings_routes, "get_secret", return_value=None
        ):
            data = self.client.get("/api/settings/env").json()
        ai = next(s for s in data["secrets"] if s["name"] == "AI_API_KEY")
        self.assertTrue(ai["configured"])
        self.assertFalse(ai["keyringConfigured"])

    def test_secret_rejects_unknown_name_and_empty_value(self) -> None:
        res = self.client.post(
            "/api/settings/secrets", json={"name": "OTHER", "value": "x"}
        )
        self.assertEqual(res.status_code, 422)
        res = self.client.post(
            "/api/settings/secrets", json={"name": "AI_API_KEY", "value": ""}
        )
        self.assertEqual(res.status_code, 422)
        self.assertEqual(
            self.client.delete("/api/settings/secrets/OTHER").status_code, 422
        )


class FrontendGuardTest(unittest.TestCase):
    """前端面板与端点引用守卫（防漂移）。"""

    def test_index_html_has_env_panel(self) -> None:
        html = (_REPO_ROOT / "ui" / "index.html").read_text(encoding="utf-8")
        self.assertIn('data-panel="env"', html)
        self.assertIn('id="env-items"', html)
        self.assertIn('id="env-secrets"', html)
        self.assertIn('id="env-file-path"', html)

    def test_app_js_references_env_endpoints(self) -> None:
        js = (_REPO_ROOT / "ui" / "app.js").read_text(encoding="utf-8")
        self.assertIn('"/api/settings/env"', js)
        self.assertIn('"/api/settings/secrets"', js)
        self.assertIn("data-env-restore", js)
        self.assertIn("data-sec-set", js)
        self.assertIn("keyringConfigured", js)
        self.assertIn('class="env-input"', js)


if __name__ == "__main__":
    unittest.main()
