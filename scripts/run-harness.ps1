# SPDX-License-Identifier: Apache-2.0
[CmdletBinding(PositionalBinding=$false)]
param(
    [string]$HoudiniRoot = '',
    [string]$BuildDirectory = '',
    [Parameter(Position=0,ValueFromRemainingArguments=$true)][string[]]$HarnessArguments
)
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'native-test-context.ps1')
$context=Get-NativeTestContext -HoudiniRoot $HoudiniRoot -BuildDirectory $BuildDirectory
$HoudiniRoot=$context.HoudiniRoot
$build=$context.BuildDirectory
$harnessManifestBytes=[IO.File]::ReadAllBytes($context.Manifest)
$harnessManifestHasher=[Security.Cryptography.SHA256]::Create()
try{$harnessManifestDigest=([BitConverter]::ToString($harnessManifestHasher.ComputeHash($harnessManifestBytes))).Replace('-','').ToLowerInvariant()}
finally{$harnessManifestHasher.Dispose()}
if($harnessManifestDigest -ne $context.RuntimeIdentity.manifest_sha256){throw 'Build manifest changed after verified runtime selection.'}
$manifest = [Text.Encoding]::UTF8.GetString($harnessManifestBytes).TrimStart([char]0xfeff) | ConvertFrom-Json
$resourceRelative = $manifest.resource_root
if(-not $resourceRelative){throw 'The staged build manifest must identify its matching resource directory.'}
$resources = [IO.Path]::GetFullPath((Join-Path $build $resourceRelative))
if (-not $resources.StartsWith([IO.Path]::GetFullPath($build)+[IO.Path]::DirectorySeparatorChar,[StringComparison]::OrdinalIgnoreCase)) {
    throw 'The manifest resource directory must stay inside the build directory.'
}
if (-not (Test-Path -LiteralPath (Join-Path $resources 'lib') -PathType Container)) {
    throw 'Matching staged GPU resources are missing. Stage the build before running the harness.'
}
$harness=Join-Path $build 'bin/ch_harness.exe'
$harnessRecord=@($manifest.binary_artifacts | Where-Object {$_.path.Replace('\','/') -eq 'bin/ch_harness.exe'})
if($harnessRecord.Count -ne 1 -or (Get-FileHash -LiteralPath $harness -Algorithm SHA256).Hash.ToLowerInvariant() -ne $harnessRecord[0].sha256){
    throw 'The harness executable differs from the staged build manifest.'
}
$deps = Join-Path $taskRoot 'third_party/cycles/lib/windows_x64'
$dllPaths = @((Join-Path $HoudiniRoot 'bin'))
foreach ($directory in @('pthreads','embree','epoxy','dpcpp','level-zero')) {
    $dllPaths += Join-Path $deps "$directory/bin"
}
$env:PATH = ($dllPaths -join ';') + ';' + $env:PATH
$env:HFS = $HoudiniRoot
& python (Join-Path $PSScriptRoot 'sdk-dependencies.py') --houdini-root $HoudiniRoot --cache (Join-Path $build 'CMakeCache.txt') --expected (Join-Path $resources 'sdk-dependencies.json')
if($LASTEXITCODE -ne 0){throw 'The harness requires the matching pinned SDK OIDN provider.'}
& $harness --resources $resources @HarnessArguments
exit $LASTEXITCODE
