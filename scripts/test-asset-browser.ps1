# SPDX-License-Identifier: Apache-2.0
# Manual asset browser trial, run from PowerShell 7 in the project folder.
#  1. Rebuilds incrementally and stages a fresh development host, so Python and
#     UI changes are picked up (native targets are only rebuilt when changed).
#  2. Once, creates a small sample Blender asset library with Blender and checks
#     the Blender side (scan, previews, material export, worker protocol).
#  3. Opens a separate Houdini with the development build and the asset browser
#     docked. Libraries, cache and panel settings of these trials persist in
#     <project>\_work\asset-browser-trial; the library folders
#     themselves are never written.
# -SkipBuild reuses the last staged build; -RebuildSample recreates the sample
# library; -WithoutAssetBrowser starts the build as an installation without the
# optional asset browser component.
[CmdletBinding()]
param([switch]$SkipBuild, [switch]$RebuildSample, [switch]$WithoutAssetBrowser,
      [string]$HoudiniRoot='', [string]$BuildDirectory='')
$ErrorActionPreference='Stop'
$root=Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'project-layout.ps1')
# Inside the project folder (until 22 November 2026: %USERPROFILE%\.cycles-houdini\asset-browser-trial).
$trial=Get-ProjectWorkPath -Name 'asset-browser-trial' -SourceRoot $root
New-Item -ItemType Directory -Force -Path $trial | Out-Null
$selection=@{}
if($HoudiniRoot){$selection.HoudiniRoot=$HoudiniRoot}
if($BuildDirectory){$selection.BuildDirectory=$BuildDirectory}

function Find-Blender {
    # Same order as core/blender_bridge.py: CYCLES_HOUDINI_BLENDER, PATH, newest Program Files install.
    if($env:CYCLES_HOUDINI_BLENDER){
        if(Test-Path -LiteralPath $env:CYCLES_HOUDINI_BLENDER -PathType Leaf){return $env:CYCLES_HOUDINI_BLENDER}
        return $null
    }
    $found=Get-Command blender.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if($found){return $found.Source}
    $foundation=Join-Path $env:ProgramFiles 'Blender Foundation'
    if(-not(Test-Path -LiteralPath $foundation)){return $null}
    $installed=@(Get-ChildItem -LiteralPath $foundation -Directory | ForEach-Object {
        $exe=Join-Path $_.FullName 'blender.exe'
        if(Test-Path -LiteralPath $exe -PathType Leaf){
            $numbers=@([regex]::Matches($_.Name,'\d+') | Select-Object -First 3 | ForEach-Object {[int]$_.Value})
            [pscustomobject]@{Path=$exe;Key=(($numbers+@(0,0,0))[0..2] | ForEach-Object {$_.ToString('00000')}) -join '.'}
        }
    } | Sort-Object Key -Descending)
    if($installed.Count){return $installed[0].Path}
    return $null
}

if(-not $SkipBuild){
    Write-Host 'Step 1/3: rebuilding and staging the development build (incremental)...'
    & (Join-Path $PSScriptRoot 'build.ps1') @selection
} else {
    Write-Host 'Step 1/3: skipped; using the last staged development build.'
}

$sample=Join-Path $trial 'sample'
$library=Join-Path $sample 'library'
if($RebuildSample -or -not(Test-Path -LiteralPath (Join-Path $library 'assets.blend'))){
    $blender=Find-Blender
    if($blender){
        Write-Host "Step 2/3: creating the sample asset library and checking the Blender side with $blender ..."
        & $blender --background --factory-startup --python-exit-code 1 --python (Join-Path $root 'tests/blend_asset_scan_smoke.py') -- --output $sample
        if($LASTEXITCODE -ne 0){
            Write-Warning 'The Blender-side asset check failed (see the output above). Houdini still opens; the sample library may be missing or incomplete.'
        }
    } else {
        Write-Warning 'Blender was not found (set CYCLES_HOUDINI_BLENDER or install Blender). Houdini still opens; add your own library with Libraries...'
    }
} else {
    Write-Host "Step 2/3: sample library already exists ($library)."
}

Write-Host 'Step 3/3: opening Houdini with the development build...'
$previous=@{}
foreach($name in 'CYCLES_HOUDINI_ASSET_LIBRARIES_FILE','CYCLES_HOUDINI_ASSET_CACHE','CYCLES_HOUDINI_ASSET_BROWSER_SETTINGS_FILE','CYCLES_HOUDINI_ASSET_SAMPLE_LIBRARY'){
    $previous[$name]=[Environment]::GetEnvironmentVariable($name,'Process')
}
try {
    # Trial sessions use fresh Houdini preferences; keep the browser's own state stable between them.
    $env:CYCLES_HOUDINI_ASSET_LIBRARIES_FILE=Join-Path $trial 'asset_libraries.json'
    $env:CYCLES_HOUDINI_ASSET_CACHE=Join-Path $trial 'cache'
    $env:CYCLES_HOUDINI_ASSET_BROWSER_SETTINGS_FILE=Join-Path $trial 'settings.json'
    $env:CYCLES_HOUDINI_ASSET_SAMPLE_LIBRARY=if(Test-Path -LiteralPath (Join-Path $library 'assets.blend')){$library}else{''}
    $launch=@{StartupScript=(Join-Path $PSScriptRoot 'asset-browser-trial-startup.py')}+$selection
    if($WithoutAssetBrowser){$launch.WithoutAssetBrowser=$true}
    & (Join-Path $PSScriptRoot 'launch-normal-build.ps1') @launch
} finally {
    foreach($name in $previous.Keys){[Environment]::SetEnvironmentVariable($name,$previous[$name],'Process')}
}
