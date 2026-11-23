# Native Windows release packaging

The release tools build a relocatable, versioned package from an existing staged
host. They do not build or run Houdini. A matching licensed Houdini installation
remains required; Houdini DLLs, Python, Qt, OIIO, OCIO, USD, OpenVDB, OpenSubdiv
and TBB supplied by that installation are not copied. CUDA and OptiX kernels,
the persistent worker, native host, HDAs, Python tools,
viewer definitions and selected dependency DLLs remain together in one immutable
version. Runtime discovery uses that version rather than the developer checkout.

The default OBJ/SOP build disables USD/Hydra. Its SDK image, VDB and subdivision
dependencies are selected explicitly by `cmake/houdini-sdk-native.cmake` rather
than through USD discovery. `scripts/build.ps1 -Solaris -ImageIris` selects the
separate experimental `build/windows-h22-solaris` tree and includes the delegate.
Native staging and packaging reject Solaris plugin artifacts; old delegate
binaries remaining elsewhere in a reused build directory are not packaged.

The pinned denoiser provider is Houdini 22.0.429's Open Image Denoise 2.3.3.
Configuration selects that SDK's headers and import library; staging freezes
HFS-relative file identities and SHA-256 hashes in
`host/resources/sdk-dependencies.json` and the build manifest. Startup verifies
the installed SDK files and actual loaded provider in both Houdini and every
worker. A matching installation can be located at a different filesystem path.
An unsupported version, changed DLL or preloaded competing provider fails
clearly; PATH ordering alone is not treated as proof of provider identity.
The package excludes the competing vendor OIDN 2.5 DLL family and never copies
SideFX SDK DLLs. This intentionally changes worker denoising from 2.5 to 2.3.3;
image/model equivalence and equal performance are not claimed. High/Balanced/Fast,
guide prefilters, guide selection and optional CUDA denoising remain available.

Use ordinary Python 3.12 or later. These commands intentionally require explicit
directories and never discover or change a user's Houdini preferences implicitly.

## Audit and build

```powershell
python scripts/release-package.py audit --source-root . --build-root build/windows-h22-release --report artifacts/release-dependency-audit.json
python scripts/release-package.py build --source-root . --build-root build/windows-h22-release --purpose internal --output artifacts/cycles-houdini-candidate.zip --report artifacts/release-build.json
python scripts/release-package.py verify artifacts/cycles-houdini-candidate.zip
```

The audit verifies staged artifact hashes, detects edited compiled/runtime
sources, parses x64 PE imports (including delayed imports), checks bundled versus
Houdini/Windows/compiler/GPU runtime prerequisites, and verifies each selected
library's pinned license inventory. Headers and static libraries contribute
notices even when they do not produce a copied DLL. Unresolved imports fail the
dependency gate. Conflicting SDK/bundled DLL providers fail the same gate.
LFS pointers are
never packaged as libraries. A required LFS dependency fails import closure.

The machine-readable audit has `result`, `gates.build_sources`,
`gates.dependencies`, `gates.notices`, `runtime_fingerprint`, `imports`,
`unresolved_imports`, `issues` and per-component `licenses`. A blocked audit exits
2; malformed input exits 1. An internal archive can be created while notice
review is blocked, but build-source and dependency gates must pass. Its purpose
is explicitly recorded and installation requires `--allow-internal`.

ZIP entries have sorted paths, fixed timestamps and permissions, and no embedded
build time. The same frozen staged manifest, selected runtime, notices and
certification produce the same archive bytes. A manifest records every payload's size and SHA-256 plus a
content-derived package identity. Verification rejects extra files, traversal,
Windows reserved names, case collisions, links, invalid identities and hash
changes. An external SHA-256 supplied through `--expected-sha256` checks the
publisher's archive checksum; embedded checksums alone do not authenticate a
publisher. An opt-in Solaris package's generated Hydra plugInfo uses relative paths.

For a public candidate, run the audit with `--purpose public` and provide the
independent release verification certificate to `build --purpose public
--certification <report.json>`. Every audit gate must pass. The certificate
records `result: "passed"` and the exact `runtime_fingerprint`,
`public_payload_sha256`, and `parent_build_manifest_sha256` values from
`public_certificate_bindings` in the audit report. The audit supplies the
bindings for review. Release verification supplies the passing result.

