# Your own build: build, change, share

This is the machinery behind the one double-click file in the repository root,
`START HERE - Build Cycles for Houdini.cmd` (`scripts/easy-start.ps1`). It is for people who do
not write code. Its result is an installer (Setup.exe) for each Houdini build it builds for; the build itself
installs nothing and changes nothing in Houdini, and the tool never runs a Setup. Every run ends with DONE /
NOT DONE and NEXT, and every run can be repeated safely: finished parts are checked, not done again. Only one run
at a time works on a folder (a second window says so and stops).

First run (no build whose quick tests passed yet), all in one window:

| Part | What it does | First time |
| --- | --- | --- |
| Check this computer | Looks for the installed Houdini builds and required tools. With an NVIDIA card, asks separately for consent to fetch the listed NVIDIA compiler parts and headers under their licences. The choice is saved per source folder and pinned dependency set. Decline for a CPU build. Missing tools are listed with sizes and permissions and installed after a separate answer. Inno Setup is optional. The check reads the driver version, lists unavailable items and reports if Windows requires a restart. PowerShell 5.1 can start the check before PowerShell 7 is available. | 30 to 60 min when everything is missing, mostly Visual Studio Build Tools (estimate) |
| Choose the Houdini | Lists the installed Houdini 22.0 builds with their state (no Cycles yet / built, tests passed / built from older sources / Cycles for Houdini installed by a Setup / no development kit). One Houdini: no question. Several: one question, Enter = those without Cycles yet, `a` = all, or one build such as `22.0.508`. | seconds |
| Get the sources and build | For each chosen Houdini build in turn, into its own build folder: offers newer sources (only in a Git clone with no local changes), downloads upstream Cycles plus `patches/` and the pinned dependencies (once; shared by all Houdini builds), builds. Prints an estimate first ("about N minutes per Houdini build", measured on this computer after the first build) and one result line per Houdini build at the end; one failing build does not stop the others. | on a Ryzen 9 7950X, installer included: about 13 min for the first Houdini build, about 5 min for each further one (GPU kernels reused; measured: 22.7 min for 22.0.508, 22.0.466 and 22.0.429) |
| Quick tests | For each build, in that Houdini's own `hython`: renders a small scene on the CPU and, with an NVIDIA GPU whose driver is new enough for OptiX (and a build with OptiX), with OptiX, and checks the picture. | 1 min |
| Make the installer | For each build whose quick tests passed (`scripts/easy-make-installer.ps1 -Tested`: not rebuilt or tested again; the quick-test result must name this exact build): checks the engine against `patches/`, packages, verifies the package and compiles a Setup.exe labelled with `build-identity.json`. Asks nothing: a ZIP download is recorded as "source commit unknown", uncommitted changes as "local changes", an unnamed build as "Unnamed community builder", each with one line. The Setup goes into the project's Installers folder as `<install folder name> - Setup.exe` (for example `Cycles for Houdini 22.0.508 (<build_id>) - Setup.exe`; a newer one replaces the older file of the same name); package, reports and logs into `_work\installer-details\<Houdini build>-<time>\` (the newest one per Houdini build is kept). | under 1 min per Houdini build (part of the build times above) |

At the end: the list of installers with the Houdini build each is for, that double-clicking one installs Cycles into
that Houdini build, that it is uninstalled in Windows Settings > Apps > Installed apps, that the build installed
nothing, and what a Setup does next to an earlier installation (below). An attended run opens the installer folder
in Explorer; unattended runs never open a window.

Later runs first look at what changed: a Houdini 22.0 build that is new on this computer and has no Cycles is built
for at once, with its installer. A removed build is reported, and an unfinished build stays eligible for retry.
The next successful run shows the menu (Enter = Close):

