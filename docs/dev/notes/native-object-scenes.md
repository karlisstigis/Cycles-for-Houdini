# Native OBJ scenes and Houdini lights

The native Cycles ROP can now render multiple OBJ render SOPs directly through
HDK. This is an OBJ/SOP workflow with no USD export or Solaris dependency. The
explicit-SOP mode remains available; the courtyard test drive now defaults to
OBJ scene selection with native Houdini Light 2.0 objects.
This increment advances stage 3; it does not complete the production release.

## Artist controls

Choose **OBJ scene** under **Geometry source** on a Cycles Native IPR ROP. Select
an **OBJ scene root**, a camera and a device. The default object/light patterns
are `*`. Add normal Houdini geometry objects, assign native Cycles materials and
create Houdini Light 2.0 objects. Their render SOPs are used even when another
SOP has its display flag. Empty geometry containers and object subnet outputs
are not treated as renderable geometry.

**Objects**, **Force objects** and **Exclude objects** use Houdini node-pattern
syntax. Subnet patterns recursively include children, absolute paths and
`@bundle` references are supported, and exclusions take precedence. Normal
objects honor display flags, the enabled Display parameter and parent subnet
visibility at the requested render time. Force Objects includes hidden objects;
it does not alter their ray visibility. **Lights**, **Force lights** and
**Exclude lights** select native lights independently. Light Enable always
controls emission, including lights matched by Force Lights. The light icon's
viewport flag is independent of its render enable.

The [optional Cycles object/light tabs and dedicated light assets](../../user/native-object-properties.md)
are available through the native **Cycles** top menu. New normal objects retain
their factory Render parameters and acquire no Cycles properties automatically.
The dedicated asset additionally supports Disk and Spot; those modes do not
imply native Houdini disk/cone compatibility.

The old **Geometry SOP**, **Point light OBJ** and **Point light strength** controls
belong to explicit-SOP mode. OBJ scene mode uses the selected Houdini lights'
parameters and creates no default calibration light. This distinction matters
when switching the courtyard: its current key light is a transform null, so
create a Houdini Light 2.0 to supply native scene lighting.

Materials retain the existing OBJ/detail/primitive/point binding rules. A ROP
material override applies to all selected sources. New objects, render flags,
transforms, assignments, source deletion, masks and bundle membership update
both native IPR and the dedicated panel. They share the same renderer. Batch
uses the same scene-source function and evaluates animated selection and
transforms at its explicit frame time without moving the host's current frame.

## Light semantics and current limits

The Cycles object tab groups Mask (Shadow Catcher and Holdout), Shadow
Terminator (Geometry Offset and Shading Offset), and Caustics (Cast Shadow
Caustics and Receive Shadow Caustics) into collapsible panels inside Shading.
Ray Visibility / Raycast controls whether the Raycast shader node can intersect
the object. It is independent of the six camera/scattering/shadow visibility
switches, and is also exposed in current Blender Cycles.
For an object with an existing Cycles tab, run Cycles Tools / Add Object Tab again to
refresh the layout; authored parameter values and expressions are retained.

This increment supports **Point**, **Grid**, **Distant** and **Sun** Light 2.0 types.
The mapping reads Color, Intensity, Exposure, Enable, Area Size, Normalize Light
Intensity to Area, Single Sided, Reverse Emission Direction, Sun Angle, Render
Light Geometry and Shadow Type Off/Raytrace. It uses the public Houdini world
transform and Cycles' existing analytic light implementations.

Numeric RGB power is `Color * Intensity * 2^Exposure` in the integration's Cycles
radiometric convention. It is not a claim of equal brightness to Mantra or
Redshift at identical slider values. Point power is normalized over the sphere;
its inactive area-normalization parameter has no effect. Rectangles use total
power when normalization is enabled and radiance when disabled. A two-sided
rectangle is represented by front/back analytic lights. Normalized total power
is divided equally; unnormalized radiance is retained on each side. Switching
sidedness/type retires the old light resources transactionally.

The installed Light 2.0 canonical transform strips its inactive OBJ scale;
Area Size defines the rectangle's dimensions. This was measured with both local
and inherited scale in `artifacts/stage3/native-scene/scale_probe.json`, rather
than inferred from generic geometry transforms. If a light supplies a sheared
public matrix, an explicit diagnostic requests a geometry-light adapter.

Native numeric lights require physical attenuation without a ramp. Cone
falloff, disk/sphere/line/tube/geometry/environment light types, light textures,
projection/IES maps, linking, contribution overrides, colored/partial shadows,
depth-map shadows and animated light properties over the shutter need further
adapters. Unsupported settings fail visibly and preserve the last complete
image; they are not silently translated to a point light. OBJ point instancing
also remains separate from the supported packed SOP workflow.

OBJ Render Visibility supports the pinned ray sets `primary`, `diffuse`,
`reflect`, `refract`, `volume`, `shadow`, `raycast` and `*`, with `|`, `&`, unary
`-` and parentheses. Blank means no ray visibility. Phantom removes primary
visibility. Unknown expressions fail before engine mutation. Reflection and
refraction map to Cycles glossy/transmission visibility; this is a documented
renderer mapping, not arbitrary Mantra ray-type compatibility. Animated scene
membership, ray visibility or explicit SOP/light paths over a shutter interval
require a temporal adapter and are rejected before mutation. Per-frame animation
with motion blur disabled remains supported.

