# SPDX-License-Identifier: Apache-2.0
# Checks what this computer needs to build Cycles for Houdini and installs what is missing,
# after asking. Run by "START HERE - Build Cycles for Houdini.cmd" (scripts/easy-start.ps1);
# Unattended runs and the fresh-clone checks may run it directly.
#   1. finds what is missing, changing nothing;
#   2. with an NVIDIA card, asks separately whether it may fetch the listed NVIDIA parts;
#      it lists missing tools and administrator requirements: y = all, a = ask for each, n = none;
#   3. installs (winget for programs; scripts/cuda-toolkit-parts.py for the CUDA compiler parts)
#      and checks everything again in this same window (PATH is re-read, nothing to reopen).
# Never installed: Houdini (it shows the download page) and any NVIDIA display driver. The
# CUDA compiler parts come from NVIDIA's ZIP archives, not from NVIDIA's installer, so the
# display driver cannot be touched; the driver version is only read and compared.
# Houdini: any installed Houdini 22.0 build with its development kit will do (scripts/easy-houdini.ps1);
# each is listed. The CUDA compiler parts are offered only with an NVIDIA graphics card; without
# them the build is CPU-only, which is not an error. The list also names NVIDIA's DLSS headers,
# which the build downloads (DLSS on, NVIDIA card present), and says in one or two lines what the
# result is: an installer per Houdini build, and which parts of it are made from NVIDIA's files.
# Inno Setup 6 (it compiles the installers) is on the list too; without it the build and the quick
# tests still run, only no installer is made (said plainly, not counted as missing).
# Safe to run again: it only installs what is still missing.
#
# Runs in Windows PowerShell 5.1 too, because PowerShell 7 may be the first thing missing.
#   -CheckOnly               only report, never offer an install
# Unattended: CYCLES_EASY_ANSWER=yes answers "all", =no answers "none".
# CYCLES_EASY_NVIDIA_CONSENT=yes|no separately controls NVIDIA downloads. CYCLES_EASY_INSTALL_ONLY
# (comma-separated keys: pwsh, git, lfs, lfs-setup, python, buildtools, cuda, inno) limits what may
# be installed at all; the fresh-clone checks use "cuda" so that they never install a program.
# Exit codes: 0 everything needed is here; 3 something is still missing (the NEXT line says
# what to do); 4 Windows must restart to finish an install; 1 unexpected error.
[CmdletBinding()]
param([switch]$CheckOnly, [switch]$ReviewNvidiaConsent)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'easy-common.ps1')
trap { Write-EasyCrash $_; exit 1 }
$lock = Get-EasyLock
$houdiniFamily = Get-EasySupportedHoudini
$cudaVersion = $lock.certification_candidate.cuda_toolkit
. (Join-Path $PSScriptRoot 'toolchain.ps1')
$buildTools = Get-CyclesVisualStudioRoot -AllowIncomplete
$vsInstaller = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\setup.exe'
$vsComponents = @('Microsoft.VisualStudio.Workload.VCTools', 'Microsoft.VisualStudio.Component.VC.14.42.17.12.x86.x64',
    'Microsoft.VisualStudio.Component.Windows11SDK.26100', 'Microsoft.VisualStudio.Component.VC.CMake.Project')
# winget's "restart needed" results (winget-cli returnCodes.md) and the Windows installer's 3010/1641.
$restartCodes = @(-1978334967, -1978334966, -1978334965, 3010, 1641)

Write-EasyTitle 'Check this computer'
Write-Host 'This looks for the programs needed to build Cycles for Houdini. Nothing is installed'
Write-Host 'before you have seen the list and said yes.'

$winget = Get-Command winget.exe -ErrorAction SilentlyContinue
if (-not $winget) {
    # winget lives in the per-user WindowsApps folder, which is not always on PATH.
    $alias = Join-Path $env:LOCALAPPDATA 'Microsoft\WindowsApps\winget.exe'
    if (Test-Path -LiteralPath $alias) { $winget = Get-Command $alias -ErrorAction SilentlyContinue }
}

