# SPDX-License-Identifier: Apache-2.0
# Run the real launchers against disposable fake native processes only.
$ErrorActionPreference='Stop'
$repo=Split-Path -Parent $PSScriptRoot
. (Join-Path $repo 'tests/support/artifacts.ps1')
. (Join-Path $repo 'scripts/test-artifact-context.ps1')
$callerEnvironment=Save-TestProcessEnvironment
try{
    Remove-Item -LiteralPath Env:HOUDINI_PATH -ErrorAction SilentlyContinue
    Set-Item -LiteralPath Env:HOUDINI_CYCLES_ROUTING_EMPTY -Value ''
    $env:CYCLES_HOUDINI_ROUTING_OVERRIDE='caller override'
    $env:CYCLES_HOUDINI_ROUTING_UNCHANGED='leave unchanged'
    $saved=Save-TestProcessEnvironment
    $env:HOUDINI_PATH='&'
    $env:HOUDINI_CYCLES_ROUTING_EMPTY='child value'
    $env:CYCLES_HOUDINI_ROUTING_OVERRIDE='child override'
    $env:HOUDINI_CYCLES_ROUTING_ADDED='child-only value'
    Restore-TestProcessEnvironment $saved
    if(Test-Path -LiteralPath Env:HOUDINI_PATH){throw 'Absent HOUDINI_PATH was restored as present-empty'}
    if(Test-Path -LiteralPath Env:HOUDINI_CYCLES_ROUTING_ADDED){throw 'Child-only variable was restored as present-empty'}
    if(-not(Test-Path -LiteralPath Env:HOUDINI_CYCLES_ROUTING_EMPTY) -or $env:HOUDINI_CYCLES_ROUTING_EMPTY -cne ''){throw 'Caller present-empty environment value was lost'}
    if($env:CYCLES_HOUDINI_ROUTING_OVERRIDE -cne 'caller override'){throw 'Caller override was lost'}
    # A no-op restoration must perform no provider mutations at all.
    function Set-Item {throw 'Unchanged environment variable was rewritten'}
    function Remove-Item {throw 'Unchanged environment variable was removed'}
    try{Restore-TestProcessEnvironment $saved}finally{
        Microsoft.PowerShell.Management\Remove-Item Function:Set-Item,Function:Remove-Item
    }
}finally{Restore-TestProcessEnvironment $callerEnvironment}
$base=Join-Path (Get-TestArtifactsRoot -SourceRoot $repo) ('runner-routing/'+[guid]::NewGuid().ToString('N'))
foreach($folder in @('scripts','tests','build/packages','fake-hfs')){New-Item -ItemType Directory -Path (Join-Path $base $folder) -Force | Out-Null}
foreach($file in @('run-prototype-test.ps1','run-gui-test.ps1','native-test-arguments.ps1','test-artifact-context.ps1','launch-sop-showcase.ps1')){
    Copy-Item -LiteralPath (Join-Path $repo "scripts/$file") -Destination (Join-Path $base "scripts/$file")
}
Set-Content -LiteralPath (Join-Path $base 'build/build-manifest.json') -Value '{"test":true}'
Set-Content -LiteralPath (Join-Path $base 'tests/fake.py') -Value '# never executed'
Set-Content -LiteralPath (Join-Path $base 'scripts/verify-verification.py') -Value '# provenance is independently tested; fake payload here'
@'
function Get-NativeTestContext {
    param($BuildDirectory,$HoudiniRoot,$PackageDirectory)
    $root=Split-Path -Parent $PSScriptRoot
    [pscustomobject]@{BuildDirectory=(Join-Path $root 'build');HoudiniRoot=(Join-Path $root 'fake-hfs');PackageDirectory=(Join-Path $root 'build/packages');Manifest=(Join-Path $root 'build/build-manifest.json');RuntimeIdentity=@{host=(Join-Path $root 'build/host');test=$true;release=$(if($PackageDirectory -eq 'installed'){'installed'}else{$null})}}
}
function Quote-NativeArgument($value){'"'+$value+'"'}
'@ | Set-Content -LiteralPath (Join-Path $base 'scripts/native-test-context.ps1')
@'
function Save-NativeTestManifest {param($Context,$Destination) Copy-Item -LiteralPath $Context.Manifest -Destination $Destination; (Get-FileHash -LiteralPath $Destination).Hash}
'@ | Set-Content -LiteralPath (Join-Path $base 'scripts/archive-test-manifest.ps1')
$global:routingLaunches=@()
function Start-Process {
    param($FilePath,$ArgumentList,$WindowStyle,[switch]$PassThru,$RedirectStandardOutput,$RedirectStandardError)
    if($global:routingLaunchFailure){throw 'mock native launch failure'}
    $global:routingLaunches+=,[pscustomobject]@{file=$FilePath;artifacts=$env:CYCLES_HOUDINI_TEST_ARTIFACTS;prefs=$env:HOUDINI_USER_PREF_DIR;workers=$env:CYCLES_HOUDINI_WORKER_LOG_DIR;ocio=$env:OCIO;launch_logs=$env:CYCLES_HOUDINI_EXAMPLE_LOG_DIR;override=$env:CYCLES_HOUDINI_ROUTING_OVERRIDE}
    if($RedirectStandardOutput){Set-Content -LiteralPath $RedirectStandardOutput -Value 'mock native launch'}
    if($RedirectStandardError){Set-Content -LiteralPath $RedirectStandardError -Value ''}
    if($FilePath -like '*hython.exe'){
        $fixtureRoot=if($env:CYCLES_HOUDINI_TEST_ARTIFACTS){$env:CYCLES_HOUDINI_TEST_ARTIFACTS}else{Join-Path $env:CYCLES_HOUDINI_SOURCE_ROOT 'artifacts'}
        $target=Join-Path $fixtureRoot 'fixture/report.json'
        New-Item -ItemType Directory -Path (Split-Path -Parent $target) -Force | Out-Null
        @{result='passed'} | ConvertTo-Json | Set-Content -LiteralPath $target
    }
    [pscustomobject]@{Handle=0;ExitCode=0;Id=12345} |
        Add-Member -MemberType ScriptMethod -Name WaitForExit -Value {param($milliseconds)
            if($env:CYCLES_HOUDINI_GUI_REPORT){@{result='passed'} | ConvertTo-Json | Set-Content -LiteralPath $env:CYCLES_HOUDINI_GUI_REPORT}
            $true
        } -PassThru
}
$selected=Join-Path $base 'selected'
$previous=$env:CYCLES_HOUDINI_TEST_ARTIFACTS
# Exercise the actual nested prototype->GUI restoration, followed by a headless
# launch. Real child bootstrapping belongs to the launchers; it cannot rely on
# a prior GUI process leaking HOUDINI_PATH into its parent.
$sequenceEnvironment=Save-TestProcessEnvironment
Remove-Item -LiteralPath Env:HOUDINI_PATH -ErrorAction SilentlyContinue
$env:CYCLES_HOUDINI_ROUTING_OVERRIDE='per-case inherited override'
& (Join-Path $base 'scripts/run-prototype-test.ps1') -Test 'fake.py' -Artifacts $selected -Report 'artifacts/fixture/report.json' | Out-Null
if($LASTEXITCODE -ne 0){throw 'Mock headless prototype failed'}
if(Test-Path -LiteralPath Env:HOUDINI_PATH){throw 'Headless restoration invented HOUDINI_PATH'}
if($env:CYCLES_HOUDINI_TEST_ARTIFACTS -ne $previous){throw 'Prototype leaked its selected artifact environment'}
$summary=Get-ChildItem -LiteralPath (Join-Path $selected 'verification') -Recurse -Filter summary.json
if(@($summary).Count -ne 1){throw 'Prototype did not write its unique selected evidence directory'}
$report=Join-Path $summary.Directory.FullName 'report.json'
if((Get-Content -LiteralPath $report -Raw | ConvertFrom-Json).result -ne 'passed'){throw 'Prototype missed selected-root fixture report'}
& (Join-Path $base 'scripts/run-prototype-test.ps1') -Test 'fake.py' -Artifacts $selected -Gui | Out-Null
if($LASTEXITCODE -ne 0){throw 'Mock GUI prototype failed'}
if(Test-Path -LiteralPath Env:HOUDINI_PATH){throw 'Nested GUI restoration leaked HOUDINI_PATH'}
if($env:CYCLES_HOUDINI_ROUTING_OVERRIDE -cne 'per-case inherited override'){throw 'Nested GUI restoration lost per-case override'}
$gui=Get-ChildItem -LiteralPath (Join-Path $selected 'verification') -Recurse -Filter fake.json
if(@($gui).Count -ne 1 -or $gui.FullName -notlike '*gui*'){throw 'Actual GUI report escaped selected prototype evidence'}
if(@($global:routingLaunches | Where-Object {$_.artifacts -ne $selected -or $_.prefs -notlike "$selected*"}).Count){throw 'Native writers or preferences escaped selected root'}
& (Join-Path $base 'scripts/run-prototype-test.ps1') -Test 'fake.py' -Artifacts $selected -Report 'artifacts/fixture/report.json' | Out-Null
if($LASTEXITCODE -ne 0 -or (Test-Path -LiteralPath Env:HOUDINI_PATH) -or $env:CYCLES_HOUDINI_ROUTING_OVERRIDE -cne 'per-case inherited override'){throw 'GUI followed by headless restoration changed caller environment'}
if(@($global:routingLaunches | Where-Object {$_.override -cne 'per-case inherited override'}).Count){throw 'A native child lost its inherited per-case override'}
$global:routingLaunchFailure=$true
try{
    & (Join-Path $base 'scripts/run-prototype-test.ps1') -Test 'fake.py' -Artifacts $selected -Gui | Out-Null
    if($LASTEXITCODE -eq 0 -or (Test-Path -LiteralPath Env:HOUDINI_PATH) -or $env:CYCLES_HOUDINI_ROUTING_OVERRIDE -cne 'per-case inherited override'){throw 'Failed GUI launch did not restore caller environment'}
}finally{Remove-Variable -Name routingLaunchFailure -Scope Global}
Restore-TestProcessEnvironment $sequenceEnvironment
@'
# Redirect OS defaults to this disposable test directory; retain actual launcher selection logic.
function Get-TestEvidenceBase {param($Artifacts='') if($Artifacts){$Artifacts}elseif($env:CYCLES_HOUDINI_TEST_ARTIFACTS){$env:CYCLES_HOUDINI_TEST_ARTIFACTS}else{Join-Path $env:CH_ROUTING_DEFAULT_BASE 'runs'}}
function Get-DefaultTestArtifactRoot {Join-Path $env:CH_ROUTING_DEFAULT_BASE 'shared'}
'@ | Add-Content -LiteralPath (Join-Path $base 'scripts/test-artifact-context.ps1')
$env:CH_ROUTING_DEFAULT_BASE=Join-Path $base 'defaults'
$env:CYCLES_HOUDINI_TEST_ARTIFACTS=$null
& (Join-Path $base 'scripts/run-prototype-test.ps1') -Test 'fake.py' -PackageDirectory 'development-packages' -Report 'artifacts/fixture/report.json' | Out-Null
if($LASTEXITCODE -ne 0 -or $global:routingLaunches[-1].artifacts -ne (Join-Path $env:CH_ROUTING_DEFAULT_BASE 'shared')){throw 'Explicit development package selected checkout evidence'}
$env:CYCLES_HOUDINI_SOURCE_ROOT=Join-Path $base 'installed-data'
$installedReport=Join-Path $env:CYCLES_HOUDINI_SOURCE_ROOT 'artifacts/fixture/report.json'
& (Join-Path $base 'scripts/run-prototype-test.ps1') -Test 'fake.py' -PackageDirectory 'installed' -Report $installedReport | Out-Null
if($LASTEXITCODE -ne 0 -or $global:routingLaunches[-1].artifacts -or -not(Test-Path -LiteralPath $installedReport)){throw 'Installed relocated data contract changed'}
$env:CYCLES_HOUDINI_TEST_ARTIFACTS=$previous
if(Test-Path -LiteralPath (Join-Path $base 'artifacts')){throw 'A runner wrote checkout artifacts'}
# Authored artist scenes keep their documented producer directory, while launch
# evidence is external. An explicitly opened old scene finds its companion OCIO.
foreach($folder in @('artifacts/examples/sop-fx-courtyard','examples/sop-fx-courtyard','fake-hfs/bin','old-artist-scene')){
    New-Item -ItemType Directory -Path (Join-Path $base $folder) -Force | Out-Null
}
foreach($file in @('artifacts/examples/sop-fx-courtyard/sop-fx-courtyard-adaptive.hiplc','artifacts/examples/sop-fx-courtyard/showcase.ocio','examples/sop-fx-courtyard/open_scene.py','fake-hfs/bin/houdini.exe','old-artist-scene/scene.hiplc','old-artist-scene/showcase.ocio')){
    Set-Content -LiteralPath (Join-Path $base $file) -Value '# mock file only'
}
$env:CYCLES_HOUDINI_TEST_ARTIFACTS=$null
& (Join-Path $base 'scripts/launch-sop-showcase.ps1') | Out-Null
if($global:routingLaunches[-1].artifacts -ne (Join-Path $base 'artifacts') -or $global:routingLaunches[-1].launch_logs -notlike ((Join-Path $base 'defaults/runs')+'*') -or $env:CYCLES_HOUDINI_TEST_ARTIFACTS){throw 'Showcase authored data or external launch evidence contract changed'}
& (Join-Path $base 'scripts/launch-sop-showcase.ps1') -SceneFile (Join-Path $base 'old-artist-scene/scene.hiplc') | Out-Null
if($global:routingLaunches[-1].ocio -ne (Join-Path $base 'old-artist-scene/showcase.ocio')){throw 'Explicit old showcase did not use companion color configuration'}
$env:CYCLES_HOUDINI_TEST_ARTIFACTS=$previous
Remove-Variable -Name routingLaunches -Scope Global
@{result='passed';native_processes_started=0;checks=@('selected shared producer root','fresh report lookup','actual GUI report routing','isolated preferences','prototype environment restored','no checkout evidence')} | ConvertTo-Json
