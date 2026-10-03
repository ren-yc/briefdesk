"""sdist 产出断言：构建 → 成员集合双向相等 → 从 sdist 重建 wheel 与直接 wheel 一致。

为什么单独成脚本（不进 pytest）：断言必须先真正构建出 sdist，与构建强耦合；放进
pytest 会给「跑测试」引入「先能构建」的隐式前置——与 scripts/wheel_smoke.py 同口径。

本地门禁：`python scripts/sdist_check.py`（构建到唯一临时目录，不碰 dist/）
CI：在 wheel-smoke job 内跑同一入口。

为什么期望集合从 scripts/runtime_manifest.py 取：sdist 与 wheel 的运行时资源必须是
同一份事实。任何一处多打/少打都由「双向相等」拦下（多出即失败——图标清单、维护
脚本、*.fromweb.json 这类只要漏进一个就会命中）。
"""

import fnmatch
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    # 直跑时 sys.path[0] 是 scripts/：包路径需显式补，否则 scripts.runtime_manifest 导不到
    sys.path.insert(0, str(REPO))

from scripts.runtime_manifest import expected_data_members

#: sdist 里的非数据成员：标准元数据与构建产物。刻意**按模式**而不是锁文件名——
#: setuptools 升级可能增删 egg-info 内的文件，锁死会让门禁因工具升级假红。
ALLOWED_NON_DATA = (
    "PKG-INFO",
    "setup.cfg",
    "MANIFEST.in",
    "briefdesk.egg-info/*",
    "pyproject.toml",
    "README.md",
    "LICENSE",
    "briefdesk/*.py",
    # vendor 的两个 subtree SDK：随包分发的镜像源码（路径按工作区拼写）
    "vendor/weflow_sdk/*.py",
    "vendor/weflow_sdk/generated/**/*.py",
    "vendor/qqflow_sdk/*.py",
    "vendor/qqflow_sdk/generated/**/*.py",
)

#: wheel 侧非数据成员（与 scripts/wheel_smoke.py 同口径；fnmatch 的 * 跨 /）
WHEEL_ALLOWED_NON_DATA = (
    "briefdesk-*.dist-info/*",
    "briefdesk/*.py",
)

#: 绝不允许进 sdist 的内容。构建时它们就躺在工作区里（.env、库文件、用户导出的用例），
#: 一旦有人加宽规则就会被打包——这里是「多出项」之外的显式命名兜底，报错更可读。
FORBIDDEN = (
    ("*.fromweb.json", "网页导出的基准用例（含真实聊天内容）"),
    ("reports/*", "基准报告产物"),
    (".tmp/*", "基准临时运行目录"),
    ("*.sqlite", "数据库文件"),
    ("*.sqlite-wal", "数据库 WAL"),
    ("*.sqlite-shm", "数据库 SHM"),
    (".env", "本地配置（含密钥）"),
    (".env.*", "本地配置（含密钥）"),
    ("tests/*", "测试代码不随包分发"),
    ("scripts/*", "维护脚本不随包分发"),
    ("docs/*", "仓库文档不随包分发"),
    ("briefdesk/ui/icon-manifest.txt", "图标清单是源码维护资源"),
    ("briefdesk/ui/icons/README.md", "图标说明是源码维护资源"),
)


class CheckFailure(RuntimeError):
    """检查失败：携带可直接定位的说明。"""


def log(message: str) -> None:
    print(f"[sdist-check] {message}", flush=True)


