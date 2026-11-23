# Capture cancellation and closing

Queued IPR now releases canceled capture payloads and capture-owned resources
on its existing conversion executor. The host completes the display/handle
release afterward. This extends
[successful scene publication](native-scene-publication.md); it adds no artist
execution mode or previous-version compatibility API.

## Ownership and ordering

| Operation | Owner |
| --- | --- |
| Cook, acquire host parameters, freeze source inputs | Houdini host thread |
| Classify and convert owned inputs | Existing conversion executor |
| Publish a prepared snapshot | Houdini host thread |
| Discard a canceled/failed capture and its owned payload | Conversion executor |
| Retire capture tables, snapshots, COP rasters and texture metadata | Conversion executor |
| Final native display/handle release | Houdini host thread |

Cancellation adds a `discard` capture stage. A pending request waits for that
stage's Future instead of evaluating the same adapter while cleanup mutates it.
The UI does not join the Future. Pending geometry changes still survive merged
camera edits, and independent previews can use the accepted scene.

`discard_apply()` invalidates unaccepted adapter work, then explicitly releases
the job's geometry, roots, graphs, DTOs, snapshots and owned images before its
capsule is dropped. Later capsule destruction is small regardless of where
Python releases the last reference. The handle stays in its transaction until
owned cleanup finishes. Published resources are preserved.

`retire_capture()` accepts only an idle capture-only handle. It releases its
frozen resources, marks capture terminal and leaves host-dependent display
finalization to `close()`. Reentry, retirement during a live transaction and
reuse after retirement remain errors. The operation reports its duration,
thread role and snapshot object count for verification.

The viewer reports `closing` until both retirement and host finalization finish.
Ordered/headless close drains the same converter. Interpreter exit has an
explicit shutdown path: it first joins the executor, then releases remaining
resources without submitting new work or registering HOM UI callbacks. There
is no active UI responsiveness promise during interpreter termination.

## Failed capture versus rejected edit

A cleanup exception is an internal session failure. It stops new acquisition,
reports `failed` with `update_rejected=False`, and sets
`capture_restart_required`. Render then replaces that capture session and its
worker. The previous capture retires asynchronously; the new viewer image must
come from the fresh session.

A safely rejected edit keeps its accepted scene and worker. Renderer-only
failure can also reuse valid captured resources while replaying them into a new
backend. The capture-specific restart flag avoids unnecessarily discarding that
valid source state. Existing local recovery for unfamiliar materials, missing
textures/attributes and geometry components is unchanged.

## Scoped measurements and checks

The phase probe uses one shared 1,036,800-triangle prototype at 1,000, 10,000 and
100,000 instances. It cancels after scan, preparation and conversion, verifies
the accepted inventory/deltas after each cancellation, and closes each capture.
It also checks terminal/reentry guards and zero retained geometry leases.
The probe excludes rendering, transport and Qt paint.

At 100,000 instances the earlier direct host cleanup measured about 14.8 ms for
releasing the converted job and 68.5 ms for capture close. The final probe moves
that work to the converter: host submission/capsule release is about 0.01 ms and
host finalization about 0.01 ms. Capture retirement itself still takes about
43 ms in the background. These are separate scoped observations, not a total
speedup claim or a production timing certificate.

The new viewer workflow runs on both the 4090 and 3080 Ti with the full shared
prototype at 100,000 instances. Deterministic barriers hold converted payloads,
cancellation cleanup and capture retirement while the host processes edits and
timers. It verifies coalesced restart, an injected internal cleanup failure,
Render-button recreation, changed rendered and Qt-displayed shading, and zero
tracked allocations/retired owners on close. A rejected light-path limit repairs
on the same worker; failed capture cleanup creates a fresh one.

In these runs Stop submits in about 0.15 ms, restart in 0.3 ms and close in less
than 1 ms. The old capture's owned retirement takes about 97-108 ms on the
converter; native host finalization takes about 0.02 ms. Barrier timings prove
scheduling and ownership, not physical-input/compositor latency or general
percentile budgets.

The evidence record (`artifacts/capture-retirement-evidence-20261020.json`)
selects 23 passing source-audited runs on one compiled runtime. These include
CPU/both individual GPUs, mixed-device motion, packed/volume repair, independent
binding/displacement motion, COP pixels, surface/world/volume previews, final
sequences and both viewers. Shutdown is checked both before worker startup and
after a rendered calibration, with the executor already shut down. A separate
test terminates an actively tracing owned worker, recovers both viewers through
backend replay, then stops rendering. Its fixed nonadaptive sample budget keeps
the worker tracing until the deliberate termination.

All twenty dense-preview edits still pass, including ten navigation previews
painted while geometry is pending. That stress case still has substantial
all-instance transform and emitter costs. No new universal latency, extended
physical-memory or release certification is claimed.

## Remaining work

Three obsolete rejection tests have been replaced by current transaction and
single-viewer recovery fixtures. Scene replacement also now detaches observers
before deleted ROP handles can produce repeated callback dialogs; see
[scene callback ownership](native-scene-lifecycle.md). The identified Sky Texture
numeric/direction gap now has local recovery and registered headless/viewer
acceptance tests; see [Sky source recovery](native-sky-source-recovery.md).
Continue distinct/deep packing, large graph acquisition, transform
serialization/core reconciliation and dense-emitter optimization. Physical
surface-only mesh/BVH sharing, remaining feature contexts, prolonged memory and
rapid session replacement, broader device/input/monitor checks and reproducible
release certification remain open. Raw abandoned capsules outside the queued
protocol and individual host-only operations do not receive a general timing
guarantee. No Loop subdivision work is included.
