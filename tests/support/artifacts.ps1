# SPDX-License-Identifier: Apache-2.0
# Match tests/support/paths.py without changing the implementation source root.
function Get-TestArtifactsRoot {
    param([Parameter(Mandatory=$true)][string]$SourceRoot)
    if($env:CYCLES_HOUDINI_TEST_ARTIFACTS){
        $testArtifactsRoot=$env:CYCLES_HOUDINI_TEST_ARTIFACTS
    }elseif($env:CYCLES_HOUDINI_SOURCE_ROOT -and [IO.Path]::GetFullPath($env:CYCLES_HOUDINI_SOURCE_ROOT) -ne [IO.Path]::GetFullPath($SourceRoot)){
        $testArtifactsRoot=Join-Path $env:CYCLES_HOUDINI_SOURCE_ROOT 'artifacts'
    }else{
        $testArtifactsRoot=Join-Path (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)) '_work/test-artifacts'
    }
    New-Item -ItemType Directory -Path $testArtifactsRoot -Force | Out-Null
    [IO.Path]::GetFullPath($testArtifactsRoot)
}
