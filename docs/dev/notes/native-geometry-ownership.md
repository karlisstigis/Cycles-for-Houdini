# Geometry ownership before background conversion

Artist IPR now uses the bounded asynchronous capture pipeline described in
[native asynchronous capture](native-async-capture.md). Cooking, host parameter
evaluation and supported packed-source acquisition stay on Houdini's host thread;
retained root classification, material-binding scans and geometry conversion run
in the background. The sections below preserve the preceding ownership experiments
and their limitations at those checkpoints.

## Evidence motivating the change

The GPU-display worker benchmark
`prototype-sync-CUDA_NVIDIA_GeForce_RTX_4090_0000_03_00_OptiX-20261012T201643739`
records approximately 37–41 ms in mixed-component signatures on curve-width,
point-radius and volume-transform edits. Those entire host applications take
99–107 ms in their first trials. A topology trial spends 56 ms in geometry
conversion and 121 ms in total host application. Its SOP cook is already warm.
These figures distinguish conversion/checking from cooking; they do not justify
moving node evaluation to an arbitrary background thread.

The installed Houdini 22.0.429 SDK documents that a plain `GU_DetailHandle`
protects lifetime but SOPs may reuse its geometry. A preserve request prevents
that reuse. The legacy read/write handle locks are not an enforced substitute.
The SDK also supplies `duplicateGeometry(GA_DATA_ID_CLONE)`, which retains data
identifiers in an independently owned detail. Houdini parameter/node evaluation
has separate thread-safety constraints. The implementation must respect all
three distinctions.

## Implemented ownership experiment

`PreservedDetail` balances one preserve request with RAII. The internal native
`preserve_geometry` entry point enforces acquisition on Houdini's main thread.
It supports preserved-handle and copied-detail experiments. These are test
candidates, not additional artist execution modes or a public SDK.

`convert_preserved_mesh` releases the Python GIL and invokes the production mesh
converter using a uniform material assignment. It never evaluates a node or
calls a Python material resolver. Its digest covers positions, triangles,
corner normals, UVs and colors; independently extracted HOM positions check the
expected point count, first position and position sums. Its conversion timer
also includes the digest and geometry-memory observation, so it is not a pure
converter microbenchmark.

`houdini_geometry_lease_probe.py` builds 721×721 points with native Grid, Wrangle
and Transform SOPs: 1,036,800 triangles. A background reader repeatedly converts
the original while the GUI moves the source, changes topology and deletes its
object. It verifies conversion after deletion, rejection of background node
access, preserved data identifiers and release of every lease. Two repetitions
each compare ordinary editing, preserved handles and copied details.

The first frozen run,
`prototype-sync-CPU-20261012T205209718`, passes retained conversion and deletion
checks against manifest
`53931d32bccb058c38ca88c505e74b921043876b792ebb468f171ab9131f72ce`.
Acquisition is approximately 0.001 ms for a preserved handle and 0.085–0.091 ms
for a copied detail. Median edit/cook time is 0.74–0.81 ms in the baseline,
0.87–1.04 ms with preservation, and 0.81–0.86 ms with a copy. Maximum edit/cook
time is approximately 10–12 ms in all three cases. Background conversion plus
digest takes approximately 109–120 ms. A copied detail is therefore a viable
candidate; the measured acquisition cost does not justify rejecting it as an
expensive full geometry duplication.

The GUI timer records maximum gaps of 97–286 ms even in baseline cases. These
measurements include host event processing and do not establish stutter-free
interaction, a rendering improvement, or an absence of memory growth. The
experiment has no Cycles render session or GPU scene. Complete provenance,
subsequent guard/lifetime checks and final-package checks are indexed in
`artifacts/stage1/geometry-ownership/verification-summary.json`.

## Required integration boundary

