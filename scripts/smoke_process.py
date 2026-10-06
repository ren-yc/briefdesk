"""wheel 冒烟的命令所有权管理：许可包装进程 + 文件日志 + 原子完成状态。

为什么不用 subprocess.run 的管道捕获：Windows 上 run(timeout) 杀掉直接子进程后再读管道，
孙进程若继承了管道写端，读取会一直挂到孙进程退出——冒烟进程树（venv/pip/console script/
基准 runner）层层嵌套，一处挂住就拖死整个 job。这里全部输出走**文件日志**，完成信号走
**原子状态文件**，等待侧没有任何管道可被孙进程握住。

所有权模型：
- Windows：每个命令包进一个「许可包装进程」，实际命令加入 Job 对象（KILL_ON_JOB_CLOSE）。
  父进程把 Popen 持有的进程句柄（_handle）指给 Job——不按 PID 重新 OpenProcess，避免
  句柄已退出、PID 被复用时把别的进程指进 Job。
- POSIX：包装进程以独立会话组长启动（start_new_session），实际命令及其后代继承进程组；
  终止先对组发 SIGTERM、宽限后最后一次 SIGKILL，之后才 wait 回收包装进程。
- 父进程硬死时：Windows 由 KILL_ON_JOB_CLOSE 内核兜底；POSIX 不承诺（这是脚本级
  所有权，不是内核容器——不要把它当 Job 的等价物）。

状态文件协议（status.json，写临时文件后 os.replace 原子发布，只发布一次）：
    {"kind": "exit", "returncode": <int>}      实际命令的退出码
    {"kind": "error", "error_type": "...", "message": "..."}  包装层失败（启动失败等）
状态存在但 JSON/schema 非法 → 立即失败（不是继续等）；缺失才按总期限等待。
"""

from __future__ import annotations

import ctypes
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

_IS_WINDOWS = os.name == "nt"

_STATUS_NAME = "status.json"

#: 已发布完成状态后等 wrapper 自然退出的宽限。发布状态意味着实际命令已结束，
#: wrapper 只剩收尾——正常毫秒级退出；超时才升级 TerminateJobObject，避免把
#: 刚写完状态文件的 wrapper 误杀（其退出码不参与命令成败判定，误杀也不影响
#: 结果，但会留下难排查的清理噪音）。
_WRAPPER_EXIT_GRACE = 2.0

#: TerminateJobObject 之后等 ActiveProcesses 归零的等待下限。内核回收进程有个短
#: 窗口，只按调用方给的宽限（失败路径常传 0）会把「还没归零」误判成清理失败，CI 上
#: 表现为抖动。独立成常量是为了让用例能把它压到可测的小值（回归见
#: tests/test_smoke_process.py 的归零超时与查询失败用例）。
_JOB_ZERO_WAIT_FLOOR = 5.0


class SmokeCommandError(RuntimeError):
    """冒烟命令管理失败（协议/清理/超时）：携带可直接定位的说明。"""


def parse_status(payload: bytes) -> dict[str, Any]:
    """解析并校验状态载荷；非法即抛 SmokeCommandError（立即失败，不等待）。"""
    try:
        data = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SmokeCommandError(f"状态文件不是合法 JSON: {error}") from error
    if not isinstance(data, dict):
        raise SmokeCommandError("状态文件必须是 JSON 对象")
    kind = data.get("kind")
    if kind == "exit":
        code = data.get("returncode")
        if isinstance(code, bool) or not isinstance(code, int):
            raise SmokeCommandError("exit 状态的 returncode 必须是整数")
        return data
    if kind == "error":
        if not isinstance(data.get("error_type"), str) or not isinstance(data.get("message"), str):
            raise SmokeCommandError("error 状态必须有 error_type 与 message 字符串")
        return data
    raise SmokeCommandError(f"未知状态 kind: {kind!r}")


