# SPDX-License-Identifier: Apache-2.0
# Downloads the engine source and pinned dependencies into third_party/ and checks them
# against source.lock.json. See docs/dev/building-from-source.md.
#
# Engine source (third_party/cycles), chosen with -EngineSource:
#   Public  - upstream Cycles (cycles.url) at cycles.commit, then patches/ applied in order;
#             the resulting Git tree must equal cycles.authoring_tree.
#   Private - a copy of the private authoring fork (-EngineRepository, default
#             artifacts/engine-authoring/locked-repo); HEAD must equal cycles.authoring_tip.
#   Auto    - Private when -EngineRepository is given or that default folder exists,
#             otherwise Public. This is the default.
# An existing third_party/cycles is never replaced, only checked.
# The double-click file (scripts/easy-get-and-build.ps1) also passes:
#   -HoudiniVersion  the Houdini build to expect at -HoudiniRoot (default: the tested build in
#                    source.lock.json); any 22.0 build with its development kit
#   -NoCuda          build without the CUDA compiler (CPU-only build): CUDA is not checked
#   -SkipDlss        do not download NVIDIA's DLSS headers (DLSS off, or no NVIDIA graphics card)
#   -SkipOptix       do not download NVIDIA's OptiX headers (CPU-only build)
[CmdletBinding()]
param(
    [switch]$SkipLfs,
    [string]$EngineRepository='',
    [ValidateSet('Auto','Public','Private')][string]$EngineSource='Auto',
    [string]$HoudiniRoot='',
    [string]$CudaRoot='C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.8',
    [string]$CMake='',
    [string]$Python='',
    [switch]$SkipToolCheck,
    [string]$HoudiniVersion='',
    [switch]$NoCuda,
    [switch]$SkipDlss,
    [switch]$SkipOptix
)
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'toolchain.ps1')
if (-not $Python) { $Python = (Get-Command python -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1).Source }
$lock = Get-Content -LiteralPath (Join-Path $taskRoot 'source.lock.json') -Raw | ConvertFrom-Json
function Assert-Prerequisites {
    # Native tools write to stderr; do not let that stop the check itself.
    $ErrorActionPreference = 'Continue'
    $missing = [Collections.Generic.List[string]]::new()
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        $missing.Add('Git, on PATH (https://git-scm.com)')
    } elseif (-not $SkipLfs) {
        & git lfs version *> $null
        if ($LASTEXITCODE -ne 0) { $missing.Add('Git LFS (https://git-lfs.com), then run: git lfs install') }
    }
    $pythonOk = $false
    if ($Python) {
        & $Python -c 'import sys, tarfile; sys.exit(0 if sys.version_info >= (3, 10) and hasattr(tarfile, ''data_filter'') else 1)' *> $null
        $pythonOk = $LASTEXITCODE -eq 0
    }
    if (-not $pythonOk) { $missing.Add('Python 3.10.12 or newer as "python" on PATH (https://www.python.org)') }
    if (-not $SkipToolCheck) {
        $version = if ($HoudiniVersion) { $HoudiniVersion } else { $lock.certification_candidate.houdini }
        $houdini = if ($HoudiniRoot) { $HoudiniRoot } else { Join-Path $env:ProgramFiles ('Side Effects Software\Houdini ' + $version) }
        $header = Join-Path $houdini 'toolkit\include\SYS\SYS_Version.h'
        $found = if (Test-Path -LiteralPath $header) { [regex]::Match((Get-Content -LiteralPath $header -Raw), '(?m)^#define SYS_VERSION_FULL "([^"]+)"').Groups[1].Value } else { '' }
        if ($found -ne $version) {
            $what = if ($found) { "found Houdini $found" } else { 'not found' }
            $missing.Add("Houdini $version with its development kit (toolkit folder) at ${houdini}: $what. Use -HoudiniRoot if it is elsewhere")
        }
        $root = Get-CyclesVisualStudioRoot
        $tools = Get-CyclesVisualStudioTools $root
        try { $null = Get-CyclesVisualStudioEnvironment $root }
        catch { $missing.Add($_.Exception.Message) }
        $cmakePath = if ($CMake) { $CMake } else { $tools.CMake }
        if (-not $cmakePath -or -not (Test-Path -LiteralPath $cmakePath -PathType Leaf)) { $missing.Add('CMake 3.24 or newer (use -CMake if not using the Visual Studio copy)') }
        $ninja = if ($CMake -and $cmakePath) { Join-Path (Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $cmakePath))) 'Ninja\ninja.exe' } else { $tools.Ninja }
        if (-not (Get-Command ninja.exe -ErrorAction SilentlyContinue) -and
            (-not $ninja -or -not (Test-Path -LiteralPath $ninja -PathType Leaf))) { $missing.Add('Ninja, on PATH or next to Visual Studio CMake') }
        if (-not $NoCuda -and -not (Test-Path -LiteralPath (Join-Path $CudaRoot 'bin\nvcc.exe') -PathType Leaf)) {
            $missing.Add("NVIDIA CUDA Toolkit $($lock.certification_candidate.cuda_toolkit) at $CudaRoot (pass -CudaRoot to bootstrap.ps1 and build.ps1 if it is elsewhere)")
        }
    }
    if ($missing.Count) {
        throw ("Missing prerequisites:`n  - " + ($missing -join "`n  - ") + "`nInstall them and run bootstrap again. -SkipToolCheck skips the Houdini, compiler and CUDA checks.")
    }
}
Assert-Prerequisites
function Invoke-Git([string[]]$Arguments, [string]$Directory = $taskRoot) {
    Push-Location -LiteralPath $Directory
    try {
        & git @Arguments
        if ($LASTEXITCODE -ne 0) { throw "git failed ($LASTEXITCODE): $($Arguments -join ' ')" }
    } finally { Pop-Location }
}
# A download over the network (clone, submodule, Git LFS): a busy connection or server gets a few more tries with a
# short growing pause (5, 10, 20 s). A clone that did not finish is removed before the next try, so nothing is left
# half-downloaded; the revision checks after each step stay the judge.
function Invoke-GitDownload([string[]]$Arguments, [string]$Directory = $taskRoot, [string]$Target = '', [string]$From = 'GitHub') {
    $attempts = 4
    for ($attempt = 1; $attempt -le $attempts; $attempt++) {
        Push-Location -LiteralPath $Directory
        try { & git @Arguments; $code = $LASTEXITCODE } finally { Pop-Location }
        if ($code -eq 0) { return }
        if ($Target -and (Test-Path -LiteralPath $Target)) { Remove-Item -LiteralPath $Target -Recurse -Force -ErrorAction SilentlyContinue }
        if ($attempt -lt $attempts) {
            $pause = 5 * [math]::Pow(2, $attempt - 1)
            Write-Host "   try $attempt of $attempts did not finish (git $($Arguments[0..2] -join ' ')); trying again in $pause s"
            Start-Sleep -Seconds $pause
        }
    }
    throw "The download from $From did not answer, your internet connection or the server may be busy: double-click the file again (nothing is left half-installed)."
}
function Assert-Revision([string]$Directory, [string]$Expected) {
    $actual = (& git -C $Directory rev-parse HEAD).Trim()
    if ($LASTEXITCODE -ne 0 -or $actual -ne $Expected) {
        throw "Revision mismatch in ${Directory}: expected $Expected, found $actual. No checkout was reset."
    }
}
function Sync-PinnedHeaders([string]$Directory, $Entry, [string]$SparsePath = '') {
    if (Test-Path -LiteralPath $Directory) {
        $revision = & git -C $Directory rev-parse HEAD
        if ($LASTEXITCODE -ne 0) { throw "Cannot identify the header checkout at $Directory." }
        $changed = & git -C $Directory status --porcelain --untracked-files=normal
        if ($LASTEXITCODE -ne 0 -or $changed) {
            throw "The header checkout at $Directory has local changes or cannot be checked. Move it aside and run the build again."
        }
        if ($revision.Trim() -eq $Entry.commit) { return }
    }

    # Verify the replacement before moving aside any existing checkout.
    $token = [Guid]::NewGuid().ToString('N').Substring(0, 12)
    $incoming = "$Directory.incoming-$token"
    $arguments = @('clone', '--depth', '1', '--branch', $Entry.tag)
    if ($SparsePath) { $arguments += @('--filter=blob:none', '--sparse') }
    Invoke-GitDownload -Arguments ($arguments + @($Entry.url, $incoming)) -Target $incoming
    Assert-Revision $incoming $Entry.commit
    if ($SparsePath) { Invoke-Git -Arguments @('sparse-checkout', 'set', $SparsePath) -Directory $incoming }
    if (Test-Path -LiteralPath $Directory) {
        $previous = "$Directory.previous-$token"
        Move-Item -LiteralPath $Directory -Destination $previous
        try { Move-Item -LiteralPath $incoming -Destination $Directory }
        catch { Move-Item -LiteralPath $previous -Destination $Directory; throw }
        Write-Host "Updated the pinned headers. The previous checkout is at $previous."
    } else {
        Move-Item -LiteralPath $incoming -Destination $Directory
    }
}

