"""基准运行监管（父进程侧）：启动、状态、取消、回收与轮转。

父子只经文件交换：本模块在 spawn **之前**建好 run_dir 与 meta.json（这样被杀/崩溃
的运行目录也带 meta，不会被 gc 当成残目录删掉），子进程只往里写产物；父进程回收
子进程后补写终态记录，并**在回收时**触发一次轮转——轮转只挂在插件 activate 上的话，
长驻进程里 run_dir 会无限增长。

结果分类统一引用三态判据（见 §3.2 的契约）：

- completed：最后一行是 done 终态行 + 两份报告齐全 → 唯一可被 /report 选中的形态；
- failed-recorded：最后一行是 error 终态行（报告可缺）；
- aborted：没有任何终态行（被杀/超时/崩溃），原因由这里补记。

两种运行模式（BENCHMARK_RUN_MODE）：

- inproc：与生产同进程跑，隔离交给 bench_environment（重定向 + 暂停 + 排空），
  行为与改造前逐位一致；不产出 run_dir，结果只驻内存（双轨期 /report 因此返回
  最近一次子进程运行的结果或 404——这是契约里写明的过渡语义）；
- subprocess：spawn runner 子进程，父进程只负责生命周期与读盘。

取消/超时/宽限 kill 一律只经本进程持有的句柄执行，不做探活、不用 os.kill(pid, 0)。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

from briefdesk import announcements, pipeline
from briefdesk.config import config

logger = logging.getLogger(__name__)

RUN_ROOT = Path(__file__).resolve().parent / ".tmp" / "runs"
CASES_SRC = Path(__file__).resolve().parent / "cases"

INPROC = "inproc"
SUBPROCESS = "subprocess"

# 子进程模式专用的公告码：**不能**复用 benchmark_running——前端是按这个码把列表区
# 整块替换成占位的，子进程模式下列表区应当照常可用。
ANNOUNCE_CODE = "benchmark_paused"
ANNOUNCE_TEXT = (
    "基准运行中：消息处理已暂停，结束后如未开启周期同步，请点一次同步补齐"
)

_TERMINATE_WAIT = 5.0  # terminate 后等待上限，超时升级为 kill
_GRACE_SECONDS = 10.0  # 终态行落盘后的宽限：进程仍不退就 kill
_POLL_SECONDS = 1.0
_CREATE_NO_WINDOW = 0x08000000


@dataclass
class _Run:
    run_id: str
    run_dir: Path
    mode: str
    features: list[str]
    started_at: str
    t0: float
    progress_every: int = 1
    proc: Any | None = None
    task: asyncio.Task[None] | None = None
    monitor: asyncio.Task[None] | None = None
    fh: IO[bytes] | None = None
    error: str | None = None
    aborted_reason: str | None = None
    exit_code: int | None = None
    payload: dict[str, Any] | None = None  # inproc 模式的内存结果
    html: str = ""
    pause: bool = True
    finished: bool = False
    terminal: str | None = None  # completed | failed-recorded | aborted
    meta: dict[str, Any] = field(default_factory=dict)


_current: _Run | None = None
_last: dict[str, Any] | None = None


# ── 对外状态 ──


def is_running() -> bool:
    return _current is not None and not _current.finished


def current_run_id() -> str | None:
    return _current.run_id if is_running() and _current else None


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _tail_line(path: Path) -> dict[str, Any] | None:
    """progress.jsonl 的最后一条**完整**行（正在写的那半行忽略）。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    return None


