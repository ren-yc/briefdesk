"""文档锚点静态守卫。

docs/ 与 README 的 TOC/交叉引用使用 `](#anchor)` 锚点；标题含标点
（如 `` `/` `+` `（``）时锚点由 GitHub 规则生成，手写极易漂移——
改写模块表时就曾产生两条断链（`+ ` 两侧空格各生成一个连字符，
即 slug 中出现 `--`）。本测试按 GitHub 规则复算每个标题的锚点，
断言仓库内文档不存在悬空链接。

规则实现已与官方 `github-slugger` 对 `architecture.md` 全部标题对拍，
0 处不一致（小写 → 去标点 → 空格转连字符；重名标题追加 -N）。
"""

import re
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]

# 参与检查的文档（相对仓库根）；含插件自带的 API 文档（有 2 条锚点链接）
_DOCS = (
    "docs/architecture.md",
    "docs/plugin-dev.md",
    "briefdesk/plugins/weflow/weflow-server-api.md",
    "README.md",
    "USAGE.md",
)

_FENCE_RE = re.compile(r"^\s*```")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_LINK_RE = re.compile(r"\]\(#([^)]+)\)")


def _slug(text: str) -> str:
    """GitHub 锚点规则：小写 → 去标点（保留下划线/连字符/汉字）→ 空格转连字符。"""
    t = text.strip().lower()
    t = re.sub(r"[^\w \-]", "", t, flags=re.UNICODE)
    return t.replace(" ", "-")


def _anchors(text: str) -> set[str]:
    """收集全部标题锚点（跳过 ``` 围栏内的伪标题；重名按 GitHub 追加 -N）。"""
    seen: dict[str, int] = {}
    anchors: set[str] = set()
    in_fence = False
    for line in text.split("\n"):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = _HEADING_RE.match(line)
        if not m:
            continue
        base = _slug(m.group(2))
        n = seen.get(base, 0)
        seen[base] = n + 1
        anchors.add(base if n == 0 else f"{base}-{n}")
    return anchors


def _dangling(text: str) -> list[str]:
    anchors = _anchors(text)
    return sorted({a for a in _LINK_RE.findall(text) if a not in anchors})


class DocsAnchorGuardTest(unittest.TestCase):
    """文档内 `](#anchor)` 链接必须命中实际标题锚点。"""

    def test_no_dangling_anchors(self):
        problems: list[str] = []
        for rel in _DOCS:
            path = _ROOT / rel
            if not path.exists():
                continue
            bad = _dangling(path.read_text(encoding="utf-8"))
            if bad:
                problems.append(f"{rel}: {bad}")
        self.assertEqual(problems, [], "存在悬空文档锚点")

    def test_architecture_toc_covers_top_level_sections(self):
        """architecture.md 头部 TOC 的每条锚点都可跳转。"""
        text = (_ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")
        anchors = _anchors(text)
        # TOC 位于文件头部：截取到第一个 `## 定位` 之前的链接。
        # 先断言边界存在——否则 split 会退化成整篇文本，断言仍能通过而守卫静默失效。
        marker = "\n## 定位"
        self.assertIn(marker, text, "顶层章节「定位」缺失，TOC 边界判据失效")
        head = text.split(marker, 1)[0]
        toc = _LINK_RE.findall(head)
        self.assertGreaterEqual(len(toc), 8, "TOC 应列出 8 个顶层章节")
        for target in toc:
            self.assertIn(target, anchors, f"TOC 锚点无法跳转: #{target}")

    def test_module_table_anchors_resolve(self):
        """核心模块表的「详见下文」锚点必须命中同名详解小节。"""
        text = (_ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")
        anchors = _anchors(text)
        # 同上：边界缺失会让取值退化为整篇文本，守卫静默失效
        marker = "### 核心模块详解"
        self.assertIn(marker, text, "「核心模块详解」小节缺失，模块表边界判据失效")
        table = text.split(marker, 1)[0]
        links = [a for a in _LINK_RE.findall(table) if a.startswith("briefdesk")]
        self.assertGreaterEqual(len(links), 20, "模块表应含大量跳转")
        missing = sorted({a for a in links if a not in anchors})
        self.assertEqual(missing, [], "模块表存在悬空锚点")


if __name__ == "__main__":
    unittest.main()