$thirdParty = Join-Path $taskRoot 'third_party'
New-Item -ItemType Directory -Path $thirdParty -Force | Out-Null
$cycles = Join-Path $thirdParty 'cycles'
if (-not (Test-Path -LiteralPath $cycles)) {
    $privateDefault = Join-Path $taskRoot 'artifacts/engine-authoring/locked-repo'
    if ($EngineSource -eq 'Auto') {
        $EngineSource = if ($EngineRepository -or (Test-Path -LiteralPath $privateDefault)) { 'Private' } else { 'Public' }
    }
    if ($EngineSource -eq 'Private') {
        if(-not $EngineRepository){$EngineRepository=$privateDefault}
        if(-not(Test-Path -LiteralPath $EngineRepository)){
            throw 'Restore the preserved engine authoring repository from source recovery, or pass -EngineRepository with the committed fork. The upstream remote does not contain the private locked fork. Without the fork, use -EngineSource Public.'
        }
        & $Python (Join-Path $PSScriptRoot 'engine-fork.py') materialize --engine-root $EngineRepository --destination $cycles
        if($LASTEXITCODE -ne 0){throw 'Committed engine materialization failed; inspect the new path, no existing checkout was reset.'}
    } else {
        if ($EngineRepository) { throw '-EngineRepository belongs to the private route; use -EngineSource Private with it.' }
        Write-Host "Engine source: $($lock.cycles.url) at $($lock.cycles.commit) plus $(@($lock.cycles.patches).Count) patches from patches/"
        & $Python (Join-Path $PSScriptRoot 'engine-fork.py') materialize-public --destination $cycles
        if($LASTEXITCODE -ne 0){throw 'Public engine replay failed (message above). Nothing existing was changed; delete third_party/cycles.incomplete if it exists, then run bootstrap again.'}
    }
}
$engineRoute = & $Python (Join-Path $PSScriptRoot 'engine-fork.py') route
if($LASTEXITCODE -ne 0){throw 'third_party/cycles is neither the private fork at cycles.authoring_tip nor the public replay with cycles.authoring_tree. It was not changed.'}
if ($engineRoute -eq 'private') {
    Assert-Revision $cycles $lock.cycles.authoring_tip
    & $Python (Join-Path $PSScriptRoot 'engine-fork.py') notes
    if($LASTEXITCODE -ne 0){throw 'Engine metadata notes could not be reconstructed from the locked inventory.'}
}
& $Python (Join-Path $PSScriptRoot 'engine-fork.py') verify
if($LASTEXITCODE -ne 0){throw 'Production fork verification failed. Preserve legacy checkout and use the documented metadata-only migration.'}
$env:GIT_LFS_SKIP_SMUDGE = '1'
try {
    Invoke-GitDownload -Arguments @('-c', 'submodule.lib/windows_x64.update=checkout', 'submodule', 'update', '--init', '--depth', '1', 'lib/windows_x64') -Directory $cycles
} finally { Remove-Item Env:GIT_LFS_SKIP_SMUDGE -ErrorAction SilentlyContinue }
$dependencies = Join-Path $cycles 'lib/windows_x64'
Assert-Revision $dependencies $lock.windows_dependencies.commit
if (-not $SkipLfs) {
    Invoke-GitDownload -Arguments @('lfs', 'pull', "--include=$($lock.windows_dependencies.lfs_include)", "--exclude=$($lock.windows_dependencies.lfs_exclude)") -Directory $dependencies
}
$optix = Join-Path $thirdParty 'optix'
if ($SkipOptix) {
    Write-Host 'OptiX headers not downloaded (-SkipOptix: CPU-only build).'
} else {
    Sync-PinnedHeaders $optix $lock.optix_headers
}
$dlss = Join-Path $thirdParty 'dlss'
if ($SkipDlss) {
    Write-Host "Engine source ($engineRoute route) and pinned dependencies are ready (DLSS headers not downloaded: -SkipDlss)."
    return
}
Sync-PinnedHeaders $dlss $lock.dlss_headers 'include'
Write-Host "Engine source ($engineRoute route), pinned dependencies, DLSS and OptiX headers are ready."

