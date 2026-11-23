# SPDX-License-Identifier: Apache-2.0
[CmdletBinding()]
param(
    [string]$Test='houdini_queued_worker_smoke.py',
    [string]$Device='CPU',
    [ValidateSet('sync','worker')][string]$Execution='sync',
    [string]$Report='',
    [string]$Artifacts='',
    [string[]]$TestArguments=@(),
    [string]$PackageDirectory='',
    [string]$UserPrefDirectory='',
    [switch]$Gui,
    [int]$TimeoutSeconds=180,
    [string]$HoudiniRoot='',
    [string]$BuildDirectory=''
)
$ErrorActionPreference='Stop'
if($TimeoutSeconds -lt 1){throw 'TimeoutSeconds must be positive.'}
$taskRoot=Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'native-test-context.ps1')
. (Join-Path $PSScriptRoot 'archive-test-manifest.ps1')
. (Join-Path $PSScriptRoot 'test-artifact-context.ps1')
. (Join-Path $PSScriptRoot 'native-test-arguments.ps1')
$context=Get-NativeTestContext -BuildDirectory $BuildDirectory -HoudiniRoot $HoudiniRoot -PackageDirectory $PackageDirectory
$HoudiniRoot=$context.HoudiniRoot
$testPath=(Resolve-Path -LiteralPath (Join-Path $taskRoot "tests/$Test")).Path
if(-not $testPath.StartsWith((Join-Path $taskRoot 'tests')+[IO.Path]::DirectorySeparatorChar,[StringComparison]::OrdinalIgnoreCase)){throw 'Prototype test must stay inside the workspace tests directory.'}
$name='prototype-'+$Execution+'-'+($Device -replace '[^a-zA-Z0-9_-]','_')+'-'+[DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfff')
$destination=Join-Path (Get-TestEvidenceBase -Artifacts $Artifacts) "verification/$name"
New-Item -ItemType Directory -Path $destination | Out-Null
$taskOutputLog=Join-Path $destination 'output.log'
# A successful native test may be completely silent. An empty pipeline does
# not create Set-Content's target, so establish the owned log before launching.
[IO.File]::WriteAllText($taskOutputLog,'',[Text.UTF8Encoding]::new($false))
$manifest=$context.Manifest
$manifestDigest=Save-NativeTestManifest -Context $context -Destination (Join-Path $destination 'build-manifest.json')
$entry=[ordered]@{test=$Test;device=$Device;execution=$Execution;arguments=@($TestArguments);package_directory=$PackageDirectory;user_pref_directory=$UserPrefDirectory;result='running';test_sha256=(Get-FileHash -LiteralPath $testPath -Algorithm SHA256).Hash}
$summary=[ordered]@{started_utc=[DateTime]::UtcNow.ToString('o');result='running';scope='native-execution-prototype';build_directory=$context.BuildDirectory;build_manifest_sha256=$manifestDigest;tests=@($entry)}
$summaryPath=Join-Path $destination 'summary.json'
$summary | ConvertTo-Json -Depth 16 | Set-Content -LiteralPath $summaryPath -Encoding utf8
$previousTestEnvironment=Save-TestProcessEnvironment
$previousArtifacts=$env:CYCLES_HOUDINI_TEST_ARTIFACTS
$previousWorkers=$env:CYCLES_HOUDINI_WORKER_LOG_DIR
# Explicit installed packages own their relocated data root. Preserve that
# release/farm contract unless the caller selects a test artifact root.
if($Artifacts){$env:CYCLES_HOUDINI_TEST_ARTIFACTS=[IO.Path]::GetFullPath($Artifacts)}
elseif(-not $env:CYCLES_HOUDINI_TEST_ARTIFACTS -and -not $context.RuntimeIdentity.release){$env:CYCLES_HOUDINI_TEST_ARTIFACTS=Get-DefaultTestArtifactRoot}
if($env:CYCLES_HOUDINI_TEST_ARTIFACTS){$env:CYCLES_HOUDINI_WORKER_LOG_DIR=Join-Path $env:CYCLES_HOUDINI_TEST_ARTIFACTS 'workers'}
$previousExecution=$env:CYCLES_HOUDINI_EXECUTION
$previousPackage=$env:HOUDINI_PACKAGE_DIR
$previousUserPref=$env:HOUDINI_USER_PREF_DIR
$previousDsoError=$env:HOUDINI_DSO_ERROR
$runStarted=[DateTime]::UtcNow
try{
    $env:CYCLES_HOUDINI_EXECUTION=$Execution
    if($Gui){
        if($TestArguments.Count){throw 'GUI test arguments use explicit environment settings.'}
        & (Join-Path $PSScriptRoot 'run-gui-test.ps1') -Test $Test -Device $Device -RunName $name -TimeoutSeconds $TimeoutSeconds -HoudiniRoot $HoudiniRoot -BuildDirectory $context.BuildDirectory -PackageDirectory $context.PackageDirectory -UserPrefDirectory $UserPrefDirectory -EvidenceDirectory (Join-Path $destination 'gui') *> (Join-Path $destination 'output.log')
        $code=$LASTEXITCODE
        $guiReport=Join-Path $destination "gui/$name/$([IO.Path]::GetFileNameWithoutExtension($Test)).json"
        if(-not(Test-Path -LiteralPath $guiReport)){throw 'The GUI test report is missing.'}
        Copy-Item -LiteralPath $guiReport -Destination (Join-Path $destination 'report.json')
        $guiOutcome=Get-Content -LiteralPath $guiReport -Raw | ConvertFrom-Json
        if($guiOutcome.result -ne 'passed' -or $null -eq $guiOutcome.process_exit_code -or $guiOutcome.process_exit_code -ne 0){$code=1}
    }else{
        $env:HOUDINI_PACKAGE_DIR=$context.PackageDirectory
        # Isolated preferences by default: an installed Cycles package in the
        # artist's Houdini preferences must never replace the build under test.
        $env:HOUDINI_USER_PREF_DIR=if($UserPrefDirectory){[IO.Path]::GetFullPath($UserPrefDirectory)}else{Join-Path $destination 'prefs/houdini__HVER__'}
        if($env:HOUDINI_USER_PREF_DIR -notlike '*__HVER__*'){throw 'Use an isolated preferences path containing __HVER__.'}
        $env:HOUDINI_DSO_ERROR='2'
        $env:CYCLES_HOUDINI_TEST_IDENTITY=$context.RuntimeIdentity | ConvertTo-Json -Compress
        $env:CYCLES_HOUDINI_TEST_IDENTITY_REPORT=Join-Path $destination 'runtime-identity.json'
        $taskArguments=@((Join-Path $taskRoot 'tests/native_test_entry.py'),$testPath,'--device',$Device)+@($TestArguments)
        $taskArguments=@($taskArguments | ForEach-Object {Quote-NativeArgument $_})
        $taskProcess=Start-Process -FilePath (Join-Path $HoudiniRoot 'bin/hython.exe') -ArgumentList $taskArguments -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $destination 'stdout.log') -RedirectStandardError (Join-Path $destination 'stderr.log')
        # Retain the handle: Windows can otherwise lose ExitCode after exit.
        $taskNativeHandle=$taskProcess.Handle
        if(-not $taskProcess.WaitForExit($TimeoutSeconds*1000)){
            if($taskProcess.GetType().GetMethod('Kill',[type[]]@([bool]))){$taskProcess.Kill($true)}
            else{& (Join-Path $env:SystemRoot 'System32/taskkill.exe') /PID $taskProcess.Id /T /F | Out-Null}
            $taskProcess.WaitForExit();$code=1;$entry.error="Headless test exceeded $TimeoutSeconds seconds."
        }else{
            $code=$taskProcess.ExitCode
            if($null -eq $code){$code=1;$entry.error='The native process exit code was unavailable.'}
        }
        Get-Content -LiteralPath (Join-Path $destination 'stdout.log'),(Join-Path $destination 'stderr.log') | Set-Content -LiteralPath (Join-Path $destination 'output.log') -Encoding utf8
        $report=(Resolve-NativeTestReport -Report $(if($Report){$Report}else{'artifacts/stage1/queued-workers/houdini-queued-worker-smoke.json'}) -SourceRoot $taskRoot -ArtifactsRoot $env:CYCLES_HOUDINI_TEST_ARTIFACTS).Path
        if($code -eq 0){
            if(-not(Test-Path -LiteralPath $report -PathType Leaf)){throw "Test exited successfully but its report is missing: $report"}
            if((Get-Item -LiteralPath $report).LastWriteTimeUtc -lt $runStarted){throw "Test exited successfully but its report predates this run: $report"}
        }
        if((Test-Path -LiteralPath $report -PathType Leaf) -and (Get-Item -LiteralPath $report).LastWriteTimeUtc -ge $runStarted){
            Copy-Item -LiteralPath $report -Destination (Join-Path $destination 'report.json')
        }elseif($code -ne 0){
            # A previous successful report cannot describe this failed process.
            [ordered]@{result='failed';process_exit_code=$code;error='Native test failed before producing a current report. See output.log.';output_log='output.log'} |
                ConvertTo-Json | Set-Content -LiteralPath (Join-Path $destination 'report.json') -Encoding utf8
        }
    }
}catch{
    $code=1;$entry.error=$_.Exception.Message
    Add-Content -LiteralPath $taskOutputLog -Value ('Runner failure: '+$entry.error) -Encoding utf8
    if(-not(Test-Path -LiteralPath (Join-Path $destination 'report.json') -PathType Leaf)){
        [ordered]@{result='failed';process_exit_code=$code;error=$entry.error;output_log='output.log'} |
            ConvertTo-Json | Set-Content -LiteralPath (Join-Path $destination 'report.json') -Encoding utf8
    }
}finally{
    $env:CYCLES_HOUDINI_TEST_ARTIFACTS=$previousArtifacts
    $env:CYCLES_HOUDINI_WORKER_LOG_DIR=$previousWorkers
    $env:CYCLES_HOUDINI_EXECUTION=$previousExecution
    $env:HOUDINI_PACKAGE_DIR=$previousPackage
    $env:HOUDINI_USER_PREF_DIR=$previousUserPref
    $env:HOUDINI_DSO_ERROR=$previousDsoError
    Restore-TestProcessEnvironment $previousTestEnvironment
    $entry.result=if($code -eq 0){'passed'}else{'failed'};$entry.exit_code=$code
    $summary.result=$entry.result;$summary.finished_utc=[DateTime]::UtcNow.ToString('o')
    $summary | ConvertTo-Json -Depth 16 | Set-Content -LiteralPath $summaryPath -Encoding utf8
    # Audit immediately, before any subsequent implementation/document edits.
    python (Join-Path $PSScriptRoot 'verify-verification.py') $destination
    if($LASTEXITCODE -ne 0){
        $entry.verification_error='The verification audit rejected this result. See output.log; the original report is retained.'
        $entry.result='failed';$summary.result='failed';$code=1
        $summary | ConvertTo-Json -Depth 16 | Set-Content -LiteralPath $summaryPath -Encoding utf8
        # A rejected verification still needs a source audit before edits.
        python (Join-Path $PSScriptRoot 'verify-verification.py') $destination
    }
}
Get-Content -LiteralPath $taskOutputLog | Select-Object -Last 24
@{result=$summary.result;report=(Join-Path $destination 'report.json')} | ConvertTo-Json -Compress
exit $code
