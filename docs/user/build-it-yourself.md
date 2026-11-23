# Build Cycles for Houdini yourself

This guide is for artists and designers who do not write code. It takes you from "I have Houdini and a Windows
computer" to "I have an installer of Cycles for my Houdini". You do not need to understand the code. There is no
ready-made installer to download: you build Cycles for Houdini on your own computer, for the Houdini builds you
have, by double-clicking one file:

**`START HERE - Build Cycles for Houdini.cmd`** (in the project folder)

The first time, it does everything in order in one window: it checks your computer, installs what is missing (after
showing you the list and asking), downloads the sources, builds for your Houdini, runs the quick tests and makes
an **installer (Setup) for each Houdini build**. It asks separately before downloading NVIDIA parts for GPU rendering.
If you have several Houdini 22.0 builds installed, it asks which to build for (press Enter for “those that have no
Cycles yet”). The build itself
installs nothing and changes nothing in Houdini: you install Cycles afterwards by double-clicking an installer. If it
has to stop (for example because Houdini is missing), the last lines say why and give the exact link; double-click
the same file again afterwards and it continues: finished parts are not done again.

When your build works, the same file shows a list of your Houdini builds and a short menu:

| Number | What it does |
| --- | --- |
| 1 | Update the sources and rebuild, for every Houdini you built for (then the quick tests and new installers) |
| 2 | Run the quick tests: renders a test scene on the CPU and, with an NVIDIA card, on the GPU |
| 3 | Make the installers again |
| 4 | Open Houdini with my build (without installing): to try a build before you install it, or while you change code. Houdini starts with separate settings, so an installed Cycles and your normal settings are not touched |
| 5 | I changed the engine: update the patch files (saves changes you made in `third_party/cycles`) |
| 6 | Turn DLSS off (or on) |
| 7 | Check this computer again or change your NVIDIA download choice |
| 0 (or just Enter) | Close |

Every run ends with a line that starts with **DONE** or **NOT DONE**, and a line **NEXT** that says what to do.

The file `launchers/Build and make installer.cmd` is the original author's official
release route. It needs his private copy of the engine and stops on yours. You do not need it.

## Before you start: is this for you?

Please read these limits first. This is an early project made by one person.

- **Windows 10 or 11, 64-bit only.** No macOS, no Linux.
- **Houdini 22.0, with its development kit.** The builder builds for the Houdini 22.0 builds you have installed
  (for example 22.0.508). The project was tested with **22.0.466**; for any other 22.0 build it says once that it is
  untested (it usually works). Houdini Apprentice does not work. Houdini Indie has been used by hand; Core and FX have
  not been checked.
- **An NVIDIA graphics card is optional.** GPU rendering and GPU denoising need an NVIDIA graphics card with its
  driver installed. Installing or updating the driver is up to you. Without an NVIDIA card your build renders on the
  CPU. AMD, Intel and Apple graphics are not supported for GPU rendering.
- **Tested on one computer only** (Windows 11 with an RTX 4090 and an RTX 3080 Ti). Your computer may show problems
  nobody has seen yet.
- **There is an open GPU problem**: one heavy scene stopped with a GPU error while both graphics cards were almost
  full. The README describes it under "Known issues".

## What you need

| What | Why | Size on disk |
| --- | --- | --- |
| Houdini 22.0 with its development kit (the `toolkit` folder inside the Houdini folder) | the program we plug into | you already have it |
| Free disk space for the project, your builds and the installers | sources, downloaded parts, one build per Houdini, one installer per Houdini | about 3.3 GB for one Houdini build, about 1.6 GB for each further one (measured) |
| About 10 GB free for the tools below | compiler, Git, Python (CUDA compiler parts: about 120 MB) | estimate |
| An internet connection | the build downloads Cycles and other parts | about 1 GB download (estimate) |

The tools (the first run checks them and installs what is missing, after asking):

- **Git** and **Git LFS**: download the engine sources and keep track of changes.
- **Python 3**: runs the helper scripts.
- **PowerShell 7**: runs the build scripts.
- **Visual Studio 2022 Build Tools** with the C++ workload: the compiler, which turns source code into programs.
- **Inno Setup 6**: makes the installers. If you say no to it, everything is still built and tested, only no
  installer is made (the end says so; run the file again and answer `y` to get the installers).
