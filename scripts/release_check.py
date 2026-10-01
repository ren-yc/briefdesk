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

_VERSION_RE = re.compile(r'(?m)^version = "[^"]*"$')
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


@contextmanager
def patched_version(pyproject: Path, version: str):
    """构建期间把 version 改成给定值，退出时**逐字节**还原原文件。"""
    original = pyproject.read_text(encoding="utf-8")
    replaced, count = _VERSION_RE.subn(f'version = "{version}"', original, count=1)
    if count != 1:
        raise ReleaseCheckFailure(
            f'{pyproject.name} 的 version 行不是预期的 version = "<值>" 形态'
        )
    pyproject.write_text(replaced, encoding="utf-8")
    try:
        yield
    finally:
        pyproject.write_text(original, encoding="utf-8")
        if pyproject.read_text(encoding="utf-8") != original:
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
