"""发布资源登记表（单一事实来源）。

wheel 冒烟、sdist 检查与 pytest 静态断言都从这里取期望集合：三处各抄一份时，
「新增资源忘了登记」会以三种不同方式漏过门禁（wheel 少文件、sdist 多文件、静态
用例过时），收敛到一处后只改这里。

`REQUIRED` 只列**显式**资源；图标集合由 `briefdesk/ui/icon-manifest.txt` 派生
（新增图标只需跑 `scripts/fetch_icons.py add`，登记自动完成）。
"""

import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

#: 必须随包分发的运行时资源（相对仓库根）：核心 SPA 3 + 插件前端 8 + 示例用例 4
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

#: 明确**不**进分发包的资源：源码维护文件与上游镜像文档（运行期从不读取）
EXCLUDED = [
    "briefdesk/ui/icon-manifest.txt",
    "briefdesk/ui/icons/README.md",
    "briefdesk/plugins/weflow_legacy/weflow-legacy-api.md",
]


def icon_manifest_entries() -> list[str]:
    """图标清单派生的资源路径（与 tests/test_icon_manifest.py 同一份清单）。"""
    manifest = (REPO / "briefdesk" / "ui" / "icon-manifest.txt").read_text(encoding="utf-8")
    return sorted(
        f"briefdesk/ui/icons/{Path(line.strip()).stem}.svg"
        for line in manifest.splitlines()
        if line.strip() and not line.strip().startswith("#")
    )


#: 随包分发的两个上游 SDK（git subtree 镜像，经 packages.find 的第二个 where 根进包）
VENDOR_PACKAGES = ("weflow_sdk", "qqflow_sdk")


def vendor_expected_members(prefix: str = "") -> list[str]:
    """vendor 两个 SDK 的**全部**文件：随包分发的存在性判据。

    为什么整棵树都不进数据成员清单：wheel 门禁把顶层 ``weflow_sdk/**`` 与
    ``qqflow_sdk/**`` 一律当作数据成员参与集合比对，只登记 spec 快照与 py.typed
    会让 105 个模块文件全部落进「多出」；而若不单独断言存在性，「声明了却没打进
    包」又没人管——两边一起漏正是互比型断言抓不到的形态。故此处只负责存在性，
    集合比对里 vendor 整棵树按非数据成员处理。

    路径来源是 ``git ls-files``：镜像以「git 跟踪的文件」为准，磁盘上的编辑器
    备份与调试残留不该被要求打进包。取不到 git 时退回遍历包目录（排除
    ``__pycache__``）。``vendor/README.md`` 是本仓的镜像说明，不属于上游 SDK，
    不在此列。

    ``prefix`` 切换两类拼写：空串＝wheel 顶层包路径，``vendor/``＝sdist 工作区路径。
    """
    rel: list[str] = []
    try:
        completed = subprocess.run(
            ["git", "ls-files", *[f"vendor/{pkg}" for pkg in VENDOR_PACKAGES]],
            cwd=REPO,
            capture_output=True,
            text=True,
            check=True,
        )
        rel = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    except (OSError, subprocess.CalledProcessError):
        rel = [
            path.relative_to(REPO).as_posix()
            for pkg in VENDOR_PACKAGES
            for path in sorted((REPO / "vendor" / pkg).rglob("*"))
            if path.is_file() and "__pycache__" not in path.parts
        ]
    members = [path.removeprefix("vendor/") for path in rel]
    return sorted(prefix + member for member in members)


def expected_data_members() -> list[str]:
    """期望的数据成员集合（显式登记 + 图标清单），已排序去重。

    vendor 的镜像文件**不在**此列：它们由 :func:`vendor_expected_members` 单独做
    存在性断言——整棵树进集合比对会与 wheel 门禁的口径冲突（详见该函数说明）。
    """
    return sorted({*REQUIRED, *icon_manifest_entries()})
