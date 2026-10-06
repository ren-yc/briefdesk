"""启动配置解析与写入契约的第二轮回归。

覆盖四条行为契约（每条对应一个已实测的失败模式，回归以「真实入口」断言）：

- 名单键逐层解码判可用性、胜出值判类型：高层合法 + 低层坏 JSON 必须显式
  不可用（真实 Settings() 会在低层解码失败），坏类型被覆盖时按合并语义放行；
- 覆盖态方向：来源判定必须返回真正压住该键的层，运行中新增高层来源时
  开关随之禁用；
- 预检键匹配与真实 dotenv 解析同口径（大小写不敏感）；
- 整值模板草稿（值来自变量展开）进入预检，落盘原文、回读展开；换行与
  dotenv 行内注释起点仍拒绝；
- 暂存提交失败显式 500：删除/写入/替换失败不得谎报 ok:true，写后重读失败
  明确「已写入、状态未知」。

测试数据全部虚构（键名带 SYNTH/BD_TEST 前缀，值为合成 JSON），不触碰
真实配置、密钥环与用户目录。
"""

import json
import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from starlette.testclient import TestClient

import briefdesk.server as srv
from briefdesk import paths, settings_env
from briefdesk.server import routes_settings_env as settings_routes
from briefdesk.settings_env import (
    SOURCE_DEFAULT,
    SOURCE_DOTENV,
    SOURCE_ENV,
    SOURCE_OVERRIDE,
    composed_value,
    resolve_alias_layers,
    write_staged,
)
from tests.test_settings_env import (
    StagedFileTestCase,
    _env_without,
)


@contextmanager
def _synth_env(values: dict[str, str]):
    """按用例注入合成环境变量，结束时恢复。"""
    with patch.dict(os.environ, values):
        yield


