# Maintained release workflow

The project root is the production source for Houdini 22.0.466. Package `82355eb8` is the feature-preservation baseline. [source.lock.json](../../source.lock.json) identifies the upstream revision, shipped fork commit and ordered patches for this checkout. The installer workflow builds with DLSS and Image Iris enabled, renders Gaussian splats on CPU and OptiX, and checks a raw PLY splat with DLSS and Image Iris in the installed OptiX worker.

Run the qualified target from `source.lock.json`:

```powershell
pwsh -NoProfile -File scripts/build-release.ps1
```

Use the `-DryRun` switch with the script or its launcher to check the release
inputs and print the plan. It requires the locked private engine and exact
Houdini SDK. The plan creates no build or output folder and opens no windows.

The optional focused checks add eight short fixture invocations to the normal
release gates:

```powershell
pwsh -NoProfile -File scripts/build-release.ps1 -FocusedChecks -OptixDevice "<exact OptiX device ID>"
```

They run the installed argument/buffer and frame-ownership contracts once,
then test status/frame conversion and independent Blender RMSE/SSIM references
on CPU and the selected OptiX GPU. The subdivision-normal pair uses the matching
staged host and its manifest-verified harness, which the installed package need
not ship. Existing built/installed Image Iris, Gaussian splats, optical, farm
and Viewer checks still run in full. The existing combined splats/DLSS/Image Iris
gate also checks the exact authored temporal sample total in status and the
published frame. These additions reuse those renders.

`-OptixDevice` accepts an exact device ID and defaults to an explicitly selected
`CYCLES_HOUDINI_TEST_OPTIX_DEVICE`, when present. Runtime reports verify that ID
and its OptiX backend, including direct rollback launches. Blender maps the
selected GPU by name and available PCI identity and rejects ambiguity. Each
focused fixture has private preferences, worker logs and temporary artifacts.
The eight calls are additional fixture invocations; their internal renders are
reported by the fixture rather than counted as separate release invocations.

The 518-case nightly stays independent. `-QualifiedNightly <run-directory>`
accepts only its complete verified result against the exact staged manifest;
`-FocusedChecks` records its own scope. `release-report.json` and the installer
JSON sidecar record `qualification_mode`/`mode`, actual native and focused
invocation counts, and `full_nightly_qualified` plus `nightly_case_count`. A
focused run with no separately qualified nightly records `false` and `0` for
those nightly fields. The tested manifest remains fixed through the normal
per-test source/stage audit, package preservation and installer archive hash
checks.

Double-clicking `launchers/Build and make installer.cmd` runs this same workflow. A previous installer or renderer build is not required. An explicit same-target reference can additionally qualify rollback:

```text
"launchers\Build and make installer.cmd" -RollbackPackage "E:\Releases\previous-qualified-466.zip"
```

`-HoudiniTarget`, `-HoudiniRoot`, `-BuildDirectory`, `-OutputDirectory`, `-BaselineManifest`, `-ExistingHip`, `-QualifiedNightly`, `-FocusedChecks`, `-OptixDevice` and `-Jobs` are optional. The normal preset places the build in `_work/dev/windows-h22.0.466-release`. Each release gets its own directory under `_work/releases` containing the installer, verified package, manifest and reports. A fresh checkout creates these locations on its first build. Explicit build and output paths still work.

Before compilation, the workflow checks the exact target SDK, HDK API and OIDN provider. A supplied rollback archive provides the reference manifest. `-BaselineManifest` supplies an explicit same-target override, recorded in the report. `-ChooseRollbackPackage` opens a ZIP chooser. The default existing HIP is the maintained fixture in `tests/fixtures`; an alternate HIP must satisfy its material contract.

The current lock qualifies .466. Qualifying another SDK means updating its SDK/source record and testing that target; rebuilding a historical runtime is not a dependency. The default gates require the locked commit, exact ordered patch hashes, the configured kernel inventory, the actual built-engine fingerprint and all production features. They do not hardcode a historical patch count. With a reference, the gate additionally requires matching engine, kernels, features and SDK. A renderer change still requires affected backend/feature-off verification under the [patch contracts](../renderer-patch-contracts.md).

Each run takes an exclusive release build lock and freezes the integration snapshot and tested build manifest in its release directory. Version 3 source snapshots bind the clean fork fingerprint and maintained integration inputs to a Git object pack containing the upstream and fork commits with their shallow boundary. `snapshot.json` gives restoration steps. Earlier snapshots remain archives. New qualification requires the committed fork and its locked patch replay. Dependency and OptiX revisions remain pinned by `source.lock.json`. The exact external SDK and toolchains are prerequisites.

Pass `-FastCIReceipt <receipt.json>` to bind qualification to a clean commit, its green fast-tier summary and its tested manifest. The release directory retains the receipt. The standalone installer selects the newest passing report for the requested Houdini target across both evidence directories and verifies its ZIP. With no qualifying report, supply the archive explicitly with `-Package`.

The command replays the complete patch stack and compares the tracked engine plus additional source files, builds and stages once with Image Iris enabled, checks staged Python/native/HDA/resources/worker identities, and renders Image Iris on CPU and a CUDA/OptiX device with both samplers. It packages that tested manifest and installs it into a new isolated root. Installed checks cover Image Iris again, saved ROP layout/channels, the existing material HIP, relocated worker Stop/restart/recovery, saved-HIP farm rendering and Viewer render/edit/Stop. If a rollback package was supplied, it additionally exercises upgrade and restoration of that different runtime. The report records whether this optional branch ran. Its isolated registration is removed; owned payloads and evidence remain for review. Full engine replay and source snapshot checks run again before installer creation.

The generated installer embeds the exact tested archive SHA-256. `release-report.json` records source snapshot, manifest, package and installer identities, target and every completed gate. A changed maintained source input or changed packaged manifest fails qualification. A failed run retains its logs and records failure; it never marks the candidate passed. Review source changes before restarting with a fresh output directory.

An upstream rebase cannot satisfy the ordinary exact-prefix baseline comparison. Use `-RebaseApproval <receipt.json>` only with the explicit receipt named by `cycles.release_rebase_approval` in the source lock and the previous baseline or rollback package. The receipt binds the previous build-manifest SHA-256, new upstream and fork commits, ordered patch hashes, engine fingerprint and a hashed review decision. Any changed input expires it. The rebase comparison retains identical SDK, feature flags, dependency revisions and kernel architecture names, while permitting reviewed source and compiled kernel changes. The normal runtime, Image Iris and rollback checks still run. The package carries triage, feature decisions, integrator grants and the rebase receipt alongside its source lock and patches; it does not claim byte preservation across the rebase.

Every release also renders the high-risk optical cases on CPU and the GPU selected by the Image Iris runtime check. The installed worker renders four overlapping dielectric stacks against analytic normal-incidence transmission, and finite-budget shiny disk-light and particle fixtures against independent higher-sample images. Reference, candidate and absolute-difference EXRs are saved alongside the measured errors and timings. A separate material suite checks selective RLS routing and pass reconstruction. The matching build's RGB Image Iris harness checks channel energy, focus and coverage alpha for both aperture samplers. The farm output fixture exercises header validation, publication races and strict missing-input behavior in the isolated installation.

The output is an **internal candidate**. This command does not activate the artist installation or certify Windows Apps/uninstaller lifecycle and two-target coexistence. Those tests remain separately required before publication. The [patch contracts](../renderer-patch-contracts.md) distinguish source consistency from targeted behavioral/backend certification.

For quick pure validation before a native build:

```powershell
python tests/release_workflow_test.py
python scripts/patch-inventory.py --check
python scripts/verify-upstream-patches.py
```
