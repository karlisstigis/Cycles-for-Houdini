# SPDX-License-Identifier: Apache-2.0
# PowerShell enumerates an empty array in an if expression to $null. Preserve
# zero arguments as zero, while retaining intentionally empty string values.
function Get-NativeTestArguments {
    param([AllowNull()][string[]]$Arguments=@())
    @($Arguments | Where-Object {$null -ne $_})
}

function Get-NativeHeadlessLaunchArguments {
    param([string]$EntryPoint, [string]$Source, [AllowNull()][string[]]$Arguments=@())
    @($EntryPoint,$Source)+@(Get-NativeTestArguments -Arguments $Arguments)
}

function Resolve-NativeTestReport {
    param([string]$Report, [string]$SourceRoot, [string]$ArtifactsRoot)
    if([IO.Path]::IsPathRooted($Report)){
        return [pscustomobject]@{Path=$Report;Route=$null}
    }
    if($ArtifactsRoot){
        $relative=if($Report.StartsWith('artifacts/')){$Report.Substring(10)}else{$Report}
        $redirected=Join-Path $ArtifactsRoot $relative
        return [pscustomobject]@{Path=$redirected;Route='shared-resolver'}
    }
    [pscustomobject]@{Path=(Join-Path $SourceRoot $Report);Route=$null}
}

function Get-NativeReleaseArguments {
    param([string[]]$Arguments, [string]$Report, [string]$SourceRoot, [string]$ArtifactsRoot)
    $result=@($Arguments)
    $index=[Array]::IndexOf($result,'--report')+1
    if($Report -and $index -gt 0 -and $index -lt $result.Count){
        # Matrix plans spell out the report's source-tree path. When the runner
        # relocates evidence, the fixture must write to the same path it audits.
        $planned=Resolve-NativeTestReport -Report $Report -SourceRoot $SourceRoot
        if([IO.Path]::IsPathFullyQualified($result[$index]) -and [IO.Path]::GetFullPath($result[$index]) -eq [IO.Path]::GetFullPath($planned.Path)){
            $result[$index]=(Resolve-NativeTestReport -Report $Report -SourceRoot $SourceRoot -ArtifactsRoot $ArtifactsRoot).Path
        }
    }
    $result
}