- **Only with an NVIDIA graphics card: the CUDA 12.8 compiler parts** from NVIDIA, needed to build the GPU part. Only
  these parts are downloaded (about 120 MB), from NVIDIA's own archives, into a folder of your own
  (`_work\cuda-12.8.0` inside the project folder); NVIDIA's installer is not used and your graphics driver is never
  touched. If an earlier version of this tool already downloaded them to `%LOCALAPPDATA%\CyclesForHoudiniTools`,
  they are copied from there (you may delete that old folder afterwards).
  NVIDIA's CUDA licence applies. If you already installed NVIDIA's CUDA Toolkit 12.8, that is used instead. If you
  decline NVIDIA downloads, have no NVIDIA card, or build without a CUDA compiler, your build renders on the CPU.

With an NVIDIA card and your approval, the build also downloads the OptiX headers for GPU rendering and, if DLSS
is on, the DLSS headers for the viewer. DLSS is on by default. The NVIDIA download question lists all three items
and their licences. Which NVIDIA part needs which licence: [docs/dev/nvidia-licences.md](../dev/nvidia-licences.md).

The exact versions are listed in [docs/dev/building-from-source.md](../dev/building-from-source.md).

## Part 1: get your installers

### Download the project

1. Make a short folder path on a drive with enough space, for example `D:\Cycles`. Short paths avoid problems.
2. On the project's GitHub page, use the green **Code** button and **Download ZIP**, then right-click the ZIP and
   choose "Extract all" into that folder. If you use Git or GitHub Desktop, cloning works too, and then "Update the
   sources and rebuild" can fetch newer versions by itself; a ZIP folder cannot update itself (download the new ZIP
   into a new folder instead). To keep updating your existing installation, copy `build-identity.json` from the
   old project folder into the new one before starting the builder.

What you will see: a folder with `README.md`, `houdini`, `scripts` and other folders inside, and the file
`START HERE - Build Cycles for Houdini.cmd`. Time: about a minute.

### Double-click `START HERE - Build Cycles for Houdini.cmd`

**1. The computer check.** If the builder detects an NVIDIA card, it shows the NVIDIA compiler parts and headers
with their licence links and asks whether to allow their downloads. Press Enter or type `n` for a CPU build with
no NVIDIA downloads. Type `y` to allow the listed downloads, then choose whether to install missing tools.
The builder remembers your answer for this copy. Choose **7** to change it and **1** to rebuild. A change to the
pinned NVIDIA dependencies asks for a fresh decision.

The window lists each remaining tool as `OK` or `MISSING`, lists your Houdini 22.0 builds, and
looks for an NVIDIA graphics card (it only reads the driver version; it never changes the driver). Then it shows
exactly what it would install, in order, which installs make Windows ask for administrator permission, and what the
result will be:

```
To build, this computer still needs (installed in this order):
  1. PowerShell 7
     winget Microsoft.PowerShell, about 100 MB; Windows asks for administrator permission
  2. CUDA 12.8 compiler parts (for GPU rendering)
     NVIDIA's compiler, headers and runtime library from NVIDIA's download archives (...); NVIDIA's CUDA licence applies: ...
  3. Inno Setup 6 (makes the installers)
     winget JRSoftware.InnoSetup, about 10 MB; Windows asks for administrator permission
The result is an installer (Setup) for each Houdini build you build for, for your own use. The build itself installs nothing.
Install all of these?  y = yes to all,  a = ask me for each one,  n = install nothing
```

Type `y` and press Enter to install all of them, `a` to be asked for each one, or `n` to install nothing. Saying yes
accepts the licences of the tools in the installation list. Programs are installed with winget, Windows' package manager. When Windows asks
"Do you want to allow this app to make changes to your device?", answer Yes. A "no" is respected: the run stops and
says what is still missing; only the CUDA compiler parts (without them the build is CPU-only) and Inno Setup
(without it no installer is made) are optional. After the check, the builder says which NVIDIA parts the result
will contain. When there is nothing to install, the builder skips the tool-installation question.

