# SPDX-License-Identifier: Apache-2.0
# Get the sources and build for one Houdini build (menu item "Update the sources and rebuild" of
# "START HERE - Build Cycles for Houdini.cmd", and part of its first run): gets the newest sources
# (when this folder came from git clone and has no local changes, and only after asking), downloads
# the engine and its dependencies (scripts/bootstrap.ps1) and builds (scripts/build.ps1) into the
# build folder of that Houdini build. Which Houdini build: CYCLES_EASY_TARGET_HOUDINI/_ROOT (set by
# scripts/easy-start.ps1), else scripts/easy-houdini.ps1 Get-EasyHoudiniTarget. Without a CUDA
# compiler it makes a CPU-only build; DLSS is downloaded and built only when build-identity.json
# does not say "off" and there is an NVIDIA graphics card. Safe to run again: unchanged parts are
# checked, not downloaded or compiled again.
[CmdletBinding()]
param([switch]$NoUpdate, [int]$Jobs = 6)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'easy-common.ps1')
trap { Write-EasyCrash $_; exit 1 }
$root = $script:EasyRoot
Write-EasyTitle 'Get the sources and build'
$git = Find-EasyGit
$python = Find-EasyPython
if (-not $git -or -not $python) {
    Write-EasyResult $false 'Git or Python is missing.' ((Get-EasyAgain 'Check this computer again') + '.')
    exit 1
}
$identity = Get-EasyBuildIdentity
if (-not $identity) {
    Write-EasyResult $false 'build-identity.json needs fixing (message above).' 'Correct it as described in docs/dev/community-builds.md, then run this step again.'
    exit 1
}
Write-EasyInfo "Build identity: $($identity.build_name) ($($identity.build_id)), DLSS $($identity.dlss)"
$engine = Join-Path $root 'third_party\cycles'
$target = Get-EasyHoudiniTarget
$build = Get-EasyBuildDirectory
Write-EasyInfo "For Houdini $($target.Version) ($($target.Root))"
if (-not (Test-Path -LiteralPath (Join-Path $build 'build-manifest.json'))) { Write-EasyUntestedNote $target.Version }
# Never rebuild under a running Houdini that has this build loaded.
$using = @(Get-EasyProcessesUsingBuild $target $build)
if ($using.Count) {
    Write-EasyResult $false (Get-EasyCloseHoudiniSentence $target.Version) ('Then ' + (Get-EasyAgain) + '. (Running now: ' + ($using -join ', ') + '.)')
    exit 1
}
$watch = [Diagnostics.Stopwatch]::StartNew()

# Is the engine checkout in step with source.lock.json and free of unsaved engine edits?
function Get-EngineState {
    if (-not (Test-Path -LiteralPath $engine)) { return 'absent' }
    $route = & $python -I (Join-Path $PSScriptRoot 'engine-fork.py') route 2>$null
    if ($LASTEXITCODE -ne 0) { return 'changed' }
    $dirty = & $git -C $engine status --porcelain --untracked-files=normal 2>$null
    if ($dirty) { return 'changed' }
    return 'in-step'
}

Write-EasyStep 'Newer sources'
$before = Get-EngineState
if ($NoUpdate) {
    Write-EasyInfo 'Skipped (-NoUpdate).'
} elseif (-not (Test-EasyGitCheckout $git)) {
    # A GitHub "Download ZIP" has no Git history, so it cannot fetch newer sources by itself.
    Write-EasyInfo 'This folder was downloaded as a ZIP, so it cannot update itself. For newer sources, download the'
    Write-EasyInfo 'ZIP again, unpack it into a new folder and double-click the START HERE file there.'
} else {
    $upstream = & $git -C $root rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $upstream) {
        Write-EasyInfo 'This folder has no "upstream" to update from, so the sources stay as they are.'
    } elseif ((Get-EasySourceChanges $git @('--untracked-files=no')).Count) {
        Write-EasyInfo 'You have changed files here, so the sources are not updated automatically (nothing was changed).'
    } elseif ($before -eq 'changed') {
        Write-EasyInfo 'The engine (third_party\cycles) has changes that are not in patches/ yet, so the sources'
        Write-EasyInfo 'are not updated. First choose "I changed the engine: update the patch files" in the menu.'
    } else {
        & $git -C $root fetch --quiet
        if ($LASTEXITCODE -ne 0) {
            Write-EasyResult $false 'Git could not fetch newer sources (the reason is above).' ('Check the connection and ' + (Get-EasyAgain 'Update the sources and rebuild') + '.')
            exit 6
        }
        $behind = [int](& $git -C $root rev-list --count 'HEAD..@{u}')
        if ($LASTEXITCODE -ne 0) { throw 'Git could not read the fetched upstream history.' }
        $ahead = [int](& $git -C $root rev-list --count '@{u}..HEAD')
        if ($LASTEXITCODE -ne 0) { throw 'Git could not read the local history.' }
        if ($behind -eq 0) { Write-EasyOk 'Already up to date.' }
        elseif ($ahead -gt 0) { Write-EasyInfo "Both you and $upstream have new commits; update by hand (git pull)." }
        elseif (Read-EasyYesNo "   $behind newer change(s) are available from $upstream. Download them now?") {
            & $git -C $root merge --ff-only --quiet '@{u}'
            if ($LASTEXITCODE -ne 0) { Write-EasyResult $false 'Updating the sources failed (message above). Nothing was built.' 'Run this step again; if it fails again, open an issue on the project page on GitHub with the lines above.'; exit 1 }
            Write-EasyOk "Updated to $((& $git -C $root rev-parse --short HEAD))."
        }
    }
}
# After an update the patches may describe a newer engine. The old engine had nothing that is
# not in the previous patches (checked above), so it is moved aside, never deleted.
if ($before -eq 'in-step' -and (Get-EngineState) -ne 'in-step') {
    $aside = $engine + '.previous-' + [DateTime]::Now.ToString('yyyyMMdd-HHmmss')
    Write-EasyInfo "The engine patches changed: the old engine is moved to $aside (you may delete it later)."
    Move-Item -LiteralPath $engine -Destination $aside
}

