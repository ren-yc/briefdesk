"""wheel 冒烟：构建 → 资源清单断言 → 全新 venv 非 editable 安装 → 真进程启动 + HTTP 探活 + 真实写入。

为什么单独成脚本、只在 CI 的独立 job 跑：它要建临时 venv、装 wheel、起真服务并触发写入，
分钟级且与构建强耦合；放进 pytest 会给「跑测试」引入「先能构建」的隐式前置。

两个索引安装模式（--install-from testpypi / testpypi-sdist）跳过构建、直接验发布物：前者装索引上的
wheel，后者用 --no-binary 强制走 sdist 并在本地构建——纯 Python 包 pip 默认只选 wheel，不强制就永远
测不到源码分发那条路（sdist 少带运行时资源、从 sdist 重建的 wheel 与直接构建的不一致，都只在它上面暴露）。

验收要点（每步都对应一种「装完 wheel 才发现」的失败）：
1. 只构建到临时目录并锁定唯一 wheel——复用 dist/ 会累积旧包，装到上一个版本也能通过；
2. wheel 内数据成员与期望集合双向相等（多出即失败：reports/、*.fromweb.json、.tmp/ 混入）；
3. 应用进程用**白名单**环境启动：黑名单会随新配置项失效，继承来的 DB_PATH/PLUGINS
   足以让「默认库落在临时目录」「插件来自 wheel」这类断言失真；
4. 启动、HTTP 断言与真实写入（配置保存 / 用例导出 / 基准 runner）分开断言，
   只发 GET 证明不了写入隔离；
5. 结束校验 site-packages 无新增库/配置/报告，且临时 data/cache 目录确实被使用；
6. 真实用户缓存目录按**前后快照对比**（相对路径 → size + mtime_ns）判泄漏——存在性断言
   在任何用过的机器上都会必然失败，只有干净 runner 才碰巧成立。
"""

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from packaging.markers import default_environment
from packaging.requirements import Requirement

REPO = Path(__file__).resolve().parent.parent

#: 索引上的项目名：--install-from testpypi / testpypi-sdist 时用它查元数据与安装
PROJECT_NAME = "briefdesk"

#: 两个索引各自独立使用，任何一步都不得同时指向两者（依赖混淆见 install_from_testpypi）
PYPI_INDEX = "https://pypi.org/simple/"
TESTPYPI_INDEX = "https://test.pypi.org/simple/"

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


#: 装到 venv 之后跑辅助片段时，必须确保导入的是**安装的包**：`python -c` 与 `python -m`
#: 都会把 cwd 放到 sys.path 首位，若 cwd 是仓库目录就会导入源码树——「wheel 里缺资源」
#: 这类问题会被源码树掩盖（本冒烟就曾因此把源码树当成被测对象）。非仓库 cwd + 显式断言双保险。
INSTALLED_GUARD = (
    "import briefdesk, pathlib; "
    "_p = pathlib.Path(briefdesk.__file__).resolve(); "
    "assert 'site-packages' in _p.parts, f'导入到了非安装路径: {_p}'; "
)

#: 版本探测片段：必须**带 INSTALLED_GUARD 且在非仓库 cwd 下执行**——仓库里 `python -m build`
#: 留下的 briefdesk.egg-info 会被 importlib.metadata 当成同名分发包，把发布版本读成仓库的静态
#: 版本号（实测把 0.1.0.dev1 读成 0.1.0），断言结果于是随构建残留漂移。
VERSION_PROBE = INSTALLED_GUARD + "import importlib.metadata as m; print(m.version('briefdesk'))"


def run(
    cmd: list[str], *, env: dict[str, str] | None = None, timeout: int = 900, cwd: Path | None = None
) -> str:
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
        cwd=str(cwd or REPO),
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


