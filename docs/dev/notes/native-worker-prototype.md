# Native persistent worker and historical scheduling experiments

## Early poses and source completion - October 17, 2026

Host `20261017T070257430` retains one persistent native IPR worker, protocol 33, the same
32 engine patches and six unchanged GPU kernels. Restart Houdini for this runtime.

Object movement can submit accepted-root poses before the normal debounced source
capture. Acquisition uses a public host callback and owned matrices, not worker
access to live Houdini nodes. The existing immutable navigation preparation and
protocol-33 pose command carry the update; no new transport or renderer mode is
needed. The root revision is shared with full captures to prevent collisions.

The preview's source debt remains outstanding. It cannot acknowledge changed SOPs,
clear their warnings or satisfy a new-pass dependency. Completed source work keeps
its ownership and receives the latest compatible navigation before commit.
CPU/OptiX fresh-reference and held-work artist tests cover this separation and
Auto/Pause/Stop behavior. [Performance evidence](../performance.md) records the actual
paint improvement and remaining O(N), device and viewport certification limits.

## Update phase probes and immutable input - October 17, 2026

Host `20261017T054241662` retains one persistent native IPR worker, protocol 33, the same
32 engine patches and six unchanged GPU kernels. Restart Houdini for this runtime.

Ordered transform commands avoid redundant key hashing. Their immutable decoded
data stays alive through the synchronous apply/boundary callback; accepted
resources own retained values/handles. Private material expansion remains separate
from caller-owned graphs. Disabled-blur pose-only edits with unchanged variants skip all-object
displacement-motion recovery without bypassing active-blur or variant preparation.

Set `CYCLES_HOUDINI_PROFILE_WORKER=1` for existing request measurements and
`CYCLES_HOUDINI_PROFILE_UPDATES=1` for detailed core phases before launching a test
process. Both are development probes, disabled by default. Core records use the
`CH_UPDATE_PROFILE` stderr prefix and contain counts, stages and generation;
binary scene/image messages remain protocol 33.

Internal update cost reductions do not establish a visible preview improvement.
Object/general edits retain an 80 ms debounce and host-safe dispatch. Twelve actual
object-move traces measure 148.2 ms notification-to-capture
and 8.7 ms first-decode-to-paint medians. Camera-only navigation
has a guarded immediate path. Further dispatch changes must preserve independent
pending geometry, source provenance and safe host cooking.

## Transform-only command checkpoint - October 17, 2026

Host `20261017T050102773` uses one persistent native IPR worker, protocol 33 and the same
32 engine patches and six GPU kernels. Restart Houdini for this runtime.

Transform-only commands carry stable resource keys and pose data without repeating
unchanged geometry bindings, visibility, identities and object properties. Full
definitions still handle creation/rebinding/property edits. Bulk matrix encoding
and bit-identical nominal deduplication reduce command overhead. Invalid or
conflicting pose targets reject before engine mutation; older wire versions reject.

The native adapter owns each immutable pose snapshot. Ordered host merges and
differences, plus transaction-local resolved engine records, reduce repeated
searches and string copies. Required displacement/adaptive variants remain tested
on CPU and OptiX. This is incremental host/worker transport, not direct GPU sharing
or a root-transform command: whole-group moves still process individual objects.

See [performance evidence](../performance.md) for matched profiled/normal 100k-group
observations. There is one native
artist IPR path; final rendering retains ordered frames and Persistent Data.

## Compact preview checkpoint - October 17, 2026

Host `20261017T040304102` uses one persistent native IPR worker, protocol 32 and 32
engine patches. The preceding 31 patches and all six GPU kernels are unchanged.
Earlier sections record their dated builds. Restart Houdini for this runtime.

Protocol 32 adds per-pass stored dimensions while retaining logical coverage,
crop/aspect and complete-image storage. The engine's opt-in compact pass reader
preserves the existing full-tile reader contract. Preview conversion runs at the
stored resolution, with exact RGBA8 expansion on the display thread. Native image
rows expand on the HDK writer; final EXR/data/Cryptomatte conventions stay tested.

Full captures freeze current poses even when only a future scene can use their
time/shutter context. Partial material/background edits preserve the latest
navigation. Regression coverage includes frame revisits, fresh-render references,
motion-vector signs and screen bounds. Viewer attachment epochs prevent stale
paint/focus input after a worker is recreated with reused logical/physical IDs.

Large group updates,
device/viewport contention and physical-memory certification remain open.

