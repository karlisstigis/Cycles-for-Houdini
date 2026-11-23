# SPDX-License-Identifier: Apache-2.0
# The one double-click file: "START HERE - Build Cycles for Houdini.cmd" (repository root).
# Its result is an installer (Setup.exe) for each Houdini build it builds for; the build itself
# installs nothing and changes nothing in Houdini, and this tool never runs a Setup.
#
# First run (no tested build yet), all in this one window:
#   check the computer, ask separately about NVIDIA downloads and missing tools, and install
#   the approved tools (scripts/easy-check-computer.ps1);
#   list the installed Houdini 22.0 builds (scripts/easy-houdini.ps1) and, only when there are several,
#   ask which to build for (Enter = those without Cycles yet); then,
#   for each: get the sources and build (scripts/easy-get-and-build.ps1), run the quick tests
#   (scripts/easy-quick-tests.ps1) and make its installer (scripts/easy-make-installer.ps1), one build
#   folder per Houdini build (<project>\_work\) and one folder for all installers (<project>\Installers\). At the end: where
#   the installers are and what to do with them; a run with a person at the keyboard opens that folder.
#   Without Inno Setup 6 everything is built and tested and the end says that no installer was made.
#   What cannot be installed automatically (Houdini) stops the run with the exact link; the next
#   run starts again from the top, and every step skips what is already done.
# Later runs (a tested build exists): a Houdini build that is new on this computer and has no Cycles
#   is built for at once, with its installer. Removed builds are reported. Unfinished builds are retried.
#   After a successful run it shows a short menu (Enter = Close).
# Starts in Windows PowerShell 5.1 when PowerShell 7 is missing: then only the computer check
# runs there, and the rest continues in PowerShell 7 in the same window.
# Refuses to run twice at the same time for the same folder.
#
# Unattended (scripts, the fresh-clone checks): -Action picks what the menu would, and
# -Yes (or CYCLES_EASY_ANSWER=yes|no) answers ordinary prerequisite questions. Pass
# CYCLES_EASY_NVIDIA_CONSENT=yes|no separately for NVIDIA downloads. Unattended runs never open
# Houdini or a folder window.
#   -Action FirstRun | Update | AddHoudini | OpenHoudini | QuickTests | Installer | DlssOn | DlssOff |
#           EnginePatches | CheckComputer
#           (Installer = "Make the installers again"; AddHoudini builds for a Houdini build chosen with
#           -Houdini, or asked, also one that has Cycles installed by a Setup)
#   -Houdini missing | all | newest | 22.0.508   which Houdini builds (default missing: those without
#                                    Cycles yet; the same setting: CYCLES_EASY_HOUDINI). Update,
#                                    QuickTests and Installer take every built one, or the one named;
#                                    OpenHoudini one (default: the newest built).
#   -NoUpdate, -Jobs N               (FirstRun, Update, Installer)
#   -SkipOptix                       (FirstRun, Update, QuickTests)
#   -OutputDirectory                 the folder for the installers (default <project>\Installers)
#   -StopBeforeInstaller             make and verify the packages, but no Setup.exe
#   -NoKernelReuse                   compile the GPU kernels in every build (the same setting:
#                                    CYCLES_EASY_NO_KERNEL_REUSE=1); by default they are compiled once and
#                                    reused while nothing they are made from changed (scripts/kernel-cache.py)
#   -Message                         (EnginePatches)
# Exit codes: 0 done; 1 not done; 3 something still missing; 4 Windows must restart; 5 already running; 6 source update failed.
[CmdletBinding()]
param(
    [ValidateSet('Auto', 'FirstRun', 'Update', 'AddHoudini', 'OpenHoudini', 'QuickTests', 'Installer', 'DlssOn', 'DlssOff', 'EnginePatches', 'CheckComputer')]
    [string]$Action = 'Auto',
    [switch]$Yes,
    [switch]$NoUpdate,
    [int]$Jobs = 6,
    [switch]$SkipOptix,
    [string]$OutputDirectory = '',
    [switch]$StopBeforeInstaller,
    [string]$Message = '',
    [string]$Houdini = '',
    [switch]$NoKernelReuse,
    # Internal: the computer check already ran (in Windows PowerShell 5.1, before this continuation).
    [switch]$SkipComputerCheck
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'easy-common.ps1')
trap { Write-EasyCrash $_; exit 1 }
if ($Yes) { $env:CYCLES_EASY_ANSWER = 'yes' }
if ($Houdini) { $env:CYCLES_EASY_HOUDINI = $Houdini }
if ($NoKernelReuse) { $env:CYCLES_EASY_NO_KERNEL_REUSE = '1' }
$root = $script:EasyRoot
$isPwsh7 = ($PSVersionTable.PSVersion.Major -ge 7)

