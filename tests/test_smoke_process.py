"""smoke_process 的协议与所有权回归。

纯协议部分（状态解析）直接测；进程部分用**真实短命子进程**验证（不需要 CI 的
合成进程树 job 也能覆盖的路径）：正常完成、非零退出、包装层失败、许可门、
close_all 回收。Windows 侧的 Job 断言走真实 ctypes 路径——本仓库主平台就是
Windows，本地能跑；POSIX 分支只做导入级冒烟（CI 的 isolation-smoke 覆盖行为）。

数据一律虚构（见 AGENTS.md）。
"""

import json
import sys
from pathlib import Path

import pytest

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

    def test_close_all_is_idempotent_and_aggregates(self, root: Path) -> None:
        """close_all 处理全部已登记对象：单个失败不漏其余（聚合报错）。"""
        cmds = SmokeCommands(root)
        cmds.run([sys.executable, "-c", "print('a')"], timeout=60)
        cmds.run([sys.executable, "-c", "print('b')"], timeout=60)
        cmds.close_all()
        assert cmds._owned == []
        # 重复调用不抛（幂等）
        cmds.close_all()