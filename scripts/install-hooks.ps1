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
if command -v python >/dev/null 2>&1; then
    exec python scripts/secret_scan.py
elif command -v py >/dev/null 2>&1; then
    exec py -3 scripts/secret_scan.py
else
    echo "secret-scan: 未找到 python，跳过扫描" >&2
    exit 0
fi
'@

[System.IO.File]::WriteAllText($hookPath, $hook, (New-Object System.Text.UTF8Encoding $false))
Write-Host "已安装 pre-commit 钩子: $hookPath"