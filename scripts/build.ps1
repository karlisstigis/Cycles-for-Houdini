# SPDX-License-Identifier: Apache-2.0
[CmdletBinding()]
param(
    [string]$HoudiniRoot = '',
    [string]$BuildDirectory = '',
    [string]$UserPrefDirectory = '',
    [string]$CudaRoot = 'C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.8',
    [string]$CMake = '',
    [string]$Python = '',
    [string]$Ninja = '',
    [int]$Jobs = 6,
    [switch]$ConfigureOnly,
    [switch]$ImageIris = $true,
    [switch]$Dlss = $true,
    [string]$DlssSdkRoot = '',
    # Community builds only (scripts/easy-*.ps1): DLSS project id from build-identity.json; 'none'
    # clears an earlier one. Not passed (the official route): the engine's own id, as always.
    [string]$DlssProjectId,
    [switch]$Solaris,
    # CPU-only build (the double-click file uses it when there is no CUDA compiler): no CUDA or OptiX.
    [switch]$NoCuda,
    # Community builds only (scripts/easy-*.ps1): the folder in which the GPU kernel files are kept
    # and reused by further builds (scripts/kernel-cache.py), or 'off'. Not passed (the official
    # route, scripts/build-release.ps1): nothing is reused, nvcc compiles the kernels as always.
    [string]$KernelCache = '',
    [string[]]$Targets = @('cycles', 'ch_harness', '_cycles_houdini', 'ch_worker')
)
$ErrorActionPreference = 'Stop'
if (-not $Python) { $Python = (Get-Command python -CommandType Application | Select-Object -First 1).Source }
. (Join-Path $PSScriptRoot 'build-layout.ps1')
$taskRoot = Split-Path -Parent $PSScriptRoot
if(-not $HoudiniRoot){
    $lockedTarget=(Get-Content -LiteralPath (Join-Path $taskRoot 'source.lock.json') -Raw | ConvertFrom-Json).certification_candidate.houdini
    $HoudiniRoot=Join-Path $env:ProgramFiles ('Side Effects Software/Houdini '+$lockedTarget)
}
$HoudiniRoot = $HoudiniRoot.Replace('\', '/')
$CudaRoot = $CudaRoot.Replace('\', '/')
$taskRoot = $taskRoot.Replace('\', '/')
. (Join-Path $PSScriptRoot 'toolchain.ps1')
$vsRoot = Get-CyclesVisualStudioRoot
$vsTools = Get-CyclesVisualStudioTools $vsRoot
if (-not $CMake) { $CMake = $vsTools.CMake }
if (-not $CMake -or -not (Test-Path -LiteralPath $CMake -PathType Leaf)) { throw 'CMake is required. Set -CMake or install Visual Studio with the C++ CMake tools.' }
if (-not $Ninja) {
    $found = Get-Command ninja.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    $Ninja = if ($found) { $found.Source }
        elseif ($PSBoundParameters.ContainsKey('CMake')) { Join-Path (Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $CMake))) 'Ninja/ninja.exe' }
        else { $vsTools.Ninja }
}
if(-not $Ninja -or -not(Test-Path -LiteralPath $Ninja -PathType Leaf)){throw 'Ninja is required. Set -Ninja or add ninja.exe to PATH.'}
$versionHeader=Join-Path $HoudiniRoot 'toolkit/include/SYS/SYS_Version.h'
$versionMatch=[regex]::Match((Get-Content -LiteralPath $versionHeader -Raw),'(?m)^#define SYS_VERSION_FULL "(\d+\.\d+\.\d+)"')
if(-not $versionMatch.Success){throw 'Cannot derive an exact Houdini target from the SDK header.'}
$hostVersion=$versionMatch.Groups[1].Value
$preset = if($Solaris){'solaris'}elseif(-not $ImageIris){'normal-no-iris'}else{'normal'}
$build = if($BuildDirectory){[IO.Path]::GetFullPath($BuildDirectory)}else{Get-DefaultBuildDirectory -SourceRoot $taskRoot -HoudiniVersion $hostVersion -Preset $preset}
if(-not $BuildDirectory){$earlier=Get-EarlierBuildNotice -SourceRoot $taskRoot -BuildDirectory $build; if($earlier){Write-Host $earlier}}
if($Solaris -and -not $PSBoundParameters.ContainsKey('Targets')){$Targets += 'hdCycles'}
if(-not $Solaris -and 'hdCycles' -in $Targets){throw 'The hdCycles target requires -Solaris (separate experimental build).'}
# Loaded Windows DLLs cannot be replaced. Give each development build its own
# host root; running artist sessions retain their existing binary and assets.
$nativeHostRoot = Join-Path $build ('hosts/'+[DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfff'))
& $Python (Join-Path $PSScriptRoot 'engine-fork.py') verify
if ($LASTEXITCODE -ne 0) { throw 'The Cycles checkout differs from the locked production fork.' }
$openpglRepo=Join-Path $taskRoot 'third_party/cycles/lib/windows_x64'
$openpglLibrary=Join-Path $openpglRepo 'openpgl/lib/openpgl.lib'
if(-not(Test-Path -LiteralPath $openpglLibrary -PathType Leaf) -or (Get-Item -LiteralPath $openpglLibrary).Length -lt 1000000){
    # A busy connection or server: a few more tries with a short growing pause; the hash check below stays the judge.
    for($try=1;$try -le 4;$try++){
        & git -C $openpglRepo lfs pull --include=openpgl/lib/openpgl.lib --exclude=
        if($LASTEXITCODE -eq 0){break}
        if($try -eq 4){throw 'The download from GitHub (Git LFS, the OpenPGL library) did not answer, your internet connection or the server may be busy: double-click the file again (nothing is left half-installed).'}
        Write-Host "   try $try of 4 did not finish (git lfs pull); trying again in $(5*[math]::Pow(2,$try-1)) s"
        Start-Sleep -Seconds (5*[math]::Pow(2,$try-1))
    }
}
if((Get-FileHash -LiteralPath $openpglLibrary -Algorithm SHA256).Hash.ToLowerInvariant() -ne '7281ef56ec70b38aa20e8eec81a62c79aad92ad0fede596997f4b93ae8690a1a'){
    throw 'OpenPGL release library differs from the pinned dependency LFS object.'
}
& $Python (Join-Path $PSScriptRoot 'sdk-dependencies.py') --houdini-root $HoudiniRoot
if ($LASTEXITCODE -ne 0) { throw 'Pinned Houdini SDK dependency validation failed.' }
if (-not (Get-Command rc.exe -ErrorAction SilentlyContinue) -or -not (Get-Command cl.exe -ErrorAction SilentlyContinue)) {
    foreach ($entry in (Get-CyclesVisualStudioEnvironment $vsRoot).GetEnumerator()) {
        [Environment]::SetEnvironmentVariable($entry.Key, $entry.Value, 'Process')
    }
}
$argsConfigure = @('--preset', $preset, '-S', $taskRoot, '-B', $build,
    "-DCMAKE_MAKE_PROGRAM=$Ninja", "-DCH_NATIVE_HOST_ROOT=$nativeHostRoot",
    "-DHOUDINI_ROOT=$HoudiniRoot", "-DHoudini_DIR=$HoudiniRoot/toolkit/cmake",
    "-DCUDA_TOOLKIT_ROOT_DIR=$CudaRoot")
if($DlssSdkRoot){$argsConfigure += "-DDLSS_SDK_ROOT=$DlssSdkRoot"}
if($DlssProjectId){$argsConfigure += "-DCH_DLSS_PROJECT_ID=$(if($DlssProjectId -eq 'none'){''}else{$DlssProjectId})"}
if(-not $Dlss){$argsConfigure += '-DCH_DLSS=OFF'}
if($NoCuda){$argsConfigure += @('-DWITH_CYCLES_DEVICE_CUDA=OFF','-DWITH_CYCLES_DEVICE_OPTIX=OFF','-DWITH_CYCLES_CUDA_BINARIES=OFF')}
if($Solaris -and -not $ImageIris){$argsConfigure += '-DCH_IMAGE_IRIS=OFF'}
# GPU kernel reuse (-KernelCache <folder>): the engine's kernel rules run kernel-reuse/nvcc.cmd in
# place of nvcc, through the engine's own CUDA<major>_TOOLKIT_ROOT_DIR / CUDA<major>_NVCC_EXECUTABLE
# settings (src/kernel/device/cuda/CMakeLists.txt). It compiles with nvcc or copies a kernel file
# made from exactly the same inputs (scripts/kernel-cache.py). A build folder that reused kernels
# before is set back to nvcc when reuse is not asked for; otherwise nothing is added here.
$kernelReuse=$false
if($KernelCache){Remove-Item -LiteralPath (Join-Path $build 'kernel-reuse/last-build.json') -Force -ErrorAction SilentlyContinue}
$cmakeCachePath=Join-Path $build 'CMakeCache.txt'
if(Test-Path -LiteralPath $cmakeCachePath -PathType Leaf){
    foreach($line in @(Get-Content -LiteralPath $cmakeCachePath | Where-Object {$_ -match '^CUDA\d+_NVCC_EXECUTABLE:[A-Z]+=.*/kernel-reuse/nvcc\.cmd$'})){
        $major=[regex]::Match($line,'^CUDA(\d+)_').Groups[1].Value
        $argsConfigure += @("-UCUDA${major}_NVCC_EXECUTABLE","-UCUDA${major}_TOOLKIT_ROOT_DIR")
    }
}
if($KernelCache -and $KernelCache -ne 'off' -and -not $NoCuda){
    $nvcc=Join-Path $CudaRoot 'bin/nvcc.exe'
    $nvccRelease=[regex]::Match(((& $nvcc --version) -join "`n"),'release (\d+)\.(\d+)')
    if($LASTEXITCODE -ne 0 -or -not $nvccRelease.Success){throw "Cannot read the version of the CUDA compiler $nvcc."}
    $major=$nvccRelease.Groups[1].Value
    $prepared=(& $Python -I (Join-Path $PSScriptRoot 'kernel-cache.py') prepare --build $build --cache ([IO.Path]::GetFullPath($KernelCache)) --nvcc $nvcc --cuda-root $CudaRoot --check-existing) | ConvertFrom-Json
    if($LASTEXITCODE -ne 0 -or -not $prepared.wrapper){throw 'Preparing the reuse of the GPU kernels failed (message above).'}
    $argsConfigure=@($argsConfigure | Where-Object {$_ -notlike "-UCUDA${major}_*"})
    $argsConfigure += @("-DCUDA${major}_TOOLKIT_ROOT_DIR=$CudaRoot","-DCUDA${major}_NVCC_EXECUTABLE=$(([string]$prepared.wrapper).Replace('\','/'))")
    if ($prepared.invalidated.Count) { Write-Host "GPU programs whose inputs changed: $($prepared.invalidated -join ', ')" }
    $kernelReuse=$true
} elseif($KernelCache -eq 'off' -and -not $NoCuda) {
    & $Python -I (Join-Path $PSScriptRoot 'kernel-cache.py') invalidate --build $build | Out-Null
    if($LASTEXITCODE -ne 0){throw 'Could not invalidate the existing GPU programs before rebuilding them.'}
}
& $CMake @argsConfigure
if ($LASTEXITCODE -ne 0) { throw "CMake configure failed ($LASTEXITCODE)." }
& $Python (Join-Path $PSScriptRoot 'sdk-dependencies.py') --houdini-root $HoudiniRoot --cache (Join-Path $build 'CMakeCache.txt')
if ($LASTEXITCODE -ne 0) { throw 'CMake selected an unsupported OIDN provider.' }
if (-not $ConfigureOnly) {
    $ninjaLog=Join-Path $build '.ninja_log'
    $ninjaLogOffset=if(Test-Path -LiteralPath $ninjaLog){(Get-Item -LiteralPath $ninjaLog).Length}else{0}
    & $CMake --build $build --parallel $Jobs --target @Targets
    if ($LASTEXITCODE -ne 0) { throw "Build failed ($LASTEXITCODE)." }
    if($KernelCache -and -not $NoCuda){
        # One plain line: GPU kernels reused or compiled (with the measured time); keep the newest two
        # stored copies of each kernel file.
        & $Python -I (Join-Path $PSScriptRoot 'kernel-cache.py') report --build $build --ninja-log-offset $ninjaLogOffset --mode $(if($kernelReuse){'on'}else{'off'})
        if($LASTEXITCODE -ne 0){Write-Warning 'The build succeeded, but the GPU reuse report could not be written.'}
        if($kernelReuse){
            & $Python -I (Join-Path $PSScriptRoot 'kernel-cache.py') prune --cache ([IO.Path]::GetFullPath($KernelCache)) | Out-Null
            if($LASTEXITCODE -ne 0){Write-Warning 'The build succeeded, but the GPU reuse cache could not be pruned.'}
        }
    }
    & (Join-Path $PSScriptRoot 'stage-development.ps1') -BuildDirectory $build -UserPrefDirectory $UserPrefDirectory -Python $Python
}
