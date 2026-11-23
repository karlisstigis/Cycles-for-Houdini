# SPDX-License-Identifier: Apache-2.0
[CmdletBinding()]
param([string]$HoudiniRoot='', [string]$SceneFile='', [string]$BuildDirectory='', [string]$Artifacts='')
$ErrorActionPreference='Stop'
$taskRoot=Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'native-test-context.ps1')
. (Join-Path $PSScriptRoot 'test-artifact-context.ps1')
$context=Get-NativeTestContext -HoudiniRoot $HoudiniRoot -BuildDirectory $BuildDirectory
$fixtureRoot=if($Artifacts){[IO.Path]::GetFullPath($Artifacts)}elseif($env:CYCLES_HOUDINI_TEST_ARTIFACTS){$env:CYCLES_HOUDINI_TEST_ARTIFACTS}else{Join-Path $taskRoot 'artifacts'}
$exampleRoot=Join-Path $fixtureRoot 'examples/sop-fx-courtyard'
if(-not $SceneFile){
    $SceneFile=Join-Path $exampleRoot 'sop-fx-courtyard-adaptive.hiplc'
}
$sourceScene=(Resolve-Path -LiteralPath $SceneFile).Path
# An explicit saved scene carries its own authored color configuration.
$ocio=Join-Path (Split-Path -Parent $sourceScene) 'showcase.ocio'
if(-not(Test-Path -LiteralPath $ocio -PathType Leaf)){$ocio=Join-Path $exampleRoot 'showcase.ocio'}
foreach($required in @($ocio,(Join-Path $taskRoot 'examples/sop-fx-courtyard/open_scene.py'))){
    if(-not(Test-Path -LiteralPath $required -PathType Leaf)){throw "Missing example/build artifact: $required. See docs/user/try-native-sops.md."}
}
$houdiniExecutable=Join-Path $context.HoudiniRoot 'bin/houdini.exe'
if(-not(Test-Path -LiteralPath $houdiniExecutable -PathType Leaf)){throw "Houdini executable is missing: $houdiniExecutable"}
$run=Join-Path (Get-TestEvidenceBase -Artifacts $Artifacts) ('showcase-sessions/'+[DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfff')+'-'+[Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $run -Force | Out-Null
$previousTestEnvironment=Save-TestProcessEnvironment
try{
$env:CYCLES_HOUDINI_TEST_ARTIFACTS=$fixtureRoot
$env:CYCLES_HOUDINI_EXAMPLE_OCIO=$ocio
$env:CYCLES_HOUDINI_EXAMPLE_LOG_DIR=$run
$env:CYCLES_HOUDINI_WORKER_LOG_DIR=Join-Path $run 'workers'
$env:CYCLES_HOUDINI_EXAMPLE_SCENE=$sourceScene
$env:HOUDINI_PACKAGE_DIR=$context.PackageDirectory
$env:HOUDINI_PATH='&'
$env:HOUDINI_USER_PREF_DIR=Join-Path $run 'prefs/houdini__HVER__'
$env:HOUDINI_NO_ENV_FILE='1'
$env:HOUDINI_NO_STARTPAGE='1'
$env:HOUDINI_DSO_ERROR='2'
$env:OCIO=$ocio
$bootstrap=(Join-Path $taskRoot 'examples/sop-fx-courtyard/open_scene.py').Replace('\','/')
# This is an interactive tool explicitly intended for the artist to see/control.
$process=Start-Process -FilePath $houdiniExecutable -ArgumentList @('-foreground','-indie','waitforui',('"'+$bootstrap+'"')) -WindowStyle Normal -PassThru
Write-Output "Opened native SOP Cycles example $sourceScene in Houdini (PID $($process.Id), host $($context.RuntimeIdentity.host)). Preferences are local to $run."

}finally{Restore-TestProcessEnvironment $previousTestEnvironment}
