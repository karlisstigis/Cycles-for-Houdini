# SPDX-License-Identifier: Apache-2.0
[CmdletBinding()]
param(
    [string]$Python='python',
    [string]$Wheelhouse='',
    [string]$Target='',
    [switch]$Download
)
$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot 'test-dependency-context.ps1')
$toolsContext=Get-NativeTestToolsContext -Wheelhouse $Wheelhouse -Target $Target
$Wheelhouse=$toolsContext.Wheelhouse
$Target=$toolsContext.Target
$requirements=$toolsContext.Requirements
New-Item -ItemType Directory -Path $Wheelhouse,$Target -Force | Out-Null
$Wheelhouse=(Resolve-Path -LiteralPath $Wheelhouse).Path
$Target=(Resolve-Path -LiteralPath $Target).Path
if($Download){
    & $Python -m pip download --only-binary=:all: --require-hashes -r $requirements --dest $Wheelhouse
    if($LASTEXITCODE -ne 0){throw 'Cannot download the hash-locked test wheelhouse.'}
}
# pip is supplied by ordinary Python; every wheel is pure Python and supports
# both the developer interpreter and Houdini Python 3.13. No artist installation.
& $Python -m pip install --no-index --find-links $Wheelhouse --require-hashes --only-binary=:all: --upgrade --target $Target -r $requirements
if($LASTEXITCODE -ne 0){throw 'Cannot provision test dependencies; provide the saved wheelhouse or use -Download.'}
$manifest=[ordered]@{requirements_sha256=(Get-FileHash -LiteralPath $requirements -Algorithm SHA256).Hash.ToLowerInvariant();wheels=@()}
foreach($wheel in Get-ChildItem -LiteralPath $Wheelhouse -Filter '*.whl'){
    $manifest.wheels+=@{name=$wheel.Name;sha256=(Get-FileHash -LiteralPath $wheel.FullName -Algorithm SHA256).Hash.ToLowerInvariant()}
}
$manifest | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $Wheelhouse 'manifest.json') -Encoding utf8
Copy-Item -LiteralPath $requirements -Destination (Join-Path $Wheelhouse 'requirements.txt') -Force
Write-Output "Test dependencies: $Target"
Write-Output "Offline wheelhouse: $Wheelhouse"
