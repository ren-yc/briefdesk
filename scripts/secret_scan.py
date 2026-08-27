"""预提交密钥扫描 — 在 staged diff 中查找疑似密钥形态，命中即退出码 1。

与 AGENTS.md「隐私与敏感数据扫描」的手动命令等价，自动化到 pre-commit 钩子
（安装：scripts/install-hooks.ps1）。只扫描**新增行**（diff 中 `+` 前缀的行）：
删除行不进入仓库、不构成风险；也避免误伤测试桩（如已提交的 sk- 形态假值）。

命中规则（刻意保持窄，防误报）：
1. 常见密钥形态：sk- 开头 16+ 位（OpenAI 风格）、AKIA 16 位（AWS）、PEM 私钥块
2. 本项目密钥环境变量赋值且值非空（空值 = 模板/占位，如 .env.example 的
   `AI_API_KEY=` 不命中）
"""

import re
import subprocess
import sys

_KNOWN_SECRET_ENV = (
    "AI_API_KEY",
    "EMBED_API_KEY",
    "WEFLOW_LEGACY_API_TOKEN",
    "WEFLOW_API_TOKEN",
    "QQFLOW_API_TOKEN",
    "QQFLOW_KEY",
)

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"sk-[A-Za-z0-9]{16,}"), "OpenAI 风格密钥"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "AWS 访问密钥"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "PEM 私钥块"),
    (
        re.compile(
            r"^(?:export\s+)?(" + "|".join(_KNOWN_SECRET_ENV) + r")\s*=\s*\S+"
        ),
        "简报台密钥环境变量赋值（值非空）",
    ),
]


def scan_text(diff_text: str) -> list[tuple[int, str, str]]:
    """扫描增量文本（仅新增行），返回 [(行号, 类别, 命中片段), ...]。

    行号相对 diff 文本；新增行以 `+` 开头（`+++` 文件头行除外）。
    """
    hits: list[tuple[int, str, str]] = []
    for offset, line in enumerate(diff_text.splitlines()):
        if not line.startswith("+") or line.startswith("+++"):
            continue
        content = line[1:]
        for pattern, label in _PATTERNS:
            match = pattern.search(content)
            if match is not None:
                hits.append((offset + 1, label, match.group(0)[:80]))
                break  # 每行只报一次
    return hits


def _staged_added_diff() -> str:
    proc = subprocess.run(
        ["git", "diff", "--cached", "-U0"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode != 0:
        print(
            f"[secret-scan] git diff 执行失败（exit {proc.returncode}），跳过扫描",
            file=sys.stderr,
        )
        return ""
    return proc.stdout


def main() -> int:
    diff = _staged_added_diff()
    hits = scan_text(diff)
    if hits:
        print("检测到疑似密钥/敏感信息（staged 新增内容），请先移除或脱敏后再提交：")
        for line, label, snippet in hits:
            print(f"  - 第 {line} 行 [{label}]: {snippet}")
        print(
            "\n确认为误报时，请人工复核后使用 `git commit --no-verify` 跳过（谨慎）。"
        )
        return 1
    print("staged 新增内容未发现疑似密钥")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())