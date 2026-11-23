# SPDX-License-Identifier: Apache-2.0
# Which Houdini builds are on this computer, which of them can be built for, and which already
# have Cycles. Used by the double-click file "START HERE - Build Cycles for Houdini.cmd"
# (scripts/easy-*.ps1); dot-sourced by scripts/easy-common.ps1.
# Works in Windows PowerShell 5.1 and PowerShell 7 (the computer check runs in 5.1).
#
# A plugin must match the Houdini build it was compiled for, so there is one build folder per
# Houdini build (<project>\_work\windows-h<build>-release, Get-EasyBuildDirectoryFor) and every step is told
# which Houdini build it works for (CYCLES_EASY_TARGET_HOUDINI and CYCLES_EASY_TARGET_ROOT, set
# by scripts/easy-start.ps1 for each build).
#
# Reads Houdini installations and preferences without modifying them.
#
# Settings (all optional):
#   CYCLES_EASY_HOUDINI         which Houdini builds to build for: missing (the default: those without
#                               Cycles yet), all, newest, or one build such as 22.0.508 (also -Houdini
#                               of scripts/easy-start.ps1).
#   CYCLES_EASY_HOUDINI_SEARCH  for tests: ';'-separated folders that hold "Houdini X.Y.Z" folders;
#                               when set, only these are searched (no registry, no Program Files).
#   CYCLES_EASY_HOUDINI_PREFS   for tests: the Houdini preferences folder (default: as Setup does,
#                               HOUDINI_USER_PREF_DIR or Documents\houdini<major.minor>).

function Get-EasyTestedHoudini { [string](Get-EasyLock).certification_candidate.houdini }
# The major.minor this project supports (22.0): the tested build's.
function Get-EasySupportedHoudini { (((Get-EasyTestedHoudini) -split '\.')[0..1]) -join '.' }

# SYS_VERSION_FULL of a Houdini folder's development kit; $null without the development kit.
function Read-EasyHoudiniKitVersion([string]$Root) {
    $header = Join-Path $Root 'toolkit\include\SYS\SYS_Version.h'
    if (-not (Test-Path -LiteralPath $header -PathType Leaf)) { return $null }
    $match = [regex]::Match((Get-Content -LiteralPath $header -Raw), '(?m)^#define SYS_VERSION_FULL "(\d+\.\d+\.\d+)"')
    if ($match.Success) { return $match.Groups[1].Value }
    return $null
}

