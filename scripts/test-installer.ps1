# SPDX-License-Identifier: Apache-2.0
<#
Exercise a built Setup.exe end to end in an isolated folder. Never touches the
real Houdini preferences. Close Houdini first: uninstall refuses while it runs.

  1. Fresh install (silent) -> package file present, install tool status passes,
     a headless hython with these preferences loads the Cycles operators from the
     installed copy.
  2. Re-run Setup over it (repair/upgrade path) -> status still passes.
  3. Uninstall -> package file and installed files removed.
  4. A hand-made package file already present -> Setup with /REPLACEEXISTING=1
     replaces it; uninstall restores the original bytes exactly.
  0. First, a fake Houdini 22.0.999 folder is refused before anything is installed.
#>
[CmdletBinding()]
param([string]$Setup='',[string]$HoudiniRoot='C:\Program Files\Side Effects Software\Houdini 22.0.466')
$ErrorActionPreference='Stop'
$root=Split-Path -Parent $PSScriptRoot
if(-not $Setup){
    $found=Get-ChildItem -LiteralPath (Join-Path $root 'artifacts\installer') -Filter '*-Setup.exe' -File | Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 1
    if(-not $found){throw 'No Setup.exe in artifacts\installer. Run scripts/build-installer.ps1 first.'}
    $Setup=$found.FullName
}
$Setup=(Resolve-Path -LiteralPath $Setup).Path
$python=Join-Path $HoudiniRoot 'python313\python.exe'
$hython=Join-Path $HoudiniRoot 'bin\hython.exe'
$metadata=Get-Content -LiteralPath ([IO.Path]::ChangeExtension($Setup,'.json')) -Raw | ConvertFrom-Json
$expected=$metadata.package_id
if(-not $metadata.app_id -or -not $metadata.package_filename){throw 'A qualified per-target installer report is required.'}
$base=Join-Path $root ('artifacts\installer\test-'+[DateTime]::UtcNow.ToString('yyyyMMddTHHmmss'))
$checks=[Collections.Generic.List[string]]::new()
$report=[ordered]@{result='running';setup=$Setup;package_id=$expected;base=$base;checks=$checks}
function Save-Report{$report | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $base 'report.json') -Encoding utf8}
function Assert([bool]$condition,[string]$message){if(-not $condition){throw $message}}

