# SPDX-License-Identifier: Apache-2.0
# Exercise the actual harness runner's guard without launching Python/Houdini.
$ErrorActionPreference='Stop'
$harnessTestRepo=Split-Path -Parent $PSScriptRoot
. (Join-Path $harnessTestRepo 'tests/support/artifacts.ps1')
$testArtifacts=Get-TestArtifactsRoot -SourceRoot $harnessTestRepo
$harnessTestRoot=Join-Path $testArtifacts ('harness-context/'+[guid]::NewGuid().ToString('N'))
foreach($harnessTestFolder in @('scripts','build/bin','build/resources/lib','fake-hfs')){
    New-Item -ItemType Directory -Path (Join-Path $harnessTestRoot $harnessTestFolder) -Force | Out-Null
}
Copy-Item -LiteralPath (Join-Path $harnessTestRepo 'scripts/run-harness.ps1') -Destination (Join-Path $harnessTestRoot 'scripts/run-harness.ps1')
Set-Content -LiteralPath (Join-Path $harnessTestRoot 'build/bin/ch_harness.exe') -Value 'Never executed'
$harnessTestBinaryDigest=(Get-FileHash -LiteralPath (Join-Path $harnessTestRoot 'build/bin/ch_harness.exe')).Hash.ToLowerInvariant()
@{resource_root='resources';publication='selected';binary_artifacts=@(@{path='bin/ch_harness.exe';sha256=$harnessTestBinaryDigest})} |
    ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $harnessTestRoot 'build/build-manifest.json')
@'
function Get-NativeTestContext {
    param([string]$BuildDirectory='', [string]$HoudiniRoot='')
    $root=Split-Path -Parent $PSScriptRoot
    $build=Join-Path $root 'build';$manifest=Join-Path $build 'build-manifest.json'
    $selected=(Get-FileHash -LiteralPath $manifest -Algorithm SHA256).Hash.ToLowerInvariant()
    $next=Get-Content -LiteralPath $manifest -Raw | ConvertFrom-Json
    $next.publication='replaced after context selection'
    $replacement=Join-Path $build 'manifest-replacement.json'
    $next | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $replacement
    [IO.File]::Move($replacement,$manifest,$true)
    [pscustomobject]@{BuildDirectory=$build;Manifest=$manifest;HoudiniRoot=(Join-Path $root 'fake-hfs');
        RuntimeIdentity=@{manifest_sha256=$selected}}
}
'@ | Set-Content -LiteralPath (Join-Path $harnessTestRoot 'scripts/native-test-context.ps1')
$global:harnessMockNativeCalls=0
function python {
    $global:harnessMockNativeCalls++
    throw 'The failed context guard must not dispatch a dependency probe or harness'
}
try{
    & (Join-Path $harnessTestRoot 'scripts/run-harness.ps1') --device CPU --subdivision-shading-contracts | Out-Null
    throw 'The harness runner accepted a replaced manifest'
}catch{if($_.Exception.Message -ne 'Build manifest changed after verified runtime selection.'){throw}}
if($global:harnessMockNativeCalls){throw 'A native dispatch occurred before rejecting the manifest replacement'}
Remove-Variable -Name harnessMockNativeCalls -Scope Global
@{result='passed';native_jobs=0;checks=@('Actual harness runner rejects atomic manifest replacement before dependency or harness dispatch')} | ConvertTo-Json