Native material conversion now reads an immutable map of authored paths to
owned material identities. The host adapter prepares it from effective component
bindings; deformation capture freezes the interned material candidates of each
shutter sample independently, then validates effective assignments in conversion.
Converters fail if a required frozen binding is missing, and live material node
resolution explicitly rejects background callers. This preserves packed fallback
propagation, unused-fallback behavior, saved material identity and animated
assignment validation without hiding node access in a bulk converter.

The extended ownership probe retains a million-triangle mesh with two native
materials, including an `op:` path. During background conversion the host renames,
deletes and recreates both material nodes, then deletes the geometry. Frozen
conversion remains identical, fresh capture resolves different lifetime keys and
all leases are released. Both preserved and copied details pass. Combined native
partition/binding preparation takes about 67 ms in this cold probe, unlike the
approximately 0.09 ms geometry-copy acquisition alone. This is ownership evidence,
not proof that the complete host preparation is cheap. The frozen report is
`prototype-sync-CPU-20261012T211719920`; material assignment, strand, volume,
mixed-motion and deformation regressions also pass.

## Frozen deformation samples

The production deformation adapter now separates host acquisition from pure
conversion. `prepare_deformation_set` cooks the requested shutter samples in the
supported host context, retains private geometry copies, freezes material
identities, and restores the nominal SOP cook. `convert_deformation_set` reads
only the retained samples and owned requests. It partitions the geometry,
validates assignments, converts meshes/points/curves, and checks their motion
correspondence without consulting live nodes. The existing synchronous capture
wrapper calls both stages in order, so artist scheduling is unchanged.

Candidate collection reads interned material strings instead of walking every
primitive on the host thread. A missing or incompatible candidate retains its
diagnostic as data. Conversion raises that diagnostic only if the binding is
actually used. An invalid unused OBJ fallback therefore does not reject a fully
assigned mesh, and an empty primitive binding still inherits its detail/OBJ
fallback. This freezes identity only; shader graph definitions still require
their own host acquisition boundary.

`houdini_deformation_ownership_probe.py` combines the million-triangle native
grid with one curve and loose points. Independent positions and shutter timing
are checked for every component. Background conversion remains identical during
20 host frame edits, a topology change, material rename, and deletion of the
geometry and material nodes. Conversion also completes after deletion, and no
leases remain. Background attempts to acquire live SOP samples are rejected.

The first split still scanned each sample during host preparation. Its report
`prototype-sync-CPU-20261012T212828396` records 378 ms preparation, including
4.0 ms cooking. After moving those scans into conversion,
`prototype-sync-CPU-20261012T213659356` records 155 ms total preparation; actual
three-sample acquisition takes 3.35 ms, including 1.85 ms cooking. The remainder
includes initial component discovery in the diagnostic entry point. The latter
run records two background conversions, both finishing after deletion, and a
maximum host edit/cook duration of 26.7 ms.

These are individual ownership probes, not a matched rendering benchmark.
The probe's initial synchronous reference conversion produces a 1.82-second
GUI timer gap. Moving work between stages does not remove its CPU or memory
cost, and this result does not establish responsive artist IPR. Frozen evidence,
including the failed first material fixture and its correction, is recorded in
`artifacts/stage1/geometry-ownership/deformation-verification-summary.json`.
Focused material, mixed-motion, deformation and subdivision renders pass.

## Frozen VDB grids

Native volume capture now uses `prepare_volume` and `convert_volume`. Preparation
runs on the host thread because Houdini's const grid accessor can synchronize
the grid transform from its primitive anchor. It creates a private grid header
with copied metadata and a copied transform, retaining a separate tree reference.
A bare grid pointer would not provide this isolation, and OpenVDB's `copyGrid`
alone also shares the transform. The installed SDK documents these contracts in
`GEO/GEO_PrimVDB.h` and `openvdb/Grid.h`.

