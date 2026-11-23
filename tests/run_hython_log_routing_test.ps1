# SPDX-License-Identifier: Apache-2.0
# Run the real launcher with an in-process fake command; no Houdini is started.
$ErrorActionPreference='Stop'
$repo=Split-Path -Parent $PSScriptRoot
. (Join-Path $repo 'tests/support/artifacts.ps1')
$base=Join-Path (Get-TestArtifactsRoot -SourceRoot $repo) ('hython-log-routing/'+[guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path (Join-Path $base 'scripts') -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $repo 'scripts/run-hython.ps1') -Destination (Join-Path $base 'scripts/run-hython.ps1')
Copy-Item -LiteralPath (Join-Path $repo 'scripts/test-artifact-context.ps1') -Destination (Join-Path $base 'scripts')
@'
function Get-NativeTestContext {
    param($BuildDirectory,$HoudiniRoot,$PackageDirectory)
    $root=Split-Path -Parent $PSScriptRoot
    [pscustomobject]@{BuildDirectory=(Join-Path $root 'selected-build');HoudiniRoot=(Join-Path $root 'fake-hfs');
        PackageDirectory=(Join-Path $root 'packages');RuntimeIdentity=@{release=$global:hythonLogCase.release}}
}
'@ | Set-Content -LiteralPath (Join-Path $base 'scripts/native-test-context.ps1')
$mock=Join-Path $base 'fake-hfs/bin/hython.exe'
Set-Item -LiteralPath ('Function:\'+$mock) -Value {
    if($env:HOUDINI_PATH -ne $global:hythonExpectedPath){throw 'Launcher hid Houdini defaults or discarded an explicit search path'}
    $global:hythonLogObserved=$env:CYCLES_HOUDINI_WORKER_LOG_DIR
    if(-not $global:hythonLogObserved){
        $global:hythonLogObserved=Join-Path $env:CYCLES_HOUDINI_SOURCE_ROOT 'artifacts/workers'
    }
    if($env:HOUDINI_USER_PREF_DIR -notlike '*selected-build*dev-prefs*__HVER__*'){throw 'Development preferences left selected build'}
    $global:LASTEXITCODE=23
}
$saved=@{}
foreach($name in @('CYCLES_HOUDINI_WORKER_LOG_DIR','CYCLES_HOUDINI_TEST_ARTIFACTS','CYCLES_HOUDINI_SOURCE_ROOT',
    'CYCLES_HOUDINI_TEST_IDENTITY','CYCLES_HOUDINI_TEST_IDENTITY_REPORT','HOUDINI_PACKAGE_DIR','HOUDINI_USER_PREF_DIR',
    'HOUDINI_NO_ENV_FILE','HOUDINI_DSO_ERROR','HOUDINI_PATH')){
    $saved[$name]=[Environment]::GetEnvironmentVariable($name,'Process')
}
$selected=Join-Path $base 'test artifacts'
$explicit=Join-Path $base 'explicit worker logs'
$data=Join-Path $base 'installed-data'
$cases=@(
    @{release='';artifacts='';workers='';expected=(Join-Path $base 'selected-build/workers')},
    @{release='';artifacts=$selected;workers='';expected=(Join-Path $selected 'workers')},
    @{release='';artifacts=$selected;workers=$explicit;expected=$explicit},
    @{release='installed-id';artifacts='';workers='';expected=(Join-Path $data 'artifacts/workers')},
    @{release='installed-id';artifacts=$selected;workers='';expected=(Join-Path $selected 'workers')},
    @{release='installed-id';artifacts=$selected;workers=$explicit;expected=$explicit}
)
try{
    $i=0
    foreach($case in $cases){
        switch($i++ % 3){
            0 {Remove-Item -LiteralPath Env:HOUDINI_PATH -ErrorAction SilentlyContinue}
            1 {$env:HOUDINI_PATH=''}
            2 {$env:HOUDINI_PATH='custom;&'}
        }
        $selectedPath=[Environment]::GetEnvironmentVariable('HOUDINI_PATH')
        $global:hythonExpectedPath=if($selectedPath){$selectedPath}else{'&'}
        $global:hythonLogCase=$case
        $env:CYCLES_HOUDINI_WORKER_LOG_DIR=$case.workers
        $env:CYCLES_HOUDINI_TEST_ARTIFACTS=$case.artifacts
        $env:CYCLES_HOUDINI_SOURCE_ROOT=$data
        & (Join-Path $base 'scripts/run-hython.ps1') 'never-executed.py'
        if(-not [object]::Equals($selectedPath,[Environment]::GetEnvironmentVariable('HOUDINI_PATH'))){throw 'Launcher changed caller search path'}
        if($LASTEXITCODE -ne 23){throw 'Launcher lost the native exit code'}
        if($global:hythonLogObserved -ne $case.expected){throw "Worker log routing mismatch: $global:hythonLogObserved; expected $($case.expected)"}
        if($env:CYCLES_HOUDINI_WORKER_LOG_DIR -ne $case.workers){throw 'Launcher leaked its default worker log override'}
    }
    if(Test-Path -LiteralPath (Join-Path $base 'artifacts')){throw 'Launcher created checkout artifacts'}
}finally{
    foreach($name in $saved.Keys){
        if($null -eq $saved[$name]){Remove-Item -LiteralPath ('Env:'+$name) -ErrorAction SilentlyContinue}
        else{[Environment]::SetEnvironmentVariable($name,$saved[$name],'Process')}
    }
    Remove-Item -LiteralPath ('Function:\'+$mock)
    Remove-Variable -Name hythonLogCase,hythonLogObserved,hythonExpectedPath -Scope Global -ErrorAction SilentlyContinue
}
@{result='passed';native_processes_started=0;cases=$cases.Count;
    checks=@('development logs in selected build','selected test artifacts','explicit worker override',
        'installed data default','installed test override','exit code and environment preserved','no checkout artifacts')} | ConvertTo-Json
