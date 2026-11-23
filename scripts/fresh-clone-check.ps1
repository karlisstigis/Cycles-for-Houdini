# SPDX-License-Identifier: Apache-2.0
# Proves that a fresh clone builds: clones one commit of this repository into an empty folder,
# runs the clone's own bootstrap.ps1 (default engine route) and build.ps1, renders a small scene
# on CPU and on OptiX with existing tests, and writes result.json. Changes nothing outside
# -Destination (and -ResultFile, if given). Houdini runs with preferences inside -Destination.
#
#   scripts/fresh-clone-check.ps1 [-Destination <empty folder>] [-Revision <commit>] [-ResultFile <path>]
# Default -Destination: a new folder in <project>\_work\tests (everything stays inside the project folder).
[CmdletBinding()]
param(
    [string]$Destination = '',
    [string]$Source = (Split-Path -Parent $PSScriptRoot),
    [string]$Revision = 'HEAD',
    [string]$ResultFile = '',
    [int]$Jobs = 6,
    [switch]$SkipOptix
)
$ErrorActionPreference = 'Stop'
$pwsh = (Get-Process -Id $PID).Path
$Source = [IO.Path]::GetFullPath($Source).TrimEnd('\','/')
# Test folders live inside the project folder, under _work\tests (ignored by Git and never exported); by default a
# new one named after the time. Anywhere else inside the source repository is refused.
if (-not $Destination) { $Destination = Join-Path $Source ('_work\tests\fresh-' + [DateTime]::UtcNow.ToString('yyyyMMddTHHmmss')) }
$Destination = [IO.Path]::GetFullPath($Destination).TrimEnd('\', '/')
$testsRoot = Join-Path $Source '_work\tests'
if (($Destination + '\').StartsWith($Source + '\', [StringComparison]::OrdinalIgnoreCase) -and -not ($Destination + '\').StartsWith($testsRoot + '\', [StringComparison]::OrdinalIgnoreCase)) { throw "Destination must be outside the source repository, or inside its _work\tests folder ($testsRoot)." }
if (@($Destination -split '[\\/]') -contains 'artifacts') { throw 'Destination must not be inside any artifacts folder.' }
if ((Test-Path -LiteralPath $Destination) -and @(Get-ChildItem -LiteralPath $Destination -Force).Count) { throw "Destination must be empty: $Destination" }
# Free space on the destination drive: this check measured 2.31 GB in one run; 2 GB spare.
$freeGB = [math]::Round((New-Object IO.DriveInfo([IO.Path]::GetPathRoot($Destination))).AvailableFreeSpace / 1GB, 1)
if ($freeGB -lt 4.5) { throw "Not enough free disk space for this check: $([IO.Path]::GetPathRoot($Destination)) has $freeGB GB free, and it needs about 4.5 GB." }
$src = Join-Path $Destination 'src'
$build = Join-Path $Destination 'build'
$prefs = Join-Path $Destination 'prefs\houdini__HVER__'
$out = Join-Path $Destination 'results'
New-Item -ItemType Directory -Path $out -Force | Out-Null
$result = [ordered]@{
    result = 'running'; started_utc = [DateTime]::UtcNow.ToString('o'); destination = $Destination
    source = $Source; revision = $null; engine_route = $null; git_autocrlf = $null; steps = [ordered]@{}
}
function Save-Result {
    $text = $result | ConvertTo-Json -Depth 6
    Set-Content -LiteralPath (Join-Path $Destination 'result.json') -Value $text -Encoding utf8
    if ($ResultFile) { Set-Content -LiteralPath $ResultFile -Value $text -Encoding utf8 }
}
function Invoke-Step([string]$Name, [scriptblock]$Body) {
    $watch = [Diagnostics.Stopwatch]::StartNew()
    Write-Host "=== $Name"
    $record = [ordered]@{ ok = $false; seconds = 0; error = $null }
    $result.steps[$Name] = $record
    try { & $Body; $record.ok = $true }
    catch { $record.error = $_.Exception.Message; throw }
    finally { $record.seconds = [math]::Round($watch.Elapsed.TotalSeconds, 1); Save-Result }
}
function Invoke-Script([string]$Script, [string[]]$Arguments) {
    # Each step runs in its own PowerShell, from the clone, as a developer would run it.
    & $pwsh -NoProfile -ExecutionPolicy Bypass -File $Script @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$(Split-Path -Leaf $Script) failed ($LASTEXITCODE)" }
}
function Get-FolderGiB([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return 0 }
    [math]::Round(((Get-ChildItem -LiteralPath $Path -Recurse -File -Force -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum) / 1GB, 2)
}
try {
    Invoke-Step 'clone' {
        $sha = (& git -C $Source rev-parse --verify "$Revision^{commit}").Trim()
        if ($LASTEXITCODE -ne 0) { throw "Unknown revision $Revision" }
        $result.revision = $sha
        & git init -q $src; if ($LASTEXITCODE) { throw 'git init failed' }
        $result.git_autocrlf = (& git -C $src config --get core.autocrlf)
        # One commit only, like a fresh public repository; no ignored folders come along.
        & git -C $src fetch -q --depth 1 ('file:///' + $Source.Replace('\','/')) $sha; if ($LASTEXITCODE) { throw 'git fetch failed' }
        & git -C $src checkout -q --detach FETCH_HEAD; if ($LASTEXITCODE) { throw 'git checkout failed' }
        foreach ($name in 'artifacts', 'third_party\cycles') {
            if (Test-Path -LiteralPath (Join-Path $src $name)) { throw "The fresh clone already contains $name" }
        }
    }
    Invoke-Step 'bootstrap' {
        Push-Location -LiteralPath $src
        try { Invoke-Script (Join-Path $src 'scripts\bootstrap.ps1') @() } finally { Pop-Location }
        $result.engine_route = (& python (Join-Path $src 'scripts\engine-fork.py') route --source-root $src).Trim()
        if ($result.engine_route -ne 'public') { throw "Expected the public engine route, got $($result.engine_route)" }
    }
    Invoke-Step 'build' {
        Push-Location -LiteralPath $src
        try { Invoke-Script (Join-Path $src 'scripts\build.ps1') @('-BuildDirectory', $build, '-UserPrefDirectory', $prefs, '-Jobs', [string]$Jobs) } finally { Pop-Location }
    }
    $devices = @('CPU'); if (-not $SkipOptix) { $devices += 'OPTIX' }
    foreach ($device in $devices) {
        Invoke-Step "render-$device" {
            Push-Location -LiteralPath $src
            try {
                # A red box, 32x24 pixels, rendered in-process and in the worker on one device.
                Invoke-Script (Join-Path $src 'scripts\run-hython.ps1') @('-BuildDirectory', $build, '-UserPrefDirectory', $prefs,
                    (Join-Path $src 'tests\hython_scene_contract.py'), '--device', $device, '--report', (Join-Path $out "scene-$device.json"))
            } finally { Pop-Location }
            $report = Get-Content -LiteralPath (Join-Path $out "scene-$device.json") -Raw | ConvertFrom-Json
            if ($report.result -ne 'passed') { throw "Scene test on $device did not pass" }
            $result.steps["render-$device"].device = $report.device
        }
    }
    $result.disk_gib = [ordered]@{ src = (Get-FolderGiB $src); build = (Get-FolderGiB $build); total = (Get-FolderGiB $Destination) }
    $result.result = 'passed'
} catch {
    $result.result = 'failed'
    $result.error = $_.Exception.Message
} finally {
    $result.finished_utc = [DateTime]::UtcNow.ToString('o')
    Save-Result
    Get-Content -LiteralPath (Join-Path $Destination 'result.json')
}
if ($result.result -ne 'passed') { exit 1 }