Supported Houdini writes call `makeGridUnique` before changing a shared tree.
That permits the converter, Cycles and worker serializer to read the frozen grid
without duplicating the entire tree again. The production adapter now removes
that redundant deep copy. Field normalization retains ordinary scalar/vector
grids and allocates new storage only when merging scalar velocity components.
Cycles' const `VDBImageLoader`, the NanoVDB conversion path and the worker's
const-grid serialization were audited for this use. This does not remove
NanoVDB conversion, GPU upload or interprocess volume serialization.

`houdini_volume_ownership_probe.py` builds 1,030,301 active voxels with native
Box, VDB From Polygons, Volume Wrangle, Transform and Attribute Wrangle SOPs.
It compares ordinary editing with retained conversion during eight density and
transform edits, a field rename and source deletion. The frozen fingerprint
covers grid metadata, transform, active/inactive values and sparse topology.
Independent voxel/transform checks establish the initial state, and a fresh
capture must see the edited values/name/position while retained conversion
continues to see the original. Background OP access is rejected and every lease
is released.

The retained-tree run `prototype-sync-CPU-20261012T215230582` passes: eight
background conversions complete, six after deletion. Frozen acquisition takes
0.016 ms and scalar normalization 0.009 ms in its initial reference. Mean host
edit/cook durations are 11.6 ms without retention and 11.4 ms with retention;
these individual runs do not establish a speedup or general cook-cost guarantee.
The preceding version still deep-copied the tree during conversion and measured
about 1 ms for that stage. Full content hashing takes much longer and is solely
test work, excluded from the converter timer and production path.

CPU/OptiX scalar-volume, velocity-motion, pyro, named-field, mixed-FX and volume
material regressions pass on the retained-tree build. Evidence is indexed in
`artifacts/stage1/geometry-ownership/volume-verification-summary.json`.
The synchronous adapter uses the same prepare/convert functions; the complete
artist scene capture has not yet moved to a background job. Volume resource byte
counts describe retained grids, not uniquely allocated process memory when trees
are shared with SOPs.

## Frozen packed prototypes

`prepare_packed_instances` now resolves packed details on the host thread and
captures each instance's identity, transform and inherited material path. A
transaction retains each original detail once through a const preserved handle.
Keeping the original detail identity preserves instance sharing and existing
geometry-variant cache keys. The production scene adapter uses this acquisition
helper; its complete capture still runs synchronously.

`houdini_packed_ownership_probe.py` checks the same helper with a native
1,036,800-triangle mesh, 16 instances through nested packs, two material bindings,
and rotated, nonuniformly scaled outer transforms. Independent authored-transform
checks validate world positions and bounds. Conversion on a background thread
continues to see the original data during eight deformation/transform edits, a
topology change, material rename/recreation, and deletion of the source nodes.
A fresh capture must see the changed geometry and bindings. Background packed
resolution is rejected before node lookup.

The initial run `prototype-sync-CPU-20261012T221000161` passes with one mesh
prototype, two material variants and four retained details including containers.
Acquisition takes 0.103 ms after a 112.5 ms initial cook. Eight background
conversions finish, four after source deletion, and all leases are released.
Observed host edit/cook times range from 1.24 to 12.86 ms. These are ownership
observations, not a matched IPR performance comparison. The private probe covers
mesh leaves; it does not certify arbitrary packed procedurals, file reloads or
mixed packed volume/curve leaves. Existing packed, motion, material, mixed-FX and
subdivision renders pass. The mesh and deformation ownership probes also pass
after the const-handle change. Frozen reports are indexed in
`artifacts/stage1/geometry-ownership/packed-verification-summary.json`.

## Frozen native shader graphs

Native assignment acquisition now calls `prepare_native_material_graphs` before
bulk geometry conversion. It resolves each distinct material identity once per
capture into owned `MaterialData`/`ShaderGraphData`, including evaluated animated
values, flattened subnet/ramp nodes, links, volume/displacement outputs, pass
index and Cryptomatte name. The table retains no Python callback, dictionary or
Houdini node. The pure `select_native_material_graphs` stage selects only effective
bindings after conversion; it never falls back to live lookup.