class ResolveAliasLayersTest(StagedFileTestCase):
    """resolve_alias_layers 的层序、插值与 staged_override 语义。"""

    def test_env_layer_participates_as_topmost(self) -> None:
        with _synth_env({"PLUGINS": '["a"]'}):
            layers = resolve_alias_layers("PLUGINS")
        self.assertEqual(layers, [(SOURCE_ENV, '["a"]')])

    def test_all_layers_listed_high_to_low(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            project = Path(d) / ".env"
            project.write_text('PLUGINS=["dotenv"]\n', encoding="utf-8")
            settings_env.write_staged({"PLUGINS": '["staged"]'})
            with _env_without("PLUGINS"), patch.object(
                paths, "project_dotenv_path", return_value=project
            ):
                layers = resolve_alias_layers("PLUGINS")
        self.assertEqual(
            layers,
            [
                (SOURCE_DOTENV, '["dotenv"]'),
                (SOURCE_OVERRIDE, '["staged"]'),
            ],
        )

    def test_staged_override_replaces_override_layer(self) -> None:
        settings_env.write_staged({"PLUGINS": '["on-disk"]'})
        layers = resolve_alias_layers(
            "PLUGINS", staged_override={"PLUGINS": '["candidate"]'}
        )
        self.assertEqual(layers, [(SOURCE_OVERRIDE, '["candidate"]')])

    def test_interpolation_applied_in_file_layers(self) -> None:
        """文件层取值必须经 dotenv 插值：整值模板回读才是真实解析结果。"""
        with tempfile.TemporaryDirectory() as d:
            project = Path(d) / ".env"
            project.write_text(
                'BD_SYNTH_PLUGINS=["benchmark"]\nPLUGINS=${BD_SYNTH_PLUGINS}\n',
                encoding="utf-8",
            )
            with _env_without("PLUGINS"), patch.object(
                paths, "project_dotenv_path", return_value=project
            ):
                layers = resolve_alias_layers("PLUGINS")
        self.assertEqual(layers, [(SOURCE_DOTENV, '["benchmark"]')])

    def test_missing_everywhere_returns_empty(self) -> None:
        with _env_without("PLUGINS"):
            self.assertEqual(resolve_alias_layers("PLUGINS"), [])

    def test_composed_value_shares_layer_order(self) -> None:
        """胜出值与来源取同一份层序：composed_value 即 resolve 首层。"""
        with _synth_env({"PLUGINS": "env-value"}):
            self.assertEqual(
                composed_value("PLUGINS"), ("env-value", SOURCE_ENV)
            )
        self.assertEqual(composed_value("PLUGINS"), (None, SOURCE_DEFAULT))

    def test_unreadable_file_layer_is_skipped(self) -> None:
        """文件层读盘失败按「该层未提供」处理（_file_value 静默口径）。"""
        self.assertIsNone(
            settings_env._file_value(Path("Z:/briefdesk-no-such-dir/x"), "PLUGINS")
        )


class PerLayerDecodeTest(StagedFileTestCase):
    """逐层解码判可用性：低层坏 JSON 不放行，坏类型被覆盖按合并语义放行。"""

    def setUp(self) -> None:
        super().setUp()
        self.client = TestClient(
            srv.app,
            base_url="http://localhost",
            headers={"Origin": "http://localhost"},
        )

    def test_env_valid_plus_dotenv_broken_json_is_409(self) -> None:
        """env 合法（胜出）+ 项目 .env 同键坏 JSON（低层）：PUT 插件键 409。

        真实 Settings() 构造时低层解码仍会触发 SettingsError——只看胜出值
        会放行「保存成功、下次启动失败」的配置。GET 侧 expected 同样不可用。
        """
        with tempfile.TemporaryDirectory() as d:
            project = Path(d) / ".env"
            project.write_text("PLUGINS=not-json\n", encoding="utf-8")
            with patch.dict(
                os.environ, {"PLUGINS": '["benchmark"]'}
            ), patch.object(
                paths, "project_dotenv_path", return_value=project
            ):
                res = self.client.put(
                    "/api/settings/env",
                    json={"items": {"PLUGINS_REQUIRED": json.dumps(["benchmark"])}},
                )
                self.assertEqual(res.status_code, 409)
                issues = res.json()["detail"]["issues"]
                self.assertTrue(
                    any(
                        i["type"] == "unavailable"
                        and i.get("key") == "PLUGINS"
                        and "来源 dotenv" in i.get("detail", "")
                        for i in issues
                    ),
                    issues,
                )
                data = self.client.get("/api/settings/env").json()
        self.assertTrue(data["pluginsExpectedError"], "GET 必须同样报预期不可用")

    def test_dotenv_valid_plus_staged_broken_json_is_409(self) -> None:
        """.env 合法（胜出）+ 暂存坏 JSON（低层）：PUT 另一插件键 409，暂存不变。

        暂存坏值只能经直写绕过 normalize 进入（PUT 入口已 422），但外部编辑器
        或旧版本残留都可能造成；组合预检不得因胜出层合法而放行。
        """
        ok = self.client.put(
            "/api/settings/env",
            json={"items": {"LOG_LEVEL": "DEBUG"}},
        )
        self.assertEqual(ok.status_code, 200)
        with tempfile.TemporaryDirectory() as d:
            project = Path(d) / ".env"
            project.write_text('PLUGINS=["benchmark"]\n', encoding="utf-8")
            write_staged({"PLUGINS": "not-json"})
            before = self.staged_path.read_bytes()
            with _env_without("PLUGINS"), patch.object(
                paths, "project_dotenv_path", return_value=project
            ):
                res = self.client.put(
                    "/api/settings/env",
                    json={"items": {"PLUGINS_REQUIRED": json.dumps(["benchmark"])}},
                )
                self.assertEqual(res.status_code, 409)
                issues = res.json()["detail"]["issues"]
                self.assertTrue(
                    any(
                        i["type"] == "unavailable" and i.get("key") == "PLUGINS"
                        for i in issues
                    ),
                    issues,
                )
                self.assertEqual(
                    self.staged_path.read_bytes(),
                    before,
                    "拒绝时暂存必须原样保留",
                )

    def test_broken_json_with_valid_required_reports_unavailable(self) -> None:
        """高层坏 PLUGINS + 合法 REQUIRED：必选复检同样按不可用报，不 500。"""
        with tempfile.TemporaryDirectory() as d:
            project = Path(d) / ".env"
            project.write_text("PLUGINS=not-json\n", encoding="utf-8")
            with patch.object(
                paths, "project_dotenv_path", return_value=project
            ):
                res = self.client.put(
                    "/api/settings/env",
                    json={
                        "items": {"PLUGINS_REQUIRED": json.dumps(["benchmark"])}
                    },
                )
        self.assertEqual(res.status_code, 409)
        issues = res.json()["detail"]["issues"]
        self.assertTrue(
            any(
                i["type"] == "unavailable" and i.get("key") == "PLUGINS"
                for i in issues
            ),
            issues,
        )

    def test_winning_value_type_decided_on_winner_only(self) -> None:
        """低层坏类型 [17] 被高层合法名单覆盖：按 pydantic 合并语义放行。

        不得新增「每层类型都必须合法」的规则——低层类型错误不影响启动。
        胜出层构造：env 提供合法名单（高于项目 .env），项目 .env 写坏类型。
        """
        with tempfile.TemporaryDirectory() as d:
            project = Path(d) / ".env"
            project.write_text("PLUGINS=[17]\n", encoding="utf-8")
            with patch.dict(
                os.environ, {"PLUGINS": '["benchmark"]'}
            ), patch.object(paths, "project_dotenv_path", return_value=project):
                res = self.client.put(
                    "/api/settings/env",
                    json={"items": {"PLUGINS_REQUIRED": json.dumps(["benchmark"])}},
                )
        self.assertEqual(res.status_code, 200)


class PluginsSourceDirectionTest(StagedFileTestCase):
    """覆盖态方向修正：来源判定必须返回真正压住该键的层。"""

    def setUp(self) -> None:
        super().setUp()
        self.client = TestClient(
            srv.app,
            base_url="http://localhost",
            headers={"Origin": "http://localhost"},
        )

    def test_startup_default_yields_to_realtime_dotenv(self) -> None:
        """启动快照 default + 运行中新增项目 .env PLUGINS：来源 dotenv 且禁开关。

        反方向症状：startup or realtime 因 default 是真值永远返回 default，
        pluginsOverridden=False、开关可编辑，草稿重启却不生效。
        """
        with tempfile.TemporaryDirectory() as d:
            project = Path(d) / ".env"
            project.write_text('PLUGINS=["benchmark"]\n', encoding="utf-8")
            with _env_without("PLUGINS"), patch.object(
                paths, "project_dotenv_path", return_value=project
            ):
                data = self.client.get("/api/settings/env").json()
        self.assertEqual(data["pluginsSource"], SOURCE_DOTENV)
        self.assertTrue(data["pluginsOverridden"])

    def test_env_beats_everything(self) -> None:
        with _synth_env({"PLUGINS": '["x"]'}):
            data = self.client.get("/api/settings/env").json()
        self.assertEqual(data["pluginsSource"], SOURCE_ENV)
        self.assertTrue(data["pluginsOverridden"])

    def test_source_evaluated_once_per_request(self) -> None:
        """来源判定单次求值：同一响应里不再出现两次独立读盘。"""
        calls = []
        real = settings_routes._plugins_source

        def _counting() -> str:
            calls.append(1)
            return real()

        with patch.object(
            settings_routes, "_plugins_source", side_effect=_counting
        ):
            self.client.get("/api/settings/env")
        self.assertEqual(len(calls), 1)


class PrecheckCaseSensitivityTest(StagedFileTestCase):
    """预检的键匹配必须与真实 dotenv 解析同口径（大小写不敏感）。"""

    def setUp(self) -> None:
        super().setUp()
        self.client = TestClient(
            srv.app,
            base_url="http://localhost",
            headers={"Origin": "http://localhost"},
        )

    def test_lowercase_staged_key_is_seen_by_precheck(self) -> None:
        """小写键暂存 + PUT 另一插件键：409 点名必选插件不在启用列表。

        外部编辑器写的小写键读盘路径命中（dotenv 解析大小写不敏感）、预检
        路径查不到（精确匹配）时，PUT 只看启用的键得 200，写后真实生效组合
        却是「必选含 benchmark、启用列表不含」→ 下次启动中止。
        """
        with tempfile.TemporaryDirectory() as d:
            staged = Path(d) / "settings.env"
            staged.write_text('plugins_required=["benchmark"]\n', encoding="utf-8")
            with patch.object(
                settings_env, "get_settings_file", return_value=staged
            ):
                # read_staged 保留文件中的原始键形态；解析侧按大小写不敏感采纳
                self.assertEqual(
                    settings_env.read_staged(),
                    {"plugins_required": '["benchmark"]'},
                )
                res = self.client.put(
                    "/api/settings/env",
                    json={"items": {"PLUGINS": json.dumps(["weflow"])}},
                )
        self.assertEqual(res.status_code, 409)
        issues = res.json()["detail"]["issues"]
        self.assertTrue(
            any(
                i["type"] == "required_not_enabled"
                and i.get("plugin") == "benchmark"
                for i in issues
            ),
            issues,
        )


class WholeValueTemplateTest(StagedFileTestCase):
    """整值模板草稿：原文非 JSON 但展开后为合法数组的口径。"""

    def setUp(self) -> None:
        super().setUp()
        self.client = TestClient(
            srv.app,
            base_url="http://localhost",
            headers={"Origin": "http://localhost"},
        )

    def test_whole_value_template_passes_and_stages_raw(self) -> None:
        """${VAR:-[...]} 整值草稿落盘原文；回读（dotenv 展开）为合法名单。"""
        res = self.client.put(
            "/api/settings/env",
            json={"items": {"PLUGINS": '${BD_TEST_PLUGINS:-["benchmark"]}'}},
        )
        self.assertEqual(res.status_code, 200)
        self.assertIn(
            '${BD_TEST_PLUGINS:-["benchmark"]}',
            self.staged_path.read_text(encoding="utf-8"),
        )
        self.assertEqual(
            settings_env.read_staged()["PLUGINS"], '["benchmark"]'
        )

    def test_expansion_to_invalid_is_409_and_file_unchanged(self) -> None:
        """展开后非法（变量缺失 → 空串）：组合预检 409，文件不变。"""
        with _env_without("BD_TEST_PLUGINS"):
            res = self.client.put(
                "/api/settings/env",
                json={"items": {"PLUGINS": "${BD_TEST_PLUGINS}"}},
            )
        self.assertEqual(res.status_code, 409)
        self.assertNotIn("PLUGINS", settings_env.read_staged())

    def test_multiline_template_rejected_422(self) -> None:
        """放宽处必须补换行拒绝：multi 分支走不到通用 text 尾巴。"""
        draft = '${BD_A:-["a"' + chr(10) + "]} ",
        res = self.client.put(
            "/api/settings/env",
            json={"items": {"PLUGINS": draft}},
        )
        self.assertEqual(res.status_code, 422)
        self.assertNotIn("PLUGINS", settings_env.read_staged())

    def test_inline_comment_template_rejected_422(self) -> None:
        """放宽处必须补「 #」拒绝：dotenv 回读截断会写出与用户所见不同的名单。"""
        res = self.client.put(
            "/api/settings/env",
            json={"items": {"PLUGINS": '${BD_A:-["a"]} # 注释'}},
        )
        self.assertEqual(res.status_code, 422)
        self.assertNotIn("PLUGINS", settings_env.read_staged())

    def test_bad_json_without_variable_still_422(self) -> None:
        """不含 ${ 的坏 JSON 仍 422：放宽只针对「展开后才定论」的草稿。"""
        res = self.client.put(
            "/api/settings/env",
            json={"items": {"PLUGINS": "not-json"}},
        )
        self.assertEqual(res.status_code, 422)
        self.assertNotIn("PLUGINS", settings_env.read_staged())

    def test_second_put_after_templated_draft_is_200(self) -> None:
        """跨条回归：落盘模板草稿后再 PUT 任一插件键必须 200，不得误 409。

        逐层解码若用未展开原文判 JSON，会把合法展开值误判成坏 JSON。
        """
        first = self.client.put(
            "/api/settings/env",
            json={"items": {"PLUGINS": '${BD_TEST_PLUGINS:-["benchmark"]}'}},
        )
        self.assertEqual(first.status_code, 200)
        res = self.client.put(
            "/api/settings/env",
            json={"items": {"PLUGINS_REQUIRED": json.dumps(["benchmark"])}},
        )
        self.assertEqual(res.status_code, 200)


class StagedCommitFailureTest(StagedFileTestCase):
    """暂存提交失败必须显式失败；写后重读失败明确「已写入、状态未知」。"""

    def setUp(self) -> None:
        super().setUp()
        self.client = TestClient(
            srv.app,
            base_url="http://localhost",
            headers={"Origin": "http://localhost"},
        )

    def test_unlink_permission_error_is_500_structured(self) -> None:
        # 暂存只放 PLUGINS：整删才走 unlink 分支（还有其他键时是重写，不删文件）
        write_staged({"PLUGINS": '["weflow"]'})
        before = self.staged_path.read_bytes()
        real_unlink = Path.unlink

        def _boom(path: Path, *a: object, **k: object) -> None:
            if path == self.staged_path:
                raise PermissionError("locked by editor")
            real_unlink(path, *a, **k)  # type: ignore[arg-type]

        with patch.object(Path, "unlink", _boom):
            res = self.client.put(
                "/api/settings/env",
                json={"items": {"PLUGINS": None}},
            )
        self.assertEqual(res.status_code, 500)
        detail = res.json()["detail"]
        self.assertEqual(detail["type"], "staged_commit_failed")
        self.assertEqual(detail["stage"], "delete")
        self.assertEqual(
            self.staged_path.read_bytes(), before, "删除失败时文件必须原样"
        )

    def test_write_bytes_oserror_is_500_with_stage(self) -> None:
        real_write = Path.write_bytes

        def _boom(path: Path, data: bytes) -> None:
            if path.suffix == ".tmp":
                raise OSError("disk full")
            real_write(path, data)  # type: ignore[arg-type]

        with patch.object(Path, "write_bytes", autospec=True, side_effect=_boom):
            res = self.client.put(
                "/api/settings/env",
                json={"items": {"LOG_LEVEL": "INFO"}},
            )
        self.assertEqual(res.status_code, 500)
        detail = res.json()["detail"]
        self.assertEqual(detail["type"], "staged_commit_failed")
        self.assertEqual(detail["stage"], "write")

    def test_replace_oserror_is_500_with_stage(self) -> None:
        write_staged({"LOG_LEVEL": "DEBUG"})
        with patch.object(
            settings_env.os,
            "replace",
            side_effect=OSError("replace denied"),
        ):
            res = self.client.put(
                "/api/settings/env",
                json={"items": {"LOG_LEVEL": "INFO"}},
            )
        self.assertEqual(res.status_code, 500)
        detail = res.json()["detail"]
        self.assertEqual(detail["type"], "staged_commit_failed")
        self.assertEqual(detail["stage"], "replace")
        self.assertEqual(
            self.staged_path.read_text(encoding="utf-8"),
            "LOG_LEVEL=DEBUG\n",
            "替换失败时原文件必须不变",
        )

    def test_mkdir_permission_error_is_500_with_stage(self) -> None:
        """目录创建失败同样必须结构化：裸 OSError 穿出路由只会是无 detail 的 500。"""
        real_mkdir = Path.mkdir

        def _boom(path: Path, *a: object, **k: object) -> None:
            if path == self.staged_path.parent:
                raise PermissionError("parent dir denied")
            real_mkdir(path, *a, **k)  # type: ignore[arg-type]

        with patch.object(Path, "mkdir", _boom):
            res = self.client.put(
                "/api/settings/env",
                json={"items": {"LOG_LEVEL": "INFO"}},
            )
        self.assertEqual(res.status_code, 500)
        detail = res.json()["detail"]
        self.assertEqual(detail["type"], "staged_commit_failed")
        self.assertEqual(detail["stage"], "mkdir")
        self.assertFalse(self.staged_path.exists(), "目录都没建起来，不应留下文件")

    def test_write_failure_with_cleanup_failure_keeps_structured_error(self) -> None:
        """写失败后 .tmp 清理再失败：清理不得顶替原始阶段，仍须是结构化 500。"""
        real_write = Path.write_bytes
        real_unlink = Path.unlink

        def _write_boom(path: Path, *a: object, **k: object) -> int:
            if path.suffix == ".tmp":
                raise OSError("disk full")
            return real_write(path, *a, **k)  # type: ignore[arg-type]

        def _unlink_boom(path: Path, *a: object, **k: object) -> None:
            if path.suffix == ".tmp":
                raise PermissionError("tmp locked")
            real_unlink(path, *a, **k)  # type: ignore[arg-type]

        with (
            patch.object(Path, "write_bytes", _write_boom),
            patch.object(Path, "unlink", _unlink_boom),
        ):
            res = self.client.put(
                "/api/settings/env",
                json={"items": {"LOG_LEVEL": "INFO"}},
            )
        self.assertEqual(res.status_code, 500)
        detail = res.json()["detail"]
        self.assertEqual(detail["type"], "staged_commit_failed")
        self.assertEqual(detail["stage"], "write")

    def test_replace_failure_with_cleanup_failure_keeps_structured_error(self) -> None:
        """替换失败后 .tmp 清理再失败：原文件不变，阶段仍是 replace。"""
        write_staged({"LOG_LEVEL": "DEBUG"})
        real_unlink = Path.unlink

        def _unlink_boom(path: Path, *a: object, **k: object) -> None:
            if path.suffix == ".tmp":
                raise PermissionError("tmp locked")
            real_unlink(path, *a, **k)  # type: ignore[arg-type]

        with (
            patch.object(
                settings_env.os, "replace", side_effect=OSError("replace denied")
            ),
            patch.object(Path, "unlink", _unlink_boom),
        ):
            res = self.client.put(
                "/api/settings/env",
                json={"items": {"LOG_LEVEL": "INFO"}},
            )
        self.assertEqual(res.status_code, 500)
        detail = res.json()["detail"]
        self.assertEqual(detail["type"], "staged_commit_failed")
        self.assertEqual(detail["stage"], "replace")
        self.assertEqual(
            self.staged_path.read_text(encoding="utf-8"),
            "LOG_LEVEL=DEBUG\n",
            "替换失败时原文件必须不变",
        )

    def test_commit_error_keeps_original_cause_when_cleanup_fails(self) -> None:
        """清理失败不得顶替原始原因：__cause__ 必须是真实的提交失败异常。

        路由的 500 明细里只有固定文案（原始 OSError 文本不回显），原因是否保留
        只能直调 commit_staged_write 从 __cause__ 断言。
        """
        original = OSError("commit denied")
        real_unlink = Path.unlink

        def _unlink_boom(path: Path, *a: object, **k: object) -> None:
            if path.suffix == ".tmp":
                raise PermissionError("tmp locked")
            real_unlink(path, *a, **k)  # type: ignore[arg-type]

        for stage, failing in (
            ("write", patch.object(Path, "write_bytes", side_effect=original)),
            ("replace", patch.object(settings_env.os, "replace", side_effect=original)),
        ):
            with (
                self.subTest(stage=stage),
                failing,
                patch.object(Path, "unlink", _unlink_boom),
                self.assertRaises(settings_env.StagedCommitError) as ctx,
            ):
                settings_env.commit_staged_write(b"LOG_LEVEL=INFO\n")
            self.assertEqual(ctx.exception.stage, stage)
            self.assertIs(ctx.exception.__cause__, original)

    def test_post_write_reread_failure_is_500_state_unknown(self) -> None:
        """写后严格重读失败：500 + staged_state_unknown，不得回 200 ok:true。

        桩打在路由命名空间：prepare_staged_write 内部经 settings_env 自己的
        绑定调用 read_staged_strict（不受影响，正常读到旧值并提交），路由的
        写后重读经本模块导入绑定调用——桩只命中后者。若实现退回吞异常的
        展示读取，该桩不会被触发，用例得到 200 并失败。
        """
        write_staged({"LOG_LEVEL": "DEBUG"})

        def _boom() -> dict[str, str]:
            raise settings_env.StagedReadError("post-write read boom")

        with patch.object(
            settings_routes, "read_staged_strict", side_effect=_boom
        ):
            res = self.client.put(
                "/api/settings/env",
                json={"items": {"LOG_LEVEL": "INFO"}},
            )
        self.assertEqual(res.status_code, 500)
        detail = res.json()["detail"]
        self.assertEqual(detail["type"], "staged_state_unknown")
        self.assertIn("已写入", detail["message"])
        self.assertEqual(
            self.staged_path.read_text(encoding="utf-8"),
            "LOG_LEVEL=INFO\n",
            "写后重读失败时写入已发生",
        )


if __name__ == "__main__":
    unittest.main()
