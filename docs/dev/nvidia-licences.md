# NVIDIA parts and their licences

Short and plain: which NVIDIA item each feature needs, who accepts its licence, and what changes when someone
hands out built files. This is a description of the texts named below, not legal advice. Read 21 November 2026.

## Which feature needs what

| Feature | NVIDIA item | Licence | Where it comes from |
| --- | --- | --- | --- |
| CPU rendering, Open Image Denoise (CPU and GPU) | none | none from NVIDIA | Open Image Denoise comes with Houdini's development kit |
| GPU rendering with CUDA | CUDA compiler parts (compiler, headers, runtime library) | NVIDIA CUDA Toolkit licence (EULA) | NVIDIA's download archives, unpacked by `scripts/cuda-toolkit-parts.py` into the project folder (`_work\cuda-12.8.0`), or NVIDIA's own CUDA Toolkit installer |
| GPU rendering with OptiX, OptiX denoiser | OptiX 9.1.0 headers | NVIDIA DesignWorks agreement, plus a BSD-3-Clause or NVIDIA proprietary notice at the top of each header | NVIDIA's `optix-dev` repository, downloaded by `scripts/bootstrap.ps1` |
| DLSS in the interactive viewer | DLSS SDK v310.9.16 headers (the NGX loader is compiled from them) | NVIDIA RTX SDKs License | NVIDIA's `DLSS` repository, downloaded by `scripts/bootstrap.ps1` |
| Any GPU feature | an NVIDIA graphics card with its display driver | the driver's own licence | you install it; this project never installs, updates or downloads a driver |

The repository itself contains no NVIDIA SDK files: no headers, libraries, DLLs or sample code. It contains only the
licence texts and notices in `packaging/notices/optix-headers/` and `packaging/notices/dlss-headers/`, and a short
notice of its own about the CUDA compiler in `packaging/notices/cuda/`, so that a built package can carry them.

What the builder fetches, and when (`scripts/easy-get-and-build.ps1`, `scripts/bootstrap.ps1`):

- With an NVIDIA card, the builder asks for a separate yes or no before downloading any NVIDIA component. It lists
  the CUDA compiler parts, the OptiX headers and the optional DLSS headers with their licences. DLSS is on by
  default and can be disabled in `build-identity.json` or from the builder menu.
- If you decline, lack an NVIDIA card or lack a CUDA compiler, the builder produces a CPU build and does not fetch
  OptiX or DLSS. Declining also skips the CUDA compiler download.
- The builder remembers your choice for this source folder and set of pinned dependencies. Choose "Check this
  computer again" to change it.

## Who accepts the licence

The person who builds. Downloading and using NVIDIA's files is accepting NVIDIA's terms for them:

- CUDA EULA (read again 22 November 2026, https://docs.nvidia.com/cuda/eula/index.html: the page heading says version
  13.4, the text "Last updated: January 26, 2026", the page footer "Last updated on Oct 12, 2026"; an earlier reading
  on 21 November noted 13.5):
  section 1.1.1 allows installing and using the SDK; section 2.1 says the SDK "is licensed for you to develop
  applications only for use in systems with NVIDIA GPUs"; section 1.2 forbids distributing the SDK "as a stand-alone
  product". Attachment A lists the files that may be handed out with an application (for example the CUDA runtime
  library); the compiler is not among them.
- OptiX: DesignWorks "Software Developer Kits, Samples and Tools License Agreement (with distribution rights)",
  V.13.06.2017 (`packaging/notices/optix-headers/LICENSE.txt`). NVIDIA's OptiX download page links to this same
  2017 agreement for OptiX 9.1 (checked 21 November 2026: the licence link on
  https://developer.nvidia.com/designworks/optix/download leads to
  `DesignWorks_SDKs_Samples_Tools_License_distrib_use_rights_2017_06_13.pdf`), so it is the one in force for the
  pinned headers. Each header also carries its own notice (`HEADER-NOTICES.txt`): some are BSD-3-Clause, some
  "NVIDIA Proprietary".
- DLSS: NVIDIA RTX SDKs License, v. March 14, 2024, with the "NVIDIA RTX Supplement"
  (`packaging/notices/dlss-headers/LICENSE.txt`, the text in the pinned DLSS v310.9.16 repository). The newest
  separate RTX SDKs licence document found on nvidia.com was dated 23 January 2023, older than this text; whether
  NVIDIA's DLSS repository has a newer one after v310.9.16 was not checked.

## When someone hands out built files

A source-only release hands out no NVIDIA files: each person downloads NVIDIA's parts and builds for themselves.
Handing out an installer, or a folder with a built plugin, is different: the files then contain code compiled from
NVIDIA's headers and GPU programs made with NVIDIA's compiler, and the person who hands them out becomes the
distributor. What the texts ask of that person, in short:

- RTX SDKs License section 2 (distribution requirements): the application must have "material additional
  functionality"; distribute "subject to the terms at least as protective as the terms of this license"; source code
  derived from NVIDIA sample code carries the notice "This software contains source code provided by NVIDIA
  Corporation."
- RTX Supplement section 1: DLSS is licensed "only for their use in systems with NVIDIA GPUs".
- RTX Supplement section 4: "You are required to notify NVIDIA prior to commercial release of an application
  (including a plug-in to a commercial application)" that uses DLSS. The licence does not define "commercial
  release", so whether a free, source-only release counts is not settled by the text.
- RTX Supplement section 7.1(b) requires SDK attribution and placement of the specified NVIDIA Marks on splash
  screens, in the application's About box if present, and in credits for game applications.
- RTX Supplement sections 7.1(a) and 7.2(c) address approval. The first calls for prior review and written approval
  for identifying NVIDIA as the SDK supplier on websites, printed material, displays or retail packaging. The second
  describes the trademark licence, requires following the mark guidelines and submitting samples at least two weeks
  before planned use, and treats a lack of response within ten business days as no approval. Section 7.3(b) also
  requires prior written approval to combine marks with other content.
- DesignWorks agreement section 1.1(ii): binaries only, for the supported NVIDIA hardware, and recipients bound by
  terms consistent with the agreement; section 1.5: "You are required to notify NVIDIA prior to use of the NVIDIA
  DesignWorks Licensed Software in a commercial application (including a plug-in to a commercial application)."
- Both texts forbid making NVIDIA's files subject to an open-source licence (RTX section 4e, DesignWorks section
  2.1(xii)).

The first-run list says, before anything is built, that the result is an installer for each Houdini build for your
own use, which parts of it are made from NVIDIA's files, and that these licences apply to you if you give an
installer to someone else. The package includes notices for the NVIDIA components it contains.

CUDA: a GPU installer contains no file of the CUDA Toolkit, only GPU programs (`kernel_sm_*.cubin`,
`kernel_compute_*.ptx`, `kernel_optix*.ptx`) compiled with NVIDIA's compiler from this project's own sources, which
may include device code from CUDA headers or libdevice. The CUDA EULA describes this compilation in sections
1.1.1 and 1.1.2. Each GPU installer carries a short project notice (`packaging/notices/cuda/NOTICE.txt`: what was made with the CUDA compiler, the EULA's
address and the version read).

## What this project does to follow the licences

a. No NVIDIA SDK material in the repository or the public export: only NVIDIA's licence texts and notices
   (`packaging/notices/`). No file carries an NVIDIA copyright line, and no code was found to be taken from NVIDIA
   sample code, so the RTX section 2b notice is not needed today (if such code is ever added, add the notice).
b. The project's Apache-2.0 licence (and GPL-3.0-or-later for `blender/`) does not cover NVIDIA's files; README and
   `THIRD_PARTY_NOTICES.md` say so. The GPL component `blender/` does not load or link any NVIDIA SDK.
c. Every build's About tab (render settings) names DLSS / NGX and NVIDIA's trademarks
   (`houdini/python3.13libs/cycles_houdini/rop/rop_help.py`, added to the node by `scripts/finalize-assets.py` in
   every build, community builds included). The About tab provides textual attribution. The Setup welcome and
   header images use the project wordmark, and the plugin has no dedicated launch splash. These surfaces do not
   supply the NVIDIA Marks named by the supplement. A distributor of a DLSS build should check the applicable
   placements and mark approval before sharing it.
d. The builder downloads and builds DLSS only after NVIDIA download approval, with a detected NVIDIA graphics card,
   a usable CUDA compiler and DLSS enabled.
e. The first-run list names the NVIDIA parts the installers will contain and says that NVIDIA's licences apply to
   whoever gives an installer to someone else; every GPU installer carries the OptiX, DLSS (when built in) and CUDA
   notices.
f. The one-file builder asks separately before fetching the listed NVIDIA compiler parts and headers, with licence
   links. Declining selects the CPU build and skips those downloads.

NVIDIA, OptiX, DLSS, RTX and CUDA are trademarks of NVIDIA Corporation; this project is not affiliated with or
endorsed by NVIDIA.

## Not verified

- Whether code in `patches/` that calls OptiX or NGX (for example `patches/0077-houdini-dlss-project.patch`,
  `patches/0092-...` to `patches/0110-...`) follows NVIDIA sample code closely enough to count as "derived"; none
  carries an NVIDIA notice, and no copied sample was found, but this was not compared line by line with NVIDIA's
  samples.
- Whether the CUDA EULA requires further notice for compiled GPU programs. GPU packages carry the project's CUDA
  notice, along with the OptiX and DLSS notices when those components are present.
- Whether NVIDIA's DLSS repository has a newer RTX SDKs licence than v. March 14, 2024 after tag v310.9.16.
