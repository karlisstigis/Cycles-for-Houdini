# SPDX-License-Identifier: Apache-2.0
[CmdletBinding()]
param(
    [string]$OutputDirectory='',
    [int]$Jobs=4,
    [switch]$ConfigureOnly,
    [string]$CMake='C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe'
)
$ErrorActionPreference='Stop'
$root=Split-Path -Parent $PSScriptRoot
$commit=(Get-Content -LiteralPath (Join-Path $root 'source.lock.json') -Raw | ConvertFrom-Json).cycles.commit
if(-not $OutputDirectory){$OutputDirectory=Join-Path $root ('build/stock-cycles-'+$commit.Substring(0,8))}
$OutputDirectory=[IO.Path]::GetFullPath($OutputDirectory)
$source=Join-Path $OutputDirectory 'source'
$build=Join-Path $OutputDirectory 'build'
& python (Join-Path $PSScriptRoot 'export-stock-cycles.py') --repository (Join-Path $root 'third_party/cycles') --commit $commit --destination $source
if($LASTEXITCODE -ne 0){throw 'Cannot extract pinned upstream source'}
$bundle=Join-Path $root 'third_party/cycles/lib/windows_x64'
$required=@(
    'OpenImageIO/lib/OpenImageIO.lib','OpenImageIO/lib/OpenImageIO_Util.lib',
    'opencolorio/lib/OpenColorIO.lib','openexr/lib/OpenEXR.lib',
    'openexr/lib/IlmThread.lib','openexr/lib/Iex.lib','openexr/lib/OpenEXRCore.lib',
    'imath/lib/Imath.lib','tbb/lib/tbb12.lib',
    'OpenImageIO/bin/OpenImageIO.dll','OpenImageIO/bin/OpenImageIO_Util.dll',
    'opencolorio/bin/OpenColorIO_2_5.dll','tbb/bin/tbb12.dll',
    'openexr/bin/OpenEXR.dll','openexr/bin/OpenEXRCore.dll','openexr/bin/Iex.dll',
    'openexr/bin/IlmThread.dll','imath/bin/imath.dll',
    'openjph/bin/openjph.0.25.dll','aom/bin/aom.dll',
    # Embree, exactly as the fork CLI links it (cmake/cycles-embree.cmake). The bundle's
    # embree4.dll imports sycl9.dll, which imports ur_win_proxy_loader.dll.
    'embree/lib/embree4.lib','embree/lib/embree4_sycl.lib','dpcpp/lib/sycl9.lib',
    'embree/bin/embree4.dll','dpcpp/bin/sycl9.dll','dpcpp/bin/ur_win_proxy_loader.dll'
)
$expected=@{}
$missing=@()
foreach($relative in $required){
    $pointer=& git -C $bundle show "HEAD:$relative"
    if($LASTEXITCODE -ne 0){throw "Pinned stock dependency is absent from Git: $relative"}
    $oid=[regex]::Match(($pointer -join "`n"),'(?m)^oid sha256:([0-9a-f]{64})$')
    if(-not $oid.Success){throw "Pinned stock dependency lacks an LFS SHA-256: $relative"}
    $expected[$relative]=$oid.Groups[1].Value
    $file=Join-Path $bundle $relative
    if(-not(Test-Path -LiteralPath $file -PathType Leaf) -or (Get-Item -LiteralPath $file).Length -lt 256){
        $missing+=$relative
    }elseif((Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash.ToLowerInvariant() -ne $expected[$relative]){
        throw "Pinned stock dependency changed: $relative"
    }
}
if($missing.Count){
    & git -C $bundle lfs pull "--include=$($missing -join ',')" '--exclude='
    if($LASTEXITCODE -ne 0){throw 'Pinned stock dependency LFS hydration failed'}
}
foreach($relative in $required){
    $file=Join-Path $bundle $relative
    if(-not(Test-Path -LiteralPath $file -PathType Leaf) -or
       (Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash.ToLowerInvariant() -ne $expected[$relative]){
        throw "Pinned stock dependency SHA-256 check failed: $relative"
    }
}
$link=Join-Path $source 'lib/windows_x64'
$sourceFull=[IO.Path]::GetFullPath($source)
$linkFull=[IO.Path]::GetFullPath($link)
if(-not $linkFull.StartsWith($sourceFull+[IO.Path]::DirectorySeparatorChar,[StringComparison]::OrdinalIgnoreCase)){
    throw 'Stock library link target leaves the exported source root.'
}
if(Test-Path -LiteralPath $link){
    $current=Get-Item -LiteralPath $link
    if($current.LinkType -eq 'Junction'){
        if([IO.Path]::GetFullPath($current.Target) -ne [IO.Path]::GetFullPath($bundle)){
            throw 'Existing stock dependency junction points at another library bundle.'
        }
    }elseif($current.PSIsContainer -and @($current.GetFileSystemInfos()).Count -eq 0){
        Remove-Item -LiteralPath $link
    }else{throw 'Stock dependency directory is not an empty gitlink placeholder.'}
}
if(-not(Test-Path -LiteralPath $link)){
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $link) | Out-Null
    New-Item -ItemType Junction -Path $link -Target $bundle | Out-Null
}
if(-not (Get-Command cl.exe -ErrorAction SilentlyContinue) -or -not (Get-Command rc.exe -ErrorAction SilentlyContinue)){
    $environment=& $env:ComSpec /d /c (Join-Path $PSScriptRoot 'toolchain.cmd')
    if($LASTEXITCODE -ne 0){throw 'Visual Studio toolchain setup failed'}
    foreach($line in $environment){$split=$line.IndexOf('=');if($split -gt 0){[Environment]::SetEnvironmentVariable($line.Substring(0,$split),$line.Substring($split+1),'Process')}}
}
$flags=@(
    '-G','Ninja','-S',$source,'-B',$build,
    '-DCMAKE_BUILD_TYPE=Release',
    '-DWITH_CYCLES_STANDALONE=ON','-DWITH_CYCLES_STANDALONE_GUI=OFF',
    '-DWITH_CYCLES_DEVICE_CUDA=OFF','-DWITH_CYCLES_DEVICE_OPTIX=OFF',
    '-DWITH_CYCLES_DEVICE_HIP=OFF','-DWITH_CYCLES_DEVICE_ONEAPI=OFF',
    '-DWITH_CYCLES_HYDRA_RENDER_DELEGATE=OFF','-DWITH_CYCLES_USD=OFF',
    # Same CPU ray-tracing backend as the fork: the 5.3 standalone dependency option is
    # WITH_CYCLES_EMBREE, the kernel compile/link gate is WITH_EMBREE. With only the first,
    # the kernel silently uses Cycles' own BVH2 (see cmake/cycles-embree.cmake).
    '-DWITH_CYCLES_EMBREE=ON','-DWITH_EMBREE=ON',
    '-DWITH_CYCLES_OPENIMAGEDENOISE=OFF','-DWITH_CYCLES_PATH_GUIDING=OFF',
    '-DWITH_CYCLES_OPENVDB=OFF','-DWITH_CYCLES_NANOVDB=OFF',
    '-DWITH_CYCLES_OPENSUBDIV=OFF','-DWITH_CYCLES_OSL=OFF','-DWITH_CYCLES_ALEMBIC=OFF'
)
& $CMake @flags
if($LASTEXITCODE -ne 0){throw 'Stock Cycles configure failed'}
# The pristine source cannot carry the fork's ch_validate_cycles_embree check, so read the
# CPU kernel's generated compile definitions instead.
$ninja=Get-Content -LiteralPath (Join-Path $build 'build.ninja') -Raw
$kernelRule=[regex]::Match($ninja,'(?ms)^build src\\kernel\\device\\cpu\\CMakeFiles\\cycles_kernel_cpu\.dir\\kernel\.cpp\.obj:.*?(?=^build |\z)')
if(-not $kernelRule.Success){throw 'Cannot find the stock CPU kernel compile rule in build.ninja'}
$kernelDefines=@([regex]::Matches($kernelRule.Value,'-D(WITH_EMBREE|EMBREE_MAJOR_VERSION=[0-9]+)(?=\s)') | ForEach-Object {$_.Groups[1].Value} | Sort-Object -Unique)
if('WITH_EMBREE' -notin $kernelDefines -or -not ($kernelDefines -match '^EMBREE_MAJOR_VERSION=')){
    throw "Stock CPU kernel was configured without Embree (defines: $($kernelDefines -join ' ')); the bias suite needs the fork's backend."
}
if($ConfigureOnly){return}
& $CMake --build $build --parallel $Jobs --target cycles
if($LASTEXITCODE -ne 0){throw 'Stock Cycles build failed'}
$exe=Join-Path $build 'bin/cycles.exe'
if(-not(Test-Path -LiteralPath $exe -PathType Leaf)){throw 'Stock Cycles CLI was not produced'}
$runtime=@('OpenImageIO','opencolorio','openexr','imath','openjph','aom','tbb','embree','dpcpp','level-zero') |
    ForEach-Object {Join-Path $bundle "$_/bin"}
$backend=& python (Join-Path $PSScriptRoot 'cycles_cli_backend.py') describe --executable $exe --search-path ($runtime -join ';')
if($LASTEXITCODE -ne 0){throw 'Cannot identify the stock CLI ray-tracing backend'}
$backend=$backend | ConvertFrom-Json
if($backend.backend -ne 'embree' -or
   [IO.Path]::GetFullPath($backend.embree_dll_path) -ne [IO.Path]::GetFullPath((Join-Path $bundle 'embree/bin/embree4.dll'))){
    throw "Stock CLI does not use the bundle's Embree (backend $($backend.backend), $($backend.embree_dll_path))."
}
$receipt=[ordered]@{
    source_commit=$commit
    source_marker=(Get-Content -LiteralPath (Join-Path $source '.stock-commit') -Raw).Trim()
    archive_sha256=(Get-Content -LiteralPath (Join-Path $source '.stock-archive-sha256') -Raw).Trim()
    executable=$exe
    executable_sha256=(Get-FileHash -LiteralPath $exe -Algorithm SHA256).Hash.ToLowerInvariant()
    flags=$flags
    kernel_defines=$kernelDefines
    bvh_backend=$backend
    built_utc=[DateTime]::UtcNow.ToString('o')
}
$receipt | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $OutputDirectory 'stock-receipt.json') -Encoding utf8
Write-Host "Stock CLI receipt: $(Join-Path $OutputDirectory 'stock-receipt.json')"
