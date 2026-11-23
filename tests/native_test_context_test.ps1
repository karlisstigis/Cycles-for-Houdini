# SPDX-License-Identifier: Apache-2.0
# Pure qualification of target selection and relocated payload checks.
$ErrorActionPreference='Stop'
$repo=Split-Path -Parent $PSScriptRoot
. (Join-Path $repo 'tests/support/artifacts.ps1')
$testArtifacts=Get-TestArtifactsRoot -SourceRoot $repo
. (Join-Path $repo 'scripts/native-test-context.ps1')
$hython=Get-Command (Join-Path $repo 'scripts/run-hython.ps1')
$argument=$hython.ParameterSets[0].Parameters | Where-Object Name -EQ 'HythonArguments'
if($argument.Position -ne 0 -or -not $argument.ValueFromRemainingArguments){throw 'Hython script arguments must bind before optional context paths'}
$base=Join-Path $testArtifacts ('test-context/'+[guid]::NewGuid().ToString('N'))
$hfs=Join-Path $base 'hfs'
$build=Join-Path $base 'build'
$hostPath=Join-Path $base 'installed/host'
$packages=Join-Path $base 'packages'
foreach($p in @("$hfs/toolkit/include/SYS","$hostPath/python3.13libs",$build,$packages)){New-Item -ItemType Directory -Path $p -Force | Out-Null}
Set-Content "$hfs/toolkit/include/SYS/SYS_Version.h" '#define SYS_VERSION_FULL "22.0.466"'
$file=Join-Path $hostPath 'python3.13libs/runtime.py'
Set-Content $file 'original runtime'
$hash=(Get-FileHash $file).Hash.ToLowerInvariant()
$identity=@{host_sdk=@{houdini='22.0.466'};resource_root='hosts/frozen/resources';host_runtime=@(
    @{path='hosts/frozen/python3.13libs/runtime.py';sha256=$hash},
    @{path='hosts/frozen/dso/native.exp';sha256='deliberately unshipped'})}
$identity | ConvertTo-Json -Depth 6 | Set-Content "$build/build-manifest.json"
@{enable="houdini_version == '22.0.466'";hpath=@($hostPath);env=@(@{CYCLES_HOUDINI_HOST_ROOT=$hostPath})} | ConvertTo-Json -Depth 4 | Set-Content "$packages/cycles.json"
@{enable="houdini_version == '22.0.466'";hpath=@('unrelated studio package')} | ConvertTo-Json | Set-Content "$packages/studio.json"
$context=Get-NativeTestContext -BuildDirectory $build -HoudiniRoot $hfs -PackageDirectory $packages
if($context.RuntimeIdentity.host -ne $hostPath){throw 'Wrong runtime selected'}
# Development packages carry the optional asset browser add-on; launchers can drop it.
@{enable="houdini_version == '22.0.466'";hpath=@($hostPath,"$hostPath/addons/asset_browser");env=@(@{CYCLES_HOUDINI_HOST_ROOT=$hostPath})} | ConvertTo-Json -Depth 4 | Set-Content "$packages/cycles.json"
$withoutBrowser=New-PackageDirectoryWithoutAssetBrowser $packages (Join-Path $base 'packages-without-asset-browser')
$filtered=Get-Content (Join-Path $withoutBrowser 'cycles.json') -Raw | ConvertFrom-Json
if(@($filtered.hpath).Count -ne 1 -or @($filtered.hpath)[0] -ne $hostPath){throw 'Asset browser add-on path kept'}
if(($filtered.env | Where-Object {$_.PSObject.Properties.Name -contains 'CYCLES_HOUDINI_ASSET_BROWSER'}).CYCLES_HOUDINI_ASSET_BROWSER -ne '0'){throw 'Asset browser was not disabled'}
if((Get-Content (Join-Path $withoutBrowser 'studio.json') -Raw) -ne (Get-Content "$packages/studio.json" -Raw)){throw 'Unrelated package changed'}
if((Get-NativeTestContext -BuildDirectory $build -HoudiniRoot $hfs -PackageDirectory $withoutBrowser).RuntimeIdentity.host -ne $hostPath){throw 'Package copy without the asset browser selected another host'}
Set-Content $file 'changed runtime'
try{Get-NativeTestContext -BuildDirectory $build -HoudiniRoot $hfs -PackageDirectory $packages;throw 'Accepted wrong bytes'}catch{if($_.Exception.Message -eq 'Accepted wrong bytes'){throw}}
Set-Content $file 'original runtime'
Set-Content "$hfs/toolkit/include/SYS/SYS_Version.h" '#define SYS_VERSION_FULL "22.0.429"'
try{Get-NativeTestContext -BuildDirectory $build -HoudiniRoot $hfs -PackageDirectory $packages;throw 'Accepted wrong target'}catch{if($_.Exception.Message -eq 'Accepted wrong target'){throw}}
@{result='passed';checks=@('Exact product selected alongside unrelated studio package','Asset browser add-on dropped from a development package copy','Unshipped development artifacts excluded','Changed payload refused','Different Houdini build refused')} | ConvertTo-Json | Set-Content "$base/report.json"
Get-Content "$base/report.json"
