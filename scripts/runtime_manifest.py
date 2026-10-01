"""发布资源登记表（单一事实来源）。

wheel 冒烟、sdist 检查与 pytest 静态断言都从这里取期望集合：三处各抄一份时，
「新增资源忘了登记」会以三种不同方式漏过门禁（wheel 少文件、sdist 多文件、静态
用例过时），收敛到一处后只改这里。

`REQUIRED` 只列**显式**资源；图标集合由 `briefdesk/ui/icon-manifest.txt` 派生
（新增图标只需跑 `scripts/fetch_icons.py add`，登记自动完成）。
"""

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


def expected_data_members() -> list[str]:
    """期望的数据成员集合（显式登记 + 图标清单），已排序去重。"""
    return sorted({*REQUIRED, *icon_manifest_entries()})
