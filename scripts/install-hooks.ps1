# 安装 briefdesk 本地 git 钩子（幂等，可重复执行）
#   1) pre-commit：密钥/敏感信息扫描 + 编号引用扫描（仅 staged 新增内容）
#   2) commit-msg：提交信息里的编号引用与流水号批次检查
# 用法: powershell -ExecutionPolicy Bypass -File scripts/install-hooks.ps1
$ErrorActionPreference = "Stop"

$root = git rev-parse --show-toplevel
if (-not $root) {
    Write-Error "当前目录不在 git 仓库内"
    exit 1
}

$hookDir = Join-Path $root ".git\hooks"
New-Item -ItemType Directory -Force -Path $hookDir | Out-Null

# 共用片段：用「执行性探测」而非 command -v —— WindowsApps 的 python 别名能通过
# command -v，执行时却打印 "Python was not found" 并以非零退出，会让所有提交被静默中止。
$pickPython = @'
pick_python() {
    if python -c "import sys" >/dev/null 2>&1; then echo python; return 0; fi
    if py -3 -c "import sys" >/dev/null 2>&1; then echo "py -3"; return 0; fi
    return 1
}
'@

$preCommit = @'
#!/bin/sh
# briefdesk 预提交检查（由 scripts/install-hooks.ps1 安装，可重复安装覆盖）：
#   1) 密钥/敏感信息扫描：scripts/secret_scan.py
#   2) 编号引用扫描：scripts/forbidden_refs.py（见 AGENTS.md「注释、文档与提交信息规范」）
__PICK_PYTHON__
if py_cmd=$(pick_python); then
    $py_cmd scripts/secret_scan.py || exit 1
    exec $py_cmd scripts/forbidden_refs.py
else
    echo "pre-commit: 未找到可用的 python，跳过扫描（可安装 Python 或手动运行 scripts/ 下的脚本）" >&2
    exit 0
fi
'@

$commitMsg = @'
#!/bin/sh
# briefdesk 提交信息检查（由 scripts/install-hooks.ps1 安装，可重复安装覆盖）：
# 提交信息（subject 与 body）不得夹带编号引用与流水号批次；
# 用「行为变化」描述取代指向仓库外报告的条目号。
__PICK_PYTHON__
if py_cmd=$(pick_python); then
    exec $py_cmd scripts/forbidden_refs.py --message-file "$1"
else
    echo "commit-msg: 未找到可用的 python，跳过检查（可安装 Python 或手动运行 scripts/forbidden_refs.py）" >&2
    exit 0
fi
'@

$preCommit = $preCommit.Replace('__PICK_PYTHON__', $pickPython.TrimEnd("`r", "`n"))
$commitMsg = $commitMsg.Replace('__PICK_PYTHON__', $pickPython.TrimEnd("`r", "`n"))

$utf8NoBom = New-Object System.Text.UTF8Encoding $false
$preCommitPath = Join-Path $hookDir "pre-commit"
$commitMsgPath = Join-Path $hookDir "commit-msg"
[System.IO.File]::WriteAllText($preCommitPath, $preCommit, $utf8NoBom)
[System.IO.File]::WriteAllText($commitMsgPath, $commitMsg, $utf8NoBom)
Write-Host "已安装 pre-commit 钩子: $preCommitPath"
Write-Host "已安装 commit-msg 钩子: $commitMsgPath"
