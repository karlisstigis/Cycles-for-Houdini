# SPDX-License-Identifier: Apache-2.0
function Get-NativeTestToolsContext {
    param([string]$Wheelhouse='', [string]$Target='')
    $repo=Split-Path -Parent $PSScriptRoot
    $requirements=Join-Path $repo 'tests/requirements.txt'
    $digest=(Get-FileHash -LiteralPath $requirements -Algorithm SHA256).Hash.ToLowerInvariant()
    # Desktop-packaged tools (MSIX apps) and the ordinary user session can see different
    # virtualized LOCALAPPDATA directories, so the tools never live there. They live in the
    # project folder, which every session sees the same way: <project>\_work\test-tools\<digest>,
    # shared by the project's linked worktrees (scripts/project-layout.ps1). Until 22 November
    # 2026 this was %USERPROFILE%\.cycles-houdini\TestTools. A copied subset of the scripts (tests,
    # execution-source snapshots) without project-layout.ps1 uses its own folder's _work.
    $layout=Join-Path $PSScriptRoot 'project-layout.ps1'
    $work=if(Test-Path -LiteralPath $layout -PathType Leaf){. $layout; Get-ProjectWorkPath -Name 'test-tools' -SourceRoot $repo}else{Join-Path $repo '_work/test-tools'}
    $toolsRoot=Join-Path $work $digest
    if(-not $Wheelhouse){$Wheelhouse=Join-Path $toolsRoot 'wheelhouse'}
    if(-not $Target){$Target=Join-Path $toolsRoot 'dependencies'}
    [pscustomobject]@{Requirements=$requirements;RequirementsSHA256=$digest;Wheelhouse=[IO.Path]::GetFullPath($Wheelhouse);Target=[IO.Path]::GetFullPath($Target)}
}
