"""scripts/migrate_embed_model.py 的单元测试（临时目录，不触网不读真实库）。

autouse fixture 隔离三类外部来源：进程环境变量、运行时 dotenv 层、平台
默认库路径——隔离不依赖任何用例的参数传递顺序。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Self

import pytest

import scripts.migrate_embed_model as mod
from scripts.migrate_embed_model import (
    _load_effective_env,
    backup_database,
    fetch_embedding_rows,
    load_env_file,
    main,
    plan_migration,
    probe_embedding_dimension,
    purge_all_embeddings,
    read_env_embedding_config,
    resolve_db_path,
    summarize_rows,
)

DDL = """
CREATE TABLE item_embeddings (
    item_id    TEXT PRIMARY KEY,
    model      TEXT NOT NULL,
    embedding  TEXT NOT NULL,
    created_at TEXT NOT NULL
)
"""


@pytest.fixture(autouse=True)
def _isolate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """把脚本消费的全部外部来源钉进沙箱：
    进程环境变量、运行时 dotenv 层（项目 .env / 用户暂存）、默认库路径。"""
    for key in ("DB_PATH", "BRIEFDESK_DATA_DIR", "EMBED_MODEL", "EMBED_API_BASE",
                "AI_MODEL", "BRIEFDESK_SETTINGS_FILE"):
        monkeypatch.delenv(key, raising=False)
    dotenv_layers = [
        ("dotenv", tmp_path / "project.env"),
        ("override", tmp_path / "settings.env"),
    ]
    monkeypatch.setattr(mod, "_dotenv_layers", lambda: dotenv_layers)
    monkeypatch.setattr(
        mod, "_default_db_path", lambda: tmp_path / "platform-default.sqlite"
    )


def _make_db(path: Path, rows: list[tuple[str, str, str]]) -> Path:
    conn = sqlite3.connect(path)
    try:
        conn.execute(DDL)
        conn.executemany(
            "INSERT INTO item_embeddings (item_id, model, embedding, created_at)"
            " VALUES (?, ?, ?, '2026-01-01')",
            rows,
        )
        conn.commit()
    finally:
        conn.close()
    return path


def _read_rows(path: Path) -> list[tuple[str, str]]:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT item_id, model FROM item_embeddings").fetchall()
    finally:
        conn.close()


def _read_backup_rows(backup: Path) -> list[tuple[str, str]]:
    conn = sqlite3.connect(backup)
    try:
        return conn.execute("SELECT item_id, model FROM item_embeddings").fetchall()
    finally:
        conn.close()


# ── env / 配置读取 ──


def test_load_env_file_parses_variants(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# 注释\n"
        "EMBED_MODEL=bge-m3\n"
        "EMBED_API_BASE = \"http://x/v1\" \n"
        "export AI_MODEL=other-model\n"
        "AI_API_BASE=http://y/v1 # 行内注释\n"
        "NO_EQUALS_LINE\n\n",
        encoding="utf-8",
    )
    env = load_env_file(env_file)
    assert env == {
        "EMBED_MODEL": "bge-m3",
        "EMBED_API_BASE": "http://x/v1",
        "AI_MODEL": "other-model",
        "AI_API_BASE": "http://y/v1",
    }


def test_load_env_file_missing(tmp_path: Path) -> None:
    assert load_env_file(tmp_path / "nope.env") == {}


def test_read_env_embedding_config_fallback_to_ai_model() -> None:
    model, base, from_fallback = read_env_embedding_config({"AI_MODEL": "m1"})
    assert (model, base, from_fallback) == ("m1", "", True)


def test_read_env_embedding_config_explicit_wins() -> None:
    model, base, from_fallback = read_env_embedding_config(
        {"EMBED_MODEL": "m2", "EMBED_API_BASE": "http://e/v1", "AI_MODEL": "m1"}
    )
    assert (model, base, from_fallback) == ("m2", "http://e/v1", False)


def test_load_effective_env_layer_order_and_process_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 两层同名键项目 .env 胜（层序与运行时一致：项目 .env > 用户暂存）；
    # 低层独有键保留；进程环境压过所有 dotenv 层
    (mod._dotenv_layers()[0][1]).write_text(
        "EMBED_MODEL=from-project\nONLY_LOW=low\n", encoding="utf-8"
    )
    (mod._dotenv_layers()[1][1]).write_text(
        "EMBED_MODEL=from-override\n", encoding="utf-8"
    )
    monkeypatch.setenv("EMBED_API_BASE", "http://from-process-env/v1")
    env = _load_effective_env()
    assert env["EMBED_MODEL"] == "from-project"
    assert env["ONLY_LOW"] == "low"
    assert env["EMBED_API_BASE"] == "http://from-process-env/v1"


def test_load_effective_env_case_insensitive_process_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """进程环境大小写不敏感（与 pydantic-settings / settings_env 同口径）。

    失败模式：os.environ.get("EMBED_MODEL") 漏掉小写变体，盘点把
    「进程环境已配置的模型」当成未设置、回落 AI_MODEL，把真实生效行
    误报为旧模型残留。
    """
    (mod._dotenv_layers()[1][1]).write_text("EMBED_MODEL=from-dotenv\n", encoding="utf-8")
    monkeypatch.setenv("embed_model", "from-lowercase-env")
    env = _load_effective_env()
    assert env["EMBED_MODEL"] == "from-lowercase-env"


def test_load_effective_env_interpolates_variable_refs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """dotenv 的 ${VAR} 插值（DotEnvSettingsSource 会插值，本脚本必须同口径）。

    失败模式：`EMBED_MODEL=${AI_MODEL}` 被当字面模型名 → 与现实维度对不上 →
    盘点把生效行误报为旧模型残留。
    """
    (mod._dotenv_layers()[1][1]).write_text(
        "AI_MODEL=real-embed-model\nEMBED_MODEL=${AI_MODEL}\n", encoding="utf-8"
    )
    monkeypatch.delenv("EMBED_MODEL", raising=False)
    monkeypatch.delenv("AI_MODEL", raising=False)
    env = _load_effective_env()
    assert env["EMBED_MODEL"] == "real-embed-model"
    model, _base, from_fallback = read_env_embedding_config(env)
    assert (model, from_fallback) == ("real-embed-model", False)


# ── 库路径解析链（与运行时层序一致） ──


def test_resolve_db_path_explicit_wins(tmp_path: Path) -> None:
    p, source = resolve_db_path(tmp_path / "x.sqlite")
    assert p == tmp_path / "x.sqlite"
    assert source == "--db 参数"


def test_resolve_db_path_process_env_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DB_PATH", str(tmp_path / "from-env.sqlite"))
    p, source = resolve_db_path(None)
    assert p == tmp_path / "from-env.sqlite"
    assert source == "环境变量 DB_PATH"


def test_resolve_db_path_dotenv_layer_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 两层都配了 DB_PATH：高层（项目 .env）赢——与运行时解析链同序
    (mod._dotenv_layers()[0][1]).write_text(
        f"DB_PATH={tmp_path / 'from-project.env.sqlite'}\n", encoding="utf-8"
    )
    (mod._dotenv_layers()[1][1]).write_text(
        f"DB_PATH={tmp_path / 'from-override.sqlite'}\n", encoding="utf-8"
    )
    p, source = resolve_db_path(None)
    assert p == tmp_path / "from-project.env.sqlite"
    assert "dotenv" in source


def test_resolve_db_path_override_layer(
    tmp_path: Path,
) -> None:
    (mod._dotenv_layers()[1][1]).write_text(
        f"DB_PATH={tmp_path / 'from-settings.sqlite'}\n", encoding="utf-8"
    )
    p, source = resolve_db_path(None)
    assert p == tmp_path / "from-settings.sqlite"
    assert "override" in source


def test_resolve_db_path_relative_resolved(tmp_path: Path) -> None:
    (mod._dotenv_layers()[0][1]).write_text("DB_PATH=rel/data.sqlite\n", encoding="utf-8")
    p, _source = resolve_db_path(None)
    assert p.is_absolute()


def test_resolve_db_path_platform_default(tmp_path: Path) -> None:
    p, source = resolve_db_path(None)
    assert p == tmp_path / "platform-default.sqlite"
    assert source == "默认（paths.database_path）"


# ── summarize_rows / plan_migration ──


def test_summarize_rows_groups_by_model_and_dim() -> None:
    rows = [
        ("m1", json.dumps([0.1, 0.2]), "a"),
        ("m1", json.dumps([0.1, 0.2]), "b"),
        ("m2", json.dumps([0.1] * 4), "c"),
        ("m1", "not-json", "d"),
    ]
    summary = summarize_rows(rows)
    assert ("m1", 2, 2) in summary
    assert ("m2", 4, 1) in summary
    assert ("<bad_json>", -1, 1) in summary


def test_plan_migration_flags_old_model_and_bad_json() -> None:
    existing = [("old-model", 768, 5), ("<bad_json>", -1, 2)]
    problems = plan_migration(existing, "bge-m3", 1024)
    assert any("旧模型行残留" in p for p in problems)
    assert any("无法解析" in p for p in problems)


def test_plan_migration_flags_same_model_dim_change() -> None:
    assert any("维度不一致" in p for p in plan_migration([("m", 768, 5)], "m", 1024))


def test_plan_migration_clean_when_consistent() -> None:
    assert plan_migration([("m", 1024, 5)], "m", 1024) == []


def test_plan_migration_unknown_target_model_refuses(tmp_path: Path) -> None:
    # 目标模型未知：绝不把全部行判成「旧模型残留」
    problems = plan_migration([("real-model", 768, 2)], "", 0)
    assert any("目标模型未配置" in p for p in problems)


# ── 表存在性 / 行读取 ──


def test_fetch_embedding_rows_missing_table_returns_empty(tmp_path: Path) -> None:
    db = tmp_path / "fresh.sqlite"
    db.touch()
    assert fetch_embedding_rows(db) == []


def test_fetch_embedding_rows_locked_db_raises(tmp_path: Path) -> None:
    db = _make_db(tmp_path / "d.sqlite", [("a", "m", "[]")])
    blocker = sqlite3.connect(db)
    try:
        blocker.execute("BEGIN EXCLUSIVE")
        with pytest.raises(RuntimeError, match="占用"):
            fetch_embedding_rows(db)
    finally:
        blocker.rollback()
        blocker.close()


def test_fetch_embedding_rows_not_a_database_raises(tmp_path: Path) -> None:
    db = tmp_path / "fake.sqlite"
    db.write_bytes(b"this is not a sqlite database" * 100)
    with pytest.raises(RuntimeError, match="SQLite"):
        fetch_embedding_rows(db)


# ── 备份 / 清空 ──


def test_backup_database_content_and_no_overwrite(tmp_path: Path) -> None:
    db = _make_db(tmp_path / "d.sqlite", [("a", "m1", "[]")])
    first = backup_database(db)
    assert _read_backup_rows(first) == [("a", "m1")]
    conn = sqlite3.connect(db)
    try:
        conn.execute("DELETE FROM item_embeddings")
        conn.commit()
    finally:
        conn.close()
    try:
        backup_database(db)
    except RuntimeError as e:
        assert "拒绝覆盖" in str(e)
    else:
        # 时间戳跨秒时第二份是新文件，第一份必须原样保留
        assert first.exists()
        assert _read_backup_rows(first) == [("a", "m1")]


def test_backup_reads_uncheckpointed_wal_rows(tmp_path: Path) -> None:
    # 生产库是 WAL 模式：已提交未 checkpoint 的行留在 -wal。
    # 在线备份必须读到它们；备份自身可独立打开（不依赖源库 sidecar）。
    db = _make_db(tmp_path / "d.sqlite", [("a", "m1", "[]")])
    conn = sqlite3.connect(db)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "INSERT INTO item_embeddings VALUES ('b', 'm2', '[]', '2026-01-01')"
        )
        conn.commit()
        wal = db.with_name(db.name + "-wal")
        assert wal.exists(), "前提：提交后未 checkpoint，行在 -wal 里"
        backup = backup_database(db)
    finally:
        conn.close()
    assert _read_backup_rows(backup) == [("a", "m1"), ("b", "m2")]
    # 备份是独立文件（不引用源库 -wal）：重开备份即验证自包含可读
    conn = sqlite3.connect(backup)
    try:
        rows = conn.execute(
            "SELECT item_id, model FROM item_embeddings ORDER BY item_id"
        ).fetchall()
    finally:
        conn.close()
    assert rows == [("a", "m1"), ("b", "m2")]


def test_backup_locked_db_raises_instead_of_hanging(tmp_path: Path) -> None:
    db = _make_db(tmp_path / "d.sqlite", [("a", "m", "[]")])
    blocker = sqlite3.connect(db)
    try:
        blocker.execute("BEGIN EXCLUSIVE")
        with pytest.raises(RuntimeError, match="占用"):
            backup_database(db)
    finally:
        blocker.rollback()
        blocker.close()
    assert not list(db.parent.glob("*.bak-embed-*"))


def test_purge_all_embeddings(tmp_path: Path) -> None:
    db = _make_db(tmp_path / "d.sqlite", [("a", "old", "[]"), ("b", "new", "[]")])
    assert purge_all_embeddings(db) == 2
    assert _read_rows(db) == []


def test_purge_missing_table_returns_zero(tmp_path: Path) -> None:
    db = tmp_path / "fresh.sqlite"
    db.touch()
    assert purge_all_embeddings(db) == 0


# ── probe ──


def test_probe_bad_base_url_raises() -> None:
    with pytest.raises(RuntimeError, match="探测失败"):
        probe_embedding_dimension("http://127.0.0.1:1/v1", "m", timeout=1.0)


def test_probe_rejects_non_http_scheme(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="http"):
        probe_embedding_dimension("file:///tmp/x", "m")


class _FakeResp:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self, _n: int) -> bytes:
        return self._body

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        return None


class _FakeOpener:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def open(self, _req: object, timeout: float) -> _FakeResp:
        return _FakeResp(self._body)


def test_probe_bad_response_shapes_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    for body in (
        b"{}",
        b'{"data": null}',
        b'{"data": "abc"}',
        b'{"data": []}',
        b'{"data": [null]}',
        b'{"data": ["x"]}',
        b'{"data": [{"embedding": null}]}',
        b'{"data": [{"embedding": "vec"}]}',
        b'{"data": [{"embedding": []}]}',
        b"\xff\xfe\x00bad-utf8",
    ):
        monkeypatch.setattr(
            mod.urllib.request,
            "build_opener",
            lambda _h, _b=body: _FakeOpener(_b),
        )
        with pytest.raises(RuntimeError):
            probe_embedding_dimension("http://local/v1", "m")


# ── main 流程 ──


def test_main_check_reports_and_exits_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = _make_db(tmp_path / "d.sqlite", [("a", "old-model", json.dumps([0.0] * 768))])
    code = main(["--db", str(db), "--check"])
    out = capsys.readouterr().out
    assert code == 0
    assert "old-model" in out
    assert "未做任何改动" in out


def test_main_missing_db_exits_two(tmp_path: Path) -> None:
    assert main(["--db", str(tmp_path / "nope.sqlite"), "--check"]) == 2


def test_main_execute_requires_purge_flag(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = _make_db(tmp_path / "d.sqlite", [("a", "old", "[]")])
    with pytest.raises(SystemExit) as exc:
        main(["--db", str(db), "--execute", "--new-model", "m", "--yes"])
    assert exc.value.code == 2
    assert "purge-old-rows" in capsys.readouterr().err


def test_main_check_execute_mutually_exclusive(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = _make_db(tmp_path / "d.sqlite", [("a", "old", "[]")])
    with pytest.raises(SystemExit) as exc:
        main(["--db", str(db), "--check", "--execute", "--purge-old-rows", "--yes"])
    assert exc.value.code == 2
    assert "互斥" in capsys.readouterr().err


def test_main_execute_refuses_unknown_target_model(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # --skip-probe + 目标模型未知：绝不清理（否则全部行被误判残留后清空报成功）
    db = _make_db(
        tmp_path / "d.sqlite",
        [("a", "real-model", "[]"), ("b", "real-model", "[]")],
    )
    code = main(
        [
            "--db", str(db), "--execute", "--purge-old-rows", "--skip-probe",
            "--yes",
        ]
    )
    out = capsys.readouterr().out
    assert code == 2
    assert "目标模型未知" in out
    assert _read_rows(db) == [("a", "real-model"), ("b", "real-model")]


def test_main_execute_refuses_noop(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = _make_db(tmp_path / "d.sqlite", [("a", "target", json.dumps([0.0] * 4))])
    code = main(
        [
            "--db", str(db), "--execute", "--purge-old-rows",
            "--new-model", "target", "--skip-probe", "--yes",
        ]
    )
    out = capsys.readouterr().out
    assert code == 2
    # 同名 + --skip-probe：无法验证维度，落到「未探测」档拒绝
    assert "未探测端点维度" in out
    assert _read_rows(db) == [("a", "target")]


def test_main_execute_skip_probe_same_model_warns_and_refuses(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # 同名 + --skip-probe：维度一致性未验证，拒绝清库并给出下一步指引
    db = _make_db(tmp_path / "d.sqlite", [("a", "m", json.dumps([0.0] * 4))])
    code = main(
        [
            "--db", str(db), "--execute", "--purge-old-rows",
            "--new-model", "m", "--skip-probe", "--yes",
        ]
    )
    out = capsys.readouterr().out
    assert code == 2
    # 同名 + --skip-probe：警告 + 落「未探测」档拒绝，库未动
    assert "维度一致性未验证" in out
    assert "未探测端点维度" in out
    assert _read_rows(db) == [("a", "m")]


def test_main_execute_empty_table_message(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = _make_db(tmp_path / "d.sqlite", [])
    code = main(
        [
            "--db", str(db), "--execute", "--purge-old-rows",
            "--new-model", "m", "--skip-probe", "--yes",
        ]
    )
    out = capsys.readouterr().out
    assert code == 2
    assert "向量表为空" in out


def test_main_execute_probe_failure_aborts_before_backup(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = _make_db(tmp_path / "d.sqlite", [("a", "old", "[]")])
    code = main(
        [
            "--db", str(db), "--execute", "--purge-old-rows",
            "--new-model", "m", "--embed-base", "http://127.0.0.1:1/v1",
            "--yes",
        ]
    )
    out = capsys.readouterr().out
    assert code == 2
    assert "迁移中止" in out
    assert _read_rows(db) == [("a", "old")]
    assert not list(db.parent.glob("*.bak-embed-*"))


def test_main_execute_happy_path_backup_before_purge(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = _make_db(
        tmp_path / "d.sqlite",
        [("a", "old-model", "[]"), ("b", "old-model", "[]")],
    )
    code = main(
        [
            "--db", str(db), "--execute", "--purge-old-rows",
            "--new-model", "bge-m3", "--skip-probe", "--yes",
        ]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert _read_rows(db) == []
    backups = list(db.parent.glob("*.bak-embed-*"))
    assert len(backups) == 1
    # 备份内容 = 清理前状态（最关键的事务边界）
    assert _read_backup_rows(backups[0]) == [
        ("a", "old-model"),
        ("b", "old-model"),
    ]
    assert "bge-m3" in out
    assert "已备份" in out
    assert "恢复口径" in out


def test_main_execute_declined_confirmation_touches_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _make_db(tmp_path / "d.sqlite", [("a", "old", "[]")])
    monkeypatch.setattr("builtins.input", lambda _p: "n")
    code = main(
        [
            "--db", str(db), "--execute", "--purge-old-rows",
            "--new-model", "m", "--skip-probe",
        ]
    )
    assert code == 1
    assert _read_rows(db) == [("a", "old")]
    assert not list(db.parent.glob("*.bak-embed-*"))


def test_main_eof_on_confirmation_cancels(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _make_db(tmp_path / "d.sqlite", [("a", "old", "[]")])

    def _raise_eof(_p: str) -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", _raise_eof)
    code = main(
        [
            "--db", str(db), "--execute", "--purge-old-rows",
            "--new-model", "m", "--skip-probe",
        ]
    )
    out = capsys.readouterr().out
    assert code == 1
    assert "--yes" in out
    assert _read_rows(db) == [("a", "old")]


def test_main_reports_db_source(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = _make_db(tmp_path / "d.sqlite", [])
    code = main(["--db", str(db), "--check"])
    out = capsys.readouterr().out
    assert code == 0
    assert "来源：--db 参数" in out


def test_main_os_environ_db_path_respected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _make_db(tmp_path / "env-db.sqlite", [])
    monkeypatch.setenv("DB_PATH", str(db))
    code = main(["--check"])
    out = capsys.readouterr().out
    assert code == 0
    assert "环境变量 DB_PATH" in out
