# Native numeric shader attributes

Native OBJ/SOP materials can read named numeric fields through **Attribute**,
**Color Attribute** and **UV Map**. This extends the dedicated `Cd` and `uv`
streams; ordinary Houdini attributes do not need special renderer names.

Geometry's **Random Per Island** output derives values from mesh connectivity
without an authored field. See [mesh island shading](../dev/notes/native-mesh-islands.md) for
subdivision, displacement seams, packed copies and repair behavior.

## Authoring and ownership

Attribute reads scalar, two-component or three-component numeric tuples.
Integer tuples convert to Cycles' float precision and interpolate as numeric
data; this is not lossless storage for large integer IDs. Strings, arrays,
matrices and four-component tuples remain unsupported shader inputs. They are
ignored when unused, and recover locally when requested.

For polygon meshes and curves, vertex values take precedence over point,
primitive and detail values. A broken more-local field does not silently select
a different domain. Mesh fields follow render corners, including reversed
winding and coarse subdivision corners. Hair fields follow curve keys. Point
spheres read point/detail fields and unambiguous particle-primitive fields;
vertex ownership is not an implicit per-point reduction. Conflicting particle
primitive ownership makes that field unavailable without removing the points.

Use Attribute's Vector output for reference positions such as `rest`, or its
Color/Fac outputs for shading. Color Attribute accepts a named numeric field;
an empty layer selects `Cd`. UV Map accepts a named two/three-component field;
an empty name or `uv` retains the default map. The existing default hair UV
stream is root-based; a named hair field is explicitly per-key.

Values are data in the render working space. Naming a field does not trigger a
texture color transform. Color/Vector conversion and scalar interpolation follow
the Cycles attribute node. Numeric fields such as age or ID are shading data;
requesting them does not add particle visibility or lifetime rules.

Only graph-requested names are captured. Disconnected nodes contribute no
requests. AOV writer inputs are conservatively included in capture candidates;
only registered, contributing AOVs affect shader validation and diagnostics.
Houdini data IDs and requested-name changes invalidate the relevant component.
Camera-only updates do not inspect or serialize field values. Retained host
details are read by the existing owned background conversion path. No host
attribute pointer crosses the worker boundary.

Owned streams reserve their element counts and use the existing bounded worker
transport. Per-geometry shader bindings include available names and tuple sizes,
so a material can work on one component while recovering on another. An internal
length-prefixed engine name prevents collisions with Cycles' reserved names and
generated tangent/sign suffixes. For example, user fields `map` and `map.tangent`
remain independent. Saved graph names, diagnostics and transport names stay intact.
Shared packed prototypes retain the same field data. Shader/material identity is
preserved when a binding uses fallback shading.

`Cd` follows these domain rules for compatible three-component float colors.
Packed `Cd` is available through Attribute and Color Attribute, including an empty
Color Attribute layer. Primitive colors on meshes and curves expand to the same
corner/key streams as point and vertex colors; invalid values recover locally.

Outer packed primitives and their points can supply uniform numeric fields when
the prototype has no authored field of that name. Shading values stay on render
objects, preserving shared prototypes; displacement uses required shape variants.
See [packed instance attributes](../dev/notes/native-packed-instance-attributes.md) for domain
precedence, recovery, update ownership and scoped verification.

For sampled native Bezier/NURBS curves, shader fields and width use the host's
ordinary attribute coefficients; rational positions and control-point velocity
use geometric coefficients. Default UV stays root-based and named fields follow
the generated keys. See [native parametric curves](../dev/notes/native-parametric-curves.md)
for sampling, motion, recovery and scoped verification.

## Recovery

Missing, deleted, non-finite or incompatible fields keep geometry rendering.
Affected color inputs use diagnostic shading. Dependent normal, displacement and
AOV branches are disabled; independent branches remain usable. An invalid color
field does not remove independent displacement, and an invalid height field does
not replace independent surface shading. Repairs restore bindings automatically
through the normal IPR update path. Empty/oversized shader field names also
recover with an artist-readable material path.

Warnings survive cache hits, report the affected geometry/material and include
the original field failure where available. Capture warnings for unused fields
are suppressed. Invalid owned layouts, sample ownership and malformed worker
messages remain genuine transaction failures; they do not replace the last
accepted generation.

## Motion and remaining scope