# ── 许可包装进程 ──
# 独立脚本体（不是 -c 内联）：引号嵌套与转义在 Windows 命令行里不可靠，写成模块常量
# 经 stdin 传给解释器最稳。wrapper 只用标准库。
_WRAPPER_SOURCE = """
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def _publish(path, payload):
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, path)


def _status_json(payload):
    import json

    return json.dumps(payload)


def main() -> int:
    log_path = Path(sys.argv[1])
    status_path = Path(sys.argv[2])
    gate_path = Path(sys.argv[3])
    # 读到准确许可字节前不创建任何东西：轮询许可文件，直到内容恰为 GO——
    # 父进程死在许可前时无人写 GO，wrapper 会一直等；因此许可由父进程显式写入，
    # 并在写前先写 PENDING（父进程启动失败时由 close_all 终止 wrapper）。
    import time as _time

    while True:
        try:
            if gate_path.read_bytes() == b"GO":
                break
        except OSError:
            pass
        _time.sleep(0.02)
    cmd = sys.argv[4:]
    log = log_path.open("ab")
    try:
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW：实际命令也要，不只 wrapper
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, **kwargs)
    except Exception as error:
        _publish(status_path, _status_json({
            "kind": "error",
            "error_type": type(error).__name__,
            "message": str(error)[:300],
        }))
        return 2
    code = proc.wait()
    _publish(status_path, _status_json({"kind": "exit", "returncode": code}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
""".lstrip()


# ── 父进程侧管理 ──


class _OwnedCommand:
    """一条已登记命令：命令目录、包装进程句柄与平台侧的清理手段。

    对象先登记再启动：启动中途抛错时 close_all 仍能看到并回收它（部分启动失败
    不得漏对象）。清理失败的对象留在登记表里，close_all 继续处理其余命令。
    """

    def __init__(self, name: str, directory: Path) -> None:
        self.name = name
        self.directory = directory
        self.proc: subprocess.Popen[bytes] | None = None
        self.status_path = directory / _STATUS_NAME
        self.log_path = directory / "output.log"
        self.gate_path = directory / "gate"
        self._job_handle: Any = None  # Windows：Job 句柄（int）
        self._cleaned = False
        # POSIX：**启动时**记下进程组号。退出后 os.getpgid(pid) 会失败，
        # 而组号在回收前不会复用——启动时取一次是后续组信号的唯一可靠依据。
        self.pgid: int | None = None
        self.reaped = False  # 是否已 wait 回收（回收后不得再向旧 PGID 发信号）

    def started(self, proc: subprocess.Popen[bytes], job_handle: Any = None) -> None:
        self.proc = proc
        self._job_handle = job_handle


