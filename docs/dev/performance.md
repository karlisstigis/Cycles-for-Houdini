# Performance evidence

## Quick Stop race and session join - October 18, 2026

Host `20261018T005418397` holds quick cancellation through the Cycles session-thread join.
The earlier flag only interrupted the current path-tracing batch, then cleared
before session shutdown. A new batch could start in that gap. Atomic requests
now remain latched until the session is idle, and clear before a later restart.

The matched detailed trace recorded 4.5 ms draining the current batch and
15.17 s in `cancel/session_wait`. After the fix the join is approximately
15 microseconds in the matched fresh-cache case. End-to-end request-to-terminal
acknowledgement improves from 15.24 to 0.063 s (warm: 15.23 to 0.062 s).
Notice, engine cancellation and resource release are measured separately.

Final background regression trials report maximum acknowledgement of 21.4 ms
on CPU (two Stops), 64.8 ms on RTX 4090 (six), and 64.5 ms on RTX 3080 Ti (six).
Each run also completes the five-frame color/recovery/output lifecycle checks.
Five direct cancel/restart cycles per device retain one heavy geometry resource
for 16 instances, finish a checked image and release allocations. Native IPR
Stop/restart, progressive focus and the heavy background GUI workflow pass.

Opt-in `engine_update_seconds` includes `cancel/device`, `cancel/path_trace`,
`cancel/progress`, `cancel/session_wait` and `cancel/total`. The existing
`cancel_join_seconds` measures the integration's total engine cancellation.
Disabled diagnostics retain their empty engine phase map; wire protocol 35 is
unchanged. Shader kernel binaries are unchanged by these host-side patches.

