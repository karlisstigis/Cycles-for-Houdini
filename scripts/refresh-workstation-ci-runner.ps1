#Requires -Version 7
# SPDX-License-Identifier: Apache-2.0
[CmdletBinding(SupportsShouldProcess)]
param()
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Split-Path -Parent $PSScriptRoot)).Path
$common = (& git -C $repo rev-parse --path-format=absolute --git-common-dir).Trim()
if ($LASTEXITCODE -ne 0) { throw 'Cannot identify the project repository.' }
$configuration = Join-Path $common 'houdini-ci.json'
if (-not (Test-Path -LiteralPath $configuration -PathType Leaf)) {
    throw 'CI is not installed. Use install-workstation-ci.ps1 for initial setup.'
}
$config = Get-Content -LiteralPath $configuration -Raw | ConvertFrom-Json
if ([IO.Path]::GetFullPath($config.repository) -ne [IO.Path]::GetFullPath($repo)) {
    throw 'Run the refresh from the production repository that owns the CI installation.'
}
$runnerDirectory = Join-Path ([IO.Path]::GetFullPath($config.state)) 'runner'
$installedWorker = Join-Path $runnerDirectory 'workstation-ci.py'
if (-not (Test-Path -LiteralPath $installedWorker -PathType Leaf)) {
    throw 'The installed CI runner is missing. Use install-workstation-ci.ps1 to repair the installation.'
}
$source = Join-Path $PSScriptRoot 'workstation-ci.py'
$expected = (Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash
if ($PSCmdlet.ShouldProcess($installedWorker, 'Atomically refresh only the installed CI runner')) {
    $pendingWorker = $installedWorker + '.' + [guid]::NewGuid().ToString('N')
    try {
        Copy-Item -LiteralPath $source -Destination $pendingWorker
        if ((Get-FileHash -LiteralPath $pendingWorker -Algorithm SHA256).Hash -ne $expected) {
            throw 'The copied CI runner differs from the selected production source.'
        }
        [IO.File]::Move($pendingWorker, $installedWorker, $true)
    } finally {
        if (Test-Path -LiteralPath $pendingWorker) { Remove-Item -LiteralPath $pendingWorker -Force }
    }
    if ((Get-FileHash -LiteralPath $installedWorker -Algorithm SHA256).Hash -ne $expected) {
        throw 'The installed CI runner failed hash verification.'
    }
    Write-Output 'Updated the installed CI runner. A running drain keeps its loaded code; the next invocation uses this copy.'
    Write-Output ('Runner SHA-256: ' + $expected.ToLowerInvariant())
}
