# Native polygon displacement

This development slice renders native Cycles displacement in Houdini
22.0.429 on CPU and RTX 4090 OptiX. It extends the existing native material,
ROP, IPR, shared panel and batch paths. Supported subdivision and displacement
motion have independent correctness checks; broader dependencies and
production-scale performance retain their separate release gates.

## Authoring

Create a **Cycles Material**, connect a surface closure to **Surface Output**,
and connect **Displacement** or **Vector Displacement** to its optional
**Displacement** input. The builder's **Displacement Method** selects:

- **Bump:** alter shading without moving vertices.
- **True:** change the polygon mesh positions and recompute applicable normals.
- **Both:** change positions and retain the engine's additional bump detail.

The default method is Bump, preserving existing materials. The surface
closure remains input zero, preserving saved output connections. An absent
displacement connection leaves the surface unchanged.

For scalar Displacement, `(Height - Midlevel) * Scale` offsets along the
normal. Midlevel defaults to 0.5; an unconnected Normal retains Cycles'
automatic normal input. Vector Displacement subtracts Midlevel from each
component, then multiplies by Scale. The default is **tangent** space with
Scale 0.01, as in Blender; scenes saved before November 14, 2026 keep the
space they were saved with (then Object by default).

Object-space offsets use local geometry units and follow the instance
transform. World-space offsets use world units. A float3 branch connected
directly to the output represents a world vector; use the Displacement
nodes to specify another space. Tangent-space vector
components correspond to **tangent, normal, bitangent**, respectively;
the second component points out of the surface. Tangent space requires
corner UVs, including point UVs expanded by the native adapter. The default
map and named two/three-component maps are supported; named maps use XY without
requiring a duplicate default UV stream. Missing/invalid maps omit dependent
displacement with a warning while retaining usable geometry and shading.

This slice certifies polygon meshes. It does not add true displacement to
point spheres, curves or volumes. True/both assignments to points or hair
omit incompatible displacement per binding and retain usable surface shading
with a warning. Sparse volumes use their compatible volume binding; unsupported
surface/displacement branches recover locally rather than stopping the scene.

## Original vertices and instance compatibility

The pinned Cycles engine adds displacement to mesh buffers in place and
evaluates a mesh using its first associated object. The adapter therefore
retains an independent, immutable snapshot of each polygon resource,
including positions, corner attributes, bindings and motion samples. It
restores that snapshot before a displacement change; repeated edits do not
accumulate offsets. Switching to Bump restores the original geometry.

The snapshot also supports enabling displacement later without recooking
the SOP. Mutating the caller's original C++ MeshData cannot alter a retained
snapshot. The headless lifetime fixture tests this explicitly. Geometry
created before an object binds it uses the default shader until binding;
the assigned shader is then marked used so the engine compiles it.

Compatible object-space instances share private mesh buffers. Displacement
using world coordinates, normals or Object Info conservatively includes
the relevant object state in a variant key. Transform-dependent variants
include the complete centre transform; identity-dependent variants include
the renderer random ID. Handedness is included for displaced geometry.
Different bindings are already separate native geometry resources.

The primary mesh serves one compatibility group. Additional private meshes
serve incompatible groups and retire when their last user disappears or
the method no longer needs them. Moving an instance cannot leave another
instance with that object's displaced vertices. These variants cost memory;
they are counted rather than hidden behind a logical prototype count.

True displacement splits shared source vertices when incident corners have
different material slots, UVs, normals or Cd. This prevents the engine's
first incident triangle from selecting another face's displacement state.
Original smooth corner normals are preserved when splitting a seam.
Discontinuous offsets can create physical gaps; the adapter does not invent
stitching across an authored discontinuity. Bump-only rendering retains the
original topology.

Camera-only edits retain non-subdivided geometry without camera-dependent
displacement and skip variant synchronization. Display-only changes retain tracing. A surface-only shader
edit uses the engine's displacement hash to avoid restoring vertices when
its displacement branch is unchanged. The general engine shader-update
call marks every socket modified; the adapter preserves the more precise
hash result before that call.

## Motion and recovery