Preparation retains diagnostics for invalid candidate graphs until selection.
This matters for shutter samples and interned material strings: an unused invalid
fallback must not reject correctly assigned geometry. Shader parameter evaluation
can itself cause a SOP cook, so the production adapter now preserves the acquired
root detail while graph callbacks run. Packed prototypes and deformation samples
already retain their own protected handles.

`houdini_material_graph_ownership_probe.py` freezes animated surface/volume graphs
at an explicit time, including a color ramp inside a subnet and true displacement.
It checks alias deduplication, callback release, isolation from mutated Python
dictionaries, and deferred invalid/missing candidates. Concurrent background
selection and worker-wire round trips remain identical during parameter edits,
rewiring, material rename/deletion/recreation. Background live acquisition is
rejected. After deletion, an independent render must produce RGB
`(0.75, 0.45, 0.675)` and Z displacement `0.3`; CPU and OptiX pass with no retained
leases. The initial CPU run records 5,362 background reads, including 14 after
deletion. This is an ownership check, not a latency benchmark.

`houdini_material_acquisition_smoke.py` checks the production call order and
deliberately recooks a SOP from a material callback. The current capture retains
the originally acquired geometry; the next capture sees the edited state. An
invalid graph on an unused deformation fallback also renders successfully.
CPU/OptiX checks pass, as do native assignments, subnet/ramp lowering, strand and
volume materials, mixed motion, deformation, displacement and subdivision.
Evidence is indexed in `artifacts/stage1/geometry-ownership/material-graph-verification-summary.json`.

This freezes graph values and resource names, not texture file contents or an
in-memory COP image. Existing texture header/color-policy acquisition and file
change detection remain separate. `op:` textures still require their own image
adapter. Artist scene capture continues to run synchronously; no callback is
being moved to an arbitrary background thread by this increment.

## Complete native geometry acquisition and conversion

`NativeSceneAdapter::prepare` now performs the host operations for a complete
geometry transaction: SOP cooking, packed resolution and transforms, binding
resolution, shader callbacks, VDB header acquisition, and shutter sampling.
Its opaque `PreparedScene` retains the root details and packed sources and owns
component selections, requests, material identities, volume headers, object
transforms, and deformation samples. It retains no SOP pointer or host callback.
Instances and material variants share one immutable selection set per retained
source, avoiding copies of large primitive/point offset lists for each instance.

`NativeSceneAdapter::convert` consumes that transaction once. Mesh conversion,
triangulation, mixed-component comparisons, point/curve conversion and deformation
correspondence operate on those retained inputs. VDB normalization uses the
prepared grids. Conversion does not perform OP lookup, cook a SOP, resolve a
packed primitive or evaluate Python material graphs. The adapter owns its caches
exclusively: no other adapter operation may overlap conversion. Preparation
serials reject stale or duplicate consumption; invalidation retires that serial.
The existing artist and ordered final paths currently use the synchronous
`capture` wrapper around these same two production stages.

`houdini_scene_ownership_probe.py` exercises the complete adapter off-thread with
1,038,600 triangles, nested packed prototypes containing meshes, curves, points
and a VDB, native materials, packed/object motion and a separate sampled deforming
mesh. It verifies independently specified resource/instance counts and tests
eight native SOP edits, a topology change, material rename and source deletion.
Fresh acquisition sees the new topology. A retained conversion completed after
deletion renders the original surface, volume, motion and data passes against
the pre-edit reference. That comparison checks retention; the separate feature
fixtures establish conversion correctness independently.

The initial OptiX run `prototype-sync-CUDA_NVIDIA_GeForce_RTX_4090_0000_03_00_OptiX-20261012T225445591`
passes with zero retained leases. Four preparations take 108–113 ms, including
already warm SOP acquisition; background conversion takes 181–205 ms. Native
edit/cook calls during the probe take 30–44 ms. Beauty/depth/UV/position match the
reference exactly and the largest normal difference is approximately 1.2e-7.
These are ownership measurements, not artist IPR latency or a general speedup.
All four preparations deliberately use separate cold adapter caches.

