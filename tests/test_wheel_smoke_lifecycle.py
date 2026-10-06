"""wheel 冒烟的终态状态机与探针形态的离线回归。

为什么在 pytest 里测这两样：`wheel-smoke` 只在 CI 真跑，本地改坏了终态判据或让
探针重新拖入 config/钥匙串链路，没有任何离线防线会响。终态机通过 fetcher 注入
（与生产缺省 http 同形状，调用方行为不变）把协议分支变成可判定用例；探针用
**文本守卫**钉住「只 import briefdesk.paths」的形态——它守的是导入面，不是行为
（行为仍由冒烟主链路与真实目录快照覆盖）。

数据一律虚构；runs_root 指向 pytest tmp_path。
"""

import json
from pathlib import Path
from typing import Any

import pytest

from scripts import wheel_smoke
from scripts.wheel_smoke import SmokeFailure, wait_benchmark_terminal

_RUN_ID = "abc123def456"


def _terminal_meta(state: str = "completed", exit_code: Any = 0, run_id: str = _RUN_ID) -> dict[str, Any]:
    return {"run_id": run_id, "terminal": {"state": state, "exit_code": exit_code}}


def _make_run_dir(runs_root: Path, name: str, meta: dict[str, Any]) -> Path:
    run_dir = runs_root / name
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8"
    )
    return run_dir


def _fetcher_seq(responses: list[tuple[int, str]]) -> Any:
    """形如 http(url, **kwargs) -> (status, text) 的假 fetcher；耗尽后重复最后一个。

    调用计数挂在 .calls 上供断言「POST/轮询次数」与时序；kwargs 必须接受
    timeout=（调用点传剩余预算，上限 5），与生产 http 的形状一致。
    """
    state = {"n": 0}

    def fetch(url: str, **kwargs: Any) -> tuple[int, str]:
        i = min(state["n"], len(responses) - 1)
        state["n"] += 1
        return responses[i]

    fetch.state = state  # type: ignore[attr-defined]
    return fetch


@pytest.fixture()
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """轮询间隙不真等：终态机循环里的 time.sleep 换成空操作。

    只动 sleep、不动 monotonic——期限语义仍按真实时钟判定。
    """
    monkeypatch.setattr(wheel_smoke.time, "sleep", lambda _s: None)