Recorded patch 0024 evaluates supported displacement at each owned shutter pose,
using that pose's original position, normal and object transform. The result is
stored in the corresponding motion buffer before tracing. This supports scalar
normal displacement, position-dependent displacement, object/world vector
displacement and compatible procedural graphs. Static geometry with a moving
world-dependent displacement receives explicit motion buffers. Subdivision retains
the corresponding generated poses. Three- and five-sample native fixtures match
independent, already-displaced reference meshes on CPU and RTX 4090 OptiX.

Animated displacement parameters, Scene Time and file-texture selections now
receive separately owned shader graphs at the uniform shutter times. Patch 0025
evaluates those states through private shaders, whose dependencies contain only
the displacement branch. Surface shading, material identity and Cryptomatte
remain on the nominal material. Changing or breaking an independent surface-color
texture does not remove displacement motion or rebuild the displaced mesh.
Changing an off-frame displacement image does rebuild the affected geometry from
retained source. The native viewer and SOHO batch adapter use the same capture
policy. The object Motion Blur opt-out also disables generated displacement motion.

In Deformation mode, polygon meshes also capture aligned shutter UV and Cd
values. Shader evaluation reads those values at the corresponding time; ordinary
surface shading uses the same interpolated attributes. Texture Coordinate UV,
the default UV Map, Cd Attribute/Color Attribute and UV-driven data textures have
independent motion-image checks. Linear and Catmull-Clark subdivision interpolate
the field samples, including separate smooth face-varying channels per stored
state. Generated displacement seams include all stored states. Constant samples
are discarded rather than retained as repeated attribute payloads.

Transform Only and Velocity retain their nominal shading-attribute policy; choose
Deformation to sample animated mesh UV/Cd and named numeric fields. Attribute,
Color Attribute and named UV Map inputs can drive sampled true displacement.
Mesh UV Tangent, tangent Normal Map and tangent Vector Displacement now use
corresponding position/normal/UV samples as well. Radial Tangent displacement
motion, Bump and pending coordinate contexts retain their guards. Missing or invalid
shutter fields keep usable shading/geometry. When an affected field contributes
to true displacement, the component uses requested-frame displaced geometry and
omits incompatible motion. Other geometry, independent camera/object motion and
independent displacement inputs remain valid. Repairs restore motion automatically.
Unused field failures stay quiet, while an explicitly requested UV pass counts as
a field consumer. Shutter-only warnings clear when motion blur is disabled.

The fallback uses the requested-frame pose, including offset shutters. The native
adapter retains corresponding nominal positions, normals, UVs and colors, plus
complete nominal OBJ/packed transforms. Shader repairs restore the original
shutter samples without geometry recapture. Offset shutters require additional
owned recovery storage, included in the reported geometry payload estimates.
Named numeric field samples are retained alongside nominal field values.
Owned COPernicus images now evaluate fractional shutter samples; legacy COP2
uses its integer image-frame sampling, with explicit pinned-frame sequences
supported. Independent images verify sampled deformation, generated subdivision
vertices, packed variant/sharing policy and local height/color recovery. Five
batch frames also match independently calculated motion centroids on CPU and
RTX 4090 OptiX. See [COP image behavior and evidence](../dev/notes/native-cop-images.md).
Native packed-prototype deformation remains separate work. Shared static packed
prototypes can use temporal shaders with packed transform sampling. Linear subdivision preserves
authored corner normals and their motion samples (patches 0026-0027). Displacement
animation in the motion-vector pass when blur is disabled is not implemented or
certified by these tests.

Sampled evaluation temporarily batches shader inputs and outputs for all shutter
poses. Temporal shaders add owned graphs, compiled shader states and image
resources. Their capture/compilation cost, extra temporary memory and large-scene
latency need measurement. Small correctness fixtures do not certify the heavy-scene
responsiveness budgets. Geometry samples interpolate between the captured poses;
high-frequency shader animation is not evaluated continuously for every ray.

## Diagnostics and ownership

Session status exposes:

| Field | Meaning |
|---|---|
| `geometry_resources` | Logical native/core geometry resources, including other geometry types. |
| `mesh_variant_resources` | Private polygon meshes, including each resource's primary mesh. |
| `mesh_variant_bytes` | Estimated payload bytes of private mesh buffers. |
| `retained_mesh_source_bytes` | Canonical source payloads and any prepared seam snapshots. |
| `displacement_rebuilds` | Restorations or creations associated with true displacement or its removal. |
| `owned_geometry_bytes` | Estimated managed geometry payload, including retained source and variants. |

Byte estimates exclude container overhead, allocator capacity, generated
engine attributes, BVHs and graphics residency. Device allocation counters
are reported separately. Source snapshots increase host memory compared
with earlier builds; historical million-triangle measurements do not
certify this new ownership path. Large fixtures, physical process memory,
edit latency and bounded-cache budgets remain required.

Source snapshots, prepared seams and additional variants are released
when a geometry resource retires. Session shutdown releases all managed
geometry and tracked device allocations.

## Verification and example

`tests/houdini_displacement_smoke.py` checks 16 native groups per device:
independent depth/position, 24 alternating edits, Bump/True/Both transitions,
surface-only and camera edits, compatible/nonuniform instances, world
variants, deletion, typed failures, scalar normals, tangent axes, material
seams, supported deformation, motion guards and shutdown. It also renders
three animated frames through the native SOHO batch path.

`tests/core_displacement_smoke.py` runs six ownership/lifecycle checks per
device and compares full images with independently constructed, already
positioned reference meshes. The four beauty images match exactly on CPU
and OptiX. Interior depth, position Z and normal Z also match exactly.
The references never replay the plugin's converted displacement scene.

`tests/houdini_displacement_ipr_smoke.py` exercises the actual native viewer
and shared panel: edits, undo/redo, method changes, camera/SOP changes,
invalid connections, recovery, rename, viewer closure and shutdown.

`tests/houdini_sampled_displacement_smoke.py` compares eight three/five-sample
images with analytic reference meshes constructed separately by the harness. It
also checks subdivision, camera-only retention, supported animated inputs, direct
final rendering and five actual ROP/SOHO batch frames. Beauty comparisons use
strict numerical budgets; stochastic position-pass averages use recorded
statistical bounds. `tests/houdini_sampled_displacement_gui_smoke.py` changes
True/Bump through the native parameter pane while one worker/viewer remains open.
The displacement-motion recovery fixtures now use actual missing shutter UVs
and a separately tracked coordinate-basis limitation. They preserve independent
object/camera motion, offset-shutter poses and shader-only repair coverage.

`tests/houdini_temporal_displacement_smoke.py` adds animated parameters, Scene Time,
file sequences and combined SOP/shader deformation at three/five shutter samples,
plus offset shutters. It compares sixteen images with independently positioned
reference geometry, checks independent color/displacement dependencies, authored
subdivision normals, shared packed prototypes, object blur opt-out, immutable
snapshot transport, malformed sample counts, stable warm tracked allocations,
four direct-final states and five actual SOHO frames. The corresponding GUI test
uses one viewer and one Render click, edits native parameters including the image
filename, and checks twelve displayed healthy/broken/repaired states. Both devices
have passing evidence. Open
`artifacts/examples/temporal-displacement/temporal-displacement.hiplc` for that small
correctness example; the courtyard remains the heavier artist showcase.

`tests/houdini_sampled_attribute_displacement_smoke.py` compares thirty images
with independently pre-displaced meshes: six input routes, three/five samples,
offset shutters and both subdivision types. It checks missing/non-finite fields,
repairs, unused attributes, independent surface/displacement inputs, the ROP Cd
input, blur controls, camera/object edits without mesh conversion/rebuilding,
warm tracked allocations, nine direct-final states and five actual native frames.
The companion GUI test checks twenty-one displayed states with one worker/viewer,
including deleting and restoring `uv` in Houdini's native Attribute Delete field.
The focused example is `artifacts/examples/sampled-attributes/sampled-attributes.hiplc`.
These small fixtures do not certify arbitrary seam populations or large scenes.

