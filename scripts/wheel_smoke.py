"""wheel 冒烟：构建 → 资源清单断言 → 全新 venv 非 editable 安装 → 真进程启动 + HTTP 探活 + 真实写入。

为什么单独成脚本、只在 CI 的独立 job 跑：它要建临时 venv、装 wheel、起真服务并触发写入，
分钟级且与构建强耦合；放进 pytest 会给「跑测试」引入「先能构建」的隐式前置。

验收要点（每步都对应一种「装完 wheel 才发现」的失败）：
1. 只构建到临时目录并锁定唯一 wheel——复用 dist/ 会累积旧包，装到上一个版本也能通过；
2. wheel 内数据成员与期望集合双向相等（多出即失败：reports/、*.fromweb.json、.tmp/ 混入）；
3. 应用进程用**白名单**环境启动：黑名单会随新配置项失效，继承来的 DB_PATH/PLUGINS
   足以让「默认库落在临时目录」「插件来自 wheel」这类断言失真；
4. 启动、HTTP 断言与真实写入（配置保存 / 用例导出 / 基准 runner）分开断言，
   只发 GET 证明不了写入隔离；
5. 结束校验 site-packages 无新增库/配置/报告，且临时 data/cache 目录确实被使用。
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    # 直跑时 sys.path[0] 是 scripts/：包路径需显式补，否则 scripts.runtime_manifest 导不到
    sys.path.insert(0, str(REPO))

from scripts.runtime_manifest import expected_data_members  # noqa: E402

#: 非数据成员：dist-info 与包内模块（.py 另由「必须落在真实包目录内」约束兜底）
ALLOWED_NON_DATA = (
    "briefdesk-*.dist-info/*",
    "briefdesk/*.py",
)

#: 应用进程只继承这些宿主变量：黑名单会随新配置项失效，白名单把「继承来的外部配置」
#: 一次性挡在门外（DB_PATH / PLUGINS / PLUGIN_PATH / PYTHONPATH / BRIEFDESK_* 都不在其中）。
ENV_ALLOWLIST = (
    "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC",
    "TEMP", "TMP", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "LOCALAPPDATA", "APPDATA",
    "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "COMMONPROGRAMFILES",
    "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "OS", "LANG", "LC_ALL",
)


class SmokeFailure(RuntimeError):
    """冒烟失败：携带可直接定位的说明。"""


def log(message: str) -> None:
    print(f"[wheel-smoke] {message}", flush=True)


def run(cmd: list[str], *, env: dict[str, str] | None = None, timeout: int = 900) -> str:
    """执行外部命令；非零退出即失败（输出附在异常里）。

    子进程与被读侧都钉死 UTF-8。仓库里的入口（benchmark runner/cli、本脚本等）按约定把
    stdout/stderr 重配成 UTF-8 输出中文，而 text=True 缺省按 locale 解码——CI 的
    Windows runner 是 cp1252，UTF-8 续字节（0x8D/0x8F）在 cp1252 里未定义，解码会在
    subprocess 的读取线程里抛 UnicodeDecodeError：异常只被 threading.excepthook 打印、
    不冒泡，run() 照常返回而 stdout 静默变成 None（解析输出的调用点随后 AttributeError）。
    """
    child_env = dict(os.environ if env is None else env)
    child_env.setdefault("PYTHONIOENCODING", "utf-8")
    result = subprocess.run(
        cmd,
        cwd=str(REPO),
        env=child_env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    if result.stdout is None or result.stderr is None:
        raise SmokeFailure(f"子进程输出读取失败（编码不一致？）: {' '.join(cmd)}")
    if result.returncode != 0:
        raise SmokeFailure(f"命令失败({result.returncode}): {' '.join(cmd)}\n{result.stdout[-2000:]}\n{result.stderr[-2000:]}")
    return result.stdout


def free_port() -> int:
    """取一个空闲端口：固定 3000 在 CI/开发机上可能被占用。"""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def http(
    url: str,
    *, method: str = "GET", payload: dict | None = None, origin: str | None = None, timeout: float = 10.0
) -> tuple[int, str]:
    """发一个请求，返回 (状态码, 文本)；HTTP 错误码不抛异常（断言用）。"""
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, method=method)
    if payload is not None:
        request.add_header("Content-Type", "application/json")
    if origin is not None:
        request.add_header("Origin", origin)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError):
        # 服务还没起来（ConnectionRefused）——就绪轮询靠这个分支继续重试
        return 0, ""


def app_env(*, data_dir: Path, cache_dir: Path, settings_file: Path, port: int) -> dict[str, str]:
    """被测应用进程的环境：白名单宿主变量 + 测试专用注入值。"""
    env = {key: value for key, value in os.environ.items() if key in ENV_ALLOWLIST}
    env.update(
        {
            "BRIEFDESK_DATA_DIR": str(data_dir),
            "BRIEFDESK_CACHE_DIR": str(cache_dir),
            "BRIEFDESK_SETTINGS_FILE": str(settings_file),
            "BRIEFDESK_KEYRING": "0",  # 不触碰当前用户的系统凭据
            "SERVER_PORT": str(port),
            "LOG_LEVEL": "WARNING",
            "AI_API_KEY": "smoke-not-a-real-key",
            "AI_API_BASE": "http://127.0.0.1:9/v1",  # 不可达：任何真实调用都立刻失败
            "EMBED_API_BASE": "",  # 显式禁用嵌入，避免预热访问外部服务
            "PLUGINS": '["benchmark"]',  # benchmark 可选：不显式启用就跑不了基准步骤
            # 应用入口没像 benchmark 入口那样重配 stdout，CI 的 cp1252 控制台上打中文
            # 日志会在编码处炸；这里钉死 UTF-8，与 wait_ready 读 app.log 的口径一致
            "PYTHONIOENCODING": "utf-8",
        }
    )
    return env


def build_wheel(out_dir: Path) -> Path:
    log(f"构建 wheel → {out_dir}")
    run([sys.executable, "-m", "build", "--wheel", "--outdir", str(out_dir)], timeout=900)
    wheels = sorted(out_dir.glob("*.whl"))
    if len(wheels) != 1:
        raise SmokeFailure(f"期望恰好一个 wheel，实际 {len(wheels)} 个: {[w.name for w in wheels]}")
    return wheels[0]


def expected_manifest() -> list[str]:
    """期望集合：scripts/runtime_manifest.py（与 sdist 检查共用同一份登记）。"""
    return expected_data_members()


def assert_manifest(wheel: Path) -> None:
    import fnmatch

    names = [name.replace("\\", "/") for name in zipfile.ZipFile(wheel).namelist()]
    data = [name for name in names if not any(fnmatch.fnmatch(name, p) for p in ALLOWED_NON_DATA)]
    expected = expected_manifest()
    extra = sorted(set(data) - set(expected))
    missing = sorted(set(expected) - set(data))
    if extra or missing:
        raise SmokeFailure(f"wheel 资源与期望集合不一致：多出 {extra}，缺失 {missing}")
    for member in ("METADATA", "RECORD", "WHEEL"):
        if not any(name.endswith(f".dist-info/{member}") for name in names):
            raise SmokeFailure(f"dist-info 缺 {member}")
    dirs_with_init = {name.rsplit("/", 1)[0] for name in names if name.endswith("/__init__.py")}
    stray = [
        name for name in names if name.endswith(".py") and name.rsplit("/", 1)[0] not in dirs_with_init
    ]
    if stray:
        raise SmokeFailure(f".py 出现在非包目录: {stray}")
    log(f"资源清单通过：{len(data)} 个数据成员")


def make_venv(tmp: Path) -> Path:
    """系统临时目录里的全新 venv（不继承仓库里的任何 editable 安装）。"""
    venv = tmp / "venv"
    run([sys.executable, "-m", "venv", str(venv)], timeout=600)
    return venv


def venv_python(venv: Path) -> Path:
    candidate = venv / ("Scripts" if os.name == "nt" else "bin") / ("python.exe" if os.name == "nt" else "python")
    if not candidate.exists():
        raise SmokeFailure(f"venv 内找不到解释器: {candidate}")
    return candidate


def console_script(venv: Path) -> Path:
    """console script 绝对路径：验证 entry point 与 uvicorn 启动路径，而不是 python -m。"""
    script = venv / ("Scripts" if os.name == "nt" else "bin") / ("briefdesk.exe" if os.name == "nt" else "briefdesk")
    if not script.exists():
        raise SmokeFailure(f"venv 内找不到 console script: {script}")
    return script


def install_wheel(venv: Path, wheel: Path) -> None:
    python = venv_python(venv)
    log("安装 wheel（非 editable）")
    run([str(python), "-m", "pip", "install", "--quiet", str(wheel)], timeout=900)


def wait_ready(base: str, log_path: Path, timeout: float = 60.0) -> None:
    deadline = time.time() + timeout
    last_error = ""
    while time.time() < deadline:
        if http(f"{base}/", timeout=3)[0] == 200:
            return
        time.sleep(0.5)
    tail = log_path.read_text(encoding="utf-8", errors="replace")[-2000:] if log_path.exists() else "(无日志)"
    raise SmokeFailure(f"应用未在 {timeout:.0f}s 内就绪（{last_error}）\n日志尾部:\n{tail}")


def assert_http_surface(base: str, port: int) -> None:
    status, body = http(f"{base}/")
    if status != 200 or "<!DOCTYPE" not in body.upper():
        raise SmokeFailure(f"首页异常: {status}")
    for path in ("/app.js", "/style.css", "/icons/bell.svg"):
        status, _ = http(base + path)
        if status != 200:
            raise SmokeFailure(f"静态资源 {path} 返回 {status}（wheel 里缺资源？）")
    status, _ = http(f"{base}/plugin-assets/calendar/ui.js")
    if status != 200:
        raise SmokeFailure(f"插件资源返回 {status}（插件前端未随包分发？）")
    status, body = http(f"{base}/settings")
    if status != 200 or "<!DOCTYPE" not in body.upper():
        raise SmokeFailure(f"SPA 回退异常: {status}")
    status, _ = http(f"{base}/no-such-file.js")
    if status != 404:
        raise SmokeFailure(f"未知带扩展名资源应 404，实际 {status}")
    log("HTTP 断言通过：首页/资源/插件资源/SPA 回退/未知 404")


def assert_writes(base: str, port: int, python: Path, env: dict[str, str], data_dir: Path, cache_dir: Path, settings_file: Path, tmp: Path) -> None:
    """真实写入：配置保存（HTTP）/ 用例导出（辅助进程）/ 基准 runner 与默认目录断言。"""
    origin = f"http://127.0.0.1:{port}"
    status, body = http(f"{base}/api/settings/env", method="PUT", payload={"items": {"LOG_LEVEL": "WARNING"}}, origin=origin)
    if status != 200:
        raise SmokeFailure(f"保存配置失败: {status} {body[:300]}")
    if not settings_file.exists() or "LOG_LEVEL=WARNING" not in settings_file.read_text(encoding="utf-8"):
        raise SmokeFailure(f"配置未写入 {settings_file}（写入隔离失效？）")
    log("写入步骤 1/5 通过：配置保存落在 BRIEFDESK_SETTINGS_FILE")

    export_code = (
        "import asyncio\n"
        "from briefdesk.plugins.benchmark import store\n"
        "case = {\"id\": \"smoke-1\", \"note\": \"smoke\", \"message\": {\"msg_id\": \"m1\", \"content\": \"smoke\"}, \"old_title\": \"t\", \"key_info\": \"\", \"expected\": {\"title\": \"smoke\"}}\n"
        "print(asyncio.run(store.export_fromweb(\"title\", [case])))\n"
    )
    out = run([str(python), "-c", export_code], env=env, timeout=300)
    exported = data_dir / "benchmark" / "cases" / "title.fromweb.json"
    if not exported.exists() or str(exported) not in out:
        raise SmokeFailure(f"用例导出未落在用户数据目录: {out.strip()}")
    log("写入步骤 2/5 通过：Web 导出写用户用例目录")

    cases_dir = run([str(python), "-c", "from briefdesk.plugins.benchmark import store; print(store.PACKAGE_CASES_DIR)"], env=env, timeout=120).strip()
    run_dir = tmp / "runner-run"
    run_dir.mkdir(parents=True, exist_ok=True)
    run(
        [
            str(python), "-m", "briefdesk.plugins.benchmark.runner",
            "--run-dir", str(run_dir),
            "--db", str(tmp / "runner-bench.sqlite"),
            "--cases-dir", cases_dir,
            "--source", "file",
            "--ai-provider", "stub",
            "--features", "dedup",
        ],
        env=env,
        timeout=600,
    )
    if not (run_dir / "report.json").exists():
        raise SmokeFailure(f"基准 runner 未产出报告: {sorted(p.name for p in run_dir.iterdir())}")
    log("写入步骤 3/5 通过：基准 runner 产出报告")

    defaults = run(
        [
            str(python), "-c",
            "from briefdesk.plugins.benchmark import cli, supervisor; print(cli._out_dir_arg(None)); print(supervisor._runs_root())",
        ],
        env=env,
        timeout=120,
    ).splitlines()
    if Path(defaults[0]) != data_dir / "benchmark" / "reports":
        raise SmokeFailure(f"CLI 默认报告目录异常: {defaults[0]}")
    if Path(defaults[1]) != cache_dir / "benchmark" / "runs":
        raise SmokeFailure(f"supervisor 运行目录异常: {defaults[1]}")
    log("写入步骤 4/5 通过：CLI 报告目录与 supervisor 运行目录落用户目录")

    # 5) Web 基准运行（**故意失败**）：证明 supervisor 运行目录落缓存目录，且
    #    运行中失败也能收尾——清理契约里最容易被忽略的就是异常路径。
    status, body = http(
        f"{base}/api/benchmark/run", method="POST", payload={"features": ["title"]}, origin=origin
    )
    if status != 200:
        raise SmokeFailure(f"启动基准失败: {status} {body[:300]}")
    deadline = time.time() + 180
    state: dict = {}
    while time.time() < deadline:
        status, body = http(f"{base}/api/benchmark/run", timeout=5)
        state = json.loads(body) if status == 200 else {}
        if not state.get("running"):
            break
        time.sleep(1)
    if state.get("running"):
        raise SmokeFailure("基准运行未在 180s 内收尾（清理契约失效？）")
    runs_root = cache_dir / "benchmark" / "runs"
    run_dirs = sorted(runs_root.glob("*")) if runs_root.exists() else []
    if not run_dirs:
        raise SmokeFailure(f"Web 基准未在缓存目录创建运行目录: {runs_root}")
    log(f"写入步骤 5/5 通过：基准运行目录 {run_dirs[-1].name}（结局: {state.get('error') or state.get('summary') or '已收尾'}）")


def assert_no_leaks(
    venv: Path, data_dir: Path, cache_dir: Path, settings_file: Path, real_runs_root: Path
) -> None:
    """site-packages 不得新增库/配置/报告；真实用户缓存目录不得被触碰。"""
    site_packages = next(venv.glob("Lib/site-packages"), None) or next(venv.glob("lib/python*/site-packages"), None)
    if site_packages is None:
        raise SmokeFailure("找不到 venv 的 site-packages")
    leaks = [
        str(path.relative_to(site_packages))
        for pattern in ("**/*.sqlite", "**/settings.env", "**/benchmark/runs/**")
        for path in site_packages.glob(pattern)
    ]
    if leaks:
        raise SmokeFailure(f"site-packages 出现运行期产物: {leaks[:10]}")
    if not (data_dir / "data" / "briefdesk.sqlite").exists():
        raise SmokeFailure(f"默认库未落在 {data_dir}/data（DB_PATH 被继承？）")
    if not settings_file.exists():
        raise SmokeFailure("临时 settings 文件未被写入")
    if not cache_dir.exists():
        raise SmokeFailure("临时缓存目录未被使用（基准运行目录没落这里？）")
    if real_runs_root.exists():
        raise SmokeFailure(f"真实用户缓存目录被写入: {real_runs_root}（BRIEFDESK_CACHE_DIR 未生效？）")
    log("隔离断言通过：site-packages 无产物、真实用户目录未被触碰")


def main() -> int:
    # 输出统一 UTF-8：Windows runner 的控制台默认是 ANSI 代码页（cp1252），
    # 直接 print 中文会抛 UnicodeEncodeError 让冒烟在第一步就失败；
    # errors=replace 保证即使流不可重配也不会因日志字符崩掉验收。
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass  # 非可重配置流（测试捕获/已关闭）保持原样
    tmp = Path(tempfile.mkdtemp(prefix="briefdesk-smoke-"))
    data_dir = tmp / "data"
    cache_dir = tmp / "cache"
    settings_file = tmp / "settings.env"
    app_log = tmp / "app.log"
    process: subprocess.Popen | None = None
    try:
        wheel = build_wheel(tmp)
        assert_manifest(wheel)
        venv = make_venv(tmp)
        install_wheel(venv, wheel)
        python = venv_python(venv)
        port = free_port()
        base = f"http://127.0.0.1:{port}"
        env = app_env(data_dir=data_dir, cache_dir=cache_dir, settings_file=settings_file, port=port)
        # 真实用户缓存目录：不注入 BRIEFDESK_* 时应用会去那里——冒烟全程必须不碰它
        probe_env = {key: value for key, value in os.environ.items() if key in ENV_ALLOWLIST}
        real_runs_root = Path(
            run(
                [
                    str(python), "-c",
                    "from briefdesk.plugins.benchmark import supervisor; print(supervisor._runs_root())",
                ],
                env=probe_env,
                timeout=120,
            ).strip()
        )
        log(f"真实用户缓存目录（应保持不存在）: {real_runs_root}")
        workdir = tmp / "cwd"
        workdir.mkdir(parents=True, exist_ok=True)
        log(f"启动 console script（cwd={workdir}，端口 {port}）")
        with app_log.open("wb") as log_file:
            process = subprocess.Popen(
                [str(console_script(venv))],
                cwd=str(workdir),
                env=env,
                stdout=log_file,
                stderr=subprocess.STDOUT,
            )
        wait_ready(base, app_log)
        assert_http_surface(base, port)
        assert_writes(base, port, python, env, data_dir, cache_dir, settings_file, tmp)
        assert_no_leaks(venv, data_dir, cache_dir, settings_file, real_runs_root)
        log("冒烟通过")
        return 0
    except SmokeFailure as error:
        print(f"[wheel-smoke] 失败: {error}", file=sys.stderr, flush=True)
        print(f"[wheel-smoke] 现场保留在 {tmp}（含 app.log）", file=sys.stderr, flush=True)
        return 1
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
        if process is None or process.poll() is not None:
            # 只有确认应用进程已退出才清理目录（Windows 上被占用的文件删不掉）
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