class TestWaitBenchmarkTerminal:
    def test_terminal_returns_matching_run_dir(self, tmp_path: Path, no_sleep: None) -> None:
        runs = tmp_path / "runs"
        made = _make_run_dir(runs, "run-x", _terminal_meta())
        fetch = _fetcher_seq([(200, json.dumps({"running": False, "run_id": _RUN_ID}))])
        got = wait_benchmark_terminal(
            "http://127.0.0.1:1", _RUN_ID, runs, timeout=5, fetcher=fetch
        )
        assert got == made

    def test_5xx_then_terminal_retries_within_deadline(self, tmp_path: Path, no_sleep: None) -> None:
        runs = tmp_path / "runs"
        made = _make_run_dir(runs, "run-x", _terminal_meta())
        body = json.dumps({"running": False, "run_id": _RUN_ID})
        fetch = _fetcher_seq([(503, "unavailable"), (500, "boom"), (200, body)])
        got = wait_benchmark_terminal("http://x", _RUN_ID, runs, timeout=5, fetcher=fetch)
        assert got == made
        assert fetch.state["n"] == 3, "临时 5xx 应在同一期限内重试而不是立刻失败"

    def test_connection_failure_retries(self, tmp_path: Path, no_sleep: None) -> None:
        runs = tmp_path / "runs"
        _make_run_dir(runs, "run-x", _terminal_meta())
        body = json.dumps({"running": False, "run_id": _RUN_ID})
        fetch = _fetcher_seq([(0, ""), (0, ""), (200, body)])
        got = wait_benchmark_terminal("http://x", _RUN_ID, runs, timeout=5, fetcher=fetch)
        assert got
        assert fetch.state["n"] == 3

    def test_4xx_fails_immediately_not_as_finished(self, tmp_path: Path, no_sleep: None) -> None:
        """4xx 是确定错误：立刻失败。旧缺陷正是「非 200 → 空 dict → running 缺失
        立即退出等待」——应用中途崩掉也会从这里被当成已收尾。"""
        runs = tmp_path / "runs"
        fetch = _fetcher_seq([(404, "not found")])
        with pytest.raises(SmokeFailure, match="HTTP 404"):
            wait_benchmark_terminal("http://x", _RUN_ID, runs, timeout=30, fetcher=fetch)
        assert fetch.state["n"] == 1, "确定错误不得拖满期限"

    def test_200_with_broken_json_is_protocol_failure(self, tmp_path: Path, no_sleep: None) -> None:
        runs = tmp_path / "runs"
        fetch = _fetcher_seq([(200, "{definitely-not-json")])
        with pytest.raises(SmokeFailure, match="不是 JSON 对象"):
            wait_benchmark_terminal("http://x", _RUN_ID, runs, timeout=30, fetcher=fetch)

    def test_missing_run_id_is_transitional_keeps_waiting(self, tmp_path: Path, no_sleep: None) -> None:
        """running False 但没带 run_id 是过渡状态：继续等，不判完成也不判失败。"""
        runs = tmp_path / "runs"
        made = _make_run_dir(runs, "run-x", _terminal_meta())
        fetch = _fetcher_seq(
            [
                (200, json.dumps({"running": False})),
                (200, json.dumps({"running": False, "run_id": _RUN_ID})),
            ]
        )
        got = wait_benchmark_terminal("http://x", _RUN_ID, runs, timeout=5, fetcher=fetch)
        assert got == made
        assert fetch.state["n"] == 2

    def test_other_run_id_never_accepted_as_finished(self, tmp_path: Path, no_sleep: None) -> None:
        """非空但不同的 run_id 是别人的运行：不得接受为本次完成。"""
        runs = tmp_path / "runs"
        _make_run_dir(runs, "run-x", _terminal_meta(run_id="other-run-999"))
        fetch = _fetcher_seq([(200, json.dumps({"running": False, "run_id": "other-run-999"}))])
        with pytest.raises(SmokeFailure, match="未在.*内出现按 run_id 核对的终态"):
            wait_benchmark_terminal("http://x", _RUN_ID, runs, timeout=2, fetcher=fetch)

    def test_meta_missing_then_appears_by_run_id(self, tmp_path: Path, no_sleep: None) -> None:
        """meta 由 supervisor 终态时才补写（_write_json 非原子）：缺失按期限重试。"""
        runs = tmp_path / "runs"
        responses = [
            (200, json.dumps({"running": False, "run_id": _RUN_ID})),
            (200, json.dumps({"running": False, "run_id": _RUN_ID})),
        ]
        calls = {"n": 0}

        def fetch(url: str, **kwargs: Any) -> tuple[int, str]:
            i = min(calls["n"], len(responses) - 1)
            calls["n"] += 1
            if calls["n"] == 2:
                _make_run_dir(runs, "run-late", _terminal_meta())
            return responses[i]

        got = wait_benchmark_terminal("http://x", _RUN_ID, runs, timeout=5, fetcher=fetch)
        assert got.name == "run-late"

    def test_half_written_meta_retried(self, tmp_path: Path, no_sleep: None) -> None:
        """半写 JSON 允许等待重试（与 wrapper 原子 status 的规则刻意不同）。"""
        runs = tmp_path / "runs"
        broken = runs / "run-half"
        broken.mkdir(parents=True)
        (broken / "meta.json").write_text('{"run_id": "', encoding="utf-8")
        calls = {"n": 0}
        body = json.dumps({"running": False, "run_id": _RUN_ID})

        def fetch(url: str, **kwargs: Any) -> tuple[int, str]:
            calls["n"] += 1
            if calls["n"] == 2:
                (broken / "meta.json").write_text(json.dumps(_terminal_meta()), encoding="utf-8")
            return (200, body)

        got = wait_benchmark_terminal("http://x", _RUN_ID, runs, timeout=5, fetcher=fetch)
        assert got == broken

    def test_terminal_without_int_exit_code_not_accepted(self, tmp_path: Path, no_sleep: None) -> None:
        """exit_code 为 bool/None 不算终态：不能拿半写的 meta 当完成。

        判别构造：先只有 exit_code=True 的目录（bool 是 int 子类，最容易被
        误接受）；真整数退出码的目录在第二次轮询才出现。若实现把 bool 当
        合法退出码，第一次就会命中 run-bool——用例按目录名区分两者。
        """
        runs = tmp_path / "runs"
        _make_run_dir(runs, "run-bool", _terminal_meta(exit_code=True))
        body = json.dumps({"running": False, "run_id": _RUN_ID})
        calls = {"n": 0}

        def fetch(url: str, **kwargs: Any) -> tuple[int, str]:
            calls["n"] += 1
            if calls["n"] == 2:
                _make_run_dir(runs, "run-real", _terminal_meta(exit_code=1))
            return (200, body)

        got = wait_benchmark_terminal("http://x", _RUN_ID, runs, timeout=5, fetcher=fetch)
        assert got.name == "run-real", "bool 退出码不得被接受为终态"

    def test_terminal_with_none_exit_code_not_accepted(self, tmp_path: Path, no_sleep: None) -> None:
        """exit_code 为 None 同样不算终态：终态行落了但退出码还没写。

        与 bool 用例同一矩阵的另一格：None 既不是 bool 也不是 int，最容易被
        「字段存在即接受」的实现放行。
        """
        runs = tmp_path / "runs"
        _make_run_dir(runs, "run-none", _terminal_meta(exit_code=None))
        body = json.dumps({"running": False, "run_id": _RUN_ID})
        calls = {"n": 0}

        def fetch(url: str, **kwargs: Any) -> tuple[int, str]:
            calls["n"] += 1
            if calls["n"] == 2:
                _make_run_dir(runs, "run-real", _terminal_meta(exit_code=1))
            return (200, body)

        got = wait_benchmark_terminal("http://x", _RUN_ID, runs, timeout=5, fetcher=fetch)
        assert got.name == "run-real", "None 退出码不得被接受为终态"

    def test_nonzero_exit_code_accepted(self, tmp_path: Path, no_sleep: None) -> None:
        """exit_code 非零同样算收尾：本步骤验证「失败也能收尾」，不要求业务成功。"""
        runs = tmp_path / "runs"
        made = _make_run_dir(runs, "run-fail", _terminal_meta(state="failed-recorded", exit_code=7))
        fetch = _fetcher_seq([(200, json.dumps({"running": False, "run_id": _RUN_ID}))])
        got = wait_benchmark_terminal("http://x", _RUN_ID, runs, timeout=5, fetcher=fetch)
        assert got == made

    def test_aborted_state_accepted(self, tmp_path: Path, no_sleep: None) -> None:
        runs = tmp_path / "runs"
        made = _make_run_dir(runs, "run-abort", _terminal_meta(state="aborted", exit_code=-1))
        fetch = _fetcher_seq([(200, json.dumps({"running": False, "run_id": _RUN_ID}))])
        got = wait_benchmark_terminal("http://x", _RUN_ID, runs, timeout=5, fetcher=fetch)
        assert got == made

    def test_deadline_is_absolute_failure(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """期限结束无条件失败：不能因为最后一个响应 dict「看着像完成」就放行。"""
        runs = tmp_path / "runs"
        stamps = [0.0, 0.0, 999.0]  # 第一次取 now、循环内两次检查跨过期限
        monkeypatch.setattr(wheel_smoke.time, "sleep", lambda _s: None)
        monkeypatch.setattr(wheel_smoke.time, "monotonic", lambda: stamps.pop(0) if stamps else 999.0)
        fetch = _fetcher_seq([(0, "")])
        with pytest.raises(SmokeFailure, match="未在"):
            wait_benchmark_terminal("http://x", _RUN_ID, runs, timeout=5, fetcher=fetch)

    def test_terminal_located_after_deadline_is_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """目录定位完成时已跨过期限：合法终态也不得被接受。

        判别构造：时钟在 _locate_run_dir 返回前才推过期限——循环开头与响应返回后
        的核对都通过，只有「接受终态前」的核对能拦下；去掉那一次核对，用例会因为
        直接返回目录而失败。
        """
        runs = tmp_path / "runs"
        made = _make_run_dir(runs, "run-late", _terminal_meta())
        clock = {"now": 0.0}
        monkeypatch.setattr(wheel_smoke.time, "sleep", lambda _s: None)
        monkeypatch.setattr(wheel_smoke.time, "monotonic", lambda: clock["now"])
        real_locate = wheel_smoke._locate_run_dir
        located: list[Path | None] = []

        def locate(runs_root: Path, run_id: str, deadline: float) -> Path | None:
            found = real_locate(runs_root, run_id, deadline)
            located.append(found)
            clock["now"] = 999.0  # 定位期间跨过期限
            return found

        monkeypatch.setattr(wheel_smoke, "_locate_run_dir", locate)
        calls = {"n": 0}

        def fetch(url: str, **kwargs: Any) -> tuple[int, str]:
            calls["n"] += 1
            return (200, json.dumps({"running": False, "run_id": _RUN_ID}))

        with pytest.raises(SmokeFailure, match="未在.*内出现按 run_id 核对的终态"):
            wait_benchmark_terminal("http://x", _RUN_ID, runs, timeout=5, fetcher=fetch)
        assert located == [made], "目录确实定位到了，失败来自期限核对而不是没找到目录"
        assert calls["n"] == 1, "跨过期限后不得再轮询"

    def test_latest_name_dir_with_other_run_id_is_not_used(self, tmp_path: Path, no_sleep: None) -> None:
        """定位按 meta.run_id，不按名字排序的「最新目录」：别的运行目录恰排在最新也不误认。"""
        runs = tmp_path / "runs"
        _make_run_dir(runs, "zzz-latest-other", _terminal_meta(run_id="other-999"))
        mine = _make_run_dir(runs, "aaa-mine", _terminal_meta())
        fetch = _fetcher_seq([(200, json.dumps({"running": False, "run_id": _RUN_ID}))])
        got = wait_benchmark_terminal("http://x", _RUN_ID, runs, timeout=5, fetcher=fetch)
        assert got == mine


class TestRuntimeDirProbeGuard:
    """探针的文本守卫（不是行为测试）：钉住「只导入 paths」这一形态。

    探针一旦重新 import supervisor/config，就会连带构造 Settings、访问真实
    钥匙串与 .env——探针本身变成泄漏源，且这类回潮在 CI 上不一定报错（干净
    runner 里钥匙串访问可能静默成功）。行为面由冒烟主链路的真实目录前后快照
    覆盖；这里守的是导入面，把片段提为模块常量后守卫直接断言该常量本体。
    """

    def test_probe_imports_only_paths(self) -> None:
        probe = wheel_smoke.RUNTIME_DIR_PROBE
        assert "import briefdesk.paths" in probe
        assert "supervisor" not in probe, "探针不得触碰 supervisor 导入链"
        assert "briefdesk.config" not in probe and "import config" not in probe, "探针不得构造 Settings"
        assert "keyring" not in probe

    def test_probe_prints_benchmark_runs_dir(self) -> None:
        assert "benchmark_runs_dir()" in wheel_smoke.RUNTIME_DIR_PROBE

    def test_probe_still_carries_installed_guard(self) -> None:
        """探针必须带着「导入落在 site-packages」的守卫：源码树会掩盖非安装包的问题。"""
        assert wheel_smoke.RUNTIME_DIR_PROBE.startswith(wheel_smoke.INSTALLED_GUARD)