`tests/houdini_named_field_displacement_smoke.py` compares twenty independent
images per device for scalar/integer/vector/float2 fields, Color Attribute, named
UV Map and combined inputs, including linear and Catmull-Clark subdivision.
Missing/type-changing shutter fields recover and repair locally; a broken surface
field preserves independent displacement. Camera/object retention, sixteen warm
resource alternations and three direct-final states are checked. Its GUI companion
checks fifteen displayed states using native parameter edits with one persistent
viewer. CPU and RTX 4090 OptiX pass. The focused example is
`artifacts/examples/named-field-displacement/named-field-displacement.hiplc`;
the current index is `artifacts/verification/tangent-bases-20261017/index.json`.

`tests/houdini_tangent_basis_smoke.py` adds 32 primary independent images and
28 bare/frozen recovery references per device. These cover float2/float3 UV sets,
mirroring, normal-map conventions, Original/Displaced bases, changing UVs and
geometry, and a shared Normal Map feeding both surface and true displacement.
Reference meshes are explicitly displaced and shaded through independent vector
algebra, without UVs or tangent/normal-map/displacement nodes. Linear and
Catmull-Clark named-map dicing also match a separately authored default-UV route.
Missing/type/non-finite nominal and shutter inputs, repairs, three direct-final
states and five native batch frames pass CPU/RTX 4090 OptiX. The GUI companion
adds 22 displayed native-parameter states per device, with one viewer and no
manual refresh. The small fixture is
`artifacts/examples/tangent-bases/tangent-bases.hiplc`.

Patch 0030 creates only requested original/displaced tangent bases, with owned
motion buffers and cancellation checks between shutter states. A shared Tangent
or Normal Map node uses the original basis when compiling displacement and its
applicable surface basis when compiling shading. Patch 0031 fixes a centre-normal
lookup in the pinned engine's moving Normal Map path. All consumers and six
kernels were rebuilt together. Sixteen warm edits have stable per-state tracked
resource counters; physical memory and large-scene latency are not certified.

Open `artifacts/stage2/displacement/native-displacement.hiplc`. Its renderer
is `/out/native_displacement_cycles`, source is
`/obj/displacement_receiver/grid1`, and material is
`/mat/native_displacement`. The HIP includes additional seam, UV and
deformation fixtures used by the tests. It was saved with the installed
Houdini Indie license.

```powershell
./scripts/run-hython.ps1 -HythonArguments 'tests/houdini_displacement_smoke.py'
./scripts/run-hython.ps1 -HythonArguments 'tests/core_displacement_smoke.py'
./scripts/run-gui-test.ps1 -Test houdini_displacement_ipr_smoke.py -Device CPU
```

## Holding unavailable mesh shader fields - October 18, 2026

Native mesh deformation no longer freezes valid positions merely because a UV,
Cd or named numeric shader field is missing, invalid or changes tuple size at a
shutter sample. The converter holds that entire affected field at its valid
requested-frame value throughout the shutter. With an offset shutter this is
the separately acquired requested frame, not the shutter centre. A field absent
at requested time is omitted instead; dependent shader branches recover locally.
No UV/color value or diagnostic magenta displacement is invented.

The held field is shared by its surface, AOV and displacement consumers.
Independent fields, shader-parameter animation, geometry positions and valid
object/camera motion continue. Compatible packed prototypes still share. Source
warnings name the field and fallback; repairs restore full sampling and clear
warnings without restarting IPR. Unused source attributes remain quiet.

Recovery produces finite, fully owned constant attribute arrays before strict
engine validation. Core contracts and worker protocol 38 remain unchanged.
Requested-frame field-layout differences no longer invalidate otherwise valid
mesh correspondence. At that milestone, Vector Transform and sampled Generated retained separate
motion recovery. The later spatial-transform milestone below supersedes that
limitation for its tested inputs.

CPU, RTX 4090 and RTX 3080 Ti each pass 209 independent coverage/cache/recovery
checks, three direct-final states and five ordered packed final frames in one
Persistent Data session. They cover missing/non-finite default/named UVs, Cd,
named scalar/color fields, image UVs, 3/5 samples, offset shutters, subdivision,
packed sharing, shader animation and cached navigation. Frame three holds a
broken field and the following frames render normally. Warm tracked counters
remain bounded across repeated failures and repairs.

