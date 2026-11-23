# SPDX-License-Identifier: Apache-2.0
# Exercise the actual broad runner with a fake context and mocked native launch.
$ErrorActionPreference='Stop'
$selectionRepo=Split-Path -Parent $PSScriptRoot
. (Join-Path $selectionRepo 'tests/support/artifacts.ps1')
$testArtifacts=Get-TestArtifactsRoot -SourceRoot $selectionRepo
$selectionBase=Join-Path $testArtifacts ('runner-selection/'+[guid]::NewGuid().ToString('N'))
foreach($selectionFolder in @('scripts','tests','build/current/host','build/current/packages','fake-hfs')){
    New-Item -ItemType Directory -Path (Join-Path $selectionBase $selectionFolder) -Force | Out-Null
}
Copy-Item -LiteralPath (Join-Path $selectionRepo 'scripts/run-tests.ps1') -Destination (Join-Path $selectionBase 'scripts/run-tests.ps1')
Copy-Item -LiteralPath (Join-Path $selectionRepo 'scripts/archive-test-manifest.ps1') -Destination (Join-Path $selectionBase 'scripts/archive-test-manifest.ps1')
foreach($dependency in @('native-test-context.ps1','native-test-arguments.ps1','test-dependency-context.ps1','native-test-tiers.ps1','native_test_plan.py','test-artifact-context.ps1','build-layout.ps1')){
    Copy-Item -LiteralPath (Join-Path $selectionRepo ('scripts/'+$dependency)) -Destination (Join-Path $selectionBase ('scripts/'+$dependency))
}
Set-Content -LiteralPath (Join-Path $selectionBase 'build/current/build-manifest.json') -Value '{"test_only":true}'
Set-Content -LiteralPath (Join-Path $selectionBase 'tests/material_override_evaluation_test.py') -Value '# dispatch target; never executed'
Set-Content -LiteralPath (Join-Path $selectionBase 'tests/native_test_entry.py') -Value '# native entry; never executed'
Copy-Item -LiteralPath (Join-Path $selectionRepo 'tests/requirements.txt') -Destination (Join-Path $selectionBase 'tests/requirements.txt')
Copy-Item -LiteralPath (Join-Path $selectionRepo 'tests/test-catalogue.json') -Destination (Join-Path $selectionBase 'tests/test-catalogue.json')
@'
function Get-NativeTestContext {
    param([string]$BuildDirectory='', [string]$HoudiniRoot='')
    $root=Split-Path -Parent $PSScriptRoot
    $build=Join-Path $root 'build/current'
    [pscustomobject]@{BuildDirectory=$build;HoudiniRoot=(Join-Path $root 'fake-hfs');
        Manifest=(Join-Path $build 'build-manifest.json');PackageDirectory=(Join-Path $build 'packages');
        Version='22.0.466';RuntimeIdentity=@{host=(Join-Path $build 'host');houdini='22.0.466';
            manifest_sha256=(Get-FileHash -LiteralPath (Join-Path $build 'build-manifest.json') -Algorithm SHA256).Hash.ToLowerInvariant()}}
}
'@ | Add-Content -LiteralPath (Join-Path $selectionBase 'scripts/native-test-context.ps1')
$global:cyclesSelectionMockLaunches=@()
function Start-Process {
    param($FilePath,$ArgumentList,$WindowStyle,[switch]$PassThru,$RedirectStandardOutput,$RedirectStandardError)
    if($global:cyclesSelectionMockFailure -eq 'launch'){throw 'mock launch failure'}
    $global:cyclesSelectionMockLaunches+=,[pscustomobject]@{file=$FilePath;arguments=@($ArgumentList)}
    [pscustomobject]@{Handle=0;ExitCode=0;Id=12345} |
        Add-Member -MemberType ScriptMethod -Name WaitForExit -Value {param($milliseconds) $true} -PassThru
}
$selectionRunner=Join-Path $selectionBase 'scripts/run-tests.ps1'
$selectionListed=@(& $selectionRunner -ListTests)
if($selectionListed.Count -lt 100 -or @($selectionListed | Where-Object {$_ -notmatch '^[a-zA-Z0-9_]+\.py$'}).Count){
    throw 'ListTests must return actual test filenames from the broad catalog'
}
if($global:cyclesSelectionMockLaunches.Count){throw 'ListTests dispatched a native process'}
$selectionOne=@(& $selectionRunner -Tests 'material_override_evaluation_test.py' -ListTests)
if($selectionOne.Count -ne 1 -or $selectionOne[0] -ne 'material_override_evaluation_test.py'){
    throw 'Explicit list selection did not preserve exactly the requested filename'
}
if($global:cyclesSelectionMockLaunches.Count){throw 'Selected ListTests dispatched a native process'}
$selectionDispatched=@(& $selectionRunner -Tests 'material_override_evaluation_test.py' -StopOnFailure)
if($global:cyclesSelectionMockLaunches.Count -ne 1){throw 'Single test selection must dispatch exactly one process'}
if($global:cyclesSelectionMockLaunches[0].arguments.Count -ne 2 -or
    $global:cyclesSelectionMockLaunches[0].arguments[0].Trim('"') -ne (Join-Path $selectionBase 'tests/native_test_entry.py') -or
    $global:cyclesSelectionMockLaunches[0].arguments[1].Trim('"') -ne (Join-Path $selectionBase 'tests/material_override_evaluation_test.py')){
    throw 'Selected dispatch did not target the requested test through the runtime entry point'
}
try{
    & $selectionRunner -Tests 'not_a_catalog_case.py' -ListTests | Out-Null
    throw 'Unknown selection was accepted'
}catch{
    if($_.Exception.Message -notlike 'Unknown or excluded test names:*'){throw}
}
if($global:cyclesSelectionMockLaunches.Count -ne 1){throw 'Unknown selection dispatched an extra native process'}
# Exact catalogue case selection must not expand CPU into an unwanted GPU run.
$catalogueTarget='houdini_panel_motion_timing_smoke.py'
Set-Content -LiteralPath (Join-Path $selectionBase "tests/$catalogueTarget") -Value '# never executed'
Set-Content -LiteralPath (Join-Path $selectionBase 'tests/houdini_motion_smoke.py') -Value '# producer; never executed'
$exactSelection=Join-Path $selectionBase 'selected-cases.json'
@(@{test=$catalogueTarget;device='CPU'}) | ConvertTo-Json -AsArray | Set-Content -LiteralPath $exactSelection
& $selectionRunner -Tier nightly -CatalogueSelectionFile $exactSelection -Artifacts (Join-Path $selectionBase 'exact-evidence') -ListTests | Out-Null
$exactSummaries=@(Get-ChildItem -LiteralPath (Join-Path $selectionBase 'exact-evidence') -Recurse -Filter summary.json)
$exact=Get-Content -LiteralPath $exactSummaries[0].FullName -Raw | ConvertFrom-Json
if($exact.scope -ne 'native-catalogue-selection' -or @($exact.plan).Count -ne 2 -or @($exact.plan | Where-Object device -ne 'CPU').Count -or $exact.plan[0].test -ne 'houdini_motion_smoke.py' -or $exact.plan[1].test -ne $catalogueTarget){throw 'Exact catalogue selection did not retain only the selected backend and its producer'}
@(@{test=$catalogueTarget;device='INVALID'}) | ConvertTo-Json -AsArray | Set-Content -LiteralPath $exactSelection
try{
    & $selectionRunner -Tier nightly -CatalogueSelectionFile $exactSelection -Artifacts (Join-Path $selectionBase 'invalid-evidence') -ListTests | Out-Null
    throw 'Invalid catalogue device accepted'
}catch{if($_.Exception.Message -ne 'Native Python tier planning failed.'){throw}}
$global:cyclesSelectionMockFailure='launch'
try{
    & $selectionRunner -Tests 'material_override_evaluation_test.py' -Artifacts (Join-Path $selectionBase 'launch-failed') | Out-Null
    throw 'Mock launch failure accepted'
}catch{if($_.Exception.Message -ne 'mock launch failure'){throw}}
$failedSummary=Get-ChildItem -LiteralPath (Join-Path $selectionBase 'launch-failed') -Recurse -Filter summary.json
$failed=Get-Content -LiteralPath $failedSummary.FullName -Raw | ConvertFrom-Json
if($failed.result -ne 'failed' -or -not $failed.finished_utc -or $failed.runner_error -ne 'mock launch failure'){throw 'Launch failure left a running or incomplete summary'}
if(@(Get-ChildItem -LiteralPath (Join-Path $selectionBase 'launch-failed') -Recurse -Filter '*.tmp').Count){throw 'Atomic summary writes left an unfinished temporary file'}
Remove-Variable -Name cyclesSelectionMockFailure -Scope Global
Remove-Variable -Name cyclesSelectionMockLaunches -Scope Global
[pscustomobject]@{result='passed';catalog_count=$selectionListed.Count;
    checks=@('Actual ListTests filenames','ListTests never dispatches','Selected ListTests has one filename',
        'Selected execution dispatches exactly one mocked process','Unknown selection rejected before launch');
    native_processes_started=0} | ConvertTo-Json -Depth 4