Deformation mode samples compatible numeric fields across the shutter on meshes,
point spheres and hair keys. Default point UV/Cd, hair-root UV and hair key Cd are
sampled as well. Surface reads interpolate at ray time. Mesh true displacement
evaluates its field dependencies at each captured pose, including subdivision.
Identical states collapse instead of retaining repeated field payloads.
Transform Only and Velocity retain their nominal shading policy. Velocity also
holds packed-instance and point-instance fields at their requested-frame values,
so it never cooks a SOP at another shutter time.

An unavailable shutter sample retains the centre field for usable shading and
preserves independent geometry motion. If the field drives true displacement,
that component uses requested-frame displaced geometry without incompatible
deformation blur. Independent surface/displacement inputs and camera/object motion
remain valid. Missing nominal data uses the existing local diagnostic policy.
Named-field diagnostics carry a real source path and an explicit `attribute` name. Unused
fields remain quiet; repairs clear warnings without a manual IPR refresh.

Deformation invalidation includes host node edit versions, references, subnet
contents and ordered wired inputs, so edits to a branch inactive at the nominal
frame still refresh its shutter data. Camera-only updates retain field buffers.
The dependency walk is host metadata work; its node-level invalidation is
conservative and broad graph/large-field latency remains to be measured.

Additional reference-object coordinate sources, radial Tangent displacement
motion and per-ray-time sampling of uniform instance fields remain separate work.
Owned COP images and packed-prototype deformation have their documented native
adapters. Four-component color/alpha tuples are not claimed. Heavy-field capture, large seam populations,
physical memory and other devices still need broader certification.

## Mesh tangent bases and normal maps

**Tangent** in UV Map mode, **Normal Map** in tangent space, and tangent-space
**Vector Displacement** accept **UV Map** names. An empty name or `uv` uses the
default UV stream. A named map accepts two/three numeric components; XY supplies
the basis, including a mirrored map's handedness. It does not require a duplicate
default UV attribute. Vertex/point/primitive/detail ownership follows the rules
above, with effective values expanded to mesh corners.

Deformation mode computes requested bases from corresponding position, normal
and UV shutter samples. Linear and Catmull-Clark subdivision retain those inputs.
Normal Map's **Base** selects Original or Displaced for surface shading. A node
also feeding true displacement uses the original basis for that output, avoiding
a circular dependency. Tangent follows the same output-specific basis rule.
Shared nodes keep their original graph and material identities.

Native UV Map/Axis/Base controls disable when their current mode does not use
them, retaining saved values. An irrelevant, stale map name creates no warning.
Missing, incompatible or non-finite used maps recover locally: dependent color
uses the diagnostic policy; dependent normal/displacement effects are omitted.
An unavailable shutter-only map retains centre shading, and only displacement
depending on that map loses incompatible deformation blur. Repairs restore the
effect automatically. Mesh-only tangent effects on points/hair continue to use
the geometry-specific recovery policy.

Changing to an already captured map generates its newly requested basis from
retained source without recooking or converting host topology. This can still
rebuild/upload engine geometry; it is not a claim of GPU updates being free.
Camera-only edits retain mesh capture and field buffers. Float3 UVs are read
directly as XY, without a second alias UV buffer. Only requested bases are made;
large-mesh tangent generation cost remains to be profiled.

## Executed evidence

The attribute/tangent milestone used host `20261016T220342676`, worker protocol 31, 31 engine
patches. Patch 0029 supplies sampled point/hair reads; patch 0030 adds sampled
and named tangent construction plus output-specific bases. Patch 0031 corrects
the pinned engine's centre-frame normal lookup for motion-blurred Normal Map
shading. All consumers and six CUDA/OptiX kernels were built together; old
versioned resources remain unchanged.

`houdini_named_attribute_smoke.py` passes ten static input routes on CPU and
RTX 4090 OptiX, including recovery, ownership, packed data, subdivision, independent
displacement and five native batch frames. Fifty-eight malformed owned-field,
motion-stream, nominal-snapshot and transport inputs are rejected per device.

`houdini_field_motion_smoke.py` passes eight analytic routes on meshes, points
and hair at three/five shutter samples, local deletion/type/non-finite recovery,
offsets, live repairs and five native frames. The independent target E[q*q]=1/3
distinguishes sampled shading from a frozen centre value of 1/4. Sixty-four warm
repairs retain exact owned bytes and shaders per state; CPU tracked-device
variation is 2,304 bytes within an explicit 8 KiB budget, and OptiX is constant.
These are small renderer-counter checks, not physical-memory certification.

