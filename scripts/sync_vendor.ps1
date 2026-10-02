
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
    git fetch $t.Remote sdk-dist
    if ($LASTEXITCODE -ne 0) { throw "fetch failed for $($t.Remote)" }

    Write-Host "== subtree pull $($t.Prefix) =="
    $out = git subtree pull --prefix $t.Prefix "$($t.Remote)" sdk-dist --squash 2>&1
    $out | ForEach-Object { Write-Host $_ }
    if ($LASTEXITCODE -ne 0) { throw "subtree pull failed for $($t.Prefix)" }

    # The squash merge message carries "changes from X..Y"; if it is missing
    # the upstream history was rewritten or git changed its message format -
    # fail loudly rather than report a sync we cannot attribute.
    $msg = ($out | Where-Object { $_ -match "changes from \w+\.\.\w+" } | Select-Object -First 1)
    if (-not $msg) {
        throw "could not parse 'changes from X..Y' from subtree pull output for $($t.Prefix)"
    }
    Write-Host "recorded: $($t.Prefix) <- $msg"
}

Write-Host "== import smoke =="
$py = $env:PYTHON
if (-not $py) { $py = "python" }
& $py -c "import sys; sys.path.insert(0, 'vendor'); import weflow_sdk, qqflow_sdk; print('vendor import OK')"
if ($LASTEXITCODE -ne 0) { throw "vendor import smoke failed" }

Write-Host "sync complete. Update the 'sync log' section in vendor/README.md before committing."
