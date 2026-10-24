
# 同步 vendor/ 下的两个上游 SDK 镜像（git subtree --squash）。
# 用法：powershell -File scripts\sync_vendor.ps1
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

$targets = @(
    @{ Prefix = "vendor/weflow_sdk"; Remote = "weflow-upstream" },
    @{ Prefix = "vendor/qqflow_sdk"; Remote = "qqflow-upstream" }
)

foreach ($t in $targets) {
    Write-Host "== fetching $($t.Remote) sdk-dist =="
    # 原生命令把进度写到 stderr；在 $ErrorActionPreference = "Stop" 下，被 2>&1
    # 合并回来的 stderr 会被当成终止性错误（NativeCommandError），正常同步也会中断。
    # 这里显式降级为 Continue，并只用退出码判失败。
    $ErrorActionPreference = "Continue"
    git fetch $t.Remote sdk-dist 2>&1 | ForEach-Object { Write-Host $_ }
    $code = $LASTEXITCODE
    $ErrorActionPreference = "Stop"
    if ($code -ne 0) { throw "fetch failed for $($t.Remote)" }

    $before = (git rev-parse HEAD).Trim()

    Write-Host "== subtree pull $($t.Prefix) =="
    $ErrorActionPreference = "Continue"
    $out = git subtree pull --prefix $t.Prefix "$($t.Remote)" sdk-dist --squash 2>&1
    $code = $LASTEXITCODE
    $ErrorActionPreference = "Stop"
    $out | ForEach-Object { Write-Host $_ }
    if ($code -ne 0) { throw "subtree pull failed for $($t.Prefix)" }

    $after = (git rev-parse HEAD).Trim()
    if ($after -eq $before) {
        Write-Host "recorded: $($t.Prefix) already at commit (HEAD unchanged)"
        continue
    }

    # squash 标记在 merge 提交的**第二父**里：merge 提交自己的标题只有
    # "Merge commit <sha> as <prefix>"，从 HEAD 正文里找 "changes from X..Y"
    # 永远找不到——正常同步也会被判成解析失败。解析不到时把两方正文都打出来，
    # 便于 git-subtree 改文案时定位，而不是静默记一条无法归属的同步。
    $squash = ""
    git rev-parse --verify -q HEAD^2 | Out-Null
    if ($LASTEXITCODE -eq 0) {
        $squash = (git show -s --format=%B HEAD^2) -join "`n"
    }
    $msg = ($squash -split "`n" | Where-Object { $_ -match "(changes from|content from) [0-9a-f]{7,}" } | Select-Object -First 1)
    if (-not $msg) {
        Write-Host "--- HEAD body ---"; git show -s --format=%B HEAD
        Write-Host "--- HEAD^2 body ---"; Write-Host $squash
        throw "could not parse the squash marker (changes from X..Y / content from <sha>) for $($t.Prefix)"
    }
    $short = (git rev-parse --short HEAD).Trim()
    Write-Host "recorded: $($t.Prefix) <- $msg"
    Write-Host "          sync log line for vendor/README.md: $short | $($t.Prefix) | $msg"
}

Write-Host "== import smoke =="
$py = $env:PYTHON
if (-not $py) { $py = "python" }
& $py -c "import sys; sys.path.insert(0, 'vendor'); import weflow_sdk, qqflow_sdk; print('vendor import OK')"
if ($LASTEXITCODE -ne 0) { throw "vendor import smoke failed" }

Write-Host "sync complete. Update the sync log section in vendor/README.md before committing."
