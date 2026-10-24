"""smoke_process 的协议与所有权回归。

纯协议部分（状态解析）直接测；进程部分用**真实短命子进程**验证（不需要 CI 的
合成进程树 job 也能覆盖的路径）：正常完成、非零退出、包装层失败、许可门、
close_all 回收。Windows 侧的 Job 断言走真实 ctypes 路径——本仓库主平台就是
Windows，本地能跑；POSIX 分支的行为用例（真实 spawn / 进程组信号清理）在 CI
的 isolation-smoke job（Linux）上执行，Windows 本地只跑协议与注入替身用例。

数据一律虚构（见 AGENTS.md）。
"""

import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from scripts import smoke_process
from scripts.smoke_process import (
    SmokeCommandError,
    SmokeCommands,
    parse_status,
)


class TestParseStatus:
    def test_exit_status_accepts_integer(self) -> None:
        assert parse_status(b'{"kind": "exit", "returncode": 0}') == {"kind": "exit", "returncode": 0}

    def test_exit_status_rejects_bool_and_non_int(self) -> None:
        for payload in (b'{"kind": "exit", "returncode": true}', b'{"kind": "exit", "returncode": null}'):
            with pytest.raises(SmokeCommandError, match="returncode 必须是整数"):
                parse_status(payload)

    def test_rejects_broken_json_immediately(self) -> None:
        """状态存在但损坏 = 协议失败，立即报错而不是傻等到超时。"""
        with pytest.raises(SmokeCommandError, match="不是合法 JSON"):
            parse_status(b"{")

    def test_rejects_unknown_kind(self) -> None:
        with pytest.raises(SmokeCommandError, match="未知状态 kind"):
            parse_status(b'{"kind": "mystery"}')

    def test_error_status_requires_strings(self) -> None:
        with pytest.raises(SmokeCommandError, match="error_type 与 message"):
            parse_status(b'{"kind": "error", "error_type": 1, "message": "x"}')


class TestSmokeCommands:
    @pytest.fixture()
    def root(self, tmp_path: Path) -> Path:
        return tmp_path

    def test_run_returns_child_output(self, root: Path) -> None:
        cmds = SmokeCommands(root)
        try:
            out = cmds.run([sys.executable, "-c", "print('marker-ok')"], timeout=60)
            assert "marker-ok" in out
        finally:
            cmds.close_all()

    def test_run_nonzero_raises_with_log_tail(self, root: Path) -> None:
        cmds = SmokeCommands(root)
        try:
            with pytest.raises(SmokeCommandError, match="命令失败"):
                cmds.run([sys.executable, "-c", "print('boom-marker'); raise SystemExit(7)"], timeout=60)
        finally:
            cmds.close_all()

    def test_run_timeout_raises_and_cleans(self, root: Path) -> None:
        """超时不挂死：状态文件永远不来，期限到即失败，进程树被回收。"""
        cmds = SmokeCommands(root)
        try:
            with pytest.raises(SmokeCommandError, match="未完成"):
                cmds.run([sys.executable, "-c", "import time; time.sleep(60)"], timeout=3)
        finally:
            cmds.close_all()

    def test_command_directory_has_log_and_status(self, root: Path) -> None:
        cmds = SmokeCommands(root)
        try:
            cmds.run([sys.executable, "-c", "print('x')"], timeout=60)
            cmd_dir = next((root / "commands").iterdir())
            assert (cmd_dir / "output.log").exists()
            status = json.loads((cmd_dir / "status.json").read_text(encoding="utf-8"))
            assert status["kind"] == "exit"
        finally:
            cmds.close_all()

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows Job 路径")
    def test_job_assigned_on_windows(self, root: Path) -> None:
        """Windows 上命令必须进 Job：句柄在清理后被关闭、Popen 仍由容器持有。"""
        cmds = SmokeCommands(root)
        try:
            cmds.run([sys.executable, "-c", "print('j')"], timeout=60)
            owned = cmds._owned[0]
            assert owned.proc is not None and owned.proc.returncode == 0
            assert owned._job_handle is None, "清理后句柄必须已关闭"
            assert owned._cleaned
        finally:
            cmds.close_all()

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows Job 路径")
    def test_process_is_actually_inside_the_job(self, root: Path) -> None:
        """句柄存在还不够：进程必须真的在 Job 里（否则 KILL_ON_JOB_CLOSE 只是摆设）。

        用长驻子进程做探针：启动后查询 Job 的 ActiveProcesses，应当 ≥ 1；
        close_all 之后再查计数归零（说明清理确实按 Job 走的）。
        """
        cmds = SmokeCommands(root)
        owned = cmds.start_app(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            env=dict(os.environ),
            cwd=root,
        )
        try:
            assert owned._job_handle is not None, "Windows 上必须已指进 Job"
            active = smoke_process._active_processes(owned._job_handle)
            assert active is not None, "应能查询 Job 的 ActiveProcesses"
            assert active >= 1, f"命令进程不在 Job 内（ActiveProcesses={active}）"
        finally:
            cmds.close_all()
        assert owned.reaped, "清理后必须已回收包装进程"

    def test_close_all_is_idempotent_and_aggregates(self, root: Path) -> None:
        """close_all 处理全部已登记对象：单个失败不漏其余（聚合报错）。"""
        cmds = SmokeCommands(root)
        cmds.run([sys.executable, "-c", "print('a')"], timeout=60)
        cmds.run([sys.executable, "-c", "print('b')"], timeout=60)
        cmds.close_all()
        assert cmds._owned == []
        # 重复调用不抛（幂等）
        cmds.close_all()

