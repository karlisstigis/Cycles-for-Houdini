# Native rendering execution policy

## Preparing fresher poses while waiting for paint - October 17, 2026

The persistent IPR controller may refresh a completed, successful object-pose job
only while it is waiting for the accepted render's presentation. A newer root
revision must exist and no complete source snapshot may be pending. Running jobs,
failed/cancelled futures and complete source jobs retain their existing commit or
error handling. Once presentation permits the next commit, prepared work proceeds
even under continuing input. This preserves camera opportunities and prevents
object/geometry starvation.

There remains one preparation executor/job and one pending desired state. At most
one retired completed job is briefly retained while its replacement starts.
Retirement runs on the controller thread outside its condition lock, so GUI status
reads do not wait for destruction of a large object map. Stop, scene epochs,
source debt, diagnostics and final/Persistent Data ownership are unchanged.
`pose_preparation_refreshes` records replacements for development diagnostics.

Deterministic tests cover fresher-pose selection, retirement thread/lock ownership,
no restart of running work, fairness after feedback, geometry preservation and
internal preparation errors. Native mouse gestures, continuous changing geometry
and CPU/OptiX artist controls are recorded in
the native drag index (`artifacts/verification/native-drag-poses-20261017/index.json`).
See [performance evidence](../performance.md) for measured improvements and limits.

## Early object-pose preview contract - October 17, 2026

Host `20261017T070257430` retains one persistent native IPR worker, protocol 33, the same
32 engine patches and six unchanged GPU kernels. Restart Houdini for this runtime.

The accepted native ledger supplies root paths, immutable local origins and each
root's shutter window. A host callback freezes current nominal/centre/shutter
transforms and camera data without starting the source adapter. Matching time,
motion and working-color contexts are mandatory. Missing or invalid new poses do
not poison the accepted overlay; the authoritative capture handles source repair
and diagnostics. Repeated unchanged poses, including parented cameras, are no-ops.

Source dirtiness and pose-preview acknowledgement are separate. Every submitted
pose preview leaves a complete-source requirement outstanding. It does not update
the entry's applied source serial, bindings, resource membership or warning ledger.
The existing background pipeline rebases completed geometry on the newest poses,
then releases the source requirement when an actual source commit is accepted.
Camera-first opportunities remain available while owned object work prepares.

Stop/session epochs are checked around acquisition. Auto, Pause and render-mode
controls gate scheduling; closing the panel removes the posted callback. Nested
host callbacks cannot advance source loading or reenter acquisition. The guard
is restored after evaluation errors, and queued Stop remains authoritative before
the backend acknowledgement arrives. The
normal source debounce and host-safe cooking path remain in place. Adaptive camera
dicing still defers through dragging and refines after settling. Final/batch frames
retain ordered ownership and Persistent Data; no extra artist execution mode or
scene protocol was introduced.

## Borrowed immutable update data and ordered validation - October 17, 2026

Host `20261017T054241662` retains one persistent native IPR worker, protocol 33, the same
32 engine patches and six unchanged GPU kernels. Restart Houdini for this runtime.

An apply call and its persistent boundary callback complete before returning.
Ordinary immutable command data is borrowed only within that call; no renderer
resource stores a reference to the command. Engine records copy scalar/string/
motion data or retain owned immutable geometry. Material-bearing commands keep a
private mutable candidate for displacement shutter-graph expansion and retirement.
Caller-owned graphs and pose vectors remain unchanged.

Sorted pose keys are checked exactly through adjacent comparisons and resolved
against the ordered accepted table. Unsorted callers retain complete duplicate
validation. Upsert/deletion conflicts, missing targets, non-finite/singular
matrices and malformed shutter samples reject before mutation. This introduces
no artist identity requirement. Pose-only updates with blur disabled cannot
change displacement-motion recovery; blur/vector transitions, active blur,
resource/variant changes and retirement continue to recompute it.

The opt-in `CYCLES_HOUDINI_PROFILE_UPDATES=1` trace records core stage wall times,
counts and accepted generation on stderr. It carries no source paths, preserves
the binary protocol and is disabled by default. Diagnostic formatting failure
does not affect source recovery. CPU contracts verify enabled/disabled logging,
strict rejections, caller immutability and fresh renders. Final/Persistent Data behavior
and the single artist IPR execution path are retained.

## Pose command ownership - October 17, 2026

Host `20261017T050102773` uses one persistent native IPR worker, protocol 33 and the same
32 engine patches and six GPU kernels. Restart Houdini for this runtime.

Protocol 33 introduces an explicit transform-only object pose command. It can
target only an already owned object and cannot rebind geometry or modify object
properties. Full object commands remain authoritative for creation, bindings,
visibility, random IDs and settings. A transaction cannot pose and replace/delete
the same object. Missing targets, invalid matrices and malformed shutter data
reject before mutation; source recovery remains the native adapter's policy.

Validation borrows stable engine records only while the transaction control lock
is held. Map insertion preserves their addresses; conflicting retirement has
already rejected. No borrow crosses a transaction. Each pose applies its transform,
motion and nominal transform, then tags the upstream object normally. Geometry
re-upload is not required by ordinary camera/object moves.

Ordered immutable host tables are merged and compared in linear walks. Old
snapshots remain owned and unchanged, while camera/material-only states retain
their existing object table. Equal nominal transforms use a bit-preserving tag;
other nominal matrices remain explicit. Protocol versions must match.

Mesh variant grouping is dependency driven for poses. Object-space displacement
retains shared variants through ordinary motion; reversed orientation, world
dependencies, pixel-adaptive dicing and sampled displacement still regroup.
Material, geometry, retirement and unfinished-dicing changes retain their normal
paths. CPU/OptiX literal-position and fresh-reference tests protect these rules.

This optimization retains one
artist IPR worker and ordered final/Persistent Data behavior. Whole-group pose
tables and engine object/BVH synchronization remain O(N), with production response
and native viewport contention certification still pending.

## Compact image and pose ownership - October 17, 2026

Host `20261017T040304102` uses one persistent native IPR worker, protocol 32 and 32
engine patches. The preceding 31 patches and all six GPU kernels are unchanged.
Earlier sections record their dated builds. Restart Houdini for this runtime.

Each immutable image plane carries stored dimensions separate from its logical
coverage. Progressive buffers remain compact through the worker protocol and
background display conversion. Exact RGBA8 expansion precedes GUI assembly;
native display expands rows on its HDK thread. Completed images require full
storage. Unsupported wire versions and malformed storage reject explicitly.

A full pose capture may target a different time/shutter context from the accepted
scene. Freeze its current camera/root poses for that future context, but apply
them only to a compatible owned scene. An older overlay must not replace those
new poses after frame revisits. Partial edits retain navigation for fields they
do not author. Analytic motion vectors, fresh-capture images and fixed screen
bounds cover time revisits and camera/background/geometry deltas.

Viewer attachment epochs additionally guard paint acknowledgements and focus
picks across session recreation. Retained old pixels remain visible until the
new conversion arrives, without becoming input for the recreated session.
The actual viewer test disables polling and forces reused generation numbers.

See [measurements and certification limits](../performance.md). Ordered final render
and Persistent Data policy remains in effect.

## Background image ownership checkpoint - October 17, 2026

Host `20261017T025024087` changes viewer delivery while retaining the native persistent-worker
IPR policy, protocol 31 and ordered final/Persistent Data processing.

The GUI caches display choices; the controller's accepted-frame callback reads
only immutable pixels/metadata and submits conversion outside its controller
lock. The bounded display service owns active/pending/completed work and separate
source/display revisions. GUI reconfiguration uses a nonblocking source read;
close retires configuration and callbacks without waiting for conversion. Engine
and transport failures remain strict. Qt widgets and image assembly stay on the
GUI thread; no source callback enters HOM, evaluates parameters or cooks geometry.

One application-owned relay posts coalesced notifications to a decorated queued
Qt slot. Receivers are weakly held, disconnect/close makes queued pulses inert,
and disposal unregisters producers before renderer retirement. See the pinned
[Qt receiver-thread rules](https://doc.qt.io/qtforpython-6.8/PySide6/QtCore/QObject.html).
Atomic logical/physical frame identity, current display settings and attachment
identity reject stale conversion results. The last displayed image remains while
new work is pending. Cached Live images retain their own metadata while snapshots
are selected; returning to Live does not require rendering or converting again.

Forty-six targeted regressions plus three matched heavy delivery observations
pass. Earlier conversion/wakeup controls and exact source archives are disclosed
in [performance evidence](../performance.md). No new artist execution mode is added,
and no end-to-end or production certification is inferred from one faster stage.

## Incremental validation checkpoint - October 17, 2026

Host `20261017T014902203` retains one persistent worker for IPR and ordered final/Persistent
Data behavior. Protocol 31, engine patches and kernels are unchanged.

Each candidate validates incoming object state and geometry references. Accepted
retained bindings need a new scan when geometry is removed; same-transaction
object replacements and deletions supersede their old references. Local hashed
string views borrow only from the immutable candidate during validation. Malformed
internal keys and dangling references still reject before mutation. Artist id/name
attributes follow the existing native recovery rules.

CPU/OptiX checks exercise both ordered/persistent mutation, preserved images after
rejection, rebind/delete/recreation and fresh depth references. Thirty-eight
targeted normal-mode wrappers pass, including live geometry/volume repairs and
actual viewer interaction. The profiled camera update-call median drops from
81.0 to 0.4 ms at 100k instances;
visible latency and remaining costs are documented in [measurements](../performance.md).

Optional request profiling uses stderr records and keeps the binary stdout
protocol intact. Per-request controller timing is bounded. A lazy worker reports
inactive diagnostic collection explicitly before initialization. Earlier sections
describe their dated checkpoints; production certification remains open.

## Continuous-update scheduling checkpoint - October 17, 2026

Host `20261017T012041968` retains one persistent native IPR worker, protocol 31 and the
same engine patches/kernels. Ordered final/Persistent Data behavior is unchanged.
One active owned capture/preparation finishes even if newer input is pending;
pending input coalesces to its latest state. Ready object/scene work follows one
camera opportunity and supplies an intermediate preview. One extra catch-up is
allowed only while that prepared state remains current; newer object/geometry
input removes the extra slot. Latest
geometry remains pending until its actual source commits. This prevents continuous
camera/transform edits from indefinitely withholding changed geometry.

Latest camera/crop overlays require matching captured time and shutter context;
object provenance/basis and explicit Stop/retirement guards still apply. Source
completion and preview generations remain separate. An obsolete safe rejection
does not suppress a pending repair. Invalid current scenes remain reported, and
unsafe state, transport and device errors retain their strict failure behavior.

Unavailable display preferences receive a typed pre-mutation rejection, retain
the accepted pass/render and enter bounded control diagnostics. Future-pass choices
still wait for the actual scene. Pause/Stop bypass those dependencies.

Single-viewer continuous tests at 1k and 100k verify multiple changed-object paints
during five-second drags and multiple changed-geometry paints during eight seconds
of combined deformation/navigation. Held scan/conversion/preparation, Stop/restart,
close, native IPR, adaptive refinement and five-frame tests pass. Large in-flight
packets and engine sync remain serialized response costs. This verifies progress,
not instant interaction or production latency/memory. See [measurements](../performance.md); earlier sections are historical.

## Camera-priority preparation checkpoint - October 17, 2026

The current checkpoint is host `20261017T001630159`, protocol 31, with the same
31 engine patches and six kernels. Native IPR retains one persistent worker;
ordered final frames retain their in-process/Persistent Data policy.

| Work | Ownership |
| --- | --- |
| Supported Houdini evaluation and pose acquisition | Host thread |
| Retained-detail scans and geometry conversion | Existing bounded converter |
| Large owned root/scene rebasing | One separate preparation thread |
| Camera priority, polling, controls and transport | Controller thread |
| Cycles scene, synchronization and GPU work | One persistent renderer process |

Camera-only preparation shares the object table and runs ahead of ready heavy
commits. Pure owned jobs never access Houdini/Qt. Completed jobs reapply the latest
camera, validate roots/basis and retire superseded/cancelled work. Physical-revision
presentation feedback prevents stale queued display work and distinguishes early
images from completed geometry. Equivalent completed captures reuse their accepted
preview accumulation; actual changes and explicit refresh intent remain supported.

Future-pass preferences wait for complete source commits; camera, Pause and Stop
bypass them. The real viewer tests at 1k/100k hold every preparation phase, change
roots during the hold, and exercise Stop/restart/close. Native IPR/pipe, adaptive
camera and five-frame checks pass. Large serialized packets and an engine update
already in progress can still delay camera feedback; group-transform speed and
continuous-drag liveness remain next work. See [measurements](../performance.md). Earlier checkpoint limits below are
historical; this does not certify production latency or memory.

## Navigation ownership checkpoint - October 17, 2026

Host `20261016T235903767` freezes camera/outer OBJ poses on the host independently
of active geometry conversion. Owned local/packed shutter poses let the controller
rebase acknowledged geometry without accessing the exclusively owned adapter or
inverting object matrices. The same persistent worker then accepts the completed
capture with the latest navigation. Time/motion/color/source guards preserve the
conservative path for incompatible changes. No second GPU scene is created.

Physical revisions distinguish early previews from completed geometry with the
same user generation. Background display work discards superseded revisions;
completion waits require current geometry. No-op requests retain their exact
completed image, pause intent survives pending capture, and strict failures retain
useful diagnostics/recover correctly. Root overrides retire with source replacement.

Actual single-viewer held-capture, native IPR, adaptive-camera and five-frame tests
pass. Large 100k root-transform work can still delay a subsequent camera preview;
camera priority ahead of that work and redundant post-preview accumulation reset
remain next tasks. See [performance measurements](../performance.md). Earlier dated limitations below describe
their corresponding checkpoints, not the entire current implementation.

## Capture and snapshot update - October 17, 2026

Native IPR still has one persistent worker; ordered final rendering retains
Persistent Data. Host build `20261016T231151646` batches compatible packed-leaf
instance expansion in its existing background capture converter when shutter
sampling is disabled. Host detail acquisition and nested packed resolution keep
their supported Houdini execution context; sampled packed motion retains the
previous path. Engine/GPU buffers do not cross the host snapshot boundary.

Accepted camera/material-only snapshots share immutable object tables. Mutation
copies a private table, and table-identity fast paths never suppress a genuinely
different table's values or deletions. Camera-only commits avoid alias traversal.
Serial zero is a valid initial capture: the first camera edit is eligible after
motion/time ownership has been established, while frame/motion changes and any
pending general edit still force the conservative route.

The current queue does not yet display independent camera/object navigation
against retained geometry during a heavy capture. It accepts/coalesces those
edits and eventually commits their combined scene. Prioritizing navigation safely
is the next performance task. Actual single-viewer paint timings and remaining
host delivery gaps are documented in [performance evidence](../performance.md);
the selection remains a development policy, not universal speed certification.

Native IPR uses one path: a persistent external renderer. Both the Cycles panel
and Houdini IPR connect to the same host registry entry for a ROP, and that entry
owns one worker. There is no execution-mode menu on the ROP and no environment
variable is required to use it.

The worker owns the Cycles rendering session, GPU resources and expensive engine
synchronization. Houdini owns supported scene evaluation and capture, the viewers
and the SOHO control pipe. Ordinary updates retain compatible resources and
replace obsolete pending edits at a safe engine boundary. Rejected edits keep
the last accepted scene. A renderer-process failure can be recovered independently
of Houdini; host extraction and interface faults still require their own fixes.

The current selection uses actual edit-to-Qt-paint measurements as well as
renderer isolation. Two matched runs per boundary on RTX 4090 OptiX use the
1,041,314-triangle SOP fixture, an independent adaptive turbine, animated true
ground displacement, 354 objects, curves, points and three volume fields.
In those earlier runs the worker's camera p95 is 206–237 ms; the queued
in-process reference is 457–481 ms. In-process medians are a little faster for camera, transforms and
surface color, and substantially faster for topology changes. Burst event-loop
p95 gaps are 44 ms for the worker and 39–42 ms in process. These are a small,
defined experiment; they do not establish universal worker superiority or
Blender parity. A final pair after the color/motion guards measures camera
median/p95 at 189/220 ms with the worker and 165/205 ms in process; burst timer
gap p95 is 49/44 ms. It does not reproduce the earlier camera-tail advantage.
The worker remains the sole validated artist path for its native-interface
coverage and independent renderer recovery, with a measurable transport cost.
These measurements do not prove that this is the fastest possible boundary.
After the sampling-interruption patch, one matched pair measures camera
median/p95 at 159/175 ms with the worker and 127/159 ms in process. Both improve
relative to the preceding pair, but the faster reference still needs the full
native lifecycle/recovery coverage before replacing the artist path.

The earlier heavy five-frame experiment completed in 13.346 s with a worker,
9.413 s queued in process and 9.118 s synchronously. Those are historical complete
sequences, not the new edit-to-paint measurements. Final rendering therefore
retains ordered in-process ownership. See [timing scopes](../performance.md).

The extra in-process queued backend has been removed. Synchronous engine access
remains necessary for the headless reference harness and ordered final ROP
rendering, using the same Cycles core and scene semantics. It is not an alternative
artist IPR mode. Direct development Session calls can still select `sync` or
`worker` for reference/transport checks; that selector does not change native ROP
IPR or final-render policy. Historical three-mode reports remain preserved.

Final ROP frames use ordered in-process ownership: each frame waits for rendering
and successful file output before the next frame. **Persistent Data**, under
Performance / Final Render Memory, retains compatible scene resources across
those frames. Completion, failure or cancellation releases the sequence. Final
ownership is separate from IPR. **Render to Disk in Background** snapshots the
current scene and launches a matching Houdini batch process for ordered final
frames, with progress reports, stop and interrupted-process recovery. See the
[background final guide](../../user/native-background-final.md). Snapshotting still runs
on Houdini's host thread; Render to Disk in the foreground continues to wait.

Device, resolution, cache-policy and thread-count changes recreate the worker.
The host SOHO control pipe stays open through that replacement. Ordinary camera
edits do not reconvert mesh topology. The native SOHO adapter consumes its dirty
and deleted-object iterators before deciding that only a camera changed. A
camera-only capture also requires unchanged motion timing and frame time:
shutter, FPS, offset or sample-count changes must refresh geometry motion samples.
Camera transforms and projection changes can retain those samples. A
no-op update also requires unchanged host notifications, time, configuration and
SOHO camera/crop state, plus a healthy active session. Final Persistent Data edits do not trigger
an interactive scene capture. HIP replacement and shutdown release owned workers
in both GUI and headless registry contexts.

Working color-policy changes use an independent, versioned worker control
message, even when Auto is off. The worker stops sampling the incompatible
scene at the existing safe color-configuration boundary and retains its old
image and resources until a scene is recaptured. A snapshot queued under the
superseded policy is rejected before entering a scene transaction. Display-only
view changes keep tracing and update the background display conversion.

This is the selected native development direction, not production certification.
The release gates still require bounded memory, recovery/stress coverage,
edit-to-first-current-image latency, UI stall attribution and the matched renderer
comparison. The worker must earn those gates; a process boundary alone does not
solve scene capture or progressive display costs.

Focused ownership and settings regressions use `houdini_render_ownership_smoke.py`,
`houdini_worker_ownership_ipr_smoke.py` and
`houdini_worker_ownership_native_ipr_smoke.py`. Their frozen run summary is recorded
at `artifacts/stage1/worker-ownership/verification-summary.json`. Rejection/recovery
and ordered final-sequence tests cover the affected shared paths separately.
# IPR scheduling and remaining work

Patch 0021 adds a latched sampling interrupt to the existing scene transaction.
The controller publishes it under the transaction mutex; CPU/GPU work and the
remaining render-pipeline stages observe it. The render thread clears it only
when consuming the transaction at its safe boundary. It neither cancels the
session loop nor waits on Houdini's UI thread. Invalid edits rejected before a
transaction do not interrupt the accepted render. This does not interrupt an
already running shader compilation, BVH build or subdivision operation.
Every accepted transaction resets accumulation, because the obsolete batch can
be partially sampled. Identical updates return before requesting the boundary;
they retain samples. The interruption cannot be used as a pause operation.
The focused heavy-render regression checks active edits, pause, Stop/restart
and final accumulation against a fresh render on CPU and OptiX. The active
unchanged-scene case verifies that samples continue increasing. Response
measurements remain separate from that correctness check. Frozen evidence is
indexed in `artifacts/stage1/sampling-interruption/verification-summary.json`.

The worker is the selected development path; it is not an architectural
requirement for every future platform. The decision criteria are edit-to-first-visible-image latency and
host responsiveness while edits are outstanding. Completed-frame throughput
cannot establish these properties. The same bounded controller is compared
against a test-only in-process engine in `tests/houdini_ipr_response_benchmark.py`.
That reference is not installed as another artist-facing rendering mode.

The current improvements enable Cycles' navigation resolution scheduler and a
camera-only host capture, including SOHO dirty-object guards. Camera-only events
skip SOP traversal and shader resolution; a simultaneous non-camera change
requires the full capture. Public reference dependencies also prevent a camera
that drives a SOP or material expression from incorrectly taking that shortcut.
The dedicated panel converts immutable render frames on a bounded display thread:
one conversion, one replaceable pending input and one completed image. Qt widgets
and image presentation remain on the UI thread. Settings/session/generation
changes retire obsolete conversion results; compatible progressive images do not
wait for the latest sample to finish. Supported color views now use OCIO-generated
shaders on an offscreen graphics context owned by that display thread. CPU
fallback preserves data/raw/alpha, old working policies and unsupported GPU
transforms. It still uploads and reads back display pixels; direct engine/graphics
interoperability is not implemented. The latest matched pair measures camera
median/p95 at 94.6/122.2 ms for the worker and 74.6/99.6 ms for the test-only
in-process reference. Neither that pair nor process isolation establishes the
worker as fastest. See [display measurements and scope](native-gpu-display.md).
Visible interactive viewers acknowledge presentation of the accepted render
generation. While its first preview is outstanding, newer desired edits replace
one pending snapshot instead of repeatedly resetting before any image appears.
The panel acknowledges after Qt paint; native IPR acknowledges delivery through
the documented tile device. Pause and Stop bypass this pacing. Hidden/history
viewers unregister, and a one-second fallback avoids a stalled viewer preventing
scene updates. An accepted image retains its own generation; it is never relabelled
as a newer pending scene. This follows Cycles' `PathTrace::ready_to_reset()` policy
of waiting for drawing after a reset, without introducing a private host hook.
Geometry conversion still runs on the host thread,
so neither process boundary alone makes arbitrary SOP edits stutter-free.

The adaptive benchmark adds an independent turbine with renderer subdivision
and a ground shader with animated true displacement. Benchmark reports describe
their actual backend, fixture, early preview and full-resolution Qt paint timings, UI
timer gaps and burst-edit convergence. Camera bursts hold the mouse and must not
retessellate until release; one settled refinement is checked independently.
Earlier experiments observed images through a Houdini idle callback and are
preserved as historical measurements, not equivalent latency samples.
Timer gaps are a responsiveness proxy;
they are not a physical input latency measurement or Blender parity claim.
Source behavior references:
[Blender viewport synchronization](https://github.com/blender/blender/blob/blender-v5.2-release/intern/cycles/blender/session.cpp),
[dirty geometry tracking](https://github.com/blender/blender/blob/blender-v5.2-release/intern/cycles/blender/sync.cpp),
[navigation scheduling](https://github.com/blender/blender/blob/blender-v5.2-release/intern/cycles/integrator/render_scheduler.cpp).

Houdini's `InputDataChanged` event does not include expression references.
`inputAncestors` includes reference inputs; the observer refreshes that public
dependency information after a supported capture has evaluated the scene. It
does not cook SOPs inside node callbacks. See
[node events](https://www.sidefx.com/docs/houdini/hom/hou/nodeEventType.html) and
[reference dependencies](https://www.sidefx.com/docs/houdini/hom/hou/OpNode.html).

