# Test execution and migration

The production Houdini version comes from `source.lock.json` and the selected
build manifest. The runners check the actual loaded host, extension, HDA and
resources inside hython. They do not activate an update or use artist preferences.

The displayed-frame benchmark runs in an owned GUI process with fresh preferences
and the GUI runner's process-tree watchdog:

```powershell
./scripts/run-gui-test.ps1 -Test houdini_displayed_frame_gui_benchmark.py -Device $exactOptixId -BuildDirectory D:/ci/build -EvidenceDirectory D:/ci/evidence/display -Artifacts D:/ci/evidence -TimeoutSeconds 240
```

It timestamps the artist action before the API call or Render click and finishes
at `canvas.framePainted`. Session identity, logical generation, physical render
revision and display epoch reject retained old frames and display-only repaints.
It reports first visible sample and first full-resolution sample separately,
five repeated warm edits, frame changes and Stop/Render restarts,
median/nearest-rank p95, largest 5 ms UI heartbeat gap, device and
cache conditions, process memory and session allocation counters. The single
cold-start sample starts at the first Render click; Houdini launch is excluded.
Stop acknowledgement is preparation time, reported
separately from the following Render click. OS and GPU driver caches are not
flushed. Set `CH_DISPLAY_REPETITIONS`, `CH_DISPLAY_RESOLUTION` and
`CH_DISPLAY_SAMPLES` to change the declared workload.
`CH_DISPLAY_CASES=session_replacement` with `CH_DISPLAY_REPETITIONS=1` runs
only the initial Render and one internal `session.close()` injection, recording
at most 32 status transitions. This is an opt-in diagnostic, excluded from the
primary timing summaries. Repeated injected retirement was inconclusive; it
does not measure a normal artist action or establish a product failure. An
unavailable injected replacement reports inconclusive. A future artist session
replacement benchmark should switch ROPs or use another normal UI lifecycle.

An optional `CH_DISPLAY_REFERENCE_MANIFEST` supplies independent full-size
display RGB uint8 `.npy` references and a normalized RGB RMSE threshold; its
schema is documented in the benchmark's module docstring. References load before
timed actions. Fixed-quality time uses the qualifying paint timestamp; comparison
cost is reported separately and may affect subsequent paints. The metric uses
display-referred 8-bit RGB, with no raw radiometric quality claim. GL framebuffer
comparison and incompatible or retained QImages are explicitly unavailable.
Without those references,
fixed quality is unavailable, not zero. Sample counts alone do not establish
equal quality across renderers. Qt paint completion does not certify compositor
scanout. Karma presentation is explicitly unavailable in this harness: husk
internal rendering statistics must be reported separately with their own timing
definition and cannot stand in for displayed-frame measurements.

The bounded K33 ramp/curve check is opt-in:

```powershell
./scripts/run-k33-gates.ps1 -BuildDirectory D:/ci/build -Artifacts D:/ci/evidence/k33
```

It imports pinned Blender material records and compares 15 actual EXR cases on
CPU and OptiX at absolute error 3e-8. At two nonlinear RGB probes the optimized CPU
may also return the documented adjacent float32 value recorded by Blender OptiX.
The gate then checks animated true displacement
against an animated scalar and a frozen negative control on both devices.
Regenerate the committed reference with pinned Blender `fbe6228777e7` and
`scripts/generate-blender-ramp-curve-gate.py`. Saved `.blend` files and EXRs are
supporting artifacts; the committed exports and measured pixels make the
regression reproducible without archived private executables.

Prepare the local pytest target once with ordinary Python (which supplies pip):

```powershell
./scripts/bootstrap-test-deps.ps1 -Download
```

This creates `wheelhouse` and `dependencies` beneath
`<project>/_work/test-tools/<requirements-sha256>/`, inside the project folder's
ignored working folder (shared by its worktrees; never `%LOCALAPPDATA%`, which
packaged desktop apps see virtualized). Bootstrap and the test runner use the same path resolver.
All five universal
wheels and their transitive dependencies are version-pinned and SHA-256 locked
in `tests/requirements.txt`. Save the entire wheelhouse, including its manifest
and requirements file, as a CI dependency artifact. On a machine without network
access, restore that directory and run:

```powershell
./scripts/bootstrap-test-deps.ps1 -Wheelhouse D:/ci/test-wheelhouse
```

No `--user` or global pip installation is used. The same target supports ordinary
Python and Houdini Python 3.13. A missing pytest target fails the tier; it never
silently skips discovery. `-TestDependencies` selects another provisioned target.

