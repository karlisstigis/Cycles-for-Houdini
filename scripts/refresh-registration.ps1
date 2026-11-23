# SPDX-License-Identifier: Apache-2.0
[CmdletBinding()]
param(
    [string]$ApplicationRoot=(Split-Path -Parent $PSScriptRoot),
    [string]$HoudiniRoot='',
    [switch]$PromptForHoudiniRoot
)
$ErrorActionPreference='Stop'
$application=[IO.Path]::GetFullPath($ApplicationRoot)
$pythonFile=Join-Path $application 'houdini-python.txt'
$tool=Join-Path $application 'tools/release-package.py'
$installation=Join-Path $application 'installation'
if(-not(Test-Path -LiteralPath $pythonFile -PathType Leaf)){throw "Installed Houdini Python record is missing: $pythonFile"}
if(-not(Test-Path -LiteralPath $tool -PathType Leaf)){throw "Installed registration tool is missing: $tool"}
$python=(Get-Content -LiteralPath $pythonFile -Raw -Encoding UTF8).Trim()
# Older installers used the Windows ANSI code page for this one-line path.
if(-not [IO.File]::Exists($python)){
    $python=[Text.Encoding]::Default.GetString([IO.File]::ReadAllBytes($pythonFile)).Trim()
}
if(-not(Test-Path -LiteralPath $python -PathType Leaf)){throw "Installed Houdini Python is missing: $python"}
if($PromptForHoudiniRoot -and -not $HoudiniRoot){
    $HoudiniRoot=Read-Host 'Additional Houdini folder (Enter scans standard installations)'
}
$arguments=@('-I',$tool,'refresh-registration','--install-root',$installation)
if($HoudiniRoot){$arguments+=@('--houdini-root',[IO.Path]::GetFullPath($HoudiniRoot))}
& $python @arguments
if($LASTEXITCODE -ne 0){throw "Registration refresh failed (exit $LASTEXITCODE). The existing package was preserved."}
