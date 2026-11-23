# SPDX-License-Identifier: Apache-2.0
function Get-InstallerRefreshMembers {
    param([Parameter(Mandatory)][IO.Compression.ZipArchive]$Archive)
    $members=@(
        @('tools/refresh-registration.ps1','refresh-registration.ps1'),
        @('tools/Refresh Cycles registration.cmd','Refresh Cycles registration.cmd')
    )
    $present=@($members | Where-Object { $null -ne $Archive.GetEntry($_[0]) })
    if($present.Count -eq 1){throw 'Package contains only one registration refresh member; rebuild the archive.'}
    return [pscustomobject]@{Enabled=($present.Count -eq 2);Members=$present}
}
