# SPDX-License-Identifier: Apache-2.0
# CMakePresets.json owns the defaults for both direct CMake and script users: the developer build cache is
# <project>\_work\dev\windows-h<build>-<variant>, inside the project folder (docs/dev/README.md, "Working folders").
function Get-DefaultBuildDirectory {
    param(
        [string]$SourceRoot=(Split-Path -Parent $PSScriptRoot),
        [string]$HoudiniVersion='',
        [ValidateSet('normal','normal-no-iris','solaris')][string]$Preset='normal'
    )
    $source=[IO.Path]::GetFullPath($SourceRoot).TrimEnd('\','/')
    $presets=Get-Content -LiteralPath (Join-Path $source 'CMakePresets.json') -Raw | ConvertFrom-Json
    $selected=@($presets.configurePresets | Where-Object {$_.name -eq $Preset})
    if($selected.Count -ne 1 -or -not $selected[0].binaryDir){throw "Missing build directory for preset $Preset"}
    $directory=$selected[0].binaryDir.Replace('${sourceDir}',$source).
        Replace('${sourceParentDir}',(Split-Path -Parent $source)).Replace('${sourceDirName}',(Split-Path -Leaf $source))
    if($directory.Contains('${')){throw "Unsupported build-directory macro in preset $Preset"}
    if($HoudiniVersion){
        if($HoudiniVersion -notmatch '^\d+\.\d+\.\d+$'){throw 'An exact Houdini build version is required.'}
        $directory=[regex]::Replace($directory,'(?<=windows-h)\d+\.\d+\.\d+(?=-)',$HoudiniVersion)
    }
    [IO.Path]::GetFullPath($directory)
}

# Before the one-folder layout (22 November 2026) the same cache was <parent>\<folder>-build\<same name>, next to
# the project folder. Such a folder is only named, never used and never deleted.
function Get-EarlierBuildDirectory {
    param([string]$SourceRoot=(Split-Path -Parent $PSScriptRoot), [Parameter(Mandatory=$true)][string]$BuildDirectory)
    $source=[IO.Path]::GetFullPath($SourceRoot).TrimEnd('\','/')
    $old=Join-Path (Join-Path (Split-Path -Parent $source) ((Split-Path -Leaf $source)+'-build')) (Split-Path -Leaf $BuildDirectory)
    if(Test-Path -LiteralPath $old -PathType Container){return [IO.Path]::GetFullPath($old)}
    return $null
}
function Get-EarlierBuildNotice {
    param([string]$SourceRoot=(Split-Path -Parent $PSScriptRoot), [Parameter(Mandatory=$true)][string]$BuildDirectory)
    $old=Get-EarlierBuildDirectory -SourceRoot $SourceRoot -BuildDirectory $BuildDirectory
    if(-not $old){return $null}
    return "An earlier build folder from before the one-folder layout exists at $old; it is no longer used (builds now go to $([IO.Path]::GetFullPath($BuildDirectory))) and can be deleted once no Houdini runs from it."
}
