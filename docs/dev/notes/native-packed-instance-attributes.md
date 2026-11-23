# Packed instance shader attributes

Packed instances have no fixed minimum scale. Nonzero invertible transforms,
including tiny and negative scales, are retained. A finite collapsed transform
(such as `pscale=0` or a zero axis) hides that packed instance and its nested
contents without rejecting the scene. Restoring its scale restores the instance.
If only a shutter sample collapses, packed motion falls back to the requested-frame
pose; independently valid OBJ motion and other instances retain their blur.
Subpixel coverage and the engine's floating-point precision still limit visible
detail. This does not make arbitrarily small world-space geometry accurate.

Use **Cycles Attribute** with a field name such as `rand` or `counter` to read
numeric data on packed primitives or their points. An ID attribute is unnecessary.
Integer values convert to the renderer's float precision, as other numeric shader
fields do. Numeric tuples have one, two or three components.

Prototype geometry fields take precedence. Otherwise, the nearest packed layer
supplies its primitive, single-vertex, point or detail value, in that order.
Ancestor packed layers supply a value only when a nearer layer has no such field.
An invalid nearer field recovers locally instead of selecting a different value
from an ancestor. Unused attributes remain ordinary Houdini data.

The adapter reads demanded values from preserved geometry during background
conversion. Owned numeric values cross the persistent worker pipe; host geometry
and attribute pointers do not. Cycles stores these values on individual render
objects, so different shading values share one prototype. Availability/type
differences can require binding variants. Values that influence true displacement
create the geometry variants needed for their different resulting shapes.

Missing, non-finite or unsupported requested fields retain visible geometry.
Affected colors receive diagnostic shading; dependent normal/displacement branches
are omitted. Independent valid displacement remains active. Diagnostics identify
the source and attribute, aggregate repeated failures and clear after repair.
Unregistered AOV dependencies do not publish unused-field warnings.

Uniform instance tuples cannot provide mesh UV derivatives. Tangent, tangent-space
Normal Map and tangent Vector Displacement require usable UV data on the prototype;
an unsuitable instance source recovers locally. Sparse volume requirements continue
to use their grid-field adapters.

Changing numeric instance values are sampled across the shutter and interpolated
at ray time. This includes scalar, integer and two/three-component numeric fields
and packed `Cd`. See [shutter-sampled instance fields](native-instance-attribute-motion.md)
for timing, ownership, recovery and displacement limits. Independently supported
prototype deformation and camera/object motion remain available.

## Updates and ownership

Both snapshot differences and the renderer's unchanged-scene check include object
attributes. Attribute-only IPR edits therefore reach the engine even when object
transforms and geometry are unchanged.

Packed deformation resource names retain their field layout without accumulating
suffixes. Acquired shutter payloads are checked independently of the outer SOP's
cook count. If Houdini generates equivalent new buffers, background snapshot
assembly retains the previous immutable geometry owner. Unchanged render geometry
is not retransmitted in a geometry packet. Cycles still synchronizes its object,
attribute and light tables; these checks do not establish zero device traffic.

The current experimental worker protocol is **49**. Restart Houdini after updating
so the host module and worker use the same schema. Historical verification below
retains the protocol and builds used for each earlier increment.

## Scoped verification

Host `20261018T224833489`, Houdini 22.0.429/Python 3.13, 45 upstream patches,
five compiled artifacts and six kernels:

- CPU, RTX 4090 OptiX and RTX 3080 Ti OptiX each pass 60 image/update/recovery
  checks and five ordered ROP output frames, using IPR's unchanged-scene reuse.
- Float and integer fields cover meshes, point spheres, hair, mixed and nested
  packed contents, field edits, failures/repairs, prototype precedence and
  deforming prototypes. Two/three-component colors and tangent recovery have
  additional mesh checks. Position passes independently check displacement.
- Both viewers pass 13 live edit/repair states separately on the 4090, with one
  viewer open and a retained 1,048,576-triangle prototype shared twelve times.
  Camera, object and ordinary field edits preserve conversion counts; the heavy
  prototype's cook count stays unchanged. Closed owned/device allocation counts
  return to zero.
- The same round checks shader layout/input states, sparse pose ownership and
  transport, compact images, volumes, packed motion, named fields, ramps and the
  native world/color IPR shutdown regressions.

The immutable reports and exact sources are indexed by
`artifacts/packed-instance-fields-evidence-20261019.json`. GUI evidence uses Qt/HOM;
physical-input latency, prolonged memory behavior and mixed-device certification
remain production gates.

## Explicit Vector output follow-up — October 19, 2026

