"""Benchmark 目录拆分的路径契约（用户数据/缓存 vs 包内只读示例）。

每条断言对应一个具体失败模式，改动时不要放宽：
- Web 导出写回安装包目录：wheel 下目录可能只读，源码树里会用真实聊天内容覆盖示例；
- 单源快照：用户导出的用例在用户目录，只拷包内示例会让 Web 基准跑的是示例数据；
- 缺省目录冻成 import 期常量：测试与冒烟脚本就无法用 BRIEFDESK_* 重定向。
"""

import json
from pathlib import Path

import pytest

from briefdesk import paths
from briefdesk.plugins.benchmark import cli, store, supervisor


def test_user_cases_dir_follows_env_after_import(monkeypatch, tmp_path):
    first = tmp_path / "a"
    monkeypatch.setenv("BRIEFDESK_DATA_DIR", str(first))
    assert store.user_cases_dir() == first / "benchmark" / "cases"
    second = tmp_path / "b"
    monkeypatch.setenv("BRIEFDESK_DATA_DIR", str(second))
    assert store.user_cases_dir() == second / "benchmark" / "cases"


def test_package_cases_dir_holds_examples_only():
    assert store.PACKAGE_CASES_DIR == Path(store.__file__).resolve().parent / "cases"
    assert (store.PACKAGE_CASES_DIR / "classify.example.json").exists()


def test_fromweb_path_defaults_to_user_cases_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("BRIEFDESK_DATA_DIR", str(tmp_path))
    assert store.fromweb_path("classify") == (
        tmp_path / "benchmark" / "cases" / "classify.fromweb.json"
    )


def test_fromweb_path_explicit_dir_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("BRIEFDESK_DATA_DIR", str(tmp_path / "data"))
    explicit = tmp_path / "snapshot"
    assert store.fromweb_path("classify", explicit) == explicit / "classify.fromweb.json"


async def test_export_list_delete_only_touch_user_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("BRIEFDESK_DATA_DIR", str(tmp_path))
    package_before = sorted(p.name for p in store.PACKAGE_CASES_DIR.glob("*"))
    case = {
        "id": "bench-path-1",
        "note": "路径契约",
        "messages": [{"msg_id": "m1", "content": "活动"}],
        "expected": [{"index": 0, "category": "活动通知"}],
    }
    path = await store.export_fromweb("classify", [case])
    assert path.parent == paths.benchmark_cases_dir()
    assert path.exists()
    assert await store.list_fromweb("classify")
    assert await store.delete_all_fromweb("classify") == 1
    assert sorted(p.name for p in store.PACKAGE_CASES_DIR.glob("*")) == package_before


def test_default_cases_dirs_prefers_user_then_package(monkeypatch, tmp_path):
    monkeypatch.setenv("BRIEFDESK_DATA_DIR", str(tmp_path))
    assert cli.default_cases_dirs() == [
        paths.benchmark_cases_dir(),
        store.PACKAGE_CASES_DIR,
    ]


def test_cases_dirs_arg_explicit_is_the_whole_world(tmp_path):
    explicit = tmp_path / "mine"
    assert cli._cases_dirs_arg(str(explicit)) == [explicit]
    assert cli._cases_dirs_arg(None) == cli.default_cases_dirs()


def test_resolve_dataset_prefers_user_dir_over_package(tmp_path):
    user = tmp_path / "user"
    user.mkdir()
    package = tmp_path / "package"
    package.mkdir()
    (package / "classify.example.json").write_text("{}", encoding="utf-8")
    assert cli._resolve_dataset("classify", [user, package], None) == (
        package / "classify.example.json"
    )
    (user / "classify.json").write_text("{}", encoding="utf-8")
    assert cli._resolve_dataset("classify", [user, package], None) == user / "classify.json"


def test_resolve_dataset_within_dir_priority_unchanged(tmp_path):
    with pytest.raises(FileNotFoundError):
        cli._resolve_dataset("classify", [tmp_path], None)
    (tmp_path / "classify.example.json").write_text("{}", encoding="utf-8")
    assert cli._resolve_dataset("classify", [tmp_path], None).name == "classify.example.json"
    (tmp_path / "classify.fromweb.json").write_text("{}", encoding="utf-8")
    assert cli._resolve_dataset("classify", [tmp_path], None).name == "classify.fromweb.json"
    (tmp_path / "classify.json").write_text("{}", encoding="utf-8")
    assert cli._resolve_dataset("classify", [tmp_path], None).name == "classify.json"


def test_resolve_dataset_missing_lists_every_candidate(tmp_path):
    with pytest.raises(FileNotFoundError) as exc:
        cli._resolve_dataset("classify", [tmp_path / "user", tmp_path / "package"], None)
    message = str(exc.value)
    assert "user" in message
    assert "package" in message


def test_out_dir_default_follows_user_data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("BRIEFDESK_DATA_DIR", str(tmp_path))
    assert cli._out_dir_arg(None) == tmp_path / "benchmark" / "reports"
    assert cli._out_dir_arg(str(tmp_path / "x")) == tmp_path / "x"


def test_supervisor_cases_sources_order(monkeypatch, tmp_path):
    monkeypatch.setenv("BRIEFDESK_DATA_DIR", str(tmp_path))
    assert supervisor._cases_sources() == (
        store.PACKAGE_CASES_DIR,
        tmp_path / "benchmark" / "cases",
    )


def test_supervisor_runs_root_follows_cache_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("BRIEFDESK_CACHE_DIR", str(tmp_path))
    assert supervisor._runs_root() == tmp_path / "benchmark" / "runs"


def test_snapshot_copies_package_then_user_overrides(monkeypatch, tmp_path):
    package = tmp_path / "package"
    package.mkdir()
    (package / "classify.fromweb.json").write_text(
        json.dumps({"feature": "classify", "cases": [{"id": "example"}]}), encoding="utf-8"
    )
    (package / "only.example.json").write_text("pkg", encoding="utf-8")
    user = tmp_path / "user"
    user.mkdir()
    (user / "classify.fromweb.json").write_text(
        json.dumps({"feature": "classify", "cases": [{"id": "user"}]}), encoding="utf-8"
    )
    monkeypatch.setattr(supervisor, "_cases_sources", lambda: (package, user))

    cases_dir = supervisor._snapshot_cases(tmp_path / "run")
    payload = json.loads((cases_dir / "classify.fromweb.json").read_text(encoding="utf-8"))
    assert payload["cases"] == [{"id": "user"}], "用户用例必须覆盖包内同名文件"
    assert (cases_dir / "only.example.json").read_text(encoding="utf-8") == "pkg"


def test_snapshot_skips_missing_sources(monkeypatch, tmp_path):
    monkeypatch.setattr(supervisor, "_cases_sources", lambda: (tmp_path / "nope",))
    cases_dir = supervisor._snapshot_cases(tmp_path / "run")
    assert cases_dir.is_dir()
    assert not list(cases_dir.iterdir())
