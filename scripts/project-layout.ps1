# SPDX-License-Identifier: Apache-2.0
# One folder for everything (docs/dev/README.md, "Working folders"). What the developer route makes goes inside the
# project folder, in one working folder that Git ignores: <project>\_work. <project> is the main working tree of the
# repository a checkout belongs to: the checkout itself, or, for a linked worktree (the reviewed copy, a working copy
# for one change, both inside <project>\_work), the main checkout it was made from. A folder that is not the top of its own
# Git working tree (a ZIP download, a made-up test tree) is its own project. Never a drive letter, a sibling folder
# or the user profile.
function Get-ProjectRoot {
    param([string]$SourceRoot=(Split-Path -Parent $PSScriptRoot))
    $source=[IO.Path]::GetFullPath($SourceRoot).TrimEnd('\','/')
    $top=$null; $common=$null
    try{
        $global:LASTEXITCODE=0
        $top=& git -C $source rev-parse --show-toplevel 2>$null | Select-Object -First 1
        if($LASTEXITCODE -eq 0){$common=& git -C $source rev-parse --path-format=absolute --git-common-dir 2>$null | Select-Object -First 1}
    }catch{return $source}
    if($LASTEXITCODE -ne 0 -or -not $top -or -not $common){return $source}
    if([IO.Path]::GetFullPath($top).TrimEnd('\','/') -ne $source){return $source}
    $common=[IO.Path]::GetFullPath($common).TrimEnd('\','/')
    if((Split-Path -Leaf $common) -ne '.git'){return $source}
    return (Split-Path -Parent $common)
}

# <project>\_work\<Name>: dev (developer build cache, from CMakePresets.json), reviewed, reviewed-build, releases,
# wt (working copies for single changes), ci, recovery, test-runs, test-artifacts, test-tools, asset-browser-trial, tests (fresh-download
# checks). The one-file builder adds windows-h<build>-release, cuda-<version> and installer-details (easy-common.ps1).
function Get-ProjectWorkPath {
    param([Parameter(Mandatory=$true)][string]$Name, [string]$SourceRoot=(Split-Path -Parent $PSScriptRoot))
    Join-Path (Join-Path (Get-ProjectRoot -SourceRoot $SourceRoot) '_work') $Name
}

# True when Path is inside <project>\_work (ignored by Git; nothing scans it).
function Test-InProjectWork {
    param([Parameter(Mandatory=$true)][string]$Path, [string]$SourceRoot=(Split-Path -Parent $PSScriptRoot))
    $work=[IO.Path]::GetFullPath((Join-Path (Get-ProjectRoot -SourceRoot $SourceRoot) '_work')).TrimEnd('\','/')
    $full=[IO.Path]::GetFullPath($Path).TrimEnd('\','/')
    return ($full -eq $work -or $full.StartsWith($work+[IO.Path]::DirectorySeparatorChar,[StringComparison]::OrdinalIgnoreCase))
}

# --- Links (directory junctions, symbolic links) ------------------------------------------------------------------
# Git for Windows ("git worktree remove", "git clean"), naive recursive deletes and robocopy without /XJ follow
# directory junctions and delete or copy what they point at; on 22 November 2026 that emptied third_party folders of
# the main checkout through a working copy's junctions. Everything below handles a link as a link: it is listed,
# removed or reported, never entered.
function Test-ProjectLink([IO.FileSystemInfo]$Item) {
    return [bool]($Item.Attributes -band [IO.FileAttributes]::ReparsePoint)
}
# Every link inside Root (Root itself excluded), found without entering any link.
function Get-ProjectLinks {
    param([Parameter(Mandatory=$true)][string]$Root)
    $found=[Collections.Generic.List[string]]::new()
    $pending=[Collections.Generic.Stack[string]]::new()
    $pending.Push([IO.Path]::GetFullPath($Root))
    while($pending.Count){
        $directory=[IO.DirectoryInfo]::new($pending.Pop())
        foreach($child in $directory.EnumerateFileSystemInfos()){
            if(Test-ProjectLink $child){$found.Add($child.FullName); continue}
            if($child -is [IO.DirectoryInfo]){$pending.Push($child.FullName)}
        }
    }
    # Plain return (not ",$array"): callers count with @(...).Count, which must be 0 when nothing is found.
    return $found.ToArray()
}
# Removes one link itself; what it points at is not touched.
function Remove-ProjectLink {
    param([Parameter(Mandatory=$true)][string]$Path)
    $item=Get-Item -LiteralPath $Path -Force
    if(-not (Test-ProjectLink $item)){throw "Not a link: $Path"}
    if($item -is [IO.DirectoryInfo]){[IO.Directory]::Delete($item.FullName,$false)}else{[IO.File]::Delete($item.FullName)}
}
# Removes every link inside Root, then checks that none is left. Returns the removed links.
function Remove-ProjectLinks {
    param([Parameter(Mandatory=$true)][string]$Root)
    $links=@(Get-ProjectLinks -Root $Root)
    foreach($link in $links){Remove-ProjectLink -Path $link}
    $left=@(Get-ProjectLinks -Root $Root)
    if($left.Count){throw "Links are still inside ${Root}: $($left -join ', ')"}
    return $links
}
# Deletes a folder tree without ever entering a link (links inside are removed as links).
function Remove-ProjectTree {
    param([Parameter(Mandatory=$true)][string]$Root)
    $top=Get-Item -LiteralPath $Root -Force -ErrorAction SilentlyContinue
    if(-not $top){return}
    if(Test-ProjectLink $top){Remove-ProjectLink -Path $top.FullName; return}
    [void](Remove-ProjectLinks -Root $top.FullName)
    foreach($file in $top.EnumerateFiles('*',[IO.SearchOption]::AllDirectories)){
        if($file.Attributes -band [IO.FileAttributes]::ReadOnly){$file.Attributes=[IO.FileAttributes]::Normal}
    }
    [IO.Directory]::Delete($top.FullName,$true)
}
