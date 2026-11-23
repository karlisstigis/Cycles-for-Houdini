# Native Material SOP overrides

The native OBJ/SOP adapter reads Houdini Material SOP Local Overrides without
changing the assigned Cycles builder. Promote the desired shader parameter to
the builder, assign that builder with a Material SOP, then choose the exported
parameter under Local Overrides. The same resolution is used in the **Cycles Render** panel,
native Houdini IPR and ordered final rendering.

Scalar, integer, toggle, menu, string, color and vector fields are supported,
including native count/stop dictionaries for whole ramps and complete promoted
curve channels.
Native color component names and whole tuples retain their authored ordering.
Detail, primitive, point and supported vertex layers follow native binding
precedence. Spheres retain their containing-particle context. Point/vertex
layers can bind a whole curve when their final effective assignments agree at
all authored keys; varying assignments recover to a supported uniform layer.
See [binding, expression and optics contracts](../dev/notes/native-binding-expression-optics.md).
Packed prototypes retain embedded assignments. Distinct effective shaders use
separate logical binding declarations; compatible polygon instances retain
physical mesh/BVH sharing. Actual displacement or geometry divergence requires
shape variants. Point/hair palettes retain their component-specific storage
and compatibility boundaries.
Volumes use their valid density field and the assigned volume builder.

Overrides are owned dictionary literals, parsed with bounded AST validation.
They are never executed. Promoted channel references and the bounded numeric
HScript subset described below work. Unsupported expressions, structural subnet
interface changes and unknown/type-incompatible fields are omitted locally with
diagnostics.
Independent valid fields and supported underlying bindings remain active.
Inactive reference expressions do not produce override warnings. Missing color textures use
magenta while independent valid displacement remains intact. File repairs and
parameter repairs restore the intended shading and clear warnings automatically.

The authored material identity and Cryptomatte name survive an override; using
a common fallback does not merge unrelated source material identities.

Object/detail/primitive and packed stylesheet assignments and material-parameter
rules now use this evaluator too; see
[native material stylesheets](native-material-stylesheets.md) for supported
categories, live recovery and the remaining stylesheet work.

## Whole ramps and curve channels

Use Material SOP's native Local Overrides entries for the promoted count and
stop parameters. There is no additional private whole-ramp literal format.
Whole Color Ramp promotion exports two native ramps and the global controls;
a promoted curve channel is an ordinary native float ramp.

For a Color Ramp promoted as `palette`, the Color count is `palette`, positions
`palette1pos`, colors `palette1c` and bases `palette1interp`. Alpha has its own
stops: count `palette_alpha`, positions `palette_alpha1pos`, values
`palette_alpha1value`, bases `palette_alpha1interp`. Global controls are
`palette_color_space`, `palette_interpolation` and `palette_hue_interpolation`.
Material SOP expands existing color/vector tuples into their native components.
Materials promoted before the inline ramps keep `palette1c` RGBA tuples with
Alpha `palette1ca` until converted. Override types and component ordering follow
the exported Houdini parameter, including newly requested stops.

The evaluator owns descriptors for added stops without creating channels on
the artist's material. An incomplete added stop rejects the contributing
count override locally, retaining valid controls. Stops excluded by the
effective count are not evaluated, so dormant broken controls stay quiet.
Color and Alpha source failures recover independently. Valid overrides and
independent displacement survive an authored fault in the other output.

Shader curve channels are inline native float ramps (2026-10-29). Promote one
like any native ramp:

```python
cycles.promote_parameter(builder, vector_curves, 'curve_x',
                         name='shape_x', label='X Curve')
```

Overrides use the native ramp fields: count `shape_x`, stops `shape_x1pos`,
`shape_x1value` and `shape_x1interp`, with the same owned-descriptor and local
recovery rules as other native ramps. Float Curve, Combined/R/G/B and X/Y/Z
channels all use this route. Range and Extension remain independent ordinary
controls. The earlier Interior Points bundles (`curve_x_points`) were removed.

## Contributing channel expressions

Numeric substitutions support finite constants, parentheses, unary plus/minus,
`+`, `-`, `*`, `/` and chains through other exported channels. Literal channel
calls are `ch("path")`, `chf("path", frame)` and `cht("path", seconds)`.
Integer channels also use `ch`. Strings support a direct literal `chs("path")`
reference. The numeric function allowlist is:

