# SPDX-License-Identifier: Apache-2.0
# Evidence defaults are inside this checkout's working folder <checkout>\_work (ignored by Git, never scanned;
# docs/dev/README.md, "Working folders"): test-runs for run folders, test-artifacts for the shared legacy fixture root.
# Until 22 November 2026 they were %LOCALAPPDATA%\CyclesForHoudini\TestRuns and TestArtifacts. Explicit paths and
# CYCLES_HOUDINI_TEST_ARTIFACTS retain their existing meaning. Self-contained on purpose: tests copy this file alone.
function Get-TestEvidenceBase {
    param([string]$Artifacts='')
    if($Artifacts){return [IO.Path]::GetFullPath($Artifacts)}
    if($env:CYCLES_HOUDINI_TEST_ARTIFACTS){return [IO.Path]::GetFullPath($env:CYCLES_HOUDINI_TEST_ARTIFACTS)}
    Join-Path (Split-Path -Parent $PSScriptRoot) '_work/test-runs'
}

function Get-DefaultTestArtifactRoot {
    Join-Path (Split-Path -Parent $PSScriptRoot) '_work/test-artifacts'
}

function Save-TestProcessEnvironment {
    $saved=@{}
    foreach($entry in Get-ChildItem Env:){
        if($entry.Name -like 'CYCLES_HOUDINI_*' -or $entry.Name -like 'HOUDINI_*' -or $entry.Name -in @('PYTHONPATH','PYTHONDONTWRITEBYTECODE','TEMP','TMP','TMPDIR','OCIO')){$saved[$entry.Name]=$entry.Value}
    }
    $saved
}
function Restore-TestProcessEnvironment($Saved) {
    $current=Save-TestProcessEnvironment
    foreach($name in $current.Keys){
        # PS7/.NET can translate a null string to an empty value. The Env
        # provider's removal preserves the caller's genuinely absent variable.
        if(-not $Saved.ContainsKey($name)){Remove-Item -LiteralPath ('Env:'+$name)}
    }
    foreach($name in $Saved.Keys){
        if(-not $current.ContainsKey($name) -or $current[$name] -cne $Saved[$name]){
            Set-Item -LiteralPath ('Env:'+$name) -Value $Saved[$name]
        }
    }
}
