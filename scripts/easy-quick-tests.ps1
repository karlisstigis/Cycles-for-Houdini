# SPDX-License-Identifier: Apache-2.0
# Quick tests (menu item "Run the quick tests"): renders a small test scene (a red box) with
# your build, on the CPU and, when an NVIDIA GPU with a new enough driver is present, with OptiX. Each render runs inside Houdini's own
# process and in the separate render worker, and the picture is checked. Takes about a minute.
# Writes reports under <build>\quick-tests\<time>\ and, with -ResultFile, a summary JSON.
[CmdletBinding()]
param([string]$BuildDirectory = '', [string]$ResultFile = '', [switch]$SkipOptix, [switch]$Nested)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'easy-common.ps1')
trap { Write-EasyCrash $_; exit 1 }
Write-EasyTitle "Quick tests (Houdini $(Get-EasyHoudiniVersion))"
[void](Find-EasyPython); [void](Find-EasyGit)
$build = if ($BuildDirectory) { [IO.Path]::GetFullPath($BuildDirectory) } else { Get-EasyBuildDirectory }
$manifest = Join-Path $build 'build-manifest.json'
if (-not (Test-Path -LiteralPath $manifest)) {
    Write-EasyResult $false "There is no finished build in $build yet." ((Get-EasyAgain) + ' (its first run builds).')
    exit 1
}
$out = Join-Path $build ('quick-tests\' + [DateTime]::UtcNow.ToString('yyyyMMddTHHmmss'))
New-Item -ItemType Directory -Path $out -Force | Out-Null
$devices = @('CPU')
# The driver is only read (scripts/easy-common.ps1, Get-EasyDriverSupport): OptiX 9.1 needs R590+.
$support = Get-EasyDriverSupport
$skipped = @()
$builtFeatures = (Get-Content -LiteralPath $manifest -Raw | ConvertFrom-Json).features
$hasOptix = ([string]$builtFeatures.WITH_CYCLES_DEVICE_OPTIX).ToUpperInvariant() -in @('ON', 'TRUE', '1')
$target = Get-EasyHoudiniTarget
$skipReason = if ($SkipOptix) { 'skipped on request' } elseif (-not $hasOptix) { 'this is a CPU-only build (no CUDA compiler when it was built)' } elseif (-not $support.Driver) { 'no NVIDIA GPU found' } elseif (-not $support.optix) { "NVIDIA driver $($support.Driver.Version) is older than $($script:EasyDriverMinimum.optix.Major), which OptiX 9.1 needs; update it to render on the GPU ($script:EasyDriverPage)" } else { '' }
if (-not $skipReason) { $devices += 'OPTIX' } else { $skipped += 'OPTIX' }
$summary = [ordered]@{ build_manifest_sha256 = (Get-FileHash -LiteralPath $manifest -Algorithm SHA256).Hash.ToLowerInvariant(); started_utc = [DateTime]::UtcNow.ToString('o'); checks = @() }
$allOk = $true
foreach ($device in $devices) {
    Write-EasyStep "Render the test scene on $device"
    $report = Join-Path $out "scene-$device.json"
    $ok = Invoke-EasyScript 'run-hython.ps1' @('-HoudiniRoot', $target.Root, '-BuildDirectory', $build, '-UserPrefDirectory', (Join-Path $out 'prefs\houdini__HVER__'),
        (Join-Path $script:EasyRoot 'tests\hython_scene_contract.py'), '--device', $device, '--report', $report)
    if ($ok -and (Test-Path -LiteralPath $report)) { $ok = ((Get-Content -LiteralPath $report -Raw | ConvertFrom-Json).result -eq 'passed') } else { $ok = $false }
    $summary.checks += @([ordered]@{ name = "scene-render-$($device.ToLowerInvariant())"; test = 'tests/hython_scene_contract.py'; device = $device; result = $(if ($ok) { 'passed' } else { 'failed' }); report = $report })
    if ($ok) { Write-EasyOk "$device render passed" } else { Write-EasyMissing "$device render FAILED (details in $report and above)"; $allOk = $false }
}
foreach ($device in $skipped) {
    $summary.checks += @([ordered]@{ name = "scene-render-$($device.ToLowerInvariant())"; test = 'tests/hython_scene_contract.py'; device = $device; result = 'skipped'; reason = $skipReason })
    Write-EasyInfo "$device render skipped: $skipReason."
}
$summary.result = if ($allOk) { 'passed' } else { 'failed' }
$summary.finished_utc = [DateTime]::UtcNow.ToString('o')
$text = $summary | ConvertTo-Json -Depth 6
Set-Content -LiteralPath (Join-Path $out 'summary.json') -Value $text -Encoding utf8
if ($ResultFile) { Set-Content -LiteralPath $ResultFile -Value $text -Encoding utf8 }
if ($Nested) { exit $(if ($allOk) { 0 } else { 1 }) }
if ($allOk) {
    Write-EasyResult $true "The test scene rendered correctly ($($devices -join ' and '))." ((Get-EasyAgain 'Make the installers again') + ', or "Open Houdini with my build (without installing)".')
    exit 0
}
Write-EasyResult $false 'A test render failed (details above).' ((Get-EasyAgain 'Update the sources and rebuild') + '; if it still fails, open an issue on the project page on GitHub with the report.')
exit 1
