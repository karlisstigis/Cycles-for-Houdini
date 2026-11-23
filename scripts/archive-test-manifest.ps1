# SPDX-License-Identifier: Apache-2.0
function Save-NativeTestManifest {
    param([Parameter(Mandatory=$true)]$Context,[Parameter(Mandatory=$true)][string]$Destination)
    $selectedDigest=[string]$Context.RuntimeIdentity.manifest_sha256
    if($selectedDigest -notmatch '^[0-9a-fA-F]{64}$'){throw 'Verified runtime context has no valid manifest digest.'}
    $selectedDigest=$selectedDigest.ToLowerInvariant()
    # A manifest path can be atomically replaced after context selection.
    # Authenticate one byte buffer, then archive that exact buffer.
    $selectedBytes=[IO.File]::ReadAllBytes($Context.Manifest)
    $selectedHasher=[Security.Cryptography.SHA256]::Create()
    try{$capturedDigest=([BitConverter]::ToString($selectedHasher.ComputeHash($selectedBytes))).Replace('-','').ToLowerInvariant()}
    finally{$selectedHasher.Dispose()}
    if($capturedDigest -ne $selectedDigest){throw 'Build manifest changed after verified runtime selection.'}
    [IO.File]::WriteAllBytes($Destination,$selectedBytes)
    if((Get-FileHash -LiteralPath $Destination -Algorithm SHA256).Hash.ToLowerInvariant() -ne $selectedDigest){
        throw 'Archived build manifest differs from the verified runtime selection.'
    }
    return $selectedDigest
}

function Assert-NativeTestArchive {
    param([Parameter(Mandatory=$true)][string]$ManifestSha256,
        [Parameter(Mandatory=$true)][string]$ArchiveIndex,
        [Parameter(Mandatory=$true)][string]$ExpectedRun)
    if($ManifestSha256 -notmatch '^[0-9a-fA-F]{64}$'){throw 'Parent verification has no valid manifest digest.'}
    $parentDigest=$ManifestSha256.ToLowerInvariant()
    $archiveRecords=@((Get-Content -LiteralPath $ArchiveIndex -Raw | ConvertFrom-Json).runs)
    if(-not $archiveRecords.Count){throw 'The child source archive contains no run records.'}
    foreach($archiveRecord in $archiveRecords){
        $childDigest=[string]$archiveRecord.manifest_sha256
        if($childDigest -notmatch '^[0-9a-fA-F]{64}$' -or $childDigest.ToLowerInvariant() -cne $parentDigest){
            throw 'Archived child manifest differs from the parent verification selection.'
        }
    }
    $matchingRecords=@($archiveRecords | Where-Object {([string]$_.run).Replace('\','/') -eq $ExpectedRun.Replace('\','/')})
    if($matchingRecords.Count -ne 1){throw 'The source archive must identify exactly the child run just dispatched.'}
    return $matchingRecords[0]
}
