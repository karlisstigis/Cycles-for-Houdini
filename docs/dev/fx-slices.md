# Native FX slices

These are implemented development slices on Houdini 22.0.429, CPU and this workstation's RTX 4090 OptiX device. They do not certify production-scale FX scenes or the latency/memory budgets in the plan.

Motion mode is selected under **ROP / Motion Blur / Geometry Motion**. The
optional **OBJ / Cycles / Motion Blur** panel can inherit that mode or override
it with Transform Only, Velocity or Deformation; its Disabled setting suppresses
the object's motion sampling. Velocity mode uses current-frame `v` for meshes,
points and curves, or velocity fields for volumes. Absent velocity produces
static geometry without velocity blur, while independent OBJ/camera motion is
retained. Adding/removing velocity during IPR updates the existing session.
Malformed velocity types, nonfinite/unrepresentable values and incomplete/ambiguous
volume velocity components fall back to current geometry with a per-resource
`motion_warnings` diagnostic, shown in the panel. Other geometry retains its blur.
Warnings persist on cache hits and clear when the data is repaired. Unrelated
geometry errors remain errors; volume fallback does not hide duplicate density. This fallback is tested on CPU/OptiX and through the
headless Qt panel. Packed instance point-velocity support is described below.

The latest missing-velocity evidence has 42 native motion checks and 32 native
volume checks per CPU/OptiX device, plus object inheritance/override checks and
11 panel timing/update cases on OptiX. Earlier counts and failure descriptions
below record historical slices; missing velocity is no longer a failed edit.

Polygon displacement now retains immutable source geometry, restores vertices
on edits and creates private variants for incompatible object state. Scalar
and object/world/tangent vector offsets, material seams and supported constant
vector deformation pass independent tests on both devices. See
[native displacement](../user/native-displacement.md) for authoring, ownership,
diagnostics and explicit limits. Animated shader/image displacement and sampled
UV/color/numeric fields have later owned-shutter adapters. Packed deformation
combines with the tested static/animated displacement and subdivision paths below.

## Packed polygons

The HDK adapter traverses supported in-memory packed details recursively and composes the packed transforms with the OBJ transform. Compatible bindings share a prototype. A different material creates an explicit geometry variant because Cycles stores material bindings on geometry. Primitive `name` or `id`, followed by point identity attributes, provides stable instance keys; absent these, the prototype uses primitive-index identity. Duplicate identities retain every instance using a local primitive index. Malformed identity attributes are ignored. Static rendering does not warn about duplicate leftovers; ambiguous packed shutter correspondence omits local transform blur with a diagnostic and preserves independently valid OBJ motion. The diagnostic status reports `packed_instances`, `index_instance_ids`, resource counts and owned bytes.

`tests/houdini_packed_smoke.py` checks independently specified box positions, normals and depth. It covers nested transforms, camera/OBJ edits without mesh conversion, sharing across groups, material variants, retirement/recreation, identity preservation after reordering and duplicate-ID recovery. Evidence is in `artifacts/stage2/packed/houdini-packed-smoke.json`.