Both heavy single-viewer Qt/HOM workflows pass fifteen native parameter edits
and navigation events on RTX 4090 beside a million-triangle prototype shared
by twelve instances. Camera/object navigation retains actual SOP cook counts,
geometry conversions and displacement rebuilds. Tangent/Normal Map references
add fourteen independently pre-displaced held-UV images, retaining geometry
rotation with a fixed UV basis. Other existing field, basis, nominal-pose,
material-motion, normal and image-contract regressions pass. Unfiltered position
comparisons use common ray hits across independent moving renders, exact depth
and bounded XY error; their beauty comparisons remain independent full images.

This verifies component-level mesh field recovery, not physical mouse input,
physical memory, arbitrary populations or production latency. Exact source
archives, baseline/failure reports and passing runs are in
the evidence index (`artifacts/verification/shutter-coordinates-20261018/index.json`).

## Sampled Generated coordinates and spatial transforms - October 18, 2026

Host 20261018T133416420 supports Generated and Vector Transform inputs in sampled true
mesh displacement. Generated is normalized from the requested-frame canonical
positions, with zero on degenerate bounds. It stays anchored through the shutter,
including offset shutters and split render vertices. Auxiliary shutter-only
graphs contribute their Generated demand; surface-source failure/repair retains
independent displacement. The canonical seam map is owned only when demanded.

Point, vector and inverse-transpose normal conversions use sampled object
transforms, including nonuniform and negative scales. Camera conversions follow
the requested-frame camera matrix; camera rays retain their independently sampled
shutter motion. Camera matrix edits rebuild only displacement that depends on that
matrix. Ordinary camera navigation and orthographic lens-only changes retain
geometry and displacement buffers. Patch 0041 fixes a double world-position
transform in motion displacement setup, independently exposed by Geometry
Position driving a Vector Transform. All six kernels were rebuilt together.

CPU and both OptiX devices each pass 276 comparisons and five ordered final
frames through one Persistent Data session. Their numeric reference geometry
does not use Generated, Vector Transform or displacement shaders. Cases include
3/5 shutter samples, offset shutters, velocity/deformation, animated inputs,
Geometry Position/Normal, material seams, subdivision, parented packed instances,
cached camera edits and bounded warm tracked allocations. Supported identity
conversions no longer emit old unsupported-basis warnings.

Both heavy one-viewer Qt/HOM workflows pass sixteen edits, navigation and repair
events on RTX 4090 with one worker and no refresh/restart. They combine a 65k
triangle edited mesh and twelve instances of a million-triangle prototype.
Actual SOP cook counts and conversion counts stay unchanged for camera/object
navigation. Matrix-dependent displacement rebuilds when needed; ordinary camera
edits do not rebuild it. World/color native IPR and the scoped motion, binding,
attribute, basis, normal and compact-output regressions pass.

Other unsupported displacement contexts retain local recovery. This is not
physical mouse, physical memory, mixed-device or production latency certification.
Exact sources, independent reference images and reproduced failures are in
the evidence index (`artifacts/verification/spatial-displacement-20261018/index.json`).

## Dense displaced geometry reference precision

The October 19 large mesh-light comparison exposed numerical differences between
fresh and retained dense displaced geometry after an object translation. A separate
uncached worker receiving the same edits matches the cached worker exactly;
fresh reconstruction can differ by about 6e-8 in Position and .005 in Normal,
affecting stochastic lighting samples. This also occurs with cached flattening
forced serial and with caching disabled. It is not attributed to parallel writes.

The small fresh-reference lighting suite is retained. The large optimization
suite additionally compares separate cached/uncached workers with matched edit
histories and unchanged pixel tolerances. The diagnosis and failed original run
are preserved with [the light-tree evidence](../dev/notes/native-parallel-light-tree.md).
Further displacement numerical-stability work remains; no kernel change is part
of that optimization.