def published_requirements(version: str) -> list[str]:
    """从索引元数据取该版本的**运行时**依赖（按 marker 过滤掉 extra 行）。

    依赖清单取自索引而不是本地 pyproject：装的就是发布物声明的依赖，本地文件改了而没
    重新发布时，这里能立刻暴露差异。
    """
    url = f"https://test.pypi.org/pypi/{PROJECT_NAME}/{version}/json"
    try:
        with urllib.request.urlopen(url, timeout=60) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as error:
        raise SmokeFailure(f"读取 {url} 失败：{error}") from error
    requires = payload.get("info", {}).get("requires_dist") or []
    # extra == "ocr"/"dev" 这类 marker 在未请求 extra 时求值为假——不装可选重依赖
    # packaging 把 default_environment() 标成 TypedDict(Environment)，展开成 dict[str, str]
    # 会被 mypy 拒（值为 object）；显式复制并补上未请求 extra 时的空值
    environment: dict[str, str] = {
        key: str(value) for key, value in default_environment().items()
    }
    environment["extra"] = ""
    runtime: list[str] = []
    for raw in requires:
        requirement = Requirement(raw)
        if requirement.marker is not None and not requirement.marker.evaluate(environment):
            continue
        runtime.append(str(requirement))
    return runtime


def build_requirements() -> list[str]:
    """构建后端下限取自 pyproject 的 `build-system.requires`，不写死常量。

    为什么必须同源：关掉构建隔离后 pip **不校验** `build-system.requires`，装到偏旧的后端
    照样构建成功——例如 PEP 639 所需的 `setuptools>=77` 被漏掉时，从 sdist 重建的 wheel
    元数据与直接构建的不同，而资源集合断言恰好看不出来（成员名一致）。空列表说明读取路径
    失效，此时必须显式失败，不能让构建悄悄用默认后端。
    """
    payload = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    requires = payload.get("build-system", {}).get("requires") or []
    return [str(item) for item in requires]


def index_install_command(python: Path, version: str, *, from_sdist: bool) -> list[str]:
    """装**本包**的命令（依赖已由调用方从 PyPI 装好，所以 --no-deps）。

    from_sdist 的三个开关各有理由，缺一个都会静默失效：
    - `--no-binary` **只限定本包**：`:all:` 会把运行时依赖也拖去源码构建，既慢又会把依赖的
      构建失败算到本包头上；
    - `--no-cache-dir`：**实测**（pip 26.1.2）HTTP 缓存里有同名 wheel 时，pip 会直接
      `Using cached briefdesk-…-py3-none-any.whl`，`--no-binary` 形同虚设——源码路径被静默
      跳过，而资源断言照样全绿。缓存只对本包这一步关掉，依赖那步仍走缓存。
    - `--no-build-isolation`：隔离环境解析 `build-system.requires` 用的仍是当前索引，等于
      把 TestPyPI 上的同名包拉进构建环境——构建后端改由调用方先从 PyPI 预装。
    """
    command = [
        str(python), "-m", "pip", "install", "--no-deps", "-v",
        "--index-url", TESTPYPI_INDEX,
    ]
    if from_sdist:
        command += ["--no-binary", PROJECT_NAME, "--no-cache-dir", "--no-build-isolation"]
    command.append(f"{PROJECT_NAME}=={version}")
    return command


def _package_log_excerpt(output: str, limit: int = 4) -> str:
    """失败时给出能直接定位的行：pip 的 `Using cached …whl` 就藏在这些行里。"""
    interesting = [
        line.strip()
        for line in output.splitlines()
        if "briefdesk" in line or "Downloading" in line or "Using cached" in line
    ]
    return " | ".join(interesting[:limit]) or output.strip()[-200:]