The subsequent [native assignment slice](../user/native-materials.md#assignment-precedence-and-sharing) adds embedded polygon bindings and recursive packed fallbacks. Partially assigned prototypes create variants for used fallback differences. Fully assigned prototypes share despite different outer fallbacks and ignore unused missing bindings. Shader-only edits retain the owned geometry. Assignment, native IPR/shared-panel and shader retirement evidence is recorded separately under `artifacts/stage3/materials/assignments` and `artifacts/stage3/materials/retirement`.

Nested prototypes support native mesh, point, curve and sparse-volume components,
mixed packed/unpacked sources and effective native material bindings. Sampled
mesh/point/curve prototype deformation and packed point-velocity are described
below. Arbitrary instancer shader fields and volume field deformation remain
separate adapters. Unsupported source effects use local recovery diagnostics.

## Particles and loose points

Homogeneous Houdini particle primitives and geometry with no primitives use native Cycles point-cloud spheres, with one geometry resource for the whole set. Particle primitives render referenced active vertices only; loose points outside their vertex lists are excluded. `pscale` is the object-space radius. The ROP's **Point radius without pscale** supplies the fallback. Zero radius is allowed; invalid size entries use the configured fallback with a diagnostic.

Point `Cd` is a scene-linear RGB multiplier for the material when **Color attribute** is `Cd`. Two/three-component point `uv` supplies the UV attribute. A unique nonempty scalar string point `name`, then a unique scalar integer `id`, then point index supplies identity. Duplicate or malformed optional identity hints fall back quietly and never discard points. The `point_random` attribute is a normalized stable identity hash, preserved when points are reordered. Index fallback is counted in diagnostics and cannot establish correspondence for motion across topology changes. Packed point prototypes using different fallback radii get separate variants; when an explicit `pscale` makes those defaults irrelevant, sharing is retained. Geometry with no size attribute therefore never inherits a different instance's fallback radius.

Native surface graphs now accept [point and particle-primitive shader palettes](../user/native-materials.md#assignment-precedence-and-sharing). A point binding overrides its containing particle primitive; conflicting primitive bindings on a shared point fail visibly. Unreferenced loose points are ignored. Simulation dead flags do not filter points or alter shader indices. Packed prototypes share or create variants according to used fallbacks. Graph `Cd` demand and identity-aligned material correspondence through deformation pass CPU/RTX 4090 checks.

`tests/houdini_points_smoke.py` checks actual Houdini particle primitives and loose points against analytic sphere/ellipsoid intersections, depth and normals. It checks UV/Cd, referenced-point filtering, camera/OBJ edits without reconversion, uniform/nonuniform/mirrored transforms, radius/color edits, identity reorder, malformed input, empty geometry and resource retirement. Eight repeated radius edits have stable tracked Cycles allocations on the small fixture. The matched harness constructs a separate point scene directly from known constants; comparisons cover beauty, position, normal, depth and UV at interior points. This is not a whole-image or production-scale equivalence claim.

The independent transform check exposed an upstream normal error: the pinned kernel calculated a world-space radial normal even for an ellipsoid. Recorded patch `0003-point-nonuniform-normals.patch` calculates the normal in object space and transforms it with the inverse transpose. CPU, CUDA and OptiX kernels were rebuilt from that patched source. The tests include off-axis normals on nonuniform and mirrored spheres, rather than relying solely on silhouettes.

## Simulation attributes

Native SOP rendering treats `age`, `life`, `dead` and `stopped` as ordinary user
attributes. They do not hide points, clip shutter visibility, suppress velocity,
or produce diagnostics for unused values/types. SOP geometry determines the
rendered particle population. Velocity motion uses `v` only when enabled through
the object's/ROP's motion settings; sampled deformation uses sampled geometry.

The earlier automatic lifetime interpretation has been removed. Both native
`particle_lifetime_clipping` and `particle_lifetime_motion` capabilities are false.
The existing low-level engine interval field and patch 0005 remain internal;
native SOP translation never populates that field. This does not add generic
named shader-attribute support, which remains a separate material task.

## Direct numeric lights

The independent shadow fixture exposed a real integration defect: the pinned engine's [default light graph](https://github.com/blender/cycles/blob/a456b761034dda42c32eef9f4aae0fa5a5c9f604/src/scene/shader.cpp) emits zero, and the integration had assigned it to numeric lights. Each shared-core session now initializes a neutral white, unit-emission graph so the numeric `LightData` strength contributes. Earlier emission/environment-lit fixture passes did not certify direct-light behavior.

`tests/houdini_lighting_smoke.py` passes 14 checks per CPU/RTX 4090 device. A black environment without lights is unlit; point, area and sun lights contribute, preserve RGB strength, scale radiance linearly when strength doubles, disable at zero, and retire after deletion. Light parameter edits retain geometry buffers. The point-light check independently specifies a zero-roughness Lambertian receiver and compares its outgoing radiance to `power*cos(theta)/(4*pi*pi*distance^2)` within 0.001. Area/sun tests establish contribution and edit semantics, not a complete photometric certification. Moving lights, authoring units and broader lighting controls remain pending.

## Round linear hair

Open polygon curves use native Cycles round linear segments, without mesh generation or implicit Catmull–Rom smoothing. At least two vertices are required per strand. Width precedence is vertex → point → primitive → detail → ROP fallback. Houdini `width` is full width; the engine radius is half that value. The convention is independently checked against this installation's public SOP-to-USD translator, which emits point diameter `2*pscale` and curve width unchanged.

`Cd` may be vertex or point data and interpolates along each strand. UVs use the root vertex/point value per curve. A unique scalar string primitive `name`, then a unique scalar integer `id`, then primitive index supplies identity and `curve_random` variation. Duplicate or malformed optional hints fall back quietly; all strands remain present. Tapered round segments form the envelope of varying-radius spheres. The analytic taper test accounts for that envelope's displaced contributing centre when checking interpolated color.

Primitive `shop_materialpath` now assigns native surface graphs per complete curve, with detail/OBJ/ROP fallbacks and compact engine palettes. Bindings remain attached to stable curve identities when deformation samples reorder. Per-key point/vertex bindings on hair require further semantics and produce a diagnostic. Independent strand-palette harness images match the native fixtures on both devices; live point-to-hair assignment changes, undo/redo and missing-material recovery pass through both viewers. See the [native material guide](../user/native-materials.md).

`tests/houdini_hair_smoke.py` checks tubes, tapered strands, width precedence with all four owners actually present, vertex color/root UV, transforms, identity reorder, malformed input, updates and retirement on CPU/RTX 4090. It also checks separate packed variants when fallback widths differ. The independent headless harness supplies a separate reference hair scene. `tests/houdini_strands_ipr_smoke.py` checks progressive native IPR and a shared panel, point-to-hair replacement, camera/material/attribute edits, invalid-width recovery, cancellation and normal process exit on both devices. The tests generate editable Indie examples under `artifacts/stage2/particles` and `artifacts/stage2/hair`.

These slices do not yet implement temporal particle population discovery, other curve bases, dedicated hair shader authoring or large-scene performance certification. Retained-particle existence has the narrower contract above. Mixed primitive inputs still require separate geometry requests. Unsupported combinations produce diagnostics; there is no exported-USD intermediary in the native rendering path.

Reference contracts: the [pinned point-cloud geometry](https://github.com/blender/cycles/blob/a456b761034dda42c32eef9f4aae0fa5a5c9f604/src/scene/pointcloud.cpp), [transform eligibility](https://github.com/blender/cycles/blob/a456b761034dda42c32eef9f4aae0fa5a5c9f604/src/scene/object.cpp), [curve intersection/shading](https://github.com/blender/cycles/blob/a456b761034dda42c32eef9f4aae0fa5a5c9f604/src/kernel/geom/curve_intersect.h), and [Houdini geometry attributes](https://www.sidefx.com/docs/houdini/model/attributes.html).

## Native polygon subdivision

Native polygon subdivision now has a separate tested development slice:
[controls, ownership, references and limitations](../user/native-subdivision.md).
It preserves polygon and polygon-soup control faces, supports Linear and
Catmull–Clark modes, crease/attribute seams and compatible packed variants,
and applies displacement after tessellation. Camera retention, stable coarse
deformation, batch and actual IPR/shared-panel transitions have CPU/RTX 4090
evidence. Large subdivision/displacement FX budgets remain open.

## Sparse volumes

The adapter accepts native VDB components, including multiple named fields beside other supported geometry in a mixed SOP. It retains float32 scalar and vec3-float32 grids, their sparse storage and affine transforms. Preparation copies grid metadata and transforms and retains separate tree references; supported Houdini writes make shared trees unique before mutation. Render threads therefore read immutable snapshots without an unconditional deep copy of every tree. Camera and OBJ-transform changes retain that snapshot. Tree, metadata, primitive-name and point-position identifiers invalidate capture when grid contents or transforms change. Acquisition and normalization contribute to extraction timing; a subsequent source edit may require a copy. See [ownership evidence and limits](notes/native-geometry-ownership.md#frozen-vdb-grids).

Density is a coefficient per world-space length: doubling an object's scale doubles its optical depth. The absorption material reads `density` and applies the material density scale. The principled volume adds scattering/anisotropy, emission and blackbody controls. Without an emission field, emission strength is multiplied by density to avoid emission throughout padded bounds. With a field, its scalar or RGB value multiplies emission color. Blackbody uses the named `temperature` field multiplied by the explicit **Kelvin per temperature unit** parameter. Missing required fields and incompatible scalar/vector types are rejected before engine mutation.

`tests/houdini_volume_smoke.py` constructs a known sparse grid independently of the converter: nine voxel centres along each axis, spacing 0.125, density 1 and zero background. Its centre-ray density integral is 1.125, so unit absorption has expected transmission `exp(-1.125)`. Tests cover material scaling, OBJ scaling, grid translation/nonuniform scaling, point-position edits, density changes, missing fields, retirement/recreation and stable tracked allocations over repeated edits. The source file is removed before rendering.

`tests/houdini_pyro_smoke.py` checks density, temperature, scalar emission and vector velocity storage. With extinction disabled, constant emission integrates to a known RGB radiance. It also checks temperature response, required-field diagnostics, shared-view error recovery and an editable original `.hiplc` example with embedded grids. This is a synthetic field fixture, not a simulated production pyro sequence. Evidence is under `artifacts/stage2/volume` and `artifacts/stage2/pyro`.

`tests/houdini_volume_ipr_smoke.py` exercises progressive native IPR and the same session in the registered Python Panel. Camera/material edits retain the grids, a field edit replaces the owned snapshot, and cancellation/last-view retirement release renderer allocations. GUI reports include process exit status and are kept under `artifacts/stage1/gui`.

The engine converts grids to NanoVDB. Scalar grids currently use the pinned engine's 16-bit quantized NanoVDB representation; vector grids use float32. This is distinct from float32 EXR output. The native loader disables implicit field clipping: a test with density 0.00025 and material scale 2000 demonstrated that the upstream 0.001 cutoff removed a physically significant volume. Any future clipping control must be explicit and tested against shader scales. Native velocity blur has additional evidence below. Volume field interpolation, production topology-changing sequence certification, additional grid types and large-scene memory/performance certification remain pending.

Tracked allocation counters describe Cycles buffers, not physical GPU residency. Small fixture timing and stable allocation counters do not establish production performance. The first full FX performance report must measure cooking, acquisition/normalization, volume serialization, NanoVDB conversion/upload, acceleration structures, rendering and output separately. Retained-grid byte counts can include storage also referenced by Houdini and are not unique process allocations.

Reference contracts: [Houdini VDB coordinates and transforms](https://www.sidefx.com/docs/houdini/hom/hou/VDB.html), [pinned Cycles volume implementation](https://github.com/blender/cycles/blob/a456b761034dda42c32eef9f4aae0fa5a5c9f604/src/scene/volume.cpp), [VDB image loader](https://github.com/blender/cycles/blob/a456b761034dda42c32eef9f4aae0fa5a5c9f604/src/scene/image_vdb.cpp), and [principled volume node](https://github.com/blender/cycles/blob/a456b761034dda42c32eef9f4aae0fa5a5c9f604/src/scene/shader_nodes.cpp).

## Shared engine motion proof

The internal session contract now accepts owned geometry position/radius samples, optional matching split-normal samples, and object/camera transforms. Geometry samples are chronological with the centre omitted; base data holds the centre. Transform samples include the centre and must match the base transform. Counts must be odd totals from 3 to 129, matching the pinned engine limit. Unmatched topology, invalid radii/normals/matrices and transform handedness changes are rejected before mutation. Producers must establish correspondence; matching array sizes alone does not prove it.

`tests/core_motion_smoke.py` runs independently authored C++ fixtures through the matched harness on CPU and RTX 4090 OptiX. Fifteen checks per device cover point/hair/mesh deformation and object translation, camera translation, point radius motion, equivalent three/five-step interpolation, disable/re-enable transitions and static rendering when blur is off. A uniform shutter translating a radius-0.2 sphere through a world-space distance of 1 has centre coverage approximately 0.4. A radius-0.1 vertical strand has coverage 0.2, and a width-0.4 planar mesh has coverage 0.4. The report records measured coverage and statistical tolerances, with raw EXRs. Malformed correspondence does not advance the generation, and closed sessions release tracked device allocations.

Testing exposed an integration requirement on OptiX: stored motion data must be distinguished from active motion. When blur is disabled, geometry motion flags/BVHs and object/camera motion arrays must reflect static rendering. The integration retains owned samples and restores them when blur is enabled again. Camera-only updates do not traverse the geometry motion records; that work is triggered by a motion-mode change or new geometry.

This is an engine-contract proof. Native Houdini shutter/FPS conversion, subframe cooking, velocity units, persistent correspondence, particle births/deaths, animated packed poses, camera intrinsics and volume velocity are not certified by these tests. The native shutter/velocity gate now has additional host evidence described below.

## Native shutter and velocity motion

The native ROP now enables OBJ/camera transforms, rigid packed transforms and point-velocity blur for polygon meshes, point spheres and linear hair. The camera's `shutter` is a fraction of a frame. For nominal render time `t`, FPS `f`, shutter `s` and offset `o`, duration is `s/f` seconds and the interval is `[t + (o-1)*s/(2*f), t + (o+1)*s/(2*f)]`. Offset -1 ends at `t`; +1 starts there. Sample counts are odd totals from 3 through 129, so the base pose is the exact shutter centre. Zero shutter or a disabled master switch renders the nominal static frame.

The host thread evaluates OBJ transforms and cooks packed poses using public time-specific HDK contexts. It never moves Houdini's current timeline. Packed paths use supplied names/IDs or automatic primitive indices. Unmatched births, deletions and identity changes omit affected packed shutter motion with a diagnostic, keeping requested-frame instances and independently valid OBJ motion. Static geometry snapshots are retained while transforms change. Transform mode freezes prototype geometry at the nominal render time; sampled SOP deformation uses the separate mode described below.

Velocity mode extrapolates nominal SOP-space positions using point `v` in units per second. Every curve key receives its referenced point's velocity. Owned sample arrays contain positions; widths, radii and shading attributes stay at the nominal snapshot. Cache keys include velocity data identifiers and relative shutter seconds. Camera-only edits and changes to absolute render time with an otherwise unchanged velocity snapshot avoid conversion. FPS/shutter/offset changes invalidate the required sample payload. These are conversion guarantees on the fixtures, not yet GPU-upload or large-scene performance certification.

Authored mesh normals need matching deformation samples. Velocity meshes with explicit normals retain their requested-frame geometry and authored normals, omit unsupported local velocity blur with a diagnostic, and preserve independent OBJ/camera motion. Temporal particle population discovery, changing camera projection/intrinsics and moving lights remain separate requirements. Volume velocity has its own evidence. Sampled deformation is described below. A velocity snapshot supplies only its retained particle population, without interpreting age/life or dead/stopped flags. Radius or color animation across the shutter is not sampled by velocity mode.

`tests/houdini_motion_smoke.py` passes 34 analytic/render/rejection checks per CPU/RTX 4090 device. It checks projected coverage/centroids, FPS, shutter lengths and offsets, 3/5-step sampling, object/camera relative motion, explicit render time, packed centre poses, caches, malformed data and recovery. Six identical updates have stable tracked allocations and closing releases them. A known trajectory moves a radius-0.2 sphere through distance 1, giving centre coverage 0.4; SOHO CPU batch output measures approximately 0.4032. The editable example is `artifacts/stage2/motion/native-velocity-motion.hiplc`.

`tests/houdini_motion_ipr_smoke.py` passes native IPR plus shared-panel lifecycle tests on both devices. Changing the camera shutter from half to quarter frame doubles centre coverage for the known trajectory. Disabling/re-enabling blur, camera/OBJ matching motion, missing-velocity fallback, recovery, cancellation and normal process exit pass. GUI evidence is kept under `artifacts/stage1/gui`.

Reference conventions: [SideFX shutter properties](https://www.sidefx.com/docs/houdini/render/blur.html), [geometry velocity units](https://www.sidefx.com/docs/houdini/nodes/obj/geo.html), and the pinned [Cycles camera sampling](https://github.com/blender/cycles/blob/a456b761034dda42c32eef9f4aae0fa5a5c9f604/src/kernel/camera/camera.h).

## Native deformation sampling

The ROP's sampled-deformation mode cooks the shutter centre and uniformly spaced
surrounding times using public HDK contexts. It retains immutable owned details
before another cook, restores the nominal context, and converts from those owned
inputs in the background. No live SOP access occurs in conversion. Conversion
and cooking timings are recorded separately.

Point/mesh positions can follow persistent point names or integer IDs even when point order changes. Mesh connectivity and oriented triangulation must still correspond; matching point counts is insufficient. Triangle order and cyclic starting corners can change while sampled split normals remain aligned. Duplicate overlapping triangles currently require a later primitive-provenance extension and are rejected. Curves follow persistent strand identities and retain their key connectivity. Reordered strands/points pass explicit identity tests.

When usable unique identities are absent, points, meshes and strands use their element indices automatically. Duplicate/malformed optional names or IDs do not require artist cleanup. A recook may change Houdini topology data IDs without changing connectivity, so those cache tokens no longer reject index correspondence. Mesh connectivity, oriented triangulation, curve key counts and geometry counts are still validated. Index correspondence follows ordering: it cannot infer physical identity after an arbitrary reorder, whereas supplied names/IDs allow that matching. Unmatchable births/deaths, connectivity or primitive-kind changes omit only the
affected component's deformation blur and retain its requested-frame geometry.
Packed prototype deformation uses the shared sampled path described below.

Mesh split normals, mesh UV/Cd, point radii and curve widths are sampled. Polygon UV/Cd remain aligned through corner correspondence and subdivision and are read at shader time. Missing/invalid shutter fields retain usable geometry; only incompatible displacement motion recovers to the requested pose. Unused fields stay quiet, and repairing them during IPR restores the sampled result. Named numeric fields and point/hair shading motion use the later sampled-field
adapters; unsupported representations still recover locally. Cache fingerprints include nominal geometry data IDs, SOP cook count and shutter times, so a parameter edit that leaves centre positions unchanged still invalidates its motion. Restoring the nominal cook context permits warm camera edits to reuse all samples without subframe recooking. Motion modes and fallback sizes use distinct rendering-state variants; conservative deformation size variants remain separate even if the nominal sample has an explicit size attribute, since that attribute may disappear at another shutter time.

`tests/houdini_deformation_smoke.py` passes 32 checks per device on CPU/RTX 4090. Its changing-shape mesh has horizontal velocity `48*(1+y)`, so independently specified sections have shutter coverage `0.4/(1+y)`. It tests this at y=0 and ±0.3, along with point/hair movement, radius/width animation, offsets, 3/5 steps, identity reordering, fallback variants, cache invalidation and failure recovery. An independent sampled-normal integral predicts normal.X approximately 0.10192; the measured value is approximately 0.10396 on both devices, within the image tolerance. Six repeated updates retain the same allocations and conversion count. The editable example is `artifacts/stage2/motion/deformation/native-deformation-motion.hiplc`.

The extended `tests/houdini_motion_ipr_smoke.py` verifies actual SOHO deformation, camera updates without subframe recooking, centre-preserving motion edits, invalid shutter identities and recovery in shared native IPR/panel views on both devices. These small fixtures do not certify production workloads or physical VRAM residency.

## Shared packed-prototype deformation

Compatible packed deformation profiles now retain accepted engine resource slots
when Houdini recreates source details. Shape changes need no instance-binding
resend; split profiles retain separate resources and merges retire unused slots.
Shutter pose tables store owned matrices. Automatic IPR captures reuse an exact
accepted preview while explicit Refresh still forces rendering. This does not
remove material-palette variants or change artist-authored identity policy.
See [measurements and limits](performance.md#stable-packed-resources-and-automatic-ipr-reuse---october-17-2026).


Packed nominal/shutter leaves now preserve shared prototype templates, short
placement arrays and immutable selections. Namespaced populations, per-instance
components and motion tables expand during owned background conversion.
Only safe full-detail homogeneous classification can share internal data-ID
metadata; mixed-detail primitive offsets remain local. Independent shutter
profiles, frame vectors, local repairs and heavy concurrent edits are covered.
See [measurements and limits](performance.md#compact-packed-deformation-capture---october-17-2026).


Packed shutter population correspondence, local/world motion table assembly,
profile grouping, remapping and deduplication now run during owned background
conversion. Host preparation stamps SOP dependencies once per immutable nominal
sample and passes no SOP pointer into that work. Concurrent source edit/deletion
and one-viewer heavy update checks cover the boundary. Per-instance nominal and
shutter object capture still needs compact batching; see the
[assembly measurements](performance.md#owned-packed-deformation-assembly---october-17-2026).

Unchanged in-memory packed deformation now retains complete local shutter
recipes before instance rebuilding in background conversion. Fresh captures
share immutable sample requests and prototype/material plans. Material repairs
and newly demanded fields invalidate the applicable recipe; cached warnings
persist until repair. File-backed/unknown procedurals are reacquired, and
several OBJ requests of one SOP use the established binding reconciliation.
See [preparation measurements](performance.md#packed-deformation-preparation---october-17-2026).

Deformation mode now acquires nested packed mesh/point/curve prototypes at each
shutter time, reusing the root SOP cook across instances and material bindings.
The owned sample profile, including required fields and effective bindings,
determines sharing. Instances with the same complete profile reuse one converted
prototype; distinct profiles receive explicit variants. Changing ordinary surface
materials does not require repeating SOP shutter cooks. Cycles still owns its
binding palette on geometry, so different used palettes may duplicate engine
geometry buffers even though source sampling is shared.

Warm identical scene captures and camera edits retain all samples without
subframe cooks. Cached warnings remain visible until repair. Failed shutter
topology or population correspondence freezes only that nominal component,
preserving independently valid mesh/point/hair/OBJ/camera motion. No authored ID
is required. Automatic indices are local to each rendering component and strand,
so a changed mesh count cannot invalidate unchanged neighboring components.
Sparse-volume field deformation still uses the existing explicit fallback.

CPU and RTX 4090 OptiX pass 43 checks and five native ROP outputs each in
`houdini_packed_deformation_smoke.py`, five nested-instance animation frames in
`houdini_packed_deformation_frames_smoke.py`, and 14 material/subdivision/static
and temporal displacement cases in `houdini_packed_deformation_material_smoke.py`.
`houdini_packed_deformation_gui_smoke.py` checks 14 accepted states with native
keyboard parameter edits, one viewer, a 1,036,800-triangle prototype and 16
nested copies, including camera/OBJ edits while geometry preparation is active.
The mixed-scene ownership probe also converts four independently acquired sampled
scenes during native edits and after source deletion. Physical display/mouse
certification, response budgets, other devices and long-run physical memory
remain separate. See archived evidence (`artifacts/verification/packed-deformation-20261017/index.json`).

## Packed instance velocity

Geometry Motion **Velocity** now also moves packed instances using point `v`, in
units per second in their containing detail's coordinates. The current packed
population is retained; no shutter SOP cook or identity matching is required.
Duplicate/malformed optional names and IDs do not prevent velocity blur. Parent
and nested velocities compose with independently sampled OBJ/camera transforms.
Prototype mesh/point/hair velocities and sparse-grid advection remain independent.
This follows the [Houdini velocity convention](https://www.sidefx.com/docs/houdini/nodes/obj/geo.html)
and the [documented instance velocity workflow](https://help.maxon.net/r3d/houdini/en-us/Content/html/Instances.html).

Missing packed `v` gives a nominal local pose with no warning. Invalid type,
nonfinite/unrepresentable values or overflow after a parent/OBJ transform omit
only that packed velocity. `motion.packed_velocity` identifies `v`, reports the
first affected path/point and aggregates the count. Other instances and valid
parent/OBJ/camera/prototype motion continue; repair clears the warning. Disabling
motion sampling ignores this unused field. Only velocity actually present on
rendered packed points is read; Copy to Points transfer settings can matter.

Use **Transform Only** to sample actual animated packed poses without prototype
deformation; **Deformation** samples both poses and supported prototype contents.
Velocity uses nominal contents and authored velocities. Angular `w` and `accel`
adapters remain separate work and are not implicitly interpreted by this mode.

The 37-case focused suite and five native ROP frames pass on CPU, each of this
workstation's two OptiX GPUs and their checked pair. Fourteen heavy live viewer
states pass on CPU/4090/3080 Ti, including native parameter keyboard edits,
warning repair, retained geometry and concurrent navigation during a topology
update. Motion vectors and packed volume translation have additional CPU/4090
checks. See evidence (`artifacts/verification/packed-velocity-20261017/index.json`).

## Native volume velocity motion

Velocity mode now advects the nominal owned VDB fields using geometry-space velocity in units per second. `vel` and `velocity` identify vec3-float grids. Matching float scalar components named `vel.x/y/z`, `velocity.x/y/z` or `velocity_x/y/z` are merged into one vector grid through sparse tree operations. The merge preserves tiles, leaf values, background values and active-state unions; it does not expand every active tile into voxels. Missing components, conflicting aliases, mismatched component transforms and nonfinite velocities fail before scene mutation. Separate scalar/vector grid types still follow the initial float32 certification boundary.

The kernel's ray time is normalized over the shutter. For duration `d = shutter/FPS` and offset `o`, the nominal field snapshot is at normalized time `(1-o)/2`. Recorded patch 0004 adds this explicit time reference while preserving the upstream camera-based default for other consumers. The adapter sends `d` in seconds; it does not multiply velocities by frame duration twice. Shader density, emission and temperature lookups use the advected location. The method is the pinned engine's first-order semi-Lagrangian extrapolation from a single field snapshot; acceleration and interpolation of multiple simulation snapshots remain separate work.

Patch 0004 also accepts a conservative object-space displacement envelope. The integration inspects all stored velocity values, including sparse outliers and nonzero backgrounds, when replacing a volume. It converts that envelope into the final merged topology's affine index space and adds interpolation support. It avoids the upstream percentile estimate and half-volume-size clamp for the native adapter. Camera edits neither rescan these values nor recopy their grids. Velocity duration/offset changes invalidate the required owned volume state. These bounds protect the tested fast trails; their potentially significant padding/conversion cost still needs production-scale measurement.

The pinned engine's octree density-extrema evaluation explicitly samples one fixed time. It does not provide a motion-aware bound for rapidly advected fields. Scenes with active volume velocity therefore select the upstream ray marcher, including any static volumes in the same scene. Disabling motion or retiring the last moving volume restores the normal octree policy. This is a correctness choice, not a claim that ray marching is the fastest solution for pyro. `Volume step size (0 = automatic)` controls object-space sampling; smaller steps require convergence and cost checks. The active policy appears in session diagnostics, the render panel and EXR metadata. Different step sizes are explicit geometry variants. A later motion-aware acceleration project must pass independent correctness and performance tests before replacing this choice.

`tests/houdini_volume_motion_smoke.py` passes 31 native checks per CPU/RTX 4090 device, plus nine independently authored matched-harness checks per device. A nine-voxel-wide density cube with spacing 0.125 has static centre transmission `exp(-1.125)`. For velocity 96 and a half-frame shutter at 24 FPS, a two-unit trail has independently integrated centre transmission approximately 0.612365. A shutter starting at nominal time has transmission approximately 0.721764 near x=1.75, outside the original active volume's bounds. Tests include offsets, FPS, shutter changes, inverse velocities, matching camera/OBJ motion, negative/nonuniform scale, distinct affine field maps, emission with extinction, sparse scalar tiles, a rare velocity outlier, invalid fields, recovery, geometry variants and repeated allocation retention. Original fixture files are removed before rendering. The editable example is `artifacts/stage2/motion/volume/native-volume-velocity.hiplc`.

`tests/houdini_volume_motion_ipr_smoke.py` passes on CPU and RTX 4090 with one session shared by native IPR and the dedicated panel. The trail responds to camera shutter changes; camera-only edits retain the owned grids. Invalid velocity fields cancel obsolete tracing and preserve the image, and valid edits recover both views. Sampling-step changes, cancellation, last-view retirement and normal host exit pass. The first GUI attempt exposed a SOHO camera-token error: the installed SDK returns `ortho`, which the adapter previously interpreted as perspective. A renderer-invoked camera probe confirmed the value, and the adapter now recognizes it. The camera regression checks both supported projections, distance-independent orthographic width, and explicit rejection of sphere/cylinder/custom-lens projections. Earlier GUI evidence remains historical and is not retroactively presented as certification of the corrected projection.

References: the [pinned volume advection kernel](https://github.com/blender/cycles/blob/a456b761034dda42c32eef9f4aae0fa5a5c9f604/src/kernel/integrator/volume_shader.h), [fixed-time volume density evaluation](https://github.com/blender/cycles/blob/a456b761034dda42c32eef9f4aae0fa5a5c9f604/src/kernel/bake/bake.h), [volume bounds implementation](https://github.com/blender/cycles/blob/a456b761034dda42c32eef9f4aae0fa5a5c9f604/src/scene/volume.cpp), [Houdini shutter conventions](https://www.sidefx.com/docs/houdini/render/blur.html), and [native IPR pixel access](https://www.sidefx.com/docs/houdini/hom/hou/IPRViewer.html).
