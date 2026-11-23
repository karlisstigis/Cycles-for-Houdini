# SPDX-License-Identifier: Apache-2.0
# Shared helpers for the double-click file in the repository root
# ("START HERE - Build Cycles for Houdini.cmd", scripts/easy-start.ps1) and the scripts it runs.
# Works in Windows PowerShell 5.1 and PowerShell 7: the computer check must run before
# PowerShell 7 is installed. Reads settings, never changes them.

$script:EasyRoot = Split-Path -Parent $PSScriptRoot
# Everything the one file makes stays inside the project folder: the installers in <project>\Installers, everything
# else (a build folder per Houdini build, the CUDA compiler parts, the installers' packages and reports, the state
# file) in <project>\_work. Deleting the project folder removes it all. Both are ignored by Git (.gitignore) and
# never part of a package, an installer or the public export. Developer and official builds keep their own layout
# (scripts/build-layout.ps1; docs/dev/README.md, "Working folders").
$script:EasyWork = Join-Path $script:EasyRoot '_work'
# A window opened from one Houdini build (its "Command Line Tools") carries that build's own settings
# (HFS and friends, and its bin folder on PATH). Another Houdini build's hython then loads the wrong
# files and crashes (seen: exit code 139 while staging a 22.0.508 build from a 22.0.466 window). Every
# step names its Houdini build itself, so these are cleared for this window's processes only.
foreach ($name in @('HFS', 'H', 'HB', 'HD', 'HDSO', 'HH', 'HHC', 'HHP', 'HT', 'HSB', 'HIH')) {
    # Remove-Item, not SetEnvironmentVariable($name, $null): PowerShell passes $null as an empty string there,
    # which leaves the variable defined but empty (an empty HOUDINI_PATH hides Houdini's own files).
    Remove-Item -LiteralPath ('Env:' + $name) -ErrorAction SilentlyContinue
}
if ($env:PATH) { $env:PATH = (@($env:PATH -split ';') | Where-Object { $_ -notmatch '\\Side Effects Software\\Houdini [0-9.]+\\bin\\?$' }) -join ';' }
# The same for the person's own Houdini settings (HOUDINI_PATH, HOUDINI_PACKAGE_DIR, HOUDINI_USER_PREF_DIR, HSITE,
# ...): they belong to their Houdini sessions. A build step that saw them could load their packages (an installed
# Cycles among them), or lose Houdini's own default search path. Every step sets the Houdini settings it needs itself (scripts/stage-development.ps1,
# scripts/run-hython.ps1, scripts/launch-normal-build.ps1). The preferences folder is remembered first for
# finding Setup installs (CYCLES_EASY_HOUDINI_PREFS). Licence settings are kept.
if ($env:HOUDINI_USER_PREF_DIR -and -not $env:CYCLES_EASY_HOUDINI_PREFS) { $env:CYCLES_EASY_HOUDINI_PREFS = $env:HOUDINI_USER_PREF_DIR }
foreach ($variable in @(Get-ChildItem Env: | Where-Object { ($_.Name -like 'HOUDINI_*' -or $_.Name -eq 'HSITE') -and $_.Name -notmatch 'LICENSE|LMINFO|HSERVER|TEMP_DIR' })) {
    Remove-Item -LiteralPath ('Env:' + $variable.Name) -ErrorAction SilentlyContinue
}
# The one file a person double-clicks. Messages name it and its menu items.
$script:EasyStartFile = 'START HERE - Build Cycles for Houdini.cmd'
function Get-EasyAgain([string]$Item = '') {
    if ($Item) { return "double-click `"$script:EasyStartFile`" again and choose `"$Item`"" }
    return "double-click `"$script:EasyStartFile`" again; it continues from where it stopped"
}

function Write-EasyTitle([string]$Text) {
    Write-Host ''
    Write-Host ('=' * 78) -ForegroundColor DarkGray
    Write-Host $Text -ForegroundColor Cyan
    Write-Host ('=' * 78) -ForegroundColor DarkGray
}
function Write-EasyStep([string]$Text) { Write-Host ''; Write-Host "-> $Text" -ForegroundColor White }
function Write-EasyOk([string]$Text) { Write-Host "   OK      $Text" -ForegroundColor Green }
function Write-EasyMissing([string]$Text) { Write-Host "   MISSING $Text" -ForegroundColor Yellow }
function Write-EasyInfo([string]$Text) { Write-Host "           $Text" }

# Summary block printed at the end of every step: what happened, and what to do next.
# Inside the one-step run (CYCLES_EASY_NESTED=1) a step that worked prints one line only; the
# run itself ends with the DONE / NOT DONE / NEXT block.
function Write-EasyResult([bool]$Success, [string]$What, [string]$Next) {
    if ($Success -and $env:CYCLES_EASY_NESTED -eq '1') { Write-Host ''; Write-Host "   finished: $What" -ForegroundColor Green; return }
    Write-Host ''
    Write-Host ('=' * 78) -ForegroundColor DarkGray
    if ($Success) { Write-Host "DONE: $What" -ForegroundColor Green }
    else { Write-Host "NOT DONE: $What" -ForegroundColor Red }
    if ($Next) { Write-Host "NEXT: $Next" -ForegroundColor Cyan }
    Write-Host ('=' * 78) -ForegroundColor DarkGray
}

# For "trap { Write-EasyCrash $_; exit 1 }" in each step: an unexpected error is shown with the
# line where it happened, so the person can see what to fix.
function Write-EasyCrash($ErrorRecord) {
    Write-Host ''
    Write-Host ('ERROR: ' + $ErrorRecord.Exception.Message) -ForegroundColor Red
    if ($ErrorRecord.InvocationInfo) { Write-Host $ErrorRecord.InvocationInfo.PositionMessage }
    if ($ErrorRecord.ScriptStackTrace) { Write-Host $ErrorRecord.ScriptStackTrace }
    Write-EasyResult $false 'The step stopped because of an unexpected error (details above).' 'Run it again; if it happens again, open an issue on the project page on GitHub with the lines above.'
}

# Asks a yes/no question. CYCLES_EASY_ANSWER=yes or =no answers every question
# without asking (for unattended runs); no answer counts as "no".
# -DefaultYes: Enter alone means yes (shown as [Y/n]); used only where yes changes nothing on
# the computer (for example removing a file this tool wrote itself).
function Read-EasyYesNo([string]$Question, [switch]$DefaultYes) {
    $hint = if ($DefaultYes) { '[Y/n]' } else { '[y/N]' }
    $preset = [string]$env:CYCLES_EASY_ANSWER
    if ($preset -eq 'yes') { Write-Host "$Question $hint y (CYCLES_EASY_ANSWER=yes)"; return $true }
    if ($preset -eq 'no') { Write-Host "$Question $hint n (CYCLES_EASY_ANSWER=no)"; return $false }
    try { $answer = Read-Host "$Question $hint" } catch { return $false }
    if ($DefaultYes -and -not $answer.Trim()) { return $true }
    return ($answer -match '^\s*(y|yes|j|ja)\s*$')
}
# Name of the lock that lets only one "START HERE" run work on this folder at a time.
function Get-EasyStartLockName {
    $sha = [System.Security.Cryptography.SHA256]::Create()
    $bytes = $sha.ComputeHash([Text.Encoding]::UTF8.GetBytes(([IO.Path]::GetFullPath($script:EasyRoot)).TrimEnd('\').ToLowerInvariant()))
    return 'Local\CyclesForHoudini-start-' + (-join ($bytes[0..7] | ForEach-Object { $_.ToString('x2') }))
}
# One command line for Start-Process from separate arguments (Windows quoting rules), so that
# paths with spaces arrive as one argument in Windows PowerShell 5.1 and PowerShell 7 alike.
function ConvertTo-EasyCommandLine([string[]]$Arguments) {
    $quoted = foreach ($argument in $Arguments) {
        if ($argument -eq '') { '""' }
        elseif ($argument -notmatch '[\s"]') { $argument }
        else { '"' + (($argument -replace '(\\*)"', '$1$1\"') -replace '(\\+)$', '$1$1') + '"' }
    }
    return ($quoted -join ' ')
}
function Test-EasyUnattended { return ([string]$env:CYCLES_EASY_ANSWER -in @('yes', 'no')) }

# Runs a program only for its exit code and its standard output, and never stops the step.
# Windows PowerShell 5.1 turns a program's error output into a stopping error when
# $ErrorActionPreference is 'Stop' and that output is redirected (2>$null, *>$null, 2>&1);
# PowerShell 7.2 and newer do not. Here the preference is 'Continue' (this function only).
# A program that cannot be started gives exit code -1.
function Invoke-EasyProgram([string]$Exe, [string[]]$Arguments = @()) {
    $ErrorActionPreference = 'Continue'
    $output = @()
    $code = -1
    try {
        $global:LASTEXITCODE = 0
        $output = @(& $Exe @Arguments 2>$null | ForEach-Object { [string]$_ })
        $code = [int]$global:LASTEXITCODE
    } catch {
        $code = -1
    }
    return [pscustomobject]@{ ExitCode = $code; Output = $output }
}

# Programs installed a moment ago are not yet on this window's PATH. Re-read the
# saved PATH (machine and user) into this process only; nothing is written.
# CYCLES_EASY_PATH_ONLY=1 keeps this window's PATH as it is (the fresh-clone check uses it
# to hide programs from step 1).
function Update-EasyProcessPath {
    if ($env:CYCLES_EASY_PATH_ONLY -eq '1') { return }
    $saved = @([Environment]::GetEnvironmentVariable('Path', 'Machine'), [Environment]::GetEnvironmentVariable('Path', 'User')) -join ';'
    $parts = New-Object System.Collections.Generic.List[string]
    foreach ($part in (($env:PATH + ';' + $saved) -split ';')) {
        if ($part -and -not $parts.Contains($part)) { $parts.Add($part) }
    }
    $env:PATH = $parts -join ';'
}
function Add-EasyProcessPath([string]$Directory) {
    if ($Directory -and (Test-Path -LiteralPath $Directory)) {
        $rest = @($env:PATH -split ';' | Where-Object { $_ -and $_.TrimEnd('\', '/') -ne $Directory.TrimEnd('\', '/') })
        $env:PATH = (@($Directory) + $rest) -join ';'
    }
}

function Test-EasyPython([string]$Exe) {
    if (-not $Exe -or -not (Test-Path -LiteralPath $Exe -PathType Leaf)) { return $false }
    # The Microsoft Store placeholder "python.exe" only opens the Store.
    if ($Exe -like '*\WindowsApps\*') { return $false }
    $probe = Invoke-EasyProgram $Exe @('-c', "import sys, tarfile; sys.exit(0 if sys.version_info >= (3, 10) and hasattr(tarfile, 'data_filter') else 1)")
    return ($probe.ExitCode -eq 0)
}

# Finds a Python 3.10.12 or newer and puts it first on this window's PATH as "python".
function Find-EasyPython {
    Update-EasyProcessPath
    $candidates = New-Object System.Collections.Generic.List[string]
    foreach ($command in @(Get-Command python.exe -All -ErrorAction SilentlyContinue)) { $candidates.Add($command.Source) }
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($launcher) {
        # With no Python 3 the launcher prints "No suitable Python runtime found" (error output).
        $found = Invoke-EasyProgram $launcher.Source @('-3', '-c', 'import sys; print(sys.executable)')
        if ($found.ExitCode -eq 0 -and $found.Output.Count) { $candidates.Add(([string]$found.Output[0]).Trim()) }
    }
    foreach ($base in @((Join-Path $env:LOCALAPPDATA 'Programs\Python'), $env:ProgramFiles)) {
        if ($base -and (Test-Path -LiteralPath $base)) {
            foreach ($folder in @(Get-ChildItem -LiteralPath $base -Directory -Filter 'Python3*' -ErrorAction SilentlyContinue | Sort-Object Name -Descending)) {
                $candidates.Add((Join-Path $folder.FullName 'python.exe'))
            }
        }
    }
    foreach ($exe in $candidates) {
        if (Test-EasyPython $exe) {
            $folder = Split-Path -Parent $exe
            Add-EasyProcessPath (Join-Path $folder 'Scripts')
            Add-EasyProcessPath $folder
            return $exe
        }
    }
    return $null
}

function Find-EasyGit {
    Update-EasyProcessPath
    $git = Get-Command git.exe -ErrorAction SilentlyContinue
    if (-not $git) {
        $default = Join-Path $env:ProgramFiles 'Git\cmd\git.exe'
        if (Test-Path -LiteralPath $default) { Add-EasyProcessPath (Split-Path -Parent $default); $git = Get-Command git.exe -ErrorAction SilentlyContinue }
    }
    if ($git) { return $git.Source }
    return $null
}

function Find-EasyPwsh {
    Update-EasyProcessPath
    $found = Get-Command pwsh.exe -ErrorAction SilentlyContinue
    if ($found) { return $found.Source }
    foreach ($candidate in @((Join-Path $env:ProgramFiles 'PowerShell\7\pwsh.exe'), (Join-Path $env:LOCALAPPDATA 'Microsoft\WindowsApps\pwsh.exe'))) {
        if ($candidate -and (Test-Path -LiteralPath $candidate -PathType Leaf)) { return $candidate }
    }
    return $null
}

# Inno Setup 6 compiles the installers (scripts/build-installer.ps1).
function Find-EasyInno {
    foreach ($p in @((Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6\ISCC.exe'), (Join-Path $env:ProgramFiles 'Inno Setup 6\ISCC.exe'), (Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe'))) {
        if ($p -and (Test-Path -LiteralPath $p -PathType Leaf)) { return $p }
    }
    return $null
}

# --- Path length ----------------------------------------------------------------------------
# Windows tools stop at 260 characters per path. The longest path below the project folder measured in a full run
# (fresh-clone check, 22 November 2026) is 178 characters (a file of the prebuilt libraries in third_party); the build
# folders reach 141, the installer details 128, the CUDA compiler parts 95. So the project folder's own path may be
# at most 80 characters long.
$script:EasyMaxProjectPath = 80
function Get-EasyPathProblem([string]$Root = $script:EasyRoot) {
    $full = [IO.Path]::GetFullPath($Root).TrimEnd('\')
    if ($full.Length -le $script:EasyMaxProjectPath) { return $null }
    return "The path of this folder is $($full.Length) characters long ($full); the build needs it to be at most $($script:EasyMaxProjectPath), because Windows tools stop at 260 characters. Move the folder closer to the drive root (for example D:\Cycles) and double-click the file there."
}

# --- Free disk space -----------------------------------------------------------------------
# Measured on the maintainer's computer (fresh-clone checks, 21 and 22 November 2026): the engine sources and
# pinned downloads in the project folder take about 1.3 GB the first time; each Houdini build about 1.7 GB (its
# build folder about 1.35 GB, its installer with the package and reports about 0.3 GB). The build folders and the
# installer folder are inside the project folder, so all of it goes to the project folder's drive. 1 GB spare.
$script:EasyDiskSourcesGB = 1.3
$script:EasyDiskPerHoudiniGB = 1.7
$script:EasyDiskSpareGB = 1
function Get-EasyFreeGB([string]$Path) {
    try { return [math]::Round((New-Object IO.DriveInfo([IO.Path]::GetPathRoot([IO.Path]::GetFullPath($Path)))).AvailableFreeSpace / 1GB, 1) } catch { return $null }
}
# GB a build for this many new Houdini builds needs here (the sources only when they are not downloaded yet); a
# rebuild in an existing build folder needs room for its new installer (about 0.3 GB).
function Get-EasyDiskNeedGB([int]$Builds, [int]$Rebuilds = 0) {
    $sources = if (Test-Path -LiteralPath (Join-Path $script:EasyRoot 'third_party\cycles')) { 0 } else { $script:EasyDiskSourcesGB }
    return [math]::Round($sources + $Builds * $script:EasyDiskPerHoudiniGB + $Rebuilds * 0.3 + $script:EasyDiskSpareGB, 1)
}
# One plain sentence when the drive of the project folder has too little free space for this many Houdini builds.
function Get-EasyDiskProblem([int]$Builds, [int]$Rebuilds = 0) {
    $free = Get-EasyFreeGB $script:EasyRoot
    $need = Get-EasyDiskNeedGB $Builds $Rebuilds
    $Builds += $Rebuilds
    if ($null -eq $free -or $free -ge $need) { return $null }
    $drive = [IO.Path]::GetPathRoot([IO.Path]::GetFullPath($script:EasyRoot))
    $what = if ($Builds -eq 1) { '1 Houdini build' } else { "$Builds Houdini builds" }
    return "Not enough free disk space: drive $drive has $free GB free, and building for $what needs about $need GB (sources, build folders and installers, inside this folder)."
}

# --- The installers: the result of a build -------------------------------------------------
# One folder for all installers: <project>\Installers\, one Setup per Houdini build, named after the folder Setup
# installs to ("Cycles for Houdini 22.0.508 (community) - Setup.exe"), with a READ ME.txt. What went into each
# (package, reports, logs) is in <project>\_work\installer-details\<Houdini build>-<time>.
function Get-EasyInstallerFolder { Join-Path $script:EasyRoot 'Installers' }
# Where an earlier version of this tool kept builds and installers (next to the project folder), and its CUDA
# compiler parts (CYCLES_EASY_EARLIER_TOOLS, for tests; default %LOCALAPPDATA%\CyclesForHoudiniTools). Only read.
function Get-EasyEarlierFolders {
    $parent = Split-Path -Parent $script:EasyRoot
    $leaf = Split-Path -Leaf $script:EasyRoot
    return @((Join-Path $parent ($leaf + '-build')), (Join-Path $parent ($leaf + '-installers')))
}
function Get-EasyEarlierToolsFolder {
    if ($env:CYCLES_EASY_EARLIER_TOOLS) { return $env:CYCLES_EASY_EARLIER_TOOLS }
    $base = if ($env:LOCALAPPDATA) { $env:LOCALAPPDATA } else { Join-Path $HOME 'AppData\Local' }
    return (Join-Path $base 'CyclesForHoudiniTools')
}
function Get-EasySetupFileName([string]$DefaultDirectory) { $DefaultDirectory + ' - Setup.exe' }
# What the installers will contain from NVIDIA, in plain words (from what this computer will build:
# CUDA compiler parts, NVIDIA card, DLSS setting). Empty for a CPU-only build.
function Get-EasyNvidiaInstallerParts([bool]$Cuda, [bool]$Nvidia, [bool]$Dlss) {
    $parts = @()
    if ($Cuda -and $Nvidia) {
        $parts += "GPU programs made with NVIDIA's CUDA compiler"
        $parts += "GPU programs and the OptiX denoiser built with NVIDIA's OptiX headers"
        if ($Dlss) { $parts += "DLSS, built from NVIDIA's DLSS headers" }
    }
    return $parts
}
# The one or two lines about what the build makes, shown on the first-run list (scripts/easy-check-computer.ps1).
function Get-EasyInstallerLines([string[]]$NvidiaParts) {
    $lines = @('The result is an installer (Setup) for each Houdini build you build for, for your own use. The build itself installs nothing.')
    if (@($NvidiaParts).Count) {
        $lines += ('The installers contain parts made from NVIDIA''s files: ' + ($NvidiaParts -join '; ') + '. If you give an installer to someone else, NVIDIA''s licences apply to you (docs\dev\nvidia-licences.md).')
    }
    return $lines
}

# --- CUDA toolkit -------------------------------------------------------------------------
# The build needs the CUDA compiler (nvcc), the CUDA headers and cudart.lib, nothing else
# (scripts/build.ps1 -CudaRoot). Either NVIDIA's full toolkit installed by its own installer
# (Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.8), or only those parts, unpacked by
# scripts/cuda-toolkit-parts.py from NVIDIA's ZIP archives into a folder of your own (no
# installer, no administrator permission, never a display driver) into <project>\_work\cuda-<version>. CYCLES_CUDA_ROOT,
# when set, is the only folder used (the fresh-clone checks use it for a computer without NVIDIA card);
# CYCLES_EASY_NO_INSTALLED_CUDA=1 (tests) ignores NVIDIA's installed toolkit.
function Get-EasyInstalledCudaRoot { Join-Path $env:ProgramFiles ('NVIDIA GPU Computing Toolkit\CUDA\v' + (Get-EasyLock).certification_candidate.cuda_toolkit) }
function Get-EasyCudaPartsVersion { [regex]::Match((Get-Content -LiteralPath (Join-Path $PSScriptRoot 'cuda-toolkit-parts.py') -Raw), '(?m)^VERSION = "([0-9.]+)"').Groups[1].Value }
function Get-EasyCudaPartsFolder {
    if ($env:CYCLES_CUDA_ROOT) { return [IO.Path]::GetFullPath($env:CYCLES_CUDA_ROOT) }
    return (Join-Path $script:EasyWork ('cuda-' + (Get-EasyCudaPartsVersion)))
}
function Get-EasyCudaParts([string]$Root = (Get-EasyCudaPartsFolder)) {
    $python = Find-EasyPython
    if (-not $python) { return $null }
    $arguments = @('-I', (Join-Path $PSScriptRoot 'cuda-toolkit-parts.py'), 'status', '--root', $Root)
    $probe = Invoke-EasyProgram $python $arguments
    if ($probe.ExitCode -ne 0) { return $null }
    return (($probe.Output -join "`n") | ConvertFrom-Json)
}
function Get-EasyCudaRoot {
    if ($env:CYCLES_CUDA_ROOT) {
        $root = [IO.Path]::GetFullPath($env:CYCLES_CUDA_ROOT)
        if (Test-Path -LiteralPath (Join-Path $root 'bin\nvcc.exe') -PathType Leaf) { return $root }
        return $null
    }
    $installed = Get-EasyInstalledCudaRoot
    if ($env:CYCLES_EASY_NO_INSTALLED_CUDA -ne '1' -and (Test-Path -LiteralPath (Join-Path $installed 'bin\nvcc.exe') -PathType Leaf)) { return $installed }
    $parts = Get-EasyCudaParts
    if ($parts -and $parts.complete) { return [string]$parts.root }
    return $null
}
# -CudaRoot for bootstrap.ps1 and build.ps1 (nothing when no toolkit was found: they then say so).
function Get-EasyCudaArguments($Plan) {
    if ($Plan.Cuda) { return @('-CudaRoot', $Plan.Cuda) }
    return @()
}

# --- NVIDIA display driver (read only) --------------------------------------------------------
# Minimum Windows driver versions for what this project uses (sources: docs/dev/community-builds.md,
# "GPU and display driver"): kernels from the CUDA 12.8 GA compiler >= 570.65 (CUDA 12.8.0 release
# notes, table 3; a 12.8 Update 1 compiler would need 572.61); OptiX 9.1.0 needs an R590 driver
# or newer; DLSS 310 needs a driver issued after 3 March 2022 (512.15; DLSS Programming Guide
# 310.6.0, section 2.1).
$script:EasyDriverMinimum = [ordered]@{ cuda = [version]'570.65'; optix = [version]'590.0'; dlss = [version]'512.15' }
$script:EasyCudaLicence = 'https://docs.nvidia.com/cuda/eula/index.html'
$script:EasyDriverPage = 'https://www.nvidia.com/en-us/drivers/'
# The one statement about the display driver (computer check and docs/user/build-it-yourself.md).
$script:EasyDriverStatement = 'GPU rendering and GPU denoising need an NVIDIA graphics card with its driver installed. Installing or updating the driver is up to you.'
# NVIDIA RTX SDKs licence of the pinned DLSS headers (packaging/notices/dlss-headers/LICENSE.txt).
$script:EasyDlssLicence = 'https://github.com/NVIDIA/DLSS/blob/v310.9.16/LICENSE.txt'
# NVIDIA's licence page for the OptiX SDK (it leads to the DesignWorks agreement of 13 June 2017,
# the text in packaging/notices/optix-headers/LICENSE.txt).
$script:EasyOptixLicence = 'https://developer.nvidia.com/designworks/sdk-samples-tools-software-license-agreement'
# Reads the installed NVIDIA driver version; changes nothing. $null when there is no NVIDIA GPU.
# CYCLES_EASY_NO_NVIDIA=1 acts as if there were none (the fresh-clone check uses it to prove the
# CPU-only build).
function Get-EasyNvidiaDriver {
    if ($env:CYCLES_EASY_NO_NVIDIA -eq '1') { return $null }
    $smi = Get-Command nvidia-smi.exe -ErrorAction SilentlyContinue
    if (-not $smi) {
        $system = Join-Path $env:SystemRoot 'System32\nvidia-smi.exe'
        if (Test-Path -LiteralPath $system) { $smi = Get-Command $system -ErrorAction SilentlyContinue }
    }
    if ($smi) {
        $query = Invoke-EasyProgram $smi.Source @('--query-gpu=name,driver_version', '--format=csv,noheader')
        $line = @($query.Output | Where-Object { $_ -match ',' } | Select-Object -First 1)
        if ($query.ExitCode -eq 0 -and $line.Count) {
            $name, $version = ([string]$line[0]) -split ',\s*', 2
            $parsed = $null
            if ([version]::TryParse($version.Trim(), [ref]$parsed)) { return [pscustomobject]@{ Gpu = $name.Trim(); Version = $parsed; Source = 'nvidia-smi' } }
        }
    }
    # Windows' own record: DriverVersion such as 32.0.15.9144 means NVIDIA driver 591.44
    # (the last five digits of the last two groups).
    try {
        $cards = @(Get-CimInstance -ClassName Win32_VideoController -ErrorAction Stop | Where-Object { $_.Name -like 'NVIDIA*' })
    } catch { $cards = @() }
    foreach ($card in $cards) {
        $groups = ([string]$card.DriverVersion) -split '\.'
        if ($groups.Count -ge 2) {
            $digits = ($groups[-2] + $groups[-1])
            if ($digits.Length -ge 5) {
                $digits = $digits.Substring($digits.Length - 5)
                return [pscustomobject]@{ Gpu = [string]$card.Name; Version = [version]($digits.Substring(0, 3) + '.' + $digits.Substring(3)); Source = 'Windows device record' }
            }
        }
        return [pscustomobject]@{ Gpu = [string]$card.Name; Version = $null; Source = 'Windows device record' }
    }
    return $null
}
# What the installed driver can run: cuda / optix / dlss = $true or $false; Driver = $null without NVIDIA GPU.
function Get-EasyDriverSupport {
    $driver = Get-EasyNvidiaDriver
    $support = [ordered]@{ Driver = $driver; cuda = $false; optix = $false; dlss = $false }
    if ($driver -and $driver.Version) {
        foreach ($key in @('cuda', 'optix', 'dlss')) { $support[$key] = ($driver.Version -ge $script:EasyDriverMinimum[$key]) }
    }
    return [pscustomobject]$support
}

function Get-EasyLock { Get-Content -LiteralPath (Join-Path $script:EasyRoot 'source.lock.json') -Raw | ConvertFrom-Json }
# The Houdini build this step works for (scripts/easy-houdini.ps1, Get-EasyHoudiniTarget): any
# installed Houdini 22.0 build with its development kit, not only the tested 22.0.466.
. (Join-Path $PSScriptRoot 'easy-houdini.ps1')
function Get-EasyHoudiniVersion { (Get-EasyHoudiniTarget).Version }
function Get-EasyHoudiniRoot { (Get-EasyHoudiniTarget).Root }
# Minutes for one first build (bootstrap, CUDA kernels, build, staging, quick tests), measured on
# the maintainer's computer (Ryzen 9 7950X; 11.4 to 12.2 minutes for 22.0.429, 22.0.466, 22.0.508): the estimate shown before a build when this computer
# has not measured one yet (scripts/easy-start.ps1 records each build's time).
$script:EasyReferenceMinutes = 12
# Minutes for each further Houdini build in the same run, its GPU kernels reused (scripts/kernel-cache.py), measured
# on the same computer (4.1 minutes of building each for 22.0.466 and 22.0.429 after 22.0.508; 4.8 and 4.9 minutes
# with quick tests and installer).
$script:EasyReferenceFurtherMinutes = 5
# Said once for each Houdini build that is not the one this project was tested with.
function Write-EasyUntestedNote([string]$Version) {
    $tested = Get-EasyTestedHoudini
    if ($Version -eq $tested) { return }
    Write-EasyInfo "Houdini $Version is not the build this project was tested with ($tested). It usually works."
    Write-EasyInfo 'If something is wrong, open an issue on the project''s GitHub page (CONTRIBUTING.md says what to include) and name your Houdini build.'
}

# Was this folder made by "git clone" (it can then update itself), or downloaded as a ZIP?
function Test-EasyGitCheckout([string]$Git) {
    if (-not $Git -or -not (Test-Path -LiteralPath (Join-Path $script:EasyRoot '.git'))) { return $false }
    $top = Invoke-EasyProgram $Git @('-C', $script:EasyRoot, 'rev-parse', '--show-toplevel')
    if ($top.ExitCode -ne 0 -or $top.Output.Count -ne 1) { return $false }
    $checkout = [IO.Path]::GetFullPath($top.Output[0]).TrimEnd('\', '/')
    return ($checkout -eq [IO.Path]::GetFullPath($script:EasyRoot).TrimEnd('\', '/'))
}
function Get-EasyGitHead([string]$Git) {
    if (-not (Test-EasyGitCheckout $Git)) { return $null }
    $head = Invoke-EasyProgram $Git @('-C', $script:EasyRoot, 'rev-parse', 'HEAD')
    if ($head.ExitCode -eq 0 -and $head.Output.Count -eq 1) { return ([string]$head.Output[0]).Trim() }
}
# What the sources are, for "built from older sources": the Git commit of a checkout, or the
# hash of source.lock.json in a downloaded ZIP.
function Get-EasySourceId {
    $git = Get-Command git.exe -ErrorAction SilentlyContinue
    $head = Get-EasyGitHead $git.Source
    if ($head) { return "git:$head" }
    return ('lock:' + (Get-FileHash -LiteralPath (Join-Path $script:EasyRoot 'source.lock.json') -Algorithm SHA256).Hash.ToLowerInvariant())
}

# The GPU download decision belongs to this copy of the source. A changed OptiX/DLSS pin
# or CUDA archive list requires a fresh decision. An unattended run must name this decision
# separately from ordinary prerequisite approval.
function Resolve-EasyNvidiaConsent([bool]$HasNvidia, [switch]$Ask, [switch]$Review) {
    if (-not $HasNvidia) { return $false }
    $preset = [string]$env:CYCLES_EASY_NVIDIA_CONSENT
    $file = Join-Path $script:EasyWork 'easy-nvidia.json'
    $lock = Get-EasyLock
    $scope = @(
        $lock.optix_headers.url
        $lock.optix_headers.commit
        $lock.dlss_headers.url
        $lock.dlss_headers.commit
        (Get-FileHash -LiteralPath (Join-Path $PSScriptRoot 'cuda-toolkit-parts.py') -Algorithm SHA256).Hash
    ) -join '|'
    if ($preset -in @('yes', 'no')) {
        if ($Ask) { Write-EasyInfo "NVIDIA downloads: $preset (CYCLES_EASY_NVIDIA_CONSENT=$preset)." }
        return ($preset -eq 'yes')
    }
    if (-not $Review -and (Test-Path -LiteralPath $file -PathType Leaf)) {
        try {
            $seen = Get-Content -LiteralPath $file -Raw | ConvertFrom-Json
            if ($seen.scope -eq $scope -and $seen.decision -is [bool]) { return $seen.decision }
        } catch { }
    }
    if (-not $Ask -or (Test-EasyUnattended)) { return $false }

    Write-EasyStep 'Optional NVIDIA downloads for GPU rendering'
    Write-EasyInfo "CUDA compiler, headers and runtime library: $script:EasyCudaLicence"
    Write-EasyInfo "OptiX headers for rendering and denoising: $script:EasyOptixLicence"
    Write-EasyInfo "DLSS headers for the viewer (only when DLSS is on): $script:EasyDlssLicence"
    Write-EasyInfo 'Existing parts are reused. A no builds for the CPU and skips every NVIDIA download.'
    $answer = Read-EasyYesNo '   Allow these NVIDIA downloads under their listed licences?'
    New-Item -ItemType Directory -Path $script:EasyWork -Force | Out-Null
    Set-Content -LiteralPath $file -Value (@{ scope = $scope; decision = $answer } | ConvertTo-Json) -Encoding utf8
    return $answer
}

function Get-EasyNvidiaPlan($Identity, [switch]$Ask, [switch]$Review, $Driver = (Get-EasyNvidiaDriver)) {
    $allowed = Resolve-EasyNvidiaConsent -HasNvidia ([bool]$Driver) -Ask:$Ask -Review:$Review
    $cuda = if ($allowed) { Get-EasyCudaRoot }
    $dlssWanted = -not ($Identity -and $Identity.dlss -eq 'off')
    return [pscustomobject]@{
        Cuda = $cuda; Nvidia = [bool]$Driver; Allowed = $allowed; DlssWanted = $dlssWanted
        Optix = [bool]$cuda; Dlss = ($dlssWanted -and [bool]$cuda)
    }
}

# The build identity (build-identity.json, see docs/dev/community-builds.md), checked by
# the same code that the installer uses. Returns $null and prints why when it is invalid.
function Get-EasyBuildIdentity {
    $file = Join-Path $script:EasyRoot 'build-identity.json'
    if (-not (Test-Path -LiteralPath $file)) { Write-EasyMissing "build-identity.json is missing in $script:EasyRoot"; return $null }
    # First use: an id of your own (and, with DLSS on, a DLSS project id of your own).
    $python = Find-EasyPython
    if (-not $python) { return $null }
    $prepared = & $python -I (Join-Path $PSScriptRoot 'release-package.py') prepare-identity --build-identity $file 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-EasyMissing 'build-identity.json could not be read:'
        Write-EasyInfo (($prepared | Out-String).Trim())
        return $null
    }
    $filled = @((($prepared | Out-String) | ConvertFrom-Json).filled)
    if ($filled.Count) { Write-EasyInfo ('build-identity.json: filled in ' + ($filled -join ', ') + ' for you (first use). Keep these values.') }
    $checked = & $python -I (Join-Path $PSScriptRoot 'release-package.py') identity --houdini (Get-EasyHoudiniVersion) --build-identity $file 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-EasyMissing 'build-identity.json is not valid:'
        Write-EasyInfo (($checked | Out-String).Trim())
        return $null
    }
    $identity = Get-Content -LiteralPath $file -Raw | ConvertFrom-Json
    Add-Member -InputObject $identity -NotePropertyName target -NotePropertyValue (($checked | Out-String) | ConvertFrom-Json) -Force
    return $identity
}

# git status of this folder without build-identity.json: editing that file, as the guide asks,
# is not a "local change" of the sources (its content is recorded in the build record instead).
function Get-EasySourceChanges([string]$Git, [string[]]$Options = @('--untracked-files=normal')) {
    @(& $Git -C $script:EasyRoot status --porcelain @Options -- . ':(exclude)build-identity.json')
}

# Build switches that follow build-identity.json. DLSS "on" (the default) compiles your own
# DLSS project id (dlss_project_id); "official-project-id-with-permission" the engine's own;
# "off" builds without DLSS. The id is always passed ('none' clears it), so an earlier one never
# stays behind in the build folder.
# -NoDlss: build without DLSS whatever build-identity.json says (no NVIDIA graphics card).
function Get-EasyBuildArguments($Identity, [switch]$NoDlss) {
    if ($NoDlss) { return @('-Dlss:$false', '-DlssProjectId', 'none') }
    if ($Identity -and $Identity.dlss -eq 'on') { return @('-Dlss', '-DlssProjectId', [string]$Identity.dlss_project_id) }
    if ($Identity -and $Identity.dlss -eq 'official-project-id-with-permission') { return @('-Dlss', '-DlssProjectId', 'none') }
    return @('-Dlss:$false', '-DlssProjectId', 'none')
}

# GPU kernel reuse (scripts/build.ps1 -KernelCache, scripts/kernel-cache.py): the GPU kernels are compiled once and
# reused by every further build whose kernels are made from exactly the same inputs. The stored copies are in
# <project>\_work\kernel-cache, next to the build folders (delete it to clear them). CYCLES_EASY_NO_KERNEL_REUSE=1
# (scripts/easy-start.ps1 -NoKernelReuse) turns reuse off: every build compiles its kernels itself.
function Test-EasyKernelReuse { return ($env:CYCLES_EASY_NO_KERNEL_REUSE -ne '1') }
function Get-EasyKernelCacheFolder { Join-Path (Split-Path -Parent (Get-EasyBuildDirectory)) 'kernel-cache' }
function Get-EasyKernelCacheArguments {
    if (-not (Test-EasyKernelReuse)) { return @('-KernelCache', 'off') }
    return @('-KernelCache', (Get-EasyKernelCacheFolder))
}

# The build folder of one Houdini build: <project>\_work\windows-h<build>-release (the folder name of the default
# build layout, scripts/build-layout.ps1, inside the working folder).
function Get-EasyBuildDirectoryFor([string]$SourceRoot = $script:EasyRoot, [string]$Version) {
    . (Join-Path $PSScriptRoot 'build-layout.ps1')
    return (Join-Path (Join-Path $SourceRoot '_work') (Split-Path -Leaf (Get-DefaultBuildDirectory -SourceRoot $SourceRoot -HoudiniVersion $Version)))
}
function Get-EasyBuildDirectory { Get-EasyBuildDirectoryFor -Version (Get-EasyHoudiniVersion) }

# Runs a script of this repository in PowerShell 7, as a developer would, and returns only
# whether it succeeded. Its output goes to the window (Out-Host): left in the pipeline it would
# become part of the return value, and a failed step would then look like a success.
function Invoke-EasyScript([string]$Script, [string[]]$Arguments) {
    $pwsh = (Get-Process -Id $PID).Path
    & $pwsh -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot $Script) @Arguments | Out-Host
    return ($LASTEXITCODE -eq 0)
}