Two things it never installs:

- **Houdini** from SideFX (you probably have it already). If no Houdini 22.0 with its development kit is installed,
  the run stops with the download link; install it, then double-click the file again.
- **Your NVIDIA graphics driver.** GPU rendering and GPU denoising need an NVIDIA graphics card with its driver
  installed. Installing or updating the driver is up to you. If your driver is too old for GPU rendering, the check
  says so in one line.

New programs are found in the same window; you do not have to close it. Only if Windows itself needs a restart to
finish an install (this can happen with Visual Studio Build Tools), the window says so: restart, then double-click
the file again and it continues.

Time: 20 to 60 minutes when everything must be installed, mostly downloading (estimate; Build Tools are about 6 GB).

**2. Which Houdini.** The window lists your Houdini 22.0 builds, for example:

```
Houdini on this computer:
  Houdini 22.0.508: no Cycles yet; not the tested build
  Houdini 22.0.466: no Cycles yet
  Houdini 22.0.429: no development kit, cannot build for it (install it again with the development kit)
```

With one Houdini there is no question. With several, it asks once: press Enter for those that have no Cycles yet,
type `a` for all of them, or type one build such as `22.0.508`. A Houdini that already has Cycles for Houdini
installed by a Setup counts as having Cycles. A plugin must match the Houdini build it was made for, so each Houdini
gets its own build folder inside the project folder (for example `_work\windows-h22.0.508-release`).

**3. Get the sources and build.** Before it starts, it says about how long one build takes. It downloads upstream
Cycles, applies this project's changes to it ("patches"), downloads the other parts it needs, then compiles
everything, once per chosen Houdini. You see a lot of text scrolling past; that is normal. Everything downloaded
(the Cycles sources with this project's changes, the prebuilt libraries, NVIDIA's OptiX and DLSS headers, the CUDA
compiler parts and the tools) is downloaded once and used again for every further Houdini build, so a second or third
Houdini build downloads nothing. Each Houdini build is still compiled on its own, the engine included, because
everything is compiled against the development kit of that Houdini build (its own version files and the libraries
it brings, such as OpenVDB, TBB and OpenImageIO). The GPU programs (the "GPU kernels", the longest part of a build)
do not depend on the Houdini build: the first build compiles them and every further build reuses them, as long as
nothing they use has changed. Before an incremental build, the builder also rechecks the compiler and inputs used to
make existing kernels. The window says which: "GPU programs reused from the first build: nothing
they are made from changed", or "GPU programs compiled in ... min" with the time. The stored copies are in
`_work\kernel-cache` inside the project folder (delete that folder to clear them); to compile them in every build instead,
set `CYCLES_EASY_NO_KERNEL_REUSE=1` or start `scripts/easy-start.ps1 -NoKernelReuse` from a terminal. The first time, it also fills in values of your own in
`build-identity.json` (see Part 2) and says so.

**4. The quick tests.** For each build it renders a small red box on the CPU and, if you have an NVIDIA card with a
new enough driver, on the GPU, in that Houdini's own `hython`.

**5. The installers.** For each build that passed its quick tests it packages the build, checks the package and makes
a Setup with Inno Setup. It asks nothing here. All installers go into one folder inside the project folder,
`Installers`, one file per Houdini build, named after it, with a short text file "READ ME", for example
`Cycles for Houdini 22.0.508 (<your build id>) - Setup.exe`. What went into each one (the package, reports and logs)
is in `_work\installer-details`.

The project folder holds build output in `Installers` and `_work`. Deleting the project folder removes those
outputs. Keep `build-identity.json` if you plan to build another update. Installed Cycles files and prerequisite
programs such as Git or Inno Setup have their own locations. The builder mentions any old
`<project folder>-build` or `<project folder>-installers` output folders when it finds them.

At the end you see one line per Houdini build (built, tested, installer made, or what failed; one failing build does
not stop the others), then the list of installers with the Houdini build each is for. With you at the keyboard, the
installer folder opens in Explorer.

