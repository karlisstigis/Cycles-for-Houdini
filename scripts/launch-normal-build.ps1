# SPDX-License-Identifier: Apache-2.0
# Opens a separate Houdini (empty scene) with the current staged native development build,
# not the installed renderer. Automated runs use fresh preferences. A manual trial
# can keep Houdini's UI preferences and caches inside its own verified build directory.
# -WithoutAssetBrowser starts it as an installation without the optional asset browser component.
[CmdletBinding()]
param([string]$HoudiniRoot='', [string]$BuildDirectory='', [switch]$ReuseTrialPreferences, [string]$StartupScript='', [string]$Artifacts='', [switch]$WithoutAssetBrowser)
$ErrorActionPreference='Stop'
$root=Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'native-test-context.ps1')
. (Join-Path $PSScriptRoot 'test-artifact-context.ps1')
$context=Get-NativeTestContext -HoudiniRoot $HoudiniRoot -BuildDirectory $BuildDirectory
$packages=$context.PackageDirectory
$hostRoot=$context.RuntimeIdentity.host
$houdini=Join-Path $context.HoudiniRoot 'bin/houdini.exe'
foreach($p in @($hostRoot,$houdini)){if(-not(Test-Path -LiteralPath $p)){throw "Missing: $p"}}
$arguments=@('-foreground')
if ($StartupScript) {
    $scriptFile=Get-Item -LiteralPath $StartupScript -ErrorAction Stop
    if ($scriptFile.PSIsContainer -or $scriptFile.Extension -ne '.py') { throw 'Expected a Houdini startup Python script.' }
    $arguments+=@('waitforui', ('"' + $scriptFile.FullName + '"'))
}
$run=Join-Path (Get-TestEvidenceBase -Artifacts $Artifacts) ('normal-build-sessions/'+[DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfff')+'-'+[Guid]::NewGuid().ToString('N'))
$previousTestEnvironment=Save-TestProcessEnvironment
$profileLock=$null
try {
    if ($ReuseTrialPreferences) {
        $profile=Join-Path $context.BuildDirectory 'user-profile'
        New-Item -ItemType Directory -Force -Path $profile | Out-Null
        # Serialize launch and PID recording. A concurrent Houdini gets its own
        # scratch profile and cannot overwrite the primary trial's preferences.
        try {
            $profileLock=[IO.File]::Open((Join-Path $profile 'launch.lock'),'OpenOrCreate','ReadWrite','None')
        } catch [IO.IOException] {
            if (($_.Exception.HResult -band 0xffff) -ne 32) { throw }
        }
        if ($profileLock) {
            $ownerRecord=Join-Path $profile 'owner.json'
            $owner=$null
            if (Test-Path -LiteralPath $ownerRecord) {
                $record=Get-Content -LiteralPath $ownerRecord -Raw | ConvertFrom-Json
                $owner=Get-Process -Id $record.pid -ErrorAction SilentlyContinue
            }
            if (-not $owner -or ($owner.StartTime -and $owner.StartTime.ToUniversalTime().Ticks.ToString() -ne $record.started_utc_ticks)) {
                $run=$profile
            }
        }
    }
    New-Item -ItemType Directory -Path $run -Force | Out-Null
    # Development packages load the optional asset browser add-on by default.
    if ($WithoutAssetBrowser) { $packages=New-PackageDirectoryWithoutAssetBrowser $packages (Join-Path $run 'packages-without-asset-browser') }
    $envs=@{HOUDINI_PACKAGE_DIR=$packages;HOUDINI_USER_PREF_DIR=(Join-Path $run 'prefs/houdini__HVER__');HOUDINI_PATH='&';
      HOUDINI_NO_ENV_FILE='1';HOUDINI_DSO_ERROR='2';HOUDINI_NO_STARTPAGE='1';HOUDINI_DISABLE_BACKGROUND_HELP_INDEXING='1';CYCLES_HOUDINI_EXECUTION='worker';CYCLES_HOUDINI_WORKER_LOG_DIR=(Join-Path $run 'workers')}
    foreach($k in [Environment]::GetEnvironmentVariables('Process').Keys){if("$k" -like 'CYCLES_HOUDINI_PT_*' -or "$k" -like 'CYCLES_HOUDINI_BDPT*'){[Environment]::SetEnvironmentVariable("$k",$null,'Process')}}
    foreach($k in $envs.Keys){[Environment]::SetEnvironmentVariable($k,$envs[$k],'Process')}
    $p=Start-Process -FilePath $houdini -ArgumentList $arguments -WindowStyle Normal -PassThru
    if ($profileLock -and $run -eq $profile) {
        @{pid=$p.Id;started_utc_ticks=$p.StartTime.ToUniversalTime().Ticks.ToString()} |
            ConvertTo-Json | Set-Content -LiteralPath $ownerRecord -Encoding utf8
    }
    Write-Output "Opened Houdini PID $($p.Id) with host $hostRoot and private preferences in $run"
} finally {
    if ($profileLock) { $profileLock.Dispose() }
    Restore-TestProcessEnvironment $previousTestEnvironment
}