| Menu item | `-Action` | Script | Time |
| --- | --- | --- | --- |
| 1 Update the sources and rebuild (then the quick tests and new installers) | `Update` | `scripts/easy-get-and-build.ps1`, `scripts/easy-quick-tests.ps1`, `scripts/easy-make-installer.ps1 -Tested`, for every existing or unfinished build | minutes, only what changed |
| 2 Run the quick tests | `QuickTests` | `scripts/easy-quick-tests.ps1`, every build | 1 min each |
| 3 Make the installers again | `Installer` | `scripts/easy-make-installer.ps1` for every built Houdini build (rebuilds what changed and runs the quick tests again) | about 70 s per Houdini build (measured: 206 s for three) |
| 4 Open Houdini with my build (without installing) | `OpenHoudini` | `scripts/easy-open-houdini.ps1`: a separate Houdini with this build and its own preferences, to try a build or while changing code; with several built Houdini builds it asks which (Enter = the newest) | 1 min |
| 5 I changed the engine: update the patch files | `EnginePatches` | `scripts/easy-update-engine-patches.ps1`: saves edits in `third_party/cycles` as new files in `patches/` | under 1 min |
| 6 Turn DLSS off / Turn DLSS on (NVIDIA's DLSS licence applies) | `DlssOff` / `DlssOn` | sets `dlss` in `build-identity.json` (off also clears `dlss_project_id`); the next rebuild applies it | seconds |
| 7 Check this computer again | `CheckComputer` | `scripts/easy-check-computer.ps1` | under 1 min |
| 0 Close | | | |

Enter closes: a stray Enter never starts a rebuild, a download or a window. `-Action AddHoudini` (no menu item)
builds, with installers, for the Houdini builds `-Houdini` names (or asks), also for one that has Cycles installed by
a Setup. Unattended use (scripts, the fresh-clone check): `scripts/easy-start.ps1 -Action <name>` with `-Yes`
(or `CYCLES_EASY_ANSWER=yes|no`) answers ordinary prerequisite questions. Set `CYCLES_EASY_NVIDIA_CONSENT=yes|no` separately to approve or decline
NVIDIA downloads. `-Houdini missing|all|newest|<build>` (or the setting
`CYCLES_EASY_HOUDINI`) chooses the Houdini builds (default `missing`); also `-NoUpdate`, `-Jobs N`, `-SkipOptix`,
`-OutputDirectory` (the installer folder), `-StopBeforeInstaller` (verified packages, no Setup), `-Message`. Exit
codes: 0 done, 1 not done, 3 something still missing, 4 Windows must restart, 5 already running, 6 source update failed. The scripts can also
be run directly; a step started on its own works for `CYCLES_EASY_HOUDINI` when it names one build, else the newest
Houdini 22.0 build with the development kit. `scripts/fresh-clone-community-check.ps1` runs the whole path from a
fresh clone (`-Zip` from a downloaded ZIP), by default in a new folder in `<project>\_work\tests`, so everything
stays inside the project folder.

## A new installer next to an earlier installation

What a Setup does (from `packaging/installer/CyclesHoudini.iss` and `scripts/release-package.py`; the check runs the
install tool the way Setup calls it, in private folders, never a real Setup):

- The same build identity (`build_id`) for the same Houdini build: the same Windows application id
  (`target_identity`), so Windows sees one application; Setup installs into the same folder and replaces the
  installed version (one entry in Installed apps, one Houdini package file). The fresh-clone check installs a newer
  package over a committed one and finds one package file afterwards.
- Another Cycles for Houdini for that Houdini build (the official one, or a community build with another
  `build_id`): Setup's preflight and the install tool refuse ("Another Cycles package in these preferences is
  eligible for the same Houdini build: <file>; remove it before installing"; an older official registration:
  "uninstall it before installing a community build"). A package can cover more than one compatible Houdini build,
  so even different Setup targets may overlap. Uninstall the other Cycles build first in Installed apps.
- A package file that Setup did not write, with the name Setup uses: Setup asks before replacing it and puts it
  back on uninstall.
- Only a real installation can show what Windows itself displays (the Installed apps entry, SmartScreen); the check
  does not run a Setup.

## Several Houdini builds

A plugin must match the Houdini build it was compiled for: Houdini 22.0.429, 22.0.466 and 22.0.508 have three
different HDK API numbers (22000425, 22000453, 22000475). So there is one build folder per Houdini build
(`<folder>\_work\windows-h<build>-release`), one installer per Houdini build, and the
package file of each build loads it only into Houdini builds with the same HDK API and the same Open Image Denoise
files (`scripts/release-package.py`, `package_enable`); this safety check is unchanged.

Found: SideFX's registry entries (`HKLM\SOFTWARE\Side Effects Software\Houdini`, values such as `22.0.0.508` whose
data is the folder) and the folders `Program Files\Side Effects Software\Houdini *`; a build counts when
`toolkit\include\SYS\SYS_Version.h` is there (`scripts/easy-houdini.ps1`). "Has Cycles" means a build here whose
quick tests passed, or a Cycles for Houdini package file of a Setup (official or community) in the Houdini
preferences that loads for that build; a Setup install is never changed. A build made from older sources is shown
as such and refreshed by "Update the sources and rebuild".