```powershell
python -m pytest -c tests/pytest.ini tests/pytest_cases -m pure --artifacts D:/ci/evidence/pure
./scripts/run-tests.ps1 -Tier fast -BuildDirectory D:/ci/build -Artifacts D:/ci/evidence
./scripts/run-tests.ps1 -Tier nightly -BuildDirectory D:/ci/build -Artifacts D:/ci/evidence -TimeoutSeconds 900
./scripts/run-tests.ps1 -Tier nightly -BuildDirectory D:/ci/build -ListTests
```

On a workstation with two OptiX devices, run independent fixture groups with separate preferences and evidence directories. Supply each device's exact ID from the verified runtime. Both backend cases for a fixture and its report producers run in the same group.

```powershell
python scripts/run-nightly.py run --run D:/ci/evidence/nightly-01 --build-directory D:/ci/build --jobs 2 --optix-device $gpuA --optix-device $gpuB
python scripts/run-nightly.py verify --run D:/ci/evidence/nightly-01 --build-directory D:/ci/build
```

Pass the verified directory to `scripts/build-release.ps1 -QualifiedNightly` to package the exact staged runtime that passed. The release runner also executes its installed-device, image and rollback checks.

For a shorter installer check after changes to the bindings, use the normal
installer launcher with `-FocusedChecks` and the previous qualified package:

```powershell
& ".\launchers\Build and make installer.cmd" -FocusedChecks -Jobs 4 -RollbackPackage $previousPackage
```

This runs the regular release checks plus binding compatibility, CPU and OptiX
Blender image comparisons at two frames, and moving subdivision normals. The
release report records the checks executed. The separate nightly has 529 cases,
including the CPU first-image check without Blender.

For direct Houdini discovery, set `PYTHONPATH` to the source
`houdini/python3.13libs` directory followed by the provisioned dependency
target, set `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`, use an isolated
`HOUDINI_USER_PREF_DIR`, and run `hython -m pytest -c
tests/pytest.ini tests/pytest_cases -m pure`. Native discovery additionally needs `--native` and the
verified runtime context supplied by the tier runner. Bare pytest defaults to a
fresh `_work/test-runs/pytest/<timestamp-id>` directory of the checkout,
printed in its session header. `--artifacts` overrides that default; an explicit
`CYCLES_HOUDINI_TEST_ARTIFACTS` remains honoured. This pytest-only default leaves
direct legacy release/farm/data and GUI producer/consumer lookup paths intact.
Native pytest cases spawn
fresh verified processes, so fake-host unit tests cannot contaminate a render.

`fast` has 17 cases: the existing CPU gates, real pytest discovery, the
small native review contract targets, a CPU first image without Blender, and the
community build checks (`community_build_test.py`).
It is intended for the per-change budget. `extended` has 29 cases, retaining
its existing CPU/OptiX, Blender and GUI checks. `nightly` has 529 cases and runs
the entire compatible broad mapped headless table, with explicit producer
dependencies so generated motion, texture and courtyard scenes precede consumers:
every script exposing `--device` runs on CPU and OptiX; scripts with an internal
backend policy run once and are explicitly labelled `script-declared`. This is
over 500 host processes and can take hours. The first mapped showcase refresh
regenerates current/adaptive HIP and OCIO fixtures from checked-in generators.
The separate release verification/installer/farm workflow remains unchanged.

Nightly requires a matching native build and harness, its kernel/resources,
licensed .466 hython, bundled NumPy/OIIO/OCIO/PySide, an NVIDIA OptiX device, and
a Blender executable (set `CYCLES_HOUDINI_BLENDER` explicitly for CI). Blender
references are generated in that run. Offscreen Qt checks are headless; cases
requiring an interactive Houdini session remain in the GUI/release workflow.
Two workstation-specific mapped checks require two CUDA/OptiX GPUs or DXGI
per-GPU telemetry; they retain their normal custom-run mapping and have explicit
portable-nightly exclusion reasons in the catalogue. The portable nightly also excludes calibrated RTX 4090 DXGI telemetry. These checks remain available through -Tests.
`-ListTests` records the exact backend, evidence and prerequisites in the plan.

