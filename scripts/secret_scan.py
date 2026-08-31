"""预提交密钥扫描 — 在 staged diff 中查找疑似密钥形态，命中即退出码 1。

与 AGENTS.md「隐私与敏感数据扫描」的手动命令等价，自动化到 pre-commit 钩子
（安装：scripts/install-hooks.ps1）。只扫描**新增行**（diff 中 `+` 前缀的行）：
删除行不进入仓库、不构成风险；也避免误伤测试桩（如已提交的 sk- 形态假值）。

命中规则（刻意保持窄，防误报）：
1. 常见密钥形态：sk- 开头 16+ 位（OpenAI 风格）、AKIA 16 位（AWS）、PEM 私钥块
2. 本项目密钥环境变量赋值且值非空（空值 = 模板/占位，如 .env.example 的
   `AI_API_KEY=` 不命中）
"""

import argparse
import re
import subprocess
import sys

# CI Windows runner 默认 stdout 编码为 cp1252，中文输出会 UnicodeEncodeError；
# 尽早切到 utf-8（失败则静默忽略，由 errors=replace 的 fallback 兜底）
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001, S110
        pass

_KNOWN_SECRET_ENV = (
    "AI_API_KEY",
    "EMBED_API_KEY",
    "WEFLOW_API_TOKEN",
    "WEFLOW_IMG_AES_KEY",
    "WEFLOW_IMG_XOR_KEY",
    "WEFLOW_DB_KEYS",
    "WEFLOW_DB_KEYS_2",
    "WEFLOW_LEGACY_API_TOKEN",
    "QQFLOW_API_TOKEN",
    "QQFLOW_KEY",
    "RAG_API_KEY",
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
    return _git_diff(["--cached"])


def _git_diff(rev_args: list[str]) -> str:
    proc = subprocess.run(
        ["git", "diff", *rev_args, "-U0"],
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="预提交密钥扫描")
    parser.add_argument(
        "--ref",
        default=None,
        help="扫描相对该基线的差异（如 origin/master…HEAD，供 CI 使用）；"
        "缺省扫描 staged 新增内容（pre-commit 钩子路径）",
    )
    args = parser.parse_args(argv)

    diff = _git_diff([f"{args.ref}...HEAD"]) if args.ref else _staged_added_diff()
    hits = scan_text(diff)
    if hits:
        target = f"相对 {args.ref} 的差异" if args.ref else "staged 新增内容"
        print(f"检测到疑似密钥/敏感信息（{target}），请先移除或脱敏后再提交：")
        for line, label, snippet in hits:
            print(f"  - 第 {line} 行 [{label}]: {snippet}")
        print(
            "\n确认为误报时，请人工复核后使用 `git commit --no-verify` 跳过（谨慎）。"
        )
        return 1
    print("未发现疑似密钥")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())