# SPDX-License-Identifier: Apache-2.0
# Opt-in Houdini GUI regression. Trial output belongs in the ignored artifacts tree.
param(
    [Parameter(Mandatory)][string]$BuildDirectory,
    [Parameter(Mandatory)][string]$PackageDirectory,
    [Parameter(Mandatory)][string]$Case,
    [string]$HoudiniRoot = '',
    [string]$Trial = '',
    [string]$Debugger = ''
)
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '../..')).Path
. (Join-Path $root 'scripts/native-test-context.ps1')
$context = Get-NativeTestContext -BuildDirectory $BuildDirectory -PackageDirectory $PackageDirectory -HoudiniRoot $HoudiniRoot
$env:CYCLES_HOUDINI_TEST_IDENTITY = $context.RuntimeIdentity | ConvertTo-Json -Compress
if (-not $Trial) {
    $Trial = Join-Path $root ('artifacts/display-stalled-peer/' + $Case + '-' + [guid]::NewGuid().ToString('N'))
}
$arguments = @(
    (Join-Path $PSScriptRoot 'run_trial.py'),
    '--case', $Case,
    '--trial', $Trial,
    '--root', $root,
    '--houdini', (Join-Path $context.HoudiniRoot 'bin/houdini.exe'),
    '--package-dir', $context.PackageDirectory
)
if ($Debugger) { $arguments += @('--debugger', $Debugger) }
python @arguments
if ($LASTEXITCODE -ne 0) { throw "Display trial failed: $Trial" }
Write-Output "Display trial passed: $Trial"
