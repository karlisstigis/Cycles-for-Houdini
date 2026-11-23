#Requires -Version 7
# SPDX-License-Identifier: Apache-2.0
[CmdletBinding()]
param(
    # Default <project>\_work\ci (until 22 November 2026: %USERPROFILE%\.cycles-houdini\CI).
    [string]$StateDirectory = '',
    [string]$Ruff = 'ruff',
    [string]$ClangFormat = 'clang-format',
    [string]$Wheelhouse = '',
    # Default for a new installation: <project>\_work\recovery. A folder on another disk also protects against
    # this disk failing; that is the maintainer's choice.
    [string]$RecoveryDirectory = '',
    [ValidateRange(1,64)][int]$Jobs = 6
)
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Split-Path -Parent $PSScriptRoot)).Path
. (Join-Path $PSScriptRoot 'project-layout.ps1')
if (-not $StateDirectory) { $StateDirectory = Get-ProjectWorkPath -Name 'ci' -SourceRoot $repo }
$state = [IO.Path]::GetFullPath($StateDirectory)
# Inside the source tree only in its ignored working folder (_work), which nothing scans.
if (($state -eq $repo -or $state.StartsWith($repo + '\', [StringComparison]::OrdinalIgnoreCase)) -and -not (Test-InProjectWork -Path $state -SourceRoot $repo)) {
    throw 'Choose a CI state directory outside the production source tree or inside its _work folder.'
}
$common = (& git -C $repo rev-parse --path-format=absolute --git-common-dir).Trim()
if ($LASTEXITCODE -ne 0) {throw 'Cannot identify the project repository.'}
$hookPath = & git -C $repo config --get core.hooksPath
if ($hookPath -and $hookPath -ne '.githooks') {throw "Existing Git hooks path requires manual integration: $hookPath"}
$defaultHook = Join-Path $common 'hooks/post-commit'
if (-not $hookPath -and (Test-Path -LiteralPath $defaultHook)) {throw "Integrate the existing post-commit hook before enabling CI: $defaultHook"}
if (-not $hookPath) {
    $otherHooks = @(Get-ChildItem -LiteralPath (Join-Path $common 'hooks') -File -ErrorAction SilentlyContinue | Where-Object {$_.Name -notlike '*.sample'})
    if($otherHooks.Count){throw "Existing active hooks require manual integration: $($otherHooks.Name -join ', ')"}
}
$python = (Get-Command python -CommandType Application | Select-Object -First 1).Source
$ps7 = (Get-Process -Id $PID).Path
. (Join-Path $PSScriptRoot 'test-dependency-context.ps1')
$Wheelhouse=(Get-NativeTestToolsContext -Wheelhouse $Wheelhouse).Wheelhouse
$config = [ordered]@{repository=$repo;state=$state;pwsh=$ps7;jobs=$Jobs
    ruff=(Get-Command $Ruff -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
    clang=(Get-Command $ClangFormat -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
    wheelhouse=(Resolve-Path -LiteralPath $Wheelhouse).Path}
$launcher = Join-Path (Split-Path -Parent $python) 'pythonw.exe'
if(-not (Test-Path -LiteralPath $launcher -PathType Leaf)){throw 'The scheduled runner needs pythonw.exe from the selected Python distribution.'}
$configuration = Join-Path $common 'houdini-ci.json'
if (Test-Path -LiteralPath $configuration) {
    $previous = Get-Content -LiteralPath $configuration -Raw | ConvertFrom-Json
    if($previous.state -ne $state -or $previous.repository -ne $repo){throw 'Existing CI installation owns another checkout or queue.'}
    if (-not $PSBoundParameters.ContainsKey('RecoveryDirectory')) { $RecoveryDirectory=$previous.recovery_directory }
} elseif (-not $PSBoundParameters.ContainsKey('RecoveryDirectory')) {
    $RecoveryDirectory = Get-ProjectWorkPath -Name 'recovery' -SourceRoot $repo
}
if ($RecoveryDirectory) { $config.recovery_directory=[IO.Path]::GetFullPath($RecoveryDirectory) }
New-Item -ItemType Directory -Force -Path (Join-Path $state 'pending') | Out-Null
# Use the repo identity in task names so separate products and checkouts coexist.
$repoHash = [Convert]::ToHexString([Security.Cryptography.SHA256]::HashData([Text.Encoding]::UTF8.GetBytes($common))).Substring(0,10)
$taskName = "CyclesHoudini-$repoHash"
# The scheduled actions always use the installed runner, even when the working
# checkout switches branches or has uncommitted changes.
$runnerDirectory = Join-Path $state 'runner'
New-Item -ItemType Directory -Path $runnerDirectory -Force | Out-Null
$installedWorker = Join-Path $runnerDirectory 'workstation-ci.py'
$pendingWorker = $installedWorker + '.' + [guid]::NewGuid().ToString('N')
Copy-Item -LiteralPath (Join-Path $repo 'scripts/workstation-ci.py') -Destination $pendingWorker
[IO.File]::Move($pendingWorker, $installedWorker, $true)
$owner = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $owner -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 22) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
foreach($mode in 'queue','nightly'){
    $name = "$taskName-$mode"
    $command = if($mode -eq 'queue'){'drain'}else{'enqueue nightly refs/heads/main'}
    $action = New-ScheduledTaskAction -Execute $launcher -WorkingDirectory $runnerDirectory -Argument ('-I "'+$installedWorker+'" --repository "'+$repo+'" '+$command)
    $old = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if($old){
        $account=$old.Principal.UserId
        $ownerSid=if($account -match '^S-1-'){$account}else{([Security.Principal.NTAccount]::new($account)).Translate([Security.Principal.SecurityIdentifier]).Value}
        if($old.Actions[0].Arguments -ne $action.Arguments -or $ownerSid -ne [Security.Principal.WindowsIdentity]::GetCurrent().User.Value){throw "Scheduled task name already in use: $name"}
    }
    if($mode -eq 'nightly'){
        # Migrate the previously provisioned daily trigger to owner-only runs.
        if($old){Disable-ScheduledTask -TaskName $name | Out-Null}
        continue
    }
    $trigger = New-ScheduledTaskTrigger -Once -At ([datetime]::Now.AddMinutes(2)) -RepetitionInterval (New-TimeSpan -Minutes 5)
    Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
}
$config | ConvertTo-Json | Set-Content -LiteralPath $configuration -Encoding utf8NoBOM
& git -C $repo config --local cycles.ciPython ($python.Replace('\','/'))
if($LASTEXITCODE){throw 'Unable to record the CI interpreter.'}
& git -C $repo config --local cycles.pwsh $ps7
if($LASTEXITCODE){throw 'Unable to record PowerShell 7 for the build launcher.'}
& git -C $repo config --local core.hooksPath .githooks
if($LASTEXITCODE){throw 'Unable to enable the project Git hook.'}
Write-Output "CI enabled for $repo. Queue checks run every five minutes under $owner. Start the nightly from the heavy renderer launcher. Evidence: $state/results."
