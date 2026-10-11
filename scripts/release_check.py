"""发布预检与构建：定版本 →（必要时瞬态改写）→ 构建 → 产物成员断言 → twine check → 还原。

为什么需要它：`pyproject.toml` 的 `version` 是静态值，`python -m build` 没有命令行覆盖版本的
能力，而索引**不允许覆盖同版本**——重复上传必然 400。这里把「版本唯一」做成脚本保证：查目标
索引已有 releases 定出版本，只在构建期间把版本写进 pyproject（`try/finally` 还原并逐字节
校验），仓库里永不落地试发号；版本与仓库静态值一致时（正式发布）根本不改写文件。

用法：
    python scripts/release_check.py                       # TestPyPI：算下一个空闲 0.1.0.devN
    python scripts/release_check.py --version 0.1.0rc1    # TestPyPI：指定版本的演练
    python scripts/release_check.py --target pypi         # 正式索引：版本必须等于仓库静态值
    python scripts/release_check.py --upload              # 追加 twine upload（本机试发）
    python scripts/release_check.py --no-build            # 只报告本次会构建的版本

凭据：本机 `--upload` 由 twine 从系统钥匙串取
（`python -m keyring set https://test.pypi.org/legacy/ __token__`），不经过环境变量、不写文件；
CI 的上传不用本机凭据（OIDC 可信发布，见 .github/workflows/publish.yml）。

为什么构建产物也要断言成员：`twine check` 只验元数据格式，看不出「wheel/sdist 少了运行时
资源」——那类问题会一路发到索引上。断言与 wheel 冒烟、sdist 检查共用 scripts/runtime_manifest.py。
"""

import argparse
import json
import re
import subprocess
import sys
import tomllib
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    # 直跑时 sys.path[0] 是 scripts/：包路径需显式补，否则 scripts.* 导不到
    sys.path.insert(0, str(REPO))

from scripts.runtime_manifest import expected_data_members  # noqa: E402
from scripts.sdist_check import (  # noqa: E402
    CheckFailure,
    assert_sdist_contents,
    wheel_data_members,
)

PYPROJECT = REPO / "pyproject.toml"
DIST_DIR = REPO / "dist"
PROJECT_NAME = "briefdesk"

#: 目标索引：JSON 元数据端点与 twine 仓库名（本机 `--upload` 用）。
#: 下载来源标记只在 wheel 冒烟里用，且刻意区分开——`pypi.org` 是 `test.pypi.org` 的子串。
INDEXES = {
    "testpypi": {
        "json": "https://test.pypi.org/pypi",
        "twine_repository": "testpypi",
    },
    "pypi": {
        "json": "https://pypi.org/pypi",
        "twine_repository": "pypi",
    },
}

_DEV_SUFFIX_RE = re.compile(r"\.dev\d+$")


class ReleaseCheckFailure(RuntimeError):
    """预检/构建失败：携带可直接定位的说明。"""


def log(message: str) -> None:
    print(f"[release-check] {message}", flush=True)


