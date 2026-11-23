# SPDX-License-Identifier: Apache-2.0
[CmdletBinding()]
param(
    [string]$HoudiniTarget='',
    [string]$HoudiniRoot='',
    [string]$BuildDirectory='',
    [string]$OutputDirectory='',
    [string]$BaselineManifest='',
    [string]$RebaseApproval='',
    [string]$ExistingHip='',
    [string]$RollbackPackage='',
    [string]$QualifiedNightly='',
    [string]$FastCIReceipt='',
    [switch]$FocusedChecks,
    [switch]$DryRun,
    [string]$OptixDevice=$env:CYCLES_HOUDINI_TEST_OPTIX_DEVICE,
    [string]$ReferenceBlender='',
    [switch]$ChooseRollbackPackage,
    [int]$Jobs=6
)
$ErrorActionPreference='Stop'
if($ReferenceBlender){$ReferenceBlender=(Get-Item -LiteralPath $ReferenceBlender -ErrorAction Stop).FullName}
. (Join-Path $PSScriptRoot 'build-layout.ps1')
$root=Split-Path -Parent $PSScriptRoot
$lock=Get-Content -LiteralPath (Join-Path $root 'source.lock.json') -Raw | ConvertFrom-Json
$target=$lock.certification_candidate
# The official release route needs the private engine fork; a public-route checkout would only
# fail at packaging, after the build. Community builds use scripts/easy-make-installer.ps1.
$engineRoute=& python -B (Join-Path $PSScriptRoot 'engine-fork.py') route 2>$null
$engineRouteResult=$LASTEXITCODE
if($engineRouteResult -eq 0 -and "$engineRoute".Trim() -eq 'public'){throw 'This is the official release route, which needs the private engine fork (third_party/cycles comes from the public route here). To make an installer of your own build, double-click "START HERE - Build Cycles for Houdini.cmd": it builds and makes an installer for each Houdini build (scripts/easy-make-installer.ps1).'}
if($DryRun -and ($engineRouteResult -ne 0 -or "$engineRoute".Trim() -ne 'private')){throw 'The official release plan requires the locked private engine checkout.'}
if(-not $HoudiniTarget){$HoudiniTarget=$target.houdini}
if($HoudiniTarget -ne $target.houdini -or $HoudiniTarget -ne $lock.sdk_dependencies.openimagedenoise.houdini){throw "Current source/SDK lock targets $($target.houdini). Update and qualify the SDK record for another target."}
if(-not $HoudiniRoot){$HoudiniRoot=Join-Path $env:ProgramFiles ('Side Effects Software/Houdini '+$HoudiniTarget)}
$HoudiniRoot=(Resolve-Path -LiteralPath $HoudiniRoot).Path
$header=Get-Content -LiteralPath (Join-Path $HoudiniRoot 'toolkit/include/SYS/SYS_Version.h') -Raw
$api=Get-Content -LiteralPath (Join-Path $HoudiniRoot 'toolkit/include/UT/UT_HDKVersion.h') -Raw
if($header -notmatch ('(?m)^#define SYS_VERSION_FULL "'+[regex]::Escape($HoudiniTarget)+'"') -or $api -notmatch ('(?m)^#define HDK_API_VERSION\s+'+[regex]::Escape([string]$target.houdini_api)+'\b')){throw 'SDK header version/API differs from the exact qualified target.'}
if(-not $BuildDirectory){$BuildDirectory=Get-DefaultBuildDirectory -SourceRoot $root -HoudiniVersion $HoudiniTarget}
$BuildDirectory=[IO.Path]::GetFullPath($BuildDirectory)
. (Join-Path $PSScriptRoot 'release-layout.ps1')
if(-not $OutputDirectory){$OutputDirectory=Join-Path (Get-ReleaseEvidenceRoot $root) ($HoudiniTarget+'-'+[DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfff'))}
$OutputDirectory=[IO.Path]::GetFullPath($OutputDirectory)
if(Test-Path -LiteralPath $OutputDirectory){throw 'Release output must be new; never mix reports or replace an earlier candidate.'}
if(-not $ExistingHip){$ExistingHip=Join-Path $root 'tests/fixtures/release-existing.hiplc'}
if($BaselineManifest){$BaselineManifest=(Resolve-Path -LiteralPath $BaselineManifest).Path}
if($RebaseApproval){$RebaseApproval=(Resolve-Path -LiteralPath $RebaseApproval).Path}
$ExistingHip=(Resolve-Path -LiteralPath $ExistingHip).Path
if(-not $RollbackPackage -and $ChooseRollbackPackage){
    if($DryRun){throw 'A dry run cannot open the package chooser. Pass -RollbackPackage with the archive path.'}
    Add-Type -AssemblyName System.Windows.Forms
    $picker=[Windows.Forms.OpenFileDialog]::new()
    try{
        $picker.Title="Select the previous qualified Houdini $HoudiniTarget release package for rollback"
        $picker.Filter='Cycles release package (*.zip)|*.zip'
        $picker.CheckFileExists=$true;$picker.Multiselect=$false
        if($picker.ShowDialog() -ne [Windows.Forms.DialogResult]::OK){throw 'No rollback package selected; no release build started.'}
        $RollbackPackage=$picker.FileName
    }finally{$picker.Dispose()}
}
if($RollbackPackage){$RollbackPackage=(Resolve-Path -LiteralPath $RollbackPackage).Path}
if($BaselineManifest){
    $baseline=Get-Content -LiteralPath $BaselineManifest -Raw | ConvertFrom-Json
    if($baseline.host_sdk.houdini -ne $HoudiniTarget -or [string]$baseline.host_sdk.hdk_api -ne [string]$target.houdini_api){throw 'Explicit baseline must belong to the exact release SDK/API.'}
}
$cachePath=Join-Path $BuildDirectory 'CMakeCache.txt'
if(Test-Path -LiteralPath $cachePath){
    $oldCache=@{};foreach($line in Get-Content -LiteralPath $cachePath){if($line -match '^([^/#][^:]*):[^=]+=(.*)$'){$oldCache[$Matches[1]]=$Matches[2]}}
    if([IO.Path]::GetFullPath($oldCache['HOUDINI_ROOT']) -ne $HoudiniRoot){throw 'Build cache belongs to a different Houdini SDK.'}
    if($oldCache['CH_BDPT_PROTOTYPE'] -in @('ON','TRUE','1') -or [IO.Path]::GetFullPath($oldCache['CH_ENGINE_ROOT']) -ne [IO.Path]::GetFullPath((Join-Path $root 'third_party/cycles'))){throw 'Release cache must use the canonical production engine, without experimental overlays.'}
    if($oldCache['CH_DLSS_PROJECT_ID']){throw 'This build folder was configured by the community steps with another DLSS project id (CH_DLSS_PROJECT_ID). Use another -BuildDirectory for an official release, or clear it first: scripts/build.ps1 -BuildDirectory <it> -DlssProjectId none.'}
}
if($DryRun){
    if($QualifiedNightly){$QualifiedNightly=(Resolve-Path -LiteralPath $QualifiedNightly).Path}
    if($FastCIReceipt){$FastCIReceipt=(Resolve-Path -LiteralPath $FastCIReceipt).Path}
    [ordered]@{
        result='planned'; houdini=$HoudiniTarget; hdk_api=$target.houdini_api; houdini_root=$HoudiniRoot;
        engine_route="$engineRoute".Trim(); build_directory=$BuildDirectory; output_directory=$OutputDirectory;
        existing_hip=$ExistingHip; baseline_manifest=$BaselineManifest; rollback_package=$RollbackPackage;
        rebase_approval=$RebaseApproval; qualified_nightly=$QualifiedNightly; fast_ci_receipt=$FastCIReceipt;
        focused_checks=[bool]$FocusedChecks; optix_device=$OptixDevice; jobs=$Jobs;
        planned_steps=@('source snapshot','compile','isolated headless and GUI qualification','candidate package and installer')
    } | ConvertTo-Json -Depth 4
    exit 0
}
New-Item -ItemType Directory -Path $OutputDirectory | Out-Null
New-Item -ItemType Directory -Path $BuildDirectory -Force | Out-Null
$buildLock=[IO.File]::Open((Join-Path $BuildDirectory 'release.lock'),[IO.FileMode]::OpenOrCreate,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None)
$report=[ordered]@{result='running';houdini=$HoudiniTarget;build=$BuildDirectory;output=$OutputDirectory;artist_activation=$false;checks=@();native_tests=@();
    qualification_mode=$(if($FocusedChecks){'release+focused'}else{'release'});focused_checks_requested=[bool]$FocusedChecks;
    expected_focused_fixture_invocations=$(if($FocusedChecks){8}else{0});full_nightly_qualified=$false;nightly_case_count=0;
    qualification_reason=$(if($FocusedChecks){'Focused CPU and OptiX release checks selected by the caller.'}else{'Standard release checks selected by the caller.'});
    started_utc=[DateTime]::UtcNow.ToString('o')}
function Save-ReleaseReport{
    $report.native_fixture_invocations=@($report.native_tests).Count
    $report.focused_fixture_invocations=@($report.native_tests | Where-Object scope -EQ 'focused').Count
    $report.focused_passed_fixture_invocations=@($report.native_tests | Where-Object {$_.scope -eq 'focused' -and $_.result -eq 'passed'}).Count
    $report | ConvertTo-Json -Depth 24 | Set-Content -LiteralPath (Join-Path $OutputDirectory 'release-report.json') -Encoding utf8
}
function Assert-ReleaseManifest{
    if($report.tested_manifest_sha256 -and (Get-FileHash -LiteralPath (Join-Path $BuildDirectory 'build-manifest.json')).Hash.ToLowerInvariant() -ne $report.tested_manifest_sha256){
        throw 'Staged build manifest changed after the tested manifest was frozen.'
    }
}
function Invoke-ReleasePython([string]$Label,[string[]]$Arguments){
    & python @Arguments *> (Join-Path $OutputDirectory ($Label+'.log'))
    if($LASTEXITCODE -ne 0){throw "$Label failed; see $OutputDirectory/$Label.log"}
    $report.checks+=@($Label);Save-ReleaseReport
}
function Invoke-ReleaseTest([string]$Label,[string]$Test,[string]$Packages,[string[]]$Arguments=@(),[string]$ReportPath='', [switch]$Gui,[string]$ContextBuildDirectory='',[string]$Device='CPU',[switch]$Focused){
    if(-not $ContextBuildDirectory){$ContextBuildDirectory=$BuildDirectory}
    Assert-ReleaseManifest
    $manifestHash=(Get-FileHash -LiteralPath (Join-Path $ContextBuildDirectory 'build-manifest.json')).Hash.ToLowerInvariant()
    if($Focused -and $manifestHash -ne $report.tested_manifest_sha256){throw 'Focused checks must use the exact frozen release build.'}
    $record=[ordered]@{label=$Label;test=$Test;device=$Device;scope=$(if($Focused){'focused'}else{'release'});result='running';
        report=$ReportPath;build_manifest_sha256=$manifestHash;started_utc=[DateTime]::UtcNow.ToString('o')}
    $report.native_tests+=@($record);Save-ReleaseReport
    $previousArtifacts=$env:CYCLES_HOUDINI_TEST_ARTIFACTS
    $previousWorkerLogs=$env:CYCLES_HOUDINI_WORKER_LOG_DIR
    try{
        if($Focused){
            $env:CYCLES_HOUDINI_TEST_ARTIFACTS=Join-Path $OutputDirectory ($Label+'/artifacts')
            $env:CYCLES_HOUDINI_WORKER_LOG_DIR=Join-Path $env:CYCLES_HOUDINI_TEST_ARTIFACTS 'workers'
        }
        $runner=@{Test=$Test;Device=$Device;Execution='worker';HoudiniRoot=$HoudiniRoot;BuildDirectory=$ContextBuildDirectory;PackageDirectory=$Packages;UserPrefDirectory=(Join-Path $OutputDirectory ($Label+'/prefs/houdini__HVER__'));TimeoutSeconds=900}
        if($Arguments.Count){$runner.TestArguments=$Arguments}
        if($ReportPath){$runner.Report=$ReportPath}
        if($Gui){$runner.Gui=$true}
        & (Join-Path $PSScriptRoot 'run-prototype-test.ps1') @runner *> (Join-Path $OutputDirectory ($Label+'.log'))
        $record.exit_code=$LASTEXITCODE
        if($record.exit_code -ne 0){throw "$Label failed; see $OutputDirectory/$Label.log"}
        Assert-ReleaseManifest
        $record.result='passed'
        $report.checks+=@($Label)
    }catch{$record.result='failed';$record.error=$_.Exception.Message;throw}
    finally{
        $record.finished_utc=[DateTime]::UtcNow.ToString('o')
        $env:CYCLES_HOUDINI_TEST_ARTIFACTS=$previousArtifacts
        $env:CYCLES_HOUDINI_WORKER_LOG_DIR=$previousWorkerLogs
        Save-ReleaseReport
    }
}
$previous=@{}
foreach($entry in Get-ChildItem Env:){if($entry.Name -like 'CYCLES_HOUDINI_*' -or $entry.Name -like 'CH_GPU_PATH_*' -or $entry.Name -like 'HOUDINI_*' -or $entry.Name -in @('PYTHONPATH','CH_IPR_LAYOUT_REPORT')){$previous[$entry.Name]=$entry.Value;Remove-Item -LiteralPath ('Env:'+$entry.Name)}}
$installRoot=Join-Path $OutputDirectory 'relocated-install'
try{
    $env:HOUDINI_NO_ENV_FILE='1';$env:HOUDINI_DISABLE_BACKGROUND_HELP_INDEXING='1'
    if ($FastCIReceipt) {
        $head = (& git -C $root rev-parse HEAD).Trim()
        $dirty = & git -C $root status --porcelain
        if ($LASTEXITCODE -or $dirty) { throw 'Fast CI receipt requires a clean source checkout.' }
        $ciReceipt = Get-Content -LiteralPath $FastCIReceipt -Raw | ConvertFrom-Json
        $ciManifest = Join-Path (Split-Path -Parent $FastCIReceipt) 'build-manifest.json'
        $ciSummary = Get-Content -LiteralPath $ciReceipt.summary -Raw | ConvertFrom-Json
        if ($ciReceipt.result -ne 'passed' -or $ciReceipt.tier -ne 'fast' -or $ciReceipt.commit -ne $head -or
            $ciSummary.build_manifest_sha256 -ne $ciReceipt.manifest_sha256 -or
            (Get-FileHash -LiteralPath $ciManifest).Hash -ne $ciReceipt.manifest_sha256) {
            throw 'Fast CI evidence does not match this source revision and passing build.'
        }
        Invoke-ReleasePython 'fast-ci' @((Join-Path $PSScriptRoot 'workstation-ci.py'),'verify-fast',$ciReceipt.summary)
        Copy-Item -LiteralPath $FastCIReceipt -Destination (Join-Path $OutputDirectory 'fast-ci-receipt.json')
        $report.fast_ci=@{commit=$head;cases=@($ciSummary.tests).Count;manifest_sha256=$ciReceipt.manifest_sha256;
            receipt_sha256=(Get-FileHash -LiteralPath $FastCIReceipt).Hash.ToLowerInvariant()}
        Save-ReleaseReport
    }
    if($OptixDevice){$env:CYCLES_HOUDINI_TEST_OPTIX_DEVICE=$OptixDevice}
    if($ReferenceBlender){
        $env:CYCLES_HOUDINI_BLENDER=$ReferenceBlender
        $report.reference_blender=$ReferenceBlender
    }
    $env:HOUDINI_USER_PREF_DIR=Join-Path $OutputDirectory 'build-prefs/houdini__HVER__'
    $report.baseline_override=[bool]$BaselineManifest
    $report.reference_comparison=[bool]($BaselineManifest -or $RollbackPackage)
    $report.rollback_exercised=$false
    if($RollbackPackage){
        $rollbackBaseline=Join-Path $OutputDirectory 'rollback-build-manifest.json'
        $rollbackReference=Join-Path $OutputDirectory 'rollback-reference.json'
        Invoke-ReleasePython 'rollback-reference' @((Join-Path $PSScriptRoot 'release-source.py'),'baseline','--package',$RollbackPackage,'--baseline',$rollbackBaseline,'--houdini',$HoudiniTarget,'--output',$rollbackReference)
        $reference=Get-Content -LiteralPath $rollbackReference -Raw | ConvertFrom-Json
        $report.rollback_package_sha256=$reference.package_sha256
        if(-not $BaselineManifest){$BaselineManifest=$rollbackBaseline}
    }
    if($BaselineManifest){
        $baseline=Get-Content -LiteralPath $BaselineManifest -Raw | ConvertFrom-Json
        if($baseline.host_sdk.houdini -ne $HoudiniTarget -or [string]$baseline.host_sdk.hdk_api -ne [string]$target.houdini_api){throw 'Baseline provenance differs from the exact qualified SDK/API.'}
    }
    Invoke-ReleasePython 'sdk-preflight' @((Join-Path $PSScriptRoot 'sdk-dependencies.py'),'--houdini-root',$HoudiniRoot)
    Invoke-ReleasePython 'patch-replay' @((Join-Path $PSScriptRoot 'verify-upstream-patches.py'),'--output',(Join-Path $OutputDirectory 'patch-replay'))
    Invoke-ReleasePython 'patch-inventory' @((Join-Path $PSScriptRoot 'patch-inventory.py'),'--check')
    Invoke-ReleasePython 'fork-policy' @((Join-Path $PSScriptRoot 'patch-triage.py'),'--check')
    $snapshot=Join-Path $OutputDirectory 'source-snapshot.zip'
    Invoke-ReleasePython 'source-snapshot' @((Join-Path $PSScriptRoot 'release-source.py'),'snapshot','--output',$snapshot)
    if($BaselineManifest){
        Copy-Item -LiteralPath $BaselineManifest -Destination (Join-Path $OutputDirectory 'baseline-build-manifest.json')
        $BaselineManifest=Join-Path $OutputDirectory 'baseline-build-manifest.json'
    }
    if($RebaseApproval){
        if(-not $BaselineManifest){throw 'A rebase approval requires a baseline manifest or rollback package.'}
        Copy-Item -LiteralPath $RebaseApproval -Destination (Join-Path $OutputDirectory 'release-rebase-approval.json')
        $report.rebase_approval=@{path=$RebaseApproval;sha256=(Get-FileHash -LiteralPath $RebaseApproval).Hash.ToLowerInvariant()}
    }
    $frozenHip=Join-Path $OutputDirectory ('existing'+[IO.Path]::GetExtension($ExistingHip))
    Copy-Item -LiteralPath $ExistingHip -Destination $frozenHip
    $report.existing_hip=@{path=$ExistingHip;sha256=(Get-FileHash -LiteralPath $ExistingHip).Hash.ToLowerInvariant()}
    if($QualifiedNightly){
        $QualifiedNightly=(Resolve-Path -LiteralPath $QualifiedNightly).Path
        Invoke-ReleasePython 'qualified-nightly' @((Join-Path $PSScriptRoot 'run-nightly.py'),'verify','--run',$QualifiedNightly,'--build-directory',$BuildDirectory)
        $nightlyPath=Join-Path $QualifiedNightly 'summary.json'
        Copy-Item -LiteralPath $nightlyPath -Destination (Join-Path $OutputDirectory 'nightly-summary.json')
        $nightly=Get-Content -LiteralPath $nightlyPath -Raw | ConvertFrom-Json
        $report.qualified_nightly=@{directory=$QualifiedNightly;cases=@($nightly.tests).Count;manifest_sha256=$nightly.build_manifest_sha256}
        $report.full_nightly_qualified=$true;$report.nightly_case_count=@($nightly.tests).Count
    }else{
        & (Join-Path $PSScriptRoot 'build.ps1') -HoudiniRoot $HoudiniRoot -BuildDirectory $BuildDirectory -UserPrefDirectory (Join-Path $OutputDirectory 'build-prefs/houdini__HVER__') -Jobs $Jobs -ImageIris -Dlss *> (Join-Path $OutputDirectory 'build.log')
        if($LASTEXITCODE -ne 0){throw 'Build/staging failed.'}
        $report.checks+=@('one-production-build-and-stage')
    }
    Save-ReleaseReport
    $manifest=Get-Content -LiteralPath (Join-Path $BuildDirectory 'build-manifest.json') -Raw | ConvertFrom-Json
    $frozenManifest=Join-Path $OutputDirectory 'tested-build-manifest.json'
    Copy-Item -LiteralPath (Join-Path $BuildDirectory 'build-manifest.json') -Destination $frozenManifest
    $report.tested_manifest_sha256=(Get-FileHash -LiteralPath $frozenManifest).Hash.ToLowerInvariant()
    if($QualifiedNightly -and $report.tested_manifest_sha256 -ne $nightly.build_manifest_sha256){throw 'Staged build changed after nightly verification.'}
    $builtHost=Join-Path $BuildDirectory ($manifest.resource_root -replace '/resources$','')
    $builtPackages=Join-Path $BuildDirectory 'packages'
    $builtReport=Join-Path $OutputDirectory 'runtime-built.json'
    Invoke-ReleaseTest 'runtime-built' 'houdini_release_runtime_smoke.py' $builtPackages @('--houdini',$HoudiniTarget,'--expected-host',$builtHost,'--report',$builtReport) $builtReport
    $archive=Join-Path $OutputDirectory 'cycles-houdini-candidate.zip'
    Invoke-ReleasePython 'package-build' @((Join-Path $PSScriptRoot 'release-package.py'),'build','--build-root',$BuildDirectory,'--source-root',$root,'--report',(Join-Path $OutputDirectory 'package-build.json'),'--output',$archive)
    $payloadArguments=@((Join-Path $PSScriptRoot 'release-source.py'),'preservation','--build-root',$BuildDirectory,'--package',$archive,'--frozen-manifest',$frozenManifest,'--output',(Join-Path $OutputDirectory 'payload-preservation.json'))
    if($RebaseApproval){$payloadArguments+=@('--rebase-approval',$RebaseApproval)}
    if($BaselineManifest){$payloadArguments+=@('--baseline',$BaselineManifest)}
    Invoke-ReleasePython 'payload-preservation' $payloadArguments
    $report.package_sha256=(Get-FileHash -LiteralPath $archive).Hash.ToLowerInvariant()
    $preferences=Join-Path $OutputDirectory 'installed-prefs/houdini22.0'
    $data=Join-Path $OutputDirectory 'installed-data'
    if($RollbackPackage){
        Invoke-ReleasePython 'baseline-install' @((Join-Path $PSScriptRoot 'release-package.py'),'install',$RollbackPackage,'--install-root',$installRoot,'--preference-root',$preferences,'--data-root',$data,'--allow-internal','--isolated','--expected-sha256',$report.rollback_package_sha256)
        $baselineState=Get-Content -LiteralPath (Join-Path $installRoot 'state.json') -Raw | ConvertFrom-Json
        $report.rollback_package_id=$baselineState.active
        $baselineBuild=Join-Path $OutputDirectory 'baseline-build'
        New-Item -ItemType Directory -Path $baselineBuild | Out-Null
        Copy-Item -LiteralPath $rollbackBaseline -Destination (Join-Path $baselineBuild 'build-manifest.json')
    }
    Invoke-ReleasePython 'relocated-install' @((Join-Path $PSScriptRoot 'release-package.py'),'install',$archive,'--install-root',$installRoot,'--preference-root',$preferences,'--data-root',$data,'--allow-internal','--isolated','--replace-existing','--expected-sha256',$report.package_sha256)
    $state=Get-Content -LiteralPath (Join-Path $installRoot 'state.json') -Raw | ConvertFrom-Json
    $installedHost=Join-Path $installRoot ('versions/'+$state.active.Substring(0,16)+'/host')
    $packages=Join-Path $preferences 'packages'
    $irisReport=Join-Path $OutputDirectory 'runtime-installed.json'
    Invoke-ReleaseTest 'runtime-installed' 'houdini_release_runtime_smoke.py' $packages @('--houdini',$HoudiniTarget,'--expected-host',$installedHost,'--package-id',$state.active,'--report',$irisReport) $irisReport
    $irisRuntime=Get-Content -LiteralPath $irisReport -Raw | ConvertFrom-Json
    $blendResolution=Join-Path $OutputDirectory 'blend-instance-resolution'
    Invoke-ReleaseTest 'blend-instance-resolution' 'houdini_blend_instance_resolution_smoke.py' $packages @('--out',$blendResolution) (Join-Path $blendResolution 'report.json')
    $blendStyles=Join-Path $OutputDirectory 'blend-instance-styles'
    Invoke-ReleaseTest 'blend-instance-styles' 'houdini_blend_direct_styles_smoke.py' $packages @('--out',$blendStyles) (Join-Path $blendStyles 'report.json')
    $devices=@($irisRuntime.checks.device | Select-Object -Unique)
    if(-not $irisRuntime.image_iris_enabled -or 'CPU' -notin $devices -or $devices.Count -lt 2){throw 'Release optics checks require Image Iris on both CPU and GPU.'}
    if($FocusedChecks){
        # Existing release gates already qualify Iris, splats, DLSS and optics in
        # built/installed runtimes. Add only binding and independent image checks.
        $focusedGpu=$irisRuntime.selected_gpu
        if(-not $focusedGpu -or $focusedGpu.backend -ne 'OPTIX'){throw 'Focused checks require an OptiX GPU; select its exact ID with -OptixDevice.'}
        if($OptixDevice -and $focusedGpu.id -ne $OptixDevice){throw 'Installed runtime selected a different focused OptiX device.'}
        $env:CYCLES_HOUDINI_TEST_OPTIX_DEVICE=[string]$focusedGpu.id
        $report.focused_optix_device=$focusedGpu
        $front=Join-Path $OutputDirectory 'focused-pybind-front/report.json'
        Invoke-ReleaseTest 'focused-pybind-front' 'houdini_pybind_front_contract.py' $packages @('--report',$front) $front -Focused
        $buffer=Join-Path $OutputDirectory 'focused-pybind-buffer/report.json'
        Invoke-ReleaseTest 'focused-pybind-buffer' 'houdini_argument_buffer_contract.py' $packages @('--report',$buffer) $buffer -Focused
        foreach($selection in @(@{label='cpu';backend='CPU';id='CPU'},@{label='optix';backend='OPTIX';id=[string]$focusedGpu.id})){
            $status=Join-Path $OutputDirectory ('focused-pybind-'+$selection.label+'/report.json')
            Invoke-ReleaseTest ('focused-pybind-'+$selection.label) 'houdini_status_conversion_contract.py' $packages @('--report',$status) $status -Device $selection.id -Focused
            $statusResult=Get-Content -LiteralPath $status -Raw | ConvertFrom-Json
            if($statusResult.device.id -ne $selection.id -or $statusResult.device.backend -ne $selection.backend){throw 'Focused binding report does not identify the required backend/device.'}
            $reference=Join-Path $OutputDirectory ('focused-blender-'+$selection.label)
            $comparison=Join-Path $reference ('comparison-'+$selection.backend+'.json')
            Invoke-ReleaseTest ('focused-blender-'+$selection.label) 'houdini_blend_instances_smoke.py' $packages @('--generate-reference','--out',$reference) $comparison -Device $selection.backend -Focused
            $imageResult=Get-Content -LiteralPath $comparison -Raw | ConvertFrom-Json
            if($imageResult.verified_houdini_device_id -ne $selection.id -or $imageResult.reference_device.backend -ne $selection.backend -or -not $imageResult.verified_reference_device_id){throw 'Focused Blender comparison did not verify both required device identities.'}
            # The package need not ship ch_harness. Its native DSO is already
            # hash-verified against the stage; use that same stage for this oracle.
            $subdivision=Join-Path $OutputDirectory ('focused-subdivision-'+$selection.label+'/report.json')
            Invoke-ReleaseTest ('focused-subdivision-'+$selection.label) 'houdini_sampled_displacement_smoke.py' $builtPackages @('--subdivision-normals-only','--report',$subdivision) $subdivision -Device $selection.id -Focused
            $subdivisionResult=Get-Content -LiteralPath $subdivision -Raw | ConvertFrom-Json
            if($subdivisionResult.mode -ne 'subdivision_normals_only' -or $subdivisionResult.references -ne 2 -or @($subdivisionResult.subdivision_normals).Count -ne 2){throw 'Focused subdivision report has an incomplete oracle comparison.'}
        }
        if($report.focused_passed_fixture_invocations -ne $report.expected_focused_fixture_invocations){throw 'Focused checks did not complete all eight fixture invocations.'}
    }
    $gpuIndex=0
    foreach($device in $devices){
        $backend=if($device -eq 'CPU'){'cpu'}else{"gpu$gpuIndex";$gpuIndex++}
        $reseed=Join-Path $OutputDirectory "blend-instance-reseed-$backend/report.json"
        Invoke-ReleaseTest "blend-instance-reseed-$backend" 'houdini_blend_instance_reseed_smoke.py' $packages @('--out',(Split-Path -Parent $reseed),'--report',$reseed) $reseed -Device $device
        $splats=Join-Path $OutputDirectory "splats-$backend"
        Invoke-ReleaseTest "splats-$backend" 'houdini_gaussian_splats_smoke.py' $packages @('--output',$splats) (Join-Path $splats 'report.json') -Device $device
        if($device -ne 'CPU'){
            $combined=Join-Path $OutputDirectory "splats-dlss-iris-$backend/report.json"
            Invoke-ReleaseTest "splats-dlss-iris-$backend" 'houdini_gsplats_dlss_worker_smoke.py' $packages @('--report',$combined) $combined -Device $device
        }
        $optics=Join-Path $OutputDirectory "optics-$backend/report.json"
        Invoke-ReleaseTest "optics-$backend" 'houdini_transport_images_smoke.py' $packages @('--report',$optics) $optics -Device $device
        $rlsRoot=Join-Path $OutputDirectory 'rls-materials'
        Invoke-ReleaseTest "rls-materials-$backend" 'houdini_selective_rls_smoke.py' $packages @('--output',$rlsRoot,'--label',$backend) (Join-Path $rlsRoot "$backend/report.json") -Device $device
        $irisColor=Join-Path $OutputDirectory "iris-color-$backend/report.json"
        Invoke-ReleaseTest "iris-color-$backend" 'core_image_iris_color_smoke.py' $builtPackages @('--report',$irisColor) $irisColor -Device $device
        $colorRun=(Get-Content -LiteralPath $irisColor -Raw | ConvertFrom-Json).runs[0]
        if(-not $colorRun.events[-1].feature_enabled -or -not $colorRun.events[-1].coverage_alpha_unchanged){throw "RGB Image Iris render failed coverage validation on $device"}
    }
    $layout=Join-Path $OutputDirectory 'layout-installed.json';$env:CH_IPR_LAYOUT_REPORT=$layout
    Invoke-ReleaseTest 'layout-installed' 'houdini_ipr_settings_layout_smoke.py' $packages @() $layout
    $existingReport=Join-Path $OutputDirectory 'existing-hip.json'
    Invoke-ReleaseTest 'existing-hip' 'houdini_release_existing_hip_smoke.py' $packages @('--hip',$frozenHip,'--report',$existingReport) $existingReport
    $relocated=Join-Path $OutputDirectory 'relocated-runtime.json'
    Invoke-ReleaseTest 'relocated-runtime' 'houdini_relocated_release_smoke.py' $packages @('--expected-host',$installedHost,'--expected-data-root',$data,'--report',$relocated) $relocated
    $farmReport=Join-Path $data 'artifacts/stage3/saved-hip-farm/report.json'
    Invoke-ReleaseTest 'saved-hip-farm' 'houdini_saved_hip_farm_smoke.py' $packages @() $farmReport
    $policyReport=Join-Path $data 'artifacts/stage3/farm-output-policy/report.json'
    Invoke-ReleaseTest 'farm-output-policy' 'houdini_farm_output_policy_smoke.py' $packages @() $policyReport
    $lightReport=Join-Path $OutputDirectory 'light-pose-rig/report.json'
    Invoke-ReleaseTest 'light-pose-rig' 'houdini_light_pose_sources_benchmark.py' $packages @('--expect-filtered','--report',$lightReport) $lightReport
    $visibilityReport=Join-Path $OutputDirectory 'light-visibility/report.json'
    Invoke-ReleaseTest 'light-visibility' 'houdini_light_pose_sources_benchmark.py' $packages @('--probe','--report',$visibilityReport) $visibilityReport
    $archiveReport=Join-Path $OutputDirectory 'scene-archive/report.json'
    Invoke-ReleaseTest 'scene-archive' 'houdini_offline_save_benchmark.py' $packages @('--report',$archiveReport) $archiveReport
    Invoke-ReleaseTest 'viewer-render-edit-stop' 'houdini_release_response_gui_smoke.py' $packages @() '' -Gui
    if($RollbackPackage){
        Invoke-ReleasePython 'rollback-baseline' @((Join-Path $PSScriptRoot 'release-package.py'),'install',$RollbackPackage,'--install-root',$installRoot,'--preference-root',$preferences,'--data-root',$data,'--allow-internal','--isolated','--replace-existing','--expected-sha256',$report.rollback_package_sha256)
        $rollbackState=Get-Content -LiteralPath (Join-Path $installRoot 'state.json') -Raw | ConvertFrom-Json
        if($rollbackState.active -ne $baselineState.active){throw 'Rollback did not restore the baseline release identity.'}
        $rollbackReport=Join-Path $OutputDirectory 'runtime-rollback.json'
        $rollbackRecord=[ordered]@{label='runtime-rollback';test='houdini_release_runtime_smoke.py';device='script-declared';scope='rollback';result='running';
            report=$rollbackReport;build_manifest_sha256=(Get-FileHash -LiteralPath (Join-Path $baselineBuild 'build-manifest.json')).Hash.ToLowerInvariant();started_utc=[DateTime]::UtcNow.ToString('o')}
        $report.native_tests+=@($rollbackRecord);Save-ReleaseReport
        try{
            & (Join-Path $PSScriptRoot 'run-hython.ps1') -HoudiniRoot $HoudiniRoot -BuildDirectory $baselineBuild -PackageDirectory $packages -UserPrefDirectory (Join-Path $OutputDirectory 'rollback-prefs/houdini__HVER__') (Join-Path $root 'tests/houdini_release_runtime_smoke.py') --houdini $HoudiniTarget --expected-host (Join-Path $installRoot ('versions/'+$rollbackState.active.Substring(0,16)+'/host')) --package-id $rollbackState.active --report $rollbackReport *> (Join-Path $OutputDirectory 'runtime-rollback.log')
            $rollbackRecord.exit_code=$LASTEXITCODE
            if($rollbackRecord.exit_code -ne 0 -or -not(Test-Path -LiteralPath $rollbackReport)){throw 'Rollback Image Iris runtime probe failed.'}
            $rollbackRecord.result='passed'
        }catch{$rollbackRecord.result='failed';$rollbackRecord.error=$_.Exception.Message;throw}
        finally{$rollbackRecord.finished_utc=[DateTime]::UtcNow.ToString('o');Save-ReleaseReport}
        if($rollbackState.active -eq $state.active){throw 'Rollback baseline and candidate have the same release identity.'}
        $report.checks+=@('rollback-different-payload-and-Iris')
        $report.rollback_exercised=$true
    }
    Assert-ReleaseManifest
    # Remove only this run's registration; retain payload/data for review and farm evidence.
    Invoke-ReleasePython 'isolated-uninstall' @((Join-Path $PSScriptRoot 'release-package.py'),'uninstall','--install-root',$installRoot)
    Invoke-ReleasePython 'patch-replay-final' @((Join-Path $PSScriptRoot 'verify-upstream-patches.py'),'--output',(Join-Path $OutputDirectory 'patch-replay-final'))
    Invoke-ReleasePython 'fork-policy-final' @((Join-Path $PSScriptRoot 'patch-triage.py'),'--check')
    Invoke-ReleasePython 'source-unchanged' @((Join-Path $PSScriptRoot 'release-source.py'),'check-snapshot','--snapshot',$snapshot)
    & (Join-Path $PSScriptRoot 'build-installer.ps1') -HoudiniRoot $HoudiniRoot -Package $archive -OutputDir (Join-Path $OutputDirectory 'installer') *> (Join-Path $OutputDirectory 'installer-build.log')
    if($LASTEXITCODE -ne 0){throw 'Installer compilation failed.'}
    $installerReport=(Get-ChildItem -LiteralPath (Join-Path $OutputDirectory 'installer') -Filter '*-Setup.json' | Select-Object -First 1).FullName
    $installer=Get-Content -LiteralPath $installerReport -Raw | ConvertFrom-Json
    if($installer.package_sha256 -ne $report.package_sha256){throw 'Installer payload differs from the tested package.'}
    Assert-ReleaseManifest
    $qualification=[ordered]@{mode=$report.qualification_mode;native_fixture_invocations=$report.native_fixture_invocations;
        focused_fixture_invocations=$report.focused_fixture_invocations;focused_passed_fixture_invocations=$report.focused_passed_fixture_invocations;
        full_nightly_qualified=$report.full_nightly_qualified;nightly_case_count=$report.nightly_case_count;tested_manifest_sha256=$report.tested_manifest_sha256}
    Add-Member -InputObject $installer -NotePropertyName qualification -NotePropertyValue $qualification -Force
    $installer | ConvertTo-Json -Depth 16 | Set-Content -LiteralPath $installerReport -Encoding utf8
    $report.installer=$installer;$report.package_id=$state.active
    $report.source_snapshot=@{path=$snapshot;sha256=(Get-FileHash -LiteralPath $snapshot).Hash.ToLowerInvariant()}
    $report.result='passed';$report.scope='Internal candidate: exact target, isolated built/installed runtime, Iris CPU/GPU, relocated recovery, existing HIP, saved HIP farm and Viewer. Installer lifecycle/coexistence tracked separately; no artist activation.'
}catch{$report.result='failed';$report.error=$_.Exception.Message;throw}
finally{
    $buildLock.Dispose()
    $report.finished_utc=[DateTime]::UtcNow.ToString('o');Save-ReleaseReport
    foreach($entry in Get-ChildItem Env:){if($entry.Name -like 'CYCLES_HOUDINI_*' -or $entry.Name -like 'CH_GPU_PATH_*' -or $entry.Name -like 'HOUDINI_*' -or $entry.Name -in @('PYTHONPATH','CH_IPR_LAYOUT_REPORT')){Remove-Item -LiteralPath ('Env:'+$entry.Name)}}
    foreach($key in $previous.Keys){[Environment]::SetEnvironmentVariable($key,$previous[$key],'Process')}
}
Write-Host "Package $($report.package_id) | $($report.qualification_mode) | $($report.focused_passed_fixture_invocations) focused fixtures | $($report.nightly_case_count) independently qualified nightly cases"
Write-Host $report.qualification_reason
Write-Host "Candidate and release report: $OutputDirectory"
$commonDir = (& git -C $root rev-parse --path-format=absolute --git-common-dir).Trim()
$ciConfiguration = Join-Path $commonDir 'houdini-ci.json'
if (Test-Path -LiteralPath $ciConfiguration) {
    $ciConfig = Get-Content -LiteralPath $ciConfiguration -Raw | ConvertFrom-Json
    # Workstation recovery is a local tool of the maintainer's workstation and is not published; it runs only
    # where it exists.
    $recoveryTool = Join-Path $PSScriptRoot 'workstation-recovery.py'
    if ($ciConfig.recovery_directory -and (Test-Path -LiteralPath $recoveryTool -PathType Leaf)) {
        & python -I $recoveryTool capture --source $root --revision HEAD --destination $ciConfig.recovery_directory --ci $ciConfig.state
        if ($LASTEXITCODE) { throw "Qualified release saved at $OutputDirectory. F: recovery publication failed. Retry workstation-recovery.py capture." }
    }
}
