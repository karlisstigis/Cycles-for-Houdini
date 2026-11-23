# SPDX-License-Identifier: Apache-2.0
# Makes the installer (Setup.exe) of YOUR build for one Houdini build (CYCLES_EASY_TARGET_HOUDINI):
# the last part of every build by "START HERE - Build Cycles for Houdini.cmd" (scripts/easy-start.ps1)
# and its menu item "Make the installers again". Built from the public sources, labelled with
# build-identity.json so that it never claims to be the official Cycles for Houdini. It rebuilds what
# changed, checks the engine against patches/, runs the quick tests, packages the build, verifies the
# package and compiles the Setup. It never runs the Setup or installs into Houdini.
# The Setup goes into <project>\Installers\ (one file per Houdini build, named after the folder it installs to,
# for example "Cycles for Houdini 22.0.508 (community) - Setup.exe"; a newer one replaces the older file of the
# same name; with a READ ME.txt); what went into it (package, reports, logs) into
# <project>\_work\installer-details\<Houdini>-<time>\ (with -InstallerFolder: <that folder>\details\).
# The official release route is scripts/build-release.ps1 ("launchers\Build and make installer.cmd").
#   -Tested               this build was just built and its quick tests passed in this run: it is not
#                         rebuilt, and the quick-test result recorded for exactly this build is used
#   -StopBeforeInstaller  do everything except compiling the Setup (makes the package only)
#   -InstallerFolder      where the Setup goes (default <project>\Installers)
#   -OutputDirectory      a new, empty folder for the details (default under the installer folder)
#   -ResultFile           also write the report (community-build-report.json) there
# Exit codes: 0 made; 1 not made (the reason is printed); 3 Inno Setup 6 is missing (nothing was made).
[CmdletBinding()]
param([string]$OutputDirectory = '', [string]$InstallerFolder = '', [string]$ResultFile = '', [switch]$StopBeforeInstaller, [switch]$Tested, [int]$Jobs = 6)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'easy-common.ps1')
trap { Write-EasyCrash $_; exit 1 }
$root = $script:EasyRoot
$houdini = Get-EasyHoudiniVersion
Write-EasyTitle "Make the installer (Houdini $houdini)"
$git = Find-EasyGit
$python = Find-EasyPython
if (-not $git -or -not $python) { Write-EasyResult $false 'Git or Python is missing.' ((Get-EasyAgain 'Check this computer again') + '.'); exit 1 }
$iscc = Find-EasyInno
if (-not $iscc -and -not $StopBeforeInstaller) {
    Write-EasyResult $false "No installer was made for Houdini ${houdini}: Inno Setup 6, which makes it, is not on this computer." ((Get-EasyAgain 'Check this computer again') + ' and answer y to install Inno Setup 6, then choose "Make the installers again".')
    exit 3
}

# --- Who made this build -------------------------------------------------------------
Write-EasyStep 'Build identity (build-identity.json)'
$identity = Get-EasyBuildIdentity
if (-not $identity) { Write-EasyResult $false 'build-identity.json needs fixing (message above).' 'See docs/dev/community-builds.md.'; exit 1 }
Write-EasyOk "$($identity.target.display_name), made by $($identity.publisher)"
Write-EasyInfo "Installs to %LOCALAPPDATA%\$($identity.target.default_directory); Houdini package file $($identity.target.package_filename)"
if ($identity.publisher -eq 'Unnamed community builder') {
    Write-EasyInfo 'Your build has no name of your own yet (build-identity.json), so the installer says "Unnamed community builder".'
    Write-EasyInfo 'That is fine for your own use. Before you give it to someone else, put your name into build-identity.json.'
}

