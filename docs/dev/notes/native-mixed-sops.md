# Mixed native SOP geometry

One native render SOP can now combine closed polygon meshes and polygon soups,
open linear curves, loose point clouds, particle primitives, packed prototypes
and sparse VDB fields. Packing each branch is no longer required. Solaris
expansion remains deferred; this work changes the HDK adapter and retains the
shared renderer services.

## Extraction and ownership

The latest heavy animation confirms four unchanged type/prototype resources
remain retained while one ordinary mesh component changes. That component
contains the turbine, ground and rocks; one changed resource does not mean only
the turbine's vertices were copied. Direct conversion, retained triangulation
and component-specific bulk signature reads reduce the repeated capture-only
median to 250 ms on already cooked data with no engine/GPU upload, versus
310 ms after direct conversion and 677 ms originally. Host-thread capture
and remaining copies still need optimization. See [the measured increment](../performance.md).

The adapter previously chose its converter from the first primitive. An
unpacked mesh/curve/point input reproduced a conversion failure in
`artifacts/examples/sop-fx-courtyard/mixed-geometry-before-support.log`, alongside
the preceding build manifest. It now validates every primitive and partitions
the retained host detail by geometry type.

Partitions contain owned point/primitive offsets and data identifiers. They do
not retain raw host pointers for asynchronous rendering. Topology and primitive
list identifiers plus element counts invalidate the partition cache; invalid
identifiers force inspection. Camera edits reuse it without walking vertices.
Committed geometry owns references to the required metadata, which retires when
its last geometry resource is deleted. `partition_cache_bytes` estimates the
partition records and offset-list storage; it is not total Houdini process RAM.

Mesh conversion reads selected primitives directly through public
`GEOtriangulatePoly`, without a copied temporary detail. It preserves corner
attributes, detail defaults, component-local indices for implicit deformation
correspondence and coarse face provenance for subdivision. A per-resource
cache keeps logical point/vertex/face mappings and the last owned mesh. Stable
valid IDs use a fast path; changing conservative IDs trigger an exact logical
connectivity comparison. Changed connectivity rebuilds the mapping. Position
edits retriangulate incident quads/n-gons, retaining stationary faces and
triangle connectivity; a changed diagonal also invalidates corner mappings.
Matching supported attribute IDs retain owned normal/UV/color arrays. This is
not a position-only GPU update: output positions and triangles are still copied
into a new owned mesh, and engine reconstruction remains separate.

