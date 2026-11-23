# SPDX-License-Identifier: Apache-2.0
# Opens a separate Houdini that uses the current staged native development build,
# not the installed renderer. It opens a copy of the test scene, so the original HIP is never saved over.
# -WithoutAssetBrowser starts it as an installation without the optional asset browser component.
# Scene: -SceneFile, else $env:CYCLES_HOUDINI_ARTIST_SCENES/Cycles_Test/Cycles_Test.hiplc, else
# Houdini/Cycles_Test/Cycles_Test.hiplc in the folder that holds this repository.
[CmdletBinding()]
param([string]$SceneFile='',
      [string]$HoudiniRoot='', [string]$BuildDirectory='', [string]$Artifacts='', [switch]$WithoutAssetBrowser)
$ErrorActionPreference='Stop'
$root=Split-Path -Parent $PSScriptRoot
if(-not $SceneFile){
    $scenes=if($env:CYCLES_HOUDINI_ARTIST_SCENES){$env:CYCLES_HOUDINI_ARTIST_SCENES}else{Join-Path (Split-Path -Parent $root) 'Houdini'}
    $SceneFile=Join-Path $scenes 'Cycles_Test/Cycles_Test.hiplc'
    if(-not(Test-Path -LiteralPath $SceneFile -PathType Leaf)){
        throw "The test scene is not at $SceneFile. Set CYCLES_HOUDINI_ARTIST_SCENES to the folder that holds Cycles_Test\ (your Houdini scenes folder), or pass -SceneFile."
    }
}
. (Join-Path $PSScriptRoot 'native-test-context.ps1')
. (Join-Path $PSScriptRoot 'test-artifact-context.ps1')
$context=Get-NativeTestContext -HoudiniRoot $HoudiniRoot -BuildDirectory $BuildDirectory
$packages=$context.PackageDirectory
$hostRoot=$context.RuntimeIdentity.host
$houdini=Join-Path $context.HoudiniRoot 'bin/houdini.exe'
foreach($p in @($hostRoot,$houdini,$SceneFile)){if(-not(Test-Path -LiteralPath $p)){throw "Missing: $p"}}
$run=Join-Path (Get-TestEvidenceBase -Artifacts $Artifacts) ('manual-tests/'+[DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfff')+'-'+[Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $run -Force | Out-Null
$copy=Join-Path $run ([IO.Path]::GetFileName($SceneFile))
Copy-Item -LiteralPath $SceneFile -Destination $copy
# Development packages load the optional asset browser add-on by default.
if($WithoutAssetBrowser){$packages=New-PackageDirectoryWithoutAssetBrowser $packages (Join-Path $run 'packages-without-asset-browser')}
$previousTestEnvironment=Save-TestProcessEnvironment
try{
$envs=@{HOUDINI_PACKAGE_DIR=$packages;HOUDINI_USER_PREF_DIR=(Join-Path $run 'prefs/houdini__HVER__');HOUDINI_PATH='&';
  HOUDINI_NO_ENV_FILE='1';HOUDINI_DSO_ERROR='2';HOUDINI_NO_STARTPAGE='1';HOUDINI_DISABLE_BACKGROUND_HELP_INDEXING='1';CYCLES_HOUDINI_EXECUTION='worker';CYCLES_HOUDINI_WORKER_LOG_DIR=(Join-Path $run 'workers')}
foreach($k in [Environment]::GetEnvironmentVariables('Process').Keys){if("$k" -like 'CYCLES_HOUDINI_PT_*' -or "$k" -like 'CYCLES_HOUDINI_BDPT*'){[Environment]::SetEnvironmentVariable("$k",$null,'Process')}}
foreach($k in $envs.Keys){[Environment]::SetEnvironmentVariable($k,$envs[$k],'Process')}
$p=Start-Process -FilePath $houdini -ArgumentList @('-foreground',('"'+$copy.Replace('\','/')+'"')) -WindowStyle Normal -PassThru
Write-Output "Opened normal development build (host $hostRoot) in a separate Houdini (PID $($p.Id)) with $copy"

}finally{Restore-TestProcessEnvironment $previousTestEnvironment}
