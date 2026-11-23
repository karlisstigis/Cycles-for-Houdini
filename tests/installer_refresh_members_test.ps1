# SPDX-License-Identifier: Apache-2.0
$ErrorActionPreference='Stop'
$root=Split-Path -Parent $PSScriptRoot
. (Join-Path $root 'scripts/installer-refresh-members.ps1')
$artifacts=[IO.Path]::GetFullPath((Join-Path $root 'artifacts/compatibility-unit/installer-refresh-members'))
New-Item -ItemType Directory -Force -Path $artifacts | Out-Null
$fixture=[IO.Path]::GetFullPath((Join-Path $artifacts ([guid]::NewGuid().ToString('N'))))
if(-not $fixture.StartsWith($artifacts+[IO.Path]::DirectorySeparatorChar,[StringComparison]::OrdinalIgnoreCase)){
    throw 'Fixture path is outside the intended artifact root'
}
New-Item -ItemType Directory -Path $fixture | Out-Null
try{
    foreach($case in @(
        @{Name='legacy';Members=@();Enabled=$false;Reject=$false},
        @{Name='current';Members=@('tools/refresh-registration.ps1','tools/Refresh Cycles registration.cmd');Enabled=$true;Reject=$false},
        @{Name='partial';Members=@('tools/refresh-registration.ps1');Enabled=$false;Reject=$true}
    )){
        $path=Join-Path $fixture ($case.Name+'.zip')
        $archive=[IO.Compression.ZipFile]::Open($path,[IO.Compression.ZipArchiveMode]::Create)
        try{foreach($name in $case.Members){$null=$archive.CreateEntry($name)}}finally{$archive.Dispose()}
        $archive=[IO.Compression.ZipFile]::OpenRead($path)
        try{
            if($case.Reject){
                try{$null=Get-InstallerRefreshMembers -Archive $archive;throw 'Partial refresh pair was accepted'}
                catch{if($_.Exception.Message -notlike 'Package contains only one registration refresh member*'){throw}}
            }else{
                $result=Get-InstallerRefreshMembers -Archive $archive
                if($result.Enabled -ne $case.Enabled -or $result.Members.Count -ne $case.Members.Count){
                    throw "Incorrect refresh plan for $($case.Name) archive"
                }
            }
        }finally{$archive.Dispose()}
    }
    'Installer refresh member tests passed'
}finally{
    Remove-Item -LiteralPath $fixture -Recurse -Force
}