# --- DLSS and its NVIDIA project id ------------------------------------------------------
# "on": your own project id (build-identity.json, made on first use). The official id only with
# "official-project-id-with-permission". What was really compiled is checked after the build.
$officialDlss = [regex]::Match((Get-Content -LiteralPath (Join-Path $root 'patches\0077-houdini-dlss-project.patch') -Raw), '\+static constexpr char ngx_project_id\[\] = "([^"]+)"').Groups[1].Value
# --- State of the sources --------------------------------------------------------------
Write-EasyStep 'State of the sources'
$commit = Get-EasyGitHead $git
$isCheckout = [bool]$commit
$changes = @()
if ($isCheckout) {
    $changes = Get-EasySourceChanges $git
    Write-EasyInfo "Commit $commit"
} else {
    # Downloaded as a ZIP: there is no commit to record, and changes cannot be seen. The installer says so.
    $commit = $null
    Write-EasyInfo 'This folder was downloaded as a ZIP, so the installer records its source commit and local changes as "unknown".'
}
if ($changes.Count) { Write-EasyInfo "$($changes.Count) file(s) changed or added and not committed: the installer records that it was built with local changes." }
elseif ($isCheckout) { Write-EasyOk 'No uncommitted changes.' }

# The package tool runs with Houdini's own Python, as the installer does (it needs Python 3.11+).
$houdiniPython = Join-Path (Get-EasyHoudiniRoot) 'python313\python.exe'
if (-not (Test-Path -LiteralPath $houdiniPython)) { Write-EasyResult $false "Houdini's Python is missing: $houdiniPython" "Install Houdini $houdini with its default components."; exit 1 }
$detailsRoot = if ($InstallerFolder) { Join-Path ([IO.Path]::GetFullPath($InstallerFolder)) 'details' } else { Join-Path $script:EasyWork 'installer-details' }
if (-not $InstallerFolder) { $InstallerFolder = Get-EasyInstallerFolder }
$InstallerFolder = [IO.Path]::GetFullPath($InstallerFolder)
$stamp = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmss')
if (-not $OutputDirectory) { $OutputDirectory = Join-Path $detailsRoot ($houdini + '-' + $stamp) }
$OutputDirectory = [IO.Path]::GetFullPath($OutputDirectory)
if ((Test-Path -LiteralPath $OutputDirectory) -and @(Get-ChildItem -LiteralPath $OutputDirectory -Force).Count) { Write-EasyResult $false "The output folder is not empty: $OutputDirectory" 'Choose a new folder.'; exit 1 }
New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
$checks = New-Object System.Collections.Generic.List[object]
function Add-Check([string]$Name, [string]$Result, [string]$Detail = '') { $checks.Add([ordered]@{ name = $Name; result = $Result; detail = $Detail }) }
function Stop-Easy([string]$What, [string]$Next) { Write-EasyResult $false $What $Next; Write-EasyInfo "Logs and reports: $OutputDirectory"; exit 1 }