def _progress(path: Path) -> dict[str, Any] | None:
    """当前功能的进度：取最后一条 progress 行；没有就返回 None。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if item.get("type") == "progress":
            return {
                "feature": item.get("feature"),
                "done": item.get("done"),
                "total": item.get("total"),
                "failed": item.get("failed"),
            }
    return None


def _classify(run_dir: Path) -> tuple[str, dict[str, Any] | None]:
    """按三态判据判定一个 run_dir 的形态；返回 (状态, 终态行)。"""
    last = _tail_line(run_dir / "progress.jsonl")
    if last is None:
        return "aborted", None
    if last.get("type") == "done":
        if (run_dir / "report.json").exists() and (run_dir / "report.html").exists():
            return "completed", last
        return "failed-recorded", last
    if last.get("type") == "error":
        return "failed-recorded", last
    return "aborted", None


def _reports(run_dir: Path) -> tuple[dict[str, Any] | None, str]:
    payload = _read_json(run_dir / "report.json")
    try:
        html = (run_dir / "report.html").read_text(encoding="utf-8")
    except OSError:
        html = ""
    return payload, html


def latest_report_dir() -> Path | None:
    """最近一次 **completed** 的 run_dir（/report 只认这一种）。"""
    newest: tuple[str, Path] | None = None
    for child in _run_dirs():
        state, _ = _classify(child)
        if state != "completed":
            continue
        meta = _read_json(child / "meta.json") or {}
        started = str(meta.get("started_at") or "")
        if newest is None or started > newest[0]:
            newest = (started, child)
    return newest[1] if newest else None


def _run_dirs() -> list[Path]:
    if not RUN_ROOT.exists():
        return []
    return [p for p in RUN_ROOT.iterdir() if p.is_dir()]

# ── 工具 ──


def _settings() -> Any:
    from briefdesk.plugins.benchmark.config import BenchmarkSettings

    return BenchmarkSettings()


def _new_run_id() -> str:
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"


def _line_count(path: Path) -> int:
    try:
        with path.open("r", encoding="utf-8") as fh:
            return sum(1 for line in fh if line.strip())
    except OSError:
        return 0


def _child_env() -> dict[str, str]:
    """把父进程**当前生效**的非密钥 AI 配置下传给子进程。

    设置页的非密钥改动是「暂存、重启后生效」，而子进程会重新读 .env 与暂存文件，
    拿到的是尚未生效的值——两条路径可能不是同一个模型，报告里的 model 字段也会与
    生产实际不符。环境变量优先级高于暂存文件，故在这里显式下传。密钥不下传，
    子进程仍走自己的密钥来源。
    """
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["AI_MODEL"] = str(config.ai_model)
    env["AI_API_BASE"] = str(config.ai_api_base)
    env["AI_MAX_CONCURRENCY"] = str(config.ai_max_concurrency)
    env["AI_DISABLE_THINKING"] = "true" if config.ai_disable_thinking else "false"
    return env


def _remove_dir(path: Path) -> None:
    """先改名成墓碑目录再删：Windows 上直接 rmtree 可能因文件被占用留下半截目录，
    改名之后即便删除失败也不会再被当成一个「运行目录」参与判定。"""
    tomb = path.with_name(path.name + ".deleting")
    try:
        path.rename(tomb)
    except OSError:
        tomb = path
    shutil.rmtree(tomb, ignore_errors=True)


async def _announce() -> None:
    try:
        await announcements.announce(ANNOUNCE_CODE, "warning", ANNOUNCE_TEXT)
    except Exception:  # 公告失败不阻断运行
        logger.debug("基准公告发布失败", exc_info=True)


async def _revoke() -> None:
    try:
        await announcements.revoke(ANNOUNCE_CODE)
    except Exception:
        logger.debug("基准公告撤销失败", exc_info=True)


async def _wait_proc(proc: Any, timeout: float) -> int | None:
    if isinstance(proc, subprocess.Popen):
        try:
            return await asyncio.wait_for(asyncio.to_thread(proc.wait), timeout=timeout)
        except TimeoutError:
            return proc.returncode
    try:
        return await asyncio.wait_for(proc.wait(), timeout=timeout)
    except TimeoutError:
        return proc.returncode


async def _terminate(proc: Any) -> None:
    """terminate → 等 5s → kill。不做优雅退出假设（Windows 的 terminate 就是硬杀）。"""
    if getattr(proc, "returncode", None) is not None:
        return
    try:
        proc.terminate()
    except ProcessLookupError:
        return
    await _wait_proc(proc, _TERMINATE_WAIT)
    if getattr(proc, "returncode", None) is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await _wait_proc(proc, _TERMINATE_WAIT)


# ── 启动 ──


async def start(features: list[str]) -> dict[str, Any]:
    """启动一次基准运行。

    判定→登记之间**没有 await**，因此天然原子：换成 spawn 之后两者之间会隔着一次
    异步调用，两个并发 POST 会各起一个子进程，而先结束的那个还会复位暂停标志与公告。
    """
    global _current
    if is_running():
        raise RuntimeError("基准正在运行中")
    settings = _settings()
    mode = settings.run_mode if settings.run_mode in (INPROC, SUBPROCESS) else INPROC
    run_id = _new_run_id()
    started_at = time.strftime("%Y-%m-%d %H:%M:%S")
    stamp = started_at.replace("-", "").replace(":", "").replace(" ", "-")
    run_dir = RUN_ROOT / f"{stamp}-{run_id[:8]}"
    # 只有子进程模式才落下 run_dir：inproc 的结果只驻内存，凭空建一个目录只会
    # 被轮转与清理当成一次「aborted 运行」，污染保留策略与 /report 的候选集
    if mode == SUBPROCESS:
        run_dir.mkdir(parents=True, exist_ok=True)
    run = _Run(
        run_id=run_id,
        run_dir=run_dir,
        mode=mode,
        features=list(features),
        started_at=started_at,
        t0=time.monotonic(),
    )
    run.pause = bool(settings.pause_pipeline)
    run.meta = {
        "run_id": run_id,
        "features": list(features),
        "started_at": started_at,
        "pid": os.getpid(),
        "db_path": "",
        "source": "fromweb",
        "mode": mode,
    }
    # meta 必须先落盘：被杀/崩溃的运行也要能被识别成「历史结果」而不是残目录
    if mode == SUBPROCESS:
        _write_json(run_dir / "meta.json", run.meta)
    _current = run
    try:
        if mode == SUBPROCESS:
            if run.pause:
                pipeline.set_processing_paused(True)
            await _announce()
            await _spawn(run)
        else:
            run.task = asyncio.create_task(_run_inproc(run))
        run.monitor = asyncio.create_task(_monitor(run, settings))
    except Exception as e:
        run.error = f"{type(e).__name__}: {e}"
        run.aborted_reason = "启动失败"
        await _finish(run)
        raise
    return {"started": True, "features": list(features), "run_id": run_id}


async def _spawn(run: _Run) -> None:
    """快照用例 → spawn runner → 关闭父进程自己的日志句柄。"""
    cases_dir = run.run_dir / "cases"
    cases_dir.mkdir(parents=True, exist_ok=True)
    shutil.copytree(CASES_SRC, cases_dir, dirs_exist_ok=True)
    scratch = run.run_dir / "bench.sqlite"
    run.meta["db_path"] = str(scratch)
    _write_json(run.run_dir / "meta.json", run.meta)
    argv = [
        sys.executable,
        "-m",
        "briefdesk.plugins.benchmark.runner",
        "--run-dir",
        str(run.run_dir),
        "--db",
        str(scratch),
        "--run-id",
        run.run_id,
        "--cases-dir",
        str(cases_dir),
        "--source",
        "fromweb",
        "--features",
        *run.features,
        "--progress-every",
        str(run.progress_every),
    ]
    run.fh = (run.run_dir / "run.log").open("ab")
    creationflags = _CREATE_NO_WINDOW if sys.platform == "win32" else 0
    try:
        run.proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=run.fh,
            stderr=run.fh,
            creationflags=creationflags,
            env=_child_env(),
        )
    except NotImplementedError:
        # Windows 的 Selector 循环（uvicorn --workers/--reload 形态）没有子进程 API
        run.proc = await asyncio.to_thread(
            subprocess.Popen,
            argv,
            stdout=run.fh,
            stderr=run.fh,
            creationflags=creationflags,
            env=_child_env(),
        )
    # 父进程不再需要自己的句柄：留着会让 Windows 上的轮转删不掉这个文件
    run.fh.close()
    run.fh = None

async def _run_inproc(run: _Run) -> None:
    """进程内运行：环境（暂停/排空/重定向/AI 端口）由 bench_environment 自理，
    行为与改造前逐位一致；不产出 run_dir。"""
    from briefdesk.plugins.benchmark import engine as bench_engine
    from briefdesk.plugins.benchmark.html_report import build_html_report

    try:
        cases_by_feature: dict[str, list[Any]] = {}
        for feature in run.features:
            cases_by_feature[feature] = await bench_engine.load_web_cases(feature)
        payload, _evals = await bench_engine.run_benchmark_cases(
            cases_by_feature, run_id=run.run_id
        )
        run.payload = payload
        run.html = build_html_report(payload)
        run.terminal = "completed"
    except asyncio.CancelledError:
        run.aborted_reason = "取消"
        raise
    except Exception as e:
        logger.exception("基准运行失败")
        run.error = f"{type(e).__name__}: {e}"
        run.terminal = "failed-recorded"


async def _monitor(run: _Run, settings: Any) -> None:
    """三条出口：终态行落盘后的宽限 kill、无进展看门狗、总时长兜底。

    看门狗只用 progress.jsonl 的**行数增长**判进展（不探活、也不拿最后一行判存活），
    阈值按 --progress-every 线性放大——进度节流后相邻两行之间最坏隔着 N 条用例。
    """
    stall = max(1, int(settings.run_stall_seconds)) * max(1, run.progress_every)
    timeout = int(settings.run_timeout_seconds)
    last_lines = 0
    last_change = time.monotonic()
    grace_from: float | None = None
    try:
        while not run.finished:
            await asyncio.sleep(_POLL_SECONDS)
            now = time.monotonic()
            if run.mode == SUBPROCESS:
                proc = run.proc
                if proc is None or getattr(proc, "returncode", None) is not None:
                    break
                lines = _line_count(run.run_dir / "progress.jsonl")
                if lines != last_lines:
                    last_lines, last_change = lines, now
                state, _ = _classify(run.run_dir)
                if state in ("completed", "failed-recorded"):
                    if grace_from is None:
                        grace_from = now
                    elif now - grace_from >= _GRACE_SECONDS:
                        run.aborted_reason = "终态行已落盘但进程未退出（宽限超时）"
                        await _terminate(proc)
                        break
                elif now - last_change >= stall:
                    run.aborted_reason = f"无进展超过 {stall}s"
                    await _terminate(proc)
                    break
                if timeout and now - run.t0 >= timeout:
                    run.aborted_reason = f"总时长超过 {timeout}s"
                    await _terminate(proc)
                    break
            else:
                task = run.task
                if task is None or task.done():
                    break
                if timeout and now - run.t0 >= timeout:
                    run.aborted_reason = f"总时长超过 {timeout}s"
                    task.cancel()
                    break
    except asyncio.CancelledError:
        # 监控自身被取消（关闭期）：仍要把运行收尾，否则暂停标志与公告留在原地
        await asyncio.shield(_finish(run))
        raise
    await _finish(run)


async def _finish(run: _Run) -> None:
    """回收：判定三态、补写 meta 终态记录、复位父进程侧状态、触发一次轮转。"""
    global _current, _last
    if run.finished:
        return
    run.finished = True
    aborted = run.aborted_reason is not None
    payload: dict[str, Any] | None = None
    if run.mode == SUBPROCESS:
        run.exit_code = getattr(run.proc, "returncode", None)
        state, terminal = _classify(run.run_dir)
        run.terminal = "aborted" if aborted else state
        if terminal is not None and terminal.get("type") == "error":
            run.error = str(terminal.get("message") or run.error or "运行失败")
        if run.pause:
            pipeline.set_processing_paused(False)
        await _revoke()
    elif run.terminal is None:
        run.terminal = "aborted" if aborted else "failed-recorded"
    if run.terminal == "completed":
        payload, _html = _reports(run.run_dir)
        if payload is None:
            payload = run.payload
    if run.mode == SUBPROCESS:
        run.meta["terminal"] = {
            "type": "done" if run.terminal == "completed" else "error",
            "state": run.terminal,
            "exit_code": run.exit_code,
            "reason": run.aborted_reason,
            "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        _write_json(run.run_dir / "meta.json", run.meta)
    source = payload if payload is not None else (run.payload or {})
    summary = {
        f: (data or {}).get("summary", {})
        for f, data in source.get("features", {}).items()
    }
    _last = {
        "run_id": run.run_id,
        "summary": summary or None,
        "error": run.error,
        "started_at": run.started_at,
        "elapsed_sec": source.get("elapsed_sec"),
        "state": run.terminal,
    }
    if _current is run:
        _current = None
    if run.mode == SUBPROCESS:
        await _rotate()

def _last_from_disk() -> dict[str, Any] | None:
    """父进程重启后仍要能显示「最近一次运行」——内存里已经没有它了，从盘上捞。"""
    run_dir = latest_report_dir()
    if run_dir is None:
        return None
    meta = _read_json(run_dir / "meta.json") or {}
    payload, _html = _reports(run_dir)
    features = (payload or {}).get("features", {})
    summary = {f: (d or {}).get("summary", {}) for f, d in features.items()}
    return {
        "run_id": meta.get("run_id"),
        "summary": summary or None,
        "error": None,
        "started_at": meta.get("started_at"),
        "elapsed_sec": (payload or {}).get("elapsed_sec"),
    }


def state() -> dict[str, Any]:
    """GET /api/benchmark/run 的载荷：形状与改造前一致，只多一个 progress。"""
    out: dict[str, Any] = {"running": False}
    run = _current
    if run is not None and not run.finished:
        out["running"] = True
        out["run_id"] = run.run_id
        out["started_at"] = run.started_at
        out["elapsed_sec"] = round(time.monotonic() - run.t0, 3)
        if run.mode == SUBPROCESS:
            progress = _progress(run.run_dir / "progress.jsonl")
            if progress is not None:
                out["progress"] = progress
    last = _last if _last is not None else _last_from_disk()
    if last:
        # 运行中时 run_id/started_at/elapsed 以当前运行为准，摘要仍显示上一次的结果
        for key in ("run_id", "started_at", "elapsed_sec"):
            if out.get(key) is None and last.get(key) is not None:
                out[key] = last[key]
        if last.get("summary"):
            out["summary"] = last["summary"]
        if last.get("error"):
            out["error"] = last["error"]
    return out


def report_payload() -> dict[str, Any] | None:
    run_dir = latest_report_dir()
    if run_dir is None:
        return None
    payload, _html = _reports(run_dir)
    return payload


def report_html() -> str | None:
    run_dir = latest_report_dir()
    if run_dir is None:
        return None
    _payload, html = _reports(run_dir)
    return html or None


async def cancel() -> dict[str, Any]:
    """DELETE /api/benchmark/run：幂等——没在跑就如实说没在跑。"""
    run = _current
    if run is None or run.finished:
        return {"cancelled": False, "reason": "not_running"}
    run.aborted_reason = "用户取消"
    if run.mode == SUBPROCESS and run.proc is not None:
        await _terminate(run.proc)
    elif run.task is not None:
        run.task.cancel()
    return {"cancelled": True, "run_id": run.run_id}


async def stop_all() -> None:
    """teardown 调用：取消当前运行并等它收尾。

    两种模式都要管：双轨期回退到 inproc 时若只取消子进程，进程内任务会在
    close_db 之后把单例还原成未关闭的生产连接，解释器退出时 join 挂死。
    """
    run = _current
    if run is None or run.finished:
        return
    await cancel()
    deadline = time.monotonic() + _TERMINATE_WAIT
    while not run.finished and time.monotonic() < deadline:
        await asyncio.sleep(0.1)
    if not run.finished:
        run.aborted_reason = run.aborted_reason or "关闭期强制收尾"
        await _finish(run)


async def _rotate() -> None:
    """保留最近 keep_runs 个运行目录；最新一个 completed 永不删除（/report 依赖它）。"""
    settings = _settings()
    keep = max(1, int(settings.keep_runs))
    dirs = sorted(
        _run_dirs(),
        key=lambda p: str((_read_json(p / "meta.json") or {}).get("started_at") or ""),
    )
    if len(dirs) <= keep:
        return
    protected = latest_report_dir()
    for victim in dirs[: len(dirs) - keep]:
        if victim != protected:
            _remove_dir(victim)


async def gc_orphans() -> None:
    """启动兜底：清掉没有 meta.json 的残目录，并做一次轮转。

    **best-effort**：它抛异常会让插件管理器把整个 benchmark 插件标记为 failed
    而不可用，一次清理失败（如 Windows 上文件被孤儿进程占用）不该禁用整个插件。
    """
    try:
        for child in _run_dirs():
            if not (child / "meta.json").exists():
                _remove_dir(child)
        await _rotate()
    except Exception:
        logger.warning("基准运行目录清理失败（忽略）", exc_info=True)
