# SPDX-License-Identifier: Apache-2.0
<#
Build the Windows Setup.exe around a verified release package.

The package's own install tool (tools/release-package.py inside the ZIP) checks
the archive first; the Setup embeds the archive's SHA-256 and passes it back to
the same tool at install time. Requires Inno Setup 6 (https://jrsoftware.org).

  pwsh -File scripts/build-installer.ps1                       # newest candidate package
  pwsh -File scripts/build-installer.ps1 -Package <zip>
#>
[CmdletBinding()]
param(
    [string]$Package='',
    [string]$OutputDir='',
    [string]$Iscc='',
    [string]$HoudiniRoot='',
    [string]$SigningThumbprint=$env:CYCLES_HOUDINI_SIGNING_THUMBPRINT,
    [uri]$TimestampServer=$env:CYCLES_HOUDINI_TIMESTAMP_SERVER
)
$ErrorActionPreference='Stop'
$certificate=$null
if($SigningThumbprint){
    if($SigningThumbprint -notmatch '^[0-9A-Fa-f]{40}$'){throw 'Expected a certificate thumbprint of 40 hexadecimal digits.'}
    $certificate=Get-Item -LiteralPath ("Cert:\CurrentUser\My\"+$SigningThumbprint)
    if(-not $certificate.HasPrivateKey){throw 'The selected code-signing certificate has no accessible private key.'}
}elseif($TimestampServer){throw 'A timestamp server requires a signing certificate.'}
Add-Type -AssemblyName System.IO.Compression.FileSystem
$root=Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'release-layout.ps1')
if(-not $HoudiniRoot){
    $version=(Get-Content -LiteralPath (Join-Path $root 'source.lock.json') -Raw | ConvertFrom-Json).certification_candidate.houdini
    $HoudiniRoot=Join-Path $env:ProgramFiles ("Side Effects Software/Houdini "+$version)
}
if(-not $Package){
    $versionHeader=Get-Content -LiteralPath (Join-Path $HoudiniRoot 'toolkit/include/SYS/SYS_Version.h') -Raw
    $versionMatch=[regex]::Match($versionHeader,'(?m)^#define SYS_VERSION_FULL "(\d+\.\d+\.\d+)"')
    if(-not $versionMatch.Success){throw "Cannot identify the exact Houdini SDK target at $HoudiniRoot."}
    $Package=Find-ReleasePackage $root $versionMatch.Groups[1].Value
}
$Package=(Resolve-Path -LiteralPath $Package).Path
if(-not $Iscc){
    foreach($candidate in @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe","$env:ProgramFiles\Inno Setup 6\ISCC.exe","$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe")){
        if($candidate -and (Test-Path -LiteralPath $candidate -PathType Leaf)){$Iscc=$candidate;break}
    }
    if(-not $Iscc){$command=Get-Command ISCC.exe -ErrorAction SilentlyContinue;if($command){$Iscc=$command.Source}}
    if(-not $Iscc){throw 'Inno Setup 6 is required (ISCC.exe not found). Install it from https://jrsoftware.org/isdl.php, or pass -Iscc <path>.'}
}
$python=Join-Path $HoudiniRoot 'python313\python.exe'
if(-not(Test-Path -LiteralPath $python -PathType Leaf)){throw "Houdini's Python is missing: $python"}
if(-not $OutputDir){$OutputDir=Join-Path (Get-ReleaseEvidenceRoot $root) ('installer-'+[DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfff'))}
# Inno resolves relative preprocessor paths against the .iss directory, not
# the caller's working directory. Resolve the output/staging root once here.
$OutputDir=[IO.Path]::GetFullPath($OutputDir)
$stage=Join-Path $OutputDir 'stage'
if([IO.Path]::GetFullPath($stage) -ne ($OutputDir.TrimEnd('\','/')+[IO.Path]::DirectorySeparatorChar+'stage')){throw 'Invalid installer staging path'}
if(Test-Path -LiteralPath $stage){Remove-Item -LiteralPath $stage -Recurse -Force}
New-Item -ItemType Directory -Force -Path $stage | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $stage 'branding') | Out-Null

# Take the install tool, licence texts and manifest from the package itself.
$zip=[IO.Compression.ZipFile]::OpenRead($Package)
try{
    . (Join-Path $PSScriptRoot 'installer-refresh-members.ps1')
    $refresh=Get-InstallerRefreshMembers -Archive $zip
    foreach($pair in @(@('tools/release-package.py','release-package.py'),
                       @('tools/maintain-registration.ps1','maintain-registration.ps1'),
                       @('LICENSE','LICENSE.txt'),
                       @('THIRD_PARTY_NOTICES.md','THIRD_PARTY_NOTICES.md'),@('release-manifest.json','release-manifest.json'))){
        $entry=$zip.GetEntry($pair[0]);if(-not $entry){throw "Package entry missing: $($pair[0])"}
        [IO.Compression.ZipFileExtensions]::ExtractToFile($entry,(Join-Path $stage $pair[1]),$true)
    }
    foreach($pair in $refresh.Members){
        $entry=$zip.GetEntry($pair[0])
        [IO.Compression.ZipFileExtensions]::ExtractToFile($entry,(Join-Path $stage $pair[1]),$true)
    }
    # Branding comes from the same verified runtime payload, so rebuilding an
    # installer never silently picks up unrelated artwork from the checkout.
    foreach($name in @('installer.ico','installer-welcome.png','installer-header.png')){
        $entry=$zip.GetEntry("host/config/branding/$name")
        if(-not $entry){throw "Package branding missing: $name. Rebuild the release package first."}
        [IO.Compression.ZipFileExtensions]::ExtractToFile($entry,(Join-Path $stage "branding/$name"),$true)
    }
}finally{$zip.Dispose()}
Copy-Item -LiteralPath (Join-Path $stage 'release-package.py') -Destination (Join-Path $stage 'release-package-setup.py')

$sha=(Get-FileHash -LiteralPath $Package -Algorithm SHA256).Hash.ToLowerInvariant()
$verified=& $python -I (Join-Path $stage 'release-package.py') verify $Package --expected-sha256 $sha
if($LASTEXITCODE -ne 0){throw "Package verification failed: $verified"}
$manifest=Get-Content -LiteralPath (Join-Path $stage 'release-manifest.json') -Raw | ConvertFrom-Json
$houdini=[string]$manifest.houdini
if($houdini -notmatch '^(\d+)\.(\d+)\.\d+$'){throw "Unexpected Houdini version in package: $houdini"}
$prefVersion="$($Matches[1]).$($Matches[2])"
# A community build (manifest.build_identity, see docs/dev/community-builds.md) gets its own
# application id, folder, package file, name and publisher from the package itself.
$community=$manifest.PSObject.Properties.Name -contains 'build_identity'
if($community){
    # Apply the current publisher rules even if the package contains an older helper.
    $targetJson=& $python -I (Join-Path $PSScriptRoot 'release-package.py') identity --houdini $houdini --archive $Package
    if($LASTEXITCODE -ne 0){throw 'The community build identity failed validation (see package tool error above).'}
}else{
    $targetJson=& $python -I (Join-Path $stage 'release-package.py') identity --houdini $houdini
}
if($LASTEXITCODE -ne 0){throw 'The package must contain the qualified per-target installer helper. Repackage with the current helper before building Setup.'}
$target=$targetJson | ConvertFrom-Json
# Setup's ownership/consent preflight must come from the verified package too.
# Refuse older helpers at build time instead of shipping a Setup that cannot run.
$preflightHelp=& $python -I (Join-Path $stage 'release-package.py') registration-status --help 2>&1
if($LASTEXITCODE -ne 0){throw 'The package must contain the registration-status installer helper. Repackage with the current helper before building Setup.'}
$installHelp=& $python -I (Join-Path $stage 'release-package.py') install --help 2>&1
if($LASTEXITCODE -ne 0 -or ($installHelp -join "`n") -notmatch '--blender-bridge'){
    throw 'The package must contain the optional Blender bridge installer helper. Repackage with the current helper before building Setup.'
}
if(($installHelp -join "`n") -notmatch '--asset-browser'){
    throw 'The package must contain the optional asset browser installer helper. Repackage with the current helper before building Setup.'
}
# Setup's Full type selects the asset browser; its add-on must be in the payload.
if(-not @($manifest.files | Where-Object {$_.path -like 'host/addons/asset_browser/python_panels/*.pypanel'}).Count){
    throw 'The package does not contain the asset browser add-on (host/addons/asset_browser). Restage and repackage before building Setup.'
}
$header=Join-Path $HoudiniRoot 'toolkit\include\SYS\SYS_Version.h'
if(-not (Select-String -LiteralPath $header -Pattern ('^#define SYS_VERSION_FULL "'+[regex]::Escape($houdini)+'"') -Quiet)){
    throw "HoudiniRoot does not match the package target $houdini. Pass its exact SDK installation."
}
$short=$manifest.package_id.Substring(0,8)
$date=(Get-Item -LiteralPath $Package).LastWriteTimeUtc
$appVersion='{0:yyyy.MM.dd} ({1}{2})' -f $date,$short,$(if($manifest.purpose -eq 'public'){''}else{', internal test build'})
$versionInfo='{0}.{1}.{2}.0' -f $date.Year,$date.Month,$date.Day
$outputName="CyclesForHoudini-$houdini-$($date.ToString('yyyyMMdd'))-$short-Setup"
$communityText=''
if($community){
    $identity=$manifest.build_identity;$record=$manifest.build_record
    $appVersion='{0:yyyy.MM.dd} ({1}, community build)' -f $date,$short
    $outputName="CyclesForHoudini-$houdini-$($date.ToString('yyyyMMdd'))-$short-$($identity.build_id)-Setup"
    $notRun=if(@($record.not_run).Count){@($record.not_run | ForEach-Object {'{0} ({1})' -f $_.name,$_.reason}) -join '; '}else{'none'}
    $failed=@($record.checks | Where-Object result -NE 'passed' | ForEach-Object {'{0}: {1}' -f $_.name,$_.result})
    $failed=if($failed.Count){"`n- Checks that did not pass: "+($failed -join ', ')+'.'}else{''}
    $changes=if($null -eq $record.local_changes){'unknown (not built from a Git checkout)'}elseif($record.local_changes){'yes: the sources had changes that were not committed'}else{'no'}
    $communityText="`n"+@"
This is a community build, not the official Cycles for Houdini
- Name: $($identity.build_name) (id $($identity.build_id)), made by $($identity.publisher).
- Built from the public sources (engine route: $($record.engine_route)) at commit $(if($record.source_commit){$record.source_commit}else{'unknown'}).
  Local changes: $changes.
- Checks that passed before packaging: $(@($record.checks | Where-Object result -EQ 'passed' | ForEach-Object name) -join ', ').
- Checks that did not run: $notRun.$failed
- DLSS: $($identity.dlss).
- It has its own folder and Windows Settings > Apps entry. Setup stops if another Cycles package in the
  selected preferences can load in any of the same Houdini builds. This can happen with different
  Setup targets when the builds are compatible. Uninstall that Cycles build before retrying.

"@
}

$before=@"
$(if($community){$target.display_name}else{'Cycles for Houdini'}) $appVersion
$communityText
Requirements
- Installer target: Houdini $houdini (Indie, Core or FX). The package may also load in another
  $prefVersion build already installed at Setup time when its HDK API and required DLLs match;
  that use produces a warning. $(if($refresh.Enabled){'After adding another Houdini build, run the installed Refresh Cycles registration.cmd (enter its path when installed outside Program Files).'}else{'Reinstall after adding another Houdini build.'})
- Houdini Apprentice is unsupported. CPU rendering needs no NVIDIA GPU. GPU rendering uses NVIDIA OptiX.
- Blender is optional. Native Houdini scenes render without it. Select the Blender .blend asset bridge
  only if you want to load .blend assets, and install Blender separately (or set CYCLES_HOUDINI_BLENDER).

What Setup does
- Verifies the package (SHA-256 $($sha.Substring(0,16))...) and every file in it.
- Installs it to the folder you choose (default: your local app data), and adds one Houdini package
  file, packages\$($target.package_filename), to your Houdini preferences folder.
- If Setup was interrupted, run the same Setup again and use the same installation folder to recover.
- Uninstall (Windows Settings > Apps) removes its package file and restores the one it replaced, if any.
  It works after Houdini is removed. Older renderer versions and saved data stay in the installed folder.

$(if($manifest.purpose -ne 'public'){"This is an internal test build, not a public release."})
$(if(-not $certificate){'This Setup is unsigned. Windows SmartScreen may show a publisher warning.'})
"@
Set-Content -LiteralPath (Join-Path $stage 'BEFORE-INSTALL.txt') -Value $before -Encoding utf8

$script=Join-Path $root 'packaging\installer\CyclesHoudini.iss'
$arguments=@("/DPackageZip=$Package","/DPackageZipName=$([IO.Path]::GetFileName($Package))","/DPackageSha=$sha",
             "/DStageDir=$stage","/DOutputDir=$OutputDir","/DOutputName=$outputName","/DAppVersion=$appVersion",
             "/DVersionInfo=$versionInfo","/DHoudiniVersion=$houdini","/DHoudiniPrefVersion=$prefVersion",'/Q',$script)
$arguments=@("/DTargetAppId=$($target.app_id)","/DTargetDirectory=$($target.default_directory)","/DTargetPackageFile=$($target.package_filename)")+$arguments
if($community){
    $arguments=@("/DAppName=$($target.display_name)","/DAppPublisherName=$($target.publisher)",
                 "/DAppPublisherLink=$($target.publisher_url)","/DBuildId=$($target.build_id)")+$arguments
}
if($refresh.Enabled){$arguments=@('/DHasRefreshRegistration=1')+$arguments}
& $Iscc @arguments
if($LASTEXITCODE -ne 0){throw "Inno Setup failed ($LASTEXITCODE)."}
$setup=Join-Path $OutputDir "$outputName.exe"
if($certificate){
    $signatureArgs=@{FilePath=$setup;Certificate=$certificate;HashAlgorithm='SHA256'}
    if($TimestampServer){$signatureArgs.TimestampServer=$TimestampServer.AbsoluteUri}
    $null=Set-AuthenticodeSignature @signatureArgs
    $verifiedSignature=Get-AuthenticodeSignature -LiteralPath $setup
    if($verifiedSignature.Status -ne 'Valid' -or $verifiedSignature.SignerCertificate.Thumbprint -ne $certificate.Thumbprint){
        throw ('Installer Authenticode verification failed: '+$verifiedSignature.StatusMessage)
    }
}
$report=[ordered]@{result='passed';setup=$setup;setup_sha256=(Get-FileHash -LiteralPath $setup -Algorithm SHA256).Hash.ToLowerInvariant();
    setup_bytes=(Get-Item -LiteralPath $setup).Length;package=$Package;package_sha256=$sha;package_id=$manifest.package_id;
    purpose=$manifest.purpose;houdini=$houdini;app_id=$target.app_id;package_filename=$target.package_filename;
    default_directory=$target.default_directory;app_version=$appVersion;iscc=$Iscc;signed=[bool]$certificate}
if($community){
    $report.official=$false;$report.display_name=$target.display_name;$report.publisher=$target.publisher
    $report.build_identity=$manifest.build_identity;$report.build_record=$manifest.build_record
}
$report | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $OutputDir "$outputName.json") -Encoding utf8
$report | ConvertTo-Json -Depth 8