| Function | Arguments |
| --- | --- |
| `abs`, `floor`, `ceil`, `int` | One numeric value |
| `min`, `max` | Two numeric values |
| `clamp`, `fit01` | Three numeric values |
| `fit`, `efit` | Five numeric values |

`$F`, `$FF` and `$T` use the actual requested render/shutter time, without
changing Houdini's timeline. `$F` rounds as `floor(frame + 0.5)`, `$FF` retains
the fractional frame, and `$T` is seconds. `chf` uses Houdini's frame-to-time
conversion. Explicit off-frame changes and deletion/recreation remain watched
even when requested-frame values do not change. Actually read animated controls
are attributed to their consumer; a constant override does not retain unused
animation from the replaced source.

Houdini can register a Python channel dependency on its first active query.
The evaluator observes those newly registered links during that same capture,
then applies supported HScript substitution or omits the contributing Python
override with a local warning. Independent operation, shading and displacement
values remain active. This observes the existing override-source graph; it does
not scan the builder or evaluate unused nodes. Constants and known deferred
controls do not repeat the observation. Native ramp counts retain the numeric
count API, without evaluating discarded stops. Exact integral expression counts
are accepted; fractional counts recover locally rather than being rounded.

This is an explicit subset, not an HScript or Python interpreter. `chi` is not
a stock HScript function in the pinned SDK. Dynamic paths, compound strings,
other functions and authored Python are unsupported for override substitution.
A contributing unsupported expression
omits its affected override locally; inactive controls remain quiet. Division
by zero, cyclic references and unsafe numeric results recover the same way.
A malformed value in an otherwise valid literal does not discard unrelated
valid fields; a structurally malformed or oversized layer is omitted as a layer.

Each literal layer is limited to 64 KiB UTF-8, 512 fields and 4,096 AST nodes.
Color ramps retain 1..32 stops. Curve channels retain 0..62 interior points
(2..64 points including endpoints). Expressions are limited to 4,096 UTF-8 bytes,
256 AST nodes and syntax/reference depth 32; the syntax cache has 256 entries.
Numeric results must fit finite float32. Deferred Int controls use native
rounding, then Toggle/Menu and declared strict Int bounds clamp before the
final int32 check. Values must first be safe for native int64 conversion;
overflow recovers locally. HScript `int()` itself truncates toward zero.
Native ramp counts retain exact-integral validation. True varying per-key
bindings, broader expressions and structural subnet changes remain unsupported.

## Large assignment populations and recovery

Ordinary material paths have no arbitrary 4,096-string rejection. Missing paths
still receive separate diagnostic materials and Cryptomatte names. Native
overrides have a per-component budget of 4,096 effective binding combinations
and 8 MiB of owned literal payload. This counts surviving assignment layers;
point material switches do not count parent layers they discard. The budget is
not a global scene or GPU-memory limit. Equal parsed values with different
literal strings may still be distinct captured bindings.

When that budget is exceeded, omit point overrides first, then primitive, then
detail as necessary. Preserve material paths, valid lower-priority layers and
geometry. A concise `material.override_capacity` diagnostic names the real SOP
and the omitted layer. Repairs restore the authored values and clear warnings;
camera-only cache hits retain current diagnostics. Mesh and point components
inside one SOP have independent policies. Packed instances retain compatible
prototype sharing. Point binding caches also include the containing primitive,
so an explicit point path inherits only the matching parent material's values.

CPU and both OptiX devices pass 22 capacity checks and 20 binding checks: 5,000
packed copies, the exact 4,096 boundary, 8,192 combined particle bindings, mixed
component repair, a 12 MiB literal population, packed material switch precedence
and 5,010 distinct missing material/Cryptomatte identities. Each device writes
five frames, including a recoverable overflow on frame three and later repair.
Four one-viewer Qt/HOM workflows pass on RTX 4090, using native keyboard edits,
file autorepair and camera movement with two million-triangle sources and twelve
shared instances. Native assertions use Houdini UI callbacks because a plain Qt
timer can stop advancing in its pane. This is scoped automated UI evidence, not
physical mouse, whole-process memory, mixed-device or latency certification.
Native Qt grabs can contain toolbar artifacts; the native pixel API independently
checks the delivered image. OS-screen appearance is not certified by those grabs.