`tests/support` supplies owned HOM fixtures, shape-aware finite RMSE, local
Gaussian-window SSIM, artifact routing and process-tree cleanup. `-Artifacts`
relocates reports, generated HIP/EXR/OCIO fixtures, logs, preferences and temporary
files. Producer/consumer paths use the same selected artifact resolver. Legacy
pure tests and their disposable package/Git fixtures also write there. The
runner disables source bytecode output. Worker logs honour
`CYCLES_HOUDINI_WORKER_LOG_DIR`, set to the selected run by the test environment.
A mapped report is required at its
redirected path; an old source-checkout report cannot satisfy missing evidence.
The dependency audit covers the 315 mapped scripts' HIP/EXR/NPZ/JSON readers,
including fixture helpers and inline reference subprocesses. Core displacement,
subdivision, strand and volume comparisons explicitly depend on their Houdini
image producers: generating an independent `reference-*` EXR does not generate
the native EXR being compared. Motion, texture, transparency and courtyard
consumers retain their scene producers. Other inspected references are generated
inside the consuming case or are immutable `tests/fixtures` inputs; the optional
`mix-modern-before.hiplc` branch is historical evidence, not a required producer.
Freshness and explicit failed results remain enforced by the runner. Older
assert-script reports without a `result` field still require a successful exit
and fresh report, recorded as a legacy evidence contract.

Image comparisons must state their color space, channels and authored intensity
span. `support.images.ssim` uses Wang et al.'s local SSIM equation with normalized
11x11 Gaussian weights (sigma 1.5), population moments, complete spatial windows
and equal per-channel pooling. It never infers a range, changes color space,
clips samples, flattens spatial neighborhoods or silently includes alpha. The
Blender instance reference check retains foreground RMSE <0.018 and adds RGB
and alpha SSIM >=0.97 over the foreground bounding box. Unit-strength emission
in scene-linear Rec.709 and coverage alpha use span 1. The existing RMSE permits
sampling differences at silhouettes; SSIM independently rejects shifted texture
or geometry structure. Both gates must pass. `CYCLES_HOUDINI_TEST_OPTIX_DEVICE` pins the actual Houdini
GPU and the Blender reference GPU: backend ID prefixes may differ, so mapping
uses the verified device name and available PCI identity, rejecting ambiguous
or mismatched selections. Reports record both backend IDs; fixture filenames
continue to use CPU/OPTIX. Reference-equation, constant,
scale-equivalence, channel, shape and corrupted-image controls run in pure pytest.
Algorithm reference: https://ece.uwaterloo.ca/~z70wang/publications/ssim.pdf

`test-catalogue.json` accounts for every Python, PowerShell, C++ and CMake test
script. `pytest` entries are discovered, `scheduled` entries have a runner,
`support` entries are helpers, and `gui`, `benchmark` and `manual` entries have
explicit execution requirements for new entries. Existing entries listed under
`legacy_without_disposition` retain their earlier classification. Run `python scripts/catalogue-tests.py` after
adding or moving tests, then review changed dispositions. The pure gate rejects
a stale catalogue. Retirement requires a reason, replacement or retained evidence,
and removal of runner references. The eight previously retired probes retain
original Git hashes and replacement coverage in `tests/retired-probes.json`.

The Threads eligibility and nominal-name-index CPU smokes are registered in
test-dispositions.json and discovered by test_reviewed_native.py under --native.
wire_instances20_fixture.cpp is compiled support for wire_instances20_test.py.
That codec qualification requires a real configured CMake build (build.ninja,
matching libraries, generated package paths), Windows MSVC/dumpbin and matching
Houdini headers/DLLs with Image Iris enabled. Run ordinary Python with explicit
paths: `python tests/wire_instances20_test.py --build <configured-build>
--destination <new-output-directory>`. Replace the placeholders; a frozen
runtime-only context has no required library closure. This setup needs no GUI
or artist approval, and registration does not claim a passing execution.

`test-dispositions.json` records an individual purpose and disposition for all 172
previously unmapped entries. This review adds 15 SDK-free Python assert/unittest
contracts and four PowerShell context guards to the mandatory pure pytest set.
It adds 37 self-contained native recipes (66 explicit backend invocations) to
`test_reviewed_native.py`, requiring `--native`, the exact verified build and
separate worker processes/preferences. Their original assertions are preserved;
report JSON is never invented for a failed child. These are additional pytest
contracts; the original 518 nightly invocation identities remain unchanged.
Fifteen helpers are classified as support; the existing native review tier also
executes the frozen legacy status-parity compiler contract. The retained 44 GUI, 25 benchmark
and 31 qualification/manual entries have reasons and human requirements in the
review file. Classification does not claim these cases ran or passed.