# Folders that may hold a Houdini installation: SideFX's registry entries and the default folder.
function Get-EasyHoudiniFolders {
    $folders = New-Object System.Collections.Generic.List[string]
    if ($env:CYCLES_EASY_HOUDINI_SEARCH) {
        foreach ($base in ($env:CYCLES_EASY_HOUDINI_SEARCH -split ';')) {
            if ($base -and (Test-Path -LiteralPath $base -PathType Container)) {
                foreach ($dir in @(Get-ChildItem -LiteralPath $base -Directory -Filter 'Houdini *' -ErrorAction SilentlyContinue)) { $folders.Add($dir.FullName) }
            }
        }
        return $folders.ToArray()
    }
    # SideFX's installer records each build in HKLM\SOFTWARE\Side Effects Software\Houdini as a
    # value named like 22.0.0.508 whose data is the installation folder. Read only.
    foreach ($view in @('Registry64', 'Registry32')) {
        try {
            $hklm = [Microsoft.Win32.RegistryKey]::OpenBaseKey([Microsoft.Win32.RegistryHive]::LocalMachine, [Microsoft.Win32.RegistryView]::$view)
            $key = $hklm.OpenSubKey('SOFTWARE\Side Effects Software\Houdini')
            if ($key) {
                foreach ($name in $key.GetValueNames()) {
                    if ($name -notmatch '^\d+\.\d+\.\d+\.\d+$') { continue }
                    $value = [string]$key.GetValue($name)
                    if ($value -and (Test-Path -LiteralPath $value -PathType Container)) { $folders.Add($value.TrimEnd('\')) }
                }
                $key.Close()
            }
            $hklm.Close()
        } catch { }
    }
    $default = Join-Path $env:ProgramFiles 'Side Effects Software'
    if ($env:ProgramFiles -and (Test-Path -LiteralPath $default -PathType Container)) {
        foreach ($dir in @(Get-ChildItem -LiteralPath $default -Directory -Filter 'Houdini *' -ErrorAction SilentlyContinue)) { $folders.Add($dir.FullName) }
    }
    return $folders.ToArray()
}

# Every installed Houdini of the supported major.minor, newest first:
#   Version, Root, Kit ($true when the development kit is there), Tested ($true for 22.0.466).
# Versions of other major.minor releases (for example 21.0) are left out.
function Get-EasyHoudiniInstalls {
    $supported = Get-EasySupportedHoudini
    $tested = Get-EasyTestedHoudini
    $seen = @{}
    $found = New-Object System.Collections.Generic.List[object]
    foreach ($folder in (Get-EasyHoudiniFolders)) {
        $full = [IO.Path]::GetFullPath($folder).TrimEnd('\', '/')
        if ($seen.ContainsKey($full.ToLowerInvariant())) { continue }
        $seen[$full.ToLowerInvariant()] = $true
        $kit = Read-EasyHoudiniKitVersion $full
        $version = $kit
        if (-not $version) {
            $name = [regex]::Match((Split-Path -Leaf $full), '^Houdini (\d+\.\d+\.\d+)$')
            if (-not $name.Success) { continue }
            # Without the development kit only a folder that really holds Houdini counts.
            if (-not (Test-Path -LiteralPath (Join-Path $full 'bin\houdini.exe') -PathType Leaf) -and -not (Test-Path -LiteralPath (Join-Path $full 'bin\hython.exe') -PathType Leaf)) { continue }
            $version = $name.Groups[1].Value
        }
        if (-not $version.StartsWith($supported + '.')) { continue }
        $found.Add([pscustomobject]@{ Version = $version; Root = $full; Kit = [bool]$kit; Tested = ($version -eq $tested) })
    }
    # One entry per version (the one with the development kit first).
    $byVersion = [ordered]@{}
    foreach ($item in @($found | Sort-Object @{ Expression = { [version]$_.Version }; Descending = $true }, @{ Expression = { $_.Kit }; Descending = $true })) {
        if (-not $byVersion.Contains($item.Version)) { $byVersion[$item.Version] = $item }
    }
    return @($byVersion.Values)
}

# The Houdini build the current step works for. Set per build by scripts/easy-start.ps1; a step
# started on its own uses CYCLES_EASY_HOUDINI when it names one build, else the newest with the
# development kit, else the tested build's default folder (whose absence the step then reports).
function Get-EasyHoudiniTarget {
    if ($env:CYCLES_EASY_TARGET_HOUDINI -and $env:CYCLES_EASY_TARGET_ROOT) {
        return [pscustomobject]@{ Version = $env:CYCLES_EASY_TARGET_HOUDINI; Root = $env:CYCLES_EASY_TARGET_ROOT; Kit = $true; Tested = ($env:CYCLES_EASY_TARGET_HOUDINI -eq (Get-EasyTestedHoudini)) }
    }
    $kits = @(Get-EasyHoudiniInstalls | Where-Object { $_.Kit })
    $wanted = [string]$env:CYCLES_EASY_HOUDINI
    if ($wanted -match '^\d+\.\d+\.\d+$') {
        $one = @($kits | Where-Object { $_.Version -eq $wanted })
        if ($one.Count) { return $one[0] }
    }
    if ($kits.Count) { return $kits[0] }
    $tested = Get-EasyTestedHoudini
    return [pscustomobject]@{ Version = $tested; Root = (Join-Path $env:ProgramFiles ('Side Effects Software\Houdini ' + $tested)); Kit = $false; Tested = $true }
}
function Set-EasyHoudiniTarget($Install) {
    if ($Install) { $env:CYCLES_EASY_TARGET_HOUDINI = $Install.Version; $env:CYCLES_EASY_TARGET_ROOT = $Install.Root }
    else { $env:CYCLES_EASY_TARGET_HOUDINI = $null; $env:CYCLES_EASY_TARGET_ROOT = $null }
}

# --- Has this Houdini build got Cycles? ----------------------------------------------------------
# The Houdini preferences folder that Setup (packaging/installer/CyclesHoudini.iss) installs into.
function Get-EasyPreferencesFolder {
    $mm = Get-EasySupportedHoudini
    $folder = [string]$env:CYCLES_EASY_HOUDINI_PREFS
    if (-not $folder) { $folder = [string]$env:HOUDINI_USER_PREF_DIR }
    if ($folder) { return $folder.Replace('__HVER__', $mm) }
    return (Join-Path ([Environment]::GetFolderPath('MyDocuments')) ('houdini' + $mm))
}

# The Houdini versions a package file's "enable" makes it load for. The grammar is the one
# scripts/release-package.py writes (package_enable, package_eligible_versions): exact builds,
# "(... and $CYCLES_HOUDINI_STRICT_BUILD != '1')" alternates, or a major.minor ("22.0.*").
# Anything else, and a package without "enable", counts as loading for every build ('*').
function ConvertFrom-EasyEligibility($Enable) {
    if ($Enable -is [bool]) { if ($Enable) { return @('*') } else { return @() } }
    if ($null -eq $Enable) { return @('*') }
    $versions = @()
    foreach ($part in (([string]$Enable) -split ' or ')) {
        $p = $part.Trim()
        $m = [regex]::Match($p, '^houdini_version == ''(\d+\.\d+\.\d+)''$')
        if (-not $m.Success) { $m = [regex]::Match($p, '^\(houdini_version == ''(\d+\.\d+\.\d+)'' and \$CYCLES_HOUDINI_STRICT_BUILD != ''1''\)$') }
        if ($m.Success) { $versions += $m.Groups[1].Value; continue }
        $m = [regex]::Match($p, '^houdini_version == ''(\d+\.\d+)''$')
        if ($m.Success) { $versions += ($m.Groups[1].Value + '.*'); continue }
        return @('*')
    }
    return $versions
}
function Test-EasyEligible([string[]]$Versions, [string]$Version) {
    foreach ($v in $Versions) {
        if ($v -eq '*' -or $v -eq $Version) { return $true }
        if ($v.EndsWith('.*') -and $Version.StartsWith($v.Substring(0, $v.Length - 1))) { return $true }
    }
    return $false
}

# Every Cycles package file in the preferences folder, read for target selection.
function Get-EasyCyclesPackages([string]$Preferences = (Get-EasyPreferencesFolder)) {
    $folder = Join-Path $Preferences 'packages'
    $list = New-Object System.Collections.Generic.List[object]
    if (-not (Test-Path -LiteralPath $folder -PathType Container)) { return @() }
    foreach ($file in @(Get-ChildItem -LiteralPath $folder -Filter 'cycles_houdini*.json' -File -ErrorAction SilentlyContinue | Sort-Object Name)) {
        $versions = @('*')
        try {
            $package = Get-Content -LiteralPath $file.FullName -Raw | ConvertFrom-Json
            $hasEnable = @($package.PSObject.Properties.Name) -contains 'enable'
            $versions = @(ConvertFrom-EasyEligibility $(if ($hasEnable) { $package.enable } else { $null }))
        } catch { }
        $list.Add([pscustomobject]@{ Path = $file.FullName; Name = $file.Name; Versions = $versions })
    }
    return $list.ToArray()
}

# Cycles for Houdini installed by a Setup.exe (official or community) or by hand for this Houdini
# build: every Cycles package file that loads for it.
function Get-EasyOtherCycles([string]$Version, $Packages) {
    return @($Packages | Where-Object { Test-EasyEligible $_.Versions $Version })
}

# Is there a build in this folder, and did its quick tests pass (the summary records the build
# manifest's hash)? Returns Build, Tested (time), Passed, Stamp (easy-build.json) or $null.
function Get-EasyBuildState([string]$BuildDirectory) {
    $manifest = Join-Path $BuildDirectory 'build-manifest.json'
    if (-not (Test-Path -LiteralPath $manifest -PathType Leaf)) { return $null }
    $hash = (Get-FileHash -LiteralPath $manifest -Algorithm SHA256).Hash.ToLowerInvariant()
    $state = [pscustomobject]@{ Build = $BuildDirectory; Passed = $false; Tested = $null; Stamp = $null }
    $stamp = Join-Path $BuildDirectory 'easy-build.json'
    if (Test-Path -LiteralPath $stamp -PathType Leaf) { try { $state.Stamp = Get-Content -LiteralPath $stamp -Raw | ConvertFrom-Json } catch { } }
    $tests = Join-Path $BuildDirectory 'quick-tests'
    foreach ($summary in @(Get-ChildItem -LiteralPath $tests -Filter 'summary.json' -Recurse -ErrorAction SilentlyContinue | Sort-Object LastWriteTimeUtc -Descending)) {
        try { $data = Get-Content -LiteralPath $summary.FullName -Raw | ConvertFrom-Json } catch { continue }
        if ($data.build_manifest_sha256 -eq $hash) {
            $state.Passed = ($data.result -eq 'passed')
            $state.Tested = $summary.LastWriteTime
            break
        }
    }
    return $state
}

# One line per Houdini build: what it has. State: built (tests passed, current sources),
# older (built from older sources), untested (built, quick tests not passed), setup (Cycles
# installed by a Setup or by hand), nokit (no development kit), none (no Cycles yet).
function Get-EasyHoudiniStates([string]$SourceRoot, [string]$SourceId, $Installs = (Get-EasyHoudiniInstalls), $Packages = (Get-EasyCyclesPackages)) {
    . (Join-Path $PSScriptRoot 'build-layout.ps1')
    $rows = New-Object System.Collections.Generic.List[object]
    foreach ($install in @($Installs)) {
        # Plain variables, then one [pscustomobject]@{...}: filling an [ordered] dictionary and casting it
        # failed in PowerShell 7 with "Argument types do not match" (seen on the development machine).
        $build = Get-EasyBuildDirectoryFor -SourceRoot $SourceRoot -Version $install.Version
        $buildState = Get-EasyBuildState $build
        $other = @(Get-EasyOtherCycles $install.Version $Packages)
        $state = 'none'
        if (-not $install.Kit) { $state = 'nokit' }
        elseif ($buildState -and $buildState.Passed) {
            $state = 'built'
            if ($SourceId -and $buildState.Stamp -and $buildState.Stamp.source_id -and $buildState.Stamp.source_id -ne $SourceId) { $state = 'older' }
        }
        elseif ($buildState) { $state = 'untested' }
        elseif ($other.Count) { $state = 'setup' }
        $rows.Add([pscustomobject]@{ Version = $install.Version; Root = $install.Root; Kit = $install.Kit; Tested = $install.Tested; State = $state; Build = $build; BuildState = $buildState; Other = $other })
    }
    return $rows.ToArray()
}
# Plain words for a state row.
function Format-EasyHoudiniState($Row) {
    $text = switch ($Row.State) {
        'built' { 'Cycles built' + $(if ($Row.BuildState.Tested) { ' ' + $Row.BuildState.Tested.ToString('yyyy-MM-dd') } else { '' }) + ', quick tests passed' }
        'older' { 'Cycles built' + $(if ($Row.BuildState.Tested) { ' ' + $Row.BuildState.Tested.ToString('yyyy-MM-dd') } else { '' }) + ' from older sources ("Update the sources and rebuild" refreshes it)' }
        'untested' { 'built, but its quick tests have not passed yet' }
        'setup' { 'Cycles for Houdini is installed for it (' + (@($Row.Other | ForEach-Object { $_.Name }) -join ', ') + '); left as it is' }
        'nokit' { 'no development kit, cannot build for it (install it again with the development kit)' }
        default { 'no Cycles yet' }
    }
    if ($Row.Kit -and -not $Row.Tested) { $text += '; not the tested build' }
    return ('Houdini ' + $Row.Version + ': ' + $text)
}

# Which Houdini builds to build for. Choice: missing (the default: those with the development
# kit that have no Cycles yet, that is no tested build here and no Cycles for Houdini installed by a
# Setup), all (every one with the development kit), newest, or one version such as 22.0.508.
# A build of your own never touches a Setup install: it lives in its own build folder, and the
# build itself installs nothing (its installer, run by the person, does).
# Returns Targets (rows) and Problem (a plain sentence when the choice cannot be met).
function Select-EasyHoudiniTargets($Rows, [string]$Choice) {
    $kits = @($Rows | Where-Object { $_.Kit })
    $choice = ([string]$Choice).Trim().ToLowerInvariant()
    if (-not $choice) { $choice = 'missing' }
    $targets = @(); $problem = ''
    if ($choice -eq 'missing') { $targets = @($kits | Where-Object { $_.State -in @('none', 'untested') }) }
    elseif ($choice -eq 'all') { $targets = $kits }
    elseif ($choice -eq 'newest') { $targets = @($kits | Select-Object -First 1) }
    elseif ($choice -match '^\d+\.\d+\.\d+$') {
        $targets = @($kits | Where-Object { $_.Version -eq $choice })
        if (-not $targets.Count) {
            if (@($Rows | Where-Object { $_.Version -eq $choice }).Count) { $problem = "Houdini $choice is installed without its development kit, so Cycles cannot be built for it." }
            else { $problem = "Houdini $choice is not installed on this computer (or it is not Houdini $(Get-EasySupportedHoudini))." }
        }
    } else { $problem = "Unknown Houdini choice '$Choice': use missing, all, newest or a build such as $(Get-EasyTestedHoudini)." }
    if (-not $kits.Count -and -not $problem) { $problem = "No Houdini $(Get-EasySupportedHoudini) with its development kit is installed." }
    return [pscustomobject]@{ Targets = @($targets); Problem = $problem }
}

# What changed since the last run (Seen: the Houdini builds installed then).
#   Gone: builds of yours (Version, Build) whose Houdini build was seen before and is no longer installed;
#   New:  rows of Houdini builds with the development kit that were not there before and have no Cycles.
# Retry also includes explicitly chosen builds that did not finish.
function Get-EasyHoudiniChanges($Rows, $Seen, [string]$SourceRoot, [string[]]$Retry = @()) {
    . (Join-Path $PSScriptRoot 'build-layout.ps1')
    $seen = @($Seen | Where-Object { $_ })
    $installed = @($Rows | ForEach-Object { $_.Version })
    $parent = Join-Path $SourceRoot '_work'
    $gone = New-Object System.Collections.Generic.List[object]
    foreach ($dir in @(Get-ChildItem -LiteralPath $parent -Directory -Filter 'windows-h*-release' -ErrorAction SilentlyContinue | Sort-Object Name)) {
        $m = [regex]::Match($dir.Name, '^windows-h(\d+\.\d+\.\d+)-release$')
        if (-not $m.Success) { continue }
        $version = $m.Groups[1].Value
        if ($installed -notcontains $version -and $seen -contains $version -and (Test-Path -LiteralPath (Join-Path $dir.FullName 'build-manifest.json'))) {
            $gone.Add([pscustomobject]@{ Version = $version; Build = $dir.FullName })
        }
    }
    $new = @($Rows | Where-Object {
        $_.Kit -and ($Retry -contains $_.Version -or ($seen.Count -and $seen -notcontains $_.Version -and $_.State -in @('none', 'untested')))
    })
    return [pscustomobject]@{ Gone = $gone.ToArray(); New = @($new) }
}

# --- Houdini running with a build that is about to be rebuilt -------------------------------------
# A rebuild puts the plugin into a new folder (scripts/build.ps1: hosts\<time>), but it also rewrites the build's
# package file and shared parts. So a build is not rebuilt while a Houdini (houdini, hython, hbatch, ...) of
# that Houdini build has it loaded, or while its render worker (ch_worker.exe) runs.
# Processes: objects with Name, Path and Modules (paths of loaded files); by default this computer's processes
# (read only). Returns the plain names of what is running, empty when nothing is.
function Get-EasyProcessesUsingBuild($Install, [string]$BuildDirectory, $Processes = $null) {
    if ($null -eq $Processes) {
        $Processes = @(foreach ($process in @(Get-Process -ErrorAction SilentlyContinue)) {
            $path = $null; try { $path = $process.Path } catch { }
            if (-not $path) { continue }
            $interesting = ($path -like ($Install.Root.TrimEnd('\') + '\bin\*')) -or ($path -like ($BuildDirectory.TrimEnd('\') + '\*'))
            if (-not $interesting) { continue }
            $modules = @(); try { $modules = @($process.Modules | ForEach-Object { $_.FileName }) } catch { }
            [pscustomobject]@{ Name = $process.ProcessName; Path = $path; Modules = $modules }
        })
    }
    $build = $BuildDirectory.TrimEnd('\', '/').Replace('/', '\') + '\'
    $bin = $Install.Root.TrimEnd('\', '/').Replace('/', '\') + '\bin\'
    $found = @()
    foreach ($process in @($Processes)) {
        $path = ([string]$process.Path).Replace('/', '\')
        if ($path.StartsWith($build, [StringComparison]::OrdinalIgnoreCase)) { $found += $process.Name; continue }
        if (-not $path.StartsWith($bin, [StringComparison]::OrdinalIgnoreCase)) { continue }
        foreach ($module in @($process.Modules)) {
            if (([string]$module).Replace('/', '\').StartsWith($build, [StringComparison]::OrdinalIgnoreCase)) { $found += $process.Name; break }
        }
    }
    return @($found | Sort-Object -Unique)
}
function Get-EasyCloseHoudiniSentence([string]$Version) { "Close Houdini $Version first (it is using your build), then run this again." }
