# SPDX-License-Identifier: Apache-2.0
# Pure manifest publication races; no Houdini, renderer or native job launch.
$ErrorActionPreference='Stop'
$archiveRepo=Split-Path -Parent $PSScriptRoot
. (Join-Path $archiveRepo 'tests/support/artifacts.ps1')
$testArtifacts=Get-TestArtifactsRoot -SourceRoot $archiveRepo
. (Join-Path $archiveRepo 'scripts/archive-test-manifest.ps1')
$archiveBase=Join-Path $testArtifacts ('test-manifest-archive/'+[guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $archiveBase -Force | Out-Null
$global:archiveMockSource=Join-Path $archiveBase 'source-manifest.json'
$global:archiveMockReplacement=Join-Path $archiveBase 'replacement-manifest.json'
$global:archiveMockDestination=''
$global:archiveMockMode='none'
$global:archiveMockTriggered=$false
$archiveSelected=[Text.Encoding]::UTF8.GetBytes('{"selected":"original bytes"}')
$global:archiveMockOther=[Text.Encoding]::UTF8.GetBytes('{"selected":"different publication"}')
[IO.File]::WriteAllBytes($global:archiveMockSource,$archiveSelected)
$archiveDigest=(Microsoft.PowerShell.Utility\Get-FileHash -LiteralPath $global:archiveMockSource -Algorithm SHA256).Hash.ToLowerInvariant()
$archiveContext=@{Manifest=$global:archiveMockSource;RuntimeIdentity=@{manifest_sha256=$archiveDigest}}
function Get-FileHash {
    param([string]$LiteralPath,[string]$Algorithm)
    if($LiteralPath -eq $global:archiveMockDestination){
        if($global:archiveMockMode -eq 'swap_source'){
            [IO.File]::Move($global:archiveMockReplacement,$global:archiveMockSource,$true)
            $global:archiveMockTriggered=$true
        }elseif($global:archiveMockMode -eq 'alter_archive'){
            [IO.File]::WriteAllBytes($LiteralPath,$global:archiveMockOther)
            $global:archiveMockTriggered=$true
        }
    }
    Microsoft.PowerShell.Utility\Get-FileHash -LiteralPath $LiteralPath -Algorithm $Algorithm
}
$archiveNormal=Join-Path $archiveBase 'normal.json'
$archiveReturned=Save-NativeTestManifest -Context $archiveContext -Destination $archiveNormal
if($archiveReturned -ne $archiveDigest -or [Convert]::ToBase64String([IO.File]::ReadAllBytes($archiveNormal)) -ne [Convert]::ToBase64String($archiveSelected)){
    throw 'The archive and summary digest must describe the exact selected bytes'
}
[IO.File]::WriteAllBytes($global:archiveMockSource,$global:archiveMockOther)
$archiveBeforeCapture=Join-Path $archiveBase 'must-not-exist.json'
try{
    Save-NativeTestManifest -Context $archiveContext -Destination $archiveBeforeCapture | Out-Null
    throw 'Changed source was accepted'
}catch{if($_.Exception.Message -ne 'Build manifest changed after verified runtime selection.'){throw}}
if(Test-Path -LiteralPath $archiveBeforeCapture){throw 'Mismatched source must be rejected before archiving'}
[IO.File]::WriteAllBytes($global:archiveMockSource,$archiveSelected)
[IO.File]::WriteAllBytes($global:archiveMockReplacement,$global:archiveMockOther)
$global:archiveMockMode='swap_source'
$global:archiveMockDestination=Join-Path $archiveBase 'source-swap.json'
$archiveReturned=Save-NativeTestManifest -Context $archiveContext -Destination $global:archiveMockDestination
if(-not $global:archiveMockTriggered){throw 'Controlled atomic replacement was not exercised'}
if($archiveReturned -ne $archiveDigest -or
    (Microsoft.PowerShell.Utility\Get-FileHash -LiteralPath $global:archiveMockDestination -Algorithm SHA256).Hash.ToLowerInvariant() -ne $archiveDigest -or
    (Microsoft.PowerShell.Utility\Get-FileHash -LiteralPath $global:archiveMockSource -Algorithm SHA256).Hash.ToLowerInvariant() -eq $archiveDigest){
    throw 'Atomic replacement after capture changed the archived selected identity'
}
[IO.File]::WriteAllBytes($global:archiveMockSource,$archiveSelected)
$global:archiveMockMode='alter_archive';$global:archiveMockTriggered=$false
$global:archiveMockDestination=Join-Path $archiveBase 'archive-altered.json'
try{
    Save-NativeTestManifest -Context $archiveContext -Destination $global:archiveMockDestination | Out-Null
    throw 'Changed archive was accepted'
}catch{if($_.Exception.Message -ne 'Archived build manifest differs from the verified runtime selection.'){throw}}
if(-not $global:archiveMockTriggered){throw 'Archive digest check was not exercised'}
try{
    Save-NativeTestManifest -Context @{Manifest=$global:archiveMockSource;RuntimeIdentity=@{}} -Destination (Join-Path $archiveBase 'missing-digest.json') | Out-Null
    throw 'Missing selected digest was accepted'
}catch{if($_.Exception.Message -ne 'Verified runtime context has no valid manifest digest.'){throw}}
$queueArchive=Join-Path $archiveBase 'queue-index.json'
$queueRun='artifacts/verification/just-dispatched'
@{runs=@(@{run=$queueRun;manifest_sha256=$archiveDigest;source_archive='artifacts/verification/sources'})} |
    ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $queueArchive
$queueRecord=Assert-NativeTestArchive -ManifestSha256 $archiveDigest -ArchiveIndex $queueArchive -ExpectedRun $queueRun
if($queueRecord.manifest_sha256 -ne $archiveDigest){throw 'Matching child archive was rejected'}
# The current build may have changed A -> B -> A during the last child. Its
# archived manifest B must still fail the parent's A identity after restoration.
$otherDigest='1'*64
@{runs=@(@{run='artifacts/verification/earlier';manifest_sha256=$archiveDigest},
    @{run=$queueRun;manifest_sha256=$otherDigest})} | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $queueArchive
try{
    Assert-NativeTestArchive -ManifestSha256 $archiveDigest -ArchiveIndex $queueArchive -ExpectedRun $queueRun | Out-Null
    throw 'A different last child manifest was accepted'
}catch{if($_.Exception.Message -ne 'Archived child manifest differs from the parent verification selection.'){throw}}
@{runs=@(@{run='artifacts/verification/wrong-child';manifest_sha256=$archiveDigest})} |
    ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $queueArchive
try{
    Assert-NativeTestArchive -ManifestSha256 $archiveDigest -ArchiveIndex $queueArchive -ExpectedRun $queueRun | Out-Null
    throw 'Wrong child run was accepted'
}catch{if($_.Exception.Message -ne 'The source archive must identify exactly the child run just dispatched.'){throw}}
Remove-Variable -Scope Global -Name archiveMockSource,archiveMockReplacement,archiveMockDestination,archiveMockMode,archiveMockTriggered,archiveMockOther
[pscustomobject]@{result='passed';native_jobs=0;checks=@('Exact selected buffer archived and returned digest bound',
    'Changed source rejected before capture','Atomic source replacement after capture cannot relabel archive',
    'Altered archive rejected','Missing context digest rejected','Matching child archive selected',
    'Last child manifest mismatch rejected despite parent restoration','Wrong archived child rejected')} | ConvertTo-Json -Depth 4