# --- Build, check, test ----------------------------------------------------------------
$build = Get-EasyBuildDirectory
$using = @(Get-EasyProcessesUsingBuild (Get-EasyHoudiniTarget) $build)
if ($using.Count) { Stop-Easy (Get-EasyCloseHoudiniSentence $houdini) ((Get-EasyAgain 'Make the installers again') + '.') }
# -Tested: the quick tests of this run passed for exactly this build (their summary records the hash of
# its build manifest); otherwise it is built (only what changed) and tested here.
$testSummary = $null
$manifestPath = Join-Path $build 'build-manifest.json'
if ($Tested -and (Test-Path -LiteralPath $manifestPath)) {
    $manifestHash = (Get-FileHash -LiteralPath $manifestPath -Algorithm SHA256).Hash.ToLowerInvariant()
    foreach ($candidate in @(Get-ChildItem -LiteralPath (Join-Path $build 'quick-tests') -Filter 'summary.json' -Recurse -ErrorAction SilentlyContinue | Sort-Object LastWriteTimeUtc -Descending)) {
        try { $data = Get-Content -LiteralPath $candidate.FullName -Raw | ConvertFrom-Json } catch { continue }
        if ($data.build_manifest_sha256 -eq $manifestHash) { if ($data.result -eq 'passed') { $testSummary = $data }; break }
    }
}
if ($testSummary) {
    Write-EasyStep 'Build: just built and tested in this run'
    & $python -I (Join-Path $PSScriptRoot 'engine-fork.py') verify
    if ($LASTEXITCODE -ne 0) { Stop-Easy 'The engine differs from the locked engine (source.lock.json). No installer was made.' ((Get-EasyAgain 'I changed the engine: update the patch files') + ', then "Make the installers again".') }
    Add-Check 'build-and-engine-verify' 'passed' 'built and tested in this run (scripts/easy-get-and-build.ps1, scripts/build.ps1); engine checked again: scripts/engine-fork.py verify'
} else {
    Write-EasyStep 'Build (only what changed since the last build)'
    if (-not (Invoke-EasyScript 'easy-get-and-build.ps1' @('-NoUpdate', '-Jobs', [string]$Jobs))) { Stop-Easy 'The build failed (message above). No installer was made.' 'Fix the error (or choose "I changed the engine: update the patch files" if you changed the engine), then "Make the installers again".' }
    Add-Check 'build-and-engine-verify' 'passed' 'scripts/build.ps1: engine matches source.lock.json and a full patch replay'
}
# The DLSS project id actually compiled: the copy CMakeLists.txt makes for "on", else the engine's own.
$features = (Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json).features
$hasDlss = [string]$features.WITH_DLSS -in @('ON', 'TRUE', '1')
$dlssProject = $null
if ($hasDlss) {
    $copy = Join-Path $build 'community-dlss\denoiser_dlss.cpp'
    $source = if (Test-Path -LiteralPath $copy) { $copy } else { Join-Path $root 'third_party\cycles\src\integrator\denoiser_dlss.cpp' }
    $dlssProject = [regex]::Match((Get-Content -LiteralPath $source -Raw), 'ngx_project_id\[\] = "([^"]+)"').Groups[1].Value
    $expected = if ($identity.dlss -eq 'on') { $identity.dlss_project_id } else { $officialDlss }
    if (-not $dlssProject -or $dlssProject -ne $expected) { Stop-Easy "The build has DLSS project id '$dlssProject', but build-identity.json asks for '$expected'." 'Choose "Update the sources and rebuild"; if it says the same, open an issue on the project page on GitHub with the lines above.' }
    if ($identity.dlss -eq 'on' -and $dlssProject -eq $officialDlss) { Stop-Easy 'The build has the official DLSS project id.' 'Leave dlss_project_id empty in build-identity.json (a new one is made) and choose "Update the sources and rebuild".' }
    Add-Check 'dlss-project-id' 'passed' "compiled with $dlssProject ($($identity.dlss))"
}
$route = (& $python -I (Join-Path $PSScriptRoot 'engine-fork.py') route).Trim()
$engineTree = (& $git -C (Join-Path $root 'third_party\cycles') rev-parse 'HEAD^{tree}').Trim()

Write-EasyStep 'Replay patches/ on upstream Cycles and compare with the engine'
& $python -I (Join-Path $PSScriptRoot 'verify-upstream-patches.py') --source-root $root --output (Join-Path $OutputDirectory 'patch-replay') *> (Join-Path $OutputDirectory 'patch-replay.log')
if ($LASTEXITCODE -ne 0) { Stop-Easy 'The engine differs from patches/ (see patch-replay.log).' ((Get-EasyAgain 'I changed the engine: update the patch files') + ', then "Make the installers again".') }
Add-Check 'patch-replay' 'passed' 'scripts/verify-upstream-patches.py'

$tests = Join-Path $OutputDirectory 'quick-tests.json'
if ($testSummary) {
    Set-Content -LiteralPath $tests -Value ($testSummary | ConvertTo-Json -Depth 6) -Encoding utf8
    $testsOk = $true
} else {
    $testsOk = Invoke-EasyScript 'easy-quick-tests.ps1' @('-BuildDirectory', $build, '-ResultFile', $tests, '-Nested')
}
if (Test-Path -LiteralPath $tests) { foreach ($c in (Get-Content -LiteralPath $tests -Raw | ConvertFrom-Json).checks) { Add-Check $c.name $c.result ([string]$c.reason) } }
if (-not $testsOk) { Stop-Easy 'A quick test failed (above). No installer was made.' 'Fix the problem, then "Make the installers again".' }