# --- What is there ---------------------------------------------------------------------------
function Test-Pwsh { [bool](Find-EasyPwsh) }
function Test-Lfs { $git = Find-EasyGit; return ($git -and (Invoke-EasyProgram $git @('lfs', 'version')).ExitCode -eq 0) }
function Test-LfsFilter {
    # Git for Windows sets the LFS filter up in its system settings; any scope counts.
    $git = Find-EasyGit
    return ($git -and [bool](Invoke-EasyProgram $git @('config', '--get', 'filter.lfs.process')).Output)
}
function Test-BuildTools {
    $root = Get-CyclesVisualStudioRoot
    if (-not $root) { return $false }
    $tools = Get-CyclesVisualStudioTools $root
    if (-not (Test-Path -LiteralPath $tools.CMake -PathType Leaf) -or -not (Test-Path -LiteralPath $tools.Ninja -PathType Leaf)) { return $false }
    try { $null = Get-CyclesVisualStudioEnvironment $root; return $true }
    catch { return $false }
}
function Test-Cuda { [bool](Get-EasyCudaRoot) }
$houdiniInstalls = @(Get-EasyHoudiniInstalls)
function Test-Houdini { return [bool]@($houdiniInstalls | Where-Object { $_.Kit }).Count }
$support = Get-EasyDriverSupport
$identity = $null
try { $identity = Get-Content -LiteralPath (Join-Path $script:EasyRoot 'build-identity.json') -Raw | ConvertFrom-Json } catch { }
$nvidia = Get-EasyNvidiaPlan $identity -Driver $support.Driver -Ask:(-not $CheckOnly) -Review:$ReviewNvidiaConsent
# Download size of the CUDA compiler parts, from the tool's pinned list (about 121 MB).
$cudaPartsMb = 121
$pythonNow = Find-EasyPython
if ($pythonNow) {
    $plan = Invoke-EasyProgram $pythonNow @('-I', (Join-Path $PSScriptRoot 'cuda-toolkit-parts.py'), 'plan')
    if ($plan.ExitCode -eq 0) { $cudaPartsMb = [math]::Round((($plan.Output -join "`n") | ConvertFrom-Json).download_bytes / 1MB) }
}