The diagnostic uses an already owned camera/color/texture-policy baseline. It
does not implement fresh texture-policy acquisition on a background thread.
It covers the in-memory packed mixed leaves described above; arbitrary custom
packed procedurals and external-file reload behavior need separate fixtures.
Evidence is indexed in `artifacts/stage1/geometry-ownership/scene-verification-summary.json`.

During regression, the existing 16-sample worker comparison produced beauty RMSE
0.006118 against its 0.005 limit. Depth and position were identical; 99% of RGB
differences were below 5e-7, with a few bright outliers dominating the error.
An unchanged repeat produced RMSE 3.5e-8 and passed the full worker lifecycle.
The exact source of that render variability is not established by these runs.
The comparison now uses 128 samples with the same beauty tolerance and additional
explicit depth/position checks. The original failure and its EXRs remain in the
evidence; it is not reported as a passed run.

## Integration requirements established by these experiments

The subsequent asynchronous implementation follows these requirements:

1. In the supported host context, evaluate object/camera parameters, cook the
   required SOP/shutter samples, acquire immutable geometry, resolve packed
   sources, and freeze material identities and definitions. Keep original source
   identity alongside snapshot identity; private copies have distinct detail IDs.
2. Convert only those frozen inputs off-thread. Mesh/curve/point partitioning,
   component comparisons and triangulation belong here where their dependencies
   permit. No OP lookup or material callback may be hidden inside a converter.
3. Keep a bounded active job and newest pending request. Apply completed results
   only to their own scene generation. Old results must not be relabelled as
   newer edits. Cache mutations need one owner and explicit acceptance/retirement.
4. Preserve native material failures, deletion, Stop, scene reload and worker
   recovery semantics. Final ordered rendering and IPR may share conversion code
   while using their appropriate scheduling policies.

The asynchronous artist implementation is now enabled. Root and nominal packed
prototype partition/material-binding scans run in its background acquisition
stage. Supported packed-source resolution remains on the host, interleaved with
background scans of each acquired packing level. See the heavy packed follow-up
in [asynchronous capture](native-async-capture.md). Instance transforms, procedural
loading and extra shutter samples remain profiling targets. Houdini's own cooking
costs are not eliminated by renderer scheduling.
Frozen deformation covers unpacked meshes, points and supported curves; VDB
ownership covers supported shared-tree edits separately, and packed ownership
covers nested mixed instances through the full adapter. Native material graphs have separate ownership
coverage; texture-policy acquisition must also fit the complete job boundary. Test
both acquisition and subsequent cooking costs on the existing heavy five-frame
and continuous-edit fixtures; otherwise a fast snapshot can merely defer a copy
to the next cook. Reuse caches by validated source/data identities, not transient
copied-detail pointers.

