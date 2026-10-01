"""子进程读取编码守卫：捕获型调用必须显式钉死 UTF-8 与替换策略。

为什么要专门守这一条：仓库约定「入口把 stdout/stderr 重配为 UTF-8」（benchmark
runner/cli、本仓库脚本等），而 `subprocess` 的 `text=True` 缺省按 **locale** 解码——
CI 的 Windows runner 是 cp1252，UTF-8 续字节（0x8D/0x8F 在 cp1252 里未定义）解不开。
解码在读取线程里进行，异常只被 `threading.excepthook` 打印、**不冒泡**：
`subprocess.run` 照常返回、`stdout` 静默变成 `None`，调用点要么
`AttributeError`、要么在宽松判断下静默放过。实测复现见本文件末尾的 run() 用例。
"""

import ast
import sys
from pathlib import Path

from scripts.runtime_manifest import REPO

#: 扫描面：生产代码与仓库脚本（tests/ 尚未收敛，见 docs/architecture.md 的入口编码约定）
_SCANNED_DIRS = ("briefdesk", "scripts")

#: 会返回「文本模式」结果的子进程入口
_SUBPROCESS_FUNCS = {"run", "Popen", "check_output", "call", "check_call"}


def _text_mode_calls_missing_encoding(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    offenders: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name not in _SUBPROCESS_FUNCS:
            continue
        kwargs = {keyword.arg for keyword in node.keywords}
        text_mode = any(
            keyword.arg in {"text", "universal_newlines"}
            and isinstance(keyword.value, ast.Constant)
            and keyword.value.value is True
            for keyword in node.keywords
        )
        if not text_mode:
            continue
        missing = [name for name in ("encoding", "errors") if name not in kwargs]
        if missing:
            offenders.append((node.lineno, ", ".join(missing)))
    return offenders


def test_captured_subprocess_calls_pin_utf8() -> None:
    failures: list[str] = []
    for top in _SCANNED_DIRS:
        for path in sorted((REPO / top).rglob("*.py")):
            for lineno, missing in _text_mode_calls_missing_encoding(path):
                rel = path.relative_to(REPO).as_posix()
                failures.append(f"{rel}:{lineno} 缺 {missing}")
    assert not failures, (
        "文本模式子进程调用必须显式 encoding=\"utf-8\" 与 errors=\"replace\""
        f"（否则读取线程静默死掉、stdout 变 None）: {failures}"
    )


def test_run_helpers_capture_utf8_child_output() -> None:
    """回归：子进程按仓库约定输出 UTF-8 中文时，run() 必须真的把输出读回来。

    修复前这两处在 cp1252 环境下会打印 "Exception in thread ... (_readerthread)"，
    却让调用方拿到 None——本用例直接断言内容，把沉默失败变成响亮失败。
    """
    from scripts.sdist_check import run as sdist_run
    from scripts.wheel_smoke import run as smoke_run

    snippet = "print('中文输出 ok')"
    for name, runner in (("wheel_smoke.run", smoke_run), ("sdist_check.run", sdist_run)):
        out = runner([sys.executable, "-c", snippet], timeout=120)
        assert "中文输出" in out, f"{name} 未按 UTF-8 读回子进程输出: {out!r}"