# --- Package --------------------------------------------------------------------------
Write-EasyStep 'Package the build'
# Filled entry by entry: this large [ordered]@{...} literal failed in PowerShell 7 with
# "Argument types do not match" (seen on the development machine).
$record = [ordered]@{}
$record['schema_version'] = 1
$record['official'] = $false
$record['route'] = 'community (scripts/easy-make-installer.ps1)'
$record['engine_route'] = $route
$record['engine_tree'] = $engineTree
$record['source_commit'] = $commit
$record['source_is_git_checkout'] = $isCheckout
$record['local_changes'] = $null
$record['local_change_count'] = $null
if ($isCheckout) { $record['local_changes'] = ($changes.Count -gt 0); $record['local_change_count'] = $changes.Count }
$record['houdini'] = $houdini
$identityFile = Get-Content -LiteralPath (Join-Path $root 'build-identity.json') -Raw | ConvertFrom-Json
$identityFile.PSObject.Properties.Remove('about')
$record['build_identity_file'] = $identityFile
$record['dlss'] = $(if ($hasDlss) { $identity.dlss } else { 'off' })
$record['dlss_project_id'] = $dlssProject
$record['built_utc'] = [DateTime]::UtcNow.ToString('o')
$record['checks'] = $checks.ToArray()
$notRun = New-Object System.Collections.Generic.List[object]
$notRun.Add([ordered]@{ name = 'official release qualification (scripts/build-release.ps1)'; reason = 'official releases only: installed-runtime, GUI, rollback, farm and existing-scene checks need the private engine route' })
$notRun.Add([ordered]@{ name = 'code signing'; reason = 'community installers are unsigned' })
$record['not_run'] = $notRun.ToArray()
$recordFile = Join-Path $OutputDirectory 'build-record.json'
Set-Content -LiteralPath $recordFile -Value ($record | ConvertTo-Json -Depth 6) -Encoding utf8
$zip = Join-Path $OutputDirectory ('cycles-houdini-' + $identity.build_id + '.zip')
& $houdiniPython -I (Join-Path $PSScriptRoot 'release-package.py') build --build-root $build --source-root $root --report (Join-Path $OutputDirectory 'package-build.json') --output $zip --build-identity (Join-Path $root 'build-identity.json') --build-record $recordFile *> (Join-Path $OutputDirectory 'package-build.log')
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $zip)) { Stop-Easy 'Packaging failed (see package-build.log and package-build.json).' 'The reason is in package-build.json; if it is unclear, open an issue on the project page on GitHub with that file.' }
$package = Get-Content -LiteralPath (Join-Path $OutputDirectory 'package-build.json') -Raw | ConvertFrom-Json
& $houdiniPython -I (Join-Path $PSScriptRoot 'release-package.py') verify $zip *> (Join-Path $OutputDirectory 'package-verify.log')
if ($LASTEXITCODE -ne 0) { Stop-Easy 'The new package did not verify (see package-verify.log).' 'Choose "Make the installers again"; if it fails again, open an issue on the project page on GitHub with the lines above.' }
Write-EasyOk "Package: $zip"
if ($package.gates.notices -ne 'passed') { Write-EasyInfo "Licence notice check: $($package.gates.notices) (before you give the installer to someone else: docs/dev/community-builds.md, 'What you may share')." }

