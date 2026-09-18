"""编号引用防回归门禁：禁止注释 / 文档 / 提交信息出现仓库外不可访问的编号指针。

规则与豁免见 scripts/forbidden_refs.py 模块 docstring。存量（审查报告条目号、
计划产物编号、流水号批次）已分批清理完毕，此处采用全库 **0 容忍**：任何命中
即失败。

为什么放在测试里而不是只靠钩子：钩子可被 --no-verify 绕过，CI 也只在 push 时跑；
纳入 pytest 后本地门禁与 CI 的三版本矩阵都会覆盖，且与 test_docs_anchors.py /
test_icon_manifest.py 同属「仓库不变量」测试。

注意：本文件的自证样例在运行期拼接编号（见 _ref），避免测试自身被规则命中。
"""

from pathlib import Path

import pytest

# 与 tests/test_fetch_icons_script.py 同口径：tests/ 为包，仓库根已在 sys.path 上
from scripts.forbidden_refs import (
    _added_lines,
    scan_commit_message,
    scan_lines,
    scan_source,
    scan_tree,
)

_ROOT = Path(__file__).resolve().parents[1]

# 增量扫描用例：diff 与文件内容是两路输入——diff 给行号，内容取自
# 「新增侧」版本。用仓库内已跟踪的文件名，配合打桩内容精确验证定位逻辑。
_DIFF_FILE = "scripts/secret_scan.py"

_DIFF_HEADER = f"""diff --git a/{_DIFF_FILE} b/{_DIFF_FILE}
--- a/{_DIFF_FILE}
+++ b/{_DIFF_FILE}
"""


def _ref(letter: str, number: int) -> str:
    """运行期拼接条目码：样例本身不能以字面量出现在源码里（否则被自身规则命中）。"""
    return f"{letter}{number}"


def test_diff_scan_ignores_unchanged_old_lines() -> None:
    """未改动的旧行不在新增行集合内：即使同文件别处有编号，也不得报出。"""
    source = "\n".join(
        [
            f"# 复核 {_ref('P', 1)}-1 示例",
            "# 本轮新增的无害注释",
        ]
    )
    assert scan_lines("tests/test_db.py", source, {2}) == []


def test_diff_scan_honours_diff_line_numbers() -> None:
    """命中行号取自 diff 的新增侧（行号与内容两路输入，各司其职）。"""
    source = "\n".join([f"# 复核 {_ref('P', 1)}-1 示例", "# 本轮新增的无害注释"])
    hits = scan_lines("tests/test_db.py", source, {1, 2})
    assert len(hits) == 1
    assert hits[0].line == 1


def test_diff_parser_maps_added_lines_to_new_file() -> None:
    """diff 解析：新增行的行号按新文件计数（本用例只测解析器本身）。"""
    diff = _DIFF_HEADER + "@@ -10,0 +12,2 @@\n+第一行\n+第二行\n"
    assert _added_lines(diff) == {_DIFF_FILE: {12, 13}}

def test_no_plan_references_anywhere() -> None:
    """全库注释 / 文档不得出现编号引用（审查报告条目号、计划产物编号、流水号批次）。"""
    hits = scan_tree(_ROOT)
    if hits:
        detail = "\n".join(f"  {hit.path}:{hit.line} [{hit.rule}] {hit.text[:120]}" for hit in hits)
        pytest.fail(
            "检测到编号引用（指向仓库外不可访问的文档，读者无法还原上下文）：\n"
            f"{detail}\n"
            "请改为说明「为什么」与失败模式，并把回归位置指向仓库内的测试或代码。"
        )


_CASES: list[tuple[str, bool]] = [
    # 规则 1：组合编号（中文方括号 + 序号 + 条目码）
    (f"# 【2·{_ref('P', 1)}】示例", True),
    # 规则 2：归因信号词 + 编号（含 1-2 字母码与裸数字）
    (f"# 复核 {_ref('PQ', 1)}-1：示例", True),
    ("# 审计 #12：示例", True),
    # 规则 3：条目码
    (f"# {_ref('XY', 3)} 说明", True),
    # 规则 4：单字母码 + 冒号（注释语境）
    (f"# {_ref('F', 5)}：示例", True),
    # 规则 5：计划 / 会话产物路径
    ("# 见 PLAN-XY.md", True),
    # 规则 6：流水号批次
    ("# 第一批改动", True),
    # 豁免：编码名 / RFC 编号 / 控制字符（规则文本本身不得自我命中）
    ("# 统一 UTF-8 编码，避免 U+FFFD", False),
    ("# 非 ASCII 基名走 RFC 5987 扩展", False),
    ("# 校验 sha256 摘要", False),
    ("# 去除 C0 控制字符", False),
    # 豁免：静态检查码（noqa）
    ("# noqa: F401 保留 re-export", False),
    ("# ruff: noqa: I001 — 导入顺序即组装顺序", False),
    # 豁免：分类 / 判重指标名（F 值）语境
    ("# accuracy / precision / recall / F1 + 混淆矩阵", False),
    ("# 精确率与召回率的调和平均 F1", False),
    # 豁免：可跟踪的 issue 编号（无归因信号词时不命中）
    ("# 关联 Fixes #123 的修复", False),
    # 豁免：行内显式标记
    (f"# {_ref('XY', 3)} allow-plan-ref 保留理由", False),
    # 归因词后接中文量词属于自然行文，不构成编号引用
    ("# 审计第三次发现该问题", False),
    # 普通注释
    ("# 关闭后拒绝重建连接，防止残余任务复活", False),
]


@pytest.mark.parametrize(("text", "expected"), _CASES)
def test_rule_matching(text: str, expected: bool) -> None:
    """规则与豁免的自证：样例必须命中，豁免样例必须放过。"""
    assert bool(scan_source("<sample>.py", text)) is expected


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (f"fix(ui): 修复浮层焦点栈与断线可见性（{_ref('P', 2)}-30 第一批）", True),
        ("fix(sources): 排期7b——翻页早停与 404 容错", True),
        ("fix(dedup): 嵌入截断防毒丸，缺失向量降级字符重叠", False),
        ("feat(ai): 支持通过 AI_DISABLE_THINKING 禁用思考模式", False),
    ],
)
def test_commit_message_matching(message: str, expected: bool) -> None:
    """提交信息（subject）同样禁止流水号与编号指针。"""
    assert bool(scan_commit_message(message)) is expected
