# Native color configuration and retained renders

## Houdini configuration ownership — K22, November 10, 2026

The renderer worker loads the file returned by Houdini's resolved OCIO config
path, including Houdini's bundled default, with the captured OCIO context and
working directory. Relative LUTs and context-dependent search paths therefore
resolve from the same file as Houdini. Worker protocol 70 carries the filename;
the serialized-config fallback is reserved for standalone in-memory C++ fixtures.
Background hython jobs also retain the original config filename and context.

The engine uses its standard current-config path. Patches 0011 and 0012 and the
process-wide render cancellation gate have been removed. Observing a host config
change does not stop another worker. An owner adopts the new policy on its own
update or explicit color notification, joining only its renderer before changing
that worker's current config. Pause and cancellation survive the notification;
the source-tagged old frame remains available until an accepted update replaces
it. Rendering-policy epochs belong to an individual session.

Artist IPR and final rendering use isolated workers. Explicit `execution="sync"`
remains a sequential reference mode using Houdini's process and stock Cycles'
global OCIO caches. Two live sync sessions may share one policy. Attempting to
create or update a conflicting sync session reports a diagnostic directing the
caller to `execution="worker"`, or to close the other sync session first. It never
recolors a peer or requires restarting Houdini. The short admission mutex checks
policy identities only; it is never held across rendering or another session's
control lock.

Portable input definitions stay in glue solely to build CPU processors when a
source encoding is absent from the Houdini config. A glue image loader converts
those inputs to working-linear pixels before handing them to stock Cycles.
Named native inputs retain the normal engine texture loader. Portable derivatives
use a separate revision/processor/alpha-qualified TX identity, validate their
captured metadata on reuse, and publish through the existing atomic output helper.
The artist and engine configurations never receive the private input overlay.

The older sections below record the superseded shared-engine implementation and
its historical evidence. Current acceptance uses
`tests/houdini_color_config_smoke.py` for config/resource identity, working-linear
EXR, data planes, independent viewer transforms and two simultaneous workers with
different configs; `tests/houdini_color_epoch_smoke.py` checks retained worker
lifecycles, and `tests/houdini_color_interop_smoke.py` checks portable inputs/TX.

## Color and camera lifecycle after faster dispatch - October 18, 2026

Host `20261017T222554370` retains native color/world IPR lifecycle behavior after the camera
notification and bounded first-image polling changes. Camera-driven SOP/shader
dependencies and focus-pass capture guards pass on CPU and RTX 4090; adaptive and
denoised navigation and camera-lock/click-to-focus controls pass on RTX 4090.
The verification index (`artifacts/verification/camera-delivery-20261017/index.json`)
records source-specific scopes and asynchronous resource-retirement checks.


## Native color lifecycle recheck after light-tree optimization - October 17, 2026

Host `20261017T213911615` passes all eight native IPR/color lifecycle stages on CPU and RTX
4090, including pause/resume, stop/restart and zero tracked resources after
asynchronous worker retirement. World and volume IPR also pass on RTX 4090.
These exercise native Houdini interfaces and shared-view ownership; the separate
heavy timing fixture uses one render viewer. Source archives and reports are in
the verification index (`artifacts/verification/flat-light-tree-20261017/index.json`).


## Reported native color IPR timeout - October 17, 2026

The old saved color fixture predated required-coordinate recovery and had no UVs.
The renderer correctly substituted diagnostic shading while the test waited for
its textured reference color. The test now authors explicit native UVs, selects
the intended SOP source, uses explicit interactive samples/adaptive/denoise settings,
and reports the stalled stage. Cleanup removes registered callbacks safely and
waits for asynchronous worker closure before checking zero owned/device bytes.
It preserves the original error instead of masking it with callback-removal errors.

On host `20261017T201600302`, actual native IPR/shared-panel checks complete all eight stages
on CPU and RTX 4090: initial input conversion, view-only reload, manual old-frame
retention, automatic working-space update, pause/resume and stop/restart. No geometry
recook is required for those color changes. Both process exits and shutdown counters
pass. The final panel image was visually inspected. See the
current verification index (`artifacts/verification/local-light-tree-cache-20261017/index.json`).

## Fractional alpha and direct data display - October 17, 2026

Host `20261017T194800117` separates source coverage from the decision to apply color
processing. Alpha-only preview now displays the actual grayscale coverage,
including partial and zero coverage; it previously appeared white. Its display
remains opaque and ignores exposure, gamma and OCIO transforms.