# Each item: how to see it, and how to install it. Admin: 'yes' = Windows asks for administrator
# permission; 'may' = it may ask; 'no' = it does not.
$items = New-Object System.Collections.Generic.List[object]
function Add-Item([string]$Key, [string]$Name, [scriptblock]$Test, [string]$How, [string]$Admin, [string]$Size, [scriptblock]$Install, [string]$Manual, [bool]$Required = $true) {
    $items.Add([pscustomobject]@{ Key = $Key; Name = $Name; Test = $Test; How = $How; Admin = $Admin; Size = $Size; Install = $Install; Manual = $Manual; Required = $Required; Result = $null })
}
$installOnly = @()
if ($env:CYCLES_EASY_INSTALL_ONLY) { $installOnly = @(($env:CYCLES_EASY_INSTALL_ONLY -split ',') | ForEach-Object { $_.Trim() } | Where-Object { $_ }) }
function Install-WithWinget([string]$Id, [string[]]$Extra = @()) {
    & $winget.Source install --id $Id --exact --source winget --accept-package-agreements --accept-source-agreements @Extra | Out-Host
    return [int]$LASTEXITCODE
}
Add-Item pwsh 'PowerShell 7' { Test-Pwsh } 'winget Microsoft.PowerShell' 'yes' 'about 100 MB' { Install-WithWinget 'Microsoft.PowerShell' } 'https://aka.ms/powershell'
Add-Item git 'Git (with Git LFS)' { [bool](Find-EasyGit) } 'winget Git.Git' 'yes' 'about 350 MB' { Install-WithWinget 'Git.Git' } 'https://git-scm.com'
Add-Item lfs 'Git LFS' { Test-Lfs } 'winget GitHub.GitLFS' 'yes' 'about 30 MB' { Install-WithWinget 'GitHub.GitLFS' } 'https://git-lfs.com'
Add-Item lfs-setup 'Git LFS set up ("git lfs install", writes .gitconfig in your user folder)' { Test-LfsFilter } 'git lfs install' 'no' '' { $git = Find-EasyGit; & $git lfs install | Out-Host; [int]$LASTEXITCODE } 'run "git lfs install" once'
Add-Item python 'Python 3.10.12 or newer' { [bool](Find-EasyPython) } 'winget Python.Python.3.12' 'may' 'about 100 MB' { Install-WithWinget 'Python.Python.3.12' } 'https://www.python.org/downloads/windows/'
$override = '--passive --wait --norestart ' + (($vsComponents | ForEach-Object { '--add ' + $_ }) -join ' ')
Add-Item buildtools 'Visual Studio 2022 Build Tools (C++ compiler 14.42, Windows SDK, CMake, Ninja)' { Test-BuildTools } $(if ($buildTools) { 'Visual Studio Installer: add the missing parts' } else { 'winget Microsoft.VisualStudio.2022.BuildTools' }) 'yes' $(if ($buildTools) { 'about 3 GB, 10 to 20 minutes' } else { 'about 6 GB, 10 to 30 minutes' }) {
    if ($buildTools -and (Test-Path -LiteralPath $vsInstaller)) {
        $adds = @(); foreach ($c in $vsComponents) { $adds += @('--add', $c) }
        & $vsInstaller modify --installPath $buildTools @adds --passive --norestart --wait | Out-Host
        return [int]$LASTEXITCODE
    }
    Install-WithWinget 'Microsoft.VisualStudio.2022.BuildTools' @('--override', $override)
} 'https://visualstudio.microsoft.com/downloads/ (Build Tools for Visual Studio 2022; select "Desktop development with C++" and "MSVC v143 - 14.42")'
# Only with an NVIDIA graphics card, and not required: without them the build is CPU-only. They go into the project
# folder (<project>\_work\cuda-<version>); a complete copy that an earlier version of this tool downloaded to
# %LOCALAPPDATA%\CyclesForHoudiniTools is copied from there instead of downloading again (the old one is left as it is).
$cudaParts = Get-EasyCudaPartsFolder
$earlierCuda = Join-Path (Get-EasyEarlierToolsFolder) (Split-Path -Leaf $cudaParts)
$earlierCudaComplete = (-not $env:CYCLES_CUDA_ROOT) -and $pythonNow -and (Test-Path -LiteralPath $earlierCuda) -and [bool](Get-EasyCudaParts $earlierCuda).complete
$cudaHow = if ($earlierCudaComplete) { "a copy of the parts an earlier version of this tool downloaded ($earlierCuda), into $cudaParts; you may delete the old folder afterwards" } else { "NVIDIA's compiler, headers and runtime library from NVIDIA's download archives (no installer, no display driver), into $cudaParts" }
Add-Item cuda "CUDA $cudaVersion compiler parts (for GPU rendering)" { Test-Cuda } ($cudaHow + "; NVIDIA's CUDA licence applies: https://docs.nvidia.com/cuda/eula/index.html") 'no' $(if ($earlierCudaComplete) { 'no download' } else { "about $cudaPartsMb MB" }) {
    if ($earlierCudaComplete) {
        New-Item -ItemType Directory -Path (Split-Path -Parent $cudaParts) -Force | Out-Null
        # A half-finished copy of our own (in _work) is replaced, never merged.
        if (Test-Path -LiteralPath $cudaParts) { Remove-Item -LiteralPath $cudaParts -Recurse -Force }
        Copy-Item -LiteralPath $earlierCuda -Destination $cudaParts -Recurse -Force
        if (-not (Get-EasyCudaParts $cudaParts).complete) { return 1 }
        Write-EasyInfo "Copied. The old folder $earlierCuda is no longer used; you may delete it."
        return 0
    }
    $arguments = @('-I', (Join-Path $PSScriptRoot 'cuda-toolkit-parts.py'), 'get', '--root', $cudaParts)
    # Progress goes to the window (error output); the JSON summary on standard output is not shown.
    $python = Find-EasyPython
    $null = & $python @arguments
    return [int]$LASTEXITCODE
} "run: python scripts\cuda-toolkit-parts.py get --root _work\cuda-12.8.0   (or install NVIDIA's CUDA Toolkit $cudaVersion yourself: https://developer.nvidia.com/cuda-toolkit-archive, Custom install, untick the driver)" $false
# Not required: without it everything is built and tested, only no installer is made.
Add-Item inno 'Inno Setup 6 (makes the installers)' { [bool](Find-EasyInno) } 'winget JRSoftware.InnoSetup' 'yes' 'about 10 MB' { Install-WithWinget 'JRSoftware.InnoSetup' } 'https://jrsoftware.org/isdl.php' $false
$wanted = @($items | Where-Object { $_.Required -or $_.Key -eq 'inno' -or ($_.Key -eq 'cuda' -and $nvidia.Allowed) })