Humans must select and inspect actual Houdini GUI/OS-input workflows, including
focus, mouse bursts, multi-screen DPI, fullscreen exits, paint evidence and
artist-scene behavior. Use an owned GUI host, isolated preferences and the
script's report/screenshots. Performance comparisons require exclusive hardware,
matching scenes, seeds, sample/time budgets and an explicitly selected baseline;
they are not portable correctness gates. Remaining qualification scripts need
special inputs: fresh Blender-produced bundles, plugin-absent save/recovery
phases, two release archives for activation, multiple GPUs/telemetry, selected
binary dependency closures, or standalone compiler/oracle contexts. Their source
is retained in place with the original invocation documentation. None is
silently archived, deleted, relabelled as a pass, or substituted for the portable
nightly suite. The review file identifies the concrete missing context per case.

Markers are `pure`, `cpu`, `optix`, `gui`, `slow`, `blender`, `release_gate` and
`hython`. Add new pytest files under `tests/pytest_cases`; legacy assert scripts
are incrementally adapted through isolated subprocesses, avoiding collection
side effects in the unreviewed corpus. Full nightly has explicit Image Iris
renders on both CPU and OptiX and rejects a feature-disabled build.

Tier and catalogue planning now lives in `scripts/native_test_plan.py`.
`Get-NativeTierCases` remains a PowerShell compatibility entry point; it delegates
planning to ordinary Python. Named-tier headless execution uses
`scripts/native_test_runner.py`; custom `-Tests` and GUI launchers retain their
existing interfaces and summaries. The frozen pre-migration plan fixture retains
the historical counts (fast 15, extended 26, nightly 518). The plan contract
names every later addition: one CPU no-Blender readiness case in each tier, the
colour-config check (extended, nightly) and, in nightly only, volume bounds and two
storage/alias contracts (CPU and OptiX each), the script-declared fatal GPU reply
check and the script-declared curve-mapping table check, and in every tier the community build checks
(`community_build_test.py`, git only). Current counts 17/29/529; every historical field and order is still compared.
The new case checks a native ROP first image, no Blender worker launch, and
actionable diagnostics only after a .blend asset is requested. Portable tests
also cover bridge selection independently of executable availability, installer
repair/registration refresh, rollback, immutable payloads, and original package
restoration. CLI arguments are checked
against each actual script's parser declarations without running render code.

The first eleven producer-dependent recipes have domain pytest cases in
`pytest_cases/test_dependent_recipes.py`: motion-panel coverage, batch
transparency, queued workers, light hierarchies, texture cache/normal/Auto,
and independent strand, volume, displacement and subdivision images. Their
numerical contracts live in `support/recipe_contracts.py`. Those fifteen backend
cases replace their generic catalogue items. `--catalogue` still discovers
exactly 520 unique backend identities; ordinary nightly audits the same domain
reports inside its 529 processes, including the separate no-Blender readiness
case. The 518 historical invocation identities remain unchanged; the 519th is the
fatal GPU reply check added with installer candidate 7429a7a0, the 520th the curve-mapping
table check (`houdini_curve_mapping_table_smoke.py`) added with the independent curve-mapping rewrite. Original native scripts retain
ownership/lifetime assertions and fresh host processes.

For incremental execution, use `--catalogue --native` with the usual verified
build options, optionally selecting a domain case with `-k`, for example:

```powershell
python -m pytest -c tests/pytest.ini tests/pytest_cases --catalogue --native -k 'test_houdini_texture_auto_batch_smoke and OPTIX' --build-directory D:/ci/build --artifacts D:/ci/evidence/recipes
```

Selection expands only the matching producer backend; internally selecting
consumers require all their declared producer backends. Producers run once per
queue. A failed producer blocks its consumers before process creation. Required
producer HIP/EXR/OCIO outputs have streamed SHA-256 receipts; changed or missing
outputs fail. Each child receives a queue-specific token, and its fresh identity
must match that token, test, Houdini version, manifest, release, loaded host paths
and selected backend. Old required reports/identity files are removed before
launch, including future-dated stale files. Archived reports and identities are
hashed and must remain within their own evidence directory.

A queue archives the selected execution scripts, support code, example generators
and catalogue separately from the selected build's historical integration source.
Run `python scripts/native_test_runner.py --audit --summary <run>/summary.json`
to verify those snapshots, child identities/reports, immutable host payload,
auxiliary binaries and kernels. `execution-audit.json` states this scope. It does
not claim an old staged build was compiled from a changed working tree. Release
qualification still requires the normal frozen-build source audit.