def assert_sdist_install_log(output: str, version: str) -> None:
    """日志必须证明走的是 sdist，而不只是「装成功了」。

    纯 Python 包 pip 默认选 wheel：sdist 被静默跳过时，冒烟依旧全绿，而「sdist 少带运行时
    资源」「从 sdist 重建的 wheel 与直接构建的不一致」这两类问题永远看不到——所以断言看
    日志证据（下载到 .tar.gz + 本地构建），不看安装结果。
    """
    sdist_name = f"{PROJECT_NAME}-{version}.tar.gz"
    if sdist_name not in output:
        raise SmokeFailure(
            f"安装日志里没有 {sdist_name}——pip 没有走源码分发包"
            f"（日志片段：{_package_log_excerpt(output)}）"
        )
    if "Building wheel for" not in output:
        raise SmokeFailure(
            "安装日志里没有本地构建记录——源码包可能被换成了现成 wheel"
            f"（日志片段：{_package_log_excerpt(output)}）"
        )


def install_from_testpypi(
    python: Path, version: str, *, from_sdist: bool = False, cwd: Path | None = None
) -> None:
    """从 TestPyPI 装指定版本：**两段式，每段只对一个索引**。

    为什么不用 --index-url testpypi + --extra-index-url pypi：pip 会把两个索引合并后取
    最高版本，而 TestPyPI 上存在别人试传的同名依赖（实测 fastapi 1.0 稳定版 vs 正式 PyPI
    的 0.142.x），依赖会被影子化成残缺 sdist，构建直接失败。两段式每段只有一个索引，
    混淆不可能发生，且「包来自 test.pypi.org」的日志断言才有意义。

    from_sdist=True 走源码分发包：装的是 sdist、构建在本地发生（理由见 index_install_command
    与 assert_sdist_install_log）。

    cwd 是**探测版本时的工作目录**，必须由调用方给一个非仓库目录：`python -c` 把 cwd 放进
    sys.path 首位，仓库根的 briefdesk.egg-info 会让 importlib.metadata 读到那个版本（见 VERSION_PROBE）。
    """
    source = "源码分发" if from_sdist else "wheel"
    log(f"从 TestPyPI 安装 {PROJECT_NAME}=={version}（依赖走 PyPI，本包走 TestPyPI，{source}路径）")
    requires = published_requirements(version)
    if requires:
        log(f"先装 {len(requires)} 条运行时依赖（--index-url {PYPI_INDEX}）")
        run(
            [str(python), "-m", "pip", "install", "--index-url", PYPI_INDEX, *requires],
            timeout=1800,
        )
    if from_sdist:
        backend = build_requirements()
        if not backend:
            raise SmokeFailure("pyproject.toml 的 build-system.requires 为空——无法预装构建后端")
        log(f"预装构建后端：{' '.join(backend)}（--index-url {PYPI_INDEX}）")
        run(
            [str(python), "-m", "pip", "install", "--index-url", PYPI_INDEX, *backend],
            timeout=600,
        )
    out = run(index_install_command(python, version, from_sdist=from_sdist), timeout=900)
    if "test.pypi.org" not in out:
        raise SmokeFailure("安装日志里没有 test.pypi.org——无法证明包来自 TestPyPI")
    if from_sdist:
        assert_sdist_install_log(out, version)
    run([str(python), "-m", "pip", "check"], timeout=300)  # 依赖闭包自洽（缺依赖即失败）
    installed = run(
        [str(python), "-c", VERSION_PROBE],
        timeout=120,
        cwd=cwd or Path(tempfile.gettempdir()),
    ).strip()
    if installed != version:
        raise SmokeFailure(f"安装版本不符：期望 {version}，实际 {installed}")
    log(f"来源、版本与依赖闭包断言通过：{installed} 来自 test.pypi.org（{source}路径）")


