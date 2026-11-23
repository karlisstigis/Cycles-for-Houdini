# SPDX-License-Identifier: Apache-2.0
[CmdletBinding()]
param(
    [string]$BuildDirectory='',
    [string]$PackageDirectory='',
    [string]$Artifacts='',
    [string]$OptiXDevice='CUDA_NVIDIA GeForce RTX 4090_0000:03:00_OptiX'
)
$ErrorActionPreference='Stop'
$taskRoot=Split-Path -Parent $PSScriptRoot
if(-not $Artifacts){$Artifacts=Join-Path $taskRoot 'artifacts/ramp-curve-k33/production'}
$Artifacts=[IO.Path]::GetFullPath($Artifacts)
$k33Outcomes=[Collections.Generic.List[object]]::new()
function Invoke-Gate([string]$Test,[string]$Device,[string]$Report,[string]$Extra=''){
    $gateArgs=@{Test=$Test;Device=$Device;Execution='worker';TimeoutSeconds=240;
        Artifacts=$Artifacts;Report=$Report}
    if($BuildDirectory){$gateArgs.BuildDirectory=$BuildDirectory}
    if($PackageDirectory){$gateArgs.PackageDirectory=$PackageDirectory}
    if($Extra){$gateArgs.TestArguments=@('--report',$Extra)}
    & (Join-Path $PSScriptRoot 'run-prototype-test.ps1') @gateArgs
    $k33Outcomes.Add([ordered]@{test=$Test;device=$Device;exit_code=$LASTEXITCODE;
        result=$(if($LASTEXITCODE -eq 0){'passed'}else{'failed'})})
}
foreach($backend in @('CPU','OptiX')){
    $device=if($backend -eq 'CPU'){'CPU'}else{$OptiXDevice}
    Invoke-Gate 'houdini_ramp_curve_blender_smoke.py' $device ('artifacts/stage3/materials/ramp-curve-blender/'+$backend+'.json')
    $shutter=Join-Path $Artifacts ('shutter/'+$backend+'.json')
    Invoke-Gate 'houdini_ramp_shutter_parity.py' $device $shutter $shutter
}
& python (Join-Path $taskRoot 'tests/ramp_curve_parity_gate.py') --runtime-reports (Join-Path $Artifacts 'stage3/materials/ramp-curve-blender') --shutter-reports (Join-Path $Artifacts 'shutter')
$parityExit=$LASTEXITCODE
$passed=$parityExit -eq 0 -and @($k33Outcomes | Where-Object exit_code -ne 0).Count -eq 0
$summary=[ordered]@{result=$(if($passed){'passed'}else{'failed'});tests=@($k33Outcomes);parity_exit_code=$parityExit}
$summary | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $Artifacts 'summary.json') -Encoding utf8
if(-not $passed){throw 'K33 regression failed; all four test outcomes are recorded in summary.json.'}