`mesh_topology_builds`, `mesh_topology_reuses`, `mesh_topology_scans`,
`mesh_triangulated_faces` and `mesh_retained_faces` are cumulative capture
counters. `mesh_capture_cache_bytes` estimates active topology/provenance
metadata, excluding retained mesh buffers and renderer copies. It reaches zero
on geometry retirement/close. Points and curves read their original selected
offsets synchronously. VDBs retain private metadata/transforms and immutable tree
references with supported host copy-on-write; see [ownership](native-geometry-ownership.md#frozen-vdb-grids). Packed
prototypes retain sharing and recursive transform composition.

## Selective component invalidation

Houdini detail-level data IDs can become dirty when only one branch of a Merge
changes. Supported mixed polygon, point and linear-curve selections now compare
a 128-bit signature of their selected rendering inputs before allocating owned
geometry. The signature includes connectivity, positions, supported attributes,
material bindings, widths/radii, identity inputs and relevant request settings.
It includes both sides of a shared mesh/curve point dependency. A match updates
the cached Houdini IDs and retains the committed geometry revision.

The signature now mirrors each converter's consumed and validated owners:

| Component | Attribute domains checked |
| --- | --- |
| Mesh | Point/vertex `N`, `uv` and `Cd`. |
| Particles | Point `pscale`, `Cd`, `uv`, `id`, `name`. |
| Curves | Point/vertex `uv`, `Cd` and `width`; primitive `width`, `id` and `name`; detail `width`. |
| Enabled velocity blur | Point `v`. |
| Material validation | Supported binding/override/style-sheet attributes retain conservative checks across all four owners. |

Numeric tuples use public `GA_AIFTuple::getRange` in blocks of at most 512
elements; numeric scratch is bounded at 64 KiB. Position checks use the same
three float components as the owned converters, in 6 KiB blocks. String
values retain their existing per-element route. Unsupported representations
fall back to conversion. Ignoring unused mesh `pscale` or hair normals does
not ignore those values when the particle or mesh converter consumes them.

This check runs only after the coarse IDs become dirty; camera and ordinary OBJ
transform edits retain the existing ID fast path. It is not a full-scene vertex
hash on each panel poll. `component_checks`, `component_reuses` and
`component_check_seconds` expose its work. Scanning a large dirty selection still
costs CPU time and is included in extraction timing.

Polygon soups, subdivision and sampled deformation retain conservative capture;
unsupported attribute representations also fall back. Changes to global indices,
primitive provenance or shared points can legitimately invalidate more than one
component. This does not promise precise invalidation for every Houdini primitive.

VDB fingerprints use the selected grids' tree, metadata and transform identities,
their actual anchor positions, names, step size and velocity/shutter policy.
Unrelated mesh edits retain their grids. Changing a grid anchor still
refreshes the volume. Camera edits retain the owned volume without rescanning it.

`artifacts/verification/20261011T153413210/summary.json` passes all **112 cases**
on the typed bulk-read build, with an immediate source/binary audit. The mixed
test now runs fifteen groups per CPU/OptiX device, including unused attribute
edits and an independently checked particle-radius update. Other cases
verify point-only position/color edits, curve-only widths, shared mesh/curve
points, mesh-only edits beside packed/point/curve/VDB data, and VDB anchor edits.
The actual courtyard deformation recaptures one geometry component; its topology
edit recaptures three under the current conservative indexing policy.

These results describe adapter ownership and conversion. Cycles packs several
geometries into shared device arrays, so an unchanged native resource can still
participate in a wider engine buffer copy or acceleration-structure update.

## Rendering semantics

| Input | Behavior |
|---|---|
| Point shared by a mesh and a curve | Participates in both; no extra sphere is created. |
| Loose points without particle primitives | Render as a point cloud, using `pscale` or the native default radius. |
| Explicit particle primitives | Render their referenced points once; other loose points retain the existing exclusion policy. |
| Deleted polygon/grid with retained unused points | Those points become the loose cloud. Remove unused points when field-only or mesh-only geometry is intended. |
| Native material paths and overrides | Validation and effective binding palettes are scoped to the relevant type's primitive, point and vertex ranges. |
| Packed prototypes | Compatible complete rendering state still determines sharing; explicit variants remain required for incompatible state. |
| Multiple VDB fields | Form one volume and require one effective volume material. Independent media still need distinct sources or packed prototypes. |
| Unknown primitive anywhere in the input | Rejects the whole update and preserves the preceding committed image. |

Physical component keys use stable type suffixes, independent of primitive order
or the presence of other types. Caller `geometry_key` and `object_key` values
still identify the logical source: explicit deletion expands to its physical
resources. Deleting geometry while objects reference it remains an error.
Deleting both retires all components and allows unchanged source recreation.

For sampled mesh/point/curve deformation, components and nested packed instances
share one root SOP cook per shutter time. Each immutable detail is owned before
the next cook; conversion runs without live SOP access after nominal restoration.
Final cook stamps let camera edits retain earlier components. Sampled OBJ motion
reaches every component, including packed instances after a VDB in the primitive
list. Unmatchable component changes omit only the affected deformation blur;
usable requested-frame geometry remains visible. Implicit correspondence uses
component-local indices (per-strand key indices for curves), so unrelated mesh
count changes do not shift point/hair/packed identities. Packed deformation has
shared source sampling across material variants. Sparse-volume field deformation
retains its explicit fallback; volume velocity remains available separately.

Unchanged packed deformation retains owned sample metadata and local poses;
its instance/alias rebuilding runs after converter handoff. Shader field demand,
material repair and source/dependency changes invalidate that metadata. The
cache stores no SOP or GU pointer. File-backed/unknown packed procedurals keep
reacquisition, and shared-SOP OBJ bindings keep their existing reconciliation.
Fresh capture shares immutable per-prototype sample requests while preserving
per-instance properties and Cryptomatte identity. Cold correspondence assembly
still has substantial host cost; see [measured boundaries](../performance.md#packed-deformation-preparation---october-17-2026).

Packed shutter population correspondence, local/world motion table assembly,
profile grouping, remapping and deduplication now run during owned background
conversion. Host preparation stamps SOP dependencies once per immutable nominal
sample and passes no SOP pointer into that work. Concurrent source edit/deletion
and one-viewer heavy update checks cover the boundary. Per-instance nominal and
shutter object capture still needs compact batching; see the
[assembly measurements](../performance.md#owned-packed-deformation-assembly---october-17-2026).


Packed nominal/shutter leaves now preserve shared prototype templates, short
placement arrays and immutable selections. Namespaced populations, per-instance
components and motion tables expand during owned background conversion.
Only safe full-detail homogeneous classification can share internal data-ID
metadata; mixed-detail primitive offsets remain local. Independent shutter
profiles, frame vectors, local repairs and heavy concurrent edits are covered.
See [measurements and limits](../performance.md#compact-packed-deformation-capture---october-17-2026).


Compatible packed deformation profiles now retain accepted engine resource slots
when Houdini recreates source details. Shape changes need no instance-binding
resend; split profiles retain separate resources and merges retire unused slots.
Shutter pose tables store owned matrices. Automatic IPR captures reuse an exact
accepted preview while explicit Refresh still forces rendering. This does not
remove material-palette variants or change artist-authored identity policy.
See [measurements and limits](../performance.md#stable-packed-resources-and-automatic-ipr-reuse---october-17-2026).

## Verified development scope

Packed Velocity mode composes point `v` through supported packed hierarchies
without cooking another population. Mesh/point/curve/volume leaves share owned
instance placement metadata and expand object/motion tables during background
conversion. Missing/invalid motion retains geometry locally. See the
[packed velocity slice](../fx-slices.md#packed-instance-velocity) and its measured
[preparation boundaries](../performance.md#packed-velocity-preparation---october-17-2026).

The original mixed-SOP baseline
`artifacts/verification/20261011T050353585/summary.json` passes **97/97 cases**:
55 headless cases and 21 actual GUI workflows on each of CPU and RTX 4090 OptiX.
The immediate source audit matches 232 integration files, all 97 test entries,
four compiled artifacts, thirteen patches and six kernels. Manifest SHA-256:
`8c93c036e657539d83769ebd57d9d97044f8764c1c43a1cac042cc6ccb7ed773`.

The subsequent native OBJ-scene increment also passes the full native suite:
**102/102 cases** in `artifacts/verification/20261011T061340687/summary.json`,
including these mixed-SOP fixtures and 22 actual GUI workflows per device.
Its immediate audit matches 240 integration files, all executed entries and
the recorded binaries, patches and kernels. See [native OBJ scenes](native-object-scenes.md)
for that increment's supported selection and Houdini-light semantics.

Three added mixed-SOP tests provide twenty groups per device. Independent
expectations cover radiance, mesh UV/normal/position, particle sphere depth,
packed box planes, VDB Beer-Lambert extinction, shutter coverage, shared-point
membership, subdivision scope, primitive order, material scope, partial deletion,
logical-source ownership, failure recovery and metadata retirement. Existing
packed, material, texture, color, motion, volume, batch and viewer regressions
also pass on this build.

The current mesh converter reverses Houdini polygon corners into Cycles winding,
including face-varying UV, normals, colors and motion correspondence. Independent
native Grid tests verify irradiance, front/back semantics, positive displacement
and mirrored transforms. The current artist example replaces its substantial
Python-generated meshes with native Houdini SOPs and compiled VEX, retaining one
small Python marker. See [native interactive updates](native-interactive-updates.md).

An earlier full native run passed 107/109 cases; the two wrong-node displacement
UI fixtures passed their corrected CPU/GPU repeats with unchanged renderer
binaries. The 97/102-case full results above are historical.

The current mixed courtyard contains 54 shared packed panels, 360 loose sparks,
265 curves, unpacked meshes and an embedded procedural VDB plume. It uses five
geometry resources for 58 objects. Actual noise displacement remains enabled on
ground and rocks. See [the test-drive guide](../../user/try-native-sops.md).

The earlier stopped runs preserve failures and matching source audits in
`20261011T044148777` (logical deletion), `20261011T044601889` (a missing-field
fixture retained a grid point) and `20261011T045237363` (the packed-ID assertion
depended on the former key layout). The ownership implementation was corrected;
the latter two fixtures now test their intended semantics explicitly. These runs
are not successful full-suite results.

This verifies development fixtures, not arbitrary FX performance or a production
release. Broader workload budgets, additional native light features and passes,
artist picking and broad denoising-quality certification remain open. Optional
[native denoising](../../user/native-denoising.md) has CPU/RTX 4090 lifecycle and pixel-transfer
regressions. Conservative component fallbacks, physical GPU transfer granularity
and viewport stalls still require optimization. The [measured worker comparison](native-worker-prototype.md)
uses a scaled courtyard with 950,906 triangles, 352 objects, 1,200 particles and
605 curves. Camera/transform edits retain geometry in all three modes. Queued
edits retain the session, but a deformation still recaptures the complete ordinary
mesh component containing unchanged ground and rocks. The current converter
retains valid triangulation/provenance and unchanged supported attribute arrays;
narrower component dependencies, background preparation and physical GPU update
granularity remain unfinished. External rendering alone did not solve host capture.