def assert_installed_resources(python: Path, cwd: Path) -> None:
    """装完按与 wheel 冒烟同一份期望集合核对磁盘资源（多出与缺失都失败）。

    源码安装与 wheel 安装共用这一份断言：sdist 少了运行时资源时，缺项就在这里暴露。

    cwd 必须是**非仓库目录**：`python -c` 把 cwd 放 sys.path 首位，仓库根会让导入落到
    源码树，于是源码树里那些「不进包」的文件（plugins/benchmark/README.md、图标清单等）
    会被算成「多出」——反过来也会掩盖 wheel 真正缺资源。
    """
    code = (
        INSTALLED_GUARD
        + "import json, pathlib, briefdesk\n"
        "root = pathlib.Path(briefdesk.__file__).resolve().parent.parent\n"
        "files = sorted(\n"
        "    p.relative_to(root).as_posix()\n"
        "    for p in root.glob('briefdesk/**/*')\n"
        "    if p.is_file() and '__pycache__' not in p.parts and p.suffix != '.py'\n"
        ")\n"
        "print(json.dumps(files))\n"
    )
    installed = set(json.loads(run([str(python), "-c", code], timeout=120, cwd=cwd)))
    expected = set(expected_manifest())
    extra, missing = sorted(installed - expected), sorted(expected - installed)
    if extra or missing:
        raise SmokeFailure(f"安装后的资源与期望集合不一致：多出 {extra}，缺失 {missing}")
    log(f"资源断言通过：{len(installed)} 个数据文件来自安装的包")


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
        INSTALLED_GUARD
        + "import asyncio\n"
        "from briefdesk.plugins.benchmark import store\n"
        "case = {\"id\": \"smoke-1\", \"note\": \"smoke\", \"message\": {\"msg_id\": \"m1\", \"content\": \"smoke\"}, \"old_title\": \"t\", \"key_info\": \"\", \"expected\": {\"title\": \"smoke\"}}\n"
        "print(asyncio.run(store.export_fromweb(\"title\", [case])))\n"
    )
    out = run([str(python), "-c", export_code], env=env, timeout=300, cwd=tmp)
    exported = data_dir / "benchmark" / "cases" / "title.fromweb.json"
    if not exported.exists() or str(exported) not in out:
        raise SmokeFailure(f"用例导出未落在用户数据目录: {out.strip()}")
    log("写入步骤 2/5 通过：Web 导出写用户用例目录")

    cases_dir = run(
        [
            str(python), "-c",
            INSTALLED_GUARD + "from briefdesk.plugins.benchmark import store; print(store.PACKAGE_CASES_DIR)",
        ],
        env=env,
        timeout=120,
        cwd=tmp,
    ).strip()
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
        # python -m 会把 cwd 放 sys.path 首位：cwd 必须是仓库外，否则跑的是源码树
        cwd=tmp,
    )
    if not (run_dir / "report.json").exists():
        raise SmokeFailure(f"基准 runner 未产出报告: {sorted(p.name for p in run_dir.iterdir())}")
    log("写入步骤 3/5 通过：基准 runner 产出报告")

    defaults = run(
        [
            str(python), "-c",
            INSTALLED_GUARD
            + "from briefdesk.plugins.benchmark import cli, supervisor; "
            "print(cli._out_dir_arg(None)); print(supervisor._runs_root())",
        ],
        env=env,
        timeout=120,
        cwd=tmp,
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


def snapshot_tree(root: Path) -> dict[str, tuple[int, int]]:
    """递归指纹：相对路径 → (size, mtime_ns)。只 stat，不读内容，也不建目录。

    为什么不只比顶层条目：泄漏可能表现为**往已存在的运行目录里写文件**（开发机上本来
    就有历史运行记录），那时条目集合不变、只看名字会漏判。用 size + mtime_ns 与
    tests/test_db.py 的「旧库不变」断言同源（刻意不碰 atime：读操作会改它）。
    """
    if not root.exists():
        return {}
    fingerprint: dict[str, tuple[int, int]] = {}
    for path in root.rglob("*"):
        try:
            stat = path.stat()
        except OSError:
            continue  # 扫描期间被删除/不可访问：跳过而不是崩掉断言
        fingerprint[path.relative_to(root).as_posix()] = (stat.st_size, stat.st_mtime_ns)
    return fingerprint


def assert_tree_unchanged(root: Path, before: dict[str, tuple[int, int]], *, why: str) -> None:
    """断言目录内容与快照一致（新增 / 删除 / 改动都算失败）。"""
    after = snapshot_tree(root)
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed = sorted(key for key in set(before) & set(after) if before[key] != after[key])
    if added or removed or changed:
        raise SmokeFailure(
            f"{why}: {root}（本次新增 {added[:5]}，删除 {removed[:5]}，改动 {changed[:5]}）"
            "——若确有应用在同时运行基准，请关掉后重跑"
        )


def assert_no_leaks(
    venv: Path,
    data_dir: Path,
    cache_dir: Path,
    settings_file: Path,
    real_runs_root: Path,
    real_runs_before: dict[str, tuple[int, int]],
) -> None:
    """site-packages 不得新增库/配置/报告；真实用户缓存目录不得被本次冒烟改动。

    真实目录用**前后快照对比**而不是「必须不存在」：开发机上本来就有历史运行记录，
    存在性断言会在任何用过的机器上必然失败（CI 的干净 runner 掩盖了这一点）。快照
    对比在干净机器上等价于原断言，在已用过的机器上则能真正证明「本次没写进去」。
    """
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
    assert_tree_unchanged(
        real_runs_root,
        real_runs_before,
        why="真实用户缓存目录被本次冒烟写入（BRIEFDESK_CACHE_DIR 未生效？）",
    )
    log("隔离断言通过：site-packages 无产物、真实用户缓存目录与冒烟前一致")


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
    parser = argparse.ArgumentParser(
        description="wheel 冒烟：默认装本地构建的 wheel；--install-from testpypi 装索引上的 wheel，"
        "testpypi-sdist 装索引上的源码分发包（强制本地构建）"
    )
    parser.add_argument("--install-from", choices=("local", "testpypi", "testpypi-sdist"), default="local")
    parser.add_argument(
        "--version", default="", help="--install-from testpypi / testpypi-sdist 时必填：断言装到的版本"
    )
    args = parser.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="briefdesk-smoke-"))
    data_dir = tmp / "data"
    cache_dir = tmp / "cache"
    settings_file = tmp / "settings.env"
    app_log = tmp / "app.log"
    process: subprocess.Popen | None = None
    failed = False
    try:
        if args.install_from in ("testpypi", "testpypi-sdist"):
            if not args.version:
                raise SmokeFailure(
                    f"--install-from {args.install_from} 需要 --version（例如 --version 0.1.0.dev1）"
                )
            venv = make_venv(tmp)
            python = venv_python(venv)
            install_from_testpypi(
                python, args.version, from_sdist=args.install_from == "testpypi-sdist", cwd=tmp
            )
            assert_installed_resources(python, tmp)
        else:
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
                    INSTALLED_GUARD
                    + "from briefdesk.plugins.benchmark import supervisor; print(supervisor._runs_root())",
                ],
                env=probe_env,
                timeout=120,
                cwd=tmp,
            ).strip()
        )
        # 快照点在此处：之前的构建/建 venv/装包都不导入本包（briefdesk/__init__.py 为空），
        # 且本次探测只算路径、不建目录——从这里开始的写入都算泄漏
        real_runs_before = snapshot_tree(real_runs_root)
        log(f"真实用户缓存目录（本次不得改动，当前 {len(real_runs_before)} 个条目）: {real_runs_root}")
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
        assert_no_leaks(venv, data_dir, cache_dir, settings_file, real_runs_root, real_runs_before)
        log("冒烟通过")
        return 0
    except SmokeFailure as error:
        failed = True
        print(f"[wheel-smoke] 失败: {error}", file=sys.stderr, flush=True)
        print(f"[wheel-smoke] 现场保留在 {tmp}（含 venv 与日志，便于定位）", file=sys.stderr, flush=True)
        return 1
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
        # 失败时保留现场：原先只要应用进程没起来就删目录，而报错信息同时声称"现场保留"，
        # 拿到的路径其实已经不存在（诊断价值归零）；成功路径与"进程未退出就不删"的 Windows
        # 约束都不变。
        if not failed and (process is None or process.poll() is not None):
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