# --- One run at a time (per folder) -------------------------------------------------------------
# A named mutex disappears with the process that holds it, so a closed or crashed window never
# blocks the next run. The PowerShell 7 continuation of a Windows PowerShell 5.1 start inherits
# the lock through CYCLES_EASY_START_LOCK.
$lockName = Get-EasyStartLockName
$mutex = $null
if ($env:CYCLES_EASY_START_LOCK -ne $lockName) {
    $mutex = New-Object System.Threading.Mutex($false, $lockName)
    $owned = $false
    try { $owned = $mutex.WaitOne(0) } catch [System.Threading.AbandonedMutexException] { $owned = $true }
    if (-not $owned) {
        Write-EasyResult $false "`"$script:EasyStartFile`" is already running for this folder in another window." 'Use that window. When it has finished, you can double-click this file again.'
        exit 5
    }
    $env:CYCLES_EASY_START_LOCK = $lockName
}

function Invoke-Part([string]$Script, [string[]]$Arguments = @()) {
    # Each part runs as its own PowerShell process in this window, like a developer would run it.
    # Start-Process keeps the window's own input and output, so the part's questions and colours
    # appear as they are (output captured through a pipe would hide a question until Enter).
    $env:CYCLES_EASY_NESTED = '1'
    try {
        $shell = (Get-Process -Id $PID).Path
        $all = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', (Join-Path $PSScriptRoot $Script)) + $Arguments
        $process = Start-Process -FilePath $shell -ArgumentList (ConvertTo-EasyCommandLine $all) -NoNewWindow -PassThru
        $null = $process.Handle  # keeps the exit code readable after the process ends
        $process.WaitForExit()
        return [int]$process.ExitCode
    } finally { $env:CYCLES_EASY_NESTED = $null }
}
function Stop-Run([int]$Code, [string]$What, [string]$Next) { Write-EasyResult $false $What $Next; exit $Code }

$testArguments = @()
if ($SkipOptix) { $testArguments += '-SkipOptix' }

function Invoke-CheckComputer([switch]$ReviewNvidiaConsent) {
    $arguments = @()
    if ($ReviewNvidiaConsent) { $arguments += '-ReviewNvidiaConsent' }
    $code = Invoke-Part 'easy-check-computer.ps1' $arguments
    if ($code -eq 4) { Stop-Run 4 'Windows must restart to finish an install (see above).' ('Restart the computer, then ' + (Get-EasyAgain) + '.') }
    if ($code -ne 0) { Stop-Run 3 'This computer still needs what is listed above, so nothing was built yet.' ('Install what is listed, then ' + (Get-EasyAgain) + '.') }
}

# --- Windows PowerShell 5.1: check the computer (it can install PowerShell 7), then continue in 7 ---
if (-not $isPwsh7) {
    Write-EasyTitle 'Cycles for Houdini: build it yourself'
    Write-Host 'This window runs in Windows PowerShell. First the computer check (it installs PowerShell 7'
    Write-Host 'if you agree); then everything continues here in PowerShell 7.'
    if ($Action -in @('Auto', 'FirstRun', 'CheckComputer')) { Invoke-CheckComputer -ReviewNvidiaConsent:($Action -eq 'CheckComputer') }
    $pwsh = Find-EasyPwsh
    if (-not $pwsh) { Stop-Run 3 'PowerShell 7 is needed for the build and is not installed.' ('Install it from https://aka.ms/powershell (or answer y when asked), then ' + (Get-EasyAgain) + '.') }
    if ($Action -eq 'CheckComputer') { Write-EasyResult $true 'Everything needed to build is on this computer.' ((Get-EasyAgain) + '.'); exit 0 }
    $forward = @('-Action', $(if ($Action -eq 'Auto') { 'FirstRun' } else { $Action }), '-Jobs', [string]$Jobs, '-SkipComputerCheck')
    foreach ($name in @('NoUpdate', 'SkipOptix', 'StopBeforeInstaller', 'NoKernelReuse')) { if ((Get-Variable $name -ValueOnly)) { $forward += "-$name" } }
    if ($OutputDirectory) { $forward += @('-OutputDirectory', $OutputDirectory) }
    if ($Message) { $forward += @('-Message', $Message) }
    Write-EasyInfo "Continuing in PowerShell 7 ($pwsh)."
    & $pwsh -NoProfile -ExecutionPolicy Bypass -File $PSCommandPath @forward
    exit $LASTEXITCODE
}

# --- PowerShell 7 ---------------------------------------------------------------------------------
. (Join-Path $PSScriptRoot 'build-layout.ps1')
$sourceId = Get-EasySourceId
$installers = if ($OutputDirectory) { [IO.Path]::GetFullPath($OutputDirectory) } else { Get-EasyInstallerFolder }
# Remembers Houdini builds seen, builds to retry, elapsed times and earlier layout notices.
$statePath = Join-Path $script:EasyWork 'easy-houdini.json'
function Read-State {
    $state = [ordered]@{ schema_version = 1; seen = @(); retry = @(); minutes = [ordered]@{}; earlier_layout_offered = @(); gpu_minutes = [ordered]@{} }
    if (Test-Path -LiteralPath $statePath) {
        try {
            $data = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
            $state.seen = @($data.seen | Where-Object { $_ })
            foreach ($p in @($data.minutes.PSObject.Properties)) { $state.minutes[$p.Name] = [double]$p.Value }
            $state.retry = @($data.retry | Where-Object { $_ })
            $state.earlier_layout_offered = @($data.earlier_layout_offered | Where-Object { $_ })
            foreach ($p in @($data.gpu_minutes.PSObject.Properties)) { $state.gpu_minutes[$p.Name] = [double]$p.Value }
        } catch { }
    }
    return $state
}
function Save-State($State) {
    $folder = Split-Path -Parent $statePath
    if (-not (Test-Path -LiteralPath $folder)) { New-Item -ItemType Directory -Path $folder -Force | Out-Null }
    $temporary = "$statePath.partial"
    [IO.File]::WriteAllText($temporary, ($State | ConvertTo-Json -Depth 4))
    [IO.File]::Move($temporary, $statePath, $true)
}
function Get-Rows { @(Get-EasyHoudiniStates -SourceRoot $root -SourceId $sourceId) }
function Show-Rows($Rows) {
    Write-Host ''
    Write-Host 'Houdini on this computer:'
    foreach ($row in $Rows) { Write-Host ('  ' + (Format-EasyHoudiniState $row)) }
    if (-not @($Rows).Count) { Write-Host "  no Houdini $(Get-EasySupportedHoudini) found" }
}
# Honest estimate: the slowest build measured on this computer, else the reference measurement. With GPU kernel
# reuse on and several builds: the first build compiles the GPU kernels, each further one reuses them
# (gpu_minutes: the last measured build time with "compiled" and with "reused" kernels).
function Write-Estimate($Targets, $State) {
    $count = @($Targets).Count
    $measured = @($State.minutes.Values)
    $what = if ($count -eq 1) { '1 Houdini build' } else { "$count Houdini builds" }
    if ($count -gt 1 -and (Test-EasyKernelReuse) -and (Get-EasyNvidiaPlan $null).Cuda) {
        $here = $State.gpu_minutes.Contains('compiled') -and $State.gpu_minutes.Contains('reused')
        $first = if ($here) { [math]::Ceiling($State.gpu_minutes['compiled']) } else { $script:EasyReferenceMinutes }
        $further = if ($here) { [math]::Ceiling($State.gpu_minutes['reused']) } else { $script:EasyReferenceFurtherMinutes }
        $where = if ($here) { 'on this computer (measured here)' } else { 'on a fast computer (measured on a Ryzen 9 7950X); a slower computer takes longer' }
        Write-EasyInfo "Building for $what. About $first minutes for the first and about $further minutes for each further one $where, about $($first + ($count - 1) * $further) minutes in all: the GPU programs are made once and reused."
        return
    }
    if ($measured.Count) {
        $per = [math]::Ceiling(($measured | Measure-Object -Maximum).Maximum)
        Write-EasyInfo "Building for $what. About $per minutes per Houdini build on this computer (measured here)."
    } else {
        Write-EasyInfo "Building for $what. About $script:EasyReferenceMinutes minutes per Houdini build on a fast computer (measured on a Ryzen 9 7950X); a slower computer takes longer."
    }
}

# Which Houdini builds a build is wanted for. -Houdini (or CYCLES_EASY_HOUDINI) decides; with
# several builds installed and nobody deciding, one question (Enter = those without Cycles yet).
function Resolve-Targets($Rows, [switch]$Ask) {
    $kits = @($Rows | Where-Object { $_.Kit })
    $choice = [string]$env:CYCLES_EASY_HOUDINI
    if (-not $choice -and $kits.Count -eq 1) { $choice = 'all' }
    if (-not $choice -and $Ask -and $kits.Count -gt 1 -and -not (Test-EasyUnattended)) {
        $missing = @((Select-EasyHoudiniTargets $Rows 'missing').Targets | ForEach-Object { $_.Version })
        $default = if ($missing.Count) { 'those without Cycles yet (' + ($missing -join ', ') + ')' } else { 'none (all have Cycles already)' }
        Write-Host ''
        for ($try = 0; $try -lt 3 -and -not $choice; $try++) {
            try { $answer = Read-Host "Build for which Houdini? Enter = $default;  a = all;  or type one, such as $($kits[0].Version)" } catch { $answer = '' }
            $answer = ([string]$answer).Trim()
            if (-not $answer) { $choice = 'missing' }
            elseif ($answer -match '^(a|all)$') { $choice = 'all' }
            elseif (@($kits | Where-Object { $_.Version -eq $answer }).Count) { $choice = $answer }
            else { Write-Host 'Please press Enter, type a, or type one of the Houdini builds listed above.' }
        }
    }
    $selection = Select-EasyHoudiniTargets $Rows $choice
    if ($selection.Problem) { Write-EasyMissing $selection.Problem }
    foreach ($row in @($selection.Targets | Where-Object { $_.Other.Count })) {
        Write-EasyInfo "Houdini $($row.Version) also has Cycles for Houdini installed ($(@($row.Other | ForEach-Object { $_.Name }) -join ', ')). Your build stays in its own folder and changes nothing there; its installer would ask you to uninstall that one first."
    }
    return @($selection.Targets)
}

# The installer of one built and tested Houdini build (scripts/easy-make-installer.ps1). Returns
# Setup (the file), Missing ($true without Inno Setup 6) and Ok.
function Invoke-Installer($Row, [switch]$JustTested) {
    Write-EasyStep "Houdini $($Row.Version): make the installer"
    $report = Join-Path ([IO.Path]::GetTempPath()) ('cycles-easy-installer-' + [Guid]::NewGuid().ToString('N') + '.json')
    $arguments = @('-Jobs', [string]$Jobs, '-ResultFile', $report)
    if ($OutputDirectory) { $arguments += @('-InstallerFolder', $installers) }
    if ($JustTested) { $arguments += '-Tested' }
    if ($StopBeforeInstaller) { $arguments += '-StopBeforeInstaller' }
    Set-EasyHoudiniTarget $Row
    try { $code = Invoke-Part 'easy-make-installer.ps1' $arguments } finally { Set-EasyHoudiniTarget $null }
    $made = $null
    if ($code -eq 0 -and (Test-Path -LiteralPath $report)) { $made = Get-Content -LiteralPath $report -Raw | ConvertFrom-Json; Remove-Item -LiteralPath $report -Force }
    return [pscustomobject]@{ Ok = ($code -eq 0); Missing = ($code -eq 3); Setup = $(if ($made) { [string]$made.setup } else { $null }); Package = $(if ($made) { [string]$made.package } else { $null }) }
}

# Builds, tests and makes the installer for each target in turn. One that fails does not stop the others.
function Invoke-Builds($Targets, [switch]$Update) {
    $fresh = @($Targets | Where-Object { -not (Test-Path -LiteralPath (Join-Path $_.Build 'build-manifest.json')) }).Count
    $disk = Get-EasyDiskProblem $fresh (@($Targets).Count - $fresh)
    if ($disk) { Stop-Run 3 $disk ('Free up space on that drive (or build for fewer Houdini builds), then ' + (Get-EasyAgain) + '.') }
    $state = Read-State
    $versions = @($Targets | ForEach-Object { $_.Version })
    $state.retry = @($state.retry + $versions | Select-Object -Unique)
    Save-State $state
    Write-Estimate $Targets $state
    $results = New-Object System.Collections.Generic.List[object]
    $first = $true
    $inno = [bool](Find-EasyInno) -or $StopBeforeInstaller
    foreach ($row in $Targets) {
        Write-EasyStep "Houdini $($row.Version): get the sources and build"
        Set-EasyHoudiniTarget $row
        $watch = [Diagnostics.Stopwatch]::StartNew()
        $setup = $null; $built = $false
        try {
            $arguments = @('-Jobs', [string]$Jobs)
            if ($NoUpdate -or -not ($Update -and $first)) { $arguments += '-NoUpdate' }
            $ok = $false; $what = ''
            $using = @(Get-EasyProcessesUsingBuild $row $row.Build)
            if ($using.Count) { $what = (Get-EasyCloseHoudiniSentence $row.Version) + ' Nothing was rebuilt for it.'; Write-EasyMissing $what }
            else {
                $code = Invoke-Part 'easy-get-and-build.ps1' $arguments
                $first = $false
                if ($code -eq 6) { Stop-Run 6 'Updating the sources failed. No builds were changed.' ((Get-EasyAgain 'Update the sources and rebuild') + ', or build the current sources with -NoUpdate.') }
                if ($code -ne 0) { $what = 'the build did not finish (the reason is above, under its name)' }
                else {
                    Write-EasyStep "Houdini $($row.Version): quick tests"
                    if ((Invoke-Part 'easy-quick-tests.ps1' $testArguments) -ne 0) { $what = 'built, but a quick test failed (details above), so no installer was made' }
                    else { $built = $true }
                }
            }
        } finally { Set-EasyHoudiniTarget $null }
        if ($built) {
            if (-not $inno) { $ok = $true; $what = 'built, quick tests passed; no installer (Inno Setup 6 is missing)' }
            else {
                $made = Invoke-Installer $row -JustTested
                if ($made.Ok) { $ok = $true; $setup = $made.Setup; $what = $(if ($StopBeforeInstaller) { 'built, quick tests passed, package made (no Setup, as asked)' } else { 'built, quick tests passed, installer made' }) }
                elseif ($made.Missing) { $ok = $true; $inno = $false; $what = 'built, quick tests passed; no installer (Inno Setup 6 is missing)' }
                else { $what = 'built, quick tests passed, but the installer could not be made (the reason is above)' }
            }
        }
        $minutes = [math]::Round($watch.Elapsed.TotalMinutes, 1)
        $state.retry = @($state.retry | Where-Object { $_ -ne $row.Version })
        if ($ok) { $state.minutes[$row.Version] = $minutes }
        else { $state.retry += $row.Version }
        if ($ok -and $built) {
            try {
                $gpu = [string](Get-Content -LiteralPath (Join-Path $row.Build 'easy-build.json') -Raw | ConvertFrom-Json).gpu_programs
                if ($gpu -in @('compiled', 'reused')) { $state.gpu_minutes[$gpu] = $minutes }
            } catch { }
        }
        Save-State $state
        $results.Add([pscustomobject]@{ Version = $row.Version; Ok = $ok; What = $what; Minutes = $minutes; Setup = $setup; Built = $built })
    }
    Write-Host ''
    Write-Host 'Result for each Houdini build:' -ForegroundColor Cyan
    foreach ($r in $results) {
        $line = "  Houdini $($r.Version): $($r.What) ($($r.Minutes) min)"
        if ($r.Ok) { Write-Host $line -ForegroundColor Green } else { Write-Host $line -ForegroundColor Red }
    }
    return $results.ToArray()
}

# The end of every run that made installers: where they are, which Houdini build each is for, what to
# do with them, and what they do to an earlier installation (CyclesHoudini.iss and release-package.py).
function Write-Installers($Results) {
    $made = @($Results | Where-Object { $_.Setup })
    if (-not $made.Count) { return }
    Write-Host ''
    Write-Host "Your installers (in $installers):" -ForegroundColor Cyan
    foreach ($r in $made) { Write-Host "  $(Split-Path -Leaf $r.Setup)   for Houdini $($r.Version)" }
    Write-Host 'Double-click one to install Cycles into that Houdini build. Uninstall in Windows Settings > Apps >'
    Write-Host 'Installed apps. The uninstaller keeps older renderer versions and your saved data. See the build guide.'
    Write-Host 'The project folder holds the builder output separately from your Cycles installation.'
    Write-Host 'A newer installer of the same build for the same Houdini build updates the one installed before.'
    Write-Host 'If another Cycles package can load in any of the same Houdini builds using these preferences,'
    Write-Host 'Setup stops and asks you to uninstall that one first. Even different target builds can overlap.'
    Write-Host 'The installers are unsigned: Windows may show "Windows protected your PC" (More info > Run anyway).'
    if (-not (Test-EasyUnattended) -and -not $StopBeforeInstaller) { try { Start-Process -FilePath 'explorer.exe' -ArgumentList ('"' + $installers + '"') } catch { } }
}
# DONE / NOT DONE for a run that built: every build failed, some failed, no installer, or all made.
function Stop-AfterBuilds($Results, [string]$Item) {
    $passed = @($Results | Where-Object { $_.Ok })
    $failed = @($Results | Where-Object { -not $_.Ok })
    Write-Installers $Results
    if (-not $passed.Count) { Stop-Run 1 'No build finished (the reasons are above).' ('Fix what the messages say, then ' + (Get-EasyAgain $Item) + '. If it fails again, open an issue on the project page on GitHub with the lines above.') }
    if ($failed.Count) { Stop-Run 1 ('Done for Houdini ' + (($passed | ForEach-Object { $_.Version }) -join ', ') + '; not for ' + (($failed | ForEach-Object { $_.Version }) -join ', ') + ' (see above).') ('Fix what the messages say, then ' + (Get-EasyAgain $Item) + '.') }
    if (-not @($Results | Where-Object { $_.Setup }).Count -and -not $StopBeforeInstaller) {
        Write-EasyResult $true 'Cycles for Houdini is built and its quick tests passed, but no installer was made: Inno Setup 6, which makes the installers, is missing.' ((Get-EasyAgain 'Check this computer again') + ', answer y to install Inno Setup 6, then choose "Make the installers again".')
        exit 0
    }
    if ($StopBeforeInstaller) {
        Write-EasyResult $true 'Cycles for Houdini is built, its quick tests passed and the packages were verified.' ''
        exit 0
    }
    Write-EasyResult $true 'Cycles for Houdini is built, its quick tests passed and the installers are made (listed above).' 'Double-click an installer to install Cycles into its Houdini build.'
    exit 0
}
function Remember-Seen($Rows) {
    $state = Read-State
    $state.seen = @($Rows | ForEach-Object { $_.Version })
    Save-State $state
}

# Which built Houdini to use (open): -Houdini when it names one, else the newest;
# with several and a person at the keyboard, one question with Enter = the newest.
function Select-BuiltTarget([string]$Purpose) {
    $built = @(Get-Rows | Where-Object { $_.Kit -and $_.State -in @('built', 'older') })
    if (-not $built.Count) { return $null }
    $wanted = [string]$env:CYCLES_EASY_HOUDINI
    if ($wanted -match '^\d+\.\d+\.\d+$') { return @($built | Where-Object { $_.Version -eq $wanted } | Select-Object -First 1)[0] }
    if ($built.Count -eq 1 -or (Test-EasyUnattended)) { return $built[0] }
    $list = ($built | ForEach-Object { $_.Version }) -join ', '
    for ($try = 0; $try -lt 3; $try++) {
        try { $answer = ([string](Read-Host "$Purpose for which Houdini ($list)? Enter = $($built[0].Version)")).Trim() } catch { $answer = '' }
        if (-not $answer) { return $built[0] }
        $one = @($built | Where-Object { $_.Version -eq $answer })
        if ($one.Count) { return $one[0] }
        Write-Host 'Please type one of the Houdini builds listed, or press Enter.'
    }
    return $built[0]
}
# The built Houdini builds an action works on: every one, or the one -Houdini names.
function Get-BuiltTargets {
    $targets = @(Get-Rows | Where-Object { $_.Kit -and $_.BuildState })
    if ($env:CYCLES_EASY_HOUDINI -match '^\d+\.\d+\.\d+$') { $targets = @($targets | Where-Object { $_.Version -eq $env:CYCLES_EASY_HOUDINI }) }
    return $targets
}

# --- DLSS on / off (build-identity.json "dlss") ------------------------------------------------------
function Set-DlssSetting([string]$Value) {
    $file = Join-Path $root 'build-identity.json'
    $text = Get-Content -LiteralPath $file -Raw
    $new = [regex]::Replace($text, '"dlss"\s*:\s*"[^"]*"', '"dlss": "' + $Value + '"')
    # An "off" build has no DLSS project id; "on" makes a new one on the next build.
    if ($Value -eq 'off') { $new = [regex]::Replace($new, '"dlss_project_id"\s*:\s*"[^"]*"', '"dlss_project_id": ""') }
    if ($new -ne $text) { [IO.File]::WriteAllText($file, $new, (New-Object System.Text.UTF8Encoding($false))) }
}
function Get-DlssSetting { try { return [string](Get-Content -LiteralPath (Join-Path $root 'build-identity.json') -Raw | ConvertFrom-Json).dlss } catch { return 'on' } }

function Invoke-FirstRun([string]$Why = '') {
    Write-EasyTitle 'Cycles for Houdini: build it yourself (first run)'
    if ($Why) { Write-Host $Why -ForegroundColor Yellow }
    Write-Host 'This does everything in order: check this computer (and install what is missing, after you'
    Write-Host 'have seen the list), get the sources, build for your Houdini, run the quick tests and make an'
    Write-Host 'installer for each Houdini build. It installs nothing into Houdini: you do that afterwards by'
    Write-Host 'double-clicking an installer. If it stops, the last lines say why; double-click this file again'
    Write-Host 'afterwards and it continues: finished parts are not done again.'
    if (-not $SkipComputerCheck) { Write-EasyStep 'Part 1 of 3: check this computer'; Invoke-CheckComputer }
    Write-EasyStep 'Part 2 of 3: choose the Houdini to build for'
    $rows = Get-Rows
    Show-Rows $rows
    $targets = Resolve-Targets $rows -Ask
    Remember-Seen $rows
    if (-not $targets.Count) {
        if (@($rows | Where-Object { $_.Kit -and $_.State -in @('built', 'older') }).Count) { Write-EasyResult $true 'Every chosen Houdini build has Cycles already.' ((Get-EasyAgain) + ' for the menu.'); exit 0 }
        Stop-Run 1 'There is no Houdini build to build for (see above).' ('Install Houdini with its development kit, or choose another one, then ' + (Get-EasyAgain) + '.')
    }
    Write-EasyStep 'Part 3 of 3: build, test and make the installers'
    Stop-AfterBuilds (Invoke-Builds $targets) 'Update the sources and rebuild'
}
function Invoke-Action([string]$Name) {
    switch ($Name) {
        'FirstRun' { Invoke-FirstRun }
        'Update' {
            $retry = (Read-State).retry
            $targets = @(Get-Rows | Where-Object { $_.Kit -and ($_.BuildState -or $retry -contains $_.Version) })
            if ($env:CYCLES_EASY_HOUDINI -match '^\d+\.\d+\.\d+$') { $targets = @($targets | Where-Object { $_.Version -eq $env:CYCLES_EASY_HOUDINI }) }
            if (-not $targets.Count) { Stop-Run 1 'There is no build of yours to update yet.' ((Get-EasyAgain) + ' (its first run builds).') }
            Stop-AfterBuilds (Invoke-Builds $targets -Update) 'Update the sources and rebuild'
        }
        'AddHoudini' {
            $rows = Get-Rows
            Show-Rows $rows
            $targets = Resolve-Targets $rows -Ask
            Remember-Seen $rows
            if (-not $targets.Count) { Write-EasyResult $true 'Nothing to build: every chosen Houdini build has Cycles already.' ''; exit 0 }
            Stop-AfterBuilds (Invoke-Builds $targets) 'Update the sources and rebuild'
        }
        'OpenHoudini' {
            $row = Select-BuiltTarget 'Open Houdini'
            if (-not $row) { Stop-Run 1 'There is no tested build of yours yet.' ((Get-EasyAgain) + ' (its first run builds).') }
            Set-EasyHoudiniTarget $row
            if ((Invoke-Part 'easy-open-houdini.ps1') -ne 0) { Stop-Run 1 'Houdini was not started (the reason is above).' ((Get-EasyAgain 'Update the sources and rebuild') + '.') }
            Write-EasyResult $true "Houdini $($row.Version) is starting with your build, without installing it (it can take a minute to appear)." 'You can close this window.'
            exit 0
        }
        'QuickTests' {
            $targets = @(Get-BuiltTargets)
            if (-not $targets.Count) { Stop-Run 1 'There is no build of yours to test yet.' ((Get-EasyAgain) + ' (its first run builds).') }
            $failed = @()
            foreach ($row in $targets) {
                Set-EasyHoudiniTarget $row
                try { if ((Invoke-Part 'easy-quick-tests.ps1' $testArguments) -ne 0) { $failed += $row.Version } } finally { Set-EasyHoudiniTarget $null }
            }
            if ($failed.Count) { Stop-Run 1 "A quick test failed for Houdini $($failed -join ', ') (details above)." ((Get-EasyAgain 'Update the sources and rebuild') + '; if it still fails, open an issue on the project page on GitHub with the lines above.') }
            Write-EasyResult $true 'The quick tests passed.' ''
            exit 0
        }
        'DlssOn' { Set-DlssSetting 'on'; Write-EasyResult $true 'DLSS is on (NVIDIA''s RTX SDKs licence applies; it works on NVIDIA cards only).' ((Get-EasyAgain 'Update the sources and rebuild') + ' to build it in.'); exit 0 }
        'DlssOff' { Set-DlssSetting 'off'; Write-EasyResult $true 'DLSS is off.' ((Get-EasyAgain 'Update the sources and rebuild') + ' to build without it.'); exit 0 }
        'Installer' {
            # "Make the installers again": for every built Houdini build (or the one -Houdini names),
            # after building what changed and running the quick tests again.
            $targets = @(Get-Rows | Where-Object { $_.Kit -and $_.State -in @('built', 'older') })
            if ($env:CYCLES_EASY_HOUDINI -match '^\d+\.\d+\.\d+$') { $targets = @($targets | Where-Object { $_.Version -eq $env:CYCLES_EASY_HOUDINI }) }
            if (-not $targets.Count) { Stop-Run 1 'There is no tested build of yours yet.' ((Get-EasyAgain) + ' (its first run builds).') }
            if (-not $StopBeforeInstaller -and -not (Find-EasyInno)) { Stop-Run 3 'No installer was made: Inno Setup 6, which makes the installers, is missing.' ((Get-EasyAgain 'Check this computer again') + ' and answer y to install it, then choose "Make the installers again".') }
            $results = @(foreach ($row in $targets) {
                $made = Invoke-Installer $row
                [pscustomobject]@{ Version = $row.Version; Ok = $made.Ok; What = $(if ($made.Ok) { 'installer made' } else { 'no installer (the reason is above)' }); Minutes = 0; Setup = $made.Setup; Built = $true }
            })
            Write-Installers $results
            $failed = @($results | Where-Object { -not $_.Ok })
            if ($failed.Count) { Stop-Run 1 ('No installer for Houdini ' + (($failed | ForEach-Object { $_.Version }) -join ', ') + ' (the reason is above).') ('Fix what the message says, then ' + (Get-EasyAgain 'Make the installers again') + '.') }
            Write-EasyResult $true $(if ($StopBeforeInstaller) { 'Packages made and verified (no Setup, as asked with -StopBeforeInstaller).' } else { 'The installers are made (listed above).' }) $(if ($StopBeforeInstaller) { '' } else { 'Double-click an installer to install Cycles into its Houdini build.' })
            exit 0
        }
        'EnginePatches' {
            $arguments = @()
            if ($Message) { $arguments += @('-Message', $Message) }
            if ((Invoke-Part 'easy-update-engine-patches.ps1' $arguments) -ne 0) { Stop-Run 1 'The engine patches were not updated (the reason is above).' ('Fix what the message says, then ' + (Get-EasyAgain 'I changed the engine: update the patch files') + '; it continues from where it stopped.') }
            Write-EasyResult $true 'patches\ and source.lock.json now describe your engine.' ((Get-EasyAgain 'Update the sources and rebuild') + ' (it also runs the quick tests and makes new installers).')
            exit 0
        }
        'CheckComputer' {
            Invoke-CheckComputer -ReviewNvidiaConsent
            Write-EasyResult $true 'Everything needed to build is on this computer.' ((Get-EasyAgain) + '.')
            exit 0
        }
    }
}

# Builds and installers of an earlier version were next to the project folder: said once, never moved or deleted.
function Invoke-EarlierLayout {
    $old = @(Get-EasyEarlierFolders | Where-Object { Test-Path -LiteralPath $_ })
    if (-not $old.Count) { return }
    $state = Read-State
    $new = @($old | Where-Object { $state.earlier_layout_offered -notcontains $_ })
    if (-not $new.Count) { return }
    foreach ($folder in $new) { Write-EasyInfo "An earlier version kept its builds or installers in $folder. Everything is now inside this folder (_work and Installers), so that folder is no longer used; you may delete it." }
    $state.earlier_layout_offered = @($state.earlier_layout_offered) + $new
    Save-State $state
}
Invoke-EarlierLayout
if ($Action -ne 'Auto') { Invoke-Action $Action }

# --- A later run: did Houdini change? ---------------------------------------------------------------
$rows = Get-Rows
$state = Read-State
$changes = Get-EasyHoudiniChanges $rows $state.seen $root $state.retry
$orphans = @($changes.Gone | ForEach-Object { $_.Version })
foreach ($gone in $changes.Gone) { Write-EasyInfo "Houdini $($gone.Version) is no longer installed, so your build for it is not used any more ($($gone.Build); you may delete that folder)." }
$working = @($rows | Where-Object { $_.Kit -and $_.State -in @('built', 'older') })
if (-not $working.Count) {
    $why = ''
    if ($orphans.Count) { $why = "Your build was made for Houdini $($orphans -join ', '), which is no longer installed. A plugin must match its Houdini build, so this builds again for the Houdini you have now." }
    Invoke-FirstRun $why
}
$new = @($changes.New)
if ($new.Count) {
    foreach ($row in $new) {
        if ($state.retry -contains $row.Version) { Write-EasyInfo "Retrying the unfinished build for Houdini $($row.Version)." }
        else { Write-EasyInfo "Houdini $($row.Version) is new on this computer and has no Cycles yet: building for it now, with its installer (a plugin must match its Houdini build)." }
    }
    $results = Invoke-Builds $new
    if (@($results | Where-Object { -not $_.Ok }).Count) { Stop-AfterBuilds $results "Update the sources and rebuild" }
    Write-Installers $results
    $rows = Get-Rows
}
Remember-Seen $rows

# --- Menu (a tested build exists) -------------------------------------------------------------------
$dlssItem = if ((Get-DlssSetting) -ne 'off') { @('6', 'DlssOff', 'Turn DLSS off') } else { @('6', 'DlssOn', 'Turn DLSS on (NVIDIA''s DLSS licence applies)') }
$menu = @(
    @('1', 'Update', 'Update the sources and rebuild (then the quick tests and new installers)'),
    @('2', 'QuickTests', 'Run the quick tests'),
    @('3', 'Installer', 'Make the installers again'),
    @('4', 'OpenHoudini', 'Open Houdini with my build (without installing; to try it, or while changing code)'),
    @('5', 'EnginePatches', 'I changed the engine: update the patch files'),
    $dlssItem,
    @('7', 'CheckComputer', 'Check this computer again'),
    @('0', 'Close', 'Close')
)
Write-EasyTitle 'Cycles for Houdini: your build'
Show-Rows $rows
Write-Host ''
Write-Host "Installers: $installers"
foreach ($row in $menu) { Write-Host ("  {0}  {1}{2}" -f $row[0], $row[2], $(if ($row[0] -eq '0') { '   (press Enter for this)' } else { '' })) }
if (Test-EasyUnattended) { Stop-Run 1 'Unattended run without -Action: nothing chosen.' 'Pass -Action (see the top of scripts/easy-start.ps1).' }
$picked = $null
for ($try = 0; $try -lt 3 -and -not $picked; $try++) {
    try { $answer = Read-Host 'Type a number and press Enter' } catch { $answer = '0' }
    $answer = ([string]$answer).Trim()
    if (-not $answer) { $answer = '0' }
    foreach ($row in $menu) { if ($row[0] -eq $answer) { $picked = $row[1] } }
    if (-not $picked) { Write-Host 'Please type one of the numbers above.' }
}
if (-not $picked -or $picked -eq 'Close') { Write-EasyResult $true 'Nothing was changed.' ''; exit 0 }
Invoke-Action $picked