Private GPU disk caches exercise first-use and warm work without deleting the
artist's caches. NVIDIA documents the [OptiX cache override](https://raytracing-docs.nvidia.com/optix9/api/group__optix__host__api__device__context.html)
and [CUDA cache environment](https://docs.nvidia.com/cuda/cuda-programming-guide/05-appendices/environment-variables.html).
Both first-use and warm traces reproduced the old join race; cache warm-up alone
did not fix it. These regression observations do not certify physical input,
arbitrary GPU work, mixed devices or memory plateau. See
`artifacts/latched-cancel-evidence-20261018.json` for exact sources and results.


## Fresh packed deformation expansion - October 18, 2026

Host `20261017T231527050` shares immutable component metadata through shutter acquisition,
then materializes one active description per compatible profile/root. The earlier
100,000-instance fixture cloned 100,000 descriptions and immediately collapsed
them to one. Motion correspondence now performs each sampled lookup once and
reserves transform arrays before filling them.

Matched unprofiled 100,000-instance deformation calls:

| Shared prototype | Before conversion | After conversion |
| --- | ---: | ---: |
| Two triangles | 1.286 s | 0.878 s |
| 1,036,800 triangles | 1.926 s | 1.647 s |

The comparison records all cold/warm/deformation/camera/object/prototype actions
at 1,000, 10,000 and 100,000 instances. It retains cooking, every host API phase,
background conversion, geometry conversion and wire encoding separately. Host
API totals explicitly include prewarm scan, omitted from the older report's
subtotal. These preparation profiles do not include GPU synchronization, BVH or
image presentation, and are individual runs rather than statistical trials.

Independent 960 x 540 viewer runs retain the million-triangle/16-instance fixture
and add a two-triangle/100,000-instance fixture. The latter tests both object and
camera feedback during geometry preparation, not the defined release-budget
fixture. Actual Qt paints, pending-work flags and five observations per action
are retained in `artifacts/packed-expansion-comparison-20261018.json`. GPU density
overlap paints happen before capture finishes in all ten cases. CPU/GPU images,
five-frame outputs and cleanup pass; physical input, a process-memory plateau
and mixed-device certification remain separate.


## Progressive focus buffer ownership - October 18, 2026

Host `20261017T225512290` keeps one current-revision Position/Depth buffer for
click-to-focus rather than requesting all enabled AOVs on every progressive update.
The cache updates when effective resolution changes. Repeated beauty updates do
not download or transport unchanged picking buffers; a native single-pixel accessor
reads compact storage without full-image expansion on Houdini's UI thread.

CPU, RTX 4090 and RTX 3080 Ti long-render checks pass. 1080p and 4K development
checks on the 4090 include actual Qt canvas clicks while the image is incomplete.
The measured current generation has one read per picking pass in small fixtures
and two in 4K as resolution improves, with stable counts after reaching full
resolution. Enabled Normal and Sample Count have zero reads. Final reports
separate individual click-handler time from time since the first workflow image.
They do not establish physical-input latency percentiles, memory plateau, mixed-GPU
certification or faster packed conversion. Evidence and limitations are retained in
`artifacts/progressive-focus-comparison-20261018.json`.


## Camera first-image delivery - October 18, 2026

Host `20261017T222554370` removes avoidable notification and first-frame polling delays. A
camera-only change is checked after both source observers run, then captured on
the next Qt turn without cooking geometry. Pending picking-pass changes stay on
the supported full-capture route. A visible viewer gets a bounded 2 ms worker poll
while awaiting its first new image, reverting after that image or 80 ms. The wait
is interruptible by controls. Normal progressive pacing is unchanged.

Both uninstrumented timing runs use the same benchmark source and geometry: a
1,036,800-triangle animated prototype, 16 instances, 960 x 540, 32 samples, three
motion samples, no denoising, and a genuine emission material with light-tree
sampling enabled. There is one Cycles viewer. The stock test layout also has
Houdini's main viewport and a 650 x 500 floating viewport; both are included in
the before/after runs. Each action has five scripted HOM parameter edits. Time
starts at the parameter action and ends at a newer nonuniform actual Qt paint.

| Edit | Median before / after | Observed p95 after |
| --- | ---: | ---: |
| camera | 63.1 / 59.1 ms | 61.3 ms |
| transform | 195.2 / 193.8 ms | 217.7 ms |
| camera-during-geometry | 92.7 / 24.5 ms | 99.5 ms |
| transform-during-geometry | 212.8 / 216.4 ms | 270.1 ms |


The detailed traces use only the host perf-counter clock and retain metadata,
not pixels. No worker-relative publication timestamp is subtracted from host
time. During camera edits overlapping geometry work, median input-to-decoded-frame
changes from 36.2 to
29.3 ms. The converted-image-to-GUI-take
wait changes from 33.5 to
57.1 ms. Current conversion, GUI merge
and paint medians are 5.7,
0.2 and 0.6 ms.
These nested medians do not add exactly, and traced runs have their own overhead.

Run-to-run host waiting remains significant. Earlier candidate runs had camera
medians near 27 ms; the final uninstrumented camera median is
59.1 ms. Its camera-overlap maximum is
117.3 ms, while the final traced
camera-overlap median is 104.4 ms.
That trace receives the decoded frame in
29.3 ms and then spends substantial time
waiting for the GUI. The implementation removes its dispatch/poll delays; normal
viewport drawing and cooking can still dominate end-to-end latency.

Diagnostic controls retain identical render work while changing only the test
viewport layout. A single viewport reduces its prior conversion-to-take wait;
disabling that viewport's redraw reduces it below a millisecond. This attributes
much of the old wait to host redraw/event scheduling, not image assembly or SOP
acquisition in the display callback. Those controls are not shipped behavior or
speedup claims. Artists keep normal Houdini viewport drawing. The new notification
and first-image paths can present sooner before that work occupies the GUI.

Camera-only cases have no new geometry conversions or triangle-buffer uploads.
Held-source tests prove that camera/object previews paint while million-triangle
capture/preparation is blocked, then preserve geometry and latest navigation.
Source-dependency tests independently verify camera-driven geometry by Position
and shader changes by radiance. A focus-pass race test intercepts full acquisition
and rejects any attempt from camera-only Qt dispatch, then checks current picking
data after the complete source update. CPU and OptiX shutdown checks wait for
closed state and zero tracked owned/device/shader resources.

These are exploratory observations, not Blender parity, physical mouse/compositor
latency or release p95 certification. Individual viewport/cook delays remain in
the full distributions; a fast median does not prove zero stutter. The before/after
reports retain cooking, extraction, capture, transport, engine and presentation
metadata separately. No direct GPU-sharing path or sampling-quality change was
introduced. Fresh 100k-instance preparation and native production completion
remain separate work.

See exact native runs (`artifacts/verification/camera-delivery-20261017/index.json`)
and `artifacts/camera-delivery-comparison-20261018.json`. Early profiler/fixture
failures are preserved and excluded from the accepted comparison.


## Direct mesh-light-tree flattening - October 17, 2026

Host `20261017T213911615` reads compatible exact numeric local-tree snapshots directly while
packing current kernel arrays. It removes a cloned pointer tree and its retirement,
without changing sampling or retaining stale instance state. Both timing runs use
`CYCLES_HOUDINI_PROFILE_LIGHT_TREE=1`; the preceding 34-patch host is the profiled
baseline, and the current 35-patch host uses borrowed snapshots. Six kernel hashes
match. The scene has a 1,036,800-triangle animated shared prototype, 16 instances,
960 x 540, 32 samples, three motion samples, no denoise, one Cycles viewer and a
real Houdini Scene Viewer. Each action has five scripted HOM edits. Measurement
starts at the parameter action and ends at a newer nonuniform actual Qt paint.

| Edit | Median before / after | Observed p95 after |
| --- | ---: | ---: |
| camera | 70.1 / 72.7 ms | 73.2 ms |
| transform | 638.1 / 202.7 ms | 209.5 ms |
| camera-during-geometry | 95.3 / 85.9 ms | 97.2 ms |
| transform-during-geometry | 735.0 / 207.6 ms | 212.9 ms |


Warm light-tree phase medians change from
623.1 to 159.7 ms.
Construction changes from 265.6 to
22.7 ms; cloned nodes from
237.0 ms to zero; flattening from
168.1 to 105.6 ms;
retirement from 172.3 to
7.9 ms. Current upload median is
24.2 ms. Medians of nested phases do not
sum exactly. Current emitter construction, nonuniform-transform triangle recount,
top-level hierarchy and uploads remain required work.

Retained numeric data peaks at 150,129,872 bytes in this fixture.
The 256 MiB/1024-entry cap includes vector capacities and entry payload, plus
separately bounded map overhead. A build pins every borrowed entry until its
short-lived builder retires. Admission is declined when all eviction candidates
are pinned; the source still renders through normal construction. The view uses
relative node/primitive ranges and current emitter packing. Thirty-six full beauty
and depth/normal/position comparisons per CPU/4090/3080 Ti include multiple current
emitter ranges, actual instance-reference swaps, negative/nonuniform transforms,
topology/emission changes, subdivision/displacement and five written frames.
Eleven budget states per CPU/4090 cover eviction, pinned admission, oversized
sources, 1025 owners, pruning and zero-byte retirement. Logical tracked bytes are
not whole-process RSS, physical VRAM or peak-transient certification.

Opt-in stderr records `CH_LIGHT_TREE_PHASES`, `CH_LIGHT_TREE_CACHE` and
`CH_LIGHT_TREE_INVALIDATION` separate construction, cloning/views, flattening,
upload, retirement and conservative invalidation reasons. Geometry revisions now
also cover internally generated volume boundary meshes. Current native sparse
volume adapters do not thereby gain new mesh-emitter support. Structural resource
changes can still conservatively invalidate a local tree without host conversion.

The latest 16 ms Qt heartbeat while awaiting paint has observed p95
16.5 ms and maximum
86.5 ms. Capture-wait maximum
is 217.3 ms. These include
viewport work and instrumentation. Earlier direct-view runs show more variable
camera timing during geometry preparation; this one run does not establish a
camera latency guarantee or isolate host scheduling, display delivery and viewport
costs. Further attribution and physical input/memory/device certification remain.

The verification index (`artifacts/verification/flat-light-tree-20261017/index.json`)
and `artifacts/flat-light-tree-comparison-20261017.json` retain exact sources and
both phase/timing series. A system restart interrupted one attempt; its report was
NUL-filled and no exit code was observable. It is explicitly failed/interrupted,
separate from completed reruns. Windows events did not establish a cause.


## Bounded mesh-light-tree reuse - October 17, 2026

The genuine-emission baseline spends most transform-update time rebuilding the
mesh-light hierarchy, despite retaining mesh geometry and avoiding triangle
uploads. Host `20261017T201600302` caches the exact local hierarchy and sorted primitive
sampling fields. It still rebuilds current instance measures, visibility,
primitive/object indices, top-level hierarchy and all kernel light-tree buffers.
Nonuniform scales retain upstream transformed-triangle measure evaluation.
Sampling defaults and illumination are unchanged.

Cache keys use a unique geometry-lifetime/edit revision, palette emission sampling
and exact estimates, applied-transform state and representative link membership.
The revision advances after internal geometry generation, before dirty flags
clear. Only completed local construction is cached. Incomplete/canceled restore
and snapshot work is discarded. Uniform local linking is validated
before caching; future unsupported local-link layouts simply use normal builds.
Stored nodes use 64-byte scalar records rather than padded renderer structures;
their float values are exact. Current object ids and relative leaf ranges are
restored for each build rather than retaining scene/kernel indices.

The same native packed fixture uses a 1,036,800-triangle animated prototype,
16 instances, 960 x 540, 32 samples, three motion samples and no denoising. There
is one persistent worker and one Cycles viewer plus Scene Viewer. Each group has
five scripted HOM parameter edits. Timing starts at the parameter action and ends
at a newer, nonuniform actual Qt canvas paint. The independent baseline uses the
preceding host with no local cache; it keeps the light tree enabled.

| Edit | Median before / after | Observed p95 after |
| --- | ---: | ---: |
| camera | 83.5 / 69.2 ms | 74.8 ms |
| transform | 1738.4 / 561.3 ms | 571.5 ms |
| camera-during-geometry | 92.1 / 78.8 ms | 173.2 ms |
| transform-during-geometry | 3742.0 / 687.7 ms | 814.8 ms |


Retained cache storage for this fixture peaks at 150,129,864 bytes
(150.1 decimal MB). The initial padded representation retained
248,832,024 bytes; its intermediate timing/evidence is also archived. The cache
accounts vector capacities and entry payloads, evicts before large candidate
allocation, bounds recorded retention to 256 MiB and entry count to 1024, and
clears removed sources, disabled trees and session retirement. Hash-map overhead
is independently bounded by the entry cap. Counters are logical retained storage,
not whole-process RSS/physical VRAM/peak-transient measurements. Cancellation
checks run during restore/snapshot; canceled construction is never stored.

Fresh cache-disabled reference workers check complete beauty and depth/normal/
position output for 29 states per CPU/RTX 4090/RTX 3080 Ti. A separately placed
diffuse receiver proves that emission still illuminates the scene. Geometry,
palette/estimate edits, negative/nonuniform transforms, packed sharing, internally
generated subdivision/displacement, emission/visibility changes, deletion and
five-frame written sequences are covered. Actual byte-limit and 1024-entry-limit
eviction, recurring owners, oversized sources, pruning and zero-byte retirement
have eleven states per CPU/RTX 4090. Native IPR/color and adaptive-subdivision
regressions retain their separately recorded scopes.

`CYCLES_HOUDINI_PROFILE_LIGHT_TREE=1` writes opt-in JSON cache counters to worker
stderr. `CYCLES_HOUDINI_DISABLE_LIGHT_TREE_CACHE=1` is a matched-reference test
switch; neither introduces an artist-facing execution mode. Details and exact
sources are in the verification index (`artifacts/verification/local-light-tree-cache-20261017/index.json`)
and `artifacts/light-tree-cache-comparison-20261017.json`.

These are development observations, not release-percentile certification or a
Blender performance comparison. The observed transform p95 still exceeds 500 ms.
Retained-node cloning, flattening and large light-tree uploads remain measurable
work; current nonuniform-transform recount is also retained. GPU sharing or stale
lighting shortcuts were not introduced. Fresh instance/profile expansion and
physical UI/memory/device certification remain separate unfinished work.

The 16 ms Qt heartbeat has a measured p95 of
16.3 ms while awaiting paints. Its maximum
gap is 154.6 ms in that phase and
293.8 ms while awaiting source capture;
baseline maxima were 85.5 and
237.0 ms respectively. These observations
include actual Houdini viewport work and test instrumentation; they do not isolate
the cause of each gap or prove zero UI stalls. Separate attribution is required
before responsiveness certification.

## Owned display copies and data packing - October 17, 2026

The baseline at 4K spent about 2.4 ms in GUI QImage assembly and 54 ms in background
beauty display conversion. Float input preparation alone cost about 17 ms. That
evidence targeted redundant buffers before adding another GUI/graphics transport.
Host `20261017T194800117` now exposes an owned, read-only frame-plane buffer to the GPU
converter. A Python buffer exporter holds a shared immutable frame until its last
consumer releases it. NumPy consumes that view without duplicating float pixels.
The existing native helper keeps copied-byte input as its default for other callers.

After GPU readback, a sealed NumPy allocation is retained through a read-only byte
view. Full-size images no longer call `tobytes()` and copy all display pixels while
holding Python's lock. Compact display input still expands into independent owned
bytes with the existing native-origin row alignment. Expansion rejects writable,
strided, ownerless and invalid-shape storage. GL upload/readback remain; this is
owned CPU-buffer sharing rather than direct Cycles/graphics interoperability.

A full 3840 x 2160 GPU display frame avoids a 132,710,400-byte float temporary and
a 33,177,600-byte readback copy. CPU raw/data/alpha conversion independently avoids
its 132,710,400-byte intermediate float RGBA workspace. These are eliminated
allocations/copies, not measurements of physical residency or whole-process peak
memory. The display queue still bounds active, pending and completed work.

The same owned calibration images are converted after renderer shutdown, with
seven warm observations per case at 540p, 1080p and 4K. The fixture is rendered on
RTX 4090; diagnostics identify the actual OpenGL adapter separately. The measured
GL renderer is also RTX 4090. CPU raw/data fallbacks are part of the same converter.

| Resolution | Case | Conversion median before / after | GUI assembly median after |
| --- | --- | ---: | ---: |
| 1920 x 1080 | view | 12.10 / 5.75 ms | 0.52 ms |
| 1920 x 1080 | gamma_exposure | 11.31 / 5.86 ms | 0.54 ms |
| 1920 x 1080 | alpha | 20.46 / 4.13 ms | 0.58 ms |
| 1920 x 1080 | normal | 17.35 / 6.62 ms | 0.59 ms |
| 3840 x 2160 | view | 54.03 / 23.40 ms | 2.38 ms |
| 3840 x 2160 | gamma_exposure | 46.62 / 23.63 ms | 2.14 ms |
| 3840 x 2160 | alpha | 85.01 / 18.11 ms | 2.37 ms |
| 3840 x 2160 | normal | 80.70 / 26.79 ms | 2.37 ms |

GPU bytes match the native CPU reference within one byte in these cases; CPU
alpha/normal output matches exactly. The opaque calibration alpha case has the
same intended output before/after. A separate independent fractional-coverage
fixture proves the alpha bug fix. The alpha display had incorrectly replaced
coverage with one when selecting alpha disabled color processing. Coverage is
now read independently, without applying OCIO, exposure or gamma to it.

The direct CPU path packs scalar/alpha once per pixel and keeps vector data
opaque. Raw color retains unpremultiplication, zero-coverage additive RGB and
existing exposure/gamma semantics. Cryptomatte retains ID/coverage false colors;
encoded NaN bit patterns never enter OCIO. Normal view transforms still use the
existing OCIO processing path. Render buffers and written EXRs are unchanged.

Ownership checks reject writes and attempts to enable NumPy writes, discard the
renderer and source capsules, allocate other frames, then verify retained values
from another thread. Output storage checks retain allocations after producer
deletion and test exact odd-size expansion. Graphics lifecycle tests exercise
surface/draw failure, sticky CPU fallback and converter retirement. Live viewer
checks cover wakeups, stale-result rejection, snapshots, fullscreen and navigation
in their recorded scopes. Exact source/build snapshots and failed reference
attempts are in the
verification index (`artifacts/verification/display-assembly-20261017/index.json`).

The comparison in `artifacts/display-assembly-comparison-20261017.json` separates background conversion,
queue-to-GUI delivery and foreground assembly. Reference generation, fixture
rendering, final pixel comparisons, live scene work, Qt painting and compositor
scanout are outside those conversion samples. Seven observations do not certify
a release percentile or physical UI latency. Actual GL adapters and mixed-device
configurations still require separate certification.

## Diagnostic surfaces and mesh-light cost - October 17, 2026

Host `20261017T192527960` sets the sampling method of integration-owned diagnostic surface
shaders to None. The Light Path camera branch still makes the marker visible
without lighting its surroundings. Cycles' conservative emission estimate sums
both branches of a linked closure mix, so previously it registered these
non-illuminating surfaces as mesh lights. Ordinary repaired shaders restore Auto;
retired slots also reset that policy before reuse. No engine patch, protocol,
device default or general emission setting changes in this increment.

The same one-worker/one-viewer heavy fixture uses 16 instances of a 1,036,800-
triangle prototype, 960 x 540, 32 samples, three motion samples and no denoising.
A native unsupported Principled assignment creates one persistent source warning.
Each action has five scripted HOM parameter edits; timing ends at newer nonuniform
actual Qt canvas pixels. The source warning persists throughout.

| Action | Median before / after | Observed p95 after |
| --- | ---: | ---: |
| camera | 66.0 / 65.8 ms | 76.6 ms |
| transform | 1391.9 / 60.8 ms | 104.7 ms |
| camera-during-geometry | 92.1 / 86.6 ms | 89.9 ms |
| transform-during-geometry | 3130.5 / 73.4 ms | 139.3 ms |

All ten overlap paints in the fixed run arrive during heavy capture. No positive
copy requests for `light_tree_nodes`, `light_tree_emitters` or `triangle_to_tree`
remain in its case diagnostics. Before the change, a pose-only edit requested
165,890,480 bytes of light-tree nodes and 33,178,112 bytes of emitters, plus
4,147,200 bytes of triangle lookup data; light synchronization took 1.326 s.
These counters describe logical host-backed requests, not physical PCIe traffic
or VRAM residency. Seven-state CPU/both-GPU tests independently verify unchanged
camera visibility, no neighbor illumination, restored real-emitter lighting,
same-identity changes, shader pooling, normal/depth passes and tracked closure.

This also isolates a remaining genuine-emitter bottleneck. In the explicit
emission fixture, a pose-only edit spends 1.340 s in `light/device_update` out of
1.341 s of scene device update. It uploads no `tri_verts`, `tri_vindex` or
`tri_shader` data. The native object preview commits in roughly 1 ms; the sampling
structure rebuild then delays its image. The matched diagnostic setting change
from Light Tree On to Off gives median object paints of 1.365 s / 0.324 s, and
2.796 s / 0.331 s during geometry work. This changes the sampling strategy, so it
is not an equal-work speedup claim. Production defaults remain unchanged.

The probe now records settled per-case engine timings and attributes a new image
to the first commit of its physical generation. Later exact no-op acknowledgements
can share that generation; the old report could incorrectly label an object
preview as a full scene commit. This corrects the previous plain-emission
attribution without changing the stored image times. During heavy capture, the
settled engine diagnostics include later geometry work and must not be read as
the cost of the earlier camera preview alone.

Source snapshots, reports and failed attempts are retained in the
verification index (`artifacts/verification/emissive-preview-20261017/index.json`).
The new resource assertion fails on the old host, passes after the fix, and
restores real lighting after repair. A fixture input-key correction retains the
independent expected radiance; the retirement test now initializes its lazy
worker before measuring default shader counts. These are test repairs, not
relaxed renderer ownership contracts. Exact timing rows are in
`artifacts/diagnostic-sampling-comparison-20261017.json`. No physical mouse/compositor percentile, physical-memory
plateau or direct Blender/Redshift comparison is certified here.

Next engine work should retain mesh-local sampling hierarchies across compatible
transforms, with explicit invalidation for geometry, emission estimates, shader
palettes, transformed geometry, orientation, visibility, linking and retirement.
Large fresh instance expansion and 1080p/4K display-copy measurements remain
separate integration priorities.

## Stable packed resources and automatic IPR reuse - October 17, 2026

Host `20261017T190144231` retains accepted packed deformation resource slots across
source detail recreation when the complete nominal/shutter profile and rendering
state remain compatible. Split profiles get separate resources; merging retires
the redundant slot. Material palettes, required fields, smoothing, subdivision
and motion semantics remain part of compatibility. This does not share GPU
geometry across incompatible shader palettes or stabilize every static/velocity
resource. Shutter pose tables now retain matrices instead of complete objects.

The capture/encoding probe ran on host `20261017T184302680`; its native C++ source,
engine revisions, patches, features and kernels match the current host. It uses
1,000, 10,000 and 100,000 instances with small and heavy shared prototypes. At
100,000 instances:

| Prototype edit | Encoded delta before / after | Encoding before / after | Capture before / after |
| --- | ---: | ---: | ---: |
| Four vertices, deformation | 30,816,833 / 38,974 bytes | 68.89 / 0.033 ms | 1.555 / 1.434 s |
| 1,036,800 triangles, deformation | 61,972,541 / 31,194,682 bytes | 109.34 / 10.38 ms | 2.900 / 2.164 s |
| 1,036,800 triangles, prototype | 61,972,541 / 31,194,682 bytes | 120.50 / 10.07 ms | 2.870 / 2.246 s |

Each changed shape produces one mesh upsert and zero object binding/pose
upserts or geometry deletions. These are unrepeated phase observations and
exclude worker transport, engine synchronization, GPU upload/BVH, rendering and
presentation. They are not a universal capture speedup: the small prototype
edit changes from 1.598 s to 1.854 s. Fresh conversion still builds substantial
instance/profile tables. Exact rows are in
`artifacts/packed-resource-slots-comparison-20261017.json`.

Automatic Cycles Viewer captures and SOHO updates now request exact unchanged
scene reuse. An already accepted navigation preview is not reset merely because
its full capture finishes later. Manual Render/Refresh still forces a restart,
including when it follows a queued automatic request. Real scene, resource or
working-color changes retain their existing invalidation behavior.

The paint probe uses 960 x 540, 32 samples, no denoising and 16 instances of a
1,036,800-triangle prototype, with three motion samples. This is not the defined
one-million-total-triangle release fixture. Each action has five repetitions,
one worker and one Cycles viewer alongside Houdini's Scene Viewer. HOM parameter
edits start timing; a newer generation/revision with nonuniform actual Qt image
pixels ends it. The interval includes host work, synchronization and canvas
painting, but not a physical mouse event or compositor scanout.

| Material | Action | Median before / after | Observed p95 after |
| --- | --- | ---: | ---: |
| diffuse | camera | 72.4 / 69.0 ms | 77.9 ms |
| diffuse | transform | 60.4 / 57.2 ms | 100.3 ms |
| diffuse | camera-during-geometry | 79.7 / 77.7 ms | 81.7 ms |
| diffuse | transform-during-geometry | 65.8 / 67.5 ms | 87.7 ms |
| emission | camera | 71.8 / 65.1 ms | 68.2 ms |
| emission | transform | 1718.1 / 1376.8 ms | 1409.8 ms |
| emission | camera-during-geometry | 85.1 / 83.9 ms | 85.9 ms |
| emission | transform-during-geometry | 3718.0 / 2938.6 ms | 2962.8 ms |

All ten diffuse overlap paints arrive while heavy capture remains pending.
Camera previews also remain prompt in the emission case, but emissive object
movement waits for a full scene commit and remains much slower. Eliminating
redundant automatic resets helps those observations but does not solve that
workload. Mesh-light synchronization needs separate profiling; these numbers do
not attribute all remaining time to a Cycles kernel. Five observations are too
few to certify a release percentile. Full reports and fixture details are in
`artifacts/packed-preview-reuse-comparison-20261017.json`.

CPU/4090 tests verify unchanged reuse, manual Refresh precedence and retained
render mode. The native IPR world lifecycle passes. The color lifecycle fixture
now supplies explicit UVs at its actual render source, uses interactive tracing
settings explicitly and waits for asynchronous worker retirement. Missing-UV
recovery had correctly produced magenta; the old test waited for a texture color
instead, then checked memory before shutdown completed. Repaired CPU/4090 color
reload/pause/restart checks pass with eight stages and zero tracked geometry or
device allocations after closure. Failed fixture attempts remain archived.

Resource split/edit/merge/deletion contracts, independent shutter/vector images,
material/displacement/local recovery, heavy live edits and five-frame dual-GPU
output pass in the scopes recorded by the
verification index (`artifacts/verification/packed-resource-slots-20261017/index.json`).
Physical UI latency, 1080p/4K copy profiling, memory plateau and full device/release
certification remain outstanding.

## Compact packed deformation capture - October 17, 2026

Host `20261017T181711359` captures shared prototype/binding templates, short placement
arrays, immutable component selections and packed populations. Per-instance
nominal/shutter object tables and component expansion now run during owned
background conversion. Multiple logical OBJ requests of one SOP retain the
previous reconciliation path. File-backed and unknown packed sources are still
reacquired. No live SOP evaluation crosses the ownership boundary.

The four-vertex prototype probe uses 1,000, 10,000 and 100,000 instances:

| Instances | Preparation before / after | Background conversion before / after |
| ---: | ---: | ---: |
| 1,000 | 7.27 / 1.48 ms | 4.50 / 8.66 ms |
| 10,000 | 94.55 / 11.63 ms | 60.16 / 104.15 ms |
| 100,000 | 1268.74 / 107.88 ms | 718.69 / 1404.76 ms |

The 100,000-instance deformation-edit total capture changes from 2.030 s to
1.555 s. Background conversion now includes the expanded object tables. This
is one observation per workload, with no percentile or rendering claim.

A second probe uses a 1,036,800-triangle shared prototype. Homogeneous geometry
classification can share immutable metadata only for matching valid internal
topology/primitive data IDs and counts. Mixed-detail offsets and unsupported
primitive examples are never transplanted. Supplied deformation selections
remain tied to preserved source data. This follows the
[SideFX data-ID contract](https://www.sidefx.com/docs/hdk/_h_d_k__geometry__intro.html),
and does not require artist-authored identity attributes.

| Instances | Host phases | Explicit SOP cook contexts | Remaining host phases |
| ---: | ---: | ---: | ---: |
| 1,000 | 9.1 ms | 5.0 ms | 4.1 ms |
| 10,000 | 24.9 ms | 7.9 ms | 16.9 ms |
| 100,000 | 179.2 ms | 32.4 ms | 146.8 ms |

At 100,000 instances, heavy deformation preparation changes from 248.19 ms to
131.61 ms. Total capture in those two observations changes from 2.679 s to
2.900 s; background conversion remains substantial and varies. The cook timer
measures explicit SOP cook contexts, not all possible Houdini callbacks or
procedural I/O. The remaining-host column is their subtraction from measured
host API phases; scan/background/encoding phases remain separate in the report.

Twenty independent shutter-profile/vector image checks pass on CPU, RTX 4090
and RTX 3080 Ti. CPU/4090 mixed mesh/hair/point/volume checks preserve ownership
rejection and full cache retirement. Heavy live-viewer break/repair and
navigation checks pass on all three devices, with five heavy frames also
passing on both GPUs together. Concurrent source edit/deletion, displacement,
material, attribute and file-backed regressions remain passing. Reports and
exact sources are in the verification index (`artifacts/verification/packed-deformation-leaves-20261017/index.json`).

These scripted SDK/Qt development checks do not certify physical mouse/display
latency, actual presented-image p95, GPU residency or a physical-memory plateau.
Phase evidence excludes worker transport, engine synchronization, GPU upload,
BVH, rendering and presentation. Exact comparisons are in
`artifacts/packed-deformation-leaves-comparison-20261017.json`. Large fresh scenes still spend time expanding/comparing
instance records and can resend object bindings after prototype identity changes.

## Owned packed deformation assembly - October 17, 2026

Host `20261017T173541492` acquires nominal/shutter details, packed poses, frozen material
bindings and SOP-dependent fingerprints in Houdini's supported cook context.
Population correspondence, local/world motion tables, complete-profile grouping,
geometry remapping and component deduplication now run during owned conversion.
Only numeric/value metadata and preserved geometry cross that boundary; no live
SOP pointer or OP evaluation is used there. The packed sample factory assembles
already acquired inputs and is explicitly safe outside the host thread.

The same 1,000/10,000/100,000-instance four-vertex prototype probe gives one
observation per workload:

| Instances | Deformation-edit host preparation before / after | Background conversion before / after |
| ---: | ---: | ---: |
| 1,000 | 11.31 / 7.27 ms | 0.64 / 4.50 ms |
| 10,000 | 166.65 / 94.55 ms | 16.10 / 60.16 ms |
| 100,000 | 2200.52 / 1268.74 ms | 170.81 / 718.69 ms |

At 100,000 instances, deformation-edit total capture changes from
2.416 s to 2.030 s. The conversion column now
includes work formerly performed on the host; it must not be read as slower
geometry translation alone. Host acquisition still builds per-instance nominal
and shutter object tables, so this remains substantial main-thread work.
The next step is compact owned leaf batches that expand those tables after
handoff, while preserving recovery, properties and complete shader profiles.

CPU/RTX 4090 analytic images, five-frame ROP output, material/subdivision and
displacement variants, malformed-attribute repair, file-backed recovery,
concurrent source edits/deletion, one-viewer heavy edits and a five-frame
dual-GPU courtyard run are retained in the
verification index (`artifacts/verification/packed-deformation-assembly-20261017/index.json`).
These are development checks. Phase timings exclude rendering, device upload,
transport, display and p95/physical UI certification. Exact observations are in
`artifacts/packed-deformation-assembly-comparison-20261017.json`.

## Packed deformation preparation - October 17, 2026

Host `20261017T171753783` keeps unchanged in-memory packed deformation recipes and their
owned local shutter poses. The host verifies SOP/dependency versions, request
semantics, native material identities and each component's shader field demand.
The converter rebuilds instance transforms/aliases and compares them against the
accepted scene after handoff. No live OP or SOP handle is retained in this cache.
Changed captures resolve a geometry/material plan once per distinct prototype
and effective binding at each shutter visit; immutable sample requests and the
shutter window are shared across their instances.

`benchmark_packed_deformation_capture.py` uses the same acquisition, scan,
frontier, preparation, conversion, finalization and encoding phases as the
velocity probe, with 1,000/10,000/100,000 copies of a four-vertex deforming plane.
One observation per workload, without an engine:

| Instances | Warm host preparation before / after | OBJ host preparation before / after |
| ---: | ---: | ---: |
| 1,000 | 7.57 / 0.10 ms | 8.33 / 0.10 ms |
| 10,000 | 163.19 / 0.14 ms | 180.62 / 0.13 ms |
| 100,000 | 2179.02 / 0.19 ms | 2278.48 / 0.17 ms |

At 100,000 instances, warm total capture changes from
2.325 s to 0.375 s.
Cold host preparation changes from 2.886 s to
2.025 s; a deformation edit changes from
2.983 s to 2.201 s.
The changed path still blocks the host for substantial work. Per-instance
correspondence/profile assembly during fresh shutter acquisition remains a
priority; this increment does not solve every geometry-edit stall.

The shortcut applies to one logical OBJ request of a SOP and known in-memory
packed implementations. Several OBJ requests sharing a SOP retain the existing
binding reconciliation, including compatible material reassignment without
additional shutter cooks. File-backed and unknown packed procedurals are
reacquired because ordinary SOP tokens cannot certify external content changes.
New shader fields, broken/recreated material assignments, source edits and
topology changes invalidate the relevant state. Cached recovery diagnostics
remain present and clear after repair; source removal retires its aliases.

These are unrepeated phase observations, not image latency, UI event latency,
GPU upload, transport/TLAS, physical memory or release-budget certification.
Scanning/conversion run in the background in artist IPR; acquisition,
frontier resolution, preparation and finalization still have host work. Exact
observations are in `artifacts/packed-deformation-capture-comparison-20261017.json`
and the source/test archive (`artifacts/verification/packed-deformation-performance-20261017/index.json`).

## Packed velocity preparation - October 17, 2026

Packed velocity leaf expansion now uses the existing owned background conversion
boundary. Host work retains packed transforms, point velocities, identities,
bindings and prototype handles; object/alias/motion table assembly uses those
owned values after handoff. Nested parent composition remains explicit and
unrepresentable motion recovers before engine validation. No renderer, kernel
or worker protocol change is required.

`benchmark_packed_velocity_capture.py` profiles 1,000/10,000/100,000 instances with
one shared four-vertex prototype and no engine. One observation per workload:

| Instances | Velocity-edit host preparation before | After |
| ---: | ---: | ---: |
| 1,000 | 5.03 ms | 0.30 ms |
| 10,000 | 86.75 ms | 2.96 ms |
| 100,000 | 1,279.32 ms | 34.01 ms |

At 100,000 instances, total capture changes from 1.498 s to 0.336 s. OBJ edits
change from 1.329 s to 0.029 s in preparation and 1.547 s to 0.283 s in total
capture. Camera-only preparation is 6.18 ms afterward. The velocity/OBJ packet
remains 26,627,467 bytes; this does not measure faster IPC or GPU synchronization.
Exact observations, phase boundaries and source references are retained in
`artifacts/packed-velocity-capture-comparison-20261017.json` and the
verification index (`artifacts/verification/packed-velocity-20261017/index.json`).
The first profile's structural assertions passed, but its wrapper rejected the
nonstandard result label `measured`; its exact sources/input hashes are retained
separately. The subsequent profile uses `passed` only for inventory/no-cook
assertions. Timing values have no acceptance threshold in this diagnostic test.

Acquisition, packed-frontier resolution and finalization still run on the host;
the preparation column is only one part of possible UI stalls. Scanning and
conversion have separate measurements and run off-thread in artist IPR. The
profile excludes transport, engine synchronization, BVH/subdivision work, GPU
rendering, display and GUI paint. It has no repetitions/percentiles and cannot
certify the release response budgets. Large deformation-mode instance tables,
transport/TLAS cost and long-run physical memory remain separate gates.

## Native mouse gestures and fresher prepared poses - October 17, 2026

Host `20261017T094546441` uses the same engine revision, 32 patches and six GPU
kernels, with one persistent worker. The functional change refreshes completed
OBJ-pose preparation while presentation of the preceding image is pending.
It never restarts running preparation or discards a complete geometry snapshot.
Once feedback opens, the prepared update gets its turn. Snapshot retirement
stays off the GUI thread and outside the GUI status lock.

The calibrated `houdini_viewport_drag_benchmark.py` uses native `objview` camera
navigation and the native `geo` translate handle. A producer posts 24 mouse moves
at 40 ms intervals per gesture, with two gestures for each kind/state. No HOM
camera/object parameter writes occur during a measured drag. Alternating gesture
direction keeps the handle in view. An application event filter measures delivery
before Houdini consumes native input; a widget filter missed those events.

The fixed fixture is the mixed courtyard, adaptive turbine, displaced ground and
a shared 1,036,800-triangle packed prototype. Unique mesh inventory is about
1.98 million triangles. Packed viewport LOD is Bounding Box, while the renderer
uses full geometry. The Scene Viewer and one Cycles panel are visible without
overlap. Settings are 960x540, no denoising, no motion blur and 65,536 sample limit.
Exact RTX 4090 OptiX selection is checked against session status.

First changed-pose Qt paint during held native drags, median milliseconds:

| Instances | Whole-group move before | After | Independent repeat | Camera before / after |
| --- | ---: | ---: | ---: | ---: |
| 1,000 | 208.3 | 146.1 | - | 88.6 / 94.6 |
| 10,000 | 225.5 | 154.5 | - | 93.7 / 100.3 |
| 100,000 | 662.0 | 444.4 | 483.9 | 114.8 / 113.0 |

With geometry preparation held pending, corresponding whole-group medians are
233.6 -> 166.4 ms, 207.6 -> 163.3 ms and 505.6 -> 385.7 ms. The independent 100k
repeat is 419.8 ms. This is an independence/control test, not a measurement of
CPU contention from actively running conversion. The separate continuous-100k
test verifies repeated changed-geometry images before movement ends.

Correlation uses acquired world poses, prepared-root revisions, accepted physical
render revisions and actual canvas paint completion. A newer logical submission
can carry an older prepared object revision; it must not be attributed to the
newest object acquisition. Native Translate notifications can precede invalidation
of Houdini's evaluated matrix cache. For the unparented, identity-basis test OBJ,
the expected translated matrix is therefore derived from the notified values.
Identical poses within 1e-7 and later refinement paints are not extra first-image
observations. Old relabelled frames cannot satisfy the GPU in-drag image check.

`scripts/analyze-viewport-drags.py` validates the current source manifest and saves
the exact sources, raw report and derived stage statistics. Run it immediately
after the benchmark, before editing or restaging.
Pass `--manifest` with the absolute path to the exact build manifest recorded by
the report; the analyzer has no implicit SDK/build-directory fallback.
The raw reports also retain
producer lateness, deadline-to-Qt delivery, acquisition, preparation, transport,
engine request costs and image sizes/samples. Latest-delivered-input ages are
freshness observations, not causal input latency: Houdini may queue its own
native actions after Qt delivery.

The final evidence has four drag executions (including the independent 100k
repeat) and twelve focused regressions. Five animated frames with 1k packed
instances, a changing prototype, a deforming adaptive turbine and five different
true displacement maps complete in 12.966 s including output and observation;
per-frame output-inclusive times are 4.901, 1.901, 1.938, 1.936 and 1.993 s.
They retain one worker. This is a sequence correctness/throughput check, not a
Blender comparison or final-ROP speed claim.

Source versions, complete distributions, small sample counts, actual settings and
the corrected animation report are bound by
`artifacts/verification/native-drag-poses-20261017/index.json`.
Only displayed poses are latency samples; coalesced intermediate inputs are not.
The two short gestures per case, especially the 5-6 displayed 100k object poses,
do not certify production p95 budgets. Physical input/display latency and viewport
GPU completion remain unmeasured; both desktop capture methods have the documented
stock-Houdini limitation. CPU/RTX 4090 regression coverage is targeted. Additional
devices, physical memory residency and Blender/Redshift parity remain uncertified.

## Early object previews measured at actual paint - October 17, 2026

Host `20261017T070257430` retains one persistent native IPR worker, protocol 33, the same
32 engine patches and six unchanged GPU kernels. Restart Houdini for this runtime.

The unchanged courtyard HIP/OCIO assets, million-triangle shared prototype and
100k packed instances are exercised through twelve actual OBJ Translate edits at
960x540 on RTX 4090 OptiX. Driver 616.92, compiler, dependencies, patches, kernels,
render settings and one Cycles viewer are retained. Timing records actual node
notifications, acquisition, worker decode, background conversion and Qt paint.
Retained old physical frames cannot count as previews. The trace fixture now
classifies pose-preview headers separately from complete source captures.

Stage medians, milliseconds:

| Observation | Before | Early poses | Independent repeat |
| --- | ---: | ---: | ---: |
| Notification to acquisition | 148.202 | 3.269 | 3.627 |
| Header acquisition | 1.687 | 0.098 | 0.085 |
| Header/pose enqueue | 33.491 | 0.186 | 0.199 |
| First decoded frame to paint | 8.714 | 8.156 | 7.141 |
| Input to actual paint | 548.349 | 389.662 | 344.400 |

Maximum input-to-paint is 473.4 ms in the first run
and 405.2 ms in the repeat.
The moved-object image uses retained geometry while the debounced source update
remains pending; it is not a completed-geometry or final-render measurement.
Header acquisition avoids explicit SOP/material/texture traversal. Transform
expressions may still evaluate their host dependencies on the supported callback.

Separate twelve-edit camera observations, milliseconds:

| Resolution | Input-to-paint median | Maximum |
| --- | ---: | ---: |
| 960x540 | 133.655 | 149.049 |
| 1920x1080 | 186.022 | 259.537 |
| 3840x2160 | 290.505 | 480.329 |

These camera observations are not an all-resolution speedup claim. The 1080p/4K
delivery variation and remaining presentation costs still need profiling.

The 85 targeted regressions include CPU/OptiX packed/mixed fresh renders, held
acquisition/scan/conversion, camera/adaptive subdivision, resource recovery,
continuous 1k/100k updates, 1k/10k/100k heavy workflows and five animated frames.
Camera delivery is checked separately at 960x540, 1080p and 4K. The exact build,
source archive, test hashes, raw records and hardware observation are linked in
`artifacts/verification/early-poses-20261017-final/index.json`.

These twelve-edit repetitions are development observations. Event-loop timing is
not a physical viewport navigation test, and viewport visibility/occlusion and GPU
contention are not certified here. No production p95, other-device, memory-residency
or Blender/Redshift parity claim follows. O(N) pose preparation/serialization and
engine/BVH synchronization remain response costs after the host dispatch gain.

## Core update attribution and retained-command ownership - October 17, 2026

Host `20261017T054241662` retains one persistent native IPR worker, protocol 33, the same
32 engine patches and six unchanged GPU kernels. Restart Houdini for this runtime.

Fine-grained opt-in traces attribute owned update work after the session control
locks. The same sequential 100k-instance courtyard fixture, hardware, driver,
compiler, dependencies, patches, kernels, image settings and workload code are
retained. One viewport and one Cycles viewer remain open. Both profiled runs
enable the same request and core trace probes. No cross-process clock subtraction
is used. Three trials per edit remain development observations.

Core stage medians for a complete 100k-group pose command, milliseconds:

| Stage | Before | Current |
| --- | ---: | ---: |
| Candidate and binding preparation | 10.304 | 0.043 |
| Validation | 54.800 | 25.156 |
| Variant dependency checks | 14.114 | 15.132 |
| Pose mutation | 20.974 | 21.523 |
| Displacement-motion recovery | 27.374 | 0.000 |
| Candidate retirement | 3.825 | 0.014 |

Core trace total median changes from 133.769 to 64.673 ms.
Stage medians are not additive. Core trace totals exclude acquiring the session
locks, decoding input, host capture/conversion and asynchronous device preparation;
the request profile separately includes the complete worker apply call.

Ordered pose commands prove key uniqueness through adjacent comparisons and resolve
their existing targets in the same linear walk. Unsorted commands retain full
duplicate checks. Empty conflict sets no longer hash every instance path. Finite,
invertible transform checks, shutter contracts, target ownership and conflicting
upsert/deletion rejection all remain mandatory.

Ordinary commands are read immutably for the duration of synchronous apply and its
completed boundary callback. Accepted resources copy values or retain owned
handles. Material-bearing commands retain a private candidate for auxiliary
displacement-shader expansion; caller graphs are never modified. This removes
the redundant pose-vector/string copy and its retirement, without changing wire
storage or claiming zero-copy or physical-memory certification.

Pose-only updates with blur already disabled and no pending variant/dependency
change skip displacement-motion recovery.
Blur/vector-pass transitions, active blur, geometry/material/variant changes and
retirement retain the recovery path. Required world displacement and adaptive
subdivision behavior remains unchanged and independently image checked.

Input to first actual paint, medians in milliseconds:

| Edit | Before, profiled | Current, profiled | Normal | Normal repeat |
| --- | ---: | ---: | ---: | ---: |
| Camera | 133.7 | 157.6 | 140.2 | 132.7 |
| Whole packed group | 572.9 | 582.0 | 566.2 | 542.5 |
| Prototype deformation | 533.5 | 608.0 | 582.8 | 566.0 |
| Prototype topology | 692.6 | 685.4 | 725.9 | 710.0 |

The internal processing reduction must not be described as an equivalent visible
latency improvement. These runs retain host notification/idle dispatch, engine
updates, polling, display and actual paint costs. Heavy geometry previews can use
accepted geometry with newer poses while source preparation remains pending.
Camera-only edits still avoid geometry conversion/upload. Normal runs disable
both core and worker tracing. No p95, Blender/Redshift, UI-stutter, other-device or
full production certification follows from these observations.

The extended twelve-edit object trace measures node notification to capture at
148.202 ms median,
capture at 1.687 ms, and first decoded
frame to paint at 8.714 ms.
Total input-to-paint is 548.349 ms median.
The following UI tick takes 1.093 ms;
this is an event-loop observation, not physical viewport/GPU contention certification.
The test rejects retained old physical frames, tracks actual node notifications,
and fingerprints its HIP/OCIO files before/after timing. Camera mode is rerun at
960x540, 1080p and 4K with the same enhanced tracing.

Source inspection finds an 80 ms object/general-edit debounce followed by host-safe
callbacks; the measured delay includes both. Camera-only navigation has an
immediate guarded route. Next work should separate early owned pose previews from
debounced geometry capture, preserving source generations, pending geometry,
dependency correctness, adaptive-dicing deferral and host-safe cooking. A generic
zero-delay capture timer would not establish those guarantees.

`artifacts/verification/object-updates-20261017-final/index.json` binds 81 targeted
native/display/output/displacement regressions, three camera-delivery runs, twelve
object delivery traces, matched
packed observations and raw phase traces to exact sources, compiled artifacts,
unchanged patches/kernels and preceding evidence. The enabled-probe CPU contract
is additional. Core checks cover 26 safe rejections, 12 fresh comparisons, caller
immutability and ordered/unsorted batches. Live CPU/OptiX mixed packed edits,
recovery, continuous 100k drags, adaptive camera work and five-frame outputs pass.

Next priorities are actual object notification/presentation attribution, remaining
O(N) host pose tables and engine object/BVH work, native viewport/GPU contention,
p95 and physical-memory certification, other devices, and remaining native
resources/cameras/operations/release work. Earlier sections retain their original
build and measurement context.

## Transform-only packed updates - October 17, 2026

Host `20261017T050102773` uses one persistent native IPR worker, protocol 33 and the same
32 engine patches and six GPU kernels. Restart Houdini for this runtime.

The matched scene contains 100,000 instances of one 1,036,800-triangle native Grid
prototype plus 950,906 courtyard triangles, points, curves and three VDB fields.
RTX 4090 OptiX, 960x540, 65,536 maximum samples; motion, denoising and adaptive
sampling off. One Cycles viewer and one SOP viewport are open. Thirty actual HOM
parameter edits include six camera/object input pairs while heavy geometry is
prepared. Every run is sequential. Compiler, host SDK, dependencies, kernels,
engine patches, driver, scene inventory and settings match the retained baseline.

Input to first actual Qt paint, medians in milliseconds, three trials per edit:

| Edit | Baseline profile / optimized profile / normal / normal repeat |
| --- | ---: |
| Camera | 126.7 / 123.3 / 136.2 / 141.3 |
| Whole packed group | 897.3 / 547.2 / 569.4 / 574.8 |
| Prototype deformation | 552.7 / 555.9 / 584.5 / 561.5 |
| Prototype topology | 652.3 / 680.7 / 694.2 / 633.1 |

The two profiled runs use identical opt-in request measurements. Normal runs
disable worker profiling. These are development observations, not randomized p95,
Blender/Redshift comparisons or a renderer-only UI-stutter measurement. Source
cooking and actual paint are recorded; existing image/conversion diagnostics
remain available. During heavy geometry work the first preview can use accepted
geometry with newer poses; completed-geometry presentation is recorded separately.
Earlier checkpoint numbers retain their original build context.

For the complete 100k-object move, shared-memory command payload drops from
33,316,372 to 11,327,490 bytes.
Median owned preparation changes from 144.28 to
72.63 ms, host encoding from 105.53
to 28.47 ms, worker decoding from 38.66
to 9.30 ms and worker update from
312.30 to 131.94 ms. The latter includes
validation and engine synchronization. Stage medians must not be added to infer
a median total. The final request log proves 100,000 poses and zero repeated full
object records per transform. These are host/worker bytes, not GPU-transfer bytes.

Transform-only differences preserve geometry bindings, visibility, random IDs,
Cryptomatte names and object settings. Sorted immutable snapshots, differences,
navigation origins and resolved engine records use linear walks; each retained
object definition is copied once rather than cloned and replaced. Matrix wire
I/O is bulk; an equal nominal transform is represented by an explicit lossless
tag. Binding/property changes still send complete definitions. The core still
validates ownership, transforms, shutter samples and conflicting commands before
mutation. Artist `id`/`name` requirements are unchanged: no new attributes needed.

Pose edits skip mesh-variant regrouping when displacement and dicing do not depend
on the changed transform. World-dependent displacement, negative orientation,
pixel-adaptive subdivision and sampled displacement retain required preparation.
Ten CPU/OptiX variant cases match fresh renders and analytic position values.
An actual viewer test edits mixed packed pigs, hair and points, properties,
membership and prototypes, and matches a fresh capture after worker shutdown.
Core contracts cover 100k records, 22 strict rejections and eight fresh images.

`artifacts/verification/object-poses-20261017-final/index.json` binds 80 targeted
regressions, matched observations, request profiles, exact sources/build hashes,
unchanged kernels/patches and correction history. It includes temporal/attribute
displacement, live recovery, native display, Cryptomatte, continuous 100k drags,
camera-first held work, compact 4K delivery and five-frame output checks. The
initial duplicated-nominal transport failures remain recorded; their strict
payload reduction assertion was preserved.

Whole-group snapshots and upstream object/BVH updates remain O(N). These three-
trial heavy-scene observations do not certify the <500 ms prospective p95 budget
on the defined one-million-triangle fixture. Root affine batching or persistent
pose layers require a separate correctness/performance
study. Changed-prototype synchronization, native viewport/GPU contention,
long-run physical memory, RTX 3080 Ti/mixed devices and remaining production
features remain open. No universal response or memory certification is claimed.

## Compact progressive transport and conversion - October 17, 2026

Host `20261017T040304102` uses one persistent native IPR worker, protocol 32 and 32
engine patches. The preceding 31 patches and all six GPU kernels are unchanged.
Earlier sections record their dated builds. Restart Houdini for this runtime.

The matched fixture remains one Cycles viewer and one SOP viewport, 100k instances
of a 1,036,800-triangle prototype, 950,906 courtyard triangles, points, curves and
three VDB fields. RTX 4090 OptiX, 65,536 maximum samples, motion/denoising/adaptive
sampling off; hardware, driver, compiler, dependencies, kernels and scene settings
match the preceding observation. Jobs run sequentially. Twelve camera edits per
run end at actual paint, using one host monotonic clock.

Before / compact-preview medians in milliseconds; stage medians must not be summed.

| Target image | Input to first paint | Display conversion | First decoded frame to paint | Current GUI merge |
| --- | ---: | ---: | ---: | ---: |
| 960x540 | 151.8 / 105.7 | 5.69 / 3.00 | 60.37 / 5.59 | 0.19 |
| 1920x1080 | 231.5 / 179.8 | 17.44 / 11.45 | 24.91 / 15.73 | 0.68 |
| 3840x2160 | 533.0 / 203.6 | 99.79 / 17.28 | 119.20 / 25.83 | 2.57 |

A second current 4K run has a 216.8 ms input-to-paint median over twelve edits.
These are repeated development observations, not randomized p95 certification or
a Blender/Redshift comparison. The early image may have reduced rendered
resolution. Host notification, render scheduling and synchronization still matter.

For a 1280x720 preview covering a 3840x2160 image, float beauty storage is 14.1 MiB
instead of the previous 126.6 MiB: nine times smaller. The wire includes metadata
in addition to those pixels. The recorded first-frame storage and packet byte
counts show what was actually transported on each edit. GPU OCIO conversion and
readback also use compact dimensions; exact nearest RGBA8 expansion runs in the
background. The final GUI raster is still full sized. This is not zero-copy or
direct GPU sharing, and no total physical-memory improvement is certified.

Per-plane dimensions avoid assuming that noisy, denoised and data buffers share
one effective resolution. Complete output planes retain full storage. Independent
odd/asymmetric fixtures check crop placement, orientation, alpha, data and exact
Cryptomatte bits. Real CPU/OptiX progressive passes, native display, output-color,
motion-vector and five-frame tests accompany the performance runs. Camera-only
measurements assert no geometry conversion or geometry/attribute upload, and no
ordinary cancellation. Light-tree object metadata may still upload.

`artifacts/verification/compact-preview-20261017-final/index.json` binds 66 current
regression wrappers and four current camera observations to their exact runtime.
The prior milestone and an intermediate 4K observation are retained separately.
The compile fixture include correction, stale pass expectation, runner selection,
and actual frame-revisit defect are recorded without overwriting their history.

Remaining work includes native viewport/GPU contention attribution, p95 budgets,
100k whole-group transform packets, changed-geometry synchronization, long-run
physical memory, RTX 3080 Ti/mixed-device testing and native production features.

## Viewer background handoff and actual paint - October 17, 2026

Host `20261017T025024087` uses the matched 100k-instance/million-triangle-prototype courtyard,
one SOP viewport and one Cycles viewer, RTX 4090 OptiX, 65,536 maximum samples,
motion/denoising/adaptive sampling off. Hardware, compiler, dependencies, scene
inventory and render settings match the earlier observations. All timing endpoints
below use the same host monotonic clock; worker-relative publication time is not
subtracted from host time. Twelve camera edits per run are traced through capture,
enqueue, frame decode/acceptance, conversion, GUI merge and actual paint. The first
available frame is recorded separately from the frame eventually painted.

Milliseconds below are medians; intervals are separate measurements and their
medians must not be summed into a median total.

| Target canvas | Accepted to conversion request | Conversion | Converted to GUI take | GUI image merge | Input to first painted preview |
| --- | ---: | ---: | ---: | ---: | ---: |
| 960x540 | 0.11 | 5.69 | 29.00 | 0.24 | 151.8 |
| 1920x1080 | 0.78 | 17.44 | 0.59 | 0.80 | 231.5 |
| 3840x2160 | 3.25 | 99.79 | 0.74 | 3.86 | 533.0 |

These runs establish prompt producer-side submission and safe overlap, not an
overall latency win or p95 compliance. End-to-end results vary with host input,
render/transport and GUI availability and exceed the prospective camera budget
in some cases. A counterbalanced 540p control on the preceding wakeup-only build
uses polling/wakeup/wakeup/polling, 24 edits per condition: its total medians are
147.5/143.8 ms and first-available-to-paint medians 49.9/40.9 ms. That control
isolates wakeups, not the subsequent direct background handoff.

The existing GPU OCIO converter is retained. Earlier matched CPU display controls
measure about 51/205/826 ms conversion at target 540p/1080p/4K, versus roughly
4/15/93 ms through OpenGL on the RTX 4090. Dedicated pixel tests verify exposure,
gamma, crop, alpha/data fallback, old working-space ownership and graphics failure.
This does not certify another machine's display device or predict its timings.

The pinned display driver expands reduced-resolution half-float previews into
full target-sized float RGBA planes before transport. A 4K beauty plane alone is
126.6 MiB even when its rendered preview is 1280x720 (14.1 MiB at compact size).
The current host then decodes/transfers that expanded frame and converts the
expanded plane. Compact preview storage/transport is the next justified target;
native IPR placement, crop/aspect, display color/data semantics, final output and
independent pixel tests must remain correct. GPU sharing is still unimplemented.

`artifacts/verification/background-display-20261017-final/index.json` records 46
targeted regressions, three current observations, the six earlier CPU/OpenGL
controls, four wakeup control runs and three intermediate handoff observations,
exact source archives and failures. Setup corrections cover inherited/empty
instance overrides, early lifecycle assertions, reference renderer choice, the
retired profile parameter and expected revision metadata. Source/runtime hashes
bind every observation to its actual build. Normal operation emits no worker
profiling records.

Session-recreation feedback/focus coverage, long-run physical memory,
viewport-only/GPU contention attribution, other devices,
100k group-move latency and production response budgets remain open.

## Incremental binding validation and request profiling - October 17, 2026

Host `20261017T014902203` keeps protocol 31 and the same patches/kernels. The fixture
contains a shared 1,036,800-triangle native prototype plus 950,906 courtyard
triangles, 1,200 points, 605 curves and three VDB fields. It uses RTX 4090 OptiX,
960x540, 65,536 maximum samples, no blur/denoising/adaptive sampling, one Cycles
viewer and the native SOP viewport. Hardware is Ryzen 9 7950X, RTX 4090 24 GiB,
NVIDIA driver 616.92 and OptiX 9.1.0, as recorded in both build manifests.
Integration render jobs run sequentially;
the existing artist Houdini session remains untouched.

Before optimization, validation constructed a full map of every accepted object
binding even for camera-only updates. Candidate duplicate checks also copied
instance paths into ordered sets. Validation now checks incoming fields directly
and revisits retained references on geometry removal, using temporary immutable
key views for uniqueness and replacement/deletion membership. All existing
material, geometry, motion and reference contracts remain authoritative.

Opt-in worker records correlate by request ID with host encoding, mapping and
reply decoding. Matched profiled observations at 100k instances use three trials;
milliseconds below are **baseline / optimized medians**:

| Edit | Host encode | Worker decode | Worker apply call | Actual preview paint |
| --- | ---: | ---: | ---: | ---: |
| Camera | 0.07 / 0.08 | 0.03 / 0.02 | 81.03 / 0.44 | 190.3 / 140.2 |
| Whole packed group | 107.75 / 135.27 | 33.42 / 37.94 | 400.94 / 328.57 | 994.0 / 984.4 |

Worker apply includes validation, scene-boundary waiting and mutation. It does
not cover all later device/BVH/render work. Host exchange also includes scheduling,
input retirement and uninstrumented overhead. Per-stage medians are not disjoint
intervals and must not be summed to reconstruct a median total. Profiling writes
one timing record per apply; these observations are separate from normal-mode
regressions and are not randomized p95 comparisons.

For developer profiling, set `CYCLES_HOUDINI_PROFILE_WORKER=1` before starting the
worker. `CH_PROFILE` JSON records go to its stderr log, while stdout remains the
binary renderer protocol. Normal mode emits no such records. Bounded controller
events retain per-request encoding/exchange measurements and the worker log path.
The two observations and exact baseline source bytes live under
`artifacts/instance-profile-baseline/` and `artifacts/instance-profile-optimized-final/`.

The default-mode benchmark repeats 30 edits and six concurrent navigation pairs
at each instance count. Milliseconds are median / maximum from three repetitions:

| Instances | Edit | First preview | Updated geometry paint |
| --- | --- | ---: | ---: |
| 1,000 | Camera | 121.3 / 123.2 | 155.6 / 156.7 |
| 1,000 | Whole group | 214.0 / 245.8 | 245.4 / 261.9 |
| 1,000 | Deformation + navigation | 477.6 / 496.7 | 1348.0 / 1376.8 |
| 1,000 | Topology + navigation | 581.2 / 604.0 | 1480.2 / 1508.9 |
| 100,000 | Camera | 131.4 / 203.8 | 177.0 / 239.3 |
| 100,000 | Whole group | 949.4 / 977.1 | 981.6 / 1007.8 |
| 100,000 | Deformation + navigation | 573.9 / 577.7 | 2763.7 / 3084.8 |
| 100,000 | Topology + navigation | 693.5 / 693.9 | 2869.5 / 2931.6 |

Early navigation may retain the previous geometry. Physical render revisions,
current image ownership and full source completion are tested separately. Camera
trials perform no geometry conversion/scan and show no named triangle/curve/point
or attribute upload requests. The pinned engine still copies object primitive
offset metadata with Light Tree enabled. These are logical request observations,
not complete physical GPU transfer counters.

The group packet is still 31.8 MiB. Pure owned
rebasing, encoding, worker decoding/mutation, device work and display remain
optimization targets. Camera apply is now much smaller than total image latency;
capture, polling, color conversion and actual presentation need finer attribution.
1080p/4K display profiling and long-run physical-memory certification remain open.

`artifacts/verification/incremental-validation-20261017-final/index.json` audits 38 default-mode wrappers and the
separate before/after profiles. CPU/OptiX each reject 19 unsafe ownership candidates
in both engine modes while retaining the accepted image, and compare valid
rebind/recreate depth images with fresh sessions. Native geometry/volume repair,
data-copy/radiance checks, continuous drags, adaptive refinement, native IPR and
five-frame/batch output pass. This is not full native production, other-device or
Blender/Redshift parity certification. Earlier sections preserve prior evidence.

## Continuous-update fairness - October 17, 2026

Host `20261017T012041968` retains the previous engine patches, kernels and protocol 31.
The real SOP fixture contains a shared 1,036,800-triangle native Grid prototype,
950,906 courtyard triangles, 1,200 points, 605 curves and three VDB fields.
One Houdini viewport and one Cycles viewer use RTX 4090 OptiX at 960x540, 65,536
maximum samples, without denoising/adaptive sampling or motion blur. GPU render
tests run sequentially; an existing artist Houdini session remains untouched.

Before the change, the combined five-second object/camera drag painted no
changed-object image. Ready work was superseded by every new input. The scheduler
lets active owned work finish. Ready work follows one camera opportunity, with
one extra catch-up only while its object/geometry state remains current. Newer
object/geometry input removes that extra slot. Pending input still coalesces.
Camera/crop overlays
must match captured time/shutter context. Feedback uses actual physical revisions.

The continuous test changes native OBJ/camera parameters repeatedly and changes
the prototype's native Transform during the third phase. A painted frame must
match the worker physical revision and show increased object-update/conversion
counters. Camera-only frames cannot count as geometry feedback. Values below are
seconds from phase start; paints in the last quarter-second are excluded. The
index retains every individual paint timestamp.

| Instances | Continuous edits | Duration (s) | Inputs | Object paints: count, first-last (s) | Geometry paints (s) | Settle after release (s) |
| --- | --- | ---: | ---: | --- | --- | ---: |
| 1,000 | Objects | 5 | 38 | 24; 0.28-4.62 | - | 0.19 |
| 1,000 | Objects + camera | 5 | 33 | 15; 0.22-4.65 | - | 0.25 |
| 1,000 | Objects + camera + deformation | 8 | 51 | 6; 0.29-6.51 | 1.99, 3.45, 4.93, 6.51 | 1.66 |
| 100,000 | Objects | 5 | 28 | 4; 1.18-4.11 | - | 1.63 |
| 100,000 | Objects + camera | 5 | 27 | 3; 1.19-4.03 | - | 1.42 |
| 100,000 | Objects + camera + deformation | 8 | 33 | 3; 1.42-6.26 | 4.02, 6.26 | 3.33 |

The requested input cadence is 20 Hz, not a measured sustained host frequency.
Actual counts include native Houdini evaluation, viewport work and test overhead.
An earlier five-second deformation trial painted once; the final phase runs eight
seconds and requires at least two changed-geometry images. It establishes progress,
not a lower geometry-latency claim. Each final state verifies exact roots/context,
an unchanged rebase, no ordinary full cancellation and the same worker. Final
settling includes display/capture polling and may lag the last rendered update.

The separate three-repetition benchmark retains 30 edits and six navigation pairs
per instance count. First current preview may use retained geometry; full pixel
resolution alone does not mean geometry is current. Milliseconds below are
median / maximum, not p95:

| Instances | Edit | First preview | Updated geometry paint |
| --- | --- | ---: | ---: |
| 1,000 | Camera | 125.8 / 127.6 | 127.6 / 152.6 |
| 1,000 | Whole group | 276.4 / 277.8 | 309.3 / 314.0 |
| 1,000 | Deformation + navigation | 486.0 / 511.5 | 1320.5 / 1431.5 |
| 1,000 | Topology + navigation | 607.5 / 612.3 | 1440.0 / 1451.7 |
| 100,000 | Camera | 205.7 / 266.1 | 235.7 / 297.1 |
| 100,000 | Whole group | 1015.2 / 1045.0 | 1164.9 / 1169.1 |
| 100,000 | Deformation + navigation | 601.3 / 644.4 | 2950.2 / 2965.1 |
| 100,000 | Topology + navigation | 713.2 / 718.4 | 3147.3 / 3178.5 |

Camera-only edits still perform zero geometry scan batches/conversions; transform
edits reuse geometry. Ordinary edits do not cancel the whole session. Matching
completed captures retain their converged object preview. The five-frame native
geometry/animated displacement sequence completes in 12.664 seconds with
outputs, retaining eight resources and converting two on warm frames.

`artifacts/verification/continuous-feedback-20261017-final/index.json` audits 31 targeted wrappers and archives
the matching integration sources. CPU/OptiX each pass 31 navigation checks
(19 image references) and seven AOV-dependency/rejection checks. Controller
supersession/rejection, active engine interruption and native batch checks pass.
The interruption comparison pins Tabulated Sobol and identical settings; Automatic
intentionally resolves differently for interactive and final sessions.
Failed iterations remain archived. This is not the full native suite, a randomized
comparison, physical-memory certification, p95 acceptance, Blender parity or
3080 Ti/mixed-device certification. Remaining costs include 100k transform copies,
wire encoding/decoding and TLAS work, engine operations already in flight,
Houdini-only attribution and 1080p/4K image buffers. Earlier sections retain their
original measurements and should be read as historical checkpoints.

## Camera-first control and bounded preparation - October 17, 2026

Host `20261017T001630159` separates the small camera/crop preview from large
root-transform rebasing. One background preparation job owns frozen snapshots;
one pending complete state and replaceable navigation remain bounded. The
controller can poll/output camera frames while that job runs, reapplies the latest
camera afterward, and retains one persistent worker/GPU scene. Root revisions
prevent camera-only edits from restarting unchanged object preparation.

The benchmark keeps the preceding fixture: 1,036,800 shared-prototype triangles
plus 950,906 courtyard triangles, points, curves and VDBs; 960x540/RTX 4090 OptiX,
65,536 maximum samples, denoising/adaptive sampling and blur off. Runs are exclusive,
one viewer, 30 edits and six concurrent navigation pairs per count. Whole-group
transforms move `/obj/heavy_packed`, including every instance. These are three
repetitions per workload, with milliseconds shown as median / maximum:

| Instances | Workload | First current navigation/image | Updated geometry paint |
| --- | --- | ---: | ---: |
| 1,000 | Camera | 124.5 / 130.3 | 151.1 / 155.3 |
| 1,000 | Whole packed group | 245.8 / 280.2 | 306.4 / 310.7 |
| 1,000 | Deformation with navigation | 534.5 / 550.3 | 1469.7 / 1471.0 |
| 1,000 | Topology with navigation | 639.6 / 651.2 | 1587.3 / 1606.7 |
| 100,000 | Camera | 198.4 / 251.0 | 229.6 / 282.3 |
| 100,000 | Whole packed group | 1014.4 / 1016.2 | 1183.7 / 1203.7 |
| 100,000 | Deformation with navigation | 642.3 / 658.4 | 2817.2 / 2820.3 |
| 100,000 | Topology with navigation | 739.5 / 751.2 | 2928.3 / 2967.8 |

The early image may retain the previous geometry. Full pixel resolution does not
prove updated geometry; both times and physical render revisions are recorded.
At 100k, camera feedback during deformation/topology now precedes their roughly
2.8-2.9 second geometry finish. Prior checkpoint first-paint medians were about
2,776/2,859 ms; current early-preview medians are 642/740 ms. These separate-build
small samples are not a randomized speedup or p95 certification. Camera/transform
edits perform zero geometry conversion and retain mesh source buffers.

All transform trials preserve the same physical render revision from first image
through the completed capture. A separate held-capture test first converges an
object preview, then releases the identical complete capture and verifies exact
pixel retention, unchanged physical generation and the engine reuse counter.
This removes a redundant sampling reset; completion ACK/validation still costs
time. A genuine change, refinement, or explicit refresh without an accepted
equivalent preview retains its normal behavior.

Deterministic barriers at 1k/100k verify camera images before scan, conversion and
owned scene preparation are released. The 100k cases paint at 383/357/394 ms.
Those are ordering/lifecycle tests, not latency certification. They also check
root supersession, nonblocking Stop/restart/close and ownership release. Five AOV
dependency checks per CPU/OptiX device ensure a new pass waits for the real source
commit while camera/Pause/Stop bypass it. Native Houdini IPR is checked separately.

One hundred thousand object transforms still cost about one second. Large packet
encoding and active engine synchronization remain on the serialized control path;
a camera arriving during that work cannot bypass it. Continuous/burst object-drag
liveness needs its own test before claiming continuously current object feedback.
Camera median/p95 budgets are not certified. Edit-phase Qt gaps have roughly
16 ms medians and 379/400 ms maxima at 1k/100k; these include Houdini/viewport/test
work. Matched viewport-only attribution and 1080p/4K physical-buffer profiling
remain outstanding. The legacy cooking_delta is still unsuitable as cook time.

The separate five-frame worker sequence takes 12.727 seconds, with adaptive
turbine, five actual displacement maps, geometry deformation and 16 nested shared
instances. Frames including outputs take 5.041/1.851/1.862/1.864/1.811 seconds;
interframe observations complete the total. Eight geometry resources stay live;
warm frames convert two. Setup is separate. This is interactive completion/output,
not final ROP throughput. Native batch separately writes three CPU and three OptiX
frames.

`artifacts/verification/camera-priority-20261017-final/index.json` indexes 27
targeted wrappers with exact source/binary/patch/kernel provenance and a current
source archive. Thirty-one navigation contracts, including nineteen fresh-capture
comparisons, pass per CPU/OptiX device. Full-suite, physical-memory, 3080 Ti/mixed
device and production certification remain separate. No Blender/Redshift parity
or steady-render percentage target is established.

## Navigation previews during capture - October 17, 2026

Host `20261016T235903767` adds owned camera/OBJ navigation against the acknowledged
worker scene while a separate capture runs. It retains one renderer/GPU scene.
The engine, 31 patches, protocol 31 and six kernels match the prior packed milestone.

The frozen runs use the same 1,036,800-triangle shared native prototype plus
950,906 courtyard triangles, 1,200 points, 605 curves and three VDB fields. There
is one Cycles viewer, RTX 4090 OptiX, 960x540, no denoising/adaptive sampling or
motion blur, and 65,536 maximum samples. Each count has 30 edits and six concurrent
camera/packed-object navigation pairs during heavy conversion. Transform trials
move every instance in `/obj/heavy_packed`.

First-current-navigation paint and updated-geometry paint are now measured
separately, in milliseconds as median / maximum of three trials:

| Instances | Workload | First navigation/image | Updated geometry |
| --- | --- | ---: | ---: |
| 1,000 | Camera | 121 / 122 | 121 / 153 |
| 1,000 | Whole packed group | 235 / 239 | 332 / 333 |
| 1,000 | Prototype deformation with navigation | 495 / 517 | 1,362 / 1,382 |
| 1,000 | Prototype topology with navigation | 613 / 671 | 1,499 / 1,532 |
| 100,000 | Camera | 183 / 187 | 215 / 236 |
| 100,000 | Whole packed group | 1,065 / 1,075 | 1,281 / 1,303 |
| 100,000 | Prototype deformation with navigation | 2,776 / 2,791 | 2,776 / 2,791 |
| 100,000 | Prototype topology with navigation | 2,859 / 2,924 | 2,859 / 2,924 |

The first column may show retained geometry with the newest navigation. It must
not be interpreted as time to the changed prototype. Full pixel resolution is
also independent of geometry freshness. Physical render revisions distinguish
these images even when they share a logical user generation. Camera edits perform
zero geometry scan batches/conversions; camera and outer-object moves retain mesh
source data. Ordinary edits have zero full-session cancellations.

Earlier packed trials below waited for combined geometry/navigation, so their
single first-paint column is not equivalent to the new early-preview column.
These are separate frozen runs, three trials rather than randomized p95
certification. At 100k, a large object-transform update can still hold back a later
camera image. The small camera must get a separate priority step before large
object-table rebasing/serialization and expensive worker synchronization. Full
scene work currently wins once ready; it can obscure the early-preview benefit.

The 1k transform trace also exposes a redundant reset: its navigation preview and
identical completed capture advance physical revisions separately. Reuse must be
permitted when a preview already supplies that source state, while preserving
explicit refresh/restart semantics and real geometry/material changes. This is
recorded next work, not an implemented optimization.

Edit-phase Qt gap medians are about 16 ms; maxima are 390 ms at 1k and 400 ms at
100k. They include Houdini evaluation, viewport and test work. They do not measure
only renderer/GPU-induced pauses. The legacy cooking_delta remains unsuitable as
incremental cook time. No no-stutter, 150 ms p95, physical-memory, 1080p/4K or
mixed-device certification follows from these results.

Deterministic barriers separately keep a heavy scan/conversion held until a real
viewer image has painted. The 1k scene shows previews at 236/215 ms before release,
then merges the pending geometry and latest poses without replacing the worker.
Barrier timings validate ordering and are not throughput/p95 benchmarks.

The separate 16-instance/two-level heavy animation with adaptive turbine and five
animated displacement maps completes five images/outputs in 12.980 seconds.
Initial capture converts eight resources, then two per warm frame with eight live
throughout. Setup is separate; native batch output is tested independently.
See `artifacts/verification/navigation-priority-20261017-final/index.json` for
per-frame timings, 24 targeted wrappers, source/binary/kernel audits and failures.
The earlier pre-documentation record and its sources remain preserved in
`navigation-priority-20261017/`. No Blender/Redshift parity is claimed.

## Packed leaves and immutable snapshots - October 17, 2026

The current host is `20261016T231151646`. The engine, protocol 31 and six kernels
match the preceding tangent milestone; this change optimizes host preparation
and snapshot ownership. Native IPR still uses one persistent worker. Historical
three-mode experiments below are preserved, not selectable artist modes.

### Capture-only diagnosis

The baseline and current native Grid/Copy to Points fixtures have one shared
32,768-triangle prototype, no manual IDs and no motion sampling. They run without
a Cycles render session, GPU upload or viewer. Each build measures a cold capture
and two warm repetitions of camera, OBJ transform, packed placement and prototype
edits at 1,000, 10,000 and 100,000 instances. Builds were run separately rather
than in randomized alternating order. Input source/binary hashes are retained.

At 100k instances, baseline warm OBJ/packed/prototype preparation takes 429-499 ms
on the host; the published current run takes 16-31 ms. Instance-object expansion
has moved to the background conversion phase, which takes about 171-193 ms. The
host completion/retirement step still takes about 21-22 ms. Prototype capture
totals 264-266 ms, excluding encoding. This moves substantial work off the UI
thread; it does not remove all capture, retirement or renderer costs.

Current camera capture takes 2.4-6.1 ms in this diagnostic, with 8-16 microseconds
of snapshot encoding. The immutable shared object table avoids copying/comparing
100k unchanged objects. Camera messages are 38,654 bytes. Moving every instance
still sends 29,316,333 bytes and takes 84-107 ms to encode in the two OBJ trials;
object changes still clone a private object table. These are owned CPU buffers,
not shared GPU scene memory or asynchronous Houdini evaluation.

The batching optimization applies to packed leaves without effective shutter
sampling. Nested containers retain host resolution and can contribute leaf
batches. Shutter-sampled packed conversion follows its previous supported path,
covered by the existing implicit-ID/motion regressions but not accelerated here.

Raw reports are under `artifacts/performance/packed-phase-before-20261017/` and
`artifacts/performance/packed-phase-published-20261017/`. Intermediate refined,
shared-table and final-counter profiles remain preserved as separate cohorts.
Exact pre-change sources are verified against the actual baseline manifest;
`packed-phase-shared-20261017/integration-change.patch` records the final code
difference. The reusable capture-only command is:

```powershell
./scripts/run-hython.ps1 -HythonArguments @(
  'tests/benchmark_packed_capture.py', '--counts', '1000', '10000', '100000',
  '--repetitions', '2', '--destination', 'artifacts/performance/my-packed-profile')
```

Use a fresh destination and a frozen staged build. The profiler verifies source
and compiled-artifact hashes before/after timing. Hashing and process-memory
observations are outside timed capture. These observations do not establish a
physical-memory plateau or a steady-render throughput budget.

### Real single-viewer interaction

The independent GPU runs use RTX 4090 OptiX, 960x540, denoising/adaptive sampling
off, 65,536 maximum samples, a native 1,036,800-triangle packed prototype and
950,906 additional courtyard triangles, 1,200 points, 605 curves and three VDB
fields. The prototype is shared; the triangle count is not multiplied into
unique geometry by the number of instances. Each count runs alone with one
worker/viewer, 30 parameter edits and six camera/packed-object input pairs during
heavy capture. Transform trials move `/obj/heavy_packed`, including all instances.
Motion blur is disabled specifically for these response runs.

The table measures parameter action to actual Qt completion of the first image
for the accepted scene, in milliseconds, as median / maximum of three trials:

| Instances | Camera | Whole packed group | Prototype deformation | Prototype topology |
| --- | ---: | ---: | ---: | ---: |
| 1,000 | 154 / 157 | 284 / 294 | 1,401 / 1,402 | 1,581 / 1,588 |
| 10,000 | 158 / 170 | 372 / 376 | 1,486 / 1,518 | 1,571 / 1,632 |
| 100,000 | 200 / 207 | 1,305 / 1,306 | 2,576 / 2,669 | 2,649 / 2,701 |

First available and full-resolution paint timestamps are recorded separately.
For example, the third 1k camera preview appears at 154 ms and full resolution at
186 ms; 100k camera previews are already full resolution. A sample metadata value
of zero is the publisher's progress state, not proof of an image containing no
traced samples. First-current-image measurements include capture/transport,
engine work and presentation; phase diagnostics are separate and overlap.

All camera trials, including the first after serial-zero scene initialization,
have zero geometry scan batches and conversions. Camera and OBJ transforms do
not request mesh vertex/index uploads. Engine metadata such as primitive offsets
can still copy. Ordinary edits preserve the worker and record zero full-session
cancellations; the explicit Stop check increments that counter once.

Edit-phase event-delivery gaps have medians near 16 ms but maxima of 395/382/387 ms
at 1k/10k/100k. Steady gaps peak near 31 ms. Complete-run gaps, including native
setup and memory-observation/testing phases, peak at 487/527/478 ms. These are Qt
proxies including Houdini cooks, viewport rendering and test work, not a causal
measurement of renderer-induced stalls. The legacy `cooking_delta` is a subtraction
of per-capture values and must not be used as incremental cook time. A matched
viewport-only control and finer scheduling attribution remain required.

Three trials are exploratory evidence, insufficient to certify p95 acceptance
budgets. Camera previews are not guaranteed below 150 ms and 100k whole-group
transforms exceed 500 ms. Geometry capture is background work, but the queue still
holds camera/object previews behind a pending capture. Concurrent input is
accepted and coalesced correctly; it does not yet display independent navigation
before heavy geometry completes. That scheduling improvement is the next task.

### Five frames and lifecycle

The heavy animation test uses 16 instances in two nested levels, the same million
triangle shared prototype, an adaptive turbine and five changing displacement
maps, at 128 samples/960x540 with one worker/viewer. The complete sequence,
including displayed completion and outputs, takes 13.419 seconds:

| Frame | Frame including output (s) | Cumulative including observations (s) | Converted resources |
| --- | ---: | ---: | ---: |
| 1 | 5.083 | 5.083 | 8 |
| 2 | 1.994 | 7.135 | 2 |
| 3 | 2.022 | 9.220 | 2 |
| 4 | 1.997 | 11.282 | 2 |
| 5 | 1.999 | 13.344 | 2 |

Eight geometry resources remain live. All five images and displacement-map
hashes differ. Setup is recorded separately. This is an interactive completed-image
sequence, not final ROP throughput; native batch separately writes three frames
per CPU/OptiX device. The held-capture GUI test verifies input coalescing,
deletion, Stop/restart and ownership release. Another GUI case retains adaptive
tessellation through 60 continuous camera exports and refines after release.

The evidence index is `artifacts/verification/packed-response-20261017/index.json`.
Nineteen targeted wrappers have matching compiled code, engine patches and
kernels, plus archived source audits. Failed iterations and superseded cohorts
remain separate. Mixed packed component references, negative scale, local missing
materials/repair, Cryptomatte, alias deletion/recreation and immutable snapshot
contracts pass. Full-suite, long-run physical-memory, 1080p/4K display-copy and
3080 Ti/mixed-device certification remain outstanding. No Blender/Redshift parity
or steady-render percentage target is established by these results.

## Selective mixed attributes and bulk capture

The new host adapter checks the attribute owners consumed or validated by each
mixed mesh/particle/curve component, instead of checking every supported name
on every owner. Numeric values and positions use bounded public HDK range reads.
Fifteen independent mixed-SOP groups per CPU/OptiX device verify both retention
for unused attribute changes and updates for consumed values. Shared mesh/hair
points still update both consumers. [Supported domains](notes/native-mixed-sops.md)
describe the conservative fallback and validation behavior.

The same 950,906-triangle animated fixture ran three alternating five-frame
viewport-only and capture-only sequences again, with twelve warm frames per
condition. These are separate frozen builds/runs, not randomized old/new trials:

| Observed median | Original copied-detail build | Direct conversion/cache build | Typed bulk-read build |
| --- | ---: | ---: | ---: |
| Owned capture after displayed-SOP resolution | 677.3 ms | 310.0 ms | 250.4 ms |
| Adapter conversion, inside capture | 572.8 ms | 213.9 ms | 153.7 ms |
| Total timed host action with capture | 722.0 ms | 350.7 ms | 286.9 ms |

Capture spans 246.9–261.9 ms in the latest run. Its observed median is 19% lower
than the direct-conversion build and 63% lower than the original. Displayed-SOP
resolution takes 36.7 ms without capture and 34.8 ms with it. Maximum warm Qt
event-delivery gaps are 389 ms without capture and 378 ms with it; independent
maxima are not a causal subtraction. Capture has no Cycles session or GPU upload.
The UI pause remains substantial, and work outside the measured converter still
needs finer timing. Conversion is part of capture, not an additional interval.

The real Cycles panel then completed five frames in each mode with native
deformation and five changing displacement-image assets:

| Mode | Sequence including completed panel images and file output | Median warm host apply |
| --- | ---: | ---: |
| Synchronous | 9.118 s | 617.0 ms |
| Queued in-process | 9.413 s | 254.1 ms |
| Persistent worker | 13.346 s | 254.4 ms |

Each later frame converts one ordinary mesh component and retains four other
resources. The ordinary mesh still includes unchanged ground/rocks alongside
the changing turbine. Topology counters remain two initial builds, four exact
fallback scans/reuses, 483,075 triangulated faces and 1,923,000 retained faces.
Queued paths record zero full-session cancellations; synchronization and sample
reset still occur. These single interactive sequences do not select a production
backend or establish final ROP throughput, p95 response or Blender parity.

Beauty differences pass the 0.005 RMSE tolerance, with maximum RGBA RMSE
0.000587. The displacement pixels agree exactly across modes; depth and position
also agree. Filtered normal/UV components still show sparse differences up to
0.115/0.133, with unresolved cause. Outputs are not bit-identical. Tracked owned
geometry/device allocations remain about 319.6 MB/1.6167 GB across these five
frames. The 55,328,992-byte topology metadata estimate excludes retained mesh
buffers and renderer copies; physical residency and long-run bounds are not
certified.

The full native suite `artifacts/verification/20261011T153413210/` passes
**112/112 cases**: 64 headless checks and 24 actual GUI workflows on each of CPU
and RTX 4090. Its immediate audit matches 269 sources, five binaries, fourteen
patches and six kernels. Frozen manifest SHA-256:
`a466225c107379ef12faa1151dd7ae46eb66b11a3f6e8ee787f45579f83e6a35`.
Reports, EXR comparison analyzer, panel images and exact frozen sources are in
`artifacts/performance/native-component-capture-20261011T153020318/`.

The viewport-only diagnostic remains explicitly labeled **Cycles diagnostic:
viewport only - rendering disabled**. Each rendering comparison saves the actual
completed Cycles panel. The reported artist-started panel switching to a viewport
has not been reproduced.

## Earlier direct mesh capture and retained triangulation

The current converter reads closed polygons and polygon soups through the
public, non-modifying `GEOtriangulatePoly` interface. It no longer builds a
temporary copied Houdini detail for triangulation. A per-resource cache retains
logical point/vertex indices, original face provenance and triangle corner
mappings. Stable IDs permit direct reuse; changed IDs require exact connectivity
comparison. Moved quads/n-gons are retriangulated because their valid diagonal
or concavity can change even when topology is unchanged. Supported unchanged
normal/UV/color arrays reuse their owned values. Positions and output arrays
are still captured into owned storage on the host thread.

The identical heavy animated fixture ran three alternating viewport-only and
capture-only sequences again, with twelve warm frames per condition:

| Observed interval | Previous capture implementation | Current implementation |
| --- | ---: | ---: |
| Median owned capture after displayed-SOP resolution | 677.3 ms | 310.0 ms |
| Median adapter conversion, inside capture | 572.8 ms | 213.9 ms |
| Median total timed host action with capture | 722.0 ms | 350.7 ms |
| Maximum warm Qt event-delivery gap with capture | 644 ms | 415 ms |

Current displayed-SOP resolution medians are 38.4 ms without capture and 37.9 ms
with capture. Current capture spans 302.3–321.2 ms; the viewport-only maximum
event gap is 400 ms. These are separate frozen runs, not randomized old/new
build comparisons. Capture's observed median fell 54%; UI stalls remain. Do
not subtract independent event-gap maxima to assign a causal renderer/GPU cost.

All three execution modes rerendered the same five heavy frames, with one Cycles
panel, deformation and five actual animated displacement maps:

| Mode | Complete five-frame sequence including outputs | Median warm host apply |
| --- | ---: | ---: |
| Synchronous | 10.315 s | 709.9 ms |
| Queued in-process | 10.363 s | 320.0 ms |
| Persistent worker | 13.321 s | 312.4 ms |

Each later frame still converts one complete ordinary mesh component, but
retains four other resources and its stationary faces. Across the sequence the
adapter records two initial topology builds, four exact fallback scans/reuses,
483,075 face-triangulation operations and 1,923,000 retained-face operations.
These are cumulative CPU capture counters, not GPU-transfer counts. Queued
paths record zero full session cancellations. The worker still has transport
and output costs; this experiment does not choose the production process.

The topology/provenance metadata occupies 55,328,992 estimated bytes on this
fixture and retires with its geometry. That counter excludes the last retained
owned `MeshData`, renderer copies and host/driver memory. Queued snapshots can
share the owned data; the synchronous core currently copies it into its own
source record. No lower total-memory claim follows from the metadata counter.
Tracked geometry/device allocations remain roughly 319.6 MB / 1.6165 GB, with
no growth across these five frames; long sequences and physical residency are
not certified.

Beauty comparisons pass the existing 0.005 RMSE tolerance, with maximum RGBA
RMSE 0.000618 across modes/frames. Depth and position match exactly; filtered
normal/UV passes still have sparse differences, up to 0.125/0.133 in individual
components, with unresolved cause. Outputs are not bit-identical. The new CPU
and OptiX regression independently compares triangles, concave quads, n-gons
and soups against a Houdini Divide SOP, including changed diagonals,
face-varying attributes, recreated IDs, same-size changed connectivity and
retirement. Its analytic AOV checks supplement reference-image comparisons.

The full native run `artifacts/verification/20261011T144349084/` passes 112/112:
64 headless checks and 24 actual GUI workflows per CPU/RTX 4090. Its immediate
audit matches 269 sources, five binaries, fourteen patches and six kernels,
manifest `388371e6c69a6eaa5f9e4d1ac64c7c938c0aa5de0e93d6c5ed6746fe84a207cf`.
Reports, analyzer, panel images and exact source archive are in
`artifacts/performance/native-capture-20261011T143818606/`. These are development
and interactive measurements, not final ROP throughput or Blender benchmarks.

Viewport-only diagnostics deliberately create no render session/panel. The
diagnostic window is now titled **Cycles diagnostic: viewport only - rendering
disabled**. Rendering comparisons require five completed current images in the
Cycles panel and save the whole panel as display evidence. A reported panel
turning into a viewport has not been reproduced by those tests.

## Earlier five-frame animation, with one render viewer

The scaled 950,906-triangle courtyard now includes a native turbine twist
expression and five different 256×256 float32 RGB EXR displacement maps. Each
map drives actual geometry displacement with explicit data color space; surface
noise/bump remains separate. All three modes render frames 1–5 at 24 fps,
960×540, 128 samples, denoising off, one Cycles panel plus a real Scene Viewer.
No native IPR is opened. Earlier two-viewer experiments below are historical
compatibility measurements. Regular timing tests now use one render viewer.

| Frame | A: sync, frame / cumulative seconds | B: queued, frame / cumulative seconds | C: worker, frame / cumulative seconds |
| --- | ---: | ---: | ---: |
| 1 | 2.679 / 2.679 | 2.739 / 2.739 | 3.347 / 3.347 |
| 2 | 2.111 / 4.848 | 2.235 / 5.032 | 2.708 / 6.113 |
| 3 | 2.102 / 7.007 | 2.163 / 7.251 | 3.056 / 9.226 |
| 4 | 2.167 / 9.232 | 2.424 / 9.733 | 2.930 / 12.216 |
| 5 | 2.207 / 11.498 | 2.233 / 12.024 | 2.869 / 15.145 |

Per-frame times include a completed current canvas, EXR and preview output.
Cumulative times also include previous interframe/memory observations. Whole
sequence totals are **11.556 / 12.086 / 15.204 s**, including about 0.29 s of
observations. Fixture setup and warming, approximately 2.8–3.0 s, are outside
those totals; initial engine construction is included in frame 1. These are
single, unrandomized interactive sequences per mode. They do not certify final
ROP throughput, percentiles or a comparison with Blender.

Every later frame converts one geometry component, retains four other geometry
resources and increases the displacement-rebuild counter. All five output images
and map hashes differ. B/C keep their renderer PID and record zero ordinary full
session cancellations; A records five, including initial submission. Thus the
11.6-second A result is the synchronous cancelling path. It is not the queued
open-session path and says nothing by itself about viewport smoothness.

Matched images pass the existing 0.005 beauty RMSE tolerance; maximum observed
RGBA RMSE across modes/frames is 0.000440. Depth and position match exactly.
Sparse differences in filtered normals/UV and beauty mean outputs are not
bit-identical. Their cause is not yet resolved; adaptive sampling is disabled,
so it is not an explanation. Map pixels agree exactly across modes. Five frames
show stable tracked geometry/device allocations but do not certify long-run
physical-memory behavior.

Reports, previews, per-frame metrics, analyzer and exact hash-checked archive of
266 sources are in
`artifacts/performance/native-animation-20261011T134218875/`. All three terminal
runs and immediate audits pass with frozen manifest
`404a453fd4448f83e7ead9000a83dda2139bd5d6d1fc7b130694f48814f8405d`.

## Separate Houdini calculation from plugin capture

The same animated heavy fixture then ran three alternating viewport-only and
three capture-only five-frame sequences in one isolated Houdini GUI. Capture
handles own converted state but construct no Cycles session or GPU scene.
The native displayed SOP resolves before capture. Statistics below use the
twelve warm frames 2–5 per condition; setup/frame 1 are excluded.

| Observed interval | Viewport only | Viewport plus capture, no engine |
| --- | ---: | ---: |
| Median explicit displayed-SOP resolution | 38.4 ms | 40.1 ms |
| Median owned capture after that resolution | — | 677.3 ms |
| Median measured adapter conversion, inside capture | — | 572.8 ms |
| Median adapter wait for cooked geometry, inside capture | — | 0.008 ms |
| Median total timed host action | 38.5 ms | 722.0 ms |
| Maximum warm Qt event-delivery gap | 326 ms | 644 ms |

Capture spans 639.6–758.6 ms across those warm frames. It is a substantial plugin
CPU cost before any renderer/device synchronization. Conversion is a subinterval
of capture; do not add it twice. SOP resolution excludes later viewport redraw,
and heartbeat gaps include scheduling/redraw. Do not describe 38 ms as total
Houdini stutter or subtract independent maximum gaps to estimate GPU loading.

The rendered animation's warm adapter cook waits are also only 0.009–0.014 ms.
Median warm conversion is 586.0 ms in A, 596.5 ms in B and 589.3 ms in C; median
host applies are 1086.8 / 693.7 / 690.8 ms. Queuing removes the UI's engine-apply
wait but leaves synchronous owned capture. No GPU-copy conclusion follows from
these CPU timers.

`artifacts/verification/prototype-sync-CUDA_NVIDIA_GeForce_RTX_4090_0000_03_00_OptiX-20261011T135904408/`
passes with an immediate audit of 267 sources, five binaries, fourteen patches
and six kernels; frozen manifest
`dbb4c8397df18ce55a10b42bc5c261f557e735be8bb5319e909c56aaf060a63d`.
The reproducible analyzer, full report, coverage audit and exact source archive
are in `artifacts/performance/native-viewport-20261011T135904408/`.

For that earlier baseline, renderer/host/runtime sources and all 111 executed
full-suite test sources were unchanged from the preceding successful full run.
The direct-converter change above has its own later complete regression run.
[Workflow policy and Blender references](notes/native-workflow-policy.md) explain
why final rendering and IPR require separate execution decisions and benchmarks.

## Queued and external rendering on the heavier courtyard

The implemented comparison uses a scaled editable SOP courtyard: 950,906 mesh
triangles, 476,734 vertices, 352 objects, 1,200 particles, 605 curves, 7,520 curve
keys and three sparse volume fields. Native SOPs/VEX build the substantive scene;
true noise displacement remains enabled. All modes use the same RTX 4090 OptiX
build/settings, 960×540, 65,536 samples, no denoising and matching OCIO, with a
native viewport and both render viewers open.

Each mode ran once, in A/B/C order, with three edits of each of ten workload
types: camera, transform, surface color, mesh deformation/topology, noise
displacement, curve width, point radius, volume transform and area-light edits.
These are exploratory unrandomized observations. They do not certify p95
latency, the release budgets, Redshift parity or a speed advantage.

| Observed metric | A: synchronous | B: queued in-process | C: persistent worker |
| --- | ---: | ---: | ---: |
| Median edit to current full-resolution canvas, all 30 edits | 752 ms | 764 ms | 812 ms |
| Maximum warm 16 ms Qt heartbeat gap | 683 ms | 869 ms | 748 ms |
| Median mesh-deformation host apply | 1,418 ms | 1,012 ms | 1,031 ms |
| Median mesh-deformation edit to canvas | 2,342 ms | 2,432 ms | 2,855 ms |
| Full session cancellations before Stop, including initial submission | 31 | 0 | 0 |

B and C use the same queue/update policy and persistent engine boundary. A-to-B
changes the cancellation policy as well as scheduling. The worker did not
demonstrate faster response or eliminate UI pauses. Host mesh capture still
copies and triangulates the complete ordinary mesh component after a deformation.
That component includes unchanged ground/rocks alongside the edited turbine.
Boundary waiting reached 503 ms in B on the control thread; the UI still does
host capture and display work.

Camera/transform edits record zero adapter geometry conversion and no observed
triangle/point/curve copy requests. Mesh deformation converts one component;
curve, point and volume edits retain other supported components. Topology
converts three through conservative index/provenance changes. These logical
counts do not prove physical object-local GPU traffic.

C's roughly 51-second workflow counted 791,804,219 input bytes and 3,120,123,646
output bytes over 557 exchanges. Accumulated encode/decode/mapping time was
0.463/1.733/0.229 seconds. Named shared mappings still require copies and VDB
serialization. End-of-run host/worker memory snapshots are separate observations,
not a simultaneous total peak or proof of lower memory usage. The raw test's
cooking_delta subtracts per-capture fields incorrectly and is excluded from the
derived comparison; total edit and host-call timing remain usable.

The archived dataset is
`artifacts/performance/native-execution-20261011T123609203/summary.json`.
It retains raw reports, provenance hashes, the original frozen manifest and an
exact hash-checked archive of 264 comparison source files. All three terminal
runs and immediate audits pass. [The worker document](notes/native-worker-prototype.md)
provides per-workload details, implementation limits and reproduction commands.

An additional test terminated the actively tracing worker on this same heavy
fixture. Houdini stayed open, retained the image and reported failure in 52 ms.
Restart rebuilt the scene and delivered current pixels to both viewers in
1.800 seconds. Stop submission took 0.029 ms and cancellation acknowledgement
took 98 ms in that run. This verifies the tested failure containment, not a
guarantee against host or GPU-driver failures.

The earlier complete `20261011T130105720` run passes 111/111 cases with an immediate
source/binary audit. Its new headless CPU case exercises the three modes; the
default 24-per-device GUI group remains synchronous. Separate focused GPU and
heavy GUI results do not establish full queued-mode feature/artist parity.
The later capture measurements isolate much of the added host cost, and valid
triangulation is now retained. Next work should further narrow preparation and
transport/display copies, then use interleaved rendered measurements.

## Historical native courtyard interaction

The following measurements investigate the reported viewport lag. They do not
certify production response budgets or Redshift parity. The pinned native build
is exercised in real Houdini 22.0.429 UI processes, with a Scene Viewer, the
Cycles panel and, for the final edit, native IPR. The courtyard mixes native SOP
meshes, 54 packed instances, 360 points, 265 curves, a VDB plume and noise
displacement at 960×540. Diagnostic collection and a 16 ms Qt heartbeat are on.

This section records the earlier unscaled courtyard investigations before the
queued/worker comparison above. They are preserved as historical observations;
the scaled fixture and execution policy differ.

`artifacts/stage1/gui/native-artist-review-cpu/` and
`native-artist-review-optix/` contain one exploratory artist run per device:

| Metric | Ryzen 7950X CPU | RTX 4090 OptiX |
| --- | ---: | ---: |
| Maximum UI heartbeat gap | 1372 ms | 575 ms |
| Heartbeat-gap p95 within that run | 378 ms | 134 ms |
| Display-image call median | 76 ms | 49 ms |
| Display-image call maximum | 136 ms | 57 ms |
| Turbine position edit to current panel image | 1954 ms | 855 ms |
| Camera edit to current panel image | 725 ms | 431 ms |
| OBJ transform to current panel image | 736 ms | 361 ms |
| Surface roughness edit to current panel image | 433 ms | 379 ms |
| Turbine topology edit to current panel image | 1209 ms | 694 ms |
| SOP edit with native IPR and panel open | 1497 ms | 772 ms |

These edit times include the test's scheduling/polling and actual canvas update;
engine first-image time alone is shorter. They are individual observations, not
p95 edit latency. The preceding full verification's artist cases measured maximum
heartbeat gaps of 777 ms on CPU and 626 ms on RTX 4090, illustrating variation.
The GPU viewport/render UI still stalls and is not declared fixed.

### Matching native-view attachment

The earlier full run `artifacts/verification/20261011T104835799/` passes all
110 cases. Its artist workflow verifies that native IPR receives an independently
specified sky pixel from the panel's existing render without advancing its
generation. A subsequent genuine SOP edit must advance it. The shared render
continues sampling during attachment; explicit Render remains a fresh generation.

| Current full-run observation | Ryzen 7950X CPU | RTX 4090 OptiX |
| --- | ---: | ---: |
| Native IPR attachment apply | 24.4 ms | 9.4 ms |
| Native IPR start to verified image pixel | 991 ms | 433 ms |
| Genuine SOP edit with both views: host apply | 273 ms | 647 ms |
| Cancellation join within that genuine edit | 1.7 ms | 506 ms |
| Genuine SOP edit to current panel image | 1071 ms | 1001 ms |
| Maximum UI heartbeat gap | 800 ms | 715 ms |
| Heartbeat-gap p95 within that run | 221 ms | 119 ms |

The preceding GPU join apply took 525 ms, including 513 ms in cancellation. Reuse
removes that unnecessary restart. Timings are individual, unpaired observations;
9.4 ms is not native-view end-to-end latency. Reused generation diagnostics retain
the preceding update's timers, so those timers must not be counted as new join
work. The previous full run recorded 872/933 ms CPU/GPU maxima, while another
targeted GPU run recorded 920 ms. These unpaired runs also have different
diagnostic/conversion work and do not establish a certified overall lag reduction.

### Cancellation and diagnostic observer cost

The pre-guard instrumented GPU baseline in
`artifacts/verification/cancel-images-baseline-20261011T102237408/` records one
full output delivery during every ordinary cancellation: roughly 10–20 ms of
pass reading plus 2–4 ms of display conversion. Scene diagnostics also waited
270–282 ms for a lock after some geometry edits. The latter is measurement
overhead; ordinary panel polling does not request that diagnostic snapshot.

Interrupted callbacks now reject obsolete output before pass allocation/read
and reject stale display data before half-to-float conversion. The display
driver still accepts the engine's update-begin contract; this does not remove
the earlier GPU-to-display-buffer copy. Normal completed passes and progressive
denoising remain covered by the regressions.

In the earlier 110-case artist cases, ordinary cancellations skip the old output
read and display conversion instead of recording another delivered output.
Diagnostic scene-lock attempts take less than 0.001 ms in these snapshots and
report `engine_statistics_pending` while the engine is busy. They do not wait
for BVH/displacement preparation. Other control operations can still block.
The remaining 506 ms GPU join demonstrates that discarded image copies and the
diagnostic observer were not the whole issue.

`artist-timing-summary.json` in the current verification directory records the
derived timings with hashes of both original GUI reports. It is exploratory
evidence, not a paired process-boundary benchmark.

The pinned engine's interactive scheduler lengthens work intervals as a render
settles, up to two seconds, and cancellation joins its render thread. Longer
work may help explain the remaining join delays; it has not been established by
a device trace. The scheduler is unchanged. Measure cancellation and work duration
before trading steady rendering throughput for shorter interactive intervals.

A separate real-viewport baseline applies twenty native turbine edits without
Cycles. `artifacts/stage1/gui/viewport-native-sops-no-render/` records 10–28 ms
SOP cooks, maximum heartbeat gap 121 ms and p95 92 ms. It is not a randomized,
paired comparison; it helps distinguish ordinary viewport work from integration
costs. No causal attribution to duplicate GPU copying follows from these numbers.

### Display conversion

The original isolated 960×540 display probe measured approximately 132–136 ms
for the ACES 2 SDR view and about 9 ms for raw pixels. RGB processing now uses a
bounded four-thread TBB arena above 65,536 pixels; smaller images stay serial.
The later probe in `artifacts/examples/sop-fx-courtyard/display-cost.json`
measured about 49–50 ms for that view and 9 ms raw. It was captured before the
face-winding correction and is a display-path observation, not a total-render
speedup claim. The corrected artist image gives the separate timings above.

`houdini_display_parallel_smoke.py` compares a 257×257 partial-row image against
an independent PyOpenColorIO whole-image transform, covering exposure, RGBA,
RGB, alpha and raw normal data. All four cases have zero maximum byte error;
alpha is preserved. OCIO processing uses disjoint RGB rows and retains the
caller's floating-point environment. Preparation/packing and display delivery
remain synchronous. GPU graphics interoperability is not implemented.

### Native conversion and device requests

Completed-generation diagnostics verify that camera and existing light-movement
edits retain mesh buffers. Light creation triggers about 12.2 MiB of logical
common-interface requests despite no HDK reconversion. A local-UV OBJ transform
retains triangle vertices/indices but requests 64 MiB of shader-evaluation input,
consistent with conservative heterogeneous-volume octree preparation in the
pinned source. Those copy counts come from the preceding completed-generation
probe. It recaptured four components for a turbine edit and requested roughly
91 MiB on the GPU path; it predates the selective mixed-component checks.

The current full-run courtyard deformation recaptures one component, retaining
curves, particles, volume and the packed prototype. A topology edit still
recaptures three under conservative index/provenance invalidation. Checks run
only after the shared detail's IDs become dirty, and their CPU time is included
in extraction. The current artist cases accumulate 127 ms of component checking
on CPU and 73 ms on GPU across their whole edit sequences, not per edit.
No new completed-generation physical-transfer benchmark has been run.

These are logical copy requests, not physical PCIe bytes or VRAM residency.
Timers can nest and overlap. GPU displacement requires input geometry during
evaluation and displaced geometry afterwards, so repeated uploads are not
universally redundant. [The source audit and remaining work](notes/native-interactive-updates.md)
explain the evidence and limitations. Granular fallback refinement, reducing
UI-thread waits and selected progressive AOV delivery remain next optimization
work; no arbitrary 500 ms or 10% budget is claimed for this courtyard.

## Historical planar-fixture measurements

The following runs belong to earlier builds and predate the current winding,
denoising and native-courtyard changes. They provide earlier performance evidence,
not certification of the current build.

These development measurements establish a repeatable native/headless
comparison for one planar fixture. Complete FX certification, viewer
presentation latency, physical memory residency and Redshift parity remain
separate acceptance work.

These runs predate the native displacement ownership path. Polygon resources
now retain an independent canonical snapshot and, when needed, a prepared
seam snapshot. See `docs/user/native-displacement.md` for byte counters. The
historical timing and physical-memory results below do not certify the new
host-memory cost or displaced-mesh workloads.

## Reproduction

Run exclusively, with other renderer tests stopped:

```powershell
./scripts/run-hython.ps1 -HythonArguments @('tests/benchmark_grid_comparison.py')
./scripts/run-hython.ps1 -HythonArguments @('tests/benchmark_grid_comparison.py','--order','native-first')
./scripts/run-hython.ps1 -HythonArguments @('tests/benchmark_grid_comparison.py','--diagnostics')
./scripts/run-hython.ps1 -HythonArguments @('tests/benchmark_grid_comparison.py','--diagnostics','--memory','--order','native-first')
```

Defaults: 501-by-1001 point XY grid, 500,000 quads converted to 1,000,000
triangles, 4-by-2.25 SOP units, 960-by-540 pixels and 128 samples. Both paths
use RTX 4090 OptiX, interactive scheduling without a resolution divider,
five complete passes (combined, depth, normal, UV and position), no denoising
and no adaptive sampling. The orthographic camera is four units wide at z=5,
near=0.01 and far=10000. The diffuse material starts at (0.6, 0.2, 0.1),
roughness zero. A radius-zero point light at (-2, 3, 4) uses strength
(500, 500, 500); the environment is (0.025, 0.025, 0.025).

The native fixture uses the HDK converter. The headless fixture constructs
equivalent planar triangles independently. Houdini may choose a different
quad diagonal; equivalent geometry does not guarantee an identical BVH.
Both binaries use the same engine, patches, compiler configuration,
dependencies, kernels and integration services. Each run preserves a build
manifest, executable/module hashes, commands, logs, raw generation records,
full-quality EXRs and image-check results.

Each path now renders 97 generations: initial capture, one warm-up and one
unmeasured block preparation per edit type, and 30 repetitions each of camera,
object transform and material edits. Block preparation resets the other
state so a measured camera edit cannot also change the material or object.
Camera and transform edits alternate +/-0.02 units; material edits alternate
the red component between 0.6 and 0.62. A generation finishes before the next
edit. Initial capture creates a fresh scene/session but does not clear driver
or kernel caches; it is not a cold-cache measurement.

## Measured planar results

The historical, pre-cache 94-generation comparisons on 2026-10-10 below
passed independent position-z=0,
depth=5, normal=(0,0,1), alpha=1 expectations and interior image comparisons.
Evidence directories below are under `artifacts/performance`:

| Run / order | Edit | Native completed median | Headless completed median | Native / headless | Native first-image p95 |
| --- | --- | ---: | ---: | ---: | ---: |
| `grid-comparison-20261010T121529` / headless first | Camera | 233.52 ms | 235.18 ms | 0.993 | 11.60 ms |
| Same run | Transform | 229.92 ms | 234.55 ms | 0.980 | 11.16 ms |
| Same run | Material | 231.17 ms | 237.41 ms | 0.974 | 10.98 ms |
| `grid-comparison-20261010T121640` / native first | Camera | 230.16 ms | 235.93 ms | 0.976 | 11.87 ms |
| Same run | Transform | 230.59 ms | 234.57 ms | 0.983 | 12.35 ms |
| Same run | Material | 231.28 ms | 236.30 ms | 0.979 | 10.67 ms |

Completed-generation time runs from the core update through preparation,
sampling and delivery of all five float32 passes. It excludes preceding
Houdini cooking/extraction and final EXR encoding. It is not isolated
path-tracing kernel time. Native first-image latency includes host apply and
polling for a current-generation image whose effective rendered dimensions
are 960-by-540; enlarged previews cannot satisfy that predicate. Viewer
presentation is excluded. Percentiles use the nearest-rank convention.

These runs meet the prospective numerical budgets for the measured metrics
on this fixture. They do not establish the separate steady-rendering target
for all FX fixtures. Small differences between paths remain subject to run
variation and equivalent geometry; no rendering speed advantage is claimed.

Both paths converted geometry once and retained 1,228,753,892 bytes of Cycles
tracked allocations through all 94 generations. These counters include
host-backed device buffers and do not measure physical VRAM. The native
owned geometry snapshot occupies 23,018,012 bytes. Both sessions check zero
tracked allocations after shutdown. Repeated edits of one scene do not
certify long-sequence memory behavior.

## Opt-in diagnostics

`Session(..., collect_diagnostics=True)` enables `session.diagnostics()`.
Default sessions leave engine statistics and buffer-copy collection disabled.
Snapshots are generation-scoped and do not copy image planes. Engine
statistics are cleared at a safe scene synchronization point, accumulate
across kernel-loading retries, and produce no stdout reports.

The snapshot includes core update and cancellation/join time, named engine
manager timers, geometry/BVH/attribute timers, CPU display conversion and
full-quality output delivery. Timers can nest and overlap; summing them does
not give a disjoint total. The upstream `scene/device_update` timer covers
the shader/kernel prepass loop only. The API labels that timer
`scene/shader_prepass_and_kernel_load` and supplies a separate
`scene/device_update_total` around the complete update.

Buffer diagnostics record named host-backed `device_memory` copy requests:
call count, logical requested bytes and host dispatch wall time. They include
mesh index/position buffers and visible backend temporary buffers, while
excluding constant-memory writes, device-only operations and backend calls
that bypass this common interface. CPU requests may share host storage;
mixed-device requests are logical calls rather than per-device bus traffic.
They are neither physical PCIe-byte counters nor GPU execution timers.

`tests/houdini_diagnostics_smoke.py` checks unchanged emitted radiance with
collection enabled, an independently known two-triangle index byte count,
camera edits without triangle-buffer copy requests, new shader-attribute
uploads without native geometry conversion, geometry edits with uploads,
generation reset and tracked shutdown on CPU and RTX 4090.

The historical, pre-cache fully instrumented million-triangle run
`artifacts/performance/grid-comparison-20261010T122742` passes the same image
checks. Its native/headless completed medians differ by 1.94–2.87%; native
first-image p95 is 11.34–12.52 ms. Thirty repetitions per edit yield these
native median diagnostics:

| Metric | Camera edit | Transform edit | Material edit |
| --- | ---: | ---: | ---: |
| Houdini cooking | 0.0102 ms | 0.0099 ms | 0.0100 ms |
| Native geometry conversion | 0 ms | 0 ms | 0 ms |
| Core update, including cancellation | 2.881 ms | 2.937 ms | 2.917 ms |
| Cancellation/join, contained in core update | 2.855 ms | 2.907 ms | 2.890 ms |
| Complete engine scene update | 0.640 ms | 0.949 ms | 0.675 ms |
| CPU display conversion, accumulated per generation | 5.993 ms | 6.054 ms | 7.977 ms |
| Full-quality output delivery | 5.991 ms | 6.044 ms | 6.000 ms |

Initial native capture cooked in 10.23 ms and converted geometry in 199.94 ms.
The complete engine update took 106.37 ms, including an 80.85 ms shader/kernel
prepass. These initial-capture numbers are single observations with persistent
driver/kernel caches. Display conversion accumulates multiple presentations
per generation and is nested inside engine display work, so this table must
not be summed as exclusive phases.

All 90 warm edits made zero requests for triangle indices, triangle vertices,
triangle shader bindings or OptiX temporary mesh index/vertex buffers. Warm
transform edits updated instance/TLAS data as expected. The records also show
one shader-buffer request on every camera edit because unchanged material and
background payloads were rebuilt. The subsequent fixes and measurements are
recorded below.

## Semantic cache and isolated edit blocks

The shared core retains owned material, light and background definitions and
skips exact semantic no-ops. Native orchestration already filters unchanged
payloads; this gives direct core callers and future adapters the same behavior.
The native registry now deep-copies retained value payloads so an in-place
caller mutation cannot silently modify the comparison baseline. Actual shader
changes, including new attribute requirements, still invalidate the engine.
Patch 0007 clears shader-buffer modified flags only after successful device
delivery. Shader rebuilds mark the buffers dirty again; device errors retain
the pending update.

Two 97-generation comparisons passed all image checks and six independent
update checks (three edit types in each adapter):

| Run / order | Edit | Native completed median | Headless completed median | Native / headless | Native first-image p95 |
| --- | --- | ---: | ---: | ---: | ---: |
| `grid-comparison-20261010T125037` / headless first | Camera | 250.10 ms | 253.55 ms | 0.986 | 12.77 ms |
| Same run | Transform | 250.94 ms | 254.50 ms | 0.986 | 14.00 ms |
| Same run | Material | 251.77 ms | 255.06 ms | 0.987 | 13.98 ms |
| `grid-comparison-20261010T125135` / native first, memory sampling | Camera | 247.74 ms | 251.37 ms | 0.986 | 11.45 ms |
| Same run | Transform | 248.21 ms | 249.30 ms | 0.996 | 12.52 ms |
| Same run | Material | 248.66 ms | 253.04 ms | 0.983 | 12.25 ms |

Diagnostics confirm zero SVM shader-buffer requests for all 30 camera and
30 transform edits, and one real material rebuild for every measured material
edit. All 90 edits retain triangle buffers in each adapter. Camera edits still
copy four bytes of object primitive-offset metadata. Light/background build
counts remain unchanged. Both paths convert geometry once and retain the same
1,228,753,892 tracked bytes through all 97 generations, with zero after close.

In the unmonitored run, median complete engine scene updates are 0.154 ms for
camera, 0.858 ms for transform and 0.643 ms for material edits. Median core
update/cancellation time is 3.63–3.66 ms, accumulated CPU display conversion
6.35–8.07 ms and output delivery 7.72–7.90 ms. These nested timings cannot be
summed. Removing redundant graph construction and shader-buffer requests is
verified. The whole-generation timings differ from the historical runs;
the block protocol changed and run variation is not isolated, so no total
render speedup is claimed.

`artifacts/verification/20261010T125711781/summary.json` passes all 37 aggregate
regression cases after these changes, including CPU/RTX 4090 attribute
invalidation, in-place payload mutations, FX motion, native IPR and shutdown.

## Opt-in process and device memory sampling

`--memory` samples the benchmark child process using Windows
`K32GetProcessMemoryInfo` and device-wide framebuffer usage through NVML.
The nominal polling interval is 100 ms plus query cost. Raw records are stored
as `native.memory.jsonl` and `core.memory.jsonl`; unavailable counters retain
their status rather than becoming zero. This observer can perturb timing,
so it is opt-in and recorded in the benchmark metadata.

The million-triangle memory run `grid-comparison-20261010T125135` reports:

| Counter | Native Houdini process | Matched headless process |
| --- | ---: | ---: |
| Samples | 258 | 232 |
| OS peak working set | 1,201,971,200 bytes | 577,691,648 bytes |
| Peak sampled private commit | 4,147,159,040 bytes | 3,517,956,096 bytes |
| RTX 4090 device-wide initial usage | 2,230,386,688 bytes | 2,235,203,584 bytes |
| RTX 4090 peak sampled device-wide usage | 5,134,323,712 bytes | 5,122,691,072 bytes |
| RTX 4090 final sampled device-wide usage | 2,235,203,584 bytes | 2,240,512,000 bytes |

Working set and private commit cover the complete process, including Houdini
and driver/runtime overhead; private commit is not a physical RAM counter.
NVML process GPU bytes are unavailable on this WDDM configuration, as described
in the installed CUDA 12.8 `nvml.h`. Device-wide usage includes other applications
and must not be attributed to the renderer. Samples can miss transient GPU
peaks. The last sample does not establish post-session retained memory, and
these results do not yet certify physical GPU residency or sequence plateaus.

## Calibrated own-process GPU segment usage

`cycles_houdini.process_memory()` uses the public Windows DXGI interface inside
the calling process, overcoming NVML's unavailable process byte counter on
this configuration. It records local/nonlocal segment usage and budget for
node zero of every hardware adapter, with adapter names and boot-scoped LUIDs.
Failures retain their HRESULT/Win32 status. These are OS-reported process
usage counters, as defined by [Microsoft's QueryVideoMemoryInfo interface](https://learn.microsoft.com/en-us/windows/win32/api/dxgi1_4/nf-dxgi1_4-idxgiadapter3-queryvideomemoryinfo);
they are not per-session allocation counters or an exact residency census.

The independent `ch_memory_probe` dynamically uses the public CUDA driver API,
matches the CUDA/DXGI adapter by [device LUID](https://docs.nvidia.com/cuda/cuda-driver-api/cuda_driver_api/group__CUDA__DEVICE.html),
creates a context, allocates/touches/synchronizes 1,073,741,824 bytes, frees it
and destroys the context. RTX 4090 and RTX 3080 Ti each report exactly that
local segment increment in ten observations and zero local/nonlocal usage
after context destruction. The raw calibration, executable/source hashes and
device identities are in `artifacts/performance/dxgi-calibration/summary.json`.
This calibrates the counter path; it does not certify Cycles rendering on
the RTX 3080 Ti.

Build/run this development-only probe with explicit CUDA ordinals:

```powershell
./scripts/build.ps1 -Targets @('ch_memory_probe')
./build/windows-h22-release/bin/ch_memory_probe.exe 0 1073741824
./build/windows-h22-release/bin/ch_memory_probe.exe 1 1073741824
./scripts/run-hython.ps1 -HythonArguments @('tests/houdini_memory_smoke.py')
```

The telemetry smoke test passes eleven checks over three CPU and three
RTX 4090 session lifecycles: real GPU usage, CPU GPU usage remaining unchanged,
read-only queries, renderer retirement and preserved image/generation state.
Its close budget is 16 MiB of OS-reported local usage, with up to three seconds
for asynchronous driver retirement. The observed small sessions return to
their baseline. This does not establish long-sequence cache behavior.

The current benchmark's `--memory` option adds own-process snapshots at each
completed generation plus before session construction and immediately after
close. The million-triangle run `grid-comparison-20261010T131744` passes all
image/update checks. Native completed medians are 236.61/235.99/239.99 ms
for camera/transform/material versus 241.34/241.83/243.67 ms in the harness;
first-image p95 is 11.12/12.18/12.32 ms. Its memory evidence is:

| OS-reported RTX 4090 process counter | Native | Headless |
| --- | ---: | ---: |
| Completed-generation samples | 97 | 97 |
| Local segment warm minimum/maximum | 2,837,446,656 / 2,837,446,656 bytes | 2,837,446,656 / 2,837,446,656 bytes |
| Nonlocal segment warm minimum/maximum | 111,149,056 / 111,149,056 bytes | 111,149,056 / 111,149,056 bytes |
| Local/nonlocal before session | 0 / 0 bytes | 0 / 0 bytes |
| Local/nonlocal immediately after close | 0 / 0 bytes | 0 / 0 bytes |

All 90 warm edits retain constant reported process GPU segment usage in each
path, with zero reported on the other adapters. The parent's OS peak working
sets are 1,202,233,344 bytes native and 494,616,576 bytes headless. These values
include host, driver and benchmark bookkeeping. Completed-generation GPU
samples miss intermediate BVH/upload/render peaks; no exact physical peak or
arbitrary FX-sequence claim follows from this planar fixture.

The dedicated panel has an optional **Process memory** display sampled once
per second. It reports whole-Houdini working set, private commit and active
GPU process segment usage/budgets. Enabling it preserves the render generation
and shared session; the actual Qt panel test verifies that behavior.

## Remaining acceptance work

Native subdivision adds owned coarse snapshots and private tessellations.
Earlier uniform planar timings and memory plateaus predate these buffers.
Measure the control-mesh validation/conversion, OpenSubdiv refinement,
adaptive splitting, attribute interpolation, seam preparation, displacement,
BVH and rendering phases on the full small/medium/large workload matrix.
The current post-tessellation payload estimate excludes refiner temporaries,
subsequently generated tangents, containers, BVHs and physical GPU residency.
Cancellation during refiner construction and the conservative signed-index
precheck also need production-scale work; a small triangle budget does not
bound every temporary allocation.

The recorded million-triangle comparisons use the uniform material fixture.
Native VOP traversal is outside those measurements. Authored graphs add
`shader_graph_preparation_seconds` before cancellation/core mutation, included
in host `apply` latency but outside the existing completed-generation core
timer. Measure notification-to-image latency including Python network
evaluation before applying the interactive budget to arbitrary authored
materials. See [native material semantics](../user/native-materials.md).

Measure actual native-view/panel presentation, isolated path-tracing and
denoising timings, physical host/GPU memory, large deformation/topology/volume
edits, and the complete small/medium/large packed-RBD, particle, hair, pyro,
displacement and texture fixture matrix. Run RTX 3080 Ti and mixed devices
explicitly. Benchmark upstream texture caching with real textures before
changing its implementation. Direct Redshift comparison requires a verified
licensed and version-matched installation.

The native texture functional fixtures now exercise upstream caching disabled,
enabled with automatic `.tx` generation, and enabled without generation on CPU
and RTX 4090 OptiX. They record cold/warm call latency, derivative disk bytes,
logical device-buffer bytes and fixed-path replacement memory samples. The
small constant images demonstrate correctness and lifecycle behavior; they do
not justify enabling automatic generation by default or claiming a cache speed
advantage. Texture-heavy FX scenes, UDIM cache startup, frame sequences and
disk retention remain part of the production benchmark matrix.

## Retained shading: measured first delivered image, 2026-10-19

Patches 50-52 preserve physical/displaced geometry for independent shading-field
and same-height material changes. Profiling found that false emitter revisions
still rebuilt the light hierarchy despite retained tessellation. The preparation
path now avoids that rebuild and redundant BLAS/normal work; genuine geometry,
height, shader-face and emission-estimate changes retain their invalidation.
An independently reproduced normal-buffer relocation bug was fixed rather than
loosening image tolerances. See native-subdivision.md and native-attributes.md.

The benchmark uses RTX 4090 OptiX, two host threads, 960x540, sixteen samples,
four copies of one 1,036,800-triangle subdivided emissive prototype, and independent
constant object-space displacement. Eleven field states include three warmup
states and eight measured edits. No other verification job runs during timing.

| First delivered worker image | Before preparation fix | Final ordinary build |
| --- | --- | --- |
| Median (same upper-middle statistic) | 2333.204 ms | 186.433 ms |
| p95 / maximum, eight warm samples | 2343.892 ms | 245.577 ms |

This is about 12.5 times faster at the median in this fixture. It includes
Houdini cooking/capture, conversion, transport and engine work through delivery
of the first image. It excludes Qt presentation and physical-input latency;
eight samples do not certify the general interaction budgets. The fixture is
static geometry with changing shading fields, not a topology/deformation test.

Both displacement/tessellation counts remain one, geometry/variant resources
remain one, objects remain four and tracked owned geometry stays 99,648,880
bytes. Warm emitter records report cache hits without misses. No temporary
OptiX vertex/index uploads are requested. Close retires tracked resources.
Twenty-one separate camera, transform, geometry and material updates also pass
the retained-emitter regression, including actual emission strength changes.

Before run: prototype-worker-CUDA_NVIDIA_GeForce_RTX_4090_0000_03_00_OptiX-20261019T063619430.
Final run: prototype-worker-CUDA_NVIDIA_GeForce_RTX_4090_0000_03_00_OptiX-20261019T074615215.
Exact manifests, diagnostics and images are indexed by
artifacts/mesh-shading-reuse-evidence-20261019.json. The final ordinary build
uses hosts/20261019T073430395/resources with instrumentation disabled.

Further work remains on host/wire field-only payloads, global emitter array
uploads, notification-to-paint measurements, production geometry changes,
1080p/4K display, prolonged physical-memory use and mixed devices. Keep the
acceptance and release requirements above active.
