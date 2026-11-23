# Native polygon subdivision

This development slice implements Linear and OpenSubdiv Catmull–Clark
subdivision for the native OBJ/SOP adapter. It preserves the original polygon
control mesh, including individual polygon-soup faces, and tessellates it inside
the pinned Cycles engine. CPU and this workstation's RTX 4090 OptiX configuration
have targeted correctness and native IPR evidence. Large-scene performance,
physical-memory budgets and production certification remain open.

Unusable native control topology now falls back to triangulated polygons with a
source diagnostic, preserving usable geometry. Winding, nonmanifold-edge and
repeated-point repairs restore subdivision during IPR. Geometry Random Per Island
also follows the coarse mesh through tessellation and displacement seams. See
[current mesh topology recovery and evidence](../dev/notes/native-mesh-islands.md).

## Use it in Houdini

Load the development package built by `scripts/build.ps1`. On a **Cycles Native
IPR** ROP, select the SOP, render camera and device, then choose **Polygon
subdivision**. Assign a native Cycles material through the OBJ or geometry, or
use the ROP's material override. Choose either the native IPR viewer or the
**Cycles Render** Python Panel; both consume this ROP.

For per-object controls, select geometry and click **Add Object Tab** on the
**Cycles Tools** shelf. The optional native tab exposes fixed IPR/final levels and adaptive
settings. Fixed subdivision is camera-independent and level zero disables it;
the ROP remains the fallback when the object override is off. See
[optional object properties](native-object-properties.md).

The editable example is
`artifacts/stage2/subdivision/native-subdivision.hiplc`. Its original six-face
cube can be changed between Linear and Catmull–Clark while rendering. The
example has explicitly authored Emission strength 1 (also the node's default
since November 14, 2026; it was 10 before). The `.hiplc` reflects the available Houdini Indie license.

| Control | Meaning |
| --- | --- |
| Polygon subdivision | None retains the existing triangulated path. Linear creates bilinear patches. Catmull–Clark evaluates the original control faces with OpenSubdiv. |
| Subdivision dicing space | Pixels adapts to the full render camera. Object space uses distances in the prototype's own coordinates. |
| Subdivision edge length | Target edge length in the selected units. Smaller values request more triangles. The minimum accepted value is 0.0001. |
| Maximum subdivision level | Limits adaptive edge refinement, from 1 to 12. Additional indexing validation can reject a control-mesh/level combination. |
| Triangle budget per variant | Limits actual generated triangles for each private tessellation. Default 4,000,000; accepted range 1–100,000,000. Exceeding the budget fails visibly and preserves the last complete image. |
| Boundary interpolation | Edges only or Edges and corners for open Catmull–Clark surfaces. None requires a closed control mesh; native open meshes use Edges and Corners with a diagnostic. |
| Linear face-varying interpolation | The six OpenSubdiv policies from smooth through bilinear, applied to corner UV and Cd. Attribute seams retain their separate values. |

The maximum level and triangle budget apply together. A triangle count is not a
physical-memory limit: attributes, motion steps, retained control data, BVHs,
OpenSubdiv temporary data and the number of instance variants also consume
memory. The current guard does not interrupt OpenSubdiv's refiner construction
internally. Large control meshes and cancellation latency still need measured
acceptance tests.

## Geometry and attributes

Closed polygon primitives and polygon soups retain their original face corner
order. The non-subdivided converter continues to triangulate its owned detail
copy; subdivision bypasses that triangulation. Face metadata records the host
primitive and the polygon within a soup. Material bindings are resolved before
tessellation on those original faces.

UV uses float2 storage. Cd uses packed float3 storage. Vertex attributes take
precedence over point attributes; point values are copied to the corresponding
coarse corners. Equal corner values at the same control point can share an
OpenSubdiv face-varying value, while different values retain a seam. This is an
explicit Cycles policy, not a claim that every Houdini Subdivide SOP attribute
policy is equivalent.

Catmull–Clark normals come from derivatives of the limit surface. Adaptive
sources retain authored point or vertex N for the original-polygon preview when
updates are disabled. Tessellated Catmull–Clark uses limit-surface normals.
Linear subdivision can interpolate authored corner normals and their supported
motion samples.