Time, measured on a fast 16-core workstation (Ryzen 9 7950X): about 13 minutes for the first Houdini build and about 5 minutes for each further one, installers included, because the GPU programs are made once and reused (12.9, 4.8 and 4.9 minutes for 22.0.508, 22.0.466 and 22.0.429, 22.7 minutes for all three; without the reuse it was 38.5 minutes); a CPU-only build with its installer took 6 minutes. A later "Update the sources and rebuild" without engine changes took about 1.5 minutes per Houdini build, new installers included. On an ordinary computer expect it to
take two to four times longer (estimate).

### Install Cycles

Double-click the installer for your Houdini build. Setup shows what it is (a community build, who made it, which
checks passed), asks for the Houdini folder and your Houdini preferences folder, and installs Cycles for that Houdini
build. Start Houdini and choose **Cycles Tools → Render ROP** to create a **Cycles Render** output node.

- The installers are not signed, so Windows shows "Windows protected your PC"; choose "More info", then "Run anyway".
- Each installer is for one Houdini build. A newer installer of your build for the same Houdini build (made later by
  "Update the sources and rebuild" or "Make the installers again") updates the one installed before: it has the same
  entry in Installed apps and the same folder.
- The installed package can also load in a Houdini build with the same HDK API and required libraries. If another
  Cycles package in those preferences covers any of the same Houdini builds, Setup asks you to uninstall it first.
  This can happen even when the two installer filenames list different builds.
- If Setup was interrupted, run the same Setup again and select the same installation folder. It checks the saved
  transaction before an update. If the preferences folder was removed during an unfinished installation, restore
  it from a backup before retrying.

### Uninstall Cycles

Use Windows **Settings > Apps > Installed apps** and uninstall **Cycles for Houdini - community build**. You can
do this after removing Houdini. Uninstall removes the owned package file and restores the exact original file when
Setup had replaced one. If the registration was already removed, uninstall deactivates its receipt and leaves the
removed settings alone.

The installed application's `installation` subfolder retains older renderer versions. Its `data` folder
retains artist files. Running renders can still use those files. After uninstall succeeds and renders have finished, review
and back up any files you want to keep. You can then remove this build's installation folder, separately from
the project folder which held the builder.

Double-click the start file again to return to its menu.

### When Houdini changes

When you install a new Houdini 22.0 build, the next double-click says so in one sentence and builds for it, with its
own installer (about the time shown). When you uninstall a Houdini build, it says that its build is no longer used.

If the automatically added build fails, double-click the start file again to retry it. The builder reports which
Houdini builds completed.

## When something fails

Do not worry; nothing is broken. A failed run leaves your Houdini and your files as they were.

1. Read the last 20 lines in the window. They usually say what is missing and what to do (the NEXT line).
2. Double-click the file again once. Downloads sometimes fail for a moment.
3. If it fails in the download step and mentions `third_party/cycles.incomplete`: delete that folder inside the
   project folder and try again.
