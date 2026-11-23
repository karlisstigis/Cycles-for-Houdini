# SPDX-License-Identifier: Apache-2.0
[CmdletBinding()]
param(
    [string]$Test='houdini_ipr_smoke.py',
    [string]$Device='CPU',
    [string]$RunName='',
    [string]$HoudiniRoot='',
    [string]$BuildDirectory='',
    [string]$PackageDirectory='',
    [string]$UserPrefDirectory='',
    [string]$EvidenceDirectory='',
    [string]$Artifacts='',
    [int]$TimeoutSeconds=120,
    [switch]$DebugCrash
)
$ErrorActionPreference='Stop'
$taskRoot=Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'native-test-context.ps1')
. (Join-Path $PSScriptRoot 'test-artifact-context.ps1')
$context=Get-NativeTestContext -BuildDirectory $BuildDirectory -HoudiniRoot $HoudiniRoot -PackageDirectory $PackageDirectory
$HoudiniRoot=$context.HoudiniRoot
$testPath=(Resolve-Path -LiteralPath (Join-Path $taskRoot "tests/$Test")).Path
if(-not $testPath.StartsWith((Join-Path $taskRoot 'tests')+[IO.Path]::DirectorySeparatorChar,[StringComparison]::OrdinalIgnoreCase)){throw 'GUI test must be inside this workspace tests directory.'}
$guiRoot=if($EvidenceDirectory){[IO.Path]::GetFullPath($EvidenceDirectory)}else{Join-Path (Get-TestEvidenceBase -Artifacts $Artifacts) 'gui'}
if(-not $RunName){$RunName=([IO.Path]::GetFileNameWithoutExtension($Test))+'-'+($Device -replace '[^a-zA-Z0-9_-]','_')+'-'+[DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfff')}
if($RunName -notmatch '^[a-zA-Z0-9][a-zA-Z0-9_-]*$'){throw 'RunName must contain only letters, numbers, underscore, and hyphen.'}
$destination=Join-Path $guiRoot $RunName
if(Test-Path -LiteralPath $destination){throw "GUI evidence directory already exists: $destination"}
New-Item -ItemType Directory -Path $destination -Force | Out-Null
$previousTestEnvironment=Save-TestProcessEnvironment
try{
if($Artifacts){$env:CYCLES_HOUDINI_TEST_ARTIFACTS=[IO.Path]::GetFullPath($Artifacts)}
elseif(-not $env:CYCLES_HOUDINI_TEST_ARTIFACTS -and -not $context.RuntimeIdentity.release){$env:CYCLES_HOUDINI_TEST_ARTIFACTS=Get-DefaultTestArtifactRoot}
$env:HOUDINI_PACKAGE_DIR=$context.PackageDirectory
$env:HOUDINI_PATH='&'
$env:HOUDINI_USER_PREF_DIR=if($UserPrefDirectory){[IO.Path]::GetFullPath($UserPrefDirectory)}else{Join-Path $destination 'prefs/houdini__HVER__'}
if($env:HOUDINI_USER_PREF_DIR -notlike '*__HVER__*'){throw 'Use an isolated preferences path containing __HVER__.'}
$env:HOUDINI_NO_ENV_FILE='1'
$env:HOUDINI_NO_STARTPAGE='1'
$env:HOUDINI_DSO_ERROR='2'
$env:CYCLES_HOUDINI_GUI_TEST=$testPath
$env:CYCLES_HOUDINI_GUI_DEVICE=$Device
$env:CYCLES_HOUDINI_GUI_REPORT=Join-Path $destination "$([IO.Path]::GetFileNameWithoutExtension($Test)).json"
$env:CYCLES_HOUDINI_TEST_IDENTITY=$context.RuntimeIdentity | ConvertTo-Json -Compress
$env:CYCLES_HOUDINI_TEST_IDENTITY_REPORT=Join-Path $destination 'runtime-identity.json'
$bootstrap=(Join-Path $taskRoot 'tests/gui_startup/bootstrap.py').Replace('\','/')
$process=Start-Process -FilePath (Join-Path $HoudiniRoot 'bin/houdini.exe') -ArgumentList @('-foreground','-indie','waitforui', ('"'+$bootstrap+'"')) -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $destination 'stdout.log') -RedirectStandardError (Join-Path $destination 'stderr.log')
# Keep the native handle open before the process exits. Windows PowerShell can
# otherwise lose ExitCode when a redirected process finishes between queries.
$nativeProcessHandle=$process.Handle
if($DebugCrash){
    $debugger=Join-Path $context.BuildDirectory 'bin/ch_gui_debugger.exe'
    if(-not(Test-Path -LiteralPath $debugger)){throw 'Build ch_gui_debugger before using -DebugCrash.'}
    $debugLog=Join-Path $destination "$([IO.Path]::GetFileNameWithoutExtension($Test)).stack.log"
    $debugProcess=Start-Process -FilePath $debugger -ArgumentList @($process.Id,('"'+$debugLog+'"')) -WindowStyle Hidden -PassThru
}
@{result='starting';pid=$process.Id;test=$testPath} | ConvertTo-Json | Set-Content -LiteralPath $env:CYCLES_HOUDINI_GUI_REPORT -Encoding utf8
$finished=$process.WaitForExit($TimeoutSeconds*1000)
if(-not $finished){
    if($process.GetType().GetMethod('Kill',[type[]]@([bool]))){$process.Kill($true)}
    else{& (Join-Path $env:SystemRoot 'System32/taskkill.exe') /PID $process.Id /T /F | Out-Null}
    $process.WaitForExit()
    $report=Get-Content -LiteralPath $env:CYCLES_HOUDINI_GUI_REPORT -Raw | ConvertFrom-Json
    $report | Add-Member -NotePropertyName result -NotePropertyValue 'failed' -Force
    $report | Add-Member -NotePropertyName runner_error -NotePropertyValue "GUI process did not exit within $TimeoutSeconds seconds" -Force
    $report | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $env:CYCLES_HOUDINI_GUI_REPORT -Encoding utf8
    throw "GUI test exceeded $TimeoutSeconds seconds. See $destination."
}
$report=Get-Content -LiteralPath $env:CYCLES_HOUDINI_GUI_REPORT -Raw | ConvertFrom-Json
$exitCode=$process.ExitCode
if($null -eq $exitCode){
    $exitCode=1
    $report | Add-Member -NotePropertyName runner_error -NotePropertyValue 'The native process exit code was unavailable; this run cannot pass.' -Force
}
$report | Add-Member -NotePropertyName process_exit_code -NotePropertyValue $exitCode -Force
if($exitCode -ne 0){$report | Add-Member -NotePropertyName result -NotePropertyValue 'failed' -Force}
if($report.PSObject.Properties.Name -contains 'quit_requested_unix' -and $report.result -eq 'passed'){
    $epoch=[DateTimeOffset]::FromUnixTimeSeconds(0).UtcDateTime
    $elapsed=($process.ExitTime.ToUniversalTime()-$epoch).TotalSeconds-[double]$report.quit_requested_unix
    $workerAlive=[bool](Get-Process -Id $report.worker_pid -ErrorAction SilentlyContinue)
    $report | Add-Member -NotePropertyName quit_to_exit_seconds -NotePropertyValue $elapsed
    $report | Add-Member -NotePropertyName worker_alive_after_exit -NotePropertyValue $workerAlive
    if($elapsed -lt 0 -or $elapsed -gt 10 -or $workerAlive){
        $report.result='failed'
        $report | Add-Member -NotePropertyName runner_error -NotePropertyValue 'Native shutdown exceeded ten seconds or left its render worker running.'
    }
}
$report | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $env:CYCLES_HOUDINI_GUI_REPORT -Encoding utf8
@{result=$report.result;exit_code=$exitCode;report=$env:CYCLES_HOUDINI_GUI_REPORT;error=$report.error} | ConvertTo-Json -Compress
Get-Content -LiteralPath (Join-Path $destination 'stderr.log') | Select-Object -Last 30
if($exitCode -ne 0){exit $exitCode}
if($report.result -ne 'passed'){exit 1}
exit 0

}finally{Restore-TestProcessEnvironment $previousTestEnvironment}