Write-EasyStep 'What is on this computer'
$houdiniOk = Test-Houdini
foreach ($install in $houdiniInstalls) {
    if ($install.Kit) { Write-EasyOk ("Houdini $($install.Version) with its development kit" + $(if ($install.Tested) { ' (the tested build)' } else { '' })) }
    else { Write-EasyInfo "Houdini $($install.Version) is installed without its development kit, so Cycles cannot be built for it." }
}
if (-not $houdiniOk) { Write-EasyMissing "Houdini $houdiniFamily with its development kit (you install it yourself; see below)" }
$todo = New-Object System.Collections.Generic.List[object]
foreach ($item in $wanted) {
    # Items that depend on an earlier one (Git LFS on Git, CUDA parts on Python) are decided after it.
    if (& $item.Test) { Write-EasyOk $item.Name } else { Write-EasyMissing $item.Name; $todo.Add($item) }
}
# Git for Windows brings Git LFS and sets it up: do not offer those separately while Git itself is missing.
if ($todo | Where-Object { $_.Key -eq 'git' }) { $todo = [System.Collections.Generic.List[object]]@($todo | Where-Object { $_.Key -notlike 'lfs*' }) }

# --- Free disk space (read only) -----------------------------------------------------------------
# The build folders and installers go next to this folder, on its drive. Too little for even one Houdini build is
# missing; enough for one but not for every Houdini build with the development kit is said in one line.
$kitCount = @($houdiniInstalls | Where-Object { $_.Kit }).Count
$pathProblem = Get-EasyPathProblem
if ($pathProblem) { Write-EasyMissing $pathProblem }
$diskProblem = Get-EasyDiskProblem 1
if ($diskProblem) { Write-EasyMissing $diskProblem }
else {
    Write-EasyOk ("Free disk space: $(Get-EasyFreeGB $script:EasyRoot) GB on the drive of this folder")
    if ($kitCount -gt 1 -and (Get-EasyDiskProblem $kitCount)) { Write-EasyInfo (Get-EasyDiskProblem $kitCount) }
}