`houdini_field_motion_gui_smoke.py` passes fifteen displayed states per device
with one viewer and one Render click. Actual native parameter-pane edits exercise
mesh scalar fields, point colors, named hair UVs, hair key Cd and hair-root UVs.
`houdini_named_field_displacement_smoke.py` adds twenty independent displacement
image comparisons per device; its GUI companion passes fifteen displayed states
per device. See [native displacement](native-displacement.md).

`houdini_tangent_basis_smoke.py` adds 32 independent primary comparisons and
28 independently constructed bare/frozen recovery images per device, three/five
samples, both subdivision types, 16 warm repair states, three direct-final states
and five native ROP/SOHO frames. References contain no tangent/normal-map nodes,
UV fields or displacement nodes. The GUI companion passes 22 displayed states
per device, with one Render click and actual native parameter-pane edits. Its
composed-window captures verify enabled tangent controls and disabled world-space
controls. Healthy, omitted-effect and repaired viewer images were inspected.

The attribute/tangent evidence index is
`artifacts/verification/tangent-bases-20261017/index.json`: 22 targeted wrappers
and their source/binary audits, not a fresh full suite or performance certification.
The prior motion-field viewer evidence remains in
`artifacts/verification/field-motion-20261016/index.json`.
The earlier static viewer tests remain historical evidence in
`artifacts/verification/named-attributes-20261016/index.json`.
Open `artifacts/examples/field-motion/field-motion.hiplc` for the small motion
fixture, or `artifacts/examples/named-attributes/named-attributes.hiplc` for the
static field fixture, after launching Houdini with the development package.
`artifacts/examples/tangent-bases/tangent-bases.hiplc` contains the small native
Grid/Reverse/Wrangle fixture, with no Python SOP or manually assigned identities.

The newer packed-capture/snapshot host is `20261016T231151646`, with the same
engine patches and kernels. Its targeted tests do not constitute a fresh rerun
of every attribute-specific test above. See [performance measurements](../dev/performance.md) for that separate milestone.

The navigation checkpoint uses host `20261016T235903767` with the same kernels
and engine patches. It retains local/shutter origins for preview rebasing and
does not introduce new attribute meanings. Its targeted checks are not a rerun
of all attribute-specific tests above.

The camera-priority checkpoint uses host `20261017T001630159`. Its 31 navigation
contracts per CPU/OptiX device include camera-only retention and completed-preview
reuse, with the same attribute semantics/engine kernels. This is not a rerun of
every attribute-specific test above.

The continuous-feedback checkpoint uses host `20261017T012041968` with the same patches and
kernels. Its 31 targeted wrappers cover navigation, packed sharing/source recovery,
mixed FX and repeated updates. This does not recertify every attribute case above.

## Retained fields and packed device-table repair, 2026-10-19

Independent shading fields now refresh plain and subdivided meshes while
retaining physical geometry and valid displacement. UV/tangent regeneration
uses retained source positions/normals. Newly requested fields are prepared
even when a material change is the only source edit. A field actually used by
height still invalidates that height; independent color failures do not.
This changes caching, not attribute meaning or authored value precision.

Shader-demand changes can also move unchanged attributes inside global device
arrays. Patch 52 tracks ordered owner/type/element/count/motion spans for each
packed pool and forces copies after layout changes. These copy requirements
do not falsely mark physical geometry dirty. A cancelled partial update leaves
the layout invalid so a later accepted update cannot reuse partial data.

A differential test against the prior build reproduces corrupted normal data
when two meshes lose their UV-dependent shader requirements. The fixed normal
build passes 29 alternating requirement/repair states on CPU and both OptiX
devices, preserving independent position and normal images within 3e-6 without
extra displacement builds. The full tangent suite passes 42 checks per device,
including independent UV omission/repair data planes, native final states and
five batch frames. Both live viewers pass 31 edits with a million-triangle
prototype shared twelve times, one cook and zero tracked allocations on close.

The instrumented worker also passes the 29 states with MSVC AddressSanitizer;
this is scoped address-safety evidence, not physical-memory certification.
Evidence and exact tested source manifests are in
artifacts/mesh-shading-reuse-evidence-20261019.json. See native-subdivision.md
for interpolation budgets, fallback behavior and remaining host/wire work.
