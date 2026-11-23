# SPDX-License-Identifier: Apache-2.0
[CmdletBinding()]
param([string]$BuildDirectory = '', [string]$UserPrefDirectory = '', [string]$Python = '')
$ErrorActionPreference = 'Stop'
if (-not $Python) { $Python = (Get-Command python -CommandType Application | Select-Object -First 1).Source }
. (Join-Path $PSScriptRoot 'staging-runtime-files.ps1')
. (Join-Path $PSScriptRoot 'build-layout.ps1')
$taskRoot = Split-Path -Parent $PSScriptRoot
$taskRoot = $taskRoot.Replace('\','/')
$build = if($BuildDirectory){[IO.Path]::GetFullPath($BuildDirectory)}else{Get-DefaultBuildDirectory -SourceRoot $taskRoot}
$cache = @{}
foreach ($line in Get-Content -LiteralPath (Join-Path $build 'CMakeCache.txt')) {
    if ($line -match '^([^/#][^:]*):[^=]+=(.*)$') { $cache[$Matches[1]]=$Matches[2] }
}
$hostRoot = if($cache['CH_NATIVE_HOST_ROOT']){$cache['CH_NATIVE_HOST_ROOT']}else{Join-Path $build 'houdini'}
$hostRoot = [IO.Path]::GetFullPath($hostRoot).Replace('\','/')
$solaris = $cache['WITH_CYCLES_HYDRA_RENDER_DELEGATE'] -in @('ON','TRUE','1')
$assetOptions = @(); if($solaris){$assetOptions += '--solaris'}
$buildAbsolute = [IO.Path]::GetFullPath($build).Replace('\','/').TrimEnd('/')
if(-not $hostRoot.StartsWith($buildAbsolute+'/',[StringComparison]::OrdinalIgnoreCase)){throw 'Development host root must stay inside the build directory.'}
# Developer runs use their own preferences: an installed Cycles package in the
# artist's Houdini preferences must never replace the build under test.
$env:HOUDINI_USER_PREF_DIR = if($UserPrefDirectory){[IO.Path]::GetFullPath($UserPrefDirectory)}else{Join-Path $build 'dev-prefs/houdini__HVER__'}
if($env:HOUDINI_USER_PREF_DIR -notlike '*__HVER__*'){throw 'Staging preferences must use an isolated __HVER__ path.'}
function Quote-StagingArgument([string]$Value){
    if($Value.Length -eq 0){return '""'}
    if($Value -notmatch '[\s"]'){return $Value}
    '"'+([regex]::Replace($Value,'(\\*)"','$1$1\"') -replace '(\\+)$','$1$1')+'"'
}
function Stop-StagingProcessTree([Diagnostics.Process]$StageProcess,[string]$LogPrefix){
    if($StageProcess.HasExited){return}
    if($StageProcess.GetType().GetMethod('Kill',[type[]]@([bool]))){
        $StageProcess.Kill($true)
    }else{
        $cleanupProcess=Start-Process -FilePath (Join-Path $env:SystemRoot 'System32/taskkill.exe') `
            -ArgumentList @('/PID',[string]$StageProcess.Id,'/T','/F') -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput ($LogPrefix+'.cleanup.stdout.log') -RedirectStandardError ($LogPrefix+'.cleanup.stderr.log')
        try{
            $cleanupHandle=$cleanupProcess.Handle
            if(-not $cleanupProcess.WaitForExit(10000)){
                $cleanupProcess.Kill()
                throw 'Staging process-tree cleanup exceeded 10 seconds.'
            }
        }finally{$cleanupProcess.Dispose()}
    }
    if(-not $StageProcess.WaitForExit(10000)){throw 'The owned staging Hython process did not exit after cleanup.'}
}
function Invoke-StagingHython([string]$Label,[string[]]$Arguments,[int]$TimeoutSeconds=300){
    $logDirectory=Join-Path $build ('staging/'+(Split-Path -Leaf $hostRoot))
    New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
    $logPrefix=Join-Path $logDirectory ($Label+'-'+[DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfff'))
    $stageProcess=$null;$stageExitCode=$null;$stageError='';$cleanupError=''
    $stageStarted=[DateTime]::UtcNow
    try{
        $quotedArguments=@($Arguments | ForEach-Object {Quote-StagingArgument $_})
        $stageProcess=Start-Process -FilePath (Join-Path $cache['HOUDINI_ROOT'] 'bin/hython.exe') `
            -ArgumentList $quotedArguments -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput ($logPrefix+'.stdout.log') -RedirectStandardError ($logPrefix+'.stderr.log')
        # Retain the native handle until ExitCode has been collected.
        $stageHandle=$stageProcess.Handle
        if(-not $stageProcess.WaitForExit($TimeoutSeconds*1000)){
            Stop-StagingProcessTree $stageProcess $logPrefix
            throw ("Staging Hython '$Label' exceeded $TimeoutSeconds seconds. Logs: $logPrefix")
        }
        $stageExitCode=$stageProcess.ExitCode
        if($null -eq $stageExitCode){throw "Staging Hython '$Label' exit code is unavailable. Logs: $logPrefix"}
        if($stageExitCode -ne 0){throw "Staging Hython '$Label' failed ($stageExitCode). Logs: $logPrefix"}
    }catch{$stageError=$_.Exception.Message;throw}
    finally{
        if($stageProcess){
            try{if(-not $stageProcess.HasExited){Stop-StagingProcessTree $stageProcess $logPrefix}}
            catch{$cleanupError=$_.Exception.Message}
            finally{$stageProcess.Dispose()}
        }
        [ordered]@{label=$Label;arguments=@($Arguments);started_utc=$stageStarted.ToString('o');
            finished_utc=[DateTime]::UtcNow.ToString('o');exit_code=$stageExitCode;error=$stageError;cleanup_error=$cleanupError;
            stdout=($logPrefix+'.stdout.log');stderr=($logPrefix+'.stderr.log')} |
            ConvertTo-Json -Depth 8 | Set-Content -LiteralPath ($logPrefix+'.json') -Encoding utf8
        if($cleanupError){throw "Staging process cleanup failed: $cleanupError"}
    }
}
$assetTool=Join-Path $taskRoot 'scripts/runtime-assets.py'
$assetCheck=& $Python $assetTool check --source-root $taskRoot --host-root $hostRoot --houdini-root $cache['HOUDINI_ROOT'] --build-directory $build @assetOptions
if($LASTEXITCODE -ne 0){throw 'Frozen runtime asset validation failed. Run build.ps1 for a fresh versioned host.'}
$assetDecision=$assetCheck | ConvertFrom-Json
function Write-FrozenJson([string]$Relative,$Value){
    $assetArguments=@($assetTool,'frozen-json','--source-root',$taskRoot,'--host-root',$hostRoot,'--path',$Relative,'--build-directory',$build)
    if($assetDecision.action -eq 'create'){$assetArguments+='--allow-create'}
    $Value | ConvertTo-Json -Depth 12 | & $Python @assetArguments
    if($LASTEXITCODE -ne 0){throw ('Could not preserve frozen runtime metadata: '+$Relative)}
}
function Publish-BuildJson([string]$Relative,$Value,[bool]$IgnoreTimestamp=$false){
    $assetArguments=@($assetTool,'publish-json','--source-root',$taskRoot,'--host-root',$hostRoot,'--path',$Relative,'--build-directory',$build)
    if($IgnoreTimestamp){$assetArguments+='--ignore-timestamp'}
    $Value | ConvertTo-Json -Depth 12 | & $Python @assetArguments
    if($LASTEXITCODE -ne 0){throw ('Could not publish development metadata: '+$Relative)}
}
function Get-ConfiguredCudaKernels([string]$KernelDirectory,[string]$Architectures){
    # Build directories retain old outputs after reconfiguration. Select only
    # the exact current CMake architecture list, never all remaining *.zst.
    $configured=@($Architectures.Split(';') | ForEach-Object {$_.Trim()} | Where-Object {$_})
    if($configured.Count -eq 0){throw 'CYCLES_CUDA_BINARIES_ARCH must explicitly list the staged CUDA architectures.'}
    if(@($configured | Sort-Object -Unique).Count -ne $configured.Count){throw 'Duplicate CUDA staging architecture.'}
    $selected=@()
    foreach($architecture in $configured){
        if($architecture -notmatch '^(sm|compute)_[0-9]+[a-z]?$'){throw "Unsupported CUDA staging architecture: $architecture"}
        $extension=if($architecture.StartsWith('sm_')){'cubin'}else{'ptx'}
        $kernelPath=Join-Path $KernelDirectory "kernel_$architecture.$extension.zst"
        if(-not (Test-Path -LiteralPath $kernelPath -PathType Leaf)){throw "Missing configured CUDA kernel: $kernelPath"}
        $selected+=Get-Item -LiteralPath $kernelPath
    }
    $selected
}
# Kernels contain private engine ABI. Loaded old modules must never pick up a
# newer build's kernels when an artist recreates a device/session.
$resources = Join-Path $hostRoot 'resources'
New-Item -ItemType Directory -Path (Join-Path $resources 'lib') -Force | Out-Null
foreach ($backend in @('cuda','optix')) {
    # A CPU-only build (scripts/build.ps1 -NoCuda: no CUDA compiler on this computer) has no GPU kernels.
    $deviceFeature=if($backend -eq 'cuda'){'WITH_CYCLES_DEVICE_CUDA'}else{'WITH_CYCLES_DEVICE_OPTIX'}
    if(([string]$cache[$deviceFeature]).ToUpperInvariant() -notin @('ON','TRUE','1')){Write-Host "No $backend kernels: this build has no $backend rendering."; continue}
    $kernelDirectory=Join-Path $build "cycles/src/kernel/device/$backend"
    $kernels = if($backend -eq 'cuda'){
        @(Get-ConfiguredCudaKernels $kernelDirectory $cache['CYCLES_CUDA_BINARIES_ARCH'])
    }else{@(Get-ChildItem -LiteralPath $kernelDirectory -Filter '*.zst' -File)}
    if ($kernels.Count -eq 0) { throw "No prebuilt $backend kernels were found." }
    if($backend -eq 'cuda'){
        $unexpected=@(Get-ChildItem -LiteralPath (Join-Path $resources 'lib') -File |
            Where-Object {$_.Name -match '^kernel_(sm|compute)_[0-9]+[a-z]?\.(cubin|ptx)\.zst$' -and $_.Name -notin $kernels.Name})
        if($unexpected.Count){throw 'The host contains CUDA kernels outside the configured architecture list. Create a fresh versioned host before staging.'}
    }
    foreach ($kernel in $kernels) {
        $target=Join-Path $resources ('lib/'+$kernel.Name)
        if(Test-Path -LiteralPath $target){
            if((Get-FileHash -LiteralPath $target).Hash -ne (Get-FileHash -LiteralPath $kernel.FullName).Hash){
                throw 'Kernels changed. Create a fresh versioned build before staging.'
            }
            continue
        }
        Copy-Item -LiteralPath $kernel.FullName -Destination $target
    }
}
$kernelRecords = @(Get-ChildItem -LiteralPath (Join-Path $resources 'lib') -File | ForEach-Object {
    @{name=$_.Name; bytes=$_.Length; sha256=(Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()}
})
# Freeze host Python, panels and SOHO alongside the versioned DLL. Restaging
# documentation is safe; runtime changes require a fresh build host root.
# addons/<name> are optional Houdini path roots (asset browser); the installer
# adds them to the package hpath only when the component is selected.
foreach($runtimeDirectory in @('python3.13libs','python_panels','soho','vop','toolbar','config','help','addons')) {
    $runtimeSource=Join-Path $taskRoot "houdini/$runtimeDirectory"
    # Runtime payload selection must not inherit the integration Git ignores
    # (notably *.png); branding and other assets are build inputs.
    $runtimeFiles=@(Get-StagingRuntimeFiles $runtimeSource)
    foreach($runtimeFile in $runtimeFiles) {
        $relativeRuntime=[IO.Path]::GetRelativePath((Join-Path $taskRoot 'houdini'),$runtimeFile)
        $runtimeTarget=Join-Path $hostRoot $relativeRuntime
        if($runtimeDirectory -eq 'help' -and $relativeRuntime.Replace('\','/') -in @('help/cycles/objects.txt','help/cycles/camera.txt','help/cycles/lights.txt','help/cycles/environment.txt')){
            continue  # generate-property-help publishes these templates plus the actual parameter reference
        }
        if(Test-Path -LiteralPath $runtimeTarget) {
            if((Get-FileHash -LiteralPath $runtimeTarget).Hash -ne (Get-FileHash -LiteralPath $runtimeFile).Hash){throw 'Host runtime changed. Run build.ps1 to create a fresh versioned host root before staging.'}
        } else {
            New-Item -ItemType Directory -Path (Split-Path -Parent $runtimeTarget) -Force | Out-Null
            Copy-Item -LiteralPath $runtimeFile -Destination $runtimeTarget
        }
    }
}
# The Blender component (blender/, GPL-3.0-or-later): the scripts that run inside Blender. It is
# staged to <host>/blender, beside python3.13libs and outside Houdini's Python path, so the
# Apache-2.0 Houdini side can never import it; the worker client starts its entry scripts there.
foreach($runtimeFile in @(Get-StagingRuntimeFiles (Join-Path $taskRoot 'blender'))) {
    $runtimeTarget=Join-Path $hostRoot ([IO.Path]::GetRelativePath($taskRoot,$runtimeFile))
    if(Test-Path -LiteralPath $runtimeTarget) {
        if((Get-FileHash -LiteralPath $runtimeTarget).Hash -ne (Get-FileHash -LiteralPath $runtimeFile).Hash){throw 'Host runtime changed. Run build.ps1 to create a fresh versioned host root before staging.'}
    } else {
        New-Item -ItemType Directory -Path (Split-Path -Parent $runtimeTarget) -Force | Out-Null
        Copy-Item -LiteralPath $runtimeFile -Destination $runtimeTarget
    }
}
foreach($runtimeName in @('MainMenuCommon.xml','OPmenu.xml','NetworkViewMenu.xml')) {
    $runtimeSource=Join-Path $taskRoot "houdini/$runtimeName"
    $runtimeTarget=Join-Path $hostRoot $runtimeName
    if(Test-Path -LiteralPath $runtimeTarget) {
        if((Get-FileHash -LiteralPath $runtimeTarget).Hash -ne (Get-FileHash -LiteralPath $runtimeSource).Hash){throw 'Host menu changed. Create a fresh versioned host root before staging.'}
    }else{Copy-Item -LiteralPath $runtimeSource -Destination $runtimeTarget}
}
foreach($entryName in @('soho_pipe.py','render_hip.py')) {
    $pipeSource=Join-Path $taskRoot "scripts/$entryName"
    $pipeTarget=Join-Path $hostRoot "scripts/$entryName"
    if(Test-Path -LiteralPath $pipeTarget) {
        if((Get-FileHash -LiteralPath $pipeTarget).Hash -ne (Get-FileHash -LiteralPath $pipeSource).Hash){throw 'Host entry script changed. Create a fresh versioned host root before staging.'}
    } else {
        New-Item -ItemType Directory -Path (Split-Path -Parent $pipeTarget) -Force | Out-Null
        Copy-Item -LiteralPath $pipeSource -Destination $pipeTarget
    }
}
$nativeBinary = [IO.Path]::GetRelativePath($buildAbsolute, "$hostRoot/dso/_cycles_houdini.dll").Replace('\','/')
$workerBinary = [IO.Path]::GetRelativePath($buildAbsolute, "$hostRoot/bin/ch_worker.exe").Replace('\','/')
$binaryPaths = @('bin/cycles.exe','bin/ch_harness.exe',$nativeBinary,$workerBinary)
if($solaris){$binaryPaths += 'cycles/src/hydra/hdCycles.dll'}
$binaryRecords = $binaryPaths | ForEach-Object {
    $binaryPath = Join-Path $build $_
    if (-not (Test-Path -LiteralPath $binaryPath -PathType Leaf)) { throw "Missing compiled build artifact: $_" }
    @{path=$_; bytes=(Get-Item -LiteralPath $binaryPath).Length; sha256=(Get-FileHash -LiteralPath $binaryPath -Algorithm SHA256).Hash.ToLowerInvariant()}
}
$featureNames=@('CH_IMAGE_IRIS','CH_BDPT_PROTOTYPE','WITH_CYCLES_PATH_GUIDING','WITH_CYCLES_DEVICE_CUDA','WITH_CYCLES_DEVICE_OPTIX','WITH_CYCLES_DEVICE_HIP','WITH_CYCLES_DEVICE_ONEAPI','WITH_CYCLES_OPENVDB','WITH_CYCLES_NANOVDB','WITH_CYCLES_OPENSUBDIV','WITH_CYCLES_OPENIMAGEDENOISE','WITH_CYCLES_OSL','WITH_CYCLES_ALEMBIC','WITH_CYCLES_HYDRA_RENDER_DELEGATE','WITH_CYCLES_USD')
$featureNames += 'WITH_DLSS'
$features=@{}; foreach($name in $featureNames) { $features[$name]=$cache[$name] }
$lock = Get-Content -LiteralPath (Join-Path $taskRoot 'source.lock.json') -Raw | ConvertFrom-Json
$environmentFile=Join-Path $build 'environment-report.json'
Invoke-StagingHython 'environment-report' @((Join-Path $taskRoot 'scripts/environment-report.py'),$environmentFile,$taskRoot,$build)
$environmentReport=Get-Content -LiteralPath $environmentFile -Raw | ConvertFrom-Json
Write-FrozenJson 'resources/sdk-dependencies.json' $environmentReport.dependency_providers
$patchRecords=@($lock.cycles.patches | ForEach-Object { @{path=$_; sha256=(Get-FileHash -LiteralPath (Join-Path $taskRoot $_) -Algorithm SHA256).Hash.ToLowerInvariant()} })
$integrationJson=& $Python (Join-Path $PSScriptRoot 'release-source.py') records --source-root $taskRoot
if($LASTEXITCODE -ne 0){throw 'Could not enumerate maintained integration release inputs.'}
$integrationRecords=@($integrationJson | ConvertFrom-Json)
$manifest=@{
    schema_version=1; generated_utc=[DateTime]::UtcNow.ToString('o'); source_lock=$lock;
    applied_patches=$patchRecords; compiler=$environmentReport.compiler; configuration=$cache['CMAKE_BUILD_TYPE'];
    integration_source=$integrationRecords;
    actual_revisions=$environmentReport.actual_revisions; host_sdk=$environmentReport.sdk;
    # private: engine HEAD is cycles.authoring_tip. public: upstream plus patches/, tree equals cycles.authoring_tree.
    engine_route=$environmentReport.engine_route; engine_tree=$environmentReport.engine_tree;
    engine_upstream_commit=$environmentReport.engine_upstream_commit;
    source_fingerprint=$environmentReport.source_fingerprint;
    dependencies=$environmentReport.dependencies; build_host=$environmentReport.host; ocio=$environmentReport.ocio;
    dependency_providers=$environmentReport.dependency_providers;
    houdini_root=$cache['HOUDINI_ROOT']; cuda_version=$cache['CUDA_VERSION']; optix_version='9.1.0';
    features=$features; kernels=$kernelRecords; binary_artifacts=@($binaryRecords);
    resource_root=[IO.Path]::GetRelativePath($buildAbsolute,$resources).Replace('\','/');
    certification='development build; runtime tests recorded separately';
    redistribution='Houdini runtime libraries are not copied; dependency license audit required before release'
}

New-Item -ItemType Directory -Path (Join-Path $hostRoot 'otls') -Force | Out-Null
if($assetDecision.action -eq 'create'){
& (Join-Path $cache['HOUDINI_ROOT'] 'bin/hotl.exe') -c (Join-Path $taskRoot 'houdini/hda_source/cycles_ipr') (Join-Path $hostRoot 'otls/cycles_ipr.hda')
if($LASTEXITCODE -ne 0){throw 'Could not compile the native SOHO driver asset.'}
Invoke-StagingHython 'finalize-assets' @((Join-Path $taskRoot 'scripts/finalize-assets.py'),(Join-Path $hostRoot 'otls/cycles_ipr.hda'))
Invoke-StagingHython 'create-light-asset' @((Join-Path $taskRoot 'scripts/create-light-asset.py'),(Join-Path $hostRoot 'otls/cycles_light.hda'))
}else{
    Write-Host 'Reused immutable native ROP, light and Blender assets.'
}
$packageDirectory = Join-Path $build 'packages'
New-Item -ItemType Directory -Path $packageDirectory -Force | Out-Null
$dependencyRoot = "$taskRoot/third_party/cycles/lib/windows_x64"
$runtimePaths=@('embree','epoxy','dpcpp','level-zero') | ForEach-Object { "$dependencyRoot/$_/bin" }
$eligibilityJson=& $Python (Join-Path $PSScriptRoot 'release-package.py') package-enable --houdini $environmentReport.sdk.houdini --hdk-api $environmentReport.sdk.hdk_api --provider-json (Join-Path $resources 'sdk-dependencies.json')
if($LASTEXITCODE -ne 0){throw 'Could not compute HDK-safe package eligibility.'}
$eligibility=($eligibilityJson | ConvertFrom-Json).enable
$package=@{
    enable=$eligibility; load_package_once=$true;
    env=@(
        @{CYCLES_HOUDINI_SOURCE_ROOT=$taskRoot},
        @{CYCLES_HOUDINI_HOST_ROOT=$hostRoot},
        @{CYCLES_HOUDINI_RESOURCES=$resources.Replace('\','/')},
        @{PATH=@{value=@((Join-Path $cache['HOUDINI_ROOT'] 'bin'))+@($runtimePaths);method='prepend'}}
    );
    # Development builds load optional add-ons by default; the normal-build
    # launchers take -WithoutAssetBrowser to test the disabled installation.
    hpath=@($hostRoot,"$hostRoot/addons/asset_browser")
}
if($solaris){
$usdPlugins = Join-Path $hostRoot 'dso/usd_plugins'
New-Item -ItemType Directory -Path (Join-Path $usdPlugins 'hdCycles/resources') -Force | Out-Null
Write-FrozenJson 'dso/usd_plugins/plugInfo.json' @{Includes=@('*/resources/')}
$plugin=@{Plugins=@(@{
    Name='hdCycles';Type='library';Root=$build.Replace('\','/');
    LibraryPath="$($build.Replace('\','/'))/cycles/src/hydra/hdCycles.dll";
    ResourcePath=$resources.Replace('\','/');
    Info=@{Types=@{HdCyclesPlugin=@{bases=@('HdRendererPlugin');displayName='Cycles (Development)';priority=0}}}
})}
Write-FrozenJson 'dso/usd_plugins/hdCycles/resources/plugInfo.json' $plugin
}
if($assetDecision.action -eq 'create'){
    # Generate help from the operators in this exact staged host. Keep its registration
    # private until both asset generation and sealing have succeeded.
    $authoringPackages=Join-Path $build 'help-authoring-packages'
    New-Item -ItemType Directory -Path $authoringPackages -Force | Out-Null
    $package | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath (Join-Path $authoringPackages 'cycles.json') -Encoding utf8
    $previousPackages=$env:HOUDINI_PACKAGE_DIR
    try{
        $env:HOUDINI_PACKAGE_DIR=$authoringPackages
        # The Blender File asset wraps the native cycles::blender_reference SOP, so it is
        # authored here, where this host's package (and its DSO) is loaded.
        Invoke-StagingHython 'create-blender-assets' @((Join-Path $taskRoot 'scripts/create-blender-assets.py'),(Join-Path $hostRoot 'otls'))
        Invoke-StagingHython 'generate-node-help' @((Join-Path $taskRoot 'scripts/generate-node-help.py'),(Join-Path $hostRoot 'help'),'--coverage',(Join-Path $build 'help-coverage.json'))
        Invoke-StagingHython 'generate-property-help' @((Join-Path $taskRoot 'scripts/generate-property-help.py'),(Join-Path $hostRoot 'help'))
        Invoke-StagingHython 'generate-viewer-help-icons' @((Join-Path $taskRoot 'scripts/generate-viewer-help-icons.py'),(Join-Path $hostRoot 'help'))
    }finally{$env:HOUDINI_PACKAGE_DIR=$previousPackages}
    & $Python $assetTool seal --source-root $taskRoot --host-root $hostRoot --houdini-root $cache['HOUDINI_ROOT'] --expected $assetDecision.recipe_sha256 --build-directory $build @assetOptions
    if($LASTEXITCODE -ne 0){throw 'Could not seal the generated native assets. Do not publish this host.'}
}
$hostRuntimeFiles=@(Get-StagingRuntimeFiles $hostRoot)
$manifest.host_runtime=@($hostRuntimeFiles | Sort-Object | ForEach-Object {
    @{path=[IO.Path]::GetRelativePath($buildAbsolute,$_).Replace('\','/');bytes=(Get-Item -LiteralPath $_).Length;sha256=(Get-FileHash -LiteralPath $_ -Algorithm SHA256).Hash.ToLowerInvariant()}
})
Publish-BuildJson 'build-manifest.json' $manifest $true
Publish-BuildJson 'packages/cycles_houdini_dev.json' $package
Write-Host "Staged $($kernelRecords.Count) kernels and recorded $($binaryRecords.Count) compiled artifacts in the development build manifest."