Shared between Houdini builds: the downloaded sources and dependencies (`third_party/`), the CUDA compiler parts,
the tools and the GPU kernels (below). Not shared: the rest of the build, engine included, because the engine is
compiled against the Houdini development kit's headers and libraries (OpenVDB, NanoVDB, OpenSubdiv and TBB headers,
Open Image Denoise: `cmake/houdini-sdk-native.cmake`, `cmake/houdini-sdk-oidn.cmake`).

## GPU kernels: compiled once, reused

The six GPU kernel files (CUDA `kernel_sm_86.cubin`, `kernel_sm_89.cubin`, `kernel_compute_75.ptx`; OptiX
`kernel_optix.ptx`, `kernel_optix_mnee.ptx`, `kernel_optix_shader_raytrace.ptx`; each also compressed) are made by
NVIDIA's compiler from the engine sources, the CUDA and OptiX headers only: no Houdini file goes into them. The
builder's path (`scripts/build.ps1 -KernelCache <folder>`, passed by `scripts/easy-get-and-build.ps1` and
`scripts/easy-make-installer.ps1`) points the engine's kernel rules at a small stand-in for nvcc,
`<build>\kernel-reuse\nvcc.cmd`, through the engine's own setting `CUDA<major>_NVCC_EXECUTABLE`
(`src/kernel/device/cuda/CMakeLists.txt`, `cuda_get_toolkit_for_arch`): no engine, patch or CMake file changes, and
the official route (`scripts/build-release.ps1`, which passes no `-KernelCache`) is as before. For each kernel the
stand-in (`scripts/kernel-cache.py`) works out a key and either copies the stored kernel file or runs nvcc with the
same command and stores the result; every other nvcc call goes to nvcc unchanged. The key covers: the nvcc command
with the build folder written as `<BUILD>`; the steps nvcc would run (`nvcc --dryrun`); the source as the GPU
compiler sees it (nvcc's own device preprocessing step, run on its own); every file named in it by content (kernel
sources and headers from the engine, the CUDA headers, the OptiX headers, the C++ compiler's headers); every file of
the CUDA compiler folders `bin`, `nvvm` and `include`; the folder of every other program those steps run (`cl.exe`);
the settings nvcc reads (`NVCC_*`, `CUDA_*`, `INCLUDE`, ...; `PATH` only through the programs it finds, because its
order can differ between two runs with the same inputs); and the stand-in itself. Anything that cannot be
worked out means: compile as usual, store nothing.

Before building, the script rechecks existing kernels against the current compiler and preprocessing inputs. A changed or unverifiable output is rebuilt. Cache-copy failures fall back to a real compile.

Stored copies: `<folder>\_work\kernel-cache\<key>\` with the kernel file and `manifest.json` (its key, size and
SHA-256), written into a temporary folder that is renamed when complete; an entry without a manifest, with another
key or a different file is removed and never used. After each successful build the newest two entries per kernel
file are kept. Delete the folder to clear it. Turn reuse off with `scripts/easy-start.ps1 -NoKernelReuse` (or
`CYCLES_EASY_NO_KERNEL_REUSE=1`): the build folder is set back to nvcc and every build compiles its kernels (the
first build after switching compiles them once, because the kernel commands changed). The build prints one line:
"GPU programs reused from the first build: nothing they are made from changed" or "GPU programs compiled in ...".
Measured (fresh-clone check at 3e9c5e7, Ryzen 9 7950X, 22.0.508, 22.0.466, 22.0.429 in one run): the first build
compiled the six GPU kernel files in 6 min 59 s and took 12.9 min with quick tests and installer; the two further
builds reused all six (about 11 s each) and took 4.8 and 4.9 min, against 12.8 and 12.4 min without reuse (62% less);
the whole run took 22.7 min instead of 38.5 min (41% less). All twelve kernel files of the reused builds are
byte-identical to those of the build that compiled them and to a build with reuse turned off.

The tested build is 22.0.466 (`certification_candidate` in `source.lock.json`, unchanged, also the official
route's only target). For any other 22.0 build the builder says once that it is untested, usually fine, and how
to report a problem; it does not block.

`launchers/Build and make installer.cmd` is the maintainer's official release route
(`scripts/build-release.ps1`); it needs the private engine fork and stops early on a public clone,
pointing to the file above. The first run and this guide say so, so that a newcomer does not
confuse the two.

## CUDA compiler parts (no NVIDIA installer, no display driver)

The build needs only three parts of the CUDA Toolkit 12.8: the compiler `nvcc` (with its own
headers), the CUDA runtime headers and `cudart.lib` (CMake's FindCUDA requires it), and the CUDA
C++ core library headers that the CUDA headers include (`scripts/build.ps1 -CudaRoot`; the engine's
`src/kernel/device/cuda` and `optix` CMake files call `nvcc` only). Nothing from the toolkit is
copied into a build or an installer.

`scripts/cuda-toolkit-parts.py` downloads exactly these from NVIDIA's redistributable archives
(`https://developer.download.nvidia.com/compute/cuda/redist/`, manifest `redistrib_12.8.0.json`,
release date 2025-01-23: `cuda_nvcc` 12.8.61, `cuda_cudart` 12.8.57, `cuda_cccl` 12.8.55, the same
compiler as the maintainer's builds), checks size and SHA-256 against the values pinned in the
script, refuses an archive with a driver file (`.sys`, `.inf`, `.cat`) or a path outside its folder,
and unpacks into the project folder, `_work\cuda-12.8.0` (about 120 MB download; no administrator permission; no
system setting; it goes with the project folder). A complete copy that an earlier version put into
`%LOCALAPPDATA%\CyclesForHoudiniTools\cuda-12.8.0` is copied from there instead (no download; the old folder can
then be deleted). `scripts/cuda-toolkit-parts.py` used on its own still defaults to that `%LOCALAPPDATA%` folder. Why this cannot touch
the display driver: the archives are plain ZIP files of toolkit files, nothing in them is run while
unpacking, and NVIDIA's manifest has no Windows driver component at all (its only driver entries,
`nvidia_driver` and `driver_assistant`, are for Linux). NVIDIA's installer is never run. A toolkit
installed by NVIDIA's own installer (`C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.8`) is
used instead when present; `CYCLES_CUDA_ROOT` names another folder.

