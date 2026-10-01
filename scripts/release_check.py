"""TestPyPI 试发的版本预检与构建：算出唯一 dev 版本 → 瞬态改版本 → 构建 → 还原。

为什么需要它：`pyproject.toml` 的 `version` 是静态值，`python -m build` 没有命令行覆盖
版本的能力，而索引**不允许覆盖同版本**——重复执行必然 400。这里把「版本唯一」做成
脚本保证：查 TestPyPI 已有 releases 算出下一个空闲的 `0.1.0.devN`，只在构建期间把
版本写进 pyproject（`try/finally` 还原并逐字节校验），仓库里永不落地 dev 号。

用法：
    python scripts/release_check.py                  # 预检 + 构建到 dist/ + twine check
    python scripts/release_check.py --upload         # 追加 twine upload --repository testpypi
    python scripts/release_check.py --no-build       # 只报告下一个可用版本

凭据：twine 从系统钥匙串取（`python -m keyring set https://test.pypi.org/legacy/ __token__`），
不经过环境变量、不写文件。
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
PYPROJECT = REPO / "pyproject.toml"
DIST_DIR = REPO / "dist"
TESTPYPI_JSON = "https://test.pypi.org/pypi"
PROJECT_NAME = "briefdesk"

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


def fetch_published_versions(index_json: str = TESTPYPI_JSON, project: str = PROJECT_NAME) -> set[str]:
    """索引上已发布的版本集合；项目尚不存在时索引返回 404，等价于「空」。"""
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


def build_artifacts(version: str, dist_dir: Path = DIST_DIR) -> list[Path]:
    dist_dir.mkdir(parents=True, exist_ok=True)
    for stale in list(dist_dir.glob("*.whl")) + list(dist_dir.glob("*.tar.gz")):
        stale.unlink()  # 复用 dist/ 会校验/上传到历史 artifact
    log(f"构建 sdist + wheel（版本 {version}）→ {dist_dir}")
    # patched_version 退出时逐字节还原并自校验，失败会直接抛 ReleaseCheckFailure
    with patched_version(PYPROJECT, version):
        run([sys.executable, "-m", "build", "--sdist", "--wheel", "--outdir", str(dist_dir)])
    wheels = sorted(dist_dir.glob(f"{PROJECT_NAME}-{version}-*.whl"))
    sdists = sorted(dist_dir.glob(f"{PROJECT_NAME}-{version}.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise ReleaseCheckFailure(
            f"期望各一个 {version} 的 wheel/sdist，实际 wheel={[w.name for w in wheels]} sdist={[s.name for s in sdists]}"
        )
    artifacts = [*wheels, *sdists]
    log("twine check " + " ".join(a.name for a in artifacts))
    run([sys.executable, "-m", "twine", "check", *[str(a) for a in artifacts]])
    return artifacts


def upload(artifacts: list[Path]) -> None:
    log("上传到 TestPyPI（凭据取自系统钥匙串）")
    run([sys.executable, "-m", "twine", "upload", "--repository", "testpypi", "--non-interactive",
         *[str(a) for a in artifacts]], timeout=1800)


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass  # 非可重配置流保持原样：Windows 控制台默认 ANSI 代码页
    parser = argparse.ArgumentParser(description="TestPyPI 试发的版本预检与构建")
    parser.add_argument("--upload", action="store_true", help="构建后上传到 TestPyPI")
    parser.add_argument("--no-build", action="store_true", help="只报告下一个可用版本")
    parser.add_argument("--dist-dir", default=str(DIST_DIR), help="构建输出目录（默认 dist/）")
    args = parser.parse_args()

    dist_dir = Path(args.dist_dir).resolve()
    try:
        base = read_base_version()
        published = fetch_published_versions()
        version = next_dev_version(base, published)
        log(f"仓库版本 {base}；索引已发布 {len(published)} 个版本；下一个可用：{version}")
        if args.no_build:
            return 0
        artifacts = build_artifacts(version, dist_dir)
        if args.upload:
            upload(artifacts)
            log(f"已发布 {version}；端到端验证：python scripts/wheel_smoke.py --install-from testpypi --version {version}")
        else:
            log("未上传。确认无误后加 --upload，或手工："
                f"python -m twine upload --repository testpypi --non-interactive {' '.join(a.name for a in artifacts)}")
        return 0
    except ReleaseCheckFailure as error:
        print(f"[release-check] 失败: {error}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