def run(cmd: list[str], *, timeout: int = 900) -> str:
    result = subprocess.run(
        cmd,
        cwd=str(REPO),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    if result.stdout is None or result.stderr is None:
        raise ReleaseCheckFailure(f"子进程输出读取失败（编码不一致？）: {' '.join(cmd)}")
    if result.returncode != 0:
        tail = (result.stdout + result.stderr).strip()[-1500:]
        raise ReleaseCheckFailure(f"命令失败（exit {result.returncode}）: {' '.join(cmd)}\n{tail}")
    return result.stdout


def read_base_version(pyproject: Path = PYPROJECT) -> str:
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    version = data.get("project", {}).get("version")
    if not isinstance(version, str) or not version:
        raise ReleaseCheckFailure(f"{pyproject.name} 里没有静态 [project].version")
    return version


def next_dev_version(base: str, releases: set[str]) -> str:
    """下一个空闲的 `<release>.devN`；已有 dev 号会被归并到同一版本线。"""
    release = _DEV_SUFFIX_RE.sub("", base)
    index = 1
    while f"{release}.dev{index}" in releases:
        index += 1
    return f"{release}.dev{index}"


def fetch_published_versions(index_json: str, project: str = PROJECT_NAME) -> set[str]:
    """目标索引上已发布的版本集合；项目尚不存在时索引返回 404，等价于「空」。"""
    url = f"{index_json}/{project}/json"
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return set()
        raise ReleaseCheckFailure(f"查询 {url} 失败：HTTP {error.code}") from error
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as error:
        raise ReleaseCheckFailure(f"查询 {url} 失败：{error}") from error
    return set(payload.get("releases", {}))


def _apply_version(original: bytes, version: str, *, path_for_message: Path) -> bytes:
    """构造替换了 [project].version 的候选 bytes；定位或语义校验失败即抛错（不写盘）。

    为什么按字节替换而不是改写后重序列化 TOML：注释、空行、CRLF/LF 混合换行与键序
    都必须原样保留——文本模式读写在 Windows 会把 LF 规范化成 CRLF，bytes 变了而
    文本比较相等，试发一次就弄脏整个文件。target 取「静态值本身」而不是任意
    version 行：`[tool]\nversion = "..."` 这类工具表版本不会被波及。
    """
    data = tomllib.loads(original.decode("utf-8"))
    static = data.get("project", {}).get("version")
    if not isinstance(static, str) or not static:
        raise ReleaseCheckFailure(f"{path_for_message.name} 里没有静态 [project].version")
    target = f'version = "{static}"'.encode()
    replacement = f'version = "{version}"'.encode()
    first = original.find(target)
    if first < 0:
        raise ReleaseCheckFailure(
            f'{path_for_message.name} 的 [project].version 不是预期的 version = "<值>" 单行形态'
        )
    if original.find(target, first + 1) >= 0:
        raise ReleaseCheckFailure(
            f'{path_for_message.name} 里 version = "{static}" 出现多次，替换目标有歧义'
        )
    replaced = original[:first] + replacement + original[first + len(target):]
    # 语义双断言：替换后必须仍可解析，且除 project.version 外整棵树逐项相等——
    # 目标值出现在注释/字符串里、或字节定位越界碰到别的表时都在这里拦下。
    new_data = tomllib.loads(replaced.decode("utf-8"))
    if new_data.get("project", {}).get("version") != version:
        raise ReleaseCheckFailure("替换后 [project].version 不是目标值")
    new_data.setdefault("project", {})["version"] = static
    if new_data != data:
        raise ReleaseCheckFailure("替换波及了 project.version 之外的 TOML 内容")
    return replaced


@contextmanager
def patched_version(pyproject: Path, version: str):
    """构建期间把 [project].version 改成给定值，退出时**逐字节**还原原文件。

    首次写入也进 try：部分写入后抛错同样要尝试恢复。恢复后用 bytes 相等自校验——
    文本比较在换行被规范化时检不出文件已被改写。
    """
    original = pyproject.read_bytes()
    replaced = _apply_version(original, version, path_for_message=pyproject)
    try:
        pyproject.write_bytes(replaced)
        yield
    finally:
        pyproject.write_bytes(original)
        if pyproject.read_bytes() != original:
            raise ReleaseCheckFailure(f"{pyproject.name} 还原失败——请用 git diff 检查并手工恢复")


def assert_artifact_members(wheel: Path, sdist: Path) -> None:
    """上传前用登记表核对两件产物的数据成员（多出与缺失都失败）。

    twine check 只看元数据格式；少了运行时资源的包会一路发到索引上（冒烟要等装完才发现）。
    sdist 侧直接复用 scripts/sdist_check.py 的断言（同一份登记表与禁止清单），wheel 侧比对
    同一份期望集合——两件产物的判据不允许各写一份。
    """
    members = wheel_data_members(wheel)
    expected = expected_data_members()
    extra = sorted(set(members) - set(expected))
    missing = sorted(set(expected) - set(members))
    if extra or missing:
        raise ReleaseCheckFailure(f"wheel 资源与期望集合不一致：多出 {extra}，缺失 {missing}")
    try:
        assert_sdist_contents(sdist)
    except CheckFailure as error:
        raise ReleaseCheckFailure(f"sdist 成员断言失败：{error}") from error
    log(f"产物成员断言通过：wheel 与 sdist 各 {len(expected)} 个运行时资源")


def build_artifacts(version: str, dist_dir: Path = DIST_DIR) -> list[Path]:
    dist_dir.mkdir(parents=True, exist_ok=True)
    for stale in list(dist_dir.glob("*.whl")) + list(dist_dir.glob("*.tar.gz")):
        stale.unlink()  # 复用 dist/ 会校验/上传到历史 artifact
    if version == read_base_version():
        # 正式发布路径：仓库静态版本就是要发的版本，不改写文件（也就没有还原风险）
        log(f"构建 sdist + wheel（版本 {version}，与仓库静态版本一致，不改写 pyproject）→ {dist_dir}")
        run([sys.executable, "-m", "build", "--sdist", "--wheel", "--outdir", str(dist_dir)])
    else:
        log(f"构建 sdist + wheel（版本 {version}，构建期间瞬态改写 pyproject）→ {dist_dir}")
        # patched_version 退出时逐字节还原并自校验，失败会直接抛 ReleaseCheckFailure
        with patched_version(PYPROJECT, version):
            run([sys.executable, "-m", "build", "--sdist", "--wheel", "--outdir", str(dist_dir)])
    wheels = sorted(dist_dir.glob(f"{PROJECT_NAME}-{version}-*.whl"))
    sdists = sorted(dist_dir.glob(f"{PROJECT_NAME}-{version}.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise ReleaseCheckFailure(
            f"期望各一个 {version} 的 wheel/sdist，实际 wheel={[w.name for w in wheels]} sdist={[w.name for w in sdists]}"
        )
    artifacts = [*wheels, *sdists]
    assert_artifact_members(wheels[0], sdists[0])
    log("twine check " + " ".join(a.name for a in artifacts))
    run([sys.executable, "-m", "twine", "check", *[str(a) for a in artifacts]])
    return artifacts


def twine_upload_command(artifacts: list[Path], *, target: str) -> list[str]:
    """本机试发的上传命令（CI 走 OIDC，不用 twine）。"""
    return [
        sys.executable, "-m", "twine", "upload",
        "--repository", INDEXES[target]["twine_repository"],
        "--non-interactive",
        *[str(a) for a in artifacts],
    ]


def upload(artifacts: list[Path], *, target: str) -> None:
    log(f"上传到 {target}（凭据取自系统钥匙串）")
    run(twine_upload_command(artifacts, target=target), timeout=1800)


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass  # 非可重配置流保持原样：Windows 控制台默认 ANSI 代码页
    parser = argparse.ArgumentParser(description="发布预检与构建（TestPyPI 试发 / PyPI 正式）")
    parser.add_argument("--target", choices=tuple(INDEXES), default="testpypi", help="目标索引（默认 testpypi）")
    parser.add_argument(
        "--version",
        default="",
        help="显式版本（如 0.1.0rc1 / 0.1.0）；缺省时试发算下一个空闲 dev 号、正式取仓库静态版本",
    )
    parser.add_argument("--upload", action="store_true", help="构建后上传到目标索引（本机试发；CI 走 OIDC）")
    parser.add_argument("--no-build", action="store_true", help="只报告本次会构建的版本")
    parser.add_argument("--dist-dir", default=str(DIST_DIR), help="构建输出目录（默认 dist/）")
    args = parser.parse_args()

    dist_dir = Path(args.dist_dir).resolve()
    try:
        base = read_base_version()
        published = fetch_published_versions(INDEXES[args.target]["json"])
        # 缺省版本按目标索引取：试发要一个空闲的 dev 号，正式索引发的就是仓库声明的版本
        default_version = base if args.target == "pypi" else next_dev_version(base, published)
        version = args.version or default_version
        if args.target == "pypi" and version != base:
            raise ReleaseCheckFailure(
                f"正式索引的版本必须等于仓库静态版本：仓库 {base}，传入 {version}"
                "——预发布演练请用 --target testpypi"
            )
        if version in published:
            raise ReleaseCheckFailure(f"{args.target} 上已存在 {version}：索引不允许覆盖同版本")
        log(f"仓库版本 {base}；{args.target} 已发布 {len(published)} 个版本；本次构建：{version}")
        if args.no_build:
            return 0
        artifacts = build_artifacts(version, dist_dir)
        smoke = f"python scripts/wheel_smoke.py --install-from {args.target}"
        if args.upload:
            upload(artifacts, target=args.target)
            log(
                f"已发布 {version}；端到端验证（两条安装路径都要跑）："
                f"{smoke} --version {version}（索引上的 wheel）与同一命令加 --from-sdist"
                "（源码分发 + 本地构建）"
            )
        else:
            log(
                "未上传。CI 发布走 .github/workflows/publish.yml（OIDC，人工触发）；本机试发加 --upload，"
                f"或手工：python -m twine upload --repository {INDEXES[args.target]['twine_repository']} "
                f"--non-interactive {' '.join(a.name for a in artifacts)}"
            )
        return 0
    except ReleaseCheckFailure as error:
        print(f"[release-check] 失败: {error}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
