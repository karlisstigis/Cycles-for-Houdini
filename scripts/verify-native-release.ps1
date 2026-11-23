# SPDX-License-Identifier: Apache-2.0
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$Cases,
    [string]$Name='candidate',
    [string]$Artifacts='',
    [int]$TimeoutSeconds=900,
    [string]$BuildDirectory='',
    [string]$HoudiniRoot=''
)
$ErrorActionPreference='Stop'
$taskRoot=Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'native-test-context.ps1')
. (Join-Path $PSScriptRoot 'archive-test-manifest.ps1')
. (Join-Path $PSScriptRoot 'test-artifact-context.ps1')
. (Join-Path $PSScriptRoot 'native-test-arguments.ps1')
$context=Get-NativeTestContext -BuildDirectory $BuildDirectory -HoudiniRoot $HoudiniRoot
if($Name -notmatch '^[a-zA-Z0-9][a-zA-Z0-9_-]*$'){throw 'Name must contain letters, numbers, underscores and hyphens.'}
if($TimeoutSeconds -lt 1){throw 'TimeoutSeconds must be positive.'}
$taskCases=@(Get-Content -LiteralPath $Cases -Raw | ConvertFrom-Json)
if(-not $taskCases.Count){throw 'At least one explicit test case is required.'}
# Validate every selection before starting a native process.
foreach($taskCase in $taskCases){
    if($taskCase.test -notmatch '^[a-zA-Z0-9_]+\.py$' -or -not(Test-Path -LiteralPath (Join-Path $taskRoot "tests/$($taskCase.test)"))){throw "Unknown test: $($taskCase.test)"}
    if(-not $taskCase.device){throw 'Every case needs an explicit device or JSON device array.'}
    if($taskCase.execution -and $taskCase.execution -notin @('sync','worker')){throw 'Verification execution must be sync or worker.'}
    if(-not $taskCase.gui -and ($taskCase.report -notlike 'artifacts/*' -or $taskCase.report -match '(^|[/\\])\.\.([/\\]|$)')){throw 'Headless cases need a workspace artifacts report path.'}
    if($taskCase.environment){foreach($taskProperty in $taskCase.environment.PSObject.Properties){
        if($taskProperty.Name -notmatch '^CYCLES_HOUDINI_[A-Z0-9_]+$'){throw "Unsupported benchmark environment key: $($taskProperty.Name)"}
    }}
}
$taskRun='native-'+$Name+'-'+[DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfff')
$taskEvidenceRoot=Get-TestEvidenceBase -Artifacts $Artifacts
$taskDestination=Join-Path $taskEvidenceRoot "verification/$taskRun"
$taskFixtureRoot=if($Artifacts){[IO.Path]::GetFullPath($Artifacts)}elseif($env:CYCLES_HOUDINI_TEST_ARTIFACTS){$env:CYCLES_HOUDINI_TEST_ARTIFACTS}else{Join-Path $taskDestination 'fixtures'}
New-Item -ItemType Directory -Path $taskDestination | Out-Null
$taskManifest=$context.Manifest
$taskIdentity=Save-NativeTestManifest -Context $context -Destination (Join-Path $taskDestination 'build-manifest.json')
$taskSummary=[ordered]@{result='running';scope='Selected native release verification';build_directory=$context.BuildDirectory;manifest_sha256=$taskIdentity;tests=@()}
$taskPreviousIndexing=$env:HOUDINI_DISABLE_BACKGROUND_HELP_INDEXING
$env:HOUDINI_DISABLE_BACKGROUND_HELP_INDEXING='1'
try{
    for($taskIndex=0;$taskIndex -lt $taskCases.Count;$taskIndex++){
        if((Get-FileHash -LiteralPath $taskManifest -Algorithm SHA256).Hash.ToLowerInvariant() -cne $taskIdentity){throw 'Build identity changed during the verification queue.'}
        $taskCase=$taskCases[$taskIndex];$taskPrevious=@{}
        try{
            if($taskCase.environment){foreach($taskProperty in $taskCase.environment.PSObject.Properties){
                $taskPrevious[$taskProperty.Name]=[Environment]::GetEnvironmentVariable($taskProperty.Name,'Process')
                [Environment]::SetEnvironmentVariable($taskProperty.Name,[string]$taskProperty.Value,'Process')
            }}
            $taskExecution=if($taskCase.execution){[string]$taskCase.execution}else{'worker'}
            $taskArguments=@{Test=$taskCase.test;Device=$taskCase.device;Execution=$taskExecution;TimeoutSeconds=$TimeoutSeconds;HoudiniRoot=$context.HoudiniRoot;BuildDirectory=$context.BuildDirectory;Artifacts=$taskFixtureRoot}
            if($taskCase.arguments){
                $taskArguments.TestArguments=@(Get-NativeReleaseArguments -Arguments $taskCase.arguments -Report $taskCase.report -SourceRoot $taskRoot -ArtifactsRoot $taskFixtureRoot)
            }
            if($taskCase.package_directory){$taskArguments.PackageDirectory=$taskCase.package_directory}
            if($taskCase.user_pref_directory){$taskArguments.UserPrefDirectory=$taskCase.user_pref_directory}
            if($taskCase.gui){$taskArguments.Gui=$true}else{$taskArguments.Report=$taskCase.report}
            $taskLog=Join-Path $taskDestination "$taskIndex.log"
            & (Join-Path $PSScriptRoot 'run-prototype-test.ps1') @taskArguments *> $taskLog
            $taskCode=$LASTEXITCODE
            $taskResult=Get-Content -LiteralPath $taskLog -Tail 1 | ConvertFrom-Json
            $taskChild=Split-Path -Parent $taskResult.report
            & python (Join-Path $PSScriptRoot 'archive-native-verification.py') $taskChild --destination (Join-Path $taskDestination 'evidence') --evidence-root $taskEvidenceRoot *> (Join-Path $taskDestination "$taskIndex.archive.json")
            if($LASTEXITCODE -ne 0){throw 'Source archival failed; preserve this run before editing sources.'}
            $taskChildRelative=if($taskChild.StartsWith($taskRoot+[IO.Path]::DirectorySeparatorChar,[StringComparison]::OrdinalIgnoreCase)){[IO.Path]::GetRelativePath($taskRoot,$taskChild).Replace('\','/')}else{[IO.Path]::GetFullPath($taskChild).Replace('\','/')}
            $taskArchivedChild=Assert-NativeTestArchive -ManifestSha256 $taskIdentity -ArchiveIndex (Join-Path $taskDestination 'evidence/index.json') -ExpectedRun $taskChildRelative
            if((Get-FileHash -LiteralPath $taskManifest -Algorithm SHA256).Hash.ToLowerInvariant() -cne $taskIdentity){throw 'Build identity changed during the verification queue.'}
            $taskSummary.tests+=@([ordered]@{test=$taskCase.test;device=$taskCase.device;execution=$taskExecution;gui=[bool]$taskCase.gui;exit_code=$taskCode;report=$taskResult.report;manifest_sha256=$taskArchivedChild.manifest_sha256;source_archive=$taskArchivedChild.source_archive})
            $taskSummary | ConvertTo-Json -Depth 16 | Set-Content -LiteralPath (Join-Path $taskDestination 'summary.json') -Encoding utf8
            $taskSummary.tests[-1] | ConvertTo-Json -Compress
            if($taskCode -ne 0){throw "Native test failed: $($taskCase.test)"}
        }finally{
            foreach($taskKey in $taskPrevious.Keys){[Environment]::SetEnvironmentVariable($taskKey,$taskPrevious[$taskKey],'Process')}
        }
    }
    $taskSummary.result='passed'
}catch{
    $taskSummary.result='failed';$taskSummary.error=$_.Exception.Message
}finally{
    $env:HOUDINI_DISABLE_BACKGROUND_HELP_INDEXING=$taskPreviousIndexing
    $taskSummary | ConvertTo-Json -Depth 16 | Set-Content -LiteralPath (Join-Path $taskDestination 'summary.json') -Encoding utf8
}
@{result=$taskSummary.result;report=(Join-Path $taskDestination 'summary.json')} | ConvertTo-Json -Compress
if($taskSummary.result -ne 'passed'){exit 1}