Raw/data/alpha CPU display packs directly from immutable source storage without
an intermediate full float RGBA image. Scalar/alpha quantization is shared across
RGB; vector data remains opaque. Raw color preserves unpremultiplication and
additive RGB at zero coverage. Cryptomatte bit patterns remain false-color data.
Independent compact references verify crop/origin alignment, coverage and these
semantics. Renderer pixels and output EXR color handling are unchanged. See
[measurements and limits](../performance.md#owned-display-copies-and-data-packing---october-17-2026).

The native renderer now freezes its rendering configuration independently of
Houdini's current display configuration. Rec.709 and ACEScg configuration
changes, retained images, input textures and shader luminance have independent
CPU and RTX 4090 OptiX checks. This is a development color milestone; the full
production color and output gates remain open.

## Artist behavior

Use Houdini's active OCIO configuration and its `scene_linear` role. The
dedicated panel lists that configuration's displays and views and preserves a
valid selection after a reload. Exposure and display/view selection affect
the displayed image without restarting path tracing.

The dedicated panel accelerates supported color views with generated OCIO
shaders on a background graphics context. Unsupported GPU transforms, data/raw/
alpha views and retained frames with different working policies use CPU
conversion. Coverage remains independent of the view transform. This changes
display processing, not linear output. See [GPU display scope and verification](native-gpu-display.md).

A rendering-policy change joins only the affected isolated worker before replacing
that worker's current config. Automatic updates then refresh its owning
scene. Manual-update mode preserves the image and waits for an explicit
update. Native IPR pause also defers its host scene update until resume;
direct sessions can accept a transaction while remaining paused. Canceling a
render prevents a configuration reload from starting it again. An explicit
render/update can restart the canceled session.

Canceling or joining a generation rejects later engine buffer callbacks from
that interrupted render. The last usable preview remains a preview; it does
not become a completed frame eligible for EXR output. Closing native Render
View leaves rendering available to an attached dedicated panel. Native Stop
continues to cancel the shared session while its native pane remains open.

The native viewer and dedicated panel retain one engine session. An old
image remains associated with its source working space and configuration.
The panel applies the current view to that source meaning; native IPR converts
transmitted color pixels into Houdini's current working space. Render buffers
remain unchanged. EXR output records the image's original working space,
configuration identity and rendering-policy identity, even after a later
configuration change. Data passes bypass color processing and exposure.

Display conversion exposes only RGB to OCIO and preserves the renderer's
coverage alpha. This applies both to current frames and retained frames that
require conversion through the interchange space. An OCIO matrix that changes
its alpha channel therefore cannot change the displayed image's coverage.

Cross-configuration display requires `aces_interchange` in both configurations.
A missing role produces a diagnostic. It does not silently reinterpret the
old RGB values. Source-tagged EXR and raw data passes remain available when
their normal generation/state requirements allow output.

## Ownership and synchronization

`ColorConfiguration` is an opaque immutable integration resource. Frames hold
a shared reference to their source snapshot and an epoch number. The snapshot
retains compiled source-to-interchange and interchange-to-working processors,
so replacing a referenced LUT cannot change an older frame's color meaning.

There are two identities. The full configuration identity drives viewer-menu
refresh and includes recorded input-LUT revisions. The rendering identity
covers all active and inactive colorspace definitions, roles, file rules and
actual input processors. Inactive spaces remain usable by saved materials.
Colorspace visibility, active views and display selection do not invalidate
rendering. Rebuilding a
display-only snapshot therefore does not change the render generation,
geometry, shaders or texture resources.

Each renderer owns its configuration transition under its session control lock.
A transition joins that renderer, preserves pause/cancel state, clears its OCIO
file-content cache, captures fresh processors and marks the owner pending.
The Houdini process only observes its authoring config; it never changes the
artist's global current-config pointer. Owners refresh shader matrices, image-resource
keys and affected displacement before starting their next generation.

`color_epoch` identifies the session/frame source. `color_target_epoch`
identifies its requested rendering epoch. The host notification ledger tracks
the target number, so a second reload cannot disappear between polling the
first update's start and completion. Camera/transform edits retain the normal
geometry cache. A color change rebuilds displaced geometry from the retained
original mesh when shader luminance or texture conversion affects displacement.

Input-processor metadata supplies the watched LUT paths. Size/mtime changes
are checked when observing the authoring config; the Houdini event loop polls roughly
once per second. This also detects disk-only edits to known working/input
LUTs. A failed reload keeps the source frame and repair-detection ledger. The
host reports the error, and a repaired tracked file permits retry. Native
image transmission keeps its existing bounded replaceable-frame queue and
performs color conversion on the display I/O thread.

The retired engine hooks are recorded in the K22 experiment recovery metadata.
Current engine consumers use OCIO's current configuration. Solaris color behavior
and coexistence with an independently linked Hydra engine remain separate gates.

## Verification

`tests/houdini_color_epoch_smoke.py` uses independent OCIO processors and
specified texels. Its fifteen groups run on CPU and RTX 4090 OptiX:

- Display-only reloads preserve generation, material builds and image identity.
- Interrupted previews retain their source pixels and remain incomplete;
  completion waits and full-quality output report the pending update.
- One barrier joins active, completed, paused and canceled sessions.
- Old EXR tags and display pixels retain their source meaning.
- Each owner refreshes retained resources before restart, preserving geometry.
- Same-named input transforms update uncached and generated-texture-cache paths.
- Inactive named inputs also update in both cache modes, while visibility-only
  changes preserve the generation, geometry and texture resources.
- True displacement follows independently calculated luminance through
  ACEScg/Rec.709/ACEScg transitions.
- Same-named external LUT changes preserve old compiled processors and update
  subsequent rendering, including a configuration reused after another policy.
- Disk-only LUT edits, missing-file repair and missing-interchange diagnostics
  preserve source output and data-pass behavior.

`tests/houdini_color_ipr_smoke.py` exercises eight events in actual native IPR
and a shared panel: source rendering, menu refresh without rerendering, manual
updates, automatic working-space updates, deferred pause, resume, canceled
reloads and explicit restart. It records generations, resource counts,
extractions, native pixels, source identities and zero tracked allocations on
close. Both tests are in the aggregate runner.

The complete run `artifacts/verification/20261011T010612853/summary.json`
passes all 82 aggregate cases, including the fifteen-group color suite,
native Auto input and batch tests, and both real native IPR/shared-panel color
lifecycles on CPU and RTX 4090. Its source audit matches the frozen manifest,
executed tests, 201 integration sources, four compiled renderer artifacts,
eleven patches and six kernels before subsequent documentation annotations.
The earlier 78-case color baseline, first Auto run's pyro float32 comparator
failure and corrected verification remain separately recorded. This evidence
does not replace the remaining production gates below.

## Remaining production gates

`tests/houdini_display_alpha_smoke.py` checks every pixel against independent
OCIO RGB processing and unchanged source alpha on CPU and RTX 4090. A deliberately
alpha-changing display transform reproduces the previous defect. Exposure and
retained frames across a working-space change now pass, alongside the existing
display and color-epoch tests. The logs and before/after manifests are in
`artifacts/stage3/color/display-alpha/`; its preceding failure log is
`artifacts/stage3/color/display-alpha-before-fix.log`. This targeted evidence
postdates the 82-case aggregate baseline and does not certify fractional or
filtered transparency.

Native Auto now resolves filename modes, file rules, header metadata and
format fallback against the frozen rendering configuration. It retains Auto
in saved graphs, supports live input-space menus and exposes committed input
diagnostics. Six fresh-process modes, CPU/RTX 4090 cache routes, UDIMs,
configuration reloads, native IPR and batch have separate regressions. See
[native Auto policy](native-texture-auto.md) for the exact implemented semantics
and intentional differences from the installed PXL evaluator.

Portable native inputs and source-tagged output identities now have a separate
[implementation and regression suite](native-color-interop.md). The installed
default configuration leaves `getInteropID` empty for tested standard spaces;
compiled analytic equivalence, authored IDs or unambiguous local IDs establish
output identity. Missing standard inputs use a private immutable overlay.
Unsupported declarations and disabled host OCIO produce visible diagnostics.
This additional work postdates the 82-case aggregate baseline above.

Native presentation, CPU/GPU viewer conversion and navigation previews consume
the last accepted color snapshot for an unchanged host config, without checking
LUT files per call. The IPR registry polls disk dependencies once per second;
explicit captures, render starts/updates and manual color queries still check
immediately. A successful explicit refresh is visible to the next presentation.
Reload and host-wide OCIO cache clearing remain synchronous at that poll or
explicit check, so the reload stall is moved away from ordinary presentation,
not eliminated. `tests/houdini_color_latency_benchmark.py` measures both costs
with 512 distinct real file LUTs, and checks edits to duplicate-content LUTs.
Processor caching is disabled only during unpublished dependency enumeration
so OCIO's content deduplication cannot hide a second physical LUT dependency.

On Houdini 22.0.466, 100 presentations of a retained CPU-worker frame to
`null:` at 25 Hz with 512 distinct local LUTs took a median 2.938 ms before
the change and 0.145 ms after it. The first presentation after editing one LUT
fell from 142.469 ms to 0.246 ms. The explicit refresh that observed the edit
took 160.476 ms on the revised build. Network filesystem latency and GUI paint
are outside this measurement. Baseline samples (`artifacts/review-648f241-color-latency-before/color-latency/report.json`)
and revised samples (`artifacts/review-648f241-color-latency-after/color-latency/report.json`).

Additional gates include initially missing/newly reachable LUT dependency
discovery, external display-only LUT edits, broader configuration/context
coverage, standardized output chromaticities, transparent/filtered alpha,
broader UDIM/cache workloads across configuration changes, and measured large-scene
reload latency and memory. Metadata watching cannot detect an edit that
preserves both size and mtime. Complete Solaris color behavior, standalone
MPlay configuration reloads and coexistence with other active renderers need
separate certification. No full color-production or Redshift-parity claim is
made by these tests.