Licence: downloading means accepting NVIDIA's CUDA Toolkit licence
(https://docs.nvidia.com/cuda/eula/index.html; read 21 November 2026: version 13.5, last updated 26 January 2026):
section 1.1.1 allows installing and using the SDK; the parts are only used on your computer to compile, nothing of
them is redistributed. The computer check shows the link before asking. The parts are offered only with an NVIDIA
graphics card after approving NVIDIA downloads. If the card, approval or compiler is missing, the build is CPU-only (`scripts/build.ps1 -NoCuda`), and
neither OptiX nor DLSS headers are downloaded. Overview of all NVIDIA parts and licences:
[nvidia-licences.md](nvidia-licences.md). (Not legal advice.)

## GPU and display driver

GPU rendering and GPU denoising need an NVIDIA graphics card with its driver installed. Installing or updating the
driver is up to you. The computer check reads the driver version (`nvidia-smi`, or Windows' own device record) and
changes nothing; no script installs, updates or downloads a display driver. When the driver is too old for GPU
rendering it adds one line saying that an update is needed for that and that the decision is yours. Minimum Windows
driver versions, checked on 21 November 2026:

| Needed for | Windows driver | Source |
| --- | --- | --- |
| GPU rendering with CUDA (kernels from the CUDA 12.8 GA compiler) | 570.65 or newer | NVIDIA CUDA 12.8.0 release notes, table 3 |
| GPU rendering with OptiX 9.1.0 | R590 or newer | NVIDIA OptiX download page ("Requires NVIDIA R590 driver or newer") |
| DLSS 310 | 512.15 or newer | NVIDIA DLSS Programming Guide, revision 310.6.0, section 2.1 |

Without an NVIDIA GPU the build is CPU-only and the quick test skips OptiX.

## build-identity.json: who made this build

| Field | Meaning |
| --- | --- |
| `build_id` | Empty in the repository: filled in on first use (the build and the installer step, `release-package.py prepare-identity`) with `<publisher>-<6 random hex digits>`, or `builder-...` while the publisher is unnamed. The random suffix helps distinguish builds. Installers with the same ID and Houdini target address the same Windows application. You may also set it yourself: 3 to 32 lowercase letters, digits or `-`, starting with a letter. Gives the build its own Windows application id, install folder (`%LOCALAPPDATA%\Cycles for Houdini 22.0.466 (<build_id>)`) and Houdini package file (`cycles_houdini_community_<build_id>_22_0_466.json`). Keep it the same for all versions of your build, so a new installer updates the old one. |
| `build_name` | Shown in Setup, its Start-menu folder and Windows Settings > Apps as `Cycles for Houdini - community build (<build_name>)` (just `Cycles for Houdini - community build` for the default name): a community build is always marked as one. |
| `publisher` | Your name as Setup and Windows show it. Also the author name of engine patches the tool commits for you. |
| `publisher_url` | Your web address, your GitHub fork URL, or empty. The official repository and its pages are reserved. |
| `dlss` | `on` (default): DLSS with your own NVIDIA project id, `dlss_project_id`. `off` (menu item "Turn DLSS off"): built without DLSS; nothing from NVIDIA's DLSS SDK is downloaded (`scripts/bootstrap.ps1 -SkipDlss`), compiled in or packaged, and no project id is made. Without an NVIDIA graphics card or without the CUDA compiler parts a build is made without DLSS whatever this says. `official-project-id-with-permission`: DLSS with the project's official id, only if its owner has agreed in writing. |
| `dlss_project_id` | Empty in the repository; with `dlss` `on` it is filled in on first use with a random GUID. The default community mode rejects the official ID. |

DLSS project id: NVIDIA's DLSS Super Resolution Programming Guide (revision 310.6.0, March 2026,
`doc/DLSS_Programming_Guide_Release.pdf` in NVIDIA's DLSS repository on GitHub), section 5.2.1 and
`NVSDK_NGX_Init_with_ProjectID`: an application without an NVIDIA application id supplies its own
identifier; for a custom engine the driver checks that it is GUID-like. The build compiles it
without changing the engine or `patches/`: `scripts/build.ps1 -DlssProjectId` sets the top-level
CMake option `CH_DLSS_PROJECT_ID`, and `CMakeLists.txt` then compiles a copy of
`third_party/cycles/src/integrator/denoiser_dlss.cpp` (build folder `community-dlss/`) in which only
the `ngx_project_id` text differs. Empty, as on the official route, nothing changes. The installer step checks
the id that was compiled and records it.

The words "official" in `build_id` or `build_name`, the official publisher name ("The Cycles for Houdini Authors", in any
spelling: case, spaces and punctuation are ignored, also inside `publisher_url`) are refused, also in
`build_name`. `build_name` and `publisher` allow letters, digits, spaces and `. , ( ) _ + -`
(no `:` or `/`, which break the Start-menu folder). The default publisher ("Unnamed community
builder") is obviously not the official one; the installer step says so in one line and uses it (fine for your own use). Editing
this file is not counted as an uncommitted source change; the installer step records its content in the
build record (`build_identity_file`). If you download a new ZIP, copy the existing `build-identity.json` into the new
project folder before you build. This preserves the Windows application ID and the configured DLSS project ID.

How it installs next to the official Cycles for Houdini: a community build has its own
application id, folder, package file and Apps entry, so neither installer updates, replaces or
uninstalls the other. Setup refuses an activation when another Cycles package in the same Houdini preferences
covers any of the same eligible Houdini builds. Setup targets can differ while those eligible builds overlap.
Uninstall the existing Cycles build in Windows Settings > Apps, then run the new Setup. The uninstaller leaves
the previous renderer versions and mutable data. [Uninstall Cycles](../user/build-it-yourself.md#uninstall-cycles)
describes how to remove them.
A community install also refuses an old official "dev" registration (`cycles_houdini_dev.json`)
for the same Houdini, and a kept folder of another build (the official one or another community
build): uninstall or choose another folder. Preflight checks the Windows 259-character limit for
installed paths, preference registrations, helper files and their temporary write paths. Setup installs with
`--defer-commit` and finishes with `commit-setup` or `abort-setup`. On a retry, Setup uses the journal to recover
an interrupted installation. Windows PowerShell handles recovery and uninstall using the same journal and locks
as the package tool. Houdini is needed to install and refresh registration. Without `build-identity.json`
information (the official route, `scripts/build-release.ps1`) the package identity is exactly as
before.

## What an installer records

The package's `release-manifest.json` (and so its `package_id`) contains `build_identity` and
`build_record`: `official: false`, the engine route (`public` or `private`), the engine's Git
tree, the source commit, whether the working tree had uncommitted changes (and how many files),
the DLSS choice and the project id compiled, the content of `build-identity.json`, every check
with its result, and the checks that did not run with the reason. `official` is always `false`
whatever the caller's record says. `certification` reads "community build; not certified by the
Cycles for Houdini project"; `purpose` stays `internal`, and a community build can never be
packaged as a certified public release. Setup's first page repeats this in plain words, including
the checks that did not run. The installer step also writes
`community-build-report.json` and the Setup's own `*-Setup.json` into `_work\installer-details\<Houdini build>-<time>\`.

Checks in the installer step, and what replaces the official ones that cannot apply:

| Official route (`build-release.ps1`) | Community route (`easy-make-installer.ps1`) |
| --- | --- |
| engine HEAD = `cycles.authoring_tip` (private fork) | engine Git tree = `cycles.authoring_tree`, one commit per patch, full patch replay (`build.ps1`, `verify-upstream-patches.py`) |
| fork policy: `patch-triage.py --check` must pass | review records are shipped when they still cover the patch series; otherwise the manifest says why (`build_record.fork_policy`) |
| release qualification: built and installed runtime, Image Iris CPU/GPU, splats, DLSS, optics, existing scene, saved-scene farm, viewer GUI, rollback | quick tests: the scene test on CPU and, with an NVIDIA GPU, OptiX, in-process and in the worker |
| signed when a certificate is configured | unsigned |

## Changing the engine

After the first build, `third_party/cycles` is a Git repository: upstream Cycles plus one commit per
file in `patches/`. Edit any file there, then run
the menu item "I changed the engine: update the patch files". It changes nothing before you answer "y", and

1. commits your edits as one commit (author: `publisher` from `build-identity.json`, address
   `engine-edit@localhost`, so your own e-mail is not published; `--my-git-identity` uses yours);
   files saved with Windows line endings are stored with Unix ones, as the engine keeps them;
   dates are written in UTC, so your time zone does not appear in the patch. A commit with any
   other e-mail address is only exported after a person answers "y"; `--yes` does not answer
   that question;
2. exports every engine commit after the locked engine as a new numbered patch (existing patch
   files are never changed);
3. replays the new patches exactly as a fresh clone does and refuses if the result differs;
4. records the patches, their SHA-256 and the new `cycles.authoring_tree` in `source.lock.json`,
   and offers to commit `patches/` and `source.lock.json` (with your own Git identity when you
   have set one, as every Git commit does);
5. gives your engine commits the ids a fresh clone will produce (same files, so only the ids
   change), which makes builds from your folder and from a fresh clone record the same engine.

`cycles.authoring_tip` (the private fork's commit) is left unchanged. Commit identity versus tree
identity: every check uses the engine's Git tree, which depends only on the files; the public
replay also gives the same commit ids on every computer (committer = patch author, committer date
= author date), but that depends on Git applying mail patches the same way, so it is a
convenience, not a check. The private fork has its own tool, `scripts/export-engine-patches.py`.

If it stops halfway (a crash, a closed window), run it again: patch files it wrote before
`source.lock.json` are taken up again, saved but uncommitted records are offered for commit, and a
patch file that belongs to nothing stops it with a plain message. When it fails it lists what it
had already done ("already_done").

## What you may share (limits)

- What a rebuilt installer may legally contain is the responsibility of whoever distributes it.
  The installer contains compiled NVIDIA OptiX kernels, GPU programs made with NVIDIA's CUDA
  compiler (notice `notices/cuda/NOTICE.txt`; no CUDA Toolkit file is packaged) and, with DLSS on,
  the compiled DLSS loader: NVIDIA's terms apply (`THIRD_PARTY_NOTICES.md`, the NVIDIA rows, and
  `packaging/notices/index.json`, which marks the OptiX and DLSS entries for review). Most shelf icons and the
  installer icon are Blender icons under GPL-2.0-or-later (`THIRD_PARTY_NOTICES.md` section 3);
  the notices travel with the installer. (Earlier text here also named code that followed GPL
  Blender functions; that code has been rewritten, see `THIRD_PARTY_NOTICES.md` section 5.)
  The installer step reports the package's own notice check (`package-build.json`, `gates.notices`).
- Community installers are unsigned: Windows SmartScreen shows "Windows protected your PC"
  (More info > Run anyway).
- Each installer is for one Houdini 22.0 build, the one it was built for (Indie, Core or FX; Apprentice cannot run
  the tests). 22.0.466 is the tested build; others are untested.
