# SPDX-License-Identifier: Apache-2.0
[CmdletBinding(PositionalBinding=$false)]
param(
    [string]$HoudiniRoot='',
    [string]$BuildDirectory='',
    [string]$PackageDirectory='',
    [string]$UserPrefDirectory='',
    [Parameter(Position=0,ValueFromRemainingArguments=$true)][string[]]$HythonArguments
)
$ErrorActionPreference='Stop'
$taskRoot=Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'native-test-context.ps1')
$context=Get-NativeTestContext -BuildDirectory $BuildDirectory -HoudiniRoot $HoudiniRoot -PackageDirectory $PackageDirectory
$HoudiniRoot=$context.HoudiniRoot
. (Join-Path $PSScriptRoot 'test-artifact-context.ps1')
$previousEnvironment=Save-TestProcessEnvironment
try{
    $env:HOUDINI_PACKAGE_DIR=$context.PackageDirectory
    # An explicitly empty search path hides the standard camera parameter definitions.
    if(-not $env:HOUDINI_PATH){$env:HOUDINI_PATH='&'}
    # Developer runs use their own preferences: an installed Cycles package in the
    # artist's Houdini preferences must never replace the build under test.
    $env:HOUDINI_USER_PREF_DIR=if($UserPrefDirectory){[IO.Path]::GetFullPath($UserPrefDirectory)}else{Join-Path $context.BuildDirectory 'dev-prefs/houdini__HVER__'}
    if($env:HOUDINI_USER_PREF_DIR -notlike '*__HVER__*'){throw 'Use an isolated preferences path containing __HVER__.'}
    $env:HOUDINI_NO_ENV_FILE='1'
    $env:HOUDINI_DSO_ERROR='2'
    $env:CYCLES_HOUDINI_TEST_IDENTITY=$context.RuntimeIdentity | ConvertTo-Json -Compress
    $env:CYCLES_HOUDINI_TEST_IDENTITY_REPORT=Join-Path (Split-Path -Parent $env:HOUDINI_USER_PREF_DIR) ('runtime-identity-'+[guid]::NewGuid().ToString('N')+'.json')
    if(-not $env:CYCLES_HOUDINI_WORKER_LOG_DIR){
        if($env:CYCLES_HOUDINI_TEST_ARTIFACTS){
            $env:CYCLES_HOUDINI_WORKER_LOG_DIR=Join-Path ([IO.Path]::GetFullPath($env:CYCLES_HOUDINI_TEST_ARTIFACTS)) 'workers'
        }elseif(-not $context.RuntimeIdentity.release){
            $env:CYCLES_HOUDINI_WORKER_LOG_DIR=Join-Path $context.BuildDirectory 'workers'
        }
        # Installed packages retain their data SOURCE_ROOT/artifacts/workers unless
        # the caller explicitly selected worker logs or test artifacts above.
    }
    & (Join-Path $HoudiniRoot 'bin/hython.exe') @HythonArguments
    $hythonExitCode=$LASTEXITCODE
}finally{
    Restore-TestProcessEnvironment $previousEnvironment
}
exit $hythonExitCode
