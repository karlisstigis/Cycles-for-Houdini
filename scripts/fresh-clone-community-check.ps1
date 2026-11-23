# SPDX-License-Identifier: Apache-2.0
# Proves the newcomer path from a fresh clone, in an empty folder outside the repository: the one
# double-click file "START HERE - Build Cycles for Houdini.cmd" (scripts/easy-start.ps1), run
# without questions. Sibling of fresh-clone-check.ps1 (the plain bootstrap/build/render path).
#   1. clones one commit into <Destination>\<-CloneFolder> (no artifacts\, no third_party\);
#   2. the computer check in check-only mode, in Windows PowerShell 5.1 (as on a new computer);
#   3. the first run on a "new computer" (every program hidden, answers "no"), through the .cmd
#      file: it must stop at the computer check with NOT DONE / NEXT, build nothing, not crash;
#   4. the first run again with the programs there, started in Windows PowerShell 5.1: computer
#      check there, then PowerShell 7: public engine route, build with build-identity.json, quick
#      tests CPU and OptiX, and an installer (Setup.exe, built, never run) for every built Houdini
#      build in <clone>\Installers, each named for its Houdini build, with only the one allowed
#      answer, nothing written to the (redirected) Houdini preferences and nothing created or changed
#      outside the project folder (the rest of -Destination and %LOCALAPPDATA%\CyclesForHoudiniTools are
#      compared before and after); made-up folders of an earlier version next to the clone are named once.
#      With -CudaParts NVIDIA's installed toolkit is ignored (CYCLES_EASY_NO_INSTALLED_CUDA=1): the CUDA
#      compiler parts are fetched once into <Destination>\earlier-tools (standing for an earlier version's
#      tools folder), the first run must copy them into <clone>\_work\cuda-<version>, and the build must use
#      them and nothing from Program Files\NVIDIA GPU Computing Toolkit;
#   5. a later run without -Action shows the menu; a run while another holds the lock is refused;
#   6. each menu item through -Action: CheckComputer, QuickTests (through the .cmd file), Update,
#      Installer ("Make the installers again"). Each package is installed only into private folders
#      inside -Destination, the way Setup calls the install tool (deferred install, then commit-setup;
#      also abort and recover), a newer package of the same build over it, and an install next to
#      another Cycles for the same Houdini build must be refused. OpenHoudini is not run: it would
#      open a window on the screen;
#   7. the engine edit loop: changes one engine source file, EnginePatches, Update (rebuild and
#      quick tests), and checks that a second fresh clone of the result replays the same engine
#      commit (bootstrap in <Destination>\replay);
#   8. verifies unchanged preferences after checking the computer again;
#   writes <Destination>\result.json (times, disk use, every step) and -ResultFile.
# -Houdini (missing, all, newest or one build such as 22.0.508; default all): which installed
# Houdini 22.0 builds the first run builds for, each into its own build folder.
# -Zip: instead of a clone, the folder a GitHub "Download ZIP" gives (git archive of -Revision,
# unpacked, no .git): no engine edit loop; "Update the sources" must say what to do instead.
# -NoNvidia: act as on a computer without an NVIDIA graphics card (CYCLES_EASY_NO_NVIDIA=1, no
# CUDA compiler): the build must be CPU-only and NVIDIA's DLSS headers must not be downloaded.
# -DlssOff: turn DLSS off (menu item "Turn DLSS off") before the first run.
# Answers tool prompts with CYCLES_EASY_ANSWER=yes and sets separate NVIDIA consent according to -NoNvidia.
# Installs no programs
# (CYCLES_EASY_INSTALL_ONLY=cuda: only the CUDA compiler parts, and only into -Destination with
# -CudaParts). Changes nothing outside -Destination (and -ResultFile). The clone gets a local Git
# identity, never yours.
[CmdletBinding()]
param(
    # Default: a new folder in <project>\_work\tests (everything stays inside the project folder).
    [string]$Destination = '',
    [string]$Source = (Split-Path -Parent $PSScriptRoot),
    [string]$Revision = 'HEAD',
    [string]$ResultFile = '',
    [int]$Jobs = 6,
    [switch]$SkipOptix,
    [switch]$SkipEngineEdit,
    [switch]$CudaParts,
    # Name of the clone's folder inside -Destination; may contain spaces (for example "my build").
    [string]$CloneFolder = 'src',
    [string]$Houdini = 'all',
    # Also the GPU kernel reuse checks (scripts/kernel-cache.py): reuse off (the reference compile), a comment in
    # a kernel header, a real kernel change, a damaged stored copy. Needs an NVIDIA card; about 45 minutes more.
    [switch]$KernelChecks,
    [switch]$Zip,
    [switch]$NoNvidia,
    [switch]$DlssOff,
    # Only the Setup-style rounds with the packages of an earlier run (its result file); no clone, no build.
    [string]$RoundsOnly = '',
    # Dry mode: no clone, no build; the judgements of the kernel checks run on the states recorded by an earlier
    # run (its result file), so the judge calls, JSON shapes and paths are exercised in seconds.
    [string]$DryJudges = '',
    # With -DryJudges: build folders whose kernel state is read (read only), to exercise that code too; separated
    # by ";" (one argument also through -File).
    [string]$DryBuildFolders = '',
    # With -DryJudges: a _work folder whose kernel-cache is read (read only): the stored-copy lookup and the counts.
    [string]$DryWork = ''
)
$ErrorActionPreference = 'Stop'
# Windows PowerShell 5.1 writes arrays that came from ConvertFrom-Json as {"value": [...], "Count": n} in
# ConvertTo-Json (an extended type of System.Array); without it they are plain arrays, as in PowerShell 7.
if ($PSVersionTable.PSVersion.Major -lt 6) { Remove-TypeData -TypeName System.Array -ErrorAction SilentlyContinue }
$pwsh = (Get-Process -Id $PID).Path
$Source = [IO.Path]::GetFullPath($Source).TrimEnd('\', '/')
# Test folders live inside the project folder, under _work\tests (ignored by Git and never exported); by default a
# new one named after the time. Anywhere else inside the source repository is refused.
if (-not $Destination) { $Destination = Join-Path $Source ('_work\tests\community-' + [DateTime]::UtcNow.ToString('yyyyMMddTHHmmss')) }
$Destination = [IO.Path]::GetFullPath($Destination).TrimEnd('\', '/')
$testsRoot = Join-Path $Source '_work\tests'
if (($Destination + '\').StartsWith($Source + '\', [StringComparison]::OrdinalIgnoreCase) -and -not ($Destination + '\').StartsWith($testsRoot + '\', [StringComparison]::OrdinalIgnoreCase)) { throw "Destination must be outside the source repository, or inside its _work\tests folder ($testsRoot)." }
if (@($Destination -split '[\\/]') -contains 'artifacts') { throw 'Destination must not be inside any artifacts folder.' }
if ((Test-Path -LiteralPath $Destination) -and @(Get-ChildItem -LiteralPath $Destination -Force).Count) { throw "Destination must be empty: $Destination" }
# Steps are judged by scripts/fresh-clone-judge.py from the recorded states (the same judges are unit-tested
# against the records of earlier runs); this file does the steps and records them.
function Invoke-Judge([string]$Name, $Arguments, [string]$What) {
    $file = Join-Path $Destination ('judge-' + [guid]::NewGuid().ToString('N').Substring(0, 8) + '.json')
    [IO.File]::WriteAllText($file, ($Arguments | ConvertTo-Json -Depth 40), [Text.UTF8Encoding]::new($false))
    $errors = $file + '.err'
    $text = & python -I (Join-Path $judgeRoot 'scripts\fresh-clone-judge.py') $Name $file 2> $errors
    $code = $LASTEXITCODE
    $problem = if (Test-Path -LiteralPath $errors) { (Get-Content -LiteralPath $errors -Raw) } else { '' }
    if ($code) { throw "${What}: the judge $Name could not run ($code): $text $problem (its input: $file)" }
    Remove-Item -LiteralPath $file, $errors -Force -ErrorAction SilentlyContinue
    $verdict = ($text -join "`n") | ConvertFrom-Json
    if (-not $verdict.ok) { throw "${What}: $($verdict.reason)" }
}
# The long-root helper of the judge: the folder whose longest path is exactly a given total.
function Get-JudgedLongRoot([string]$Base, [int]$BelowRoot, [int]$Total) {
    $file = Join-Path $Destination ('judge-' + [guid]::NewGuid().ToString('N').Substring(0, 8) + '.json')
    [IO.File]::WriteAllText($file, (@{ base = $Base; below_root = $BelowRoot; total = $Total } | ConvertTo-Json), [Text.UTF8Encoding]::new($false))
    $text = & python -I (Join-Path $judgeRoot 'scripts\fresh-clone-judge.py') 'long-root' $file
    $code = $LASTEXITCODE
    Remove-Item -LiteralPath $file -Force
    if ($code) { throw "The long-root helper could not run ($code): $text" }
    return (($text -join "`n") | ConvertFrom-Json).root
}
# GPU kernel reuse (scripts/kernel-cache.py): what each build did with its kernels (last-build.json) and the
# SHA-256 of its 12 kernel files (cubin/ptx and their .zst), compared between the builds.
function Get-KernelState([string]$Folder) {
    $summaryFile = Join-Path $Folder 'kernel-reuse\last-build.json'
    $summary = if (Test-Path -LiteralPath $summaryFile) { Get-Content -LiteralPath $summaryFile -Raw | ConvertFrom-Json } else { $null }
    $files = [ordered]@{}
    foreach ($kind in 'cuda', 'optix') {
        foreach ($file in @(Get-ChildItem -LiteralPath (Join-Path $Folder "cycles\src\kernel\device\$kind") -File -ErrorAction SilentlyContinue | Where-Object { $_.Name -match '\.(cubin|ptx)(\.zst)?$' } | Sort-Object Name)) {
            # .NET directly, not Get-FileHash: a Windows PowerShell 5.1 started from PowerShell 7 may not find that
            # module.
            $sha = [Security.Cryptography.SHA256]::Create(); $stream = [IO.File]::OpenRead($file.FullName)
            try { $files["$kind/$($file.Name)"] = (-join ($sha.ComputeHash($stream) | ForEach-Object { $_.ToString('x2') })) } finally { $stream.Dispose(); $sha.Dispose() }
        }
    }
    $ninja = Join-Path $Folder 'build.ninja'
    return [ordered]@{ state = $(if ($summary) { $summary.state } else { $null }); mode = $(if ($summary) { $summary.mode } else { $null })
        reused = @($(if ($summary) { $summary.reused } else { @() })); compiled = @($(if ($summary) { $summary.compiled } else { @() }))
        damaged_ignored = $(if ($summary) { [bool]$summary.damaged_ignored } else { $false }); kernel_seconds = $(if ($summary) { $summary.kernel_seconds } else { $null })
        line = $(if ($summary) { $summary.line } else { $null }); decisions = @($(if ($summary) { $summary.decisions } else { @() }))
        wrapper_in_build_ninja = [bool](Select-String -LiteralPath $ninja -Pattern 'kernel-reuse[\\/]+nvcc\.cmd' -List -ErrorAction SilentlyContinue); files = $files }
}
# The stored kernel copies in <_work>\kernel-cache (entries being written start with "."), the stored copy of one
# kernel for the key a build's decision names, and the number of copies per kernel. Used by the kernel steps and
# by the dry mode alike.
function Get-KernelCacheEntries([string]$Work) {
    $cache = Join-Path $Work 'kernel-cache'
    @(Get-ChildItem -LiteralPath $cache -Directory -ErrorAction SilentlyContinue | Where-Object { -not $_.Name.StartsWith('.') } | ForEach-Object {
        $m = Get-Content -LiteralPath (Join-Path $_.FullName 'manifest.json') -Raw | ConvertFrom-Json
        [pscustomobject]@{ Folder = $_.FullName; Output = $m.output; Key = $m.key; Sha256 = $m.sha256; Bytes = $m.bytes } })
}
function Find-StoredKernelCopy($Decisions, [string]$Name, [string]$Work) {
    $key = @(@($Decisions) | Where-Object { $_.output -eq $Name })[0].key
    $entry = @(Get-KernelCacheEntries $Work | Where-Object { $_.Key -eq $key })[0]
    $file = if ($entry) { Join-Path $entry.Folder $Name } else { $null }
    [pscustomobject]@{ Key = $key; Entry = $entry; File = $file; Exists = [bool]($file -and (Test-Path -LiteralPath $file)) }
}
function Get-KernelCacheCounts([string]$Work) {
    $counts = [ordered]@{}
    foreach ($group in @(Get-KernelCacheEntries $Work | ForEach-Object { $_.Output } | Group-Object)) { $counts[$group.Name] = $group.Count }
    return $counts  # a dictionary is passed on whole, never enumerated
}
if ($DryJudges) {
    New-Item -ItemType Directory -Path $Destination -Force | Out-Null
    $judgeRoot = $Source
    # ConvertFrom-Json gives objects (Windows PowerShell 5.1 has no -AsHashtable): names via PSObject.Properties.
    $recorded = Get-Content -LiteralPath $DryJudges -Raw | ConvertFrom-Json
    function Get-Names($Object) { if ($null -eq $Object) { @() } else { @($Object.PSObject.Properties | ForEach-Object { $_.Name }) } }
    $k = $recorded.kernels
    $verdicts = [ordered]@{}
    function Get-Verdict([string]$Label, [string]$Name, $Arguments) {
        try { Invoke-Judge $Name $Arguments $Label; $verdicts[$Label] = 'passed' } catch { $verdicts[$Label] = 'failed: ' + $_.Exception.Message }
    }
    $versions = @($recorded.versions | Where-Object { $_ })
    $recordedVersions = @(Get-Names $k.first_run)
    if (-not $versions.Count -or $versions.Count -ne $recordedVersions.Count -or
        @($versions | Select-Object -Unique).Count -ne $versions.Count -or
        @($versions | Where-Object { $_ -notmatch '^\d+\.\d+\.\d+$' -or $_ -notin $recordedVersions }).Count) {
        $verdicts['input'] = 'failed: expected an ordered versions list matching kernels.first_run'
    } else {
        $newest = $versions[0]
        Get-Verdict 'first-run' 'first-compile' @{ state = $k.first_run.$newest }
        foreach ($v in @($versions | Select-Object -Skip 1)) { Get-Verdict "first-run $v" 'reuse-all' @{ state = $k.first_run.$v; reference = $k.first_run.$newest } }
        foreach ($when in 'update_unchanged', 'update_after_engine_edit_outside_the_kernels') {
            foreach ($v in (Get-Names $k.$when)) { Get-Verdict "$when $v" 'kept' @{ state = $k.$when.$v; reference = $k.first_run.$v } }
        }
        $c = $k.checks
        if ($c.reuse_off) { Get-Verdict 'reuse-off' 'reference-compile' @{ state = $c.reuse_off; references = $k.first_run } }
        if ($c.reuse_on_again) { Get-Verdict 'reuse-on-again' 'reuse-all' @{ state = $c.reuse_on_again; reference = $c.reuse_off } }
        if ($c.comment_edit) { Get-Verdict 'comment-edit' 'new-key-compiles-all' @{ state = $c.comment_edit } }
        if ($c.real_edit) { Get-Verdict 'real-edit' 'real-edit' @{ states = $c.real_edit; newest = $newest; before = $c.comment_edit } }
        if ($c.damaged) { Get-Verdict 'damaged' 'damaged' @{ state = $c.damaged; name = 'kernel_compute_75.ptx'; reference = $c.real_edit.$newest } }
        if ($k.cache_entries) { $counts = [ordered]@{}; foreach ($g in @($k.cache_entries)) { $counts[$g.kernel] = $g.entries }; Get-Verdict 'cache-entries' 'cache-entries' @{ counts = $counts } }
    }
    # Live-read states go through the judges exactly as in a run: 'kept' against itself, and 'reuse-all' against
    # itself when the folder's last build reused all six.
    $read = [ordered]@{}
    $live = @()
    foreach ($folder in @($DryBuildFolders -split ';' | Where-Object { $_ })) {
        $s = Get-KernelState $folder
        $live += ,$s
        $read[$folder] = [ordered]@{ state = $s.state; mode = $s.mode; reused = $s.reused.Count; compiled = $s.compiled.Count; decisions = $s.decisions.Count
            files = $s.files.Count; wrapper_in_build_ninja = $s.wrapper_in_build_ninja; line = $s.line }
        Get-Verdict "live kept $folder" 'kept' @{ state = $s; reference = $s }
        if ($s.reused.Count -eq 6) { Get-Verdict "live reuse-all $folder" 'reuse-all' @{ state = $s; reference = $s } }
    }
    # -DryWork: the stored-copy lookup of the damaged step and the counting of the stored-copies step, read only.
    $work = [ordered]@{}
    if ($DryWork) {
        $work.entries = @(Get-KernelCacheEntries $DryWork).Count
        if ($live.Count) {
            $copy = Find-StoredKernelCopy $live[0].decisions 'kernel_compute_75.ptx' $DryWork
            $work.lookup = [ordered]@{ key = $copy.Key; file = $copy.File; exists = $copy.Exists }
            $verdicts['live stored copy of kernel_compute_75.ptx'] = $(if ($copy.Exists) { 'passed' } else { "failed: no stored copy for key $($copy.Key)" })
        }
        $counts = Get-KernelCacheCounts $DryWork
        $work.counts = $counts
        Get-Verdict 'live cache-entries' 'cache-entries' @{ counts = $counts }
    }
    $out = [ordered]@{ dry_judges = $DryJudges; powershell = $PSVersionTable.PSVersion.ToString(); verdicts = $verdicts; read_build_folders = $read; work = $work }
    if ($ResultFile) { Set-Content -LiteralPath $ResultFile -Value ($out | ConvertTo-Json -Depth 8) -Encoding utf8 }
    $out | ConvertTo-Json -Depth 8
    exit $(if (@($verdicts.Values | Where-Object { $_ -ne 'passed' }).Count) { 1 } else { 0 })
}
# Free space on the destination drive, measured from earlier runs of this check: about 2.3 GB for one Houdini build
# from a ZIP (CPU-only), about 6.6 GB for three Houdini builds before installers were part of the run; with the
# installers about 2 GB plus 1.7 GB per Houdini build, and 2 GB spare. Refuse to start when it is not there.
$houdiniCount = 1
if ($Houdini -eq 'all') { $houdiniCount = [math]::Max(1, @(Get-ChildItem -LiteralPath (Join-Path $env:ProgramFiles 'Side Effects Software') -Directory -Filter 'Houdini 22.0.*' -ErrorAction SilentlyContinue | Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName 'toolkit\include\SYS\SYS_Version.h') }).Count) }
$needGB = [math]::Round(2 + 1.7 * $houdiniCount + 2, 1)
$freeGB = [math]::Round((New-Object IO.DriveInfo([IO.Path]::GetPathRoot($Destination))).AvailableFreeSpace / 1GB, 1)
if ($freeGB -lt $needGB) { throw "Not enough free disk space for this check: $([IO.Path]::GetPathRoot($Destination)) has $freeGB GB free, and a run for $houdiniCount Houdini build(s) needs about $needGB GB." }
$src = Join-Path $Destination $CloneFolder
$judgeRoot = $src
# Everything the one file makes is inside the project folder: builds and the rest in _work, installers in Installers.
$buildRoot = Join-Path $src '_work'
$replay = Join-Path $Destination 'replay'
New-Item -ItemType Directory -Path $Destination -Force | Out-Null
$installers = Join-Path $src 'Installers'
$installerDetails = Join-Path $buildRoot 'installer-details'
# The folder an earlier version of the tool used for its CUDA compiler parts (here a test folder, see -CudaParts).
$earlierTools = Join-Path $Destination 'earlier-tools'
$result = [ordered]@{ result = 'running'; started_utc = [DateTime]::UtcNow.ToString('o'); destination = $Destination; source = $Source
    revision = $null; houdini_choice = $Houdini; zip = [bool]$Zip; no_nvidia = [bool]$NoNvidia; dlss_off = [bool]$DlssOff; steps = [ordered]@{} }
function Save-Result {
    $text = $result | ConvertTo-Json -Depth 8
    Set-Content -LiteralPath (Join-Path $Destination 'result.json') -Value $text -Encoding utf8
    if ($ResultFile) { Set-Content -LiteralPath $ResultFile -Value $text -Encoding utf8 }
}
function Invoke-Step([string]$Name, [scriptblock]$Body) {
    $watch = [Diagnostics.Stopwatch]::StartNew()
    Write-Host "=== $Name"
    $record = [ordered]@{ ok = $false; seconds = 0; error = $null }
    $result.steps[$Name] = $record
    try { & $Body; $record.ok = $true }
    catch { $record.error = $_.Exception.Message; throw }
    finally { $record.seconds = [math]::Round($watch.Elapsed.TotalSeconds, 1); Save-Result }
}
# The Setup-style rounds (also run alone with -RoundsOnly): for every Houdini build in $result.builds, with the
# packages in $result.installers_after_update and $result.package, the install tool of $src.
function Invoke-SetupRounds {
    # The package's own install tool, called the way Setup calls it (CyclesHoudini.iss): a
    # registration preflight, an install with --defer-commit and a Setup token, then
    # commit-setup (or abort-setup when Setup fails). Everything goes into private folders
    # inside -Destination (--isolated): no Windows application entry, no real Houdini
    # preferences. For every built Houdini build: abort, an interrupted Setup that is recovered,
    # a committed install, a newer package of the same build installed over it (an update), an
    # uninstall; and an install next to another Cycles package for the same Houdini build (here a
    # made-up official one) that must be refused.
    $result.isolated_install = [ordered]@{}
    foreach ($houdini in @($result.builds.Keys)) {
        $houdiniPython = Join-Path $env:ProgramFiles ('Side Effects Software\Houdini ' + $houdini + '\python313\python.exe')
        $iso = Join-Path $Destination ('isolated-install\' + $houdini)
        $root = Join-Path $iso 'app\installation'
        $prefs = Join-Path $iso 'prefs'
        $tool = Join-Path $src 'scripts\release-package.py'
        # Two installers of the same build for this Houdini build: the one "Update" made, then the newer one of
        # "Make the installers again".
        $first = $result.installers_after_update[$houdini]
        $newer = $result.package[$houdini]
        function Invoke-Tool([string[]]$Arguments, [switch]$MayFail) {
            $text = & $houdiniPython -I $tool @Arguments 2>&1 | ForEach-Object { [string]$_ }
            if ($LASTEXITCODE -and -not $MayFail) { throw "release-package.py $($Arguments[0]) failed ($LASTEXITCODE): $text" }
            return [pscustomobject]@{ Code = $LASTEXITCODE; Text = ($text -join "`n") }
        }
        function Install-Deferred($Package, [string]$Token, [string]$IntoPrefs = '', [string]$IntoRoot = '', [switch]$MayFail) {
            if (-not $IntoPrefs) { $IntoPrefs = $prefs }
            if (-not $IntoRoot) { $IntoRoot = $root }
            $sha = (Get-FileHash -LiteralPath $Package -Algorithm SHA256).Hash.ToLowerInvariant()
            Invoke-Tool @('install', $Package, '--install-root', $IntoRoot, '--preference-root', $IntoPrefs, '--data-root', (Join-Path (Split-Path -Parent $IntoRoot) 'data'),
                '--allow-internal', '--isolated', '--expected-sha256', $sha, '--defer-commit', '--setup-token', $Token) -MayFail:$MayFail
        }
        $identity = (Invoke-Tool @('identity', '--houdini', $houdini, '--archive', $first.package)).Text | ConvertFrom-Json
        $registration = Join-Path $prefs ('packages\' + $identity.package_filename)
        $officialFile = Join-Path $prefs ('packages\cycles_houdini_' + $houdini.Replace('.', '_') + '.json')
        $rounds = [ordered]@{}
        New-Item -ItemType Directory -Path $prefs -Force | Out-Null
        $preflight = (Invoke-Tool @('registration-status', '--houdini', $houdini, '--install-root', $root, '--preference-root', $prefs,
            '--data-root', (Join-Path $iso 'app\data'), '--archive', $first.package, '--build-id', $identity.build_id)).Text | ConvertFrom-Json
        $rounds.preflight = $preflight.ownership
        # Setup fails after the install step and runs abort-setup.
        [void](Install-Deferred $first.package 'check-abort')
        if (-not (Test-Path -LiteralPath $registration)) { throw "The community package file $($identity.package_filename) was not written" }
        [void](Invoke-Tool @('abort-setup', '--install-root', $root, '--setup-token', 'check-abort'))
        if (Test-Path -LiteralPath $registration) { throw 'abort-setup left the community package file behind' }
        $rounds.abort = 'passed'
        # Setup was interrupted (no commit, no abort); the next run recovers.
        [void](Install-Deferred $first.package 'check-interrupted')
        [void](Invoke-Tool @('recover', '--install-root', $root))
        if (Test-Path -LiteralPath $registration) { throw 'recover left the community package file behind' }
        $rounds.recover = 'passed'
        # A Setup that finishes.
        [void](Install-Deferred $first.package 'check-commit')
        [void](Invoke-Tool @('commit-setup', '--install-root', $root, '--setup-token', 'check-commit'))
        if (-not (Test-Path -LiteralPath $registration)) { throw 'commit-setup removed the community package file' }
        if (Test-Path -LiteralPath $officialFile) { throw 'The official package file name was used' }
        $before = (Get-Content -LiteralPath (Join-Path $root 'state.json') -Raw | ConvertFrom-Json)
        $rounds.commit = 'passed'
        # The newer installer of the same build for the same Houdini build: an update of that installation.
        [void](Install-Deferred $newer.package 'check-update')
        [void](Invoke-Tool @('commit-setup', '--install-root', $root, '--setup-token', 'check-update'))
        $after = (Get-Content -LiteralPath (Join-Path $root 'state.json') -Raw | ConvertFrom-Json)
        $packages = @(Get-ChildItem -LiteralPath (Join-Path $prefs 'packages') -File -Filter 'cycles_houdini*.json' | ForEach-Object { $_.Name })
        if (($before | ConvertTo-Json -Depth 20 -Compress) -eq ($after | ConvertTo-Json -Depth 20 -Compress)) { throw 'Installing the newer package changed nothing' }
        if ($packages.Count -ne 1 -or $packages[0] -ne $identity.package_filename) { throw "After the update the preferences hold: $($packages -join ', ')" }
        $rounds.update = [ordered]@{ result = 'passed'; package_files = $packages }
        $pending = @(Get-ChildItem -LiteralPath (Join-Path $root 'transactions') -Filter '*.json' | Where-Object {
            (Get-Content -LiteralPath $_.FullName -Raw | ConvertFrom-Json).state -notin 'committed', 'rolled_back' })
        if ($pending.Count) { throw "commit-setup left $($pending.Count) unfinished transaction(s)" }
        [void](Invoke-Tool @('refresh-registration', '--install-root', $root))
        $rounds.refresh = 'passed'
        $state = Get-Content -LiteralPath (Join-Path $root 'state.json') -Raw | ConvertFrom-Json
        [void](Invoke-Tool @('uninstall', '--install-root', $root))
        if (Test-Path -LiteralPath $registration) { throw 'Uninstall left the community package file behind' }
        $rounds.uninstall = 'passed'
        # Another Cycles for the same Houdini build already registered (a made-up official package file):
        # the install must be refused, so that one Houdini never loads two Cycles.
        $otherPrefs = Join-Path $iso 'prefs-with-official'
        New-Item -ItemType Directory -Path (Join-Path $otherPrefs 'packages') -Force | Out-Null
        $fake = [ordered]@{ enable = "houdini_version == '$houdini'"; env = @(@{ CYCLES_HOUDINI_RELEASE_ID = 'made-up-official-install' }) }
        Set-Content -LiteralPath (Join-Path $otherPrefs ('packages\cycles_houdini_' + $houdini.Replace('.', '_') + '.json')) -Value ($fake | ConvertTo-Json -Depth 4) -Encoding utf8
        $refused = Install-Deferred $first.package 'check-refused' $otherPrefs (Join-Path $iso 'app2\installation') -MayFail
        if (-not $refused.Code -or $refused.Text -notmatch 'Another Cycles package') { throw "An install next to another Cycles for Houdini $houdini was not refused ($($refused.Code)): $($refused.Text)" }
        if (Test-Path -LiteralPath (Join-Path $otherPrefs ('packages\' + $identity.package_filename))) { throw 'The refused install wrote its package file' }
        $rounds.refused_next_to_other_cycles = ($refused.Text -split "`n" | Where-Object { $_ -match 'Another Cycles package' } | Select-Object -First 1)
        # Installed path length (Task Y): the longest path in the package (and, for information, the longest
        # bytecode path Houdini's Python may write later; Python skips it when it does not fit) and the app
        # folder length at which Windows' 259 characters are reached. One
        # character longer must be refused before anything is written; exactly at the limit must install,
        # recover and uninstall.
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $zip = [IO.Compression.ZipFile]::OpenRead($first.package)
        try { $names = @($zip.Entries | Where-Object { $_.Name } | ForEach-Object { $_.FullName }) } finally { $zip.Dispose() }
        $bytecode = [string](@($names | Where-Object { $_ -like '*.py' } | ForEach-Object { $d = [IO.Path]::GetDirectoryName($_).Replace('\', '/'); ($(if ($d) { $d + '/' } else { '' }) + '__pycache__/' + [IO.Path]::GetFileNameWithoutExtension($_) + '.cpython-313.pyc') } | Sort-Object Length -Descending | Select-Object -First 1))
        $longestName = ($names | Sort-Object Length -Descending | Select-Object -First 1)
        $belowRoot = 'versions/'.Length + 16 + 1 + $longestName.Length   # below <app>\installation
        $paths = [ordered]@{ longest_in_package = $longestName; longest_in_package_length = $longestName.Length; below_app_folder = 'installation/'.Length + $belowRoot
            longest_bytecode_houdini_may_write = $bytecode; longest_bytecode_length = $(if ($bytecode) { $bytecode.Length } else { 0 }) }
        # Do and record; judged after the loop by scripts/fresh-clone-judge.py path-rounds. The folders come from
        # the judge's long-root helper (its own arithmetic, unit-tested against the install tool's guard).
        foreach ($case in @(@{ name = 'one_over_the_limit'; total = 260 }, @{ name = 'at_the_limit'; total = 259 })) {
            $base = Join-Path $iso ('long-' + $case.total)
            $longRoot = Get-JudgedLongRoot $base $belowRoot $case.total
            $longPrefs = Join-Path $base 'prefs'
            New-Item -ItemType Directory -Path $longPrefs -Force | Out-Null
            $attempt = Install-Deferred $first.package ('check-' + $case.total) $longPrefs $longRoot -MayFail
            $record = [ordered]@{ app_folder_length = (Split-Path -Parent $longRoot).Length; root_plus_below = $longRoot.Length + 1 + $belowRoot
                message = ($attempt.Text -split "`n" | Where-Object { $_ -match 'too long' } | Select-Object -First 1) }
            if ($attempt.Code) {
                $record.result = 'refused'
                $record.created_app_folder = (Test-Path -LiteralPath (Split-Path -Parent $longRoot))
                $record.wrote_preferences = [bool]@(Get-ChildItem -LiteralPath $longPrefs -Recurse -Force).Count
            } else {
                [void](Invoke-Tool @('recover', '--install-root', $longRoot))
                [void](Install-Deferred $first.package ('check-' + $case.total + 'b') $longPrefs $longRoot)
                [void](Invoke-Tool @('commit-setup', '--install-root', $longRoot, '--setup-token', ('check-' + $case.total + 'b')))
                $longest = (Get-ChildItem -LiteralPath $longRoot -Recurse -File -Force | Sort-Object { $_.FullName.Length } -Descending | Select-Object -First 1).FullName
                [void](Invoke-Tool @('uninstall', '--install-root', $longRoot))
                $record.result = 'installed, recovered, committed, uninstalled'
                $record.longest_installed = $longest.Length
                $record.longest_installed_path = $longest
            }
            $paths[$case.name] = $record
        }
        $rounds.path_length = $paths
        Invoke-Judge 'path-rounds' @{ record = $paths } "Installed path length, Houdini $houdini"
        $result.isolated_install[$houdini] = [ordered]@{ app_id = $identity.app_id; package_filename = $identity.package_filename; default_directory = $identity.default_directory
            display_name = $identity.display_name; state_build_id = $state.build_identity.build_id; rounds = $rounds }
    }
}
function Invoke-Easy([string]$Root, [string]$Script, [string[]]$Arguments = @(), [string]$Shell = $pwsh) {
    & $Shell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Root "scripts\$Script") @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Script failed ($LASTEXITCODE)" }
}
if ($RoundsOnly) {
    # Only the Setup-style rounds (no clone, no build): the packages of an earlier run of this check (its result
    # file) are installed in private folders under -Destination with the install tool of -Source.
    $src = $Source; $judgeRoot = $Source
    $earlier = Get-Content -LiteralPath $RoundsOnly -Raw | ConvertFrom-Json
    $result.rounds_only = $RoundsOnly
    $result.builds = [ordered]@{}; $result.installers_after_update = [ordered]@{}; $result.package = [ordered]@{}
    foreach ($v in @($earlier.isolated_install.PSObject.Properties | ForEach-Object { $_.Name })) {
        $result.builds[$v] = [ordered]@{ folder = $earlier.builds.$v.folder }
        # Two packages of the same build for this Houdini build, older first. The earlier run keeps only the newest
        # details folder per Houdini build in each place, so the recorded ones may be gone: then the two newest
        # packages left in its details folders (<Houdini build>-<time>\*.zip) are used.
        $found = @(Get-ChildItem -LiteralPath $earlier.destination -Recurse -File -Filter '*.zip' -ErrorAction SilentlyContinue |
            Where-Object { $_.Directory.Name -like "$v-*" -and $_.Directory.Parent.Name -in 'installer-details', 'details' } |
            Sort-Object { $_.Directory.Name.Substring($v.Length + 1) })
        if ($found.Count -lt 2) { throw "The earlier run left fewer than two packages for Houdini ${v}: $($found.FullName -join ', ')" }
        $result.installers_after_update[$v] = [ordered]@{ package = $found[-2].FullName }
        $result.package[$v] = [ordered]@{ package = $found[-1].FullName }
    }
    try { Invoke-Step 'setup-style-install-update-refuse-abort-recover-commit-uninstall' { Invoke-SetupRounds }; $result.result = 'passed' }
    catch { $result.result = 'failed'; $result.error = $_.Exception.Message }
    $result.finished_utc = [DateTime]::UtcNow.ToString('o'); Save-Result
    if ($result.result -ne 'passed') { exit 1 }
    exit 0
}
# The one-step file's script with -Action (what a menu choice does).
function Invoke-Start([string[]]$Arguments, [int[]]$Expect = @(0), [string]$Shell = $pwsh) {
    $text = & $Shell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $src 'scripts\easy-start.ps1') @Arguments 2>&1 | ForEach-Object { [string]$_ }
    $code = $LASTEXITCODE
    $text | Out-Host
    if ($Expect -notcontains $code) { throw "easy-start.ps1 $($Arguments -join ' ') gave exit code $code (expected $($Expect -join ' or '))" }
    if (-not ($text -match '^(DONE|NOT DONE): ')) { throw "easy-start.ps1 $($Arguments -join ' ') printed no DONE / NOT DONE line" }
    if ($text -match '^ERROR: ') { throw "easy-start.ps1 $($Arguments -join ' ') stopped with an unexpected error" }
    return [pscustomobject]@{ Code = $code; Text = $text }
}
# Sets or removes a variable of this process. [Environment]::SetEnvironmentVariable(name, $null) is not used to
# remove one: PowerShell passes $null as an empty string, which leaves the variable defined but empty (an empty
# HOUDINI_PATH made every later hython print "Can't open dophints.cmd").
function Set-ProcessVariable([string]$Name, $Value) {
    if ($null -eq $Value) { Remove-Item -LiteralPath ('Env:' + $Name) -ErrorAction SilentlyContinue }
    else { [Environment]::SetEnvironmentVariable($Name, [string]$Value, 'Process') }
}
# Names, sizes and times of everything under a folder, to show that a run created or changed nothing there.
function Get-Listing([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return @() }
    @(Get-ChildItem -LiteralPath $Path -Recurse -Force -ErrorAction SilentlyContinue | ForEach-Object { $_.FullName + '|' + $(if ($_.PSIsContainer) { 'dir' } else { [string]$_.Length + '|' + $_.LastWriteTimeUtc.Ticks }) } | Sort-Object)
}
# Entries in only one of two listings (either may be empty).
function Get-ListingChanges($Before, $After) {
    $old = New-Object 'System.Collections.Generic.HashSet[string]'; foreach ($x in @($Before)) { if ($null -ne $x) { [void]$old.Add([string]$x) } }
    $new = New-Object 'System.Collections.Generic.HashSet[string]'; foreach ($x in @($After)) { if ($null -ne $x) { [void]$new.Add([string]$x) } }
    @($new | Where-Object { -not $old.Contains($_) }) + @($old | Where-Object { -not $new.Contains($_) })
}
function Get-FolderGiB([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return 0 }
    [math]::Round(((Get-ChildItem -LiteralPath $Path -Recurse -File -Force -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum) / 1GB, 2)
}
function New-Clone([string]$From, [string]$Commit, [string]$Into) {
    & git init -q $Into; if ($LASTEXITCODE) { throw 'git init failed' }
    & git -C $Into fetch -q --depth 1 ('file:///' + $From.Replace('\', '/')) $Commit; if ($LASTEXITCODE) { throw 'git fetch failed' }
    & git -C $Into checkout -q -B main FETCH_HEAD; if ($LASTEXITCODE) { throw 'git checkout failed' }
    # A local identity for commits the tools make in this scratch clone.
    & git -C $Into config user.name 'Fresh clone check'; & git -C $Into config user.email 'fresh-clone-check@localhost'
}
$previousEnvironment = @{}
foreach ($name in 'CYCLES_EASY_ANSWER', 'CYCLES_EASY_NVIDIA_CONSENT', 'CYCLES_EASY_INSTALL_ONLY', 'CYCLES_EASY_NO_PAUSE', 'CYCLES_CUDA_ROOT', 'CYCLES_EASY_HOUDINI', 'CYCLES_EASY_HOUDINI_PREFS', 'CYCLES_EASY_NO_NVIDIA', 'HOUDINI_USER_PREF_DIR', 'HOUDINI_PACKAGE_DIR', 'CYCLES_EASY_TARGET_HOUDINI', 'CYCLES_EASY_TARGET_ROOT') { $previousEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process') }
$env:CYCLES_EASY_ANSWER = 'yes'
$env:CYCLES_EASY_NVIDIA_CONSENT = $(if ($NoNvidia) { 'no' } else { 'yes' })
$env:CYCLES_EASY_INSTALL_ONLY = 'cuda'
$env:CYCLES_EASY_NO_PAUSE = '1'
$env:CYCLES_EASY_HOUDINI = $Houdini
$env:CYCLES_EASY_TARGET_HOUDINI = $null; $env:CYCLES_EASY_TARGET_ROOT = $null; $env:HOUDINI_PACKAGE_DIR = $null; $env:HOUDINI_USER_PREF_DIR = $null
# The Houdini preferences the tools read and write: a folder inside -Destination, never yours.
$testPrefs = Join-Path $Destination 'houdini-prefs\houdini__HVER__'
$env:CYCLES_EASY_HOUDINI_PREFS = $testPrefs
# -CudaParts: NVIDIA's installed toolkit is ignored, so the builder uses the CUDA compiler parts in the project
# folder (_work\cuda-<version>). They are fetched once into a folder that stands for an earlier version's
# %LOCALAPPDATA%\CyclesForHoudiniTools (CYCLES_EASY_EARLIER_TOOLS); the first run must copy them into the project.
$cudaVersion = [regex]::Match((Get-Content -LiteralPath (Join-Path $Source 'scripts\cuda-toolkit-parts.py') -Raw), '(?m)^VERSION = "([0-9.]+)"').Groups[1].Value
$cudaPartsRoot = Join-Path $buildRoot ('cuda-' + $cudaVersion)
foreach ($name in 'CYCLES_EASY_NO_INSTALLED_CUDA', 'CYCLES_EASY_EARLIER_TOOLS') { $previousEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process') }
$env:CYCLES_EASY_EARLIER_TOOLS = $earlierTools
if ($CudaParts) { $env:CYCLES_EASY_NO_INSTALLED_CUDA = '1' }
if ($NoNvidia) {
    $env:CYCLES_EASY_NO_NVIDIA = '1'
    $env:CYCLES_CUDA_ROOT = Join-Path $Destination 'no-cuda'
    $env:CYCLES_EASY_INSTALL_ONLY = 'none'
}
$windowsPowerShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$startFile = Join-Path $src 'START HERE - Build Cycles for Houdini.cmd'
$testArgs = @(); if ($SkipOptix) { $testArgs += '-SkipOptix' }
try {
    Invoke-Step $(if ($Zip) { 'download-zip' } else { 'clone' }) {
        $sha = (& git -C $Source rev-parse --verify "$Revision^{commit}").Trim()
        if ($LASTEXITCODE -ne 0) { throw "Unknown revision $Revision" }
        $result.revision = $sha
        if ($Zip) {
            # What GitHub's "Download ZIP" gives: the files of one commit, no .git folder.
            $archive = Join-Path $Destination 'download.zip'
            & git -C $Source archive --format=zip -o $archive $sha; if ($LASTEXITCODE) { throw 'git archive failed' }
            Expand-Archive -LiteralPath $archive -DestinationPath $src
            if (Test-Path -LiteralPath (Join-Path $src '.git')) { throw 'The unpacked ZIP has a .git folder' }
            $result.zip_bytes = (Get-Item -LiteralPath $archive).Length
        } else { New-Clone $Source $sha $src }
        foreach ($name in 'artifacts', 'third_party\cycles') { if (Test-Path -LiteralPath (Join-Path $src $name)) { throw "The fresh clone already contains $name" } }
        if ($DlssOff) { [void](Invoke-Start @('-Action', 'DlssOff')) }
        # An earlier version's state: its CUDA compiler parts in its tools folder, and its build and installer
        # folders next to the project folder (made up here; they must be named once and left alone).
        if ($CudaParts) {
            & python -I (Join-Path $src 'scripts\cuda-toolkit-parts.py') get --root (Join-Path $earlierTools ('cuda-' + $cudaVersion)) | Out-Null
            if ($LASTEXITCODE) { throw 'Getting the CUDA compiler parts into the earlier tools folder failed' }
        }
        New-Item -ItemType Directory -Path ($src + '-build\windows-h22.0.466-release') -Force | Out-Null
        Set-Content -LiteralPath ($src + '-build\windows-h22.0.466-release\build-manifest.json') -Value '{"made_up": "earlier layout"}' -Encoding ascii
        New-Item -ItemType Directory -Path ($src + '-installers') -Force | Out-Null
        Set-Content -LiteralPath ($src + '-installers\old.txt') -Value 'earlier layout' -Encoding ascii
    }
    Invoke-Step 'step1-check-computer-windows-powershell-5' {
        # Proves the script runs in Windows PowerShell 5.1. Its verdict (exit 3 = something is
        # missing on this computer) is recorded, not treated as a failure of the check.
        $shell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
        & $shell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $src 'scripts\easy-check-computer.ps1') -CheckOnly
        $result.step1_exit_code = $LASTEXITCODE
        if ($LASTEXITCODE -notin 0, 3) { throw "easy-check-computer.ps1 crashed in Windows PowerShell 5.1 ($LASTEXITCODE)" }
    }
    Invoke-Step 'first-run-stops-on-a-new-computer' {
        # The first run on a "new computer": every program hidden (this window's PATH only, empty
        # Program Files and local app data folders), and the Python launcher asked for a Python
        # that does not exist, so it writes error output, as on a computer without Python 3.
        # Started through the .cmd file, so the launcher falls back to Windows PowerShell 5.1.
        # It must stop at the computer check (exit 3) with NOT DONE / NEXT, without an error,
        # and build nothing. Answers "no": nothing is installed.
        $empty = Join-Path $Destination 'step1-empty'
        New-Item -ItemType Directory -Path $empty -Force | Out-Null
        $saved = @{}
        $hidden = [ordered]@{ CYCLES_EASY_PATH_ONLY = '1'; CYCLES_EASY_ANSWER = 'no'; CYCLES_EASY_NVIDIA_CONSENT = 'no'; CYCLES_CUDA_ROOT = (Join-Path $empty 'cuda'); PY_PYTHON3 = '3.99'
            ProgramFiles = $empty; 'ProgramFiles(x86)' = $empty; LOCALAPPDATA = $empty
            PATH = ("$env:SystemRoot\System32;$env:SystemRoot;$env:SystemRoot\System32\WindowsPowerShell\v1.0") }
        foreach ($name in $hidden.Keys) { $saved[$name] = [Environment]::GetEnvironmentVariable($name, 'Process'); [Environment]::SetEnvironmentVariable($name, $hidden[$name], 'Process') }
        try {
            $text = & cmd.exe /d /c call $startFile -Action FirstRun 2>&1 | ForEach-Object { [string]$_ }
            $code = $LASTEXITCODE
        } finally {
            foreach ($name in $hidden.Keys) { Set-ProcessVariable $name $saved[$name] }
        }
        $text | Out-Host
        $result.first_run_new_computer = [ordered]@{ exit_code = $code; py_launcher = (Test-Path -LiteralPath (Join-Path $env:SystemRoot 'py.exe'))
            missing_lines = @($text | Where-Object { $_ -match 'MISSING' }).Count; error_lines = @($text | Where-Object { $_ -match '^ERROR:' }).Count
            windows_powershell = [bool]($text -match 'This window runs in Windows PowerShell') }
        if ($code -ne 3 -or $result.first_run_new_computer.error_lines) { throw "The first run on a new computer gave exit code $code (expected 3, 'something is missing')" }
        if (-not ($text -match '^NOT DONE: ') -or -not ($text -match '^NEXT: ')) { throw 'The first run on a new computer did not say NOT DONE and NEXT' }
        if (-not ($text -match 'MISSING Python')) { throw 'The first run on a new computer did not report Python as missing' }
        if (Test-Path -LiteralPath $buildRoot) { throw 'The first run built something although programs were missing' }
    }
    Invoke-Step 'first-run' {
        # The same first run with the programs there (the next double-click), started in Windows
        # PowerShell 5.1: the check runs there, then the run continues in PowerShell 7.
        # Outside the project folder nothing may appear or change: the other entries of -Destination (the earlier
        # layout's folders, the earlier tools folder) and the real %LOCALAPPDATA%\CyclesForHoudiniTools.
        $localTools = Join-Path $env:LOCALAPPDATA 'CyclesForHoudiniTools'
        $outsideBefore = @(Get-ChildItem -LiteralPath $Destination -Force | Where-Object { $_.FullName -ne $src -and $_.Name -ne 'result.json' } | ForEach-Object { Get-Listing $_.FullName; $_.FullName })
        $toolsBefore = @(Get-Listing $localTools)
        $run = Invoke-Start (@('-Action', 'FirstRun', '-NoUpdate', '-Jobs', [string]$Jobs) + $testArgs) @(0) $windowsPowerShell
        $outsideAfter = @(Get-ChildItem -LiteralPath $Destination -Force | Where-Object { $_.FullName -ne $src -and $_.Name -ne 'result.json' } | ForEach-Object { Get-Listing $_.FullName; $_.FullName })
        $changedOutside = @(Get-ListingChanges $outsideBefore $outsideAfter)
        $changedTools = @(Get-ListingChanges $toolsBefore @(Get-Listing $localTools))
        $result.first_run_outside_project = [ordered]@{ changed_in_destination = $changedOutside; changed_in_local_tools = $changedTools; local_tools = $localTools }
        if ($changedOutside.Count -or $changedTools.Count) { throw "The first run created or changed files outside the project folder: $(($changedOutside + $changedTools) -join ', ')" }
        if (-not ($run.Text -match 'An earlier version kept its builds or installers in .*-build\.')) { throw 'The earlier layout folder was not named' }
        # The longest path below the project folder: with the longest project path the tool allows (80 characters)
        # it must stay under Windows' 260.
        $longest = @([IO.Directory]::EnumerateFileSystemEntries($src, '*', [IO.SearchOption]::AllDirectories) | Sort-Object Length -Descending | Select-Object -First 1)[0]
        $below = $longest.Length - $src.Length
        $result.longest_path = [ordered]@{ path = $longest; length = $longest.Length; below_project = $below; with_longest_allowed_project = 80 + $below }
        if (80 + $below -ge 260) { throw "A path below the project folder is $below characters long: with an 80-character project path it would pass Windows' 260 ($longest)" }
        if ($CudaParts -and -not ($run.Text -match 'The old folder .*earlier-tools.* is no longer used')) { throw 'The CUDA compiler parts were not copied from the earlier tools folder' }
        if (-not ($run.Text -match 'Continuing in PowerShell 7')) { throw 'The first run did not continue in PowerShell 7' }
        if (-not ($run.Text -match '^DONE: Cycles for Houdini is built, its quick tests passed and the installers are made')) { throw 'The first run did not end with DONE and the installers' }
        $result.first_run_questions = @($run.Text | Where-Object { $_ -match '\(CYCLES_EASY_ANSWER=yes\)' })
        # The one allowed answer: the install list of the computer check (when something is missing).
        if ($result.first_run_questions.Count -gt 1 -or @($result.first_run_questions | Where-Object { $_ -notmatch '^Install all of these\?' }).Count) { throw "The first run asked more than the one install question: $($result.first_run_questions -join ' | ')" }
        foreach ($never in 'Open Houdini', 'every time', 'Continue\?', 'Continue without', 'anyway\?') { if (@($run.Text | Where-Object { $_ -match "$never.*\[[yY]/[nN]\]" }).Count) { throw "The first run asked '$never'" } }
        # Nothing written into the Houdini preferences (redirected here): the build installs nothing.
        $prefsRoot = Split-Path -Parent $testPrefs
        $result.first_run_preferences_files = @(if (Test-Path -LiteralPath $prefsRoot) { Get-ChildItem -LiteralPath $prefsRoot -Recurse -File -Force | ForEach-Object { $_.FullName } })
        if ($result.first_run_preferences_files.Count) { throw "The first run wrote into the Houdini preferences: $($result.first_run_preferences_files -join ', ')" }
        $result.first_run_lines = [ordered]@{
            houdini_list = @($run.Text | Where-Object { $_ -match '^  Houdini \d+\.\d+\.\d+: ' })
            estimate = @($run.Text | Where-Object { $_ -match 'minutes per Houdini build|minutes for the first' })
            gpu_programs = @($run.Text | Where-Object { $_ -match '^GPU program' })
            per_build = @($run.Text | Where-Object { $_ -match '^  Houdini \d+\.\d+\.\d+: (built|the build)' })
            untested_note = @($run.Text | Where-Object { $_ -match 'is not the build this project was tested with' })
            nvidia = @($run.Text | Where-Object { $_ -match 'CPU-only|DLSS|GPU rendering and GPU denoising' })
            installer_lines = @($run.Text | Where-Object { $_ -match 'The result is an installer|parts made from NVIDIA|Your installers|for Houdini \d+\.\d+\.\d+$|Installed apps|updates the one installed|uninstall that one first' })
        }
    }
    # The Houdini builds that were built, each with its own build folder.
    $builtFolders = @(Get-ChildItem -LiteralPath $buildRoot -Directory -Filter 'windows-h*-release' | Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName 'build-manifest.json') } | Sort-Object Name -Descending)
    $result.builds = [ordered]@{}
    foreach ($folder in $builtFolders) {
        $version = [regex]::Match($folder.Name, 'windows-h(\d+\.\d+\.\d+)-release').Groups[1].Value
        $cacheLines = Get-Content -LiteralPath (Join-Path $folder.FullName 'CMakeCache.txt')
        $entry = { param($name) (@($cacheLines | Where-Object { $_ -match ('^' + $name + ':') }) -replace '^[^=]*=', '') | Select-Object -First 1 }
        $manifest = Get-Content -LiteralPath (Join-Path $folder.FullName 'build-manifest.json') -Raw | ConvertFrom-Json
        $result.builds[$version] = [ordered]@{ folder = $folder.FullName; gib = (Get-FolderGiB $folder.FullName); houdini_root = (& $entry 'HOUDINI_ROOT')
            host_sdk = $manifest.host_sdk.houdini; hdk_api = $manifest.host_sdk.hdk_api; cuda = (& $entry 'WITH_CYCLES_DEVICE_CUDA'); optix = (& $entry 'WITH_CYCLES_DEVICE_OPTIX')
            dlss = (& $entry 'WITH_DLSS'); dlss_project_id = (& $entry 'CH_DLSS_PROJECT_ID'); community_dlss_copy = (Test-Path -LiteralPath (Join-Path $folder.FullName 'community-dlss'))
            stamp = (Get-Content -LiteralPath (Join-Path $folder.FullName 'easy-build.json') -Raw | ConvertFrom-Json) }
        if ($result.builds[$version].host_sdk -ne $version) { throw "The build folder for $version holds a build for $($result.builds[$version].host_sdk)" }
    }
    # Build completion rows are printed in execution order. The directory listing is only for inventory.
    $result.versions = @($result.first_run_lines.per_build | ForEach-Object {
        if ($_ -match '^  Houdini (\d+\.\d+\.\d+): ') { $Matches[1] }
    })
    if ($result.versions.Count -ne $result.builds.Count -or
        @($result.versions | Select-Object -Unique).Count -ne $result.versions.Count -or
        @($result.versions | Where-Object { $_ -notin $result.builds.Keys }).Count) {
        throw 'The first-run result does not account for the builds in execution order.'
    }
    if (-not $NoNvidia) {
        # The first build compiles the six GPU kernels; every further Houdini build reuses them, byte for byte.
        $versions = $result.versions
        $result.kernels = [ordered]@{ first_run = [ordered]@{} }
        foreach ($v in $versions) { $result.kernels.first_run[$v] = Get-KernelState $result.builds[$v].folder }
        # ninja runs all six rules in every new build folder: the first compiles, every further one reuses.
        $firstKernels = $result.kernels.first_run[$versions[0]]
        Invoke-Judge 'first-compile' @{ state = $firstKernels } "The first build ($($versions[0]))"
        foreach ($v in @($versions | Select-Object -Skip 1)) {
            Invoke-Judge 'reuse-all' @{ state = $result.kernels.first_run[$v]; reference = $firstKernels } "First run, Houdini $v"
        }
    } else {
        # A CPU-only build: no GPU kernels, so nothing of the kernel reuse is made.
        $result.kernels = [ordered]@{ cache_folder = (Test-Path -LiteralPath (Join-Path $buildRoot 'kernel-cache')); reuse_folders = @($result.builds.Values | Where-Object { Test-Path -LiteralPath (Join-Path $_.folder 'kernel-reuse') } | ForEach-Object { $_.folder }) }
        if ($result.kernels.cache_folder -or $result.kernels.reuse_folders.Count) { throw 'A CPU-only build made a GPU kernel cache' }
    }
    # "Update the sources and rebuild" with unchanged kernel inputs: no kernel is compiled; the files stay the same.
    function Assert-KernelsKept([string]$When) {
        if ($NoNvidia) { return }
        $states = [ordered]@{}
        foreach ($v in @($result.builds.Keys)) {
            $k = Get-KernelState $result.builds[$v].folder
            $states[$v] = $k
            Invoke-Judge 'kept' @{ state = $k; reference = $result.kernels.first_run[$v] } "$When, Houdini $v"
        }
        $result.kernels[$When] = $states
    }
    # One installer per built Houdini build, in one folder, named for its Houdini build; its package names
    # the same Houdini build and HDK API as the build, and carries the licence notices (CUDA ones in GPU builds).
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    function Get-InstallerRecord([string]$Folder, [string]$Version, [string]$DetailsFolder = '') {
        if (-not $DetailsFolder) { $DetailsFolder = Join-Path $Folder 'details' }
        $details = Get-ChildItem -LiteralPath $DetailsFolder -Directory -Filter ($Version + '-*') | Sort-Object Name -Descending | Select-Object -First 1
        if (-not $details) { throw "No installer details for Houdini $Version in $Folder" }
        $report = Get-Content -LiteralPath (Join-Path $details.FullName 'community-build-report.json') -Raw | ConvertFrom-Json
        if ($report.result -ne 'passed') { throw "The installer report for Houdini $Version says $($report.result)" }
        $setup = Get-Item -LiteralPath ([string]$report.setup)
        if ($setup.DirectoryName -ne $Folder) { throw "The Setup for Houdini $Version is not in $Folder ($($setup.FullName))" }
        if ($setup.Name -ne ($report.installer.default_directory + ' - Setup.exe') -or $setup.Name -notmatch [regex]::Escape("Cycles for Houdini $Version ")) { throw "Unexpected Setup name $($setup.Name)" }
        $hash = (Get-FileHash -LiteralPath $setup.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($hash -ne $report.installer.setup_sha256 -or $setup.Length -ne [int64]$report.installer.setup_bytes) { throw "The Setup for Houdini $Version differs from its installer report" }
        $zip = [IO.Compression.ZipFile]::OpenRead([string]$report.package)
        try {
            $names = @($zip.Entries | ForEach-Object { $_.FullName })
            $read = { param($name) $r = New-Object IO.StreamReader(($zip.Entries | Where-Object { $_.FullName -eq $name } | Select-Object -First 1).Open()); try { $r.ReadToEnd() } finally { $r.Dispose() } }
            $manifest = (& $read 'release-manifest.json') | ConvertFrom-Json
            # The HDK API the package was built against (the frozen SDK provider, scripts/release-package.py archive_sdk_provider).
            $hdk = [string]((& $read 'host/resources/sdk-dependencies.json') | ConvertFrom-Json).openimagedenoise.hdk_api
        } finally { $zip.Dispose() }
        return [ordered]@{ setup = $setup.FullName; name = $setup.Name; bytes = $setup.Length; sha256 = $hash; package = [string]$report.package
            package_houdini = $manifest.houdini; package_hdk_api = $hdk; notices = @($names | Where-Object { $_ -like 'notices/*' }); gates = $report.package_gates
            app_id = $report.installer.app_id; package_filename = $report.installer.package_filename; signed = $report.installer.signed; official = $report.installer.official }
    }
    $result.installers = [ordered]@{ folder = $installers; files = @(Get-ChildItem -LiteralPath $installers -File -Filter '*.exe' | ForEach-Object { $_.Name }) }
    foreach ($version in @($result.builds.Keys)) {
        $record = Get-InstallerRecord $installers $version $installerDetails
        $result.installers[$version] = $record
        if ($record.package_houdini -ne $version) { throw "The installer for $version holds a package for $($record.package_houdini)" }
        if ([string]$record.package_hdk_api -ne [string]$result.builds[$version].hdk_api) { throw "The installer for $version has HDK API $($record.package_hdk_api), the build $($result.builds[$version].hdk_api)" }
        if ($record.official -ne $false) { throw "The installer for $version does not say official: false" }
        if ($result.builds[$version].cuda -in 'ON', 'TRUE', '1') {
            foreach ($notice in 'notices/cuda/', 'notices/optix-headers/') { if (-not @($record.notices | Where-Object { $_.StartsWith($notice) }).Count) { throw "The installer for $version lacks $notice" } }
        }
    }
    if ($result.installers.files.Count -ne $result.builds.Count) { throw "Expected $($result.builds.Count) installers in $installers, found: $($result.installers.files -join ', ')" }
    $result.third_party_dlss_present = (Test-Path -LiteralPath (Join-Path $src 'third_party\dlss'))
    $result.build_identity_after_first_run = Get-Content -LiteralPath (Join-Path $src 'build-identity.json') -Raw | ConvertFrom-Json
    if ($NoNvidia -or $DlssOff) {
        if ($result.third_party_dlss_present) { throw 'NVIDIA''s DLSS headers were downloaded although DLSS is off or there is no NVIDIA card' }
        foreach ($b in $result.builds.Values) { if ($b.dlss -notin 'OFF', 'FALSE', '0') { throw "WITH_DLSS is $($b.dlss) in $($b.folder)" } }
    }
    if ($DlssOff -and $result.build_identity_after_first_run.dlss_project_id) { throw 'A DLSS project id was generated although DLSS is off' }
    if ($NoNvidia) { foreach ($b in $result.builds.Values) { if ($b.cuda -notin 'OFF', 'FALSE', '0') { throw "Not a CPU-only build: WITH_CYCLES_DEVICE_CUDA is $($b.cuda)" } } }
    $newest = $builtFolders[0].FullName
    $result.engine_route = (& python -I (Join-Path $src 'scripts\engine-fork.py') route --source-root $src).Trim()
    if ($result.engine_route -ne 'public') { throw "Expected the public engine route, got $($result.engine_route)" }
    if (-not $NoNvidia) { Invoke-Step 'cuda-toolkit-used' {
        $cache = Get-Content -LiteralPath (Join-Path $newest 'CMakeCache.txt')
        $toolkit = (@($cache | Where-Object { $_ -match '^CUDA_TOOLKIT_ROOT_DIR:' }) -replace '^[^=]*=', '') | Select-Object -First 1
        $nvcc = (@($cache | Where-Object { $_ -match '^CUDA_NVCC_EXECUTABLE:' }) -replace '^[^=]*=', '') | Select-Object -First 1
        $result.cuda = [ordered]@{ toolkit_root = $toolkit; nvcc = $nvcc; parts_requested = [bool]$CudaParts
            nvcc_version = ((& $nvcc --version) | Select-Object -Last 2) -join ' ' }
        if ($CudaParts) {
            $status = (& python -I (Join-Path $src 'scripts\cuda-toolkit-parts.py') status --root $cudaPartsRoot) | ConvertFrom-Json
            $result.cuda.parts_status = $status
            if (-not $status.complete) { throw 'The CUDA compiler parts are not complete' }
            if ([IO.Path]::GetFullPath($toolkit) -ne [IO.Path]::GetFullPath($cudaPartsRoot)) { throw "The build used $toolkit, not the CUDA compiler parts in $cudaPartsRoot" }
            # Nothing the build uses may come from NVIDIA's installed toolkit: no build command (*.ninja,
            # *.rsp) names it, and neither do the CMake entries for the compiler, headers and runtime.
            # FindCUDA also looks up optional libraries (cuBLAS, cuFFT, NPP, ...) that nothing links; on a
            # computer that has the full toolkit too, it may find those there. They are listed, not refused.
            $named = @(Get-ChildItem -LiteralPath $buildRoot -Recurse -File -Include '*.ninja', '*.rsp' -ErrorAction SilentlyContinue |
                Select-String -SimpleMatch 'NVIDIA GPU Computing Toolkit' -List | ForEach-Object { $_.Path })
            $used = @($cache | Where-Object { $_ -match '^(CUDA_TOOLKIT_ROOT_DIR|CUDA_NVCC_EXECUTABLE|CUDA_TOOLKIT_INCLUDE|CUDA_CUDART_LIBRARY|CUDA_CUDA_LIBRARY|CUDA_cudadevrt_LIBRARY|CUDA_cudart_static_LIBRARY)[:=]' })
            $named += @($used | Where-Object { $_ -match 'NVIDIA GPU Computing Toolkit' })
            $result.cuda.used_cache_entries = $used
            $result.cuda.optional_entries_from_installed_toolkit = @($cache | Where-Object { $_ -match 'NVIDIA GPU Computing Toolkit' -and $_ -notmatch '-ADVANCED' } | ForEach-Object { ($_ -split ':', 2)[0] })
            # Recorded, not thrown here, so that the menu steps still run; the check fails at the end.
            $result.cuda.files_naming_installed_toolkit = $named
            $result.cuda.parts_gib = Get-FolderGiB $cudaPartsRoot
        }
    } }
    if ($NoNvidia) {
        Invoke-Step 'cpu-only-and-no-dlss-download' {
            # Also the answer "no" on a computer with an NVIDIA card: the CUDA compiler parts are
            # missing, which must not count as "something is missing" (exit 0, CPU-only sentence).
            $saved = $env:CYCLES_EASY_NO_NVIDIA; $savedAnswer = $env:CYCLES_EASY_ANSWER; $savedConsent = $env:CYCLES_EASY_NVIDIA_CONSENT
            $env:CYCLES_EASY_NO_NVIDIA = $null; $env:CYCLES_EASY_ANSWER = 'no'; $env:CYCLES_EASY_NVIDIA_CONSENT = 'no'
            try { $text = @(& $pwsh -NoProfile -ExecutionPolicy Bypass -File (Join-Path $src 'scripts\easy-check-computer.ps1') 2>&1 | ForEach-Object { [string]$_ }); $code = $LASTEXITCODE }
            finally { $env:CYCLES_EASY_NO_NVIDIA = $saved; $env:CYCLES_EASY_ANSWER = $savedAnswer; $env:CYCLES_EASY_NVIDIA_CONSENT = $savedConsent }
            $text | Out-Host
            $result.answer_no_with_nvidia = [ordered]@{ exit_code = $code; lines = @($text | Where-Object { $_ -match 'CPU-only|CUDA|DLSS' }) }
            if ($code -ne 0) { throw "Answering no to the CUDA compiler parts gave exit code $code (expected 0: a CPU-only build)" }
            if (-not ($text -match 'built for the CPU')) { throw 'Answering no did not say that the build will be CPU-only' }
        }
    }
    Invoke-Step 'later-run-shows-the-menu' {
        # A tested build exists now: without -Action the file shows the menu. Unattended, it lists
        # the menu and stops without choosing (exit 1).
        $run = Invoke-Start @() @(1)
        foreach ($item in 'Update the sources and rebuild (then the quick tests and new installers)', 'Run the quick tests', 'Make the installers again', 'Open Houdini with my build (without installing', 'I changed the engine: update the patch files', 'Turn DLSS o', 'Check this computer again', 'Close   (press Enter for this)') {
            if (-not ($run.Text -match [regex]::Escape($item))) { throw "The menu lacks '$item'" }
        }
        foreach ($item in 'every time', 'Build for another Houdini') { if ($run.Text -match [regex]::Escape($item)) { throw "The menu still has '$item'" } }
    }
    Invoke-Step 'second-run-at-the-same-time-is-refused' {
        . (Join-Path $src 'scripts\easy-common.ps1')
        $mutex = New-Object System.Threading.Mutex($true, (Get-EasyStartLockName))
        try { $run = Invoke-Start @('-Action', 'CheckComputer') @(5) } finally { $mutex.ReleaseMutex(); $mutex.Dispose() }
        if (-not ($run.Text -match 'already running')) { throw 'The second run did not say that the first one is running' }
    }
    Invoke-Step 'menu-check-computer' { [void](Invoke-Start @('-Action', 'CheckComputer')) }
    Invoke-Step 'menu-quick-tests-through-the-cmd-file' {
        $text = & cmd.exe /d /c call $startFile -Action QuickTests @testArgs 2>&1 | ForEach-Object { [string]$_ }
        $code = $LASTEXITCODE
        $text | Out-Host
        if ($code -ne 0 -or -not ($text -match '^DONE: The quick tests passed')) { throw "QuickTests through the .cmd file gave exit code $code" }
        $result.quick_tests = [ordered]@{}
        foreach ($folder in $builtFolders) {
            $latest = Get-ChildItem -LiteralPath (Join-Path $folder.FullName 'quick-tests') -Filter 'summary.json' -Recurse | Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 1
            Copy-Item -LiteralPath $latest.FullName -Destination (Join-Path $Destination ('quick-tests-' + $folder.Name + '.json'))
            $result.quick_tests[$folder.Name] = (Get-Content -LiteralPath $latest.FullName -Raw | ConvertFrom-Json).checks
        }
    }
    Invoke-Step 'menu-update-and-rebuild' {
        $run = Invoke-Start (@('-Action', 'Update', '-Jobs', [string]$Jobs) + $testArgs)
        if ($Zip -and -not ($run.Text -match 'downloaded as a ZIP, so it cannot update itself')) { throw 'Update in a ZIP folder did not say what to do instead' }
        $result.update_per_build = @($run.Text | Where-Object { $_ -match '^  Houdini \d+\.\d+\.\d+: ' })
        # Update made new installers (the installer folder keeps the newest details of each Houdini build).
        $result.installers_after_update = [ordered]@{}
        foreach ($version in @($result.builds.Keys)) { $result.installers_after_update[$version] = Get-InstallerRecord $installers $version $installerDetails }
        Assert-KernelsKept 'update_unchanged'
    }
    Invoke-Step 'menu-make-the-installers-again' {
        $out = Join-Path $Destination 'package'
        $run = Invoke-Start @('-Action', 'Installer', '-OutputDirectory', $out, '-Jobs', [string]$Jobs)
        foreach ($never in 'Continue\?', 'Continue without', 'anyway\?') { if (@($run.Text | Where-Object { $_ -match "$never.*\[[yY]/[nN]\]" }).Count) { throw "Make the installers again asked '$never'" } }
        $result.package = [ordered]@{}
        foreach ($version in @($result.builds.Keys)) {
            $record = Get-InstallerRecord $out $version
            if ($record.sha256 -eq $result.installers_after_update[$version].sha256) { throw "Make the installers again gave the same Setup for $version (expected a new package)" }
            $result.package[$version] = $record
        }
        $details = Get-ChildItem -LiteralPath (Join-Path $out 'details') -Directory | Sort-Object Name -Descending | Select-Object -First 1
        $report = Get-Content -LiteralPath (Join-Path $details.FullName 'community-build-report.json') -Raw | ConvertFrom-Json
        if ($report.build_record.local_changes) { throw 'A fresh clone must not report local changes' }
        $result.package_record = [ordered]@{ local_changes = $report.build_record.local_changes; checks = $report.build_record.checks; dlss = $report.build_record.dlss
            dlss_project_id = $report.build_record.dlss_project_id; build_id = $report.build_identity.build_id; gates = $report.package_gates }
        # NVIDIA material in the package and its notices.
        $zipFile = [IO.Compression.ZipFile]::OpenRead([string]$report.package)
        try { $names = @($zipFile.Entries | ForEach-Object { $_.FullName }) } finally { $zipFile.Dispose() }
        $result.package_record.nvidia_entries = @($names | Where-Object { $_ -match '(?i)kernel_|optix|dlss|ngx|nvidia|cudart|notices/cuda' })
        $result.package_record.unsigned = @($result.package.Values | ForEach-Object { -not $_.signed })
    }
    Invoke-Step 'setup-style-install-update-refuse-abort-recover-commit-uninstall' { Invoke-SetupRounds }
    if (-not $SkipEngineEdit -and -not $Zip) {
        Invoke-Step 'engine-edit-and-update-patches' {
            # A .cpp file few others depend on, so the rebuild stays short. Windows line endings on
            # purpose: the tool must store the patch with LF, as the engine keeps the file.
            $file = Join-Path $src 'third_party\cycles\src\util\md5.cpp'
            Add-Content -LiteralPath $file -Value "`r`n/* Fresh clone check: an engine edit (comment only). */" -Encoding ascii
            [void](Invoke-Start @('-Action', 'EnginePatches', '-Message', 'Fresh clone check engine edit', '-Yes'))
            $result.edited_engine_head = (& git -C (Join-Path $src 'third_party\cycles') rev-parse HEAD).Trim()
            $result.edited_commit = (& git -C $src rev-parse HEAD).Trim()
            # build-identity.json was filled in on first use (step 2); it is not a source change.
            if (& git -C $src status --porcelain --untracked-files=no -- . ':(exclude)build-identity.json') { throw 'update-engine-patches left uncommitted changes' }
        }
        Invoke-Step 'menu-update-and-rebuild-after-engine-edit' {
            [void](Invoke-Start (@('-Action', 'Update', '-NoUpdate', '-Jobs', [string]$Jobs) + $testArgs))
            Assert-KernelsKept 'update_after_engine_edit_outside_the_kernels'
        }
        Invoke-Step 'second-fresh-clone-replays-the-same-engine' {
            New-Clone $src $result.edited_commit $replay
            & python -I (Join-Path $replay 'scripts\engine-fork.py') materialize-public --source-root $replay --destination (Join-Path $replay 'third_party\cycles') --output (Join-Path $Destination 'replay-evidence')
            if ($LASTEXITCODE) { throw 'Public replay of the edited patch series failed' }
            $head = (& git -C (Join-Path $replay 'third_party\cycles') rev-parse HEAD).Trim()
            $result.replayed_engine_head = $head
            if ($head -ne $result.edited_engine_head) { throw "Fresh replay gives engine commit $head, the edited clone has $($result.edited_engine_head)" }
        }
        if ($KernelChecks -and -not $NoNvidia) {
            $versions = $result.versions
            $newest = $versions[0]
            $reference = $result.kernels.first_run[$newest]
            $result.kernels.checks = [ordered]@{}
            Invoke-Step 'kernel-reuse-off-compiles-everything-identically' {
                # The switch: with -NoKernelReuse the build runs nvcc itself (no stand-in in build.ninja) and
                # compiles all kernels; the result is the reference compile for the byte identity.
                [void](Invoke-Start (@('-Action', 'Update', '-NoUpdate', '-Houdini', $newest, '-NoKernelReuse', '-Jobs', [string]$Jobs) + $testArgs))
                $off = Get-KernelState $result.builds[$newest].folder
                $result.kernels.checks.reuse_off = $off
                # The switch changes the command of all six rules, so ninja runs them all, with nvcc itself.
                if ($off.wrapper_in_build_ninja) { throw 'With -NoKernelReuse build.ninja still runs the stand-in for nvcc' }
                Invoke-Judge 'reference-compile' @{ state = $off; references = $result.kernels.first_run } 'Reuse off (reference compile)'
                # Back on: the stand-in again; nothing changed, so every kernel is reused. PATH is given with every
                # entry twice (the same programs are found, the text differs): two runs with the same inputs can see
                # PATH in another order (seen in a test run), which must not change the key.
                $savedPath = $env:PATH
                try { $env:PATH = $savedPath + ';' + $savedPath; [void](Invoke-Start (@('-Action', 'Update', '-NoUpdate', '-Houdini', $newest, '-Jobs', [string]$Jobs) + $testArgs)) }
                finally { $env:PATH = $savedPath }
                $on = Get-KernelState $result.builds[$newest].folder
                $result.kernels.checks.reuse_on_again = $on
                # The command changes back: ninja runs all six rules through the stand-in; all come from the store.
                if (-not $on.wrapper_in_build_ninja) { throw 'With reuse on again build.ninja does not run the stand-in' }
                Invoke-Judge 'reuse-all' @{ state = $on; reference = $off } 'Reuse on again'
            }
            function Edit-EngineFile([string]$Relative, [string]$Text, [string]$Message) {
                Add-Content -LiteralPath (Join-Path $src ('third_party\cycles\' + $Relative)) -Value $Text -Encoding ascii
                [void](Invoke-Start @('-Action', 'EnginePatches', '-Message', $Message, '-Yes'))
            }
            Invoke-Step 'kernel-comment-edit-is-a-new-key' {
                # A comment in a header every kernel includes: the GPU programs may come out the same, but the key
                # covers every input file by content, so all six are compiled again (asserted on the decision).
                Edit-EngineFile 'src\kernel\types.h' "`n/* Fresh clone check: a comment in a kernel header. */" 'Fresh clone check kernel header comment'
                [void](Invoke-Start (@('-Action', 'Update', '-NoUpdate', '-Houdini', $newest, '-Jobs', [string]$Jobs) + $testArgs))
                $k = Get-KernelState $result.builds[$newest].folder
                $k.same_bytes_as_before = (($k.files | ConvertTo-Json -Compress) -eq ($reference.files | ConvertTo-Json -Compress))
                $result.kernels.checks.comment_edit = $k
                # kernel/types.h is an input of all six rules: ninja runs them all; every input counts by content.
                Invoke-Judge 'new-key-compiles-all' @{ state = $k } 'A comment in kernel/types.h'
            }
            Invoke-Step 'kernel-real-edit-recompiles-only-what-it-touches' {
                # A real change in the CUDA kernel source (one more, unused kernel function): the three CUDA kernels
                # are compiled again and differ; the OptiX rules do not depend on that file and do not run. The other
                # Houdini builds then reuse all six from the newest one, and their quick tests pass.
                Edit-EngineFile 'src\kernel\device\cuda\kernel.cu' "`nextern `"C`" __global__ void kernel_cuda_fresh_clone_check(int *value) { if (value) { *value = 1; } }" 'Fresh clone check kernel source change'
                [void](Invoke-Start (@('-Action', 'Update', '-NoUpdate', '-Houdini', 'all', '-Jobs', [string]$Jobs) + $testArgs))
                $states = [ordered]@{}
                foreach ($v in $versions) { $states[$v] = Get-KernelState $result.builds[$v].folder }
                $result.kernels.checks.real_edit = $states
                # device/cuda/kernel.cu is an input of the three CUDA rules only: in the newest build ninja runs those
                # three (compiled) and not the OptiX ones (their files stay as they are); in the other builds
                # kernel/types.h changed since their last build, so all six rules run there and all six are reused.
                Invoke-Judge 'real-edit' @{ states = $states; newest = $newest; before = $result.kernels.checks.comment_edit } 'A real change in device/cuda/kernel.cu'
            }
            Invoke-Step 'kernel-damaged-copy-is-ignored' {
                # The stored copy of one kernel is damaged (same size, other bytes) and that kernel is removed from an
                # older Houdini build's folder: the build must not use the damaged copy; it compiles that kernel and
                # gets the same bytes as the newest build.
                $older = $versions[-1]
                $name = 'kernel_compute_75.ptx'
                $copy = Find-StoredKernelCopy $result.kernels.checks.real_edit[$older].decisions $name $buildRoot
                if (-not $copy.Exists) { throw "No stored copy for $name with key $($copy.Key)" }
                $stored = $copy.File
                $bytes = [IO.File]::ReadAllBytes($stored); $bytes[100] = $bytes[100] -bxor 0xFF; [IO.File]::WriteAllBytes($stored, $bytes)
                Remove-Item -LiteralPath (Join-Path $result.builds[$older].folder ('cycles\src\kernel\device\cuda\' + $name)) -Force
                [void](Invoke-Start (@('-Action', 'Update', '-NoUpdate', '-Houdini', $older, '-Jobs', [string]$Jobs) + $testArgs))
                $k = Get-KernelState $result.builds[$older].folder
                $result.kernels.checks.damaged = $k
                # Removing the file makes ninja run its rule (and its .zst; no other kernel depends on it): the damaged copy is
                # detected and that kernel is compiled again.
                Invoke-Judge 'damaged' @{ state = $k; name = $name; reference = $result.kernels.checks.real_edit[$newest] } 'A damaged stored copy'
            }
            $counts = Get-KernelCacheCounts $buildRoot
            $result.kernels.cache_entries = @($counts.Keys | ForEach-Object { [ordered]@{ kernel = $_; entries = $counts[$_] } })
            Invoke-Judge 'cache-entries' @{ counts = $counts } 'Stored copies'
        }
    }
    Invoke-Step 'check-computer-leaves-preferences-untouched' {
        $prefs = $testPrefs.Replace('__HVER__', '22.0')
        $packages = Join-Path $prefs 'packages'
        New-Item -ItemType Directory -Path $packages -Force | Out-Null
        $registration = Join-Path $packages 'cycles_houdini_mine.json'
        Set-Content -LiteralPath $registration -Value '{"enable":"houdini_version == ''21.0.440''","env":[{"MY":"1"}]}' -Encoding ascii
        $before = Get-Listing $prefs
        [void](Invoke-Start @('-Action', 'CheckComputer'))
        $after = Get-Listing $prefs
        if (@(Get-ListingChanges $before $after).Count) { throw 'Checking the computer changed its Houdini preferences' }
    }
    $result.disk_gib = [ordered]@{ total = (Get-FolderGiB $Destination); project = (Get-FolderGiB $src); work = (Get-FolderGiB $buildRoot); installers = (Get-FolderGiB $installers); builds = [math]::Round((@($builtFolders | ForEach-Object { Get-FolderGiB $_.FullName }) | Measure-Object -Sum).Sum, 2); cuda_parts = (Get-FolderGiB $cudaPartsRoot); earlier_tools = (Get-FolderGiB $earlierTools); package = (Get-FolderGiB (Join-Path $Destination 'package')); replay = (Get-FolderGiB $replay) }
    $result.result = 'passed'
    if ($CudaParts -and $result.cuda.files_naming_installed_toolkit.Count) { throw "Build files name the installed CUDA toolkit: $($result.cuda.files_naming_installed_toolkit -join ', ')" }
} catch {
    $result.result = 'failed'
    $result.error = $_.Exception.Message
} finally {
    foreach ($name in $previousEnvironment.Keys) { Set-ProcessVariable $name $previousEnvironment[$name] }
    $result.finished_utc = [DateTime]::UtcNow.ToString('o')
    Save-Result
    Get-Content -LiteralPath (Join-Path $Destination 'result.json')
}
if ($result.result -ne 'passed') { exit 1 }