The Attribute node's **Vector** output reads these same tuples. Three-component
vectors retain XYZ; two-component fields supply XY with Z zero. Different values
on copies do not require different prototypes unless they affect geometry, such
as true displacement. Use the field's name; no `id` attribute is required.

The follow-up retains host `20261018T233558847`, protocol 44 and all five compiled
artifacts from the retained-emitter fix. CPU, RTX 4090 OptiX and RTX 3080 Ti OptiX
each pass 100 checks and five ordered output frames. Forty checks per device
explicitly exercise the Vector socket on packed meshes, points, hair and mixed
contents, with both primitive and point ownership, two/three-component values,
value edits, a non-finite component and repair. Ordinary value edits retain the
prototype's cook/conversion counts.

Each viewer separately passes 20 live states on the 4090 while retaining a
1,048,576-triangle prototype shared twelve times. These include Vector-socket
edits, ownership changes, local failure/repair, and the Principled Surface-first
connector layout. Both close with zero owned geometry and device allocations.
The audited reports are indexed by
`artifacts/packed-vector-evidence-20261019.json`.

## Constant fields on unpacked copies

Point values copied onto an unpacked mesh now remain exact when all triangle
corners agree. This prevents floating-point interpolation from creating White
Noise patterns on otherwise uniform pieces. Packed and unpacked fields, actual
gradients, subdivision and live repair are covered by the independent checks in
`native-constant-attributes.md`. Neither packing nor manual rounding is required
for a constant per-piece field.


## Cd and white fallback defaults - October 20, 2026

**Attribute** with `Cd` and **Color Attribute** with `Cd` or an empty layer now
read uniform packed colors using the ownership rules above. Compatible `Cd` is a
three-component float color. An ID/name is unnecessary. An authored prototype
`Cd` retains precedence; otherwise the nearest packed primitive, single vertex,
point or detail field supplies the color. Enclosing packed layers remain available
when a nearer layer has no field.

Mesh and curve colors support vertex, point, primitive and detail ownership,
including render-time subdivision. Point spheres use point/detail fields and
unambiguous particle-primitive values; mesh vertex colors are not implicitly
reduced to loose points. Missing or invalid used fields retain visible geometry,
with source-specific diagnostics and local repair. Independent displacement stays
valid; a broken shared color/height dependency disables the affected height branch.

Surface colors stay on individual render objects, preserving one shared prototype.
Different colors affecting true displacement create the required shape variants.
The ROP fallback preserves its selected shader, tint, roughness/metallic and emission
strength on packed colors. Its default **Material color** is white `(1, 1, 1)`;
saved colors are retained and can be reverted to defaults. Blender shader-node
constant defaults are separate from this unassigned-geometry fallback.

The final development runtime `hosts/20261019T220335544/resources` passes 128
Cd/owner/edit/recovery checks and five output frames separately on CPU and each
OptiX GPU (4090 and 3080 Ti). The tests include Attribute and Color Attribute,
packed/unpacked mesh domains, nested instances, points/hair/mixed components,
subdivision, actual position-pass height checks, ROP material parity and rendered
white defaults. Both viewers separately pass 26 live edits/repairs on the 4090,
retaining the million-triangle prototype shared twelve times at one cook. One
worker stays active and closed allocation counters return to zero.

Exact sources, regressions and preserved failures are recorded in
`artifacts/packed-cd-evidence-20261020.json`. At that earlier increment, instance
values were held at the requested frame over a shutter; the current sampled-field
implementation supersedes that restriction. Four-component Cd/Alpha tuples, implicit point
vertex reduction, physical-input timing, prolonged memory and mixed devices are
outside this scoped certification.

## Current sampled field verification - October 21, 2026

Changing uniform instance fields are implemented and verified on runtime
`hosts/20261020T234635239/resources`, worker protocol **49**, with **66** engine patches. The
final immutable evidence (`artifacts/sampled-instance-overrides-final-evidence-20261021.json`) records **37** selected passing native runs:
**28** feature runs include **528** live viewer states and **40** ordered output
frames across CPU, RTX 4090 OptiX, RTX 3080 Ti OptiX and mixed-device rendering.
The older sections above retain their original runtime and certification scope.

The [sampled-field guide](native-instance-attribute-motion.md) describes the
separate requested-frame value, ray-time numeric interpolation, owner precedence,
local recovery and displacement limits. The current checks retain independent
motion and shared surface-only prototypes through edits and repairs; they also
verify native ramp/curve overrides without merging unrelated material identities.

An early object pose preview is observed separately from the later authoritative
capture. Native IPR can apply its SOHO update first, so an early preview is
opportunistic there. Authoritative field sampling can cook source SOPs while
heavy prototype geometry remains retained. This evidence does not establish
zero device traffic, physical-input timing, a long-session memory plateau or a
complete production/release certificate.