# --- The driver (read only) ------------------------------------------------------------------
Write-EasyStep 'NVIDIA graphics card (read only)'
Write-EasyInfo $script:EasyDriverStatement
$min = $script:EasyDriverMinimum
$driverNote = $null
if (-not $support.Driver) {
    Write-EasyInfo 'No NVIDIA graphics card found: the build will be CPU-only (Cycles renders on the CPU).'
} elseif (-not $support.Driver.Version) {
    Write-EasyInfo "$($support.Driver.Gpu): the driver version could not be read."
} else {
    $v = $support.Driver.Version
    Write-EasyOk "$($support.Driver.Gpu), driver $v"
    if (-not ($support.optix -and $support.cuda)) {
        $driverNote = "Your NVIDIA driver $v is too old for GPU rendering (it needs $($min.optix.Major) or newer); an update is needed for that, and whether you update is your decision ($script:EasyDriverPage)."
        Write-EasyMissing 'driver update needed for GPU rendering (see the end)'
    }
}
# --- Install ---------------------------------------------------------------------------------
$restart = New-Object System.Collections.Generic.List[string]
$wingetItems = @($todo | Where-Object { $_.How -like 'winget *' })
if ($todo.Count -and -not $CheckOnly) {
    if ($installOnly.Count) {
        foreach ($item in @($todo | Where-Object { $installOnly -notcontains $_.Key })) { Write-EasyInfo "$($item.Name): not installed here (CYCLES_EASY_INSTALL_ONLY=$env:CYCLES_EASY_INSTALL_ONLY)." }
        $todo = [System.Collections.Generic.List[object]]@($todo | Where-Object { $installOnly -contains $_.Key })
    }
    if ($wingetItems.Count -and -not $winget) {
        Write-EasyInfo 'winget (Windows Package Manager) is missing, so programs cannot be installed automatically.'
        Write-EasyInfo 'Install "App Installer" from the Microsoft Store (https://apps.microsoft.com/detail/9nblggh4nns1), then run this again.'
        $todo = [System.Collections.Generic.List[object]]@($todo | Where-Object { $_.How -notlike 'winget *' })
    }
}
if ($todo.Count -and -not $CheckOnly) {
    Write-Host ''
    Write-Host 'To build, this computer still needs (installed in this order):' -ForegroundColor Cyan
    $n = 0
    foreach ($item in $todo) {
        $n++
        $admin = switch ($item.Admin) { 'yes' { 'Windows asks for administrator permission' } 'may' { 'Windows may ask for administrator permission' } default { 'no administrator permission' } }
        $size = if ($item.Size) { ", $($item.Size)" } else { '' }
        Write-Host ("  {0}. {1}" -f $n, $item.Name)
        Write-Host ("     {0}{1}; {2}" -f $item.How, $size, $admin)
    }
    foreach ($line in (Get-EasyInstallerLines @())) { Write-Host $line }
    if (@($todo | Where-Object { $_.Key -eq 'cuda' }).Count) { Write-Host '  Without the CUDA compiler parts the build is CPU-only (it renders on the CPU only).' }
    Write-Host 'Saying yes accepts each program''s licence. Nothing else on the computer is changed.'
    if (@($todo | Where-Object { $_.Admin -ne 'no' }).Count) { Write-Host 'When Windows asks "Do you want to allow this app to make changes to your device?", answer Yes.' }
    $choice = $null
    $preset = [string]$env:CYCLES_EASY_ANSWER
    if ($preset -eq 'yes') { $choice = 'y'; Write-Host 'Install all of these? y (CYCLES_EASY_ANSWER=yes)' }
    elseif ($preset -eq 'no') { $choice = 'n'; Write-Host 'Install all of these? n (CYCLES_EASY_ANSWER=no)' }
    else {
        for ($try = 0; $try -lt 3 -and -not $choice; $try++) {
            try { $answer = Read-Host 'Install all of these?  y = yes to all,  a = ask me for each one,  n = install nothing' } catch { $answer = 'n' }
            if ($answer -match '^\s*(y|yes|j|ja)\s*$') { $choice = 'y' } elseif ($answer -match '^\s*a\s*$') { $choice = 'a' } elseif ($answer -match '^\s*(n|no|nein)\s*$') { $choice = 'n' }
        }
        if (-not $choice) { $choice = 'n' }
    }
    if ($choice -ne 'n') {
        foreach ($item in $todo) {
            if (& $item.Test) { continue }  # brought by an earlier install (Git LFS comes with Git)
            if ($item.Key -eq 'cuda' -and -not (Find-EasyPython)) { Write-EasyInfo "$($item.Name): skipped, Python is needed first."; continue }
            if ($item.Key -like 'lfs*' -and -not (Find-EasyGit)) { continue }
            if ($choice -eq 'a' -and -not (Read-EasyYesNo "   Install $($item.Name) now?")) { $item.Result = 'declined'; continue }
            Write-EasyStep "Installing $($item.Name)"
            $code = & $item.Install
            $code = [int](@($code)[-1])
            Update-EasyProcessPath
            if ($restartCodes -contains $code) { $restart.Add($item.Name); Write-EasyInfo 'Windows must restart to finish this install.' }
            elseif ($code -ne 0) { Write-EasyInfo "The install reported a problem (exit code $code). The messages above say why." }
        }
    }
}

