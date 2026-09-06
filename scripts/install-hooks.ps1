# 安装 briefdesk 预提交密钥扫描钩子（幂等，可重复执行）
# 用法: powershell -ExecutionPolicy Bypass -File scripts/install-hooks.ps1
$ErrorActionPreference = "Stop"

$root = git rev-parse --show-toplevel
if (-not $root) {
    Write-Error "当前目录不在 git 仓库内"
    exit 1
}

$hookDir = Join-Path $root ".git\hooks"
$hookPath = Join-Path $hookDir "pre-commit"
New-Item -ItemType Directory -Force -Path $hookDir | Out-Null

$hook = @'
#!/bin/sh
# briefdesk 预提交密钥扫描（由 scripts/install-hooks.ps1 安装，可重复安装覆盖）
# 用「执行性探测」而非 command -v：WindowsApps 的 python 别名能通过
# command -v，执行时却打印 "Python was not found" 并以非零退出——会让
# 所有提交被静默中止（实测缺陷，2026-08）。
pick_python() {
    if python -c "import sys" >/dev/null 2>&1; then echo python; return 0; fi
    if py -3 -c "import sys" >/dev/null 2>&1; then echo "py -3"; return 0; fi
    return 1
}
if py_cmd=$(pick_python); then
    exec $py_cmd scripts/secret_scan.py
else
    echo "secret-scan: 未找到可用的 python，跳过扫描（可安装 Python 或手动运行 scripts/secret_scan.py）" >&2
    exit 0
fi
'@

[System.IO.File]::WriteAllText($hookPath, $hook, (New-Object System.Text.UTF8Encoding $false))
Write-Host "已安装 pre-commit 钩子: $hookPath"