class SmokeCommands:
    """冒烟命令的所有权容器：run() 同步命令与 start_app() 长驻应用共用。

    根目录下每条命令一个独占子目录（commands/<seq>-<name>/），日志与状态文件
    都在里面——不能从 cwd 推导日志根，也不能用模块级临时路径（冒烟可能有多个
    实例，测试也需要隔离）。
    """

    def __init__(self, root: Path) -> None:
        self._root = root
        self._commands_dir = root / "commands"
        self._commands_dir.mkdir(parents=True, exist_ok=True)
        self._owned: list[_OwnedCommand] = []
        self._seq = 0
        self._close_errors: list[str] = []

    # ── 登记 ──

    def _new_command(self, name: str) -> _OwnedCommand:
        self._seq += 1
        directory = self._commands_dir / f"{self._seq:03d}-{name}"
        directory.mkdir(parents=True, exist_ok=False)
        owned = _OwnedCommand(name, directory)
        self._owned.append(owned)
        return owned

    # ── 平台细节：Windows Job / POSIX 组 ──

    def _assign_to_job(self, owned: _OwnedCommand, proc: subprocess.Popen[bytes]) -> None:
        """把 wrapper 进程（及其后代）指进 Kill-on-Close 的 Job。

        句柄来源是 **Popen 持有的进程句柄**：只在适配函数内借用 int(proc._handle)，
        强引用 Popen 保证句柄生命周期，不手动 CloseHandle，不按 PID 重新
        OpenProcess（PID 复用会把无关进程指进 Job）。私有接口不可用时显式失败，
        不静默退化为无 Job 启动。
        """
        if not _IS_WINDOWS:
            return
        job = _create_kill_on_close_job()
        assigned = False
        try:
            handle = getattr(proc, "_handle", None)
            if handle is None:
                raise SmokeCommandError("CPython 未暴露 Popen._handle——无法把命令指进 Job")
            assigned = _assign_process_to_job(job, int(handle))
        except SmokeCommandError:
            _close_handle(job)
            raise
        if not assigned:
            _close_handle(job)
            raise SmokeCommandError(
                f"AssignProcessToJobObject 失败（WinError {_last_error()}）——"
                "runner 环境可能不允许嵌套 Job，不绕开其管理"
            )
        owned.started(proc, job_handle=job)

    def _spawn_wrapper(self, owned: _OwnedCommand, cmd: list[str], *, env: dict[str, str], cwd: Path | None) -> subprocess.Popen[bytes]:
        """启动包装进程但不放行：实际命令要等纳管成功后才允许创建。

        许可与启动拆开是 Windows 所有权的关键时序——先写 GO 再纳管时，实际命令
        在 AssignProcessToJobObject 之前就可能已启动（慢启动命令更必然），它和
        它的后代全部落在 Job 外，KILL_ON_JOB_CLOSE 兜底整体旁路。
        """
        owned.gate_path.write_bytes(b"PENDING")  # 许可文件：GO 才放行
        popen_kwargs: dict[str, Any] = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "env": env,
        }
        if cwd is not None:
            popen_kwargs["cwd"] = str(cwd)
        if _IS_WINDOWS:
            popen_kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
        else:
            # 独立会话组长：实际命令及后代继承进程组，组信号一次覆盖
            popen_kwargs["start_new_session"] = True
        proc = subprocess.Popen(
            [sys.executable, "-I", "-S", "-c", _WRAPPER_SOURCE,
             str(owned.log_path), str(owned.status_path), str(owned.gate_path), *cmd],
            **popen_kwargs,
        )
        owned.started(proc)
        if not _IS_WINDOWS:
            # start_new_session 已让 wrapper 成为组长：组号就是它的 pid，
            # 在此刻取一次（进程退出后 getpgid 会失败）
            owned.pgid = proc.pid
        return proc

    @staticmethod
    def _release(owned: _OwnedCommand) -> None:
        """写许可放行实际命令；只能由 run/start_app 在纳管（或 POSIX 无纳管）后调用。

        许可用文件不用管道：管道写端句柄会被子进程继承链纠缠，文件没有这个问题。
        """
        owned.gate_path.write_bytes(b"GO")

    # ── 对外接口 ──

    def run(self, cmd: list[str], *, env: dict[str, str] | None = None, timeout: float = 900, cwd: Path | None = None) -> str:
        """同步执行一条命令并返回其日志文本（失败由调用方按状态判定）。

        Windows 规范顺序：spawn → 立即 poll（未放行时退出只可能是 wrapper 自身
        启动失败）→ 纳管 → 放行。放行后命令成败只以状态文件为事实，wrapper 的
        退出码不作判据——发布完成状态后 wrapper 被 TerminateJobObject 收尾是
        正常路径，按其 returncode 反推会把成功命令误报成失败，还把「进程已退出、
        句柄回收」误报成「runner 不允许嵌套 Job」。
        """
        owned = self._new_command(_safe_name(cmd))
        try:
            child_env = dict(os.environ if env is None else env)
            child_env.setdefault("PYTHONIOENCODING", "utf-8")
            self._spawn_wrapper(owned, cmd, env=child_env, cwd=cwd)
            if _IS_WINDOWS:
                proc = owned.proc
                if proc is None or proc.poll() is not None:
                    # 尚未放行，退出只可能是 wrapper 自身启动失败
                    code = proc.returncode if proc is not None else None
                    raise SmokeCommandError(
                        f"包装进程提前退出（{code}），命令未执行: {owned.name}"
                    )
                self._assign_to_job(owned, proc)
        except Exception:
            self._cleanup(owned, grace_seconds=0.0)
            raise
        self._release(owned)
        status = self._wait_status(owned, timeout=timeout)
        self._cleanup(owned, grace_seconds=0.0)
        if status["kind"] == "error":
            raise SmokeCommandError(
                f"命令包装层失败: {status['error_type']}: {status['message']}"
            )
        if status["returncode"] != 0:
            tail = self._log_tail(owned)
            raise SmokeCommandError(
                f"命令失败({status['returncode']}): {' '.join(cmd)}\n{tail}"
            )
        return self._log_tail(owned, limit=None)

    def start_app(self, cmd: list[str], *, env: dict[str, str], cwd: Path) -> _OwnedCommand:
        """启动长驻应用：不等它退出，由 close_all 统一回收。"""
        owned = self._new_command("app")
        try:
            child_env = dict(env)
            child_env.setdefault("PYTHONIOENCODING", "utf-8")
            self._spawn_wrapper(owned, cmd, env=child_env, cwd=cwd)
            if _IS_WINDOWS:
                proc = owned.proc
                if proc is None or proc.poll() is not None:
                    code = proc.returncode if proc is not None else None
                    raise SmokeCommandError(
                        f"包装进程提前退出（{code}），命令未执行: {owned.name}"
                    )
                self._assign_to_job(owned, proc)
        except Exception:
            self._cleanup(owned, grace_seconds=0.0)
            raise
        self._release(owned)
        return owned

    def close_all(self, timeout: float = 10.0) -> None:
        """回收全部已登记命令；聚合错误但逐条处理（第一个失败不得漏掉其余）。"""
        for owned in self._owned:
            try:
                self._cleanup(owned, grace_seconds=timeout)
            except Exception as error:  # noqa: BLE001 — 聚合清理错误，最后统一抛
                self._close_errors.append(f"{owned.name}: {error}")
        self._owned.clear()
        if self._close_errors:
            raise SmokeCommandError("命令清理失败: " + "; ".join(self._close_errors))

    # ── 等待与清理 ──

    def _wait_status(self, owned: _OwnedCommand, *, timeout: float) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            if owned.status_path.exists():
                return parse_status(owned.status_path.read_bytes())
            # 只在 Windows 上用 poll() 判「包装进程提前退出」：POSIX 的 poll 会**回收**
            # 子进程，一旦回收就无法再向它所在的进程组发信号（组号可能被复用）——
            # POSIX 侧按契约只用状态文件与总期限判断命令是否完成。
            if (
                _IS_WINDOWS
                and owned.proc is not None
                and owned.proc.poll() is not None
                and owned.proc.returncode not in (0, None)
            ):
                # wrapper 在许可前/读许可失败时退出：不会有状态文件
                raise SmokeCommandError(
                    f"包装进程提前退出（{owned.proc.returncode}），命令未执行: {owned.name}"
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SmokeCommandError(f"命令在 {timeout:.0f}s 内未完成: {owned.name}")
            time.sleep(min(0.2, max(remaining, 0.01)))

    def _log_tail(self, owned: _OwnedCommand, *, limit: int | None = 2000) -> str:
        try:
            text = owned.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return "(无日志)"
        return text if limit is None else text[-limit:]

    def _cleanup(self, owned: _OwnedCommand, *, grace_seconds: float) -> int | None:
        """终止并回收一条命令的所属进程；返回 wrapper 退出码（已回收时）。

        顺序（Windows）：已发布完成状态的命令先等 wrapper 自然退出，仅在
        ActiveProcesses 为 0 时才走该捷径；其余情况先 TerminateJobObject 再等
        ActiveProcesses 归零，最后关 Job 句柄并 wait wrapper——确认计数之前不关句柄
        （关了就失去内核兜底的计数依据）。POSIX：组 SIGTERM → 宽限 → 组 SIGKILL
        → wait wrapper。
        """
        if owned._cleaned:
            return owned.proc.returncode if owned.proc is not None else None
        owned._cleaned = True
        if owned.proc is None:
            return None
        try:
            if _IS_WINDOWS and owned._job_handle is not None:
                if owned.status_path.exists():
                    # 已发布完成状态：实际命令已结束，先等 wrapper 自然退出，
                    # 不立即 TerminateJobObject（会把它连同状态一起粗暴收尾）
                    try:
                        owned.proc.wait(timeout=_WRAPPER_EXIT_GRACE)
                        owned.reaped = True
                    except subprocess.TimeoutExpired:
                        pass
                    else:
                        # wrapper 退出只代表它的**直接子进程**结束：命令 spawn 出的
                        # 孙进程可能仍在 Job 内。归零确认过才敢走这条捷径——否则残留
                        # 进程只会在 finally 关句柄时由 KILL_ON_JOB_CLOSE 兜底，清理
                        # 结果不可知却报成功。
                        if _active_processes(owned._job_handle) == 0:
                            return owned.proc.returncode
                _terminate_job(owned._job_handle, grace_seconds=grace_seconds)
            elif not _IS_WINDOWS:
                # 组信号在回收**之前**发：wrapper 自己先退出不代表组里没有后代
                # （命令 detach 出的孙进程仍在组内）。等 wrapper 退出后再发就晚了——
                # 组号可能随回收被复用，那时只能保守放弃，孤儿无人收。
                _terminate_group(owned, grace_seconds=grace_seconds)
            else:
                # 无平台句柄（启动中途失败）：直接按进程终止
                if owned.proc.poll() is None:
                    owned.proc.kill()
            try:
                owned.proc.wait(timeout=grace_seconds + 5)
            except subprocess.TimeoutExpired:
                owned.proc.kill()
                owned.proc.wait(timeout=5)
            owned.reaped = True
        finally:
            if owned._job_handle is not None:
                # finally 里关 Job：终止/查询失败也要触发 KILL_ON_JOB_CLOSE 内核兜底
                _close_handle(owned._job_handle)
                owned._job_handle = None
        return owned.proc.returncode


def _safe_name(cmd: list[str]) -> str:
    """从命令首参推导目录名：只留安全字符，避免长路径/怪字符进目录名。"""
    head = Path(cmd[0]).stem if cmd else "cmd"
    keep = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in head)
    return (keep or "cmd")[:40]