# --- Check again -----------------------------------------------------------------------------
Write-EasyStep 'Result'
$missing = New-Object System.Collections.Generic.List[string]
if ($diskProblem) { $missing.Add("$diskProblem Free up space on that drive (or move this folder to a drive with more space).") }
if ($pathProblem) { $missing.Add($pathProblem) }
if ($houdiniOk) { Write-EasyOk ('Houdini ' + ((@($houdiniInstalls | Where-Object { $_.Kit }) | ForEach-Object { $_.Version }) -join ', ')) } else {
    Write-EasyMissing "Houdini $houdiniFamily with its development kit"
    $without = @($houdiniInstalls | ForEach-Object { $_.Version })
    $hint = if ($without.Count) { " Houdini $($without -join ', ') is installed without it: install that build again and keep its development kit selected." } else { '' }
    $missing.Add("Houdini $houdiniFamily with its development kit: you install it yourself. Download a Houdini $houdiniFamily build from https://www.sidefx.com/download/ (Indie, Core or FX, not Apprentice; $(Get-EasyTestedHoudini) is the tested build) and keep its development kit selected.$hint")
}
foreach ($item in $wanted) {
    if (& $item.Test) { Write-EasyOk $item.Name }
    elseif ($item.Key -eq 'cuda') { Write-EasyInfo 'No CUDA compiler parts: the build will be CPU-only (it renders on the CPU only).' }
    elseif ($item.Key -eq 'inno') {
        $why = if ($item.Result -eq 'declined') { 'you said no to it' } else { 'it is not installed' }
        Write-EasyInfo "Inno Setup 6 is missing ($why): Cycles is still built and tested, but no installer is made. To get the installers, run this again and answer y."
    }
    elseif ($item.Required) {
        Write-EasyMissing $item.Name
        $why = if ($item.Result -eq 'declined') { ' (you said no)' } else { '' }
        $missing.Add("$($item.Name)$why - $($item.Manual)")
    }
}
$nvidia = Get-EasyNvidiaPlan $identity -Driver $support.Driver
if ($nvidia.Nvidia -and -not $nvidia.Allowed) {
    Write-EasyInfo 'NVIDIA downloads are disabled. Cycles will be built for the CPU. Choose "Check this computer again" to change this.'
}
if ($nvidia.Cuda) { Write-EasyInfo "CUDA toolkit used for building: $($nvidia.Cuda)" }
foreach ($line in (Get-EasyInstallerLines @(Get-EasyNvidiaInstallerParts ([bool]$nvidia.Cuda) $nvidia.Optix $nvidia.Dlss))) {
    Write-EasyInfo $line
}
if ($driverNote) { Write-Host ''; Write-Host "Note: $driverNote" -ForegroundColor Yellow }
if ($restart.Count) {
    Write-Host ''
    foreach ($line in $missing) { Write-Host "  - $line" }
    Write-EasyResult $false ('Windows must restart to finish installing: ' + ($restart -join ', ') + '.') ('Restart the computer, then ' + (Get-EasyAgain) + '.')
    exit 4
}
if ($missing.Count -eq 0) {
    Write-EasyResult $true 'Everything needed to build is on this computer.' $(if ($env:CYCLES_EASY_NESTED -eq '1') { '' } else { 'Build with "' + $script:EasyStartFile + '".' })
    exit 0
}
Write-Host ''
Write-Host 'Still missing:' -ForegroundColor Yellow
foreach ($line in $missing) { Write-Host "  - $line" }
$next = if ($CheckOnly) { 'Install them, then ' + (Get-EasyAgain) + '.' } else { 'Install what is listed (or run this again and answer y), then ' + (Get-EasyAgain) + '.' }
Write-EasyResult $false "$($missing.Count) thing(s) still missing (listed above)." $next
exit 3
