# SPDX-License-Identifier: Apache-2.0
. (Join-Path $PSScriptRoot 'build-layout.ps1')
function Get-NativeTestContext {
    param([string]$BuildDirectory='', [string]$HoudiniRoot='', [string]$PackageDirectory='')
    $repo=Split-Path -Parent $PSScriptRoot
    if(-not $HoudiniRoot){
        $lock=Get-Content -LiteralPath (Join-Path $repo 'source.lock.json') -Raw | ConvertFrom-Json
        $HoudiniRoot=Join-Path $env:ProgramFiles ('Side Effects Software/Houdini '+$lock.certification_candidate.houdini)
    }
    $header=Join-Path $HoudiniRoot 'toolkit/include/SYS/SYS_Version.h'
    $match=[regex]::Match((Get-Content -LiteralPath $header -Raw),'(?m)^#define SYS_VERSION_FULL "([^"]+)"')
    if(-not $match.Success){throw "Cannot identify Houdini at $HoudiniRoot"}
    $version=$match.Groups[1].Value
    if(-not $BuildDirectory){
        $BuildDirectory=Get-DefaultBuildDirectory -SourceRoot $repo -HoudiniVersion $version
        if(-not(Test-Path -LiteralPath $BuildDirectory)){
            $earlier=Get-EarlierBuildNotice -SourceRoot $repo -BuildDirectory $BuildDirectory
            throw ("No build at $BuildDirectory; build it first with scripts/build.ps1."+$(if($earlier){' '+$earlier}else{''}))
        }
    }
    $build=(Resolve-Path -LiteralPath $BuildDirectory).Path
    $manifest=Join-Path $build 'build-manifest.json'
    $manifestBytes=[IO.File]::ReadAllBytes($manifest)
    $manifestHasher=[Security.Cryptography.SHA256]::Create()
    try{$manifestDigest=[BitConverter]::ToString($manifestHasher.ComputeHash($manifestBytes)).Replace('-','').ToLowerInvariant()}finally{$manifestHasher.Dispose()}
    $identity=[Text.Encoding]::UTF8.GetString($manifestBytes).TrimStart([char]0xfeff) | ConvertFrom-Json
    if($identity.host_sdk.houdini -ne $version){throw "Test host $version differs from build $($identity.host_sdk.houdini)"}
    if(-not $PackageDirectory){$PackageDirectory=Join-Path $build 'packages'}
    $packages=(Resolve-Path -LiteralPath $PackageDirectory).Path
    if(-not(Test-Path -LiteralPath $packages -PathType Container)){throw 'PackageDirectory must be a directory of Houdini package JSON files.'}
    $matching=@(Get-ChildItem -LiteralPath $packages -Filter '*.json' | ForEach-Object {
        $package=Get-Content -LiteralPath $_.FullName -Raw | ConvertFrom-Json
        $product=@($package.env | Where-Object {$_.PSObject.Properties.Name -contains 'CYCLES_HOUDINI_HOST_ROOT'})
        $majorMinor=($version -split '\.')[0..1] -join '.'
        $eligible=@($package.enable -split ' or ')
        if(("houdini_version == '$version'" -in $eligible -or $package.enable -eq "houdini_version == '$majorMinor'") -and $package.hpath -and $product.Count){$package}
    })
    if($matching.Count -ne 1){throw 'Test packages must select exactly one matching Cycles host.'}
    $hostPath=(Resolve-Path -LiteralPath $matching[0].hpath[0]).Path
    $releaseManifest=Join-Path (Split-Path -Parent $hostPath) 'release-manifest.json'
    if(Test-Path -LiteralPath $releaseManifest){
        $provenance=Join-Path (Split-Path -Parent $hostPath) 'provenance/build-manifest.json'
        $receipt=Get-Content -LiteralPath $releaseManifest -Raw | ConvertFrom-Json
        if((Get-FileHash -LiteralPath $provenance).Hash.ToLowerInvariant() -ne $manifestDigest){
            if($receipt.purpose -ne 'public' -or -not $receipt.public_payload_sha256){throw 'Installed payload belongs to a different build manifest.'}
            & python (Join-Path $PSScriptRoot 'release-package.py') verify-build-provenance --version-root (Split-Path -Parent $hostPath) --build-manifest $manifest | Out-Null
            if($LASTEXITCODE){throw 'Installed portable provenance belongs to a different build manifest.'}
        }
        foreach($record in $receipt.files){
            $file=Join-Path (Split-Path -Parent $hostPath) $record.path
            if((Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash.ToLowerInvariant() -ne $record.sha256){throw "Installed payload differs from receipt: $($record.path)"}
        }
    }
    $prefix=($identity.resource_root -replace '/resources$','')+'/'
    $records=@((@($identity.host_runtime)+@($identity.binary_artifacts)) | Where-Object {$_})
    foreach($record in $records){
        $relative=$record.path.Replace('\','/')
        if(($relative -split '/') -contains 'backup' -or $relative -match '\.(pyc|pdb|lib|exp)$'){continue}
        if($relative.StartsWith($prefix)){
            $file=Join-Path $hostPath $relative.Substring($prefix.Length)
            if((Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash.ToLowerInvariant() -ne $record.sha256){throw "Test payload differs from build manifest: $relative"}
        }
    }
    foreach($record in $identity.kernels){
        $file=Join-Path $hostPath ('resources/lib/'+$record.name)
        if((Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash.ToLowerInvariant() -ne $record.sha256){throw "Test kernel differs from build: $($record.name)"}
    }
    $release=$null
    foreach($entry in $matching[0].env){if($entry.PSObject.Properties.Name -contains 'CYCLES_HOUDINI_RELEASE_ID'){$release=$entry.CYCLES_HOUDINI_RELEASE_ID}}
    if((Get-FileHash -LiteralPath $manifest -Algorithm SHA256).Hash.ToLowerInvariant() -ne $manifestDigest){throw 'Build manifest changed during runtime selection.'}
    $runtime=@{host=$hostPath;houdini=$version;release=$release;build_directory=$build;manifest=$manifest;manifest_sha256=$manifestDigest;houdini_root=[IO.Path]::GetFullPath($HoudiniRoot)}
    [pscustomobject]@{BuildDirectory=$build;HoudiniRoot=[IO.Path]::GetFullPath($HoudiniRoot);Manifest=$manifest;PackageDirectory=$packages;Version=$version;RuntimeIdentity=$runtime}
}

function Quote-NativeArgument([string]$Value){
    if($Value.Length -eq 0){return '""'}
    if($Value -notmatch '[\s"]'){return $Value}
    '"'+([regex]::Replace($Value,'(\\*)"','$1$1\"') -replace '(\\+)$','$1$1')+'"'
}

# Copy of a development package directory that mirrors an installation without
# the optional asset browser: its add-on leaves the Houdini path and the panel
# reports itself disabled. Package files without a Cycles host stay unchanged.
function New-PackageDirectoryWithoutAssetBrowser([string]$PackageDirectory,[string]$Destination){
    New-Item -ItemType Directory -Force -Path $Destination | Out-Null
    foreach($file in @(Get-ChildItem -LiteralPath $PackageDirectory -Filter '*.json' -File)){
        $target=Join-Path $Destination $file.Name
        $package=Get-Content -LiteralPath $file.FullName -Raw | ConvertFrom-Json
        $product=@($package.env | Where-Object {$_.PSObject.Properties.Name -contains 'CYCLES_HOUDINI_HOST_ROOT'})
        if(-not $product.Count -or -not $package.hpath){Copy-Item -LiteralPath $file.FullName -Destination $target;continue}
        $package.hpath=[object[]]@($package.hpath | Where-Object {"$_".Replace('\','/').TrimEnd('/') -notlike '*/addons/asset_browser'})
        $environment=@($package.env | Where-Object {$_.PSObject.Properties.Name -notcontains 'CYCLES_HOUDINI_ASSET_BROWSER'})
        $package.env=[object[]]($environment+@([pscustomobject]@{CYCLES_HOUDINI_ASSET_BROWSER='0'}))
        $package | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $target -Encoding utf8
    }
    (Resolve-Path -LiteralPath $Destination).Path
}