function Invoke-Setup([string]$app,[string]$prefs,[string]$name,[string[]]$extra=@()){
    $log=Join-Path $base "$name.log"
    $arguments=@('/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART',"/DIR=`"$app`"","/HFS=`"$HoudiniRoot`"","/PREFS=`"$prefs`"","/LOG=`"$log`"")+$extra
    $process=Start-Process -FilePath $Setup -ArgumentList $arguments -WindowStyle Hidden -Wait -PassThru
    Assert ($process.ExitCode -eq 0) "Setup ($name) exited $($process.ExitCode); see $log"
}
function Invoke-Uninstall([string]$app,[string]$name){
    $uninstaller=Join-Path $app 'unins000.exe'
    Assert (Test-Path -LiteralPath $uninstaller) "Uninstaller missing: $uninstaller"
    $process=Start-Process -FilePath $uninstaller -ArgumentList @('/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART',"/LOG=`"$(Join-Path $base "$name.log")`"") -WindowStyle Hidden -Wait -PassThru
    Assert ($process.ExitCode -eq 0) "Uninstall ($name) exited $($process.ExitCode)"
    # Inno's uninstaller continues from a temporary copy; wait for it to finish.
    $deadline=[DateTime]::UtcNow.AddSeconds(180)
    while((Test-Path -LiteralPath $uninstaller) -and [DateTime]::UtcNow -lt $deadline){Start-Sleep -Milliseconds 500}
    Assert (-not(Test-Path -LiteralPath $uninstaller)) "Uninstall ($name) did not finish"
}
function Get-Status([string]$app){
    $output=& $python -I (Join-Path $app 'tools\release-package.py') status --install-root (Join-Path $app 'installation')
    Assert ($LASTEXITCODE -eq 0) "Install status failed: $output"
    $output | ConvertFrom-Json
}
function Assert-AssetBrowser([string]$app,[string]$packageFile,[bool]$expected,[string]$message){
    # Receipt, effective status, package env and hpath must agree; the add-on
    # files stay in the immutable payload either way.
    $status=Get-Status $app
    $configuration=Get-Content -LiteralPath $packageFile -Raw | ConvertFrom-Json
    $hostPath=($configuration.env | Where-Object { $_.PSObject.Properties.Name -contains 'CYCLES_HOUDINI_HOST_ROOT' }).CYCLES_HOUDINI_HOST_ROOT
    Assert ($hostPath -like "*/versions/$($status.state.active.Substring(0, 16))/host") "$message (package host)"
    $addon="$hostPath/addons/asset_browser"
    $hpath=@($configuration.hpath | ForEach-Object {"$_".Replace('\','/')})
    $value=($configuration.env | Where-Object { $_.PSObject.Properties.Name -contains 'CYCLES_HOUDINI_ASSET_BROWSER' }).CYCLES_HOUDINI_ASSET_BROWSER
    Assert ($status.state.components.asset_browser -eq $expected -and $status.components.asset_browser -eq $expected) "$message (receipt)"
    Assert ($value -eq $(if($expected){'1'}else{'0'})) "$message (package environment)"
    Assert ($hpath[0] -eq $hostPath -and ($addon -in $hpath) -eq $expected -and $hpath.Count -eq $(if($expected){2}else{1})) "$message (package hpath)"
    Assert (Test-Path -LiteralPath (Join-Path $hostPath 'addons/asset_browser/python_panels/cycles_assets.pypanel')) "$message (payload add-on missing)"
}

New-Item -ItemType Directory -Force -Path $base | Out-Null
try{
    # Stable application IDs are shared across all installs of this exact host.
    # Never overwrite an existing Apps entry, including leftovers from a test.
    $entry='HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\'+$metadata.app_id+'_is1'
    foreach($hive in 'HKCU:','HKLM:'){
        $candidate=$hive+'\Software\Microsoft\Windows\CurrentVersion\Uninstall\'+$metadata.app_id+'_is1'
        Assert (-not(Test-Path -LiteralPath $candidate)) "This target already has a Windows Apps entry at $candidate. Use a disposable Windows profile to run installer lifecycle tests."
    }
    # A different Houdini build is refused before anything is installed.
    $fake=Join-Path $base 'houdini-22.0.999'
    foreach($file in 'bin\houdini.exe','python313\python.exe'){New-Item -ItemType File -Force -Path (Join-Path $fake $file) | Out-Null}
    New-Item -ItemType Directory -Force -Path (Join-Path $fake 'toolkit\include\SYS') | Out-Null
    Set-Content -LiteralPath (Join-Path $fake 'toolkit\include\SYS\SYS_Version.h') -Value '#define SYS_VERSION_FULL "22.0.999"' -Encoding ascii
    $wrongApp=Join-Path $base 'app-wrong';$wrongPrefs=Join-Path $base 'prefs-wrong\houdini22.0'
    $process=Start-Process -FilePath $Setup -ArgumentList @('/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART',"/DIR=`"$wrongApp`"","/HFS=`"$fake`"","/PREFS=`"$wrongPrefs`"","/LOG=`"$(Join-Path $base 'install-wrong-version.log')`"") -WindowStyle Hidden -Wait -PassThru
    Assert ($process.ExitCode -ne 0) 'Setup accepted a different Houdini build'
    Assert (-not(Test-Path -LiteralPath (Join-Path $wrongPrefs "packages\$($metadata.package_filename)"))) 'Setup wrote a package file for a different Houdini build'
    Assert (-not(Test-Path -LiteralPath (Join-Path $wrongApp 'installation'))) 'Setup installed files for a different Houdini build'
    $checks.Add('Setup refuses a different Houdini build (22.0.999) before installing anything')

    $app=Join-Path $base 'app';$prefsParent=Join-Path $base 'prefs';$prefs=Join-Path $prefsParent 'houdini22.0'
    $packageFile=Join-Path $prefs "packages\$($metadata.package_filename)"

    Invoke-Setup $app $prefs 'install'
    Assert (Test-Path -LiteralPath $packageFile) 'Package file was not created'
    $status=Get-Status $app
    Assert ($status.state.active -eq $expected) "Active package $($status.state.active) differs from $expected"
    Assert ($status.state.components.blender_bridge -eq $true) 'Default installation changed the legacy enabled bridge choice'
    Assert-AssetBrowser $app $packageFile $true 'Default (Full) installation did not select the asset browser'
    $checks.Add('Fresh silent install activates the verified package and its status check passes')

    # Load check in a headless hython with only these preferences.
    $probe=Join-Path $base 'load_probe.py'
    Set-Content -LiteralPath $probe -Encoding utf8 -Value @'
import sys, hou, cycles_houdini
assert hou.nodeType(hou.vopNodeTypeCategory(), 'cycles::material::1') is not None, 'Cycles Material operator missing'
assert cycles_houdini.capabilities()['image_iris'], 'Image Iris support missing'
print(cycles_houdini.__file__)
'@
    $saved=@{};foreach($name in 'HOUDINI_USER_PREF_DIR','HOUDINI_PACKAGE_DIR','HOUDINI_PATH','CYCLES_HOUDINI_SOURCE_ROOT'){$saved[$name]=[Environment]::GetEnvironmentVariable($name,'Process')}
    try{
        $env:HOUDINI_USER_PREF_DIR=Join-Path $prefsParent 'houdini__HVER__'
        Remove-Item Env:HOUDINI_PACKAGE_DIR,Env:HOUDINI_PATH,Env:CYCLES_HOUDINI_SOURCE_ROOT -ErrorAction SilentlyContinue
        $loaded=& $hython $probe 2>&1 | Out-String
        Assert ($LASTEXITCODE -eq 0) "Installed package did not load in hython: $loaded"
    }finally{foreach($name in $saved.Keys){[Environment]::SetEnvironmentVariable($name,$saved[$name],'Process')}}
    $module=($loaded -split "`r?`n" | Where-Object {$_ -match 'cycles_houdini'} | Select-Object -Last 1).Trim()
    # Houdini reports mixed separators; compare normalized full paths.
    $installed=[IO.Path]::GetFullPath((Join-Path $app 'installation'))+[IO.Path]::DirectorySeparatorChar
    Assert ([IO.Path]::GetFullPath($module).StartsWith($installed,[StringComparison]::OrdinalIgnoreCase)) "Cycles loaded from $module, not the installed copy"
    $checks.Add('A headless hython using only the test preferences loads the Cycles operators from the installed copy')

    Invoke-Setup $app $prefs 'reinstall'
    $status=Get-Status $app
    Assert ($status.state.active -eq $expected) 'Reinstall changed the active package'
    Assert-AssetBrowser $app $packageFile $true 'Reinstall lost the asset browser selection'
    $checks.Add('Running Setup again over an existing installation succeeds and keeps it verified')

    Invoke-Setup $app $prefs 'disable-blender-bridge' @('/COMPONENTS="runtime"')
    $status=Get-Status $app
    Assert ($status.state.active -eq $expected -and $status.state.components.blender_bridge -eq $false) 'Optional bridge selection did not disable this installation only'
    $configuration=Get-Content -LiteralPath $packageFile -Raw | ConvertFrom-Json
    Assert ('0' -eq ($configuration.env | Where-Object { $_.PSObject.Properties.Name -contains 'CYCLES_HOUDINI_BLENDER_BRIDGE' }).CYCLES_HOUDINI_BLENDER_BRIDGE) 'Runtime package disagrees with disabled component receipt'
    Assert-AssetBrowser $app $packageFile $false 'Disabling the bridge did not disable the asset browser'
    Invoke-Setup $app $prefs 'repair-without-blender-bridge'
    Assert ((Get-Status $app).state.components.blender_bridge -eq $false) 'Repair lost the previous optional bridge selection'
    Assert-AssetBrowser $app $packageFile $false 'Repair without the bridge enabled the asset browser'
    & $python -I (Join-Path $app 'tools\release-package.py') refresh-registration --install-root (Join-Path $app 'installation')
    Assert ($LASTEXITCODE -eq 0) 'Refreshing registration failed'
    Assert ((Get-Status $app).state.components.blender_bridge -eq $false) 'Registration refresh lost the optional bridge selection'
    Assert-AssetBrowser $app $packageFile $false 'Registration refresh changed the disabled asset browser'
    Invoke-Setup $app $prefs 'enable-blender-bridge' @('/COMPONENTS="runtime,blenderbridge"')
    $status=Get-Status $app
    Assert ($status.state.active -eq $expected -and $status.state.components.blender_bridge -eq $true) 'Optional bridge selection changed the immutable payload or failed to enable the bridge'
    Assert-AssetBrowser $app $packageFile $false 'Selecting only the bridge enabled the asset browser'
    $checks.Add('Setup disables/enables the optional Blender bridge; repair and registration refresh preserve its independent choice')

    Invoke-Setup $app $prefs 'enable-asset-browser' @('/COMPONENTS="runtime,blenderbridge,blenderbridge\assetbrowser"')
    Assert-AssetBrowser $app $packageFile $true 'Selecting the asset browser did not add its add-on path'
    Invoke-Setup $app $prefs 'repair-with-asset-browser'
    Assert-AssetBrowser $app $packageFile $true 'Repair lost the asset browser selection'
    & $python -I (Join-Path $app 'tools\release-package.py') refresh-registration --install-root (Join-Path $app 'installation')
    Assert ($LASTEXITCODE -eq 0) 'Refreshing registration with the asset browser failed'
    Assert-AssetBrowser $app $packageFile $true 'Registration refresh lost the asset browser selection'
    Invoke-Setup $app $prefs 'disable-asset-browser' @('/COMPONENTS="runtime,blenderbridge"')
    Assert ((Get-Status $app).state.components.blender_bridge -eq $true) 'Disabling the asset browser disabled the bridge'
    Assert-AssetBrowser $app $packageFile $false 'Deselecting the asset browser kept its add-on path'
    $checks.Add('Setup enables/disables the asset browser via hpath and env; it follows the bridge, and repair and refresh preserve it')

    Invoke-Uninstall $app 'uninstall'
    Assert (-not(Test-Path -LiteralPath $packageFile)) 'Package file remained after uninstall'
    Assert (-not (Get-Content -LiteralPath (Join-Path $app 'installation\state.json') -Raw | ConvertFrom-Json).active) 'Installation remained active after uninstall'
    $checks.Add('Uninstall removes the package file and the installed files')

    $app2=Join-Path $base 'app-replace';$prefs2=Join-Path $base 'prefs-replace\houdini22.0'
    $packageFile2=Join-Path $prefs2 "packages\$($metadata.package_filename)"
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $packageFile2) | Out-Null
    $original=[Text.Encoding]::UTF8.GetBytes('{"env": [{"NOTE": "hand-made package kept by the installer test"}]}')
    [IO.File]::WriteAllBytes($packageFile2,$original)
    Invoke-Setup $app2 $prefs2 'install-replace' @('/REPLACEEXISTING=1')
    Assert ((Get-Status $app2).state.active -eq $expected) 'Replacing install did not activate the package'
    Assert (-not [Linq.Enumerable]::SequenceEqual([byte[]][IO.File]::ReadAllBytes($packageFile2),[byte[]]$original)) 'Existing package file was not replaced'
    Invoke-Uninstall $app2 'uninstall-replace'
    Assert ([Linq.Enumerable]::SequenceEqual([byte[]][IO.File]::ReadAllBytes($packageFile2),[byte[]]$original)) 'Uninstall did not restore the original package file'
    $checks.Add('An existing package file is replaced only on request and restored byte-for-byte on uninstall')
    # Uninstall retains state.json. That inactive receipt must not bypass the
    # consent check for the original registration restored above.
    $process=Start-Process -FilePath $Setup -ArgumentList @('/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART',"/DIR=`"$app2`"","/HFS=`"$HoudiniRoot`"","/PREFS=`"$prefs2`"","/LOG=`"$(Join-Path $base 'reinstall-without-consent.log')`"") -WindowStyle Hidden -Wait -PassThru
    Assert ($process.ExitCode -ne 0) 'Reinstall replaced the restored package without consent'
    Assert ([Linq.Enumerable]::SequenceEqual([byte[]][IO.File]::ReadAllBytes($packageFile2),[byte[]]$original)) 'Refused reinstall changed the original package'
    Assert (-not(Test-Path -LiteralPath $entry)) 'Refused reinstall registered a Windows Apps entry'
    Invoke-Setup $app2 $prefs2 'reinstall-replace' @('/REPLACEEXISTING=1')
    Assert ((Get-Status $app2).state.active -eq $expected) 'Consented reinstall did not activate the package'
    Invoke-Uninstall $app2 'uninstall-reinstall-replace'
    Assert ([Linq.Enumerable]::SequenceEqual([byte[]][IO.File]::ReadAllBytes($packageFile2),[byte[]]$original)) 'Second uninstall did not restore the original package bytes'
    $checks.Add('Reinstall into a retained inactive root refuses without consent, succeeds with consent, and restores the original bytes again')
    $report.result='passed'
}catch{
    $report.result='failed';$report.error=$_.Exception.Message
}finally{
    Save-Report
    $report | ConvertTo-Json -Depth 6
}
if($report.result -ne 'passed'){exit 1}

