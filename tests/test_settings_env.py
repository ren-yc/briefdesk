"""启动配置面板测试：暂存存储层 / 解析优先级 / /api/settings/env 与密钥路由。

- 存储层：BRIEFDESK_SETTINGS_FILE 显式路径、读写/删键/整文件移除、来源判定
- 优先级链：暂存文件 > .env > 默认（pydantic-settings 多文件后加载优先）
- 路由：GET 元数据/暂存/来源/插件开关数据；PUT 白名单/类型校验/插件依赖
  互斥复检（409）/原子写/null 恢复；POST/DELETE 密钥（fake keyring 隔离
  真实凭据管理器）
- 前端守卫：index.html 面板与 app.js 端点引用不漂移
"""

import json
import os
import re
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import ClassVar
from unittest.mock import patch

import keyring
from keyring.backend import KeyringBackend
from pydantic import Field, SecretStr
from starlette.testclient import TestClient

import briefdesk.server as srv
from briefdesk import paths, settings_env
from briefdesk.config import Settings
from briefdesk.server import routes_settings_env as settings_routes
from briefdesk.settings_base import KeyringSettingsBase
from briefdesk.settings_env import (
    SOURCE_DOTENV,
    field_env_key,
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

    def test_default_path_delegates_to_paths(self) -> None:
        """无覆盖变量时与 paths.settings_file() 同源（平台目录口径只在 paths 一处）。"""
        with _env_without("BRIEFDESK_SETTINGS_FILE"):
            self.assertEqual(get_settings_file(), paths.settings_file())
            self.assertEqual(get_settings_file(), paths.user_config_dir() / "settings.env")

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

    def test_write_rejects_newline_value_and_keeps_file(self) -> None:
        """写入前断言拒绝换行值：防 KEY=VALUE 行格式被注入
        伪配置行（含密钥名）；失败时原文件保持不变。"""
        write_staged({"LOG_LEVEL": "DEBUG"})
        with self.assertRaises(ValueError):
            write_staged({"LOG_LEVEL": "DEBUG\nAI_API_KEY=sk-evil"})
        self.assertEqual(read_staged(), {"LOG_LEVEL": "DEBUG"})

    def test_write_staged_cleans_tmp_on_failure(self) -> None:
        # 写入/替换失败时残留 .tmp 必须被清理，原文件保持不变（审查回归）
        write_staged({"LOG_LEVEL": "DEBUG"})
        tmp_path = self.staged_path.parent / (self.staged_path.name + ".tmp")
        with (
            patch.object(settings_env.os, "replace", side_effect=OSError("boom")),
            self.assertRaises(OSError),
        ):
            write_staged({"LOG_LEVEL": "INFO"})
        self.assertFalse(tmp_path.exists())
        self.assertEqual(read_staged(), {"LOG_LEVEL": "DEBUG"})

    def test_source_of_priority(self) -> None:
        # 环境变量 > 项目 .env > override（暂存文件）> default
        with tempfile.TemporaryDirectory() as d:
            env_root = Path(d)
            project = env_root / ".env"
            project.write_text(
                "POLL_OVERLAP_SECONDS=99\nLOG_LEVEL=INFO\n", encoding="utf-8"
            )
            with _env_without(
                "LOG_LEVEL", "SERVER_PORT", "IGNORE_SELF", "POLL_OVERLAP_SECONDS"
            ), patch.object(paths, "project_dotenv_path", return_value=project):
                self.assertEqual(source_of("SERVER_PORT"), "default")
                write_staged({"SERVER_PORT": "3001"})
                self.assertEqual(source_of("SERVER_PORT"), "override")
                with patch.dict(os.environ, {"IGNORE_SELF": "false"}):
                    self.assertEqual(source_of("IGNORE_SELF"), "env")
                write_staged({"IGNORE_SELF": "true"})
                with patch.dict(os.environ, {"IGNORE_SELF": "false"}):
                    self.assertEqual(source_of("IGNORE_SELF"), "env")
                self.assertEqual(source_of("POLL_OVERLAP_SECONDS"), "dotenv")
                # 项目 .env 高于暂存：同名键在暂存里也写了，来源仍报 dotenv
                write_staged({"LOG_LEVEL": "DEBUG"})
                self.assertEqual(source_of("LOG_LEVEL"), "dotenv")


class PriorityChainTest(unittest.TestCase):
    def test_staged_file_beats_dotenv_and_env_beats_staged(self) -> None:
        # 暂存文件（后加载）优先于 .env；环境变量优先于暂存文件
        # 排除宿主环境 LOG_LEVEL 干扰（本地可能预置该变量）
        with _env_without("LOG_LEVEL"), tempfile.TemporaryDirectory() as d:
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


class ReasoningEffortConfigTest(unittest.TestCase):
    """推理强度的取值校验：非法值在配置加载期即报错，而不是静默按 auto 处理。"""

    def test_invalid_value_rejected_at_load(self) -> None:
        from pydantic import ValidationError

        from briefdesk.config import Settings

        with self.assertRaises(ValidationError):
            Settings(_env_file=[], AI_REASONING_EFFORT="bogus")


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
        self.assertEqual(len(data["secrets"]), 10)
        self.assertEqual(
            {s["name"] for s in data["secrets"]},
            {
                "AI_API_KEY",
                "EMBED_API_KEY",
                "WEFLOW_API_TOKEN",
                "WEFLOW_IMG_AES_KEY",
                "WEFLOW_IMG_XOR_KEY",
                "WEFLOW_DB_KEYS",
                "WEFLOW_LEGACY_API_TOKEN",
                "QQFLOW_API_TOKEN",
                "QQFLOW_KEY",
                # rag 插件密钥（RAG_ 前缀归插件域，与 WEFLOW_/QQFLOW_ 同级）
                "RAG_API_KEY",
            },
        )

    def _env_items(self) -> dict[str, dict]:
        """GET 一次并把 items 按 key 建索引（覆盖态断言共用）。"""
        data = self.client.get("/api/settings/env").json()
        return {item["key"]: item for item in data["items"]}

    def test_put_stages_and_get_reports_draft_state(self) -> None:
        # 暂存后 source 仍是**启动快照**（本进程还在用启动时的值），草稿与下次启动值
        # 由 staged/expected_* 表达；只有被更高层压住时才置 overridden。
        # 宿主环境若预置 LOG_LEVEL 会改变来源判定，测试内隔离该变量。
        with _env_without("LOG_LEVEL"):
            res = self.client.put(
                "/api/settings/env", json={"items": {"LOG_LEVEL": "DEBUG"}}
            )
            self.assertEqual(res.status_code, 200)
            self.assertEqual(read_staged(), {"LOG_LEVEL": "DEBUG"})
            data = self.client.get("/api/settings/env").json()
            log_level = next(i for i in data["items"] if i["key"] == "LOG_LEVEL")
            self.assertEqual(log_level["staged"], "DEBUG")
            self.assertEqual(log_level["source"], "default")
            self.assertFalse(log_level["overridden"])
            self.assertEqual(log_level["expected_source"], "override")
            self.assertEqual(log_level["expected_value"], "DEBUG")

    def test_draft_blocked_by_project_dotenv_is_marked_overridden(self) -> None:
        """覆盖态属预期语义：草稿会不会在下次启动被更高层压住。"""
        write_staged({"LOG_LEVEL": "DEBUG"})
        with tempfile.TemporaryDirectory() as d:
            project = Path(d) / ".env"
            project.write_text("LOG_LEVEL=INFO\n", encoding="utf-8")
            with _env_without("LOG_LEVEL"), patch.object(
                paths, "project_dotenv_path", return_value=project
            ):
                items = self._env_items()
        item = items["LOG_LEVEL"]
        self.assertEqual(item["staged"], "DEBUG")
        self.assertTrue(item["overridden"])
        self.assertEqual(item["override_source"], "dotenv")
        self.assertEqual(item["override_value"], "INFO")
        self.assertEqual(item["expected_source"], "dotenv")
        self.assertEqual(item["expected_value"], "INFO")

    def test_draft_applies_next_start_when_nothing_overrides(self) -> None:
        write_staged({"LOG_LEVEL": "DEBUG"})
        with _env_without("LOG_LEVEL"):
            item = self._env_items()["LOG_LEVEL"]
        self.assertFalse(item["overridden"])
        self.assertIsNone(item["override_source"])
        self.assertEqual(item["expected_source"], "override")
        self.assertEqual(item["expected_value"], "DEBUG")

    def test_invalid_expected_config_marks_unavailable_without_500(self) -> None:
        """运行中把配置改坏：设置页不得 500，expected_* 标不可用并给脱敏摘要。"""
        write_staged({"SERVER_PORT": "not-a-number"})
        with _env_without("SERVER_PORT"):
            items = self._env_items()
        item = items["SERVER_PORT"]
        self.assertFalse(item["expectedAvailable"])
        self.assertNotIn("not-a-number", item["expectedError"], "摘要不得回显原始值")
        self.assertIsNone(item["expected_value"])
        self.assertIn("LOG_LEVEL", items, "其余字段仍须可展示")

    def test_put_multi_json_array(self) -> None:
        res = self.client.put(
            "/api/settings/env",
            json={"items": {"PLUGINS": json.dumps(["weflow", "qqflow"])}},
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(read_staged()["PLUGINS"], '["weflow","qqflow"]')

    def test_put_rejects_required_plugin_not_enabled(self) -> None:
        """必选但未启用：暂存时就要拦下，不能等到下次启动才中止。

        两个键由面板分别写入，单独看都合法、合起来矛盾；一旦落盘，用户只能在
        启动失败后回头比对两份名单。
        """
        res = self.client.put(
            "/api/settings/env",
            json={
                "items": {
                    "PLUGINS": json.dumps(["weflow"]),
                    "PLUGINS_REQUIRED": json.dumps(["benchmark"]),
                }
            },
        )
        self.assertEqual(res.status_code, 409)
        issues = res.json()["detail"]["issues"]
        self.assertTrue(
            any(
                i["type"] == "required_not_enabled" and i["plugin"] == "benchmark"
                for i in issues
            ),
            issues,
        )
        # 拒绝时不落盘：暂存态保持调用前原样
        self.assertNotIn("PLUGINS_REQUIRED", read_staged())

    def test_put_rejects_dropping_a_required_plugin(self) -> None:
        """已暂存的必选插件，不能被后续写入挤出启用列表。"""
        ok = self.client.put(
            "/api/settings/env",
            json={
                "items": {
                    "PLUGINS": json.dumps(["weflow", "benchmark"]),
                    "PLUGINS_REQUIRED": json.dumps(["benchmark"]),
                }
            },
        )
        self.assertEqual(ok.status_code, 200)
        res = self.client.put(
            "/api/settings/env",
            json={"items": {"PLUGINS": json.dumps(["weflow"])}},
        )
        self.assertEqual(res.status_code, 409)
        issues = res.json()["detail"]["issues"]
        self.assertTrue(
            any(
                i["type"] == "required_not_enabled" and i["plugin"] == "benchmark"
                for i in issues
            ),
            issues,
        )
        # 写入被拒：先前的暂存态原样保留（必选仍在，插件仍在启用列表）
        staged = read_staged()
        self.assertEqual(staged["PLUGINS_REQUIRED"], json.dumps(["benchmark"]))
        self.assertIn("benchmark", json.loads(staged["PLUGINS"]))

    def test_put_rejects_unknown_required_plugin(self) -> None:
        """必选名单里的未知名同样要在暂存期拦下（启动期只会说「未发现」）。

        没有管理器可校验时（测试进程常见）拿不到插件清单，类型会落到
        「必选但未启用」那一类——关键是**拒绝并点名**，而不是放行到启动期才炸。
        """
        res = self.client.put(
            "/api/settings/env",
            json={"items": {"PLUGINS_REQUIRED": json.dumps(["no-such-plugin"])}},
        )
        self.assertEqual(res.status_code, 409)
        issues = res.json()["detail"]["issues"]
        self.assertTrue(
            any(i["plugin"] == "no-such-plugin" for i in issues),
            issues,
        )

    def test_put_accepts_required_plugin_when_enabled(self) -> None:
        """自洽的组合照常放行：必选插件同时在启用列表里。"""
        res = self.client.put(
            "/api/settings/env",
            json={
                "items": {
                    "PLUGINS": json.dumps(["weflow", "benchmark"]),
                    "PLUGINS_REQUIRED": json.dumps(["benchmark"]),
                }
            },
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(read_staged()["PLUGINS_REQUIRED"], json.dumps(["benchmark"]))

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

    def test_ai_reasoning_effort_is_select_with_all_values(self) -> None:
        """推理强度必须渲染成下拉：options 按环境键名登记，否则只是文本框。"""
        from briefdesk.config import REASONING_EFFORT_VALUES

        res = self.client.get("/api/settings/env")
        item = next(
            i for i in res.json()["items"] if i["key"] == "AI_REASONING_EFFORT"
        )
        self.assertEqual(item["type"], "select")
        self.assertEqual(item["options"], list(REASONING_EFFORT_VALUES))

    def test_ai_reasoning_effort_rejects_unknown_value(self) -> None:
        res = self.client.put(
            "/api/settings/env", json={"items": {"AI_REASONING_EFFORT": "bogus"}}
        )
        self.assertEqual(res.status_code, 422)

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

    def test_put_response_carries_fresh_item_state(self) -> None:
        # 行级贴片依据：响应携带受影响键的最终 staged/source（与 GET 同口径）；
        # source 依赖服务端解析链，客户端无法自行推算
        with _env_without("LOG_LEVEL"):
            res = self.client.put(
                "/api/settings/env", json={"items": {"LOG_LEVEL": "DEBUG"}}
            )
            self.assertEqual(res.status_code, 200)
            fresh = res.json()["items"]["LOG_LEVEL"]
            self.assertEqual(fresh["staged"], "DEBUG")
            self.assertEqual(fresh["source"], "default")  # 启动快照（本进程仍用默认值）
            self.assertFalse(fresh["overridden"])
            self.assertEqual(fresh["expected_source"], "override")
            self.assertEqual(fresh["expected_value"], "DEBUG")
            # 恢复默认：staged 回 None、source 脱离 override
            # （本地有 .env 时为 dotenv，CI 无 .env 时为 default，均合法）
            res = self.client.put(
                "/api/settings/env", json={"items": {"LOG_LEVEL": None}}
            )
            self.assertEqual(res.status_code, 200)
            item = res.json()["items"]["LOG_LEVEL"]
            self.assertIsNone(item["staged"])
            self.assertIn(item["source"], ("default", "dotenv"))

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
            # 钥匙串写入成功即两枚为真（行级贴片依据）
            self.assertIs(res.json()["configured"], True)
            self.assertIs(res.json()["keyringConfigured"], True)
            data = self.client.get("/api/settings/env").json()
            ai = next(s for s in data["secrets"] if s["name"] == "AI_API_KEY")
            self.assertTrue(ai["configured"])
            self.assertTrue(ai["keyringConfigured"])
            # 明文永不回传
            self.assertNotIn(_VALID_SECRET, json.dumps(data))
            res = self.client.delete("/api/settings/secrets/AI_API_KEY")
            self.assertEqual(res.status_code, 200)
            # 删除后 keyringConfigured 恒 False；configured 取决于是否另有
            # 环境变量/.env 配置（本用例核心 schema 快照 configured=False）
            self.assertIs(res.json()["keyringConfigured"], False)
            self.assertIs(res.json()["configured"], False)
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

    def test_keyring_empty_string_not_configured(self) -> None:
        # keyring 空串条目不得判「已配置」——`secrets set X ""`
        # 会在钥匙串留下空串，is not None 会误报已配置，与实际解析链相反。
        core_schema = [
            {**item, "configured": False}
            for item in settings_routes._CORE_SECRET_SCHEMA
        ]
        with patch.object(settings_routes, "_CORE_SECRET_SCHEMA", core_schema), patch.object(
            settings_routes, "get_secret", return_value=""
        ):
            data = self.client.get("/api/settings/env").json()
        ai = next(s for s in data["secrets"] if s["name"] == "AI_API_KEY")
        self.assertFalse(ai["configured"])
        self.assertFalse(ai["keyringConfigured"], "空串条目应判未配置")

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


class PluginToggleRoutesTest(StagedFileTestCase):
    """「插件」面板路由：GET plugins 数组（核心/可选 + 期望启用态）、
    PUT PLUGINS 的依赖/互斥复检（409）与 PLUGINS_DISABLED 键淘汰。"""

    def setUp(self) -> None:
        super().setUp()
        self.client = TestClient(
            srv.app,
            base_url="http://localhost",
            headers={"Origin": "http://localhost"},
        )

    _META: ClassVar[list[dict]] = [
        {
            "name": "weflow",
            "version": "1.0.0",
            "dependencies": [],
            "conflicts": ["weflow-legacy"],
            "core": False,
        },
        {
            "name": "ai_provider",
            "version": "1.0.0",
            "dependencies": [],
            "conflicts": [],
            "core": True,
        },
    ]

    def test_get_returns_plugin_toggle_data(self) -> None:
        infos = [
            {"name": "weflow", "version": "1.0.0", "status": "disabled",
             "reason": "未启用：在 PLUGINS 中列出或经「插件」面板开关即可启用",
             "has_frontend": False, "core": False},
            {"name": "ai_provider", "version": "1.0.0", "status": "loaded",
             "reason": "", "has_frontend": False, "core": True},
            # 加载失败记录（无插件实例、无元数据）应兜底展示
            {"name": "ghost", "version": "", "status": "failed",
             "reason": "entry point 加载失败", "has_frontend": False, "core": False},
        ]
        # 隔离宿主环境：config 单例回落值与 PLUGINS 来源判定均指向干净默认
        with patch.object(
            settings_routes, "get_plugin_meta", return_value=self._META
        ), patch.object(
            settings_routes, "get_plugins_info", return_value=infos
        ), patch.object(
            settings_routes, "config", Settings(
                plugins=[], plugins_required=[], plugin_path=""
            )
        ), patch.object(
            settings_routes, "_plugins_source", return_value="default"
        ):
            data = self.client.get("/api/settings/env").json()
        plugins = {p["name"]: p for p in data["plugins"]}
        self.assertIs(plugins["ai_provider"]["core"], True)
        self.assertIs(plugins["ai_provider"]["enabled"], True)  # 核心恒启用
        self.assertIs(plugins["weflow"]["enabled"], False)  # 默认 PLUGINS=[]，未列出即禁用
        self.assertEqual(plugins["weflow"]["status"], "disabled")
        self.assertIn("ghost", plugins)  # 失败记录兜底可见
        self.assertEqual(plugins["ghost"]["status"], "failed")
        self.assertEqual(data["pluginsSource"], "default")
        # PLUGINS 项带 hidden 标记（由插件面板编辑）；PLUGINS_DISABLED 已淘汰
        plugins_item = next(i for i in data["items"] if i["key"] == "PLUGINS")
        self.assertIs(plugins_item["hidden"], True)
        self.assertNotIn("PLUGINS_DISABLED", {i["key"] for i in data["items"]})
        # 芯片选项不含通配符
        self.assertNotIn("*", data["pluginOptions"])

    def test_get_enabled_follows_staged_value(self) -> None:
        # 排除宿主环境 PLUGINS（env > .env > 暂存 > 默认）：来源判定若被宿主
        # PLUGINS 抢占，pluginsSource 会返回 'env' 而非本用例期望的 'default'
        # （本进程没有被更高层压住，开关重启后生效）
        with _env_without("PLUGINS"):
            write_staged({"PLUGINS": '["weflow"]'})
            with patch.object(
                settings_routes, "get_plugin_meta", return_value=self._META
            ), patch.object(settings_routes, "get_plugins_info", return_value=[]):
                data = self.client.get("/api/settings/env").json()
        self.assertEqual(
            {p["name"]: p["enabled"] for p in data["plugins"]},
            {"weflow": True, "ai_provider": True},
        )
        self.assertEqual(data["pluginsSource"], "default")
        self.assertFalse(data["pluginsOverridden"])

    def test_put_plugins_conflict_rejected_409(self) -> None:
        issues = [
            {
                "type": "conflict",
                "plugin": "weflow",
                "detail": "与 weflow-legacy 互斥，两者不可同时启用",
            }
        ]
        with patch.object(
            settings_routes, "validate_plugin_selection", return_value=issues
        ):
            res = self.client.put(
                "/api/settings/env",
                json={"items": {"PLUGINS": json.dumps(["weflow", "weflow-legacy"])}},
            )
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.json()["detail"]["issues"], issues)
        self.assertNotIn("PLUGINS", read_staged())  # 校验失败不落盘

    def test_put_plugins_valid_selection_stages(self) -> None:
        with patch.object(
            settings_routes, "validate_plugin_selection", return_value=[]
        ):
            res = self.client.put(
                "/api/settings/env",
                json={"items": {"PLUGINS": json.dumps(["weflow"])}},
            )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(read_staged()["PLUGINS"], '["weflow"]')

    def test_put_plugins_disabled_key_rejected(self) -> None:
        # PLUGINS_DISABLED 已随通配语义一并移除：键不再在白名单
        res = self.client.put(
            "/api/settings/env",
            json={"items": {"PLUGINS_DISABLED": json.dumps(["weflow-legacy"])}},
        )
        self.assertEqual(res.status_code, 422)
        self.assertEqual(read_staged(), {})


class FrontendGuardTest(unittest.TestCase):
    """前端面板与端点引用守卫（防漂移）。"""

    def test_index_html_has_env_panel(self) -> None:
        html = (_REPO_ROOT / "briefdesk" / "ui" / "index.html").read_text(encoding="utf-8")
        self.assertIn('data-panel="env"', html)
        self.assertIn('id="env-items"', html)
        self.assertIn('id="env-secrets"', html)
        self.assertIn('id="env-file-path"', html)

    def test_index_html_has_plugins_toggle_panel(self) -> None:
        html = (_REPO_ROOT / "briefdesk" / "ui" / "index.html").read_text(encoding="utf-8")
        self.assertIn('data-panel="plugins"', html)
        self.assertIn('id="plugins-list"', html)

    def test_app_js_references_env_endpoints(self) -> None:
        js = (_REPO_ROOT / "briefdesk" / "ui" / "app.js").read_text(encoding="utf-8")
        self.assertIn('"/api/settings/env"', js)
        self.assertIn('"/api/settings/secrets"', js)
        self.assertIn("data-env-restore", js)
        self.assertIn("data-sec-set", js)
        self.assertIn("keyringConfigured", js)
        self.assertIn('class="env-input"', js)

    def test_app_js_references_plugin_toggle_flow(self) -> None:
        js = (_REPO_ROOT / "briefdesk" / "ui" / "app.js").read_text(encoding="utf-8")
        self.assertIn("data-plugin-toggle", js)
        self.assertIn("_onPluginToggle", js)
        self.assertIn("renderPluginToggles", js)
        self.assertIn("_pluginChanges", js)
        # 多选控件不再注入通配符选项
        self.assertNotIn('opts.unshift("*")', js)


#: `.env.example` 允许保持主动赋值的键：密钥 + 消息源必填项（新增须在此显式登记）
_TEMPLATE_ALLOWED_ACTIVE = {
    "AI_API_KEY",
    "EMBED_API_KEY",
    "RAG_API_KEY",
    "WEFLOW_LEGACY_API_TOKEN",
    "QQFLOW_API_TOKEN",
    "QQFLOW_KEY",
    "WEFLOW_WXID",
    "QQFLOW_QQ",
}


class EnvExampleTemplateTest(unittest.TestCase):
    """模板守卫：`.env.example` 的主动赋值键只允许显式登记的例外。

    为什么：显式赋值的键高于 UI 暂存值，而 `cp .env.example .env` 是文档教的第一步——
    钉死 UI 可编辑键会让「保存了却不生效」变成默认体验（纯假覆盖）。
    """

    def _template(self) -> str:
        return (_REPO_ROOT / ".env.example").read_text(encoding="utf-8")

    def test_active_keys_are_registered_exceptions(self) -> None:
        actives = {
            match.group(1)
            for line in self._template().splitlines()
            if (match := re.match(r"^([A-Z][A-Z0-9_]*)=", line))
        }
        self.assertEqual(
            actives - _TEMPLATE_ALLOWED_ACTIVE,
            set(),
            "主动赋值键超出登记例外：会盖住 UI 暂存值（改成注释行或在例外清单登记）",
        )
        self.assertTrue(actives, "模板仍应保留密钥/消息源必填项作为主动赋值")

    def test_header_describes_new_priority(self) -> None:
        header = "\n".join(self._template().splitlines()[:24])
        self.assertIn("项目 .env > UI 暂存文件", header)
        self.assertIn("空串", header, "KEY= 空值会覆盖默认值，头部必须写明")

    def test_no_legacy_database_usage_guidance(self) -> None:
        text = self._template()
        self.assertNotIn("手动移动", text, "不得给旧库操作指引")
        self.assertNotIn("DB_PATH=C:", text, "不得出现旧库路径示例")
        self.assertIn("旧库不会被自动发现", text, "只保留边界说明")


if __name__ == "__main__":
    unittest.main()


class SourceLayerTest(StagedFileTestCase):
    """来源判定的边界：大小写、空串、显式层序（回归：小写键曾被漏看）。"""

    def test_lowercase_key_is_reported_as_dotenv(self) -> None:
        # 已复现的漂移：解析按 case_sensitive=False 采纳小写键，判定却精确匹配大写，
        # 结果「显示的来源」与「实际生效值」互相矛盾。
        with tempfile.TemporaryDirectory() as d:
            project = Path(d) / ".env"
            project.write_text("db_path=lower-value\n", encoding="utf-8")
            with patch.object(paths, "project_dotenv_path", return_value=project):
                self.assertEqual(source_of("DB_PATH"), "dotenv")

    def test_empty_value_counts_as_provided(self) -> None:
        # `KEY=` 会以空串覆盖默认值（普通 str 字段得到 ''），因此算「已提供」
        with tempfile.TemporaryDirectory() as d:
            project = Path(d) / ".env"
            project.write_text("LOG_LEVEL=\n", encoding="utf-8")
            with patch.object(paths, "project_dotenv_path", return_value=project):
                self.assertEqual(source_of("LOG_LEVEL"), "dotenv")

    def test_explicit_layers_replace_default_order(self) -> None:
        # 显式 `_env_file` 构造时，展示侧也要按那份文件判定（不再看默认两层）
        with tempfile.TemporaryDirectory() as d:
            explicit = Path(d) / "explicit.env"
            explicit.write_text("SERVER_PORT=3111\n", encoding="utf-8")
            self.assertEqual(
                source_of("SERVER_PORT", [(SOURCE_DOTENV, explicit)]), "dotenv"
            )
            self.assertEqual(source_of("SERVER_PORT"), "default")

    def test_env_lookup_is_case_insensitive(self) -> None:
        with _env_without("SERVER_PORT"), patch.dict(os.environ, {"server_port": "3222"}):
            self.assertEqual(source_of("SERVER_PORT"), "env")


class FieldEnvKeyTest(unittest.TestCase):
    """字段 → 环境变量键：alias 优先，否则 env_prefix + 字段名大写。"""

    def test_alias_wins(self) -> None:
        self.assertEqual(field_env_key(Settings, "db_path"), "DB_PATH")

    def test_plugin_prefix_applied(self) -> None:
        from briefdesk.plugins.benchmark.config import BenchmarkSettings

        self.assertEqual(
            field_env_key(BenchmarkSettings, "keep_runs"), "BENCHMARK_KEEP_RUNS"
        )


class TwoLayerPriorityTest(StagedFileTestCase):
    """两个独立 dotenv 来源：项目 .env 高于暂存；wheel 模式只剩暂存。"""

    def test_project_dotenv_beats_staged(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            project = Path(d) / ".env"
            project.write_text("LOG_LEVEL=INFO\n", encoding="utf-8")
            write_staged({"LOG_LEVEL": "DEBUG"})
            with _env_without("LOG_LEVEL"), patch.object(
                paths, "project_dotenv_path", return_value=project
            ):
                self.assertEqual(Settings().log_level, "INFO")
                self.assertEqual(source_of("LOG_LEVEL"), "dotenv")

    def test_wheel_mode_reads_staged_only(self) -> None:
        write_staged({"LOG_LEVEL": "DEBUG"})
        with _env_without("LOG_LEVEL"), patch.object(
            paths, "project_dotenv_path", return_value=None
        ):
            self.assertEqual(Settings().log_level, "DEBUG")
            self.assertEqual(source_of("LOG_LEVEL"), "override")


class EnvFileContractTest(unittest.TestCase):
    """显式 `_env_file`（含显式 None）完全取代两层；环境变量仍在其之上。"""

    class _Probe(KeyringSettingsBase):
        value: str = Field(default="default", alias="PROBE_VALUE")

    def test_five_forms(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            project = root / ".env"
            project.write_text("PROBE_VALUE=project\n", encoding="utf-8")
            staged = root / "settings.env"
            staged.write_text("PROBE_VALUE=staged\n", encoding="utf-8")
            explicit = root / "explicit.env"
            explicit.write_text("PROBE_VALUE=explicit\n", encoding="utf-8")
            with (
                _env_without("PROBE_VALUE"),
                patch.dict(os.environ, {"BRIEFDESK_SETTINGS_FILE": str(staged)}),
                patch.object(paths, "project_dotenv_path", return_value=project),
            ):
                self.assertEqual(self._Probe().value, "project")
                self.assertEqual(self._Probe(_env_file=str(explicit)).value, "explicit")
                self.assertEqual(self._Probe(_env_file=explicit).value, "explicit")
                self.assertEqual(self._Probe(_env_file=[project, staged]).value, "staged")
                self.assertEqual(self._Probe(_env_file=None).value, "default")
                with patch.dict(os.environ, {"PROBE_VALUE": "env"}):
                    self.assertEqual(self._Probe().value, "env")
                    self.assertEqual(self._Probe(_env_file=str(explicit)).value, "env")


class AllSettingsTwoLayerTest(StagedFileTestCase):
    """六个 Settings 类同口径：项目 .env 高于暂存（env_prefix 各自生效）。"""

    def _cases(self):
        from briefdesk.plugins.benchmark.config import BenchmarkSettings
        from briefdesk.plugins.qqflow.config import QqFlowSettings
        from briefdesk.plugins.rag.config import RagSettings
        from briefdesk.plugins.weflow.config import WeFlowSettings
        from briefdesk.plugins.weflow_legacy.config import WeFlowLegacySettings

        return [
            (Settings, "LOG_LEVEL", "log_level", "WARNING"),
            (WeFlowSettings, "WEFLOW_API_BASE", "api_base", "http://proj:1"),
            (WeFlowLegacySettings, "WEFLOW_LEGACY_API_BASE", "api_base", "http://proj:2"),
            (QqFlowSettings, "QQFLOW_API_BASE", "api_base", "http://proj:3"),
            (RagSettings, "RAG_MODEL", "model", "model-proj"),
            (BenchmarkSettings, "BENCHMARK_RUN_STALL_SECONDS", "run_stall_seconds", "1234"),
        ]

    def test_project_dotenv_beats_staged_for_every_class(self) -> None:
        for model, key, attr, raw in self._cases():
            with self.subTest(model=model.__name__):
                self.assertEqual(field_env_key(model, attr), key)
                with tempfile.TemporaryDirectory() as d:
                    project = Path(d) / ".env"
                    project.write_text(f"{key}={raw}\n", encoding="utf-8")
                    # 暂存写一个必然不同的合法值：若优先级反了就会读到它
                    write_staged({key: "0" if raw.isdigit() else "http://staged"})
                    with (
                        _env_without(key),
                        patch.object(paths, "project_dotenv_path", return_value=project),
                    ):
                        parsed = getattr(model(), attr)
                        expected = int(raw) if raw.isdigit() else raw
                        self.assertEqual(parsed, expected, key)