4. Keep the text: the window stays open at the end so that you can read it. Click into the window, press Ctrl+A and
   then Ctrl+C to copy everything it showed. The installer step also saves its logs and reports, in
   `_work\installer-details\<Houdini build>-<time>\` inside the project folder.
5. If it says that it is already running in another window: use that window. Only one run at a time can work on the
   project folder.

When you ask for help (on the project's GitHub "Issues" page, or from a friend), copy:

- what you chose (first run or a menu number) and the last 50 lines of the window or the log;
- your Windows version, your graphics card, and your Houdini version (Help > About Houdini);
- the project version: on the GitHub page, or `git log -1` if you use Git.

Remove anything private before you post it, such as your user name in folder paths.

## Part 2: give an installer to someone else

The installers from Part 1 can also be given to others: someone double-clicks one to add Cycles to their Houdini
build, without building anything.

> **Before you give an installer to someone else, read this.** An installer contains compiled parts from other
> companies and projects, for example code built from NVIDIA's OptiX and DLSS headers, GPU programs made with
> NVIDIA's CUDA compiler, Blender's interface icons (GNU GPL), and many libraries. Each one has its own licence, and
> some have conditions for anyone who passes them on. If you hand an installer to someone, you are the distributor,
> and meeting those conditions is your responsibility. `THIRD_PARTY_NOTICES.md` lists the components and
> [docs/dev/nvidia-licences.md](../dev/nvidia-licences.md) explains the NVIDIA parts. We are not lawyers, and this
> guide is not legal advice.

Your installers must not appear under the project's name as publisher ("The Cycles for Houdini Authors"). They show your name as the publisher and use their
own identifier, so Windows does not mix them up with the original. Open `build-identity.json` (in the project folder)
in Notepad and change:

- `build_name`: a short name for your build, for example `Anna hair fix`. Windows Settings > Apps shows "Cycles for
  Houdini - community build (Anna hair fix)".
- Set `publisher` to your name. Use your web address or your fork's GitHub URL as `publisher_url`, or leave it empty.

Then choose **3** (Make the installers again). For `build_name` and `publisher`, you can use letters, digits, spaces
and `. , ( ) _ + -`. The official publisher name and repository URL are reserved. Until you name your build, the
installer says "Unnamed community builder" (fine for your own use). Two values are filled in for you the first
time you build. These are `build_id` (your own id, for example `anna-berg-3fa9c1`) and, with DLSS on, `dlss_project_id`.
Keep them afterwards so that a new installer of yours updates the earlier one. For a new ZIP download, copy this
file from your previous project folder before you build.
The builder records changes to this file separately from changes to the source code.

DLSS (NVIDIA's denoiser for the interactive viewer) is on by default (`"dlss": "on"` in `build-identity.json`) and
uses `dlss_project_id`, an NVIDIA project identifier of your own: a random code made for you, as NVIDIA's DLSS
programming guide allows for custom programs. The default community mode rejects the project's identifier. Menu item 6
("Turn DLSS off") sets `"dlss": "off"`: then nothing from NVIDIA's DLSS SDK is downloaded or built in, and no project
identifier is made. The builder downloads DLSS only after your NVIDIA approval and when it has a CUDA compiler and
detects an NVIDIA graphics card.

## Part 3: changing something yourself

You can change the project for your own build: fix a bug you see, add a small option, change a default. Before
you change anything, read the [developer guide](../dev/README.md): it shows where each part of the project is, how
to build and test, and the rules every change follows.

### Always work on a copy

Make a new Git branch for the change (`git switch -c <short-topic>`). A branch is a separate line of work inside the
same folder: your working version stays as it was, and you can switch back to it at any time. If you prefer, copy
the whole project folder first as well.

### Make the smallest change that works

Change as little as possible, and add or extend a test that shows the change works. Never make a test "pass" by
loosening or deleting it; the developer guide explains why.

### Check the result

1. Run the tests the developer guide names for this kind of change
   ([which tests for which change](../dev/README.md#which-tests-for-which-change)).
2. If the change is inside the engine (`third_party/cycles`), double-click **`START HERE - Build Cycles for Houdini.cmd`**
   and choose **5** (I changed the engine: update the patch files): it saves the change as a new file in `patches/` so
   that a fresh copy of the project builds the same engine. It asks before it changes anything, writes the time in
   UTC, and uses your publisher name with a placeholder address (never your e-mail unless you agree).
3. Build and test again: choose **1** (Update the sources and rebuild; it runs the quick tests and makes new
   installers too).
4. Try it before you install it: choose **4** (Open Houdini with my build, without installing) and render a scene you
   know, before and after the change. Compare the two images. Then install the new installer if you want to keep it.

### Share your change

- **Code:** make your own copy of the project on GitHub (a "fork"), put your branch there, and, if you like, propose
  it to the original project as a "pull request". [CONTRIBUTING.md](../../CONTRIBUTING.md) says what is needed.
- **An installer:** follow Part 2, with your own build identity. Say clearly that it is your build, what you changed,
  and which version of the project it is based on. Never publish it under the project's name as publisher.
- **Licences:** use Apache-2.0 for changes to the Houdini integration and GPL-3.0-or-later for changes to the
  shipped Blender component. Use documented behaviour and public APIs when adding compatibility with Blender.
  [CONTRIBUTING.md](../../CONTRIBUTING.md) explains the contribution and attribution rules.
