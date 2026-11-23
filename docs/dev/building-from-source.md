# Building from source

This builds Cycles for Houdini on Windows from a fresh clone. Everything else the build needs is
downloaded from public addresses at the commits recorded in `source.lock.json`.

## What you need installed

| What | Version | Where the scripts look |
| --- | --- | --- |
| Windows 10 or 11, 64-bit | | |
| Git and Git LFS | any recent; run `git lfs install` once | on `PATH` |
| Python | 3.10.12 or newer, as `python` | on `PATH` |
| PowerShell | 7 | `pwsh` |
| Houdini, with its development kit (the `toolkit` folder) | exactly 22.0.466 | `C:\Program Files\Side Effects Software\Houdini 22.0.466` (`-HoudiniRoot` to change) |
| Visual Studio 2022 Build Tools: C++ workload, MSVC v143 toolset 14.42, a Windows SDK, C++ CMake tools | 14.42 | `C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools` |
| NVIDIA CUDA Toolkit | 12.8 | `C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.8` (`-CudaRoot` to change) |
| NVIDIA GPU and driver | a driver new enough for OptiX 9.1 | only for the OptiX check |

`bootstrap.ps1` checks this list first and names everything that is missing.

## Commands

From PowerShell 7, in an empty folder on a drive with at least 5 GB free:

```powershell
git clone <repository address> Cycles_Houdini
cd Cycles_Houdini
./scripts/bootstrap.ps1
./scripts/build.ps1
```

What `bootstrap.ps1` downloads into `third_party/`:

| Folder | From | Pinned by |
| --- | --- | --- |
| `third_party/cycles` | `https://github.com/blender/cycles.git` at `cycles.commit`, then the files in `patches/` (116 today) applied in the order of `cycles.patches` | each patch's SHA-256 (`cycles.patch_sha256`) and the resulting Git tree (`cycles.authoring_tree`) |
| `third_party/cycles/lib/windows_x64` | `https://projects.blender.org/blender/lib-windows_x64.git` (Git LFS, only the folders in `lfs_include`) | `windows_dependencies.commit` |
| `third_party/optix` | `https://github.com/NVIDIA/optix-dev.git`, tag `v9.1.0` | `optix_headers.commit` |
| `third_party/dlss` | `https://github.com/NVIDIA/DLSS.git`, tag `v310.9.16`, only `include/` | `dlss_headers.commit` |

The patched engine is checked twice: its Git tree must equal `cycles.authoring_tree`, and
`engine-fork.py verify` replays the patches again and compares every engine file. A failed
download leaves `third_party/cycles.incomplete`; delete it and run `bootstrap.ps1` again. An
existing `third_party/cycles` is never replaced, only checked.

The engine has a second, internal route: `-EngineSource Private` (or `-EngineRepository <path>`)
copies the project's private authoring fork instead. When that fork is present under
`artifacts/engine-authoring/locked-repo` it is the default. Both routes give the same files; only
the private route keeps the original engine commit IDs, which release packaging
(`scripts/release-source.py`, `scripts/release-package.py`) requires; those steps stop with a
message naming the private route when given a public-route checkout or build. The build accepts
either route and records which one in `build-manifest.json` (`engine_route`: `public` or
`private`, and `engine_tree`, the engine's Git tree).

`build.ps1` writes to `_work\dev\windows-h22.0.466-release`, inside the clone (ignored by Git; see
[working folders](README.md#working-folders)). Use
`-BuildDirectory` to change it and `-Jobs` for the number of parallel compile jobs (default 6).

## Time and disk

| Step | Time | Disk |
| --- | --- | --- |
| Clone | under a minute (estimate) | about 40 MB for one commit (measured) |
| Engine source: fetch upstream and apply the patches | 6 s on a Linux machine (measured); Windows estimate 1 to 3 minutes | about 30 MB (measured) |
| Dependencies (`lib/windows_x64`, OptiX, DLSS headers) | depends on the connection: about 1 GB to download (estimate from the files) | about 1 GB (measured) |
| Build, including the CUDA and OptiX kernels | about 11 minutes with `-Jobs 6` on a 16-core workstation (measured once, earlier build) | about 1.4 GB (measured) |

Plan for about 3 GB and 15 to 30 minutes in total, not counting the installs above.

## Check the result

Render a small scene (a red box) on the CPU and with OptiX. Each command ends with exit code 0 and
writes a report with `"result": "passed"`:

```powershell
./scripts/run-hython.ps1 tests/hython_scene_contract.py --device CPU --report $env:TEMP/cycles-cpu.json
./scripts/run-hython.ps1 tests/hython_scene_contract.py --device OPTIX --report $env:TEMP/cycles-optix.json
```

To look at it in Houdini, start `./scripts/launch-normal-build.ps1`. It uses its own Houdini
preferences, so an installed Cycles for Houdini is not affected.

`scripts/fresh-clone-check.ps1` does all of the above in one go,
from a new clone of the current commit, and writes `result.json` with times and disk use. Its test folder is a new
folder in `<project>\_work\tests` (ignored by Git and never exported), so everything stays inside the project folder;
`-Destination` names another one (inside the project only under `_work\tests`).
