# SPDX-License-Identifier: Apache-2.0
[CmdletBinding()]
param(
    [ValidateSet('furnace','bias','cross-backend','equal-time','nightly','everything')]
    [Parameter(Mandatory=$true)][string]$Suite,
    [string]$BuildDirectory='',
    [string]$HoudiniRoot='',
    [string]$StockBuildDirectory='',
    [string]$RisScene='',
    [string]$RisIrisImage='',
    [string]$OutputDirectory='',
    [switch]$Preflight
)
$ErrorActionPreference='Stop'
$root=Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'native-test-context.ps1')
$context=Get-NativeTestContext -BuildDirectory $BuildDirectory -HoudiniRoot $HoudiniRoot
if(-not $OutputDirectory){$OutputDirectory=Join-Path $root ('artifacts/heavy-renderer/'+[DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfff'))}
$OutputDirectory=[IO.Path]::GetFullPath($OutputDirectory)
if(-not $Preflight -and (Test-Path -LiteralPath $OutputDirectory)){
    if(-not(Test-Path -LiteralPath $OutputDirectory -PathType Container)){
        throw "Heavy renderer output path is not a directory: $OutputDirectory"
    }
    if(Get-ChildItem -LiteralPath $OutputDirectory -Force | Select-Object -First 1){
        throw "Heavy renderer output directory is not empty: $OutputDirectory"
    }
}
New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null
$selection=if($Suite -eq 'everything'){@('furnace','bias','cross-backend','equal-time','nightly')}else{@($Suite)}
$stockExe=$null
if('bias' -in $selection){
    if(-not $StockBuildDirectory){
        $pinned=(Get-Content -LiteralPath (Join-Path $root 'source.lock.json') -Raw | ConvertFrom-Json).cycles.commit
        $StockBuildDirectory=Join-Path $root ('build/stock-cycles-'+$pinned.Substring(0,8))
    }
    $receiptPath=Join-Path $StockBuildDirectory 'stock-receipt.json'
    if(-not(Test-Path -LiteralPath $receiptPath -PathType Leaf)){
        throw "Stock Cycles receipt is missing at $receiptPath. Run scripts/build-stock-cycles.ps1 first."
    }
    $stock=Get-Content -LiteralPath $receiptPath -Raw | ConvertFrom-Json
    $upstream=(Get-Content -LiteralPath (Join-Path $root 'source.lock.json') -Raw | ConvertFrom-Json).cycles.commit
    if($stock.source_commit -ne $upstream -or $stock.source_marker -ne $upstream -or $stock.archive_sha256 -notmatch '^[0-9a-f]{64}$'){
        throw 'Stock runtime is not the pinned pristine Cycles commit.'
    }
    $archiveMarker=Join-Path $StockBuildDirectory 'source/.stock-archive-sha256'
    if(-not(Test-Path -LiteralPath $archiveMarker) -or (Get-Content -LiteralPath $archiveMarker -Raw).Trim() -ne $stock.archive_sha256){
        throw 'Stock source archive digest differs from its build receipt.'
    }
    $stockExe=[IO.Path]::GetFullPath($stock.executable)
    if(-not(Test-Path -LiteralPath $stockExe -PathType Leaf) -or
       (Get-FileHash -LiteralPath $stockExe -Algorithm SHA256).Hash.ToLowerInvariant() -ne $stock.executable_sha256){
        throw 'Stock CLI differs from its pinned build receipt.'
    }
    $forkManifest=Get-Content -LiteralPath $context.Manifest -Raw | ConvertFrom-Json
    $forkExe=Join-Path $context.BuildDirectory 'bin/cycles.exe'
    $forkRecord=@($forkManifest.binary_artifacts | Where-Object {$_.path.Replace('\','/') -eq 'bin/cycles.exe'})
    if($forkRecord.Count -ne 1 -or (Get-FileHash -LiteralPath $forkExe -Algorithm SHA256).Hash.ToLowerInvariant() -ne $forkRecord[0].sha256){
        throw 'Fork CLI differs from the staged build manifest.'
    }
    $bundle=Join-Path $root 'third_party/cycles/lib/windows_x64'
    # The stock CLI is built with the fork's Embree (build-stock-cycles.ps1); embree4.dll
    # imports sycl9.dll from dpcpp, which loads level-zero only for GPU devices.
    $stockDlls=@('OpenImageIO','opencolorio','openexr','imath','openjph','aom','tbb','embree','dpcpp','level-zero') |
        ForEach-Object {Join-Path $bundle "$_/bin"}
    $forkDlls=@((Join-Path $context.HoudiniRoot 'bin'))
    foreach($part in @('pthreads','embree','dpcpp','level-zero','epoxy','tbb')){
        $forkDlls+=Join-Path $bundle "$part/bin"
    }
    # Unlike BVH backends (BVH2 versus Embree) differ in last-bit intersection results,
    # which the bias rows would report as engine bias. Refuse such a pair before capture;
    # run-heavy-bias.py records each image's backend and the analysis checks it again.
    & python (Join-Path $PSScriptRoot 'cycles_cli_backend.py') check --stock $stockExe `
        --stock-search-path (($stockDlls+@($env:PATH)) -join ';') --fork $forkExe `
        --fork-search-path (($forkDlls+@($env:PATH)) -join ';') --stock-receipt $receiptPath
    if($LASTEXITCODE -ne 0){
        throw 'Refusing to compare unlike stock and fork Cycles builds (ray-tracing backend differs; see the message above).'
    }
}
$entry=Join-Path $root 'tests/heavy_renderer_suites.py'
if($Preflight){
    Write-Host (($selection | ConvertTo-Json -Compress))
    return
}
function Invoke-HeavyCase([string]$Name,[string]$Role,[object]$SelectedContext,[switch]$Analyze){
    $previous=@{}
    foreach($key in @('HOUDINI_PACKAGE_DIR','HOUDINI_PATH','HOUDINI_USER_PREF_DIR','HOUDINI_NO_ENV_FILE','PYTHONDONTWRITEBYTECODE','CYCLES_HOUDINI_TEST_IDENTITY')){
        $previous[$key]=[Environment]::GetEnvironmentVariable($key,'Process')
    }
    try{
        $env:HOUDINI_PACKAGE_DIR=$SelectedContext.PackageDirectory
        $env:HOUDINI_PATH='&'
        $env:HOUDINI_USER_PREF_DIR=Join-Path $OutputDirectory ('prefs-'+$Name+'-'+$Role+'/houdini__HVER__')
        $env:HOUDINI_NO_ENV_FILE='1'
        $env:PYTHONDONTWRITEBYTECODE='1'
        $env:CYCLES_HOUDINI_TEST_IDENTITY=$SelectedContext.RuntimeIdentity | ConvertTo-Json -Compress
        $arguments=@($entry,'--suite',$Name,'--output',(Join-Path $OutputDirectory $Name),'--role',$Role)
        if($Name -eq 'equal-time' -and $RisScene){$arguments+=@('--ris-scene',$RisScene)}
        if($Name -eq 'equal-time' -and $RisIrisImage){$arguments+=@('--ris-iris-image',$RisIrisImage)}
        if($Analyze){$arguments+='--analyze'}
        & (Join-Path $SelectedContext.HoudiniRoot 'bin/hython.exe') @arguments
        if($LASTEXITCODE -ne 0){throw "$Name $Role failed with exit code $LASTEXITCODE"}
    }finally{
        foreach($key in $previous.Keys){[Environment]::SetEnvironmentVariable($key,$previous[$key],'Process')}
    }
}
foreach($name in $selection){
    if($name -eq 'nightly'){
        & python (Join-Path $PSScriptRoot 'run-nightly.py') run --run (Join-Path $OutputDirectory 'nightly') --build-directory $context.BuildDirectory --houdini-root $context.HoudiniRoot --jobs 1
        if($LASTEXITCODE -ne 0){throw 'Nightly failed'}
        continue
    }
    if($name -eq 'bias'){
        $bias=Join-Path $OutputDirectory 'bias'
        $script=Join-Path $PSScriptRoot 'run-heavy-bias.py'
        $oldPath=$env:PATH
        $hython=Join-Path $context.HoudiniRoot 'bin/hython.exe'
        $biasEnvironment=@{}
        foreach($key in @('HOUDINI_PACKAGE_DIR','HOUDINI_PATH','HOUDINI_USER_PREF_DIR',
                          'HOUDINI_NO_ENV_FILE','PYTHONDONTWRITEBYTECODE','CYCLES_HOUDINI_TEST_IDENTITY')){
            $biasEnvironment[$key]=[Environment]::GetEnvironmentVariable($key,'Process')
        }
        try{
            $env:HOUDINI_PACKAGE_DIR=$context.PackageDirectory
            $env:HOUDINI_PATH='&'
            $env:HOUDINI_USER_PREF_DIR=Join-Path $OutputDirectory 'prefs-bias-cli/houdini__HVER__'
            $env:HOUDINI_NO_ENV_FILE='1'
            $env:PYTHONDONTWRITEBYTECODE='1'
            $env:CYCLES_HOUDINI_TEST_IDENTITY=$context.RuntimeIdentity | ConvertTo-Json -Compress
            & $hython $script --source $root --output $bias --prepare
            if($LASTEXITCODE -ne 0){throw 'Bias XML preparation failed'}
            $env:PATH=($stockDlls -join ';')+';'+$oldPath
            & $hython $script --source $root --output $bias --executable $stockExe --role stock
            if($LASTEXITCODE -ne 0){throw 'Stock Cycles bias capture failed'}
            $env:PATH=($forkDlls -join ';')+';'+$oldPath
            & $hython $script --source $root --output $bias --executable $forkExe --role fork
            if($LASTEXITCODE -ne 0){throw 'Fork Cycles bias capture failed'}
        }finally{
            $env:PATH=$oldPath
            foreach($key in $biasEnvironment.Keys){
                [Environment]::SetEnvironmentVariable($key,$biasEnvironment[$key],'Process')
            }
        }
        Invoke-HeavyCase $name 'fork' $context
        Invoke-HeavyCase $name 'fork' $context -Analyze
        continue
    }
    Invoke-HeavyCase $name 'fork' $context
    Invoke-HeavyCase $name 'fork' $context -Analyze
}
Write-Host "Heavy renderer selection complete: $OutputDirectory"