## Current interactive increment

The native courtyard now uses this OBJ/SOP path with native geometry nodes and a
Houdini key light. The actual artist workflow adds a Grid area light during IPR,
selects its viewport handle, moves it fifteen times, edits SOP geometry and opens
native settings. CPU and RTX 4090 cases pass in the current audited run; HOM
performs the parameter edits and QtTest clicks actual controls. Physical manual
handle dragging is not yet tested. The original reported segmentation fault is
still unreproduced, and measurable UI pauses remain.

The full run `20261011T085026571` passed 107/109 cases. Its two displacement UI
fixture errors were corrected and both repeats passed in
`20261011T091228252-displacement-followup`, with unchanged renderer binaries and
matched source audits. See [interactive updates](native-interactive-updates.md)
for scheduling, geometry uploads and current limits. The 102-case result below
is the historical OBJ-scene baseline, not the current build's full-run result.

## Implementation and evidence

`object_scene.py` owns public selection and light policy. `native_scene.py`
produces geometry requests and is shared with the SOHO adapter. Only HDK cooks
and converts geometry. Stable OBJ/SOP session identities survive renames;
material-compatible resource keys and the native retirement ledger remain in
the C++ adapter. Object visibility does not enter the geometry-conversion key.
Core light descriptors now carry normalization, shadow, sampling and visibility
state, keeping these semantics available to a later Solaris adapter.

The scene observer watches public node/flag/network notifications and selected
SOP dependencies. In explicit-SOP mode it records public BeingDeleted events
for the configured SOP/light sources. Only those observed deletions may produce
an empty source; a mistyped missing path remains an error. Deletion provenance
is retained across camera edits and pruned on source replacement or a mode
change, so it cannot accumulate an unbounded path history. Bundle membership
lacks a node callback, so a bounded one-second membership check wakes an update
without reading geometry. Camera
and parent-transform checks verify zero extra geometry conversion.

Targeted development evidence currently includes:

- `houdini_object_scene_smoke.py`: seventeen groups per CPU/RTX 4090, with
  independently specified emissions, projections, nesting, flags, masks,
  bundles, source changes, stable identity, failure and retirement.
- `houdini_object_lights_smoke.py`: thirteen groups per device. Point irradiance,
  rectangular-light integrals and distant illumination have independent
  Lambertian expectations; exposure, normalization, sidedness, shadows,
  type changes and unsupported configurations are also checked.
- `houdini_object_scene_batch_smoke.py`: six actual SOHO EXR frames with animated
  visibility and positions, native lights and an unchanged current host frame.
- `houdini_object_scene_ipr_smoke.py`: fifteen actual shared IPR/panel events on
  each device, including bundle edits, object creation/deletion and a visible
  failure followed by recovery.

The first expanded full-native run `20261011T055403446` stopped after 59
checks: all 58 headless cases passed, but the existing SOP IPR lifecycle
stalled at its light-deletion stage. The shared scene-source function had
incorrectly treated a deleted explicit light as a missing initial asset. The
run and its immediate source/binary audit remain preserved. Public deletion
provenance now distinguishes that edit from a mistyped path; the corrected
headless source-switch/deletion checks pass on both devices. The corrected
twenty-event native SOP lifecycle also passes on CPU and RTX 4090, including
light/SOP deletion, undo, scene reload, cancellation and zero tracked allocations
after closure.

The fresh full-native run
`artifacts/verification/20261011T061340687/summary.json` passes **102/102 cases**,
finishing at 06:34:00 UTC on 2026-10-11. It contains all 58 headless cases and
22 actual GUI workflows on each of CPU and RTX 4090. The immediate audit matches
240 integration files, all 102 executed test entries, four compiled artifacts,
thirteen patches and six kernels. Its frozen manifest SHA-256 is
`6b2038c3948be1cc37c5b40a9656bf98564bfd984c78117df9b9f725d3922dc6`.
Both native source modes, the complex SOP courtyard, material/FX/output
regressions and the new OBJ-scene interactions are included.

The first GUI attempt rendered successfully but checked the pipe helper PID
before its handshake arrived. The corrected fixture waits for that public
bridge event; the original failure remains under `native-obj-cpu-first`.
The initial shadow fixture received indirect light from its white occluder;
the independent zero-irradiance check now uses an assigned black occluder.
The scale fixture was corrected after the measured Light 2.0 transform audit.

This completes development regression verification for this increment.
Performance budgets, general lighting certification, remaining native production
features and Redshift comparisons retain their own gates. Selection uses
[Houdini recursiveGlob](https://www.sidefx.com/docs/houdini/hom/hou/Node.html)
and [public OBJ visibility](https://www.sidefx.com/docs/houdini/hom/hou/ObjNode.html).
The numeric parameter names follow the [Light 2.0 documentation](https://www.sidefx.com/docs/houdini/nodes/obj/hlight.html);
rendered expectations are independently specified in the tests.
