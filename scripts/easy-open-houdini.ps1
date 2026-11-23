# SPDX-License-Identifier: Apache-2.0
# Opens a separate Houdini that uses your build without installing it (menu item "Open Houdini with
# my build (without installing)": to try a build, or while changing code). It has its own Houdini
# preferences inside the build folder, so an installed Cycles for Houdini and your normal Houdini
# preferences are not touched. Safe to run as often as you like. Never asked on the first run.
[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'easy-common.ps1')
trap { Write-EasyCrash $_; exit 1 }
Write-EasyTitle "Open Houdini $(Get-EasyHoudiniVersion) with my build"
[void](Find-EasyPython); [void](Find-EasyGit)
$build = Get-EasyBuildDirectory
if (-not (Test-Path -LiteralPath (Join-Path $build 'build-manifest.json'))) {
    Write-EasyResult $false "There is no finished build in $build yet." ((Get-EasyAgain) + ' (its first run builds).')
    exit 1
}
if (-not (Invoke-EasyScript 'launch-normal-build.ps1' @('-HoudiniRoot', (Get-EasyHoudiniRoot), '-BuildDirectory', $build, '-ReuseTrialPreferences'))) {
    Write-EasyResult $false 'Houdini could not be started with this build (message above).' ((Get-EasyAgain 'Update the sources and rebuild') + '; if that does not help, open an issue on the project page on GitHub with the lines above.')
    exit 1
}
Write-EasyResult $true 'Houdini is starting with your build (it can take a minute to appear).' 'Close this window whenever you like.'
exit 0
