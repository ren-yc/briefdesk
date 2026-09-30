"""发布资源静态一致性：package-data 声明与实际文件必须对得上。

三道断言，各自对应一种「装完 wheel 才发现」的失败：

- 声明的模式必须至少命中一个真实文件——目录搬走了而声明没改时，构建不会报错，
  只有装完发现页面 404；
- 运行时资源必须被声明覆盖——漏声明等于资源不进 wheel（核心 SPA、插件前端、
  示例用例、图标）；
- 明确排除的资源不得被任何模式命中——图标清单/说明是源码维护资源，上游镜像文档
  运行期不读，用户导出的 *.fromweb.json 含真实聊天内容，都不能进包。
"""

import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_PYPROJECT = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
PACKAGE_DATA: dict[str, list[str]] = _PYPROJECT["tool"]["setuptools"]["package-data"]

#: 必须随 wheel 分发的运行时资源（相对仓库根；图标由清单动态校验，见下方用例）
REQUIRED = [
    "briefdesk/ui/index.html",
    "briefdesk/ui/app.js",
    "briefdesk/ui/style.css",
    "briefdesk/plugins/calendar/ui/ui.js",
    "briefdesk/plugins/calendar/ui/ui.css",
    "briefdesk/plugins/reminders/ui/ui.js",
    "briefdesk/plugins/reminders/ui/ui.css",
    "briefdesk/plugins/rag/ui/ui.js",
    "briefdesk/plugins/rag/ui/ui.css",
    "briefdesk/plugins/benchmark/ui/ui.js",
    "briefdesk/plugins/benchmark/ui/ui.css",
    "briefdesk/plugins/benchmark/cases/classify.example.json",
    "briefdesk/plugins/benchmark/cases/dedup.example.json",
    "briefdesk/plugins/benchmark/cases/merge.example.json",
    "briefdesk/plugins/benchmark/cases/title.example.json",
]

#: 明确不进 wheel 的资源（源码维护文件 / 上游镜像文档 / 用户数据）
EXCLUDED = [
    "briefdesk/ui/icon-manifest.txt",
    "briefdesk/ui/icons/README.md",
    "briefdesk/plugins/weflow_legacy/weflow-legacy-api.md",
]


def _expand(package: str, pattern: str) -> list[Path]:
    """按 setuptools 的口径展开某个包的数据模式（* 不跨目录分隔符）。

    包名到目录：本仓库的目录布局与包名一一对应（a.b → a/b）。
    """
    base = REPO.joinpath(*package.split("."))
    return sorted(p for p in base.glob(pattern) if p.is_file())


def _declared_files() -> dict[str, Path]:
    """声明模式命中的全部文件：相对仓库根路径 → 绝对路径。"""
    matched: dict[str, Path] = {}
    for package, patterns in PACKAGE_DATA.items():
        for pattern in patterns:
            for file in _expand(package, pattern):
                matched[file.relative_to(REPO).as_posix()] = file
    return matched


def test_declared_patterns_match_real_files() -> None:
    for package, patterns in PACKAGE_DATA.items():
        base = REPO.joinpath(*package.split("."))
        assert base.is_dir(), f"声明的包目录不存在（目录搬走了？）: {package}"
        for pattern in patterns:
            assert _expand(package, pattern), f"{package} 的 {pattern!r} 未命中任何文件"


def test_required_runtime_resources_are_declared() -> None:
    matched = _declared_files()
    missing = [item for item in REQUIRED if item not in matched]
    assert not missing, f"运行时资源未被 package-data 覆盖（wheel 里会缺）: {missing}"


def test_icon_set_matches_manifest() -> None:
    """图标以清单为单一事实来源：声明覆盖的文件集合必须与清单一一对应。"""
    matched = _declared_files()
    icons = sorted(name for name in matched if name.startswith("briefdesk/ui/icons/"))
    manifest = (REPO / "briefdesk" / "ui" / "icon-manifest.txt").read_text(encoding="utf-8")
    expected = sorted(
        f"briefdesk/ui/icons/{Path(line.strip()).stem}.svg"
        for line in manifest.splitlines()
        if line.strip() and not line.strip().startswith("#")
    )
    assert icons == expected
    assert icons, "图标集合不应为空"


def test_excluded_resources_are_not_matched() -> None:
    matched = _declared_files()
    for item in EXCLUDED:
        assert item not in matched, f"该资源不应进 wheel: {item}"
    leaked = [name for name in matched if name.endswith(".fromweb.json")]
    assert not leaked, f"用户导出的用例（含真实聊天内容）不得进 wheel: {leaked}"