## Viewer delivery checkpoint - October 17, 2026

Current host `20261017T025024087` retains protocol 31 and the same engine patches/kernels.
Accepted frames are handed directly to bounded background conversion using cached
display settings; Qt consumes coalesced notifications and performs GUI assembly/
painting. No producer dereferences host geometry, touches widgets or pumps capture.
Atomic accepted-frame identity and display revisions retire stale work. Closing a
viewer unregisters the source and makes pending notifications harmless. Snapshot
return restores cached Live metadata immediately, including after completion.

CPU/OptiX polling-disabled viewer tests, concurrent-source/closure unit contracts,
native recovery/ownership tests, heavy drag/five-frame cases and display/history/
fullscreen workflows cover this host.
Measured remaining limits and expanded 4K preview buffers are described in
[performance evidence](../performance.md). There remains one native artist IPR path;
the timed polling control is test instrumentation, not a selectable renderer mode.

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

## Owned navigation preview checkpoint - October 17, 2026

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

## Current capture ownership - October 17, 2026

The staged host `20261016T231151646` keeps one persistent-worker IPR path. Public
Houdini evaluation, root acquisition, packed-detail resolution and host callbacks
remain on supported host execution contexts. Pure retained-detail scans,
conversion and compatible no-shutter packed-leaf expansion run in the existing
background capture path. Display/color conversion is already background work;
Qt painting remains on the GUI thread. The historical host-thread-only capture
measurements below describe earlier builds.

Camera/material-only snapshots now share immutable object tables. Object mutations
create private tables; diffs skip unchanged ownership without weakening strict
resource validation. Native camera-only commits skip alias retirement. The first
camera edit after serial-zero initialization also uses the shortcut when time,
motion and outstanding general edits permit it. All three current heavy camera
trials at each of 1k/10k/100k instances use zero geometry scan batches.

Independent navigation while a heavy geometry capture is pending is still
unfinished: the queue accepts/coalesces input, but its combined preview waits
for that capture. The next scheduler change must render navigation against the
last accepted geometry, preserve pending geometry, merge the latest camera/object
state at commit and discard superseded images without duplicating the session.
See [current measurements and limits](../performance.md). This remains development
work, not a claim that worker isolation has eliminated host UI stalls.

## Current result

Native IPR now uses one persistent external worker by default, in both viewers.
There is no execution-mode menu. The extra local queued backend has been removed;
ordered final ROP frames and the headless reference retain the shared in-process
engine core. The repeated edit-to-paint comparison supports the worker through
better camera tail latency; the in-process reference has faster medians for
several other edits. See [the current policy](native-execution-policy.md) for the
selection, measured scope and remaining host-capture pauses.

The following comparisons are preserved historical evidence. Mode B kept Cycles inside
Houdini with a separate control thread. Mode C ran the same session/update
semantics in a persistent standalone ch_worker.exe. That process owns Cycles,
its scene, device work and GPU allocations; the earlier SOHO helper remains a
separate control-only bridge. Native OBJ/SOP work remains the priority.

Normal edits in B and C no longer fully cancel/restart the Cycles session.
They queue owned desired scene state, replace obsolete pending state, and commit
at a safe render-iteration boundary before resetting accumulated sampling.
The heavier-scene measurements did **not** establish a speed advantage or solve
viewport pauses. They do not certify the selected worker path's latency or memory
gates. The synchronous baseline and local queued mode below describe those older
experiments, not current artist-facing choices.

The restarted five-frame test uses **one render viewer**, a native deforming
turbine and five actual animated displacement maps. Sync/queued/worker totals
are now 9.118 / 9.413 / 13.346 s after direct mesh capture, valid
triangulation reuse and component-specific bulk reads, including complete
panel images and file output.
These are interactive sequence timings. The earlier two-viewer comparisons
below remain historical compatibility evidence. A separate alternating
viewport/capture-only test measures about 35 ms displayed-SOP resolution and
250 ms added owned capture on cooked geometry, versus 310 ms after direct
conversion and 677 ms originally, with no Cycles
rendering session or GPU upload. Capture still runs on the host thread. The
worker boundary does not make that work asynchronous.
[The performance report](../performance.md) supplies per-frame
accumulation, image checks, timing scopes and frozen sources.
[Workflow policy](native-workflow-policy.md) uses Blender's source as the main
reference and separates IPR responsiveness from final-render throughput.