Public packages retain the asset seal bytes and their existing SDK path.
Use the standard Program Files Houdini directory on the build machine or
pass the same reviewed, nonpersonal SDK directory to both commands with
`--public-sdk-root`, for example `--public-sdk-root "D:\SDK\Houdini 22.0.466"`.
End users resolve the SDK from their local Houdini installation. The public
build manifest omits the builder's SDK, compiler and OCIO paths as well as
GPU UUIDs and PCI bus IDs. Keep the original byte-for-byte build manifest
alongside internal qualification evidence. The public manifest records its
SHA-256 and the release source gate checks the exact projection when the
original is available. Review archived patch-triage labels, branches, receipt
paths and upstream source references as part of publication review.

The payload digest covers the canonical release manifest and all its shipped
file records except the certificate's own record. The binding and package
identity fields are omitted from that digest. The outer package identity
includes the certificate's record, and the publisher checksum covers the
complete ZIP including the certificate bytes. Existing installer signature
verification continues to cover the signed installer if one is issued.

The runtime fingerprint includes host Python/HDAs,
menus/resources, native binaries, kernels, dependency DLLs and pinned engine
patch/source identity and pinned SDK dependency identities. A Python-only runtime edit invalidates an earlier runtime
certificate. Hydra descriptor replacement bytes are planned once, hashed after
relocation normalization and emitted unchanged. A transformation-only change
invalidates certification; equivalent developer paths that normalize to the
same descriptor retain the same runtime identity. Creating a local candidate
does not publish or approve a release.

## Install, recover and uninstall

Test against a new preference directory before an artist installation:

```powershell
python scripts/release-package.py install artifacts/cycles-houdini-candidate.zip --install-root artifacts/release-test/installation --preference-root artifacts/release-test/houdini22.0 --data-root artifacts/release-test/data --allow-internal
python scripts/release-package.py status --install-root artifacts/release-test/installation
python scripts/release-package.py uninstall --install-root artifacts/release-test/installation
python scripts/release-package.py recover --install-root artifacts/release-test/installation
```

Installation requires a dedicated empty or already owned install root. The
payload goes into `versions/<first 16 hex digits of package_id>` (the full id stays
in its `release-manifest.json` and in `state.json`; a payload installed earlier under
the full 64-digit name is still found there); activation changes one Houdini package
JSON atomically.