Exact sources, failures, repairs and reports are retained in
the capacity evidence index (`artifacts/verification/material-capacity-20261018/index.json`).

## Reuse and verification

Logical override keys include their values. For polygon meshes, the worker can
retain the physical mesh and shader across a key change when every owned source
geometry field, material-slot layout and surviving object-owner group matches.
Real displacement changes still rebuild displacement. Geometry, attribute,
topology, subdivision, motion, grouping or incompatible recovered bindings keep
the ordinary update path. No additional scan is added to camera-only updates.
Capture/conversion/transport work can still occur for an override edit; retained
GPU storage is not a claim that all host-side work has been eliminated.

Engine patch 0040 separates generic attribute allocation from primitive/BVH
storage and chooses the actual position/radius backing buffer when deciding
copies. An authored Generated-coordinate requirement stays resident through
temporary color-texture recovery.

Scoped checks cover CPU, RTX 4090 OptiX and RTX 3080 Ti OptiX: parameter types,
local failures/repairs, faces/points/hair/packed/mixed bindings, color/displacement
independence, adaptive subdivision and volume extinction. The basic test writes
five frames on each device. Sixty-four distinct override values retain bounded
owned geometry and shader counts; real position changes update and restore the
image. These tracked counters are not physical-memory certification.

Both one-viewer GUI workflows pass on RTX 4090: native parameter keyboard edits,
warning repair, automatic file repair and camera edits. The edited plane has
1,048,576 triangles, alongside a separate 1,048,576-triangle prototype shared by
twelve packed instances. GPU geometry and independent displacement stay resident.
Qt/HOM workflows and image checks are not physical mouse or latency certification.
Exact reports, tested manifests and archived sources are in
the evidence index (`artifacts/verification/material-overrides-20261018/index.json`).

## Material overrides across the shutter

With Geometry Motion set to Deformation, native mesh, point and hair capture
retains separate geometry correspondence and requested-frame material bindings.
Changing an override or material path between shutter samples no longer discards
valid geometry motion. No additional user-authored IDs are needed when ordinary
geometry correspondence is valid. This does not invent correspondence for
changing topology.

True polygon displacement uses owned shutter snapshots of the effective native
material, including literal overrides, shader animation and image paths. The
requested-frame surface and Cryptomatte identity remain intact. Packed copies
share geometry when their complete sampled binding histories agree; genuinely
different displacement histories require explicit variants.

Off-frame shader fields are captured only when displacement needs them. Newly
required fields trigger capture; removed demands retire their owned storage.
Invalid off-frame material bindings retain requested-frame displacement values
and valid SOP motion, with a `motion.displacement_binding` diagnostic. A broken
off-frame displacement field or texture uses valid requested-frame displacement
across the existing geometry motion, reporting `motion.displacement_samples`
and the affected field where available. Repairs restore the intended result and
clear warnings automatically. File and off-frame operator paths stay watched.

CPU and both OptiX devices pass 37 checks against independently authored motion
images, including 3/5 samples, offset shutters, scalar/vector displacement,
Linear/Catmull-Clark subdivision, packed sharing/splitting/merging and moving
points/hair. Each writes five final frames, including a recoverable binding
failure and later repair, through one Persistent Data session. Sixteen distinct
warm timelines retain bounded tracked geometry, device and shader counts. These
counters do not certify physical memory.

Both single-viewer Qt/HOM workflows pass 15 native parameter/file/field/camera/
object edits on RTX 4090. They retain a separate 1,048,576-triangle prototype
shared by twelve instances while updating a 65,536-triangle displaced plane.
Camera/object edits retain geometry conversions and displacement rebuild counts;
file repairs avoid geometry recapture. Native IPR image pixels are checked
independently of the Qt screenshot, whose toolbar can contain capture artifacts.
Physical mouse input and production latency are not certified by these tests.

Exact sources, failures, repairs and reports are retained in
the sampled binding evidence index (`artifacts/verification/sampled-overrides-20261018/index.json`).

## Retaining sampled geometry during surface edits

Temporal polygon bindings can retain their existing engine mesh and complete
surface/displacement shader palette when only logical override keys change.
This requires identical owned geometry fields, material-slot/shutter layout,
and surviving object-owner groups. Every shader alias must be unambiguous.
Geometry-specific recovered bindings and frozen motion fallbacks retain the
ordinary update path. Real displacement changes still evaluate displacement;
changed vertices and incompatible layouts still rebuild geometry.

The independent CPU baseline reproduced a surface-only override repeating mesh
conversion and displacement, growing shader slots from 10 to 14 and requesting
triangle-buffer copies. CPU and both OptiX devices now pass 98 image/reuse checks.
Each device checks 64 distinct surface overrides, independent color filenames,
missing-file fallback/repair and branch removal, while preserving sampled
displacement, geometry and subdivision. Retained edits request no triangle
vertex/index buffer copies. Three/five samples, scalar/vector displacement,
Linear/Catmull-Clark subdivision and real displacement/vertex repairs pass.

Both heavy single-viewer Qt/HOM workflows pass 17 native parameter/repair/camera/
object events on RTX 4090, including a surface-only override with unchanged
geometry, displacement and shader counts. Packed splitting/merging, five final
frames, the earlier override path and strict image contracts also pass.
Host cooking, capture/conversion and transport can still occur. These results
do not certify physical VRAM, physical mouse input or production latency.

Exact sources, the reproduced failure and passing reports are in
the temporal reuse evidence index (`artifacts/verification/temporal-binding-reuse-20261018/index.json`).

## Historical boundaries from the initial override milestone

The following records the original boundary and limit, rather than the current
interface. Whole ramps, complete curve channels and the numeric expression
subset are now implemented; see [whole-ramp controls](#whole-ramps-and-curve-channels)
and [current expressions and limits](#contributing-channel-expressions), including
the current 512-field layer limit. Stylesheet support and its remaining limits
are described in [native stylesheets](native-material-stylesheets.md).

> Stylesheets, per-key hair overrides, whole-ramp overrides and complex promoted
> expressions require additional adapters. Override literals are limited to 64 KiB,
> 128 fields and 4,096 AST nodes; malformed/oversized fields are omitted locally.
The capacity budget is per component; a global scene material budget and broad
production-scale population certification remain separate work. Overrides are
sampled across the shutter in Deformation, Velocity and Transform modes within
the verified scope below. Motion when requested-frame displacement itself lacks
necessary sampled coordinates or attributes remains separate work. Broader
packed/volume contexts and retained GPU storage for point, hair and volume
override edits remain separate work.

See SideFX's [Material SOP documentation](https://www.sidefx.com/docs/houdini/nodes/sop/material.html)
and [material override semantics](https://www.sidefx.com/docs/houdini/shade/overrides.html).

## Material SOP sampling independent of geometry motion - October 18, 2026

Velocity and Transform modes now acquire shutter-varying native mesh bindings
and Material SOP literal overrides without using off-frame mesh positions.
Velocity uses requested-frame v; Transform retains requested-frame geometry.
Packed transform metadata is reused from the same acquisition. No manual IDs
are introduced. Requested-frame surface shading and material identity remain.

The host acquires binding and packed-transform metadata at shutter times, then
restores one immutable nominal detail and its packed ownership ledger. Caches
keep one nominal root plus owned plain metadata per active source; off-frame
details and host callbacks are not retained. Time-dependent native bindings
with true mesh displacement can require extra Houdini shutter cooks. Camera
and object navigation reuse samples; this is not a claim that cooking is free.

Per-face histories remain distinct even when their requested-frame assignments
match. Packed histories share when nominal rendering state and exact effective
displacement graphs agree. Unused off-frame surface differences do not split
geometry. Hash buckets are confirmed by exact comparisons, and actual off-frame
operator dependencies remain watched even when their current displacement is
equivalent. Deletion and repair retain usable geometry and valid velocity.
Missing sampled fields/images freeze nominal displacement with diagnostics;
diagnostic magenta is never used as geometry displacement.

CPU, RTX 4090 and RTX 3080 Ti each pass 88 checks and five topology-changing
packed final frames through one Persistent Data session. Cases include 3/5
samples, offset shutters, scalar/vector displacement, Linear/Catmull-Clark
subdivision, cached navigation, per-face/packed histories and field/file/source
repairs. Sixteen distinct warm timelines retain bounded tracked geometry,
device and shader counts. Frame three recovers a broken shutter binding and
frames four/five render the repaired state.

Both heavy single-viewer Qt/HOM workflows pass eleven native parameter-edit,
recovery and navigation events on RTX 4090, alongside a million-triangle
prototype shared by twelve instances. Actual SOP cook counts, conversions and
displacement rebuilds stay unchanged during the four camera/object edits.
Fast camera submissions keep previous geometry-capture statistics, so those
latched statistics alone are not a measure of new cook work. Native image pixels
are checked independently of the Qt grab. No refresh or render restart is used.

At this earlier milestone, sampled Generated used nominal-displacement recovery.
The later spatial-transform milestone now supports these sampled coordinates. Other unsupported
coordinate/shader cases retain their documented recovery. Broad populations,
physical memory, physical mouse input, mixed devices and production latency
remain uncertified. Exact sources, reproduced failures and reports are in
the evidence index (`artifacts/verification/independent-bindings-20261018/index.json`).

## Recovering requested-frame coordinate fields

Native deformation now holds unavailable shutter UV/Cd/numeric fields at their
valid requested-frame values while retaining geometry motion. A requested-frame
field that is itself unavailable is omitted for local shader recovery. Field
consumers share that constant fallback; independent shader animation continues.
This complements temporal material-binding recovery rather than freezing the
whole mesh. See [mesh field recovery and independent evidence](native-displacement.md).
Generated and Vector Transform now retain sampled motion in the tested mesh
paths; see the later spatial-transform milestone in the displacement document.

## Whole-ramp and first-active expression verification - October 21, 2026

The E runtime `hosts/20261020T234635239/resources` passes 48 material checks and five
ordered final frames separately on CPU, RTX 4090 OptiX, RTX 3080 Ti OptiX, and
both OptiX devices in one session. Both **Cycles Render** and native IPR separately
pass 24 live states on all four targets. Status records confirm the exact
requested device IDs, including both adapters in mixed sessions.

The tests cover native Float/RGB/RGBA count/stop dictionaries, all five Blender
RGB interpolation modes, a complete 32-stop dictionary, all three promoted curve
types, partial layers, animated dependencies and sampled displacement. Authored
RGB and curve-X faults preserve independent Alpha/End-Y height, with warnings at
the authored material path and automatic clearance after repair. The first
unprimed relative Python read reports the omitted override while preserving its
independent height; HScript repair restores the intended owned values.

The original graph regression separately passes 18 checks and 64 warm override
values on CPU and each GPU. It covers active/inactive operation inputs, local
Python-operand recovery, overridden file repair, adaptive geometry reuse and
actual source-position changes. This existing graph test uses one device at a
time; mixed-device coverage above belongs to the new material tests.

Each new viewer retains one worker, one cook for its unchanged receiver and
heavy shared prototype, and unchanged geometry conversions. Unrelated source
fault/repair pairs preserve displacement rebuild counts. Each five-frame final
sequence uses one Persistent Data session and writes the third frame with its
recoverable binding diagnostics, cleared on frames four/five. Checked material session closures report zero
tracked owned/device bytes and inactive workers. The graph test records normal
process exit, but does not record a post-close allocation status.

Forty-eight pure checks pass. HFS-only probes confirm the actual native schema,
lazy dependency registration and passive raw-value snapshots. Test snapshots
avoid `keyframes()` and authored-value evaluation, which could prime Python
dependencies and conceal the first-read case. Active expression observation adds
metadata work; these checks do not establish an end-to-end latency budget.

Exact report/source hashes and independent image/height assertions are in the
E material evidence audit (`artifacts/sampled-material-overrides-e-acceptance-audit.json`). Source-archived queue indexes:
native-sampled-overrides-e-overrides-20261020T234750065 (`artifacts/verification/native-sampled-overrides-e-overrides-20261020T234750065/evidence/index.json`); native-sampled-overrides-e-cpu-20261020T234945349 (`artifacts/verification/native-sampled-overrides-e-cpu-20261020T234945349/evidence/index.json`); native-sampled-overrides-e-gpu-20261020T235530353 (`artifacts/verification/native-sampled-overrides-e-gpu-20261020T235530353/evidence/index.json`).

The invalid-curve sentinel still gives generic point-count wording for an
authored invalid X; its warning code, material path and recovery action are
correct. This is a diagnostic wording limit. These automated HOM/image checks
do not certify physical input/display appearance, whole-process physical memory,
long-duration production latency, public distribution or a clean-machine release.
Earlier milestone counts and runtime identities above remain historical.