$summary = [ordered]@{}
$summary['result'] = 'running'
$summary['houdini'] = $houdini
$summary['output'] = $OutputDirectory
$summary['package'] = $zip
$summary['package_sha256'] = (Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash.ToLowerInvariant()
$summary['package_gates'] = $package.gates
$summary['build_identity'] = $identity
$summary['build_record'] = $record
$summary['installer'] = $null
$summary['setup'] = $null
function Save-Summary {
    $text = $summary | ConvertTo-Json -Depth 8
    Set-Content -LiteralPath (Join-Path $OutputDirectory 'community-build-report.json') -Value $text -Encoding utf8
    if ($ResultFile) { Set-Content -LiteralPath $ResultFile -Value $text -Encoding utf8 }
}
if ($StopBeforeInstaller) {
    $summary.result = 'package-only'
    Save-Summary
    Write-EasyResult $true "Package made and verified (no installer, as asked): $zip" 'Run this step without -StopBeforeInstaller to compile the Setup.'
    exit 0
}

# --- Installer ------------------------------------------------------------------------
Write-EasyStep 'Compile the installer (Inno Setup)'
if (-not (Invoke-EasyScript 'build-installer.ps1' @('-Package', $zip, '-OutputDir', (Join-Path $OutputDirectory 'installer'), '-Iscc', $iscc, '-HoudiniRoot', (Get-EasyHoudiniRoot)))) { Stop-Easy 'Compiling the installer failed (message above).' 'Search for the message, or open an issue on the project page on GitHub with the lines above.' }
$setupReport = Get-ChildItem -LiteralPath (Join-Path $OutputDirectory 'installer') -Filter '*-Setup.json' | Select-Object -First 1
$summary.installer = Get-Content -LiteralPath $setupReport.FullName -Raw | ConvertFrom-Json
# Into the installer folder, named after the folder Setup installs to. The file is moved (renamed),
# not changed: its SHA-256 stays the one in the installer report.
$setup = Join-Path $InstallerFolder (Get-EasySetupFileName ([string]$summary.installer.default_directory))
New-Item -ItemType Directory -Path $InstallerFolder -Force | Out-Null
Move-Item -LiteralPath ([string]$summary.installer.setup) -Destination $setup -Force
if ((Get-FileHash -LiteralPath $setup -Algorithm SHA256).Hash.ToLowerInvariant() -ne $summary.installer.setup_sha256) { Stop-Easy "The Setup in $setup differs from the one Inno Setup made." 'Choose "Make the installers again".' }
$summary.setup = $setup
# What the files in the installer folder are, in plain words.
$readMe = @(
    'Cycles for Houdini: your installers',
    '',
    'Each "... - Setup.exe" file here installs Cycles for Houdini into one Houdini build: the one in its name',
    '(for example "Cycles for Houdini 22.0.508 (...) - Setup.exe" is for Houdini 22.0.508).',
    '',
    'Install: double-click the Setup for your Houdini build and follow it. The files are not signed, so Windows may',
    'show "Windows protected your PC": choose "More info", then "Run anyway".',
    'Uninstall: Windows Settings > Apps > Installed apps, "Cycles for Houdini - community build ...", Uninstall.',
    'A newer Setup of the same build for the same Houdini build updates the one installed before.',
    '',
    'They were made by "START HERE - Build Cycles for Houdini.cmd" in the project folder.',
    'The uninstaller keeps renderer versions and your data in the installed Cycles folder.',
    'See the build guide before removing files or deleting the project folder.',
    'Before you give a Setup to someone else, read docs\user\build-it-yourself.md, Part 2.')
Set-Content -LiteralPath (Join-Path $InstallerFolder 'READ ME.txt') -Value $readMe -Encoding utf8
$summary.result = 'passed'
Save-Summary
# Only the newest details folder of this Houdini build is kept (older ones were made by this step).
foreach ($old in @(Get-ChildItem -LiteralPath $detailsRoot -Directory -ErrorAction SilentlyContinue | Where-Object { $_.Name -match ('^' + [regex]::Escape($houdini) + '-\d{8}T\d{6}$') -and $_.FullName -ne $OutputDirectory })) {
    Remove-Item -LiteralPath $old.FullName -Recurse -Force -ErrorAction SilentlyContinue
}
Write-EasyResult $true "Installer for Houdini ${houdini}: $setup" 'Double-click it to install Cycles into that Houdini build; uninstall it in Windows Settings > Apps > Installed apps.'
exit 0