Write-EasyStep 'Engine source and dependencies (first time: about 1 GB download)'
$nvidia = Get-EasyNvidiaPlan $identity -Ask
$cuda = Get-EasyCudaArguments $nvidia
$houdiniArguments = @('-HoudiniRoot', $target.Root, '-Python', $python)
$bootstrapArguments = $houdiniArguments + @('-HoudiniVersion', $target.Version)
$buildExtra = @()
if ($cuda.Count) { Write-EasyInfo "CUDA toolkit: $($cuda[1])"; $bootstrapArguments += $cuda }
else {
    $why = if (-not $nvidia.Nvidia) { 'No NVIDIA graphics card was found' }
        elseif (-not $nvidia.Allowed) { 'NVIDIA downloads were not approved' }
        else { 'No CUDA compiler is available' }
    Write-EasyInfo "$why. This build renders on the CPU and skips OptiX and DLSS."
    $bootstrapArguments += @('-NoCuda', '-SkipOptix'); $buildExtra += '-NoCuda'
}
if (-not $nvidia.Dlss) { $bootstrapArguments += '-SkipDlss' }
if (-not (Invoke-EasyScript 'bootstrap.ps1' $bootstrapArguments)) {
    if ((Get-EngineState) -eq 'changed') {
        Write-EasyResult $false 'The engine (third_party\cycles) has changes that are not saved in patches\ yet, so it cannot be checked.' ((Get-EasyAgain 'I changed the engine: update the patch files') + ', then "Update the sources and rebuild".')
    } else {
        Write-EasyResult $false 'Downloading or checking the engine failed (message above).' ('If a download broke off, ' + (Get-EasyAgain) + '. If it says a program is missing, choose "Check this computer again".')
    }
    exit 1
}

Write-EasyStep "Build for Houdini $($target.Version) (first time: about 15 minutes; later only what changed)"
$arguments = $houdiniArguments + @('-BuildDirectory', $build, '-Jobs', [string]$Jobs) + $cuda + $buildExtra + (Get-EasyBuildArguments $identity -NoDlss:(-not $nvidia.Dlss)) + (Get-EasyKernelCacheArguments)
if (-not (Invoke-EasyScript 'build.ps1' $arguments)) {
    Write-EasyResult $false "The build for Houdini $($target.Version) failed (message above)." 'If you changed engine files, choose "I changed the engine: update the patch files" in the menu first. Otherwise search for the error message, or open an issue on the project page on GitHub with the lines above.'
    exit 1
}
# What this build was made from: shown as "built from older sources" after an update.
$stamp = [ordered]@{}
$stamp['schema_version'] = 1
$stamp['houdini'] = $target.Version
$stamp['houdini_root'] = $target.Root
$stamp['source_id'] = Get-EasySourceId
$stamp['built_utc'] = [DateTime]::UtcNow.ToString('o')
$stamp['seconds'] = [math]::Round($watch.Elapsed.TotalSeconds)
$stamp['cuda'] = [bool]$cuda.Count
$stamp['dlss'] = [bool]$nvidia.Dlss
# GPU kernels of this build: reused, compiled or unchanged (scripts/kernel-cache.py report), for the time estimate.
$kernelSummary = Join-Path $build 'kernel-reuse\last-build.json'
$stamp['gpu_programs'] = $null
if ($cuda.Count -and (Test-Path -LiteralPath $kernelSummary)) { try { $stamp['gpu_programs'] = [string](Get-Content -LiteralPath $kernelSummary -Raw | ConvertFrom-Json).state } catch { } }
Set-Content -LiteralPath (Join-Path $build 'easy-build.json') -Value ($stamp | ConvertTo-Json) -Encoding utf8
Write-EasyResult $true "Built for Houdini $($target.Version) into $build" ((Get-EasyAgain 'Run the quick tests') + ', then "Make the installers again".')
exit 0