class TestSpawnReleaseOrdering:
    """纳管后才放行的规范时序与 run() 的状态文件事实原则。

    旧实现的两个失败模式：先写 GO 再纳管，实际命令在 AssignProcessToJobObject
    前启动、整体落在 Job 外（KILL_ON_JOB_CLOSE 兜底旁路）；放行后用 wrapper
    退出码反推命令失败，把「发布状态后 wrapper 被终止收尾」误报成命令失败，
    负载下连 run() 都会因「句柄已回收」误报成 runner 不允许嵌套 Job。
    """

    @pytest.fixture()
    def root(self, tmp_path: Path) -> Path:
        return tmp_path

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows Job 路径")
    def test_real_command_pid_is_inside_the_job(self, root: Path) -> None:
        """真实命令的 PID 必须在 Job 内（ActiveProcesses 计数抓不到「只有 wrapper
        在内」的错序，必须按 PID 查）。同步手法：命令先写哨兵文件再阻塞，测试轮询
        哨兵出现后按其中记录的 PID 查归属——start_app 立即返回，恰好提供断言窗口。
        """
        sentinel = root / "sentinel.pid"
        script = (
            "from pathlib import Path; import os, time;"
            + " Path(r'" + str(sentinel).replace("'", "''") + "').write_text(str(os.getpid()));"
            + " time.sleep(30)"
        )
        cmds = SmokeCommands(root)
        owned = cmds.start_app(
            [sys.executable, "-c", script],
            env=dict(os.environ),
            cwd=root,
        )
        try:
            deadline = 10.0
            while not sentinel.exists() and deadline > 0:
                time.sleep(0.05)
                deadline -= 0.05
            assert sentinel.exists(), "实际命令未在期限内启动"
            pid = int(sentinel.read_text(encoding="utf-8"))
            assert owned._job_handle is not None, "Windows 上必须已指进 Job"
            assert smoke_process._process_in_job(
                owned._job_handle, pid
            ), "实际命令不在 Job 内——放行早于纳管，Job 兜底旁路"
        finally:
            cmds.close_all()

    def test_wrapper_linger_after_status_does_not_fail_run(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """发布完成状态后 wrapper 逗留：run() 仍正常返回（防 TerminateJobObject
        误杀 + 退出码反推误报）。构造缝隙：wrapper 源码在发布状态后 sleep 数秒——
        插入点必须在 main() 的 publish 与 return 之间，脚本体以 sys.exit(main())
        结尾，追加到末尾不可达，会造出假 linger 让用例照绿。"""
        old_line = '    _publish(status_path, _status_json({"kind": "exit", "returncode": code}))'
        new_line = old_line + "\n    import time as _linger\n    _linger.sleep(3.0)"
        linger_source = smoke_process._WRAPPER_SOURCE.replace(old_line, new_line)
        assert linger_source != smoke_process._WRAPPER_SOURCE, "变体构造失败"
        monkeypatch.setattr(smoke_process, "_WRAPPER_SOURCE", linger_source)
        cmds = SmokeCommands(root)
        try:
            out = cmds.run([sys.executable, "-c", "print('linger-ok')"], timeout=60)
            assert "linger-ok" in out
        finally:
            cmds.close_all()

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows Job 路径")
    def test_wrapper_early_exit_before_release_reported_as_early_exit(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """wrapper 在放行前自行退出：报「包装进程提前退出」，不误报嵌套 Job 被拒。

        新时序下 poll 紧邻 spawn，真实 wrapper 不可能确定性地在此之前退出——
        把 _WRAPPER_SOURCE 换成立即退出的变体，并把 spawn 包装成「等它退出后
        再返回」，poll 必然看到已退出。

        「提前退出」判定只在 Windows 分支（POSIX 的 poll 会回收子进程，一旦回收就
        无法再向它所在的进程组发信号），所以本用例限定 Windows：POSIX 上它等满总
        期限后报的是「未完成」，与生产分支不匹配。"""
        exit_source = smoke_process._WRAPPER_SOURCE.replace(
            "    while True:",
            "    import sys as _s\n    _s.exit(9)\n    while True:",
        )
        assert exit_source != smoke_process._WRAPPER_SOURCE, "变体构造失败"
        monkeypatch.setattr(smoke_process, "_WRAPPER_SOURCE", exit_source)
        real_spawn = SmokeCommands._spawn_wrapper

        def _spawn_then_wait(self, owned, cmd, *, env, cwd):
            proc = real_spawn(self, owned, cmd, env=env, cwd=cwd)
            proc.wait(timeout=10)
            # 已 wait 回收的进程不得再向旧进程组发信号（组号可能已被复用）：桩在这里
            # 补上真实清理路径会置的标记，保持「回收过就只报告不误杀」的不变式。
            owned.reaped = True
            return proc

        monkeypatch.setattr(SmokeCommands, "_spawn_wrapper", _spawn_then_wait)
        cmds = SmokeCommands(root)
        released: list[bool] = []

        real_release = SmokeCommands._release

        def _tracking_release(owned):
            released.append(True)
            real_release(owned)

        # 生产 _release 是 @staticmethod，调用点是 self._release(owned)：桩以普通函数挂到
        # 类上会被绑定、多收一个 self 而抛 TypeError，released 永远为空——断言恒真、等于
        # 没测。包成 staticmethod 才让跟踪生效：把放行提到提前退出判定之前时，本用例必须
        # 给出 assert released == [] 的失败，而不是 TypeError。
        monkeypatch.setattr(SmokeCommands, "_release", staticmethod(_tracking_release))
        try:
            with pytest.raises(SmokeCommandError, match="提前退出") as exc_info:
                cmds.run([sys.executable, "-c", "print('never')"], timeout=30)
            assert "嵌套 Job" not in str(exc_info.value)
            assert released == [], "提前退出路径不得写放行"
        finally:
            cmds.close_all()

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows Job 路径")
    def test_assign_failure_keeps_gate_pending_and_cleans(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """纳管失败（伪造内核拒绝）：保持 PENDING、真实命令未启动、清理后显式报错。"""
        monkeypatch.setattr(
            smoke_process, "_assign_process_to_job", lambda job, handle: False
        )
        cmds = SmokeCommands(root)
        try:
            with pytest.raises(SmokeCommandError, match="AssignProcessToJobObject 失败"):
                cmds.run([sys.executable, "-c", "print('never')"], timeout=30)
            owned = cmds._owned[0]
            assert owned.gate_path.read_bytes() == b"PENDING", "纳管失败不得写放行"
        finally:
            cmds.close_all()


class TestJobCleanupConfirmsZero:
    """Job 清理必须确认归零，未确认即失败。

    旧实现的快速分支在状态文件已发布、wrapper 自然退出后直接置 reaped 并返回，既不查
    ActiveProcesses 也不调 _terminate_job：wrapper 只等它的直接子进程，孙进程可能仍在
    Job 内，清理结果不可知却报成功。另一侧 _terminate_job 归零超时后既不抛也不返回状态，
    注释却写着「调用方据此记失败」。
    """

    @pytest.fixture()
    def root(self, tmp_path: Path) -> Path:
        return tmp_path

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows Job 路径")
    def test_cleanup_confirms_job_zeroed_after_natural_wrapper_exit(
        self, root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """wrapper 自然退出但孙进程仍在 Job 内：清理必须终止并确认归零。

        进程树形状是本用例的前提：直接子进程 spawn 一个活得更久的孙进程（两条命令串进
        同一个 -c 顺序执行不产生孙进程，用例会空跑、缺陷代码下也通过）。两个 PID 都写进
        哨兵文件，先按 PID 分别断言它们确实在 Job 内。

        归零采样必须发生在句柄关闭**之前**：close_all 返回时句柄已在 finally 关闭，事后
        查不到计数。因此截获 _close_handle，在委托真实关闭前查一次 ActiveProcesses。
        """
        sentinel = root / "tree.json"
        child_code = (
            "import json, os, subprocess, sys, time\n"
            "from pathlib import Path\n"
            "grand = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            "Path(r'" + str(sentinel) + "').write_text("
            "json.dumps([os.getpid(), grand.pid]), encoding='utf-8')\n"
            "time.sleep(5.0)\n"
        )
        cmds = SmokeCommands(root)
        owned = cmds.start_app([sys.executable, "-c", child_code], env=dict(os.environ), cwd=root)
        try:
            # 哨兵用 write_text 直接发布：「文件已存在但内容未写完」的窗口会让读取拿到
            # 空串或半截内容，轮询必须容忍并重试。
            pids: list[int] = []
            deadline = 20.0
            while len(pids) != 2 and deadline > 0:
                try:
                    loaded = json.loads(sentinel.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    loaded = None
                if isinstance(loaded, list) and len(loaded) == 2 and all(
                    isinstance(pid, int) for pid in loaded
                ):
                    pids = loaded
                else:
                    time.sleep(0.05)
                    deadline -= 0.05
            assert len(pids) == 2, "直接子进程未在期限内写出完整进程树哨兵"
            child_pid, grand_pid = pids
            job = owned._job_handle
            assert job is not None, "Windows 上必须已指进 Job"
            assert smoke_process._process_in_job(job, child_pid), "直接子进程不在 Job 内"
            assert smoke_process._process_in_job(job, grand_pid), "孙进程不在 Job 内"

            # 状态文件发布即代表 wrapper 已等到直接子进程结束
            deadline = 20.0
            while not owned.status_path.exists() and deadline > 0:
                time.sleep(0.05)
                deadline -= 0.05
            assert owned.status_path.exists(), "wrapper 未在期限内发布完成状态"

            real_close = smoke_process._close_handle
            sampled: list[int | None] = []

            def _recording_close(handle: Any) -> None:
                if handle is not None and handle == job:
                    sampled.append(smoke_process._active_processes(handle))
                real_close(handle)

            monkeypatch.setattr(smoke_process, "_close_handle", _recording_close)
            cmds.close_all(timeout=10)
            assert sampled == [0], f"Job 句柄关闭时仍有活动进程：{sampled}"
        finally:
            cmds.close_all()

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows Job 路径")
    def test_terminate_job_timeout_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """归零超时不得静默：旧实现只在注释里说「调用方据此记失败」，实际什么都不做。"""
        monkeypatch.setattr(smoke_process, "_JOB_ZERO_WAIT_FLOOR", 0.05)
        monkeypatch.setattr(smoke_process, "_active_processes", lambda _job: 1)
        monkeypatch.setattr(smoke_process._kernel32, "TerminateJobObject", lambda _job, _code: 1)
        with pytest.raises(SmokeCommandError, match="未归零"):
            smoke_process._terminate_job(object(), grace_seconds=0.0)

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows Job 路径")
    def test_active_processes_query_failure_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """查询失败（None）不能当成归零：句柄随即关闭，之后再也查不到计数。"""
        monkeypatch.setattr(smoke_process, "_active_processes", lambda _job: None)
        monkeypatch.setattr(smoke_process._kernel32, "TerminateJobObject", lambda _job, _code: 1)
        with pytest.raises(SmokeCommandError, match="查询失败"):
            smoke_process._terminate_job(object(), grace_seconds=0.0)


class TestPosixSigchldPrecondition:
    """POSIX 启动前置：宿主自动回收 SIGCHLD 时拒绝构造，不静默改写宿主处理器。

    被忽略或自定义回收时 wrapper 退出即被内核收走，组信号与 wait 的时序失去依据
    （清理结果不可知却报成功）。Windows 分支不走这条路（Job 内核对象负责回收），
    所以用例显式把 _IS_WINDOWS 置 False。

    Windows 的 signal 模块没有 SIGCHLD：必须先把该属性注入模块命名空间，否则生产
    代码在实参求值处就抛 AttributeError，三例会以错误（而非按设计的拒绝/通过）
    收场——「Windows 也能覆盖这条前置」的前提正是这个注入。
    """

    @pytest.fixture()
    def sigchld_probe(self, monkeypatch: pytest.MonkeyPatch) -> list[int]:
        """构造 POSIX 前提并返回「假 getsignal 收到的信号号」记录表。"""
        monkeypatch.setattr(smoke_process, "_IS_WINDOWS", False)
        monkeypatch.setattr(signal, "SIGCHLD", 17, raising=False)
        return []

    def test_ignored_sigchld_is_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sigchld_probe: list[int]
    ) -> None:
        def fake_getsignal(sig: int) -> Any:
            sigchld_probe.append(sig)
            return signal.SIG_IGN

        monkeypatch.setattr(signal, "getsignal", fake_getsignal)
        with pytest.raises(SmokeCommandError, match="SIGCHLD 为默认处置"):
            SmokeCommands(tmp_path)
        assert sigchld_probe == [signal.SIGCHLD], "检查必须针对 SIGCHLD"  # type: ignore[attr-defined]

    def test_custom_sigchld_handler_is_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sigchld_probe: list[int]
    ) -> None:
        def handler(signum: int, frame: Any) -> None:  # pragma: no cover - 不会被调用
            pass

        def fake_getsignal(sig: int) -> Any:
            sigchld_probe.append(sig)
            return handler

        monkeypatch.setattr(signal, "getsignal", fake_getsignal)
        with pytest.raises(SmokeCommandError, match="SIGCHLD 为默认处置"):
            SmokeCommands(tmp_path)
        assert sigchld_probe == [signal.SIGCHLD], "检查必须针对 SIGCHLD"  # type: ignore[attr-defined]

    def test_default_sigchld_disposition_constructs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sigchld_probe: list[int]
    ) -> None:
        """默认处置是脚本的固有前提：检查必须放行，不能一律拒绝。"""

        def fake_getsignal(sig: int) -> Any:
            sigchld_probe.append(sig)
            return signal.SIG_DFL

        monkeypatch.setattr(signal, "getsignal", fake_getsignal)
        cmds = SmokeCommands(tmp_path)
        assert cmds._owned == []
        assert sigchld_probe == [signal.SIGCHLD], "检查必须针对 SIGCHLD"  # type: ignore[attr-defined]