The complete regression run
artifacts/verification/20261011T153413210/summary.json passes **112/112 cases**:
64 headless cases and 24 actual GUI workflows on each of CPU and RTX 4090. Its
immediate audit matches 269 integration sources, five binaries, fourteen patches
and six kernels. The queued-worker headless case exercises all three modes on CPU; the
24-per-device GUI regression group uses the default synchronous mode. Separate
OptiX transport, heavy GUI and active-worker-failure evidence is listed below.
This is development evidence, not full artist-workflow parity for each prototype.

## Execution and synchronization

| Mode | Session ownership | Scene application |
| --- | --- | --- |
| A: sync | Houdini process | Existing synchronous capture, cancellation join, apply and restart. |
| B: queued | Houdini process, one control thread | Owned host snapshot submitted without waiting for engine application. |
| C: worker | Persistent standalone renderer, same controller | Same desired-state semantics plus versioned binary/shared-memory transport. |

B and C use the same compiled core, persistent synchronization hook, resource
keys, settings and snapshot representation. A-to-B changes both execution
scheduling and the engine cancellation policy; it cannot isolate thread overhead
alone. These development modes do not commit the first release to maintaining
multiple native backends.

Patch 0014 adds Session::synchronize_scene to the pinned standalone engine. A
control caller queues a callback and waits for completion on its own thread.
The render thread handles that callback at an iteration boundary, including
paused/idle states. Existing engine preparation, rendering and buffer ownership
remain serialized at that boundary. Queued edits do not replace data referenced
by an active kernel. The preceding committed scene can trace while the new
snapshot is being captured or waits for synchronization.

After commitment, Session::reset discards the previous scene's accumulated
samples. Its internal PathTrace::cancel flag still participates in reset at the
safe boundary. The verified zero-cancellation result concerns **full session
cancellation**, not removal of all internal cancellation flags. Explicit Stop,
shutdown and coordinated working-color changes remain separate control paths.
The test observed up to 503 ms of boundary waiting in B, on the control thread.
No scheduler work-interval policy was changed.