def run(cmd: list[str], *, cwd: Path = REPO, timeout: int = 900) -> str:
    """执行外部命令；非零退出即失败（输出附在异常里）。

    子进程输出统一按 UTF-8 读、并让子进程也按 UTF-8 写：text=True 缺省按 locale
    （CI 的 Windows runner = cp1252）解码，而仓库入口按约定输出 UTF-8，UTF-8 续字节
    在 cp1252 里未定义——解码异常发生在 subprocess 的读取线程里，只被
    threading.excepthook 打印、不冒泡，run() 会照常返回且 stdout 静默变成 None。
    """
    child_env = dict(os.environ)
    child_env.setdefault("PYTHONIOENCODING", "utf-8")
    result = subprocess.run(
        cmd,
        cwd=str(cwd),
        env=child_env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    if result.stdout is None or result.stderr is None:
        raise CheckFailure(f"子进程输出读取失败（编码不一致？）: {' '.join(cmd)}")
    if result.returncode != 0:
        tail = (result.stdout + result.stderr).strip()[-1500:]
        raise CheckFailure(f"命令失败（exit {result.returncode}）: {' '.join(cmd)}\n{tail}")
    return result.stdout


def build_sdist(out_dir: Path) -> Path:
    log(f"构建 sdist → {out_dir}")
    run([sys.executable, "-m", "build", "--sdist", "--outdir", str(out_dir)])
    archives = sorted(out_dir.glob("*.tar.gz"))
    if len(archives) != 1:
        raise CheckFailure(
            f"期望恰好一个 sdist，实际 {len(archives)} 个: {[a.name for a in archives]}"
        )
    return archives[0]


def sdist_members(sdist: Path) -> list[str]:
    """归档内的相对成员路径（剥掉 `briefdesk-<version>/` 顶层前缀）。"""
    with tarfile.open(sdist) as archive:
        names = [m.name.replace("\\", "/") for m in archive.getmembers() if m.isfile()]
    if not names:
        raise CheckFailure(f"sdist 为空: {sdist.name}")
    top = names[0].split("/")[0] + "/"
    if not all(name.startswith(top) for name in names):
        raise CheckFailure(f"归档成员不在同一顶层目录下: {sorted(names)[:5]}")
    return sorted(name[len(top):] for name in names)


def assert_sdist_contents(sdist: Path) -> list[str]:
    names = sdist_members(sdist)
    data = [n for n in names if not any(fnmatch.fnmatch(n, p) for p in ALLOWED_NON_DATA)]
    expected = expected_data_members()
    extra = sorted(set(data) - set(expected))
    missing = sorted(set(expected) - set(data))
    if extra or missing:
        raise CheckFailure(f"sdist 资源与期望集合不一致：多出 {extra}，缺失 {missing}")

    if "PKG-INFO" not in names:
        raise CheckFailure("sdist 缺 PKG-INFO（元数据未生成？）")

    for pattern, why in FORBIDDEN:
        hits = [n for n in names if fnmatch.fnmatch(n, pattern)]
        if hits:
            raise CheckFailure(f"sdist 混入禁止内容（{why}）: {hits[:5]}")

    dirs_with_init = {n.rsplit("/", 1)[0] for n in names if n.endswith("/__init__.py")}
    stray = [
        n for n in names if n.endswith(".py") and n.rsplit("/", 1)[0] not in dirs_with_init
    ]
    if stray:
        raise CheckFailure(f".py 出现在非包目录（调试脚本误入？）: {stray}")

    log(f"sdist 成员断言通过：{len(names)} 个成员，其中数据成员 {len(data)} 个")
    return data


def extract(sdist: Path, dest: Path) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(sdist) as archive:
        archive.extractall(dest, filter="data")
    roots = [p for p in dest.iterdir() if p.is_dir()]
    if len(roots) != 1:
        raise CheckFailure(f"解包后应恰有一个顶层目录: {[p.name for p in roots]}")
    return roots[0]


def build_wheel(srcdir: Path, out_dir: Path) -> Path:
    run([sys.executable, "-m", "build", "--wheel", "--outdir", str(out_dir), str(srcdir)])
    wheels = sorted(out_dir.glob("*.whl"))
    if len(wheels) != 1:
        raise CheckFailure(f"期望恰好一个 wheel，实际 {len(wheels)} 个: {[w.name for w in wheels]}")
    return wheels[0]


def wheel_data_members(wheel: Path) -> list[str]:
    names = [n.replace("\\", "/") for n in zipfile.ZipFile(wheel).namelist()]
    return sorted(n for n in names if not any(fnmatch.fnmatch(n, p) for p in WHEEL_ALLOWED_NON_DATA))


def assert_wheel_consistency(sdist: Path, workdir: Path) -> None:
    """从 sdist 重建的 wheel 必须与直接构建的 wheel 数据成员一致。

    这条同时抓两类失败：sdist 少带了资源（重建的 wheel 缺文件），以及本地 wheel
    混进了只有工作区才有的文件（直接构建多文件）。
    """
    unpacked = extract(sdist, workdir / "unpacked")
    from_sdist = build_wheel(unpacked, workdir / "wheel-from-sdist")
    direct = build_wheel(REPO, workdir / "wheel-direct")
    a, b = wheel_data_members(from_sdist), wheel_data_members(direct)
    if a != b:
        raise CheckFailure(
            "从 sdist 重建的 wheel 与直接构建的 wheel 不一致："
            f"只在 sdist 侧 {sorted(set(a) - set(b))}，只在直接构建侧 {sorted(set(b) - set(a))}"
        )
    log(f"wheel 一致性通过：两侧数据成员均为 {len(a)} 个")

    # 声明对了不等于打进去了：两侧互比抓不到「两边一起漏」。这里对直接构建的
    # wheel 逐个点名 vendor 的两个 SDK——它们靠 packages.find 的第二 where 根
    # 进包，是「随包分发」这一供应方式的唯一保证。
    required_vendor = (
        "weflow_sdk/client.py",
        "weflow_sdk/generated/spec.json",
        "weflow_sdk/generated/weflow_sdk/py.typed",
        "qqflow_sdk/client.py",
        "qqflow_sdk/generated/spec.json",
        "qqflow_sdk/generated/qqflow_sdk/py.typed",
    )
    vendor_missing = [name for name in required_vendor if name not in b]
    if vendor_missing:
        raise CheckFailure(f"wheel 缺少 vendor SDK 成员（随包分发失效）: {vendor_missing}")
    log(f"vendor SDK 成员齐全：{len(required_vendor)} 项")
    run([sys.executable, "-m", "twine", "check", str(sdist), str(from_sdist), str(direct)])
    log("twine check 通过（sdist + 两个 wheel）")


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass  # 非可重配置流（测试捕获/已关闭）保持原样：Windows 控制台默认 ANSI 代码页
    workdir = Path(tempfile.mkdtemp(prefix="briefdesk-sdist-"))
    try:
        sdist = build_sdist(workdir / "dist")
        data = assert_sdist_contents(sdist)
        assert_wheel_consistency(sdist, workdir)
        log(f"sdist 检查通过：{sdist.name}，数据成员 {len(data)} 个")
        return 0
    except CheckFailure as error:
        print(f"[sdist-check] 失败: {error}", file=sys.stderr, flush=True)
        print(f"[sdist-check] 现场保留在 {workdir}", file=sys.stderr, flush=True)
        return 1
    finally:
        if sys.exc_info()[0] is None:
            shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
