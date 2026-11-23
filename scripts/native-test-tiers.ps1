# SPDX-License-Identifier: Apache-2.0
# Compatibility entry point; Python owns tier/backend/dependency planning.
. (Join-Path $PSScriptRoot 'native-test-arguments.ps1')
function Get-NativeTierCases {
    param([string]$Tier, [string]$Destination, [string]$NightlySelectionFile='', [string]$CatalogueSelectionFile='')
    $arguments=@((Join-Path $PSScriptRoot 'native_test_plan.py'),'--tier',$Tier,'--destination',$Destination)
    if($NightlySelectionFile){$arguments+=@('--nightly-selection',$NightlySelectionFile)}
    if($CatalogueSelectionFile){$arguments+=@('--catalogue-selection',$CatalogueSelectionFile)}
    $output=& python @arguments
    if($LASTEXITCODE -ne 0){throw 'Native Python tier planning failed.'}
    $output | ConvertFrom-Json | ForEach-Object {$_}
}