[Blender's viewport integration](https://github.com/blender/blender/blob/main/intern/cycles/blender/session.cpp)
provides the reference for delayed busy synchronization and reset. Our CPU-buffer
display does not implement Blender's draw-based reset readiness; ready_to_reset
is not a device-idle test. The recorded patch must reproduce against the pinned
engine; its latest patch audit is artifacts/patch-verification/20261011T120301288366/.

## Desired state, acknowledgements and lifetime

The controller retains complete desired host state separately from its last
acknowledged engine state. Unchanged geometry is shared through immutable owned
resources, not copied into every desired snapshot. Host capture caches describe
staged desired state; they do not claim that the renderer has committed it.

There is one transaction in flight and one replaceable pending snapshot. A newer
camera value replaces an older pending camera value without erasing an earlier
unacknowledged mesh change. The controller derives the next resource diff from
the last acknowledged state to the latest desired state. Deletions, type changes
and shared dependencies must retain their final desired state.

An in-flight transaction can finish before its replacement. It cannot be
arbitrarily removed after engine mutation has begun. Epoch, transaction and
physical render identifiers distinguish its acknowledgement and images. A
monotonic frontend image sequence also allows native IPR to accept images from
a restarted worker whose engine sequence began again at one. Pending updates
retain the last usable preceding image.

For the prototypes, public generation is the submission identifier; physical
Cycles generations are available in controller events. Reusing an unchanged
engine render can advance that public identifier. Full unchanged-generation
parity across the existing UI/API remains an acceptance item.

Stop retires submissions through the public generation that exists when it is
pressed. A later Render can be submitted before the backend acknowledges Stop;
that new generation stays pending instead of inheriting the old cancellation.
Discarded pending edits are no longer advertised as queued work. Restart
captures the current host scene and compares it with the last acknowledged
backend scene, retaining unchanged geometry.

## Rejected edits and renderer failures

The backend explicitly distinguishes pre-transaction validation rejection from
an unsafe renderer failure. `RenderSession::apply` classifies an
`invalid_argument` only during validation and owned graph preparation, before
cancellation or the synchronization/scene-mutation boundary. It does not infer
safety from error text or an unchanged generation. Exceptions during a scene
transaction, device errors and disconnected/malformed transport remain failures.
An engine-reported failed session is unsafe even when the command reply itself
succeeded. It follows the same backend retirement and replay path.

On rejection, the queued controller retains the backend and the **last accepted
snapshot acknowledgement**. It does not acknowledge the malformed desired
scene, advance the committed render generation, or accept a frame from that
submission. The complete latest desired snapshot still contains staged host
edits, so a later valid capture is diffed against the accepted scene and recovers
unacknowledged geometry as well as corrected materials. An obsolete rejection
cannot overwrite the status of a newer pending edit.

Public status keeps the rejected submission as `state=failed` with
`update_rejected=true`. `backend_state` reports what the accepted render is
actually doing. Polls can continue delivering correctly tagged accepted-scene
tiles, but cannot erase the diagnostic. In the dedicated panel, **Edit rejected**
identifies this state. Render can retry; Pause/Resume and Stop still control the
accepted render. A pending edit no longer unchecks Pause while the engine is
paused. Stop clears the rejected submission, retires pending edits and retains
the existing explicit Stop semantics.

The renderer's texture notification ledger may track a failed candidate path
without installing that shader. An unchanged missing file is not a reason for
repeated automatic retries. Creating or repairing the file changes its watch
revision and permits a new capture. The committed graph owns its resolved
texture assets independently of the candidate notification policy.

Working-color changes remain a separate global epoch operation: they can stop
tracing before scene validation and require coordinated resource invalidation.
Rejection does not undo that color change or promise that an old-color render
continues. Its previous image retains its original color policy. Generic
transaction/transport/device failures discard the unsafe backend and require
full replay on a replacement worker. The same recovery regression still kills
an actual worker to verify this distinction.
An additional injected failed-status reply tests the status channel without
causing an actual GPU out-of-memory/device fault on the artist's workstation.

Payload protocol **16** includes the explicit rejection flag. CHWP command
framing remains version one. Worker and host binaries must come from the same
build manifest; mismatched payload versions fail visibly.

Focused evidence is indexed by
`artifacts/stage1/update-rejection/verification-summary.json`. The technical
fixture covers all three execution modes, rejected initial scenes, six repeated
reject/repair cycles, a real missing EXR repaired on disk, active/paused edits,
Pause/Resume/Stop, and a captured mesh edit superseded by a corrected camera
capture. A fresh independently captured synchronous session checks the final
geometry/camera image. The artist fixture uses over one million triangles plus
packed objects, particles, curves and VDB fields, one render viewer, native
parameter entry and actual panel buttons. A separate native IPR fixture checks
the persistent SOHO control pipe and helper/worker identities.

These are bounded development regressions. They do not certify long-session
memory plateaus, arbitrary host-capture failures, native diagnostic presentation,
all concurrent color/device failure combinations or a production process policy.

## Worker transport and owned inputs

The worker is a standalone renderer, not another Houdini application. It uses
the same SDK-compatible dependencies and prebuilt kernels recorded in the
manifest. It does not cook SOP nodes or receive native Houdini pointers.

Small CHWP version-one commands travel over persistent stdin/stdout pipes.
Geometry, configuration and frame payloads use Windows named shared mappings.
The little-endian codec explicitly serializes fields, with an 8 GiB packet bound;
it does not copy C++ object layouts, callbacks or pointer addresses. Individual
string/blob containers have a 512 MiB decode limit, including one serialized VDB
grid. Larger production grids need a revised streaming/chunking contract.

Only one command exchange is active. Input mappings remain alive through the
commit/rejection reply. Output mappings remain alive until the parent has decoded
owned data and sent an explicit acknowledgement. Serialized volume grids use
OpenVDB streams and owned decode, without delayed loading. This route performs
serialization, mapping copies and decode; it is **not zero-copy**. Camera edits
retain worker geometry and grids already committed.

Host-dependent texture Auto policy and texture headers are resolved into owned
metadata on the host. Rendering callbacks use that frozen policy. Changed headers
require recapture. OCIO configuration, directory/context and render-policy
identity are serialized separately; received frames regain the matching owned
host display configuration. Current op: COP texture support remains unfinished.
Actual working-color reconfiguration, denoising, all settings and all existing
artist workflows need additional queued-mode certification.

Live HOM/HDK capture and cooking stay in supported host execution contexts.
[SideFX's thread-safety requirements](https://www.sidefx.com/docs/hdk/_h_d_k__node_intro__thread_safety.html)
still apply. Owned snapshot encoding and engine work run on the control thread.
General parallel host scene extraction and conversion have not been implemented
by moving the renderer into the worker.

Status, diagnostic and image polling read cached controller state. Both viewers
share one session and GPU scene when settings match. While the single control
thread waits for an apply acknowledgement, it does not fetch fresh intermediate
frames: the last image remains visible. Independent asynchronous image delivery
and graphics interoperability remain future optimizations. Display/view conversion
is still on the host CPU.

## Measured heavier-scene comparison

The primary performance fixture is a scaled copy of the editable SOP courtyard.
The artist's saved file was not overwritten. Ground, rocks, turbine, packed
panels, particles, curves, VDB fields and true renderer noise displacement are
present; substantive geometry uses native SOPs/VEX with one tiny Python accent.

| Actual owned fixture count | Value |
| --- | ---: |
| Mesh triangles / vertices | 950,906 / 476,734 |
| Objects / geometry resources / materials | 352 / 5 / 11 |
| Particles | 1,200 |
| Curves / curve keys | 605 / 7,520 |
| VDB fields | 3 |
| Source points / primitives | 485,541 / 482,111 |

One isolated real GUI run per mode used RTX 4090 OptiX, 960×540, 65,536 samples,
no denoising, matching OCIO, a native Scene Viewer, the dedicated panel and native
IPR. Thirty warm edits cover camera, transform, surface color, mesh deformation,
topology, noise displacement, curve width, particle radius, volume transform and
area-light creation/movement, three edits of each type. Settings were opened in
Houdini's native parameter editor. QtTest clicks and HOM edits actual UI/nodes;
physical manual handle dragging is not yet verified.

The modes ran A, B, C once each, without randomized/interleaved repetitions.
Results are exploratory; a pooled percentile across different workloads is not
certification of the plan's specified p95 budget. Timings include host capture,
engine work, scheduling and arrival of a current full-resolution canvas image.
A 16 ms Qt heartbeat measures observed event-loop gaps, including viewport/display
work; it does not isolate all delay caused by the renderer.

| Observation | A: sync | B: queued | C: worker |
| --- | ---: | ---: | ---: |
| Median edit to current canvas, all 30 edits | 752 ms | 764 ms | 812 ms |
| Maximum warm UI heartbeat gap | 683 ms | 869 ms | 748 ms |
| Median mesh-deformation host apply | 1,418 ms | 1,012 ms | 1,031 ms |
| Median mesh-deformation edit to canvas | 2,342 ms | 2,432 ms | 2,855 ms |
| Full session cancellations before explicit Stop, including initial submission | 31 | 0 | 0 |

The worker is not demonstrated faster. Engine cancellation no longer blocks
queued edit submission, but full host mesh capture still costs approximately a
second here. Camera/transform edits have zero adapter geometry conversions and
no observed triangle/curve/point buffer-copy requests. Mesh deformation captures
one mesh component; curve, particle and volume edits retain other supported
components. Topology captures three components through conservative index changes.
Those logical counters do not measure physical PCIe traffic.

Across C's approximately 51-second workflow, transport counted 791,804,219 input
bytes and 3,120,123,646 output bytes over 557 exchanges. Accumulated encoding,
decoding and mapping time was 0.463, 1.733 and 0.229 seconds. These are cumulative
host codec observations, not per-edit latency or physical bus bandwidth. The
full-image CPU route needs profiling and reduction.

End-of-run host and worker memory snapshots are recorded separately. They do
not establish a simultaneous total memory peak or a memory saving. Historical
per-capture cooking_seconds fields were incorrectly subtracted as cumulative
values in the raw benchmark; the derived report excludes that cooking_delta.
Use total edit/host-call timings, not that field, for this comparison.

Exact reports, original manifest, hashes and a checked archive of all 264
comparison integration files are retained in
artifacts/performance/native-execution-20261011T123609203/. Manifest SHA-256:
693c509ea285dd7df5b112251d7fe8c32759d0c62cf750e1a17f65f5592da42a.
Later changes repaired shutdown evidence and added tests without changing the
comparison's edit/update policy. source-snapshot/snapshot-audit.json verifies
that the historical source copy matches the original manifest.

## Correctness and worker failure evidence

Courtyard transport/coalescing tests compare all three modes with the synchronous
image and with a fresh final desired-state render. CPU final comparisons are
identical; the coalesced OptiX comparisons have RMSE below 5×10^-8. Paused edits,
explicit cancellation/restart and worker reconstruction pass. This checks mode
agreement; independent conversion expectations are supplied by the existing
geometry, material, motion, volume and color regressions.

The isolated focused runs are:

- CPU: artifacts/verification/prototype-sync-CPU-20261011T122233515/.
- RTX 4090: artifacts/verification/prototype-sync-CUDA_NVIDIA_GeForce_RTX_4090_0000_03_00_OptiX-20261011T122249932/.
- Heavy A: artifacts/verification/prototype-sync-CUDA_NVIDIA_GeForce_RTX_4090_0000_03_00_OptiX-20261011T122953393/.
- Heavy B: artifacts/verification/prototype-queued-CUDA_NVIDIA_GeForce_RTX_4090_0000_03_00_OptiX-20261011T123419986/.
- Heavy C: artifacts/verification/prototype-worker-CUDA_NVIDIA_GeForce_RTX_4090_0000_03_00_OptiX-20261011T123609203/.
- Active worker failure: artifacts/verification/prototype-worker-CUDA_NVIDIA_GeForce_RTX_4090_0000_03_00_OptiX-20261011T125825631/.

Each has terminal results and an immediate source/binary audit. Early failed
prototype runs remain archived: paused-boundary deadlock, missing VDB type
registration, duplicate packed identities in the scaled fixture and incorrect
test cleanup were corrected before the successful comparisons.

The active-failure run terminates only the owned worker while the same heavy
scene is tracing. Houdini stays open, reports failure, retains its image and
reconstructs the scene in a new worker epoch. Failure detection took 52 ms;
recovery to current images in both viewers took 1.800 seconds. Stop submission
took 0.029 ms and cancellation acknowledgement took 98 ms in that run.

This establishes containment of the tested worker termination. It does not
contain host adapter/UI faults or guarantee isolation from GPU-driver failures.
Failure during scene mutation/image delivery, unresponsive-worker timing, scene
reload and long sequences require broader certification. Parent-exit monitoring
and a 60-second exchange watchdog are implemented but not stress-certified.
The original PID 19708 light crash and reported gear crash remain unreproduced.

Explicit close still joins the control thread and can wait five seconds before
attempting to terminate an external worker, followed by another bounded join.
An in-process backend cannot safely be killed independently. Do not describe
all prototype lifecycle controls as universally nonblocking.

## Next optimization and process decision

The adapter currently recaptures the complete ordinary mesh component after a
position/attribute edit. capture_mesh copies or merges a GU_Detail and calls
convex(3) again. In this merged courtyard, moving the turbine recaptures the
component containing unchanged ground and rocks. A resource count of one does
not mean only changed vertices were copied.

Next work is to measure and retain valid triangulation/provenance, separate
position/normal/attribute updates from topology and material-assignment changes,
and move expensive preparation of safely owned data away from UI work where
supported. The viewport/capture-only comparison now identifies substantial
plugin CPU cost independently of engine/GPU work; redraw remains separate.
Then refine display transport and repeat interleaved heavy measurements. Do not
move arbitrary live SOP access to loader threads or omit dependent attributes.

Accept the worker for production only after bounded-memory, required-feature,
responsive artist-workflow and matched-harness gates pass. Fault isolation is
a measured benefit; current response timings do not justify a speed claim.
Keep the process boundary open while resolving the measured host capture cost.
Solaris, broad GPUs/platforms and renderer feature expansion remain separate work.

## Reproduce or test the interface

The native SOP launcher starts the persistent IPR worker automatically:

    & '<repository>\scripts\launch-sop-showcase.ps1'

No environment selector or ROP mode menu is needed. Direct development Session
calls retain sync/worker for engine/transport comparisons; the local queued mode
is retired. The normal test-drive file is
the editable courtyard, while the comparison script scales an isolated copy.
See [native SOP controls](../../user/try-native-sops.md) for the actual interface and limits.

Reproduce a heavy GPU mode, from the repository directory:

    .\scripts\run-prototype-test.ps1 -Test houdini_heavy_animation_benchmark.py -Gui -Execution worker -Device 'CUDA_NVIDIA GeForce RTX 4090_0000:03:00_OptiX' -TimeoutSeconds 240

Run in a fresh owned process. The runner records a manifest/test hash, terminal
result, real GUI report and immediate audit. Active failure uses
houdini_worker_active_failure_smoke.py with the same worker/GPU arguments.
Viewport/capture attribution uses houdini_heavy_viewport_baseline.py with
-Gui -Execution sync and the same device. Regular animation/response tests
open one render viewer; only explicit shared-view compatibility tests open two.
The complete default regression command is:

    .\scripts\run-tests.ps1 -Gui -GuiRepetitions 1 -StopOnFailure -TimeoutSeconds 120