Houdini vertex `creaseweight` sets the outgoing edge's sharpness. Primitive
`creaseweight` is the fallback when that vertex attribute is absent. Shared
edges use the maximum contributing weight. Point `cornerweight` supplies vertex
sharpness. Values must be finite and nonnegative; values above 10 saturate at
10. The integration converts this host sharpness to Cycles' normalized storage,
whose OpenSubdiv adapter multiplies it by 10. Sharpness 1 and 10 have separate
tests; a fully sharp cube alone would not establish the correct scale.

Non-manifold edges, repeated vertices within a face and inconsistent shared-edge
orientation are rejected. A nonempty `subdivision_hole` group and the five
`osd_*` detail overrides require further adapters. They now generate recoverable
source diagnostics: hole faces remain present and configured Cycles subdivision
settings remain in effect. Removing the metadata clears its diagnostic; disabled
subdivision ignores this unused metadata quietly.
Open-boundary Catmull–Clark with boundary interpolation None is rejected because
the pinned evaluation path dereferences a missing patch without an integration
hole policy. These limitations are not production feature parity.

Reference conventions: [SideFX Subdivide SOP](https://www.sidefx.com/docs/houdini/nodes/sop/subdivide.html)
and [SideFX subdivision rendering](https://www.sidefx.com/docs/houdini/render/subdivision.html).

## Updates, instances and displacement

**Rendering > IPR Settings > Adaptive Subdivision Update** enables automatic adaptive
tessellation updates by default. The **Cycles Render** panel exposes the same
setting as a square-and-circle icon, followed by a refresh arrow in one compact
wrapper. Disable updates to retain the current tessellation through camera and
object movement. **Refresh Tessellation** recalculates once for the current
scene, dicing camera and object transforms without changing the toggle.

With updates off and no cached tessellation, a mesh renders its original control
polygons with their original authored normals. This path triangulates concave faces and remaps their shading corners;
it does not invoke OpenSubdiv or adaptive dicing. A source edit that invalidates
cached geometry also displays original polygons until refreshed or updates are
enabled. Camera rays, object transforms, lighting and surface shading remain
live. Uniform subdivision is independent of this adaptive-only control.

The cache and one-shot refresh revision belong to the current IPR session.
Starting a new IPR with updates off displays the original mesh, including after
a refresh in a previous session. Either Interactive or Final viewer sampling
can use this policy. Disk renders always calculate current subdivision.

Regression coverage is in `tests/houdini_tessellation_update_smoke.py`,
`tests/houdini_tessellation_update_ipr_gui_smoke.py`, and
`tests/houdini_dicing_disk_smoke.py`. Reports are in
`artifacts/tessellation-update`.

The object Subdivision panel starts with **Enable in IPR**, enabled by default.
Turn it off to render the original polygons in **Cycles Render** and native IPR,
for both uniform and adaptive subdivision, including packed instances. Disk
renders continue using Levels Render or the adaptive render settings. The
Subdivision header still controls whether the object overrides the ROP defaults.
For objects with an existing Cycles tab, run **Cycles Tools > Add Object
Parameters** to refresh the tab while retaining authored values and expressions.

The core owns a deep copy of the control topology, positions, corner attributes,
creases and supported motion data. Re-tessellation and displacement always
start from that copy. Render threads do not dereference live SOP data.

Subdivision settings and crease attribute data identifiers participate in the
native resource key and invalidation. Camera-only changes retain the captured
control data. Object-space camera edits retain tessellation as well. Pixel-space
camera edits redice compatible variants without converting the SOP again. An
identical camera supplied during a surface-only material edit is compared
semantically and does not force another tessellation. Crop changes keep the
full-camera dicing policy.

Object-space instances share compatible tessellations. Pixel-space instances
with different centre transforms receive private variants because their screen
sizes and locations can differ. Displacement compatibility also participates in
the variant key. This conservative policy may create more variants than a future
measured sharing policy. It does not imply arbitrary per-instance shading can
share one engine geometry allocation.

True displacement splits generated render vertices at material and required
attribute seams **after** subdivision. Splitting the coarse control vertices
would change the Catmull–Clark surface. All generated vertex attributes and
supported motion steps are remapped together; corner attributes retain triangle
corner addressing. The current seam key can split near-equal floating-point
values, so dense attribute/displacement workloads still require memory profiling.

Stable coarse face connectivity can carry sampled deformation. Oriented faces
are matched through stable point identities, independently of cyclic starting
corners and face order. Changing faces, bindings, crease state or correspondence
across the shutter is an unsupported correspondence. Strict low-level requests
fail before mutation; native capture recovers usable requested-frame geometry.
Mesh UV/Cd now carry aligned samples in Deformation mode, including smooth
face-varying refinement at each stored state. Other named attributes remain a
separate adapter task. Supported displacement evaluates the generated
shutter poses with owned shader snapshots; broader dependencies remain tracked in
`docs/native-displacement.md`. Linear subdivision now preserves authored corner
normals and their motion data. Generated normal writes tolerate the absence of a
vertex-normal array, and the post-tessellation object cache refreshes its triangle
count before indexing motion corner normals. Independent CPU/OptiX image tests
cover changing authored normals as well as animated shader displacement.

## Diagnostics and failure recovery

Session status reports logical control face count, generated vertices/triangles
across private variants, cumulative tessellations, variant count, retained source
bytes and tracked engine allocation bytes. A post-tessellation callback publishes
counts under a lock; status reads do not inspect engine arrays while they change.

Subdivision payload byte estimates cover the control and generated arrays at
that callback. They omit container overhead, temporary refiner data, subsequent
tangent generation, BVHs and physical GPU residency. Existing performance reports
that predate source ownership and subdivision cannot certify this path's memory
cost.

If a render is cancelled or the tessellation guard fails before completing,
its unfinished geometry is reseeded from owned control data for recovery. The
guard is checked before allocating each subpatch's interior grid. Splitting,
dicing and interpolation honor cancellation at the added synchronization points;
OpenSubdiv refinement itself remains the documented latency gap. Closing the
session retires its control snapshots, variants, generated arrays and device
buffers.

## Verification

`tests/houdini_subdivision_smoke.py` covers 27 native groups on each of CPU and
RTX 4090 OptiX, plus three animated native SOHO batch frames. It includes original
quads/cubes/n-gons, polygon soups, primitive/vertex creases, corner crease retirement,
boundary controls, UV/Cd seams, subdivision plus displacement, camera retention,
perspective and nonuniform transforms, packed variants, repeated mode edits,
stable sampled deformation, invalid shutter topology and ownership retirement.

The smooth crease fixture is compared with an independently configured Houdini
OpenSubdiv SOP. At sharpness 1, the maximum interior position-Z difference is
0.00311071 and mean RGBA difference is 0.0000334201. Discontinuous smooth Cd is
compared with six uniform reference refinement levels at 64 samples: maximum
interior RGB difference 0.01435265, mean 0.000128731. A separate continuous affine
Cd field is checked against its mathematically specified relationship to rendered
position; maximum interior error is 0.000402868. Increasing the engine's adaptive
isolation depth did not fix the earlier comparison and was reverted. The reference
was refined further to reduce sample crossings at discontinuous color seams; the
original tolerance remains 0.025.

`tests/core_subdivision_smoke.py` runs eight independent harness checks per
device. Its reference cube is constructed with five uniform Catmull–Clark
refinement steps implemented independently of the plugin's coarse mesh converter.
It also checks immutable control ownership, object-space camera retention, sharp
creases, precommit validation, asynchronous allocation failure/recovery and close.

`tests/houdini_subdivision_ipr_smoke.py` checks 14 actual GUI events per device:
the independent rounded depth, panel sharing, Linear/undo/redo, crease edits,
pixel dicing, camera updates, budget failure, recovery, rename, viewer closure
and continued panel rendering. Successful runs must exit normally with zero
tracked owned/device allocations. The first diagnostic attempts exposed an
incorrect fixture emission expectation and remain separate from passing runs.

Reports, EXRs and the example are under `artifacts/stage2/subdivision`; GUI
reports and previews are under `artifacts/stage1/gui`. The aggregate runner
passes all 70 cases in `artifacts/verification/20261010T203129214/summary.json`,
including 40 headless/Hydra cases and 15 actual GUI cases per device, with
one GUI repetition. This covers the subdivision slice and preexisting adapters.
`scripts/verify-upstream-patches.py`
also reproduces all locked patches from pristine affected files at the pinned
commit and compares them with the engine build checkout.

This evidence establishes a native subdivision development slice. It does not
close the large FX fixtures, cold/warm timing breakdowns, cancellation bounds,
long-running physical-memory tests, complete material/output/color capabilities,
Solaris release or Redshift parity requirements.

## Verified patch-49 shading interpolation groundwork (development)

Patch 49 adds an opt-in owned sampling map for completed Linear and Catmull-Clark
dicing. A shared atomic budget covers all map allocations, including temporary
seam remapping. Budget exhaustion declines this optimization. No map budget is
installed by RenderSession yet, so artist rendering does not allocate these
unused caches or claim a new attribute-update performance improvement.

The new interpolation API refreshes explicitly modified scalar/vector fields,
UVs, colors and supported intrinsic fields at retained vertex/corner samples.
It preserves position/normal motion buffers and triangle bindings, tags changed
attributes for upload and rejects incomplete maps or changed control geometry.
Displacement reuse remains an explicit integration assertion, guarded against
position/topology/dicing edits. No session update currently sets that assertion.

The standalone test exposed an existing unsafe post-tessellation normal path:
coarse normal storage has the control-vertex count, while diced normals have the
render-vertex count. Both normal-generation sign branches now use the coarse
count for coarse storage. MSVC AddressSanitizer located the overwrite; 24 fixed
instrumented processes and 12 ordinary processes pass 38 contracts each, using
independent affine fields and exact geometry/motion preservation checks.

The normal CPU and both OptiX builds pass the 59-check Pointiness regression and
five ordered frames per device. One modern Mix and 83 Blender UI names also
pass 31 checks per device. Evidence, including failed diagnostic iterations,
is in artifacts/subdivision-shading-foundation-evidence-20261019.json.

Still required: classify geometry versus shading-only payload edits, persist
effective displacement dependency stamps across shader mutation/cancellation,
refresh attributes without refilling meshes, update binding/storage handoffs,
restrict seams to actual height dependencies, and certify tangent, emission,
sampled-displacement and private packed variants. This is not the finished
session optimization.

CH_NATIVE_ASAN_CONTRACTS instruments the standalone harness, core, scene and
subdivision targets for diagnostics. Normal scripts/build.ps1 explicitly turns
it off; sanitizer builds must not be staged as ordinary development builds.

## Unverified session integration, 2026-10-19

The preceding patch-49 section describes the last verified/staged build.
Working source now contains the session integration and patch 50, but no native
build or test of this integration has run. Automatic approval review rejected
five build requests because its reviewer model was at capacity; the commands
were not launched. The staged patch-49 binaries and manifest remain unchanged.

The new source compares physical geometry and actual height dependencies,
keeps value stamps across shader mutation/recovery, transfers compatible packed
storage across resource-key changes, and updates managed fields at retained
triangle/subdivision samples. Surface palettes/face assignments do not require
new positions when their effective height functions match. Original positions
and normals are retained for UV tangent refresh. Completion callbacks account
for actual owned mesh buffers. Incomplete geometry still restores the source
before displacement; optional map-budget misses use ordinary rebuilds.

Source-only checks passed: Python syntax and pristine replay of all 50 patches.
Pending tests add 70 C++ contracts, field break/repair with packed and unpacked
objects in all three mesh/subdivision modes, a new material requesting Generated,
actual height edits, and unchanged-displacement assertions in the existing
independent tangent reference suite. These tests have not executed yet.

Next required work is compilation and native CPU/GPU validation, followed by
one-viewer live edits, cancellation, memory and heavy-scene timing. Host/wire
attribute deltas still need profiling/implementation: retaining engine geometry
does not itself prove that only shading fields were captured or transported.
Review scene-query displacement dependencies before broad certification.

### Unverified index-dependency follow-up

Source inspection confirmed that Object Info reads Object Index from each
engine object's pass ID and Material Index from the active shader's pass ID.
Those values must participate when they actually drive height, even when no
graph node or geometry buffer changes. Working source now tracks both outputs,
includes the required object index in displacement grouping and schedules
material-index displacement updates from effective value stamps. Location and
Random retain their separate transform/identity dependencies. Index metadata
that does not contribute to height is excluded from this geometry invalidation.

The new native test also checks these index edits and packed sharing; none of
its runtime cases has executed yet. An old index-pass fixture now connects Mix
Shader by named shader inputs, following the already implemented Factor-last
layout. Python syntax and unchanged-engine replay hashes pass. Native approval
review was retried once after a deadline failure and then reported capacity
again; neither request launched a process. The staged binaries remain unchanged.

## Verified retained shading and attribute ownership, 2026-10-19

This section supersedes the preceding unverified patch-50/index notes. Native
build review became available, patches 50-52 compiled successfully, and the
ordinary development build uses hosts/20261019T073430395/resources, protocol 45.
The complete production plan remains open.

RenderSession now supplies the optional shared 256 MiB sampling-map budget.
Independent UV, Cd and named shading fields can refresh at retained Linear or
Catmull-Clark samples without rebuilding physical positions or displacement.
Plain triangle meshes use the same separation. Same-height material palettes
and compatible packed storage handoffs retain geometry; incomplete maps and
budget misses take the ordinary rebuild path. Smooth fields requiring an
OpenSubdiv refiner still construct one; scalar corner fields use their actual
linear interpolation. New shader demand is reevaluated, including Generated,
Pointiness and island fields, using independent reference images.

Actual height dependencies remain decisive: control geometry, motion, dicing,
height inputs/graphs and contributing Object/Material Index changes rebuild.
Unrelated index metadata does not. Retained original positions/normals allow
tangent regeneration after UV edits. Genuine triangle shader reassignment
invalidates emitter preparation; emission estimates remain in the light-tree
cache key. Validated unchanged geometry avoids redundant normal generation,
BLAS refit and false emitter-tree revisions.

The independent tangent suite exposed a real packed attribute-table bug.
Changing shader demand could relocate an unchanged normal field without
copying its values to the new offset. Patch 52 records ordered ownership spans
for six attribute pools and three primitive-position pools, separately from
source dirty flags. Layout changes force copies; cancellation invalidates the
layout before writes and commits it only after successful upload/offset sync.
The old native build fails the targeted two-mesh test at state 3 with corrupted
normals. The fixed build preserves exact position/normal images through 29
requirement swaps per CPU, RTX 4090 OptiX and RTX 3080 Ti OptiX.

The final ordinary build passes 42 tangent checks per device, including 32
independent images, 28 recovery references, 14 held-UV references, 72 independent
UV-repair position/normal pairs, three direct-final states and five native batch
frames. Runtime-unchanged patch-52 builds also pass 138 shading-payload checks
per device. Each live viewer passes 31 native parameter-edit states, with an
unrelated million-triangle prototype shared twelve times remaining at one cook.
Field failures/repairs retain displacement; real height edits rebuild it.
Stop/Render and closure verify their respective owner lifecycle and zero tracked
owned/device bytes after close.

MSVC AddressSanitizer passes 78 contracts in 24 processes and the 29-state test
in an instrumented worker. Core/Scene/Subdivision and worker main are instrumented;
Houdini capture and kernel assets remain ordinary. LeakSanitizer is not enabled.
The normal build was restored with CH_NATIVE_ASAN_CONTRACTS=OFF. Its wrapper
passes 78 contracts in twelve processes; an additional 100 processes each return
explicit success JSON. Diagnostic failures and fixture corrections remain in
the archived run index rather than being relabeled as accepted tests.

On the scoped 960x540 RTX 4090 fixture, eight warm field edits now deliver their
first worker image at median 186 ms / p95 246 ms, previously 2333 / 2344 ms.
Four instances share 1,036,800 subdivided triangles. Displacement and tessellation
counts stay at one, with stable owned geometry and no temporary OptiX position/
index uploads on these edits. This measures cooking/capture through delivered
worker image, not GUI paint or physical-input latency. See performance.md.

Evidence: artifacts/mesh-shading-reuse-evidence-20261019.json and
artifacts/verification/mesh-shading-reuse-20261019/index.json. Host/wire field
deltas, remaining global emitter-buffer uploads, scene-query height dependency
review, large cancellation/numerical coverage, long physical-memory tests,
1080p/4K presentation and mixed-device certification remain unfinished.