Primary sources:
[GU_DetailHandle](https://www.sidefx.com/docs/hdk/class_g_u___detail_handle.html),
[HDK thread safety](https://www.sidefx.com/docs/hdk/_h_d_k__node_intro__thread_safety.html).

## Velocity blur with authored normals - October 18, 2026

Choose Velocity in Geometry Motion on the ROP or its optional object override.
Finite point/vertex N values no longer disable mesh velocity blur. Positions use
requested-frame point v, while authored normals are held in object space across
the same shutter steps. Independent object/packed transforms still apply. No
normal derivative is inferred from velocity; use Deformation for explicitly
animated authored normal values. This retains the distinction between velocity
and recooked deformation described in SideFX's
[motion blur documentation](https://www.sidefx.com/docs/houdini/render/blur.html).

The native converter supplies owned corner-normal arrays for every non-centre
position step because Cycles motion triangles index those arrays directly.
The strict core requirement for matching normal/position samples is unchanged.
No SOP shutter cook or deformation identity matching is introduced. Generated
normals retain the engine's existing geometry-derived motion behavior.

Invalid authored normals recover to generated normals without dropping valid
velocity blur. Missing velocity retains requested-frame geometry and authored
shading. Repairs restore the intended result and clear diagnostics. Packed
prototypes share their compatible complete rendering state; no user IDs are
required for velocity correspondence, including topology changes between frames.

CPU, RTX 4090 and RTX 3080 Ti each pass 37 checks against independent motion
images and normal expectations. They cover point/vertex normals, 3/5 samples,
offset shutters, shader displacement, nonuniform velocity, packed repair/sharing,
camera reuse and subdivision. Each device writes five topology-changing packed
frames through one Persistent Data session, with invalid normals on frame three
and repair afterward. All tested velocity captures report zero SOP shutter cooks.

Both heavy single-viewer Qt/HOM workflows pass eight events on RTX 4090 while
retaining a million-triangle prototype shared by twelve instances. Native IPR
can publish an accepted interim image during a group of edits; the test records
it and requires convergence to the intended image without refresh/restart.
Native image pixels are checked independently of the Qt grab, whose toolbar may
contain capture artifacts. Physical mouse, memory and production latency are
not certified. Source-specific reports, failures and exact source archives are
in the evidence index (`artifacts/verification/velocity-normals-20261018/index.json`).

Independent Material SOP binding/literal sampling now retains the selected
Velocity/Transform geometry policy. It acquires only off-frame binding/packed
metadata and restores a single nominal ownership ledger; warm navigation reuses
it. Time-dependent bindings can introduce additional host shutter cooks.
See [material override semantics and evidence](../../user/native-material-overrides.md).
Broader nominal-coordinate recovery and production-scale memory remain open.

## Velocity mode without shutter cooks; prototype identity across recooks - November 14, 2026

Task M1. Velocity mode (`NativeGeometryMotion::Velocity`) follows Karma and
Redshift: point `v` (SOP units per second, SideFX
[motion blur](https://www.sidefx.com/docs/houdini/render/blur.html)) alone
defines the blur and no SOP is cooked at another shutter time.

* Packed primitives and point instances (synthesized packed primitives) take the
  `v` of their packed point and, when present, `w`: an angular velocity vector in
  radians per second (same SideFX page), applied right-handed about `w/|w|` and
  about the packed point position, the pivot (as USD's PointInstancer rotates an
  instance about its prototype origin before translating it). Both are in the
  space of the detail holding the packed point; each nested level applies its own
  (`scene_adapter.cpp` `packed_velocity_samples`). `dt` is each step's offset from
  the frame, `NativeMotionWindow::deltas` (shutter and Shutter Offset included).
  All velocity positions go through `velocity_position`, the single place to add a
  `0.5 * accel * dt^2` term.
* Instance transforms keep the ROP's motion samples. Cycles decomposes each step
  and slerps neighbouring steps along the shorter arc
  (`util/transform.cpp transform_motion_decompose`), so half a turn or more
  between two steps is rejected with `motion.packed_velocity`. The decomposed
  translation is interpolated linearly: a pivot away from the prototype origin
  follows chords between steps.
* Unpacked points and curves publish three position steps (open, centre, close),
  as Blender's `attr_create_motion_from_velocity`; meshes keep the configured
  count because shutter-sampled displacement needs matching geometry steps
  (`core/session_validation.cpp`).
* Instance fields (`assemble_instance_attributes`) are held at the frame: the
  instance-field shutter acquisition, the only SOP cooks of a velocity capture,
  no longer runs for velocity requests.
* Linked Blender packed rows (`packed_blender`) are unchanged and take no `w`.

Leaf packed prototypes are content-addressed (`canonical_prototype`,
`prototype_content`): their geometry key uses the GU detail id of the first
detail seen with the same contents (all public tuple attributes of every owner,
polygon/particle topology, the `subdivision_hole` group; arrays and dictionaries
by name only; any other primitive type or a `material_stylesheet` keeps the
detail's own id). A recook of the SOP at shutter times, or a new frame, gives
identical prototypes new detail ids; they now keep their key, and the converter
reuses their geometry through the full-detail component signature. Entries
unused for two captures are dropped.

Shutter discovery for Blender references (`instancing.ExternalPlan._discover`)
reads geometry at other times only when the frame has a Blender reference;
reading it recooked deformation-mode SOPs at shutter times on every capture and
defeated deformation reuse.

Threads benchmark (387,713 points, 20,783 packed curves, 5 samples, hython):
in velocity mode `motion_cooks` 8 to 0; the capture after a render or frame
change sent 20,783 objects and 6 prototypes again and now sends none, in all
three modes; a frame change is a pose update of the instances instead of a full
update. Images: `houdini_velocity_motion_smoke.py` compares each velocity render
with the same geometry actually moving (deformation/transform mode).

## Deformation modes: Acceleration and Velocity if Available - November 14, 2026

Task M2. The ROP parameter `geometry_motion` (label **Deformation**) and the
object parameter `cycles_geometry_motion` keep their names and saved tokens;
only labels changed: `transform` **Off**, `velocity` **Velocity**,
`acceleration` **Acceleration** (new), `deformation` **Geometry Samples**,
`velocity_if_available` **Velocity if Available** (new); the object adds
`inherit` **From ROP**. The object template version is 22, so `install`
refreshes older tabs' labels while Houdini keeps their values.
`NativeGeometryMotion` gained `Acceleration` and `VelocityIfAvailable`;
`attribute_motion()` is true for Velocity and Acceleration, and every former
`== Velocity` test uses it, so Acceleration takes exactly the Velocity paths
(no shutter cook, fields held at the frame, packed leaves expanded on the
converter).

* Acceleration reads point `accel` beside `v` in `capture_mesh`,
  `capture_points`, `capture_curves` (Bezier/NURBS samples interpolate it as
  `v`), the packed preparation (`NativePackedInstance::acceleration`) and
  external direct placements. `velocity_position` adds `0.5 * accel * dt^2`.
  Points and curves with `accel` publish every configured step, not the
  three linear ones; meshes always did, so displacement step counts still
  match (`core/session_validation.cpp`). Without `accel` (or with an `accel`
  that is not a float vector, which warns) the output is byte-identical to
  Velocity. Volumes keep their velocity field and ignore `accel`.
* Velocity if Available never reaches conversion. At the start of
  `NativeSceneAdapter::prepare`, each top-level request with it reads its
  frame detail (the prewarmed root, or the SOP's frame cook that the capture
  then reuses from Houdini's cache) and becomes Velocity when the top level
  carries point `v`, point `w` where the output holds packed primitives or is
  an Instance OBJ's template points, or a `vel`/`velocity` volume; otherwise
  Deformation. The granularity is the SOP output (one OBJ request): point
  attributes exist for every point of a detail, so a finer split inside one
  output would only differ for zero `v`, and packed contents and point
  prototypes copy the outer request (`point_prototype_request`), so they
  follow the outer decision. Python discovery treats it like Deformation
  (shutter reads only for Blender references).
* Requests carry `motion_explicit` (the object's own choice, not From ROP).
  The same pre-pass reports one `motion.missing_velocity` warning per capture
  and object for an explicit Velocity or Acceleration without `v`/`w`/velocity
  field, and an `info` `motion.missing_acceleration` for an explicit
  Acceleration with `v` but no `accel`. Inherited modes stay silent so static
  scenery needs no `v`.
* Houdini's `geo_velocityblur` stays unused. Unchanged: time-dependent
  material bindings may still cook at shutter times in Velocity and
  Acceleration; a spin of half a turn or more between two steps drops that
  instance's packed velocity with `motion.packed_velocity`.
* The renderer worker protocol is unchanged: only host-side `GeometryRequest`
  gained a field; `SceneUpdate` keeps its shape.