Installed path length: Windows paths must stay below 260 characters. A package is
built only when its longest path has at most 86 characters: with `installation\versions\<16 hex>\` (39 characters) that is at most
125 below the app folder, so an app folder of up to 110 characters
(`C:\Users\<20-character name>\AppData\Local\` plus the longest default folder name
with a 32-character build_id) stays within 240 in all. Deep licence paths of a
component are stored flat: `notices/<component>/<12 hex of the original path>-<file name>`
when `notices/<component>/<original path>` would be longer than 64 characters;
`notices/index.json` in the package names the original path of every stored notice.
The install step checks the longest path it would write against 259 characters
before it writes anything and otherwise stops with one sentence that names the
folder and says how much shorter it must be. Not counted: the bytecode that
Houdini's Python may write next to a `.py` file later
(`__pycache__/<name>.cpython-313.pyc`, 25 characters longer); Python skips writing it
when the path does not fit and reads the `.py` file instead. The explicit data directory receives writable worker artifacts
instead of writing into the package or developer source tree. The configuration
enables only the compiled Houdini version. Qualified exact hosts are 22.0.429
and 22.0.466. Their registrations are `cycles_houdini_22_0_429.json` and
`cycles_houdini_22_0_466.json`, allowing one shared preferences directory.
`identity --houdini <exact-version>` reports the stable per-target Windows
application ID, filename and default application directory. A new target needs
qualification before entering this installation workflow.
Background jobs use a unique private package and the child-only
`HOUDINI_PACKAGE_SKIPLIST` to exclude mutable product registrations. The
original preferences, unrelated packages and preference-installed HDAs remain
available. The child verifies Python, native DLL, resources and release identity
against the submitting runtime.
The loader uses the active `$HFS/bin` provider before private dependency paths;
the runtime's loaded-module guard separately detects preloaded conflicts.

An existing unmanaged package is refused unless `--replace-existing` is given.
That option preserves its exact original bytes through upgrades and restores
them on uninstall. Changes made externally after activation are preserved and
reported rather than overwritten. Symlink/junction paths and concurrent install
mutations are refused. OS-held locks release automatically after a crash.
An exact registration owned by another installation cannot be replaced with
`--replace-existing`. A registered Windows target updates in place; moving it
to another `/DIR` requires a separate supported relocation workflow.

Setup requires a dedicated new/empty application directory or its existing
owned directory. It rejects overlapping preference/data paths, another target's
tree and all known legacy recursive-uninstaller trees. Its default exact-target
directories are siblings of the old generic installation, never children.
When an owned legacy `cycles_houdini_dev.json` has a verified active manifest for
another host, it stays untouched. When it matches this target, a journal transfers
the exact unmanaged backup, writes the new exact registration, removes the owned
generic registration and retires the old active receipt. The old uninstaller
then sees no active installation and cannot restore the generic package. The
new uninstall restores the original unmanaged generic bytes; changed or
ambiguous ownership is preserved and reported as a conflict.

Each activation has a prepared journal. If interrupted, the next mutation
requires `recover`, which restores the prior package and receipt; it refuses to
overwrite an external edit made after interruption. Installation verifies the
archive again on its extraction handle and verifies extracted payloads before
activation. Existing version directories are hash-checked before reuse.

Uninstall deactivates the package and restores any previous configuration. It
retains immutable version directories for rollback and avoids deleting loaded
DLLs. After closing Houdini and its workers, the user can remove the dedicated
owned installation directory when those versions are no longer needed. Runtime
data and unrelated preference files are retained.

## Windows installer

The current SDK target is Houdini **22.0.466** (HDK API **22000453**), with
Houdini's OpenImageDenoise 2.3.3 libraries. To preserve another build directory,
use `scripts/build.ps1 -BuildDirectory build/windows-h22.0.466-release -ImageIris`.
The package's exact-version condition is generated from the staged SDK report;
22.0.429 installers remain separate and must not be relabelled as 22.0.466.

`scripts/build-installer.ps1` wraps a package in a normal Windows Setup.exe
(Inno Setup 6 required to build it, not to run it):

```powershell
pwsh -File scripts/build-installer.ps1 [-Package <zip>]
pwsh -File scripts/test-installer.ps1 [-Setup <exe>]
```

The build verifies the package with its own tool, embeds its SHA-256 and writes
`artifacts/installer/CyclesForHoudini-<houdini>-<date>-<id>-Setup.exe` with a
JSON report. Setup installs per user without administrator rights, defaults to
`%LOCALAPPDATA%\Cycles for Houdini <exact-version>`, and runs the same verified `install` with
Houdini's bundled Python, so the target needs nothing besides Houdini. A page
selects the Houdini installation (registry, then the standard folder) and the
preferences folder (`HOUDINI_USER_PREF_DIR` when set, else
`Documents\houdini<major.minor>`). The exact Houdini build is read from that
installation's `toolkit/include/SYS/SYS_Version.h`, the file Cycles checks at
load; any other build, or a missing toolkit, stops Setup before anything is
installed. An existing unmanaged package file is replaced only with consent and
restored on uninstall. Uninstall (Windows Settings > Apps) deactivates through
the tool first. Runtime versions and mutable data are retained so running
renders can finish. Other targets' Apps entries and registrations are untouched.
Private installers are unsigned by default and Windows SmartScreen can display a publisher warning.
To sign Setup, set `CYCLES_HOUDINI_SIGNING_THUMBPRINT` to a trusted code-signing certificate in the current user's Personal store and optionally set `CYCLES_HOUDINI_TIMESTAMP_SERVER` to the publisher's timestamp service. The build verifies the signature and signer before recording the final EXE hash.

Setup invokes Python directly and logs through the helper, preserving literal
percent characters in custom paths. Activation has a pending Setup journal with
a unique per-run token until the final installer step commits. Cancellation or
failure calls token-scoped `abort-setup`, restoring only that run's changes.
An interrupted process requires explicit `recover` before another update. The
journal is bound to its installation marker, exact target and preference root;
external receipt or registration edits block recovery instead of being lost.

Setup uses Inno Setup 6.7 or newer's built-in `modern dark` styling. The v2
Cycles for Houdini wordmark appears on the welcome/finished pages and in a wide
header slot, preserving its proportions. Branding is extracted from the verified
package's `host/config/branding/`, not from an unrelated source checkout. To
regenerate the committed assets after editing the PDF-compatible Illustrator
artwork, run `python scripts/export-branding.py` with PyMuPDF and Pillow installed.
Those libraries and Illustrator are not build or installation dependencies.
There is no custom ROP Info tab or parameter-interface replacement.

Unattended: `/VERYSILENT /SUPPRESSMSGBOXES /DIR=... /HFS=... /PREFS=...
[/REPLACEEXISTING=1]`. `test-installer.ps1` uses this in isolated folders: it
checks refusal of another Houdini build, a fresh install with a headless hython
loading Cycles from the installed copy, reinstall, uninstall, and byte-exact
restoration of a replaced package file. It refuses to run over a real
installation, whose target-specific Windows Apps entry a test install would
take over. Use `HOUDINI_USER_PREF_DIR=<isolated-parent>/houdini__HVER__` when
launching the host; install the registration into the expanded `houdini22.0`
directory. A concrete preferences path is not a reliable isolated host launch
setting in these Houdini builds. Full dual-host acceptance and real legacy
uninstaller qualification are additional release gates.

## Notices and validation

`packaging/notices/index.json` records exact dependency versions from the pinned
`windows_x64/deps.md`, immutable upstream revisions or versioned distribution
archives, source URLs, file hashes and coverage state. Refresh it explicitly:

```powershell
python scripts/fetch-release-notices.py --output-root packaging/notices
python -m unittest discover -s tests -p release_package_test.py -v
```

Notice capture is the only network operation. The packager/installer is offline.
The source fetcher preserves upstream text without rewriting licenses. A
`review_required` component remains a public gate failure; generic root license
text is not presented as proof of a vendor binary's transitive inventory or SDK
redistribution permission. An enabled OptiX backend or a packaged OptiX kernel
requires three files under `notices/optix-headers/`: the DesignWorks agreement,
NVIDIA's license overview and the copyright and license blocks from the pinned
SDK headers. Missing or altered files block internal and public packaging.
The public audit also checks the SDK revision and publisher review status.

The pure tests use unique directories inside `artifacts/packaging-unit` and
exercise deterministic archives, PE parsing, tampering/path rejection,
relocation, install/upgrade/uninstall, simulated interrupted activation, recovery,
external edits and exclusive locking. They import no Houdini modules. Native
acceptance must separately launch the relocated candidate against an isolated
Houdini preference root and exercise CPU/OptiX, worker restart, background final,
viewer and official delegate before public certification.

`tests/sdk_dependency_policy_test.py` verifies relative SDK identity, relocation,
mismatch and foreign/preloaded-family rejection without launching Houdini.
`tests/houdini_oidn_provider_smoke.py` uses a noisy diffuse/occluded fixture,
exercises every OIDN quality/prefilter/guide control in synchronous and worker
renders, checks actual loaded paths/hashes/versions, and writes EXRs. Run it on
CPU and each supported OptiX device; GPU renders also test CPU and GPU OIDN.
Cross-render normal/data comparisons use explicit numerical tolerances;
Cryptomatte comparison uses exact bit patterns. A JSON GPU-ID array selects one
mixed-device session, with actual selected devices verified. Both paths must
close with zero owned allocations and workers must exit successfully.

The October 20 final internal candidate passes nineteen packaging and twenty-two
SDK policy checks, full-byte deterministic reproduction, isolated activation and
exact interrupted-journal recovery. Five relocated native runs verify CPU and
both GPUs' SDK providers plus CPU/4090 material repair, worker restart and ordered
final output. All 77 host/runtime/native-tool files equal the verified D payload.
See operations (`artifacts/release-sdk-final-operational.json`) and
the combined source-archived evidence (`artifacts/parallel-completion-final-evidence.json`).
This local matching-HFS check does not certify clean-machine installation,
public redistribution or all viewer/delegate workflows.