# ── Windows Job 对象（ctypes，显式签名）──

if _IS_WINDOWS:
    _HANDLE = ctypes.c_void_p
    _DWORD = ctypes.c_uint32
    _BOOL = ctypes.c_long
    _LPVOID = ctypes.c_void_p

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    _kernel32.CreateJobObjectW.argtypes = (_LPVOID, ctypes.c_wchar_p)
    _kernel32.CreateJobObjectW.restype = _HANDLE
    _kernel32.SetInformationJobObject.argtypes = (_HANDLE, _DWORD, _LPVOID, _DWORD)
    _kernel32.SetInformationJobObject.restype = _BOOL
    _kernel32.AssignProcessToJobObject.argtypes = (_HANDLE, _HANDLE)
    _kernel32.AssignProcessToJobObject.restype = _BOOL
    _kernel32.TerminateJobObject.argtypes = (_HANDLE, ctypes.c_uint32)
    _kernel32.TerminateJobObject.restype = _BOOL
    _kernel32.QueryInformationJobObject.argtypes = (_HANDLE, _DWORD, _LPVOID, _DWORD, _LPVOID)
    _kernel32.QueryInformationJobObject.restype = _BOOL
    _kernel32.IsProcessInJob.argtypes = (_HANDLE, _HANDLE, ctypes.POINTER(_BOOL))
    _kernel32.IsProcessInJob.restype = _BOOL
    _kernel32.OpenProcess.argtypes = (_DWORD, _BOOL, _DWORD)
    _kernel32.OpenProcess.restype = _HANDLE
    _kernel32.CloseHandle.argtypes = (_HANDLE,)
    _kernel32.CloseHandle.restype = _BOOL

    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9  # JobObjectExtendedLimitInformation
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000

    import ctypes as _ctypes

    class _IO_COUNTERS(_ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", _ctypes.c_ulonglong),
            ("WriteOperationCount", _ctypes.c_ulonglong),
            ("OtherOperationCount", _ctypes.c_ulonglong),
            ("ReadTransferCount", _ctypes.c_ulonglong),
            ("WriteTransferCount", _ctypes.c_ulonglong),
            ("OtherTransferCount", _ctypes.c_ulonglong),
        ]

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(_ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", _ctypes.c_longlong),
            ("PerJobUserTimeLimit", _ctypes.c_longlong),
            ("LimitFlags", _DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", _DWORD),
            ("Affinity", _LPVOID),
            ("PriorityClass", _DWORD),
            ("SchedulingClass", _DWORD),
        ]

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(_ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]


def _last_error() -> int:
    return ctypes.get_last_error() if _IS_WINDOWS else 0


def _create_kill_on_close_job() -> Any:
    """创建未命名、不可继承、KILL_ON_JOB_CLOSE 的 Job；失败抛 SmokeCommandError。"""
    if not _IS_WINDOWS:
        raise SmokeCommandError("Job 对象只在 Windows 上可用")
    job = _kernel32.CreateJobObjectW(None, None)
    if not job:
        raise SmokeCommandError(f"CreateJobObjectW 失败（WinError {_last_error()}）")
    info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    ok = _kernel32.SetInformationJobObject(
        job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
        ctypes.byref(info), ctypes.sizeof(info),
    )
    if not ok:
        error = _last_error()
        _close_handle(job)
        raise SmokeCommandError(f"SetInformationJobObject 失败（WinError {error}）")
    return job


def _assign_process_to_job(job: Any, process_handle: int) -> bool:
    """把（Popen 持有的）进程句柄指进 Job；返回内核原始布尔。"""
    return bool(_kernel32.AssignProcessToJobObject(_HANDLE(job), _HANDLE(process_handle)))


def _process_in_job(job: Any, pid: int) -> bool:
    """按 PID 查询进程是否在指定 Job 内（测试断言真实归属用）。

    句柄取不到（进程已退出/权限不足）返回 False：测试侧把它当作「不在 Job 内」
    的保守结论，不伪造归属证据。只读查询，不改变进程或 Job 状态。
    """
    if not _IS_WINDOWS or not job:
        return False
    handle = _kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        in_job = _BOOL()
        ok = _kernel32.IsProcessInJob(handle, _HANDLE(job), ctypes.byref(in_job))
        return bool(ok and in_job.value)
    finally:
        _close_handle(handle)


def _active_processes(job: Any) -> int | None:
    """Job 内当前活动进程数；查询失败返回 None（调用方保守处理）。"""
    if not _IS_WINDOWS or not job:
        return None
    accounting = _JOBOBJECT_BASIC_ACCOUNTING()
    queried = _kernel32.QueryInformationJobObject(
        job, 1,  # JobObjectBasicAccountingInformation（类号 1，不是 8——8 是 Basic+IO，结构不同）
        ctypes.byref(accounting), ctypes.sizeof(accounting), None,
    )
    return int(accounting.ActiveProcesses) if queried else None


def _terminate_job(job: Any, *, grace_seconds: float) -> None:
    """终止 Job 内全部进程并等 ActiveProcesses 归零；未归零或查询失败即抛错。

    归零确认属于清理契约：句柄随后会在 finally 关闭、KILL_ON_JOB_CLOSE 仍会内核兜底
    终止，但那时已经查不到计数——「可能还有残留」与「确认清干净」不能都报成成功。
    查询失败（None）同样不算确认，必须让调用方看到失败。
    """
    if not _kernel32.TerminateJobObject(job, 1):
        raise SmokeCommandError(f"TerminateJobObject 失败（WinError {_last_error()}）")
    wait_seconds = max(grace_seconds, _JOB_ZERO_WAIT_FLOOR)
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        active = _active_processes(job)
        if active == 0:
            return
        if active is None:
            raise SmokeCommandError("Job 活动进程数查询失败，无法确认归零")
        time.sleep(0.05)
    raise SmokeCommandError(f"Job 内进程在 {wait_seconds:.0f}s 内未归零，清理结果不可知")


if _IS_WINDOWS:
    class _JOBOBJECT_BASIC_ACCOUNTING(_ctypes.Structure):
        _fields_ = [
            ("TotalUserTime", _ctypes.c_longlong),
            ("TotalKernelTime", _ctypes.c_longlong),
            ("ThisPeriodTotalUserTime", _ctypes.c_longlong),
            ("ThisPeriodTotalKernelTime", _ctypes.c_longlong),
            ("TotalPageFaultCount", _DWORD),
            ("TotalProcesses", _DWORD),
            ("ActiveProcesses", _DWORD),
            ("TotalTerminatedProcesses", _DWORD),
        ]


def _close_handle(handle: Any) -> None:
    if handle:
        _kernel32.CloseHandle(_HANDLE(handle))


# ── POSIX 进程组 ──


def _terminate_group(owned: _OwnedCommand, *, grace_seconds: float) -> None:
    """对进程组发 SIGTERM → 宽限 → 最后一次 SIGKILL；之后才由调用方 wait 回收。

    组号取自**启动时**记下的 owned.pgid（进程退出后 getpgid 会失败，且那时若已
    wait 回收，组号可能被复用——向复用的组号发信号就是杀别的进程）。已经回收过
    的命令不再发信号：此时无法区分「组已空」与「组号被复用」，保守报告而非误杀。
    """
    if owned.proc is None or owned.reaped or owned.pgid is None:
        return
    # 运行在 POSIX 才有进程组 API；Windows 类型检查下这些属性不存在，运行时判定
    if not hasattr(os, "killpg"):
        return
    pgid = owned.pgid
    try:
        os.killpg(pgid, signal.SIGTERM)  # type: ignore[attr-defined]
    except ProcessLookupError:
        return
    # 宽限期按墙钟走，不用 proc.poll() 判结束：poll 会**回收**子进程，
    # 一旦回收就失去向该组发信号的安全窗口（组号可能被复用）。
    deadline = time.monotonic() + max(grace_seconds, 0.0)
    while time.monotonic() < deadline:
        time.sleep(0.05)
    try:
        os.killpg(pgid, signal.SIGKILL)  # type: ignore[attr-defined]
    except ProcessLookupError:
        pass
