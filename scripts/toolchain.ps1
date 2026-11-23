# SPDX-License-Identifier: Apache-2.0
# Resolve the VS 2022 x64 build tools without assuming the installation is on C:.

function Get-CyclesVisualStudioRoot([switch]$AllowIncomplete) {
    $vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
    $roots = @()
    if (Test-Path -LiteralPath $vswhere -PathType Leaf) {
        $roots = @(& $vswhere -products '*' -version '[17.0,18.0)' -property installationPath)
        if ($LASTEXITCODE -ne 0) { $roots = @() }
    }
    $standalone = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\2022\BuildTools'
    $roots = @($roots + $standalone | Where-Object {
        Test-Path -LiteralPath (Join-Path $_ 'Common7\Tools\VsDevCmd.bat') -PathType Leaf
    } | Select-Object -Unique)
    foreach ($root in $roots) {
        $msvc = Join-Path $root 'VC\Tools\MSVC'
        if (Get-ChildItem -LiteralPath $msvc -Directory -Filter '14.42.*' -ErrorAction SilentlyContinue) {
            return $root
        }
    }
    if ($AllowIncomplete -and $roots.Count) { return $roots[0] }
}

function Get-CyclesVisualStudioTools([string]$Root) {
    if (-not $Root) { return $null }
    $bundle = Join-Path $Root 'Common7\IDE\CommonExtensions\Microsoft\CMake'
    return [pscustomobject]@{
        CMake = Join-Path $bundle 'CMake\bin\cmake.exe'
        Ninja = Join-Path $bundle 'Ninja\ninja.exe'
    }
}

function Get-CyclesVisualStudioEnvironment([string]$Root) {
    if (-not $Root) { throw 'Visual Studio 2022 with the 14.42 C++ compiler was not found.' }
    $start = New-Object Diagnostics.ProcessStartInfo
    $start.FileName = $env:ComSpec
    $start.Arguments = '/d /s /u /c ""%CYCLES_TOOLCHAIN_SCRIPT%" "%CYCLES_TOOLCHAIN_VS_ROOT%""'
    $start.EnvironmentVariables['CYCLES_TOOLCHAIN_SCRIPT'] = Join-Path $PSScriptRoot 'toolchain.cmd'
    $start.EnvironmentVariables['CYCLES_TOOLCHAIN_VS_ROOT'] = $Root
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $start.StandardOutputEncoding = [Text.Encoding]::Unicode
    $start.StandardErrorEncoding = [Text.Encoding]::Unicode
    $process = [Diagnostics.Process]::Start($start)
    try {
        $diagnostics = $process.StandardError.ReadToEndAsync()
        $settings = $process.StandardOutput.ReadToEnd() -split "`r?`n"
        $process.WaitForExit()
        $problem = $diagnostics.GetAwaiter().GetResult().Trim()
        if ($process.ExitCode -ne 0) {
            throw "Visual Studio developer environment initialization failed. $problem"
        }
    } finally { $process.Dispose() }
    $environment = @{}
    foreach ($line in $settings) {
        $separator = $line.IndexOf('=')
        if ($separator -gt 0) { $environment[$line.Substring(0, $separator)] = $line.Substring($separator + 1) }
    }
    if ($environment.VCToolsVersion -notlike '14.42.*' -or -not $environment.VCToolsInstallDir -or
        -not (Test-Path -LiteralPath (Join-Path $environment.VCToolsInstallDir 'bin\Hostx64\x64\cl.exe') -PathType Leaf)) {
        throw 'The Visual Studio 2022 MSVC 14.42 x64 compiler (cl.exe) is missing.'
    }
    if (-not $environment.WindowsSdkVerBinPath -or
        -not (Test-Path -LiteralPath (Join-Path $environment.WindowsSdkVerBinPath 'x64\rc.exe') -PathType Leaf)) {
        throw 'The Windows SDK resource compiler (rc.exe) is missing.'
    }
    return $environment
}
