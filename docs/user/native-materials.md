# Native Cycles material authoring

## Mixing shaders and colors

**Mix Shader** combines two shaders; Factor 0 selects Shader 1 and Factor 1
selects Shader 2. **Add Shader** adds their contributions. New tiles use
`mix_shader` and `add_shader`. Closure remains an internal Cycles type term.

**Mix** is the single modern node for **Float**, **Vector** and **Color** values.
Select **Data Type** in its parameter pane. The three ports **A**, **B** and
**Factor** adapt their types and keep their connections when the type changes.
The pane shows the relevant typed defaults and controls. Connected defaults are
disabled. Factor stays below the two inputs. Vector adds **Factor Mode** with
**Uniform** and **Non-Uniform** factors; the latter mixes XYZ independently.
Color adds blend modes and **Clamp Result**. **Clamp Factor** limits Factor to
0..1; disable it to extrapolate. The Color-menu entry defaults to Color, Factor
1 and gray A/B of 0.5, matching Blender authoring defaults. New tiles use `mix`.
The legacy RGB mixer and separate typed backend mixers are not native nodes.
The owned compiler selects the corresponding current typed Cycles graph node.

Mode-specific defaults survive mode changes and HIP reload. User colors are
preserved while switching modes. Existing modern-Mix/Mix-Shader named HIP wires
remain supported. Names
follow Blender UI names, including Glossy BSDF, RGB to BW, Brightness/Contrast
and Hue/Saturation/Value. The independent reference is
`tests/fixtures/blender-shader-node-names.json`. Source/node identifiers remain
internal implementation details. See `artifacts/modern-mix-evidence-20261019.json`
for scoped CPU/both-GPU, five-frame, world and live-viewer checks.

## Mesh Pointiness

Geometry Pointiness now derives original-polygon geometric curvature without an
authored attribute. Packed sharing, subdivision, displacement, requested-frame
motion semantics and mixed-component recovery are described in
`native-mesh-pointiness.md`. An ordinary attribute named `pointiness` remains
independent of this intrinsic.

## Native material previews

Restart Houdini to load the current development host. In `/mat`, select a Cycles
surface, volume or world material (or shader nodes inside it), then click
**Material Preview** on the **Cycles Tools** shelf. The tool enables native node
previews in visible network editors and requests the selected materials. It opens no extra
render viewer and does not add objects to the HIP file.

Previews update after shader parameter, wiring, bypass and frame changes.
They use the active Houdini OCIO config. The default CPU worker uses two threads
and 32 samples, processes one material at a time, and yields to active IPR work.
Stop IPR to release pending previews immediately; no preview device setting is
added to the renderer ROP. Idle preview rendering retires after ten seconds.

The request/watch queues hold 16 materials each. Native node images retain at
most 32 rasters / 8 MiB, at 32..256 pixels. Rapid requests coalesce; deleting a
material or making another edit prevents stale results from replacing it.
Unsupported surfaces render diagnostic magenta. Missing color, normal and
displacement textures use the same branch-local recovery as the main renderer.
Open the information menu in **Cycles Render** and choose **Material Preview
Warnings** to see affected paths and explanations. The read-only window is
non-modal: keep editing the material while it is open, and repairs clear its
details automatically. Reopening the action reuses that window. New warnings
and repairs also appear in Houdini's status line. Structured diagnostics remain
available through `cycles_houdini.material_previews.service().diagnostics()`.

The last material's file repairs are observed while its worker is live. Use
Material Preview again to refresh an older cached material or after the worker
has retired. World previews light a neutral sphere from the world shader;
volume previews use a small owned density cloud, with density, temperature and
color fields. World materials with a medium instead use a finite emissive
reference sphere: infinitely distant lighting can be extinguished by an
unbounded absorbing medium, so a diffuse environment-only sphere can be black.
The reference makes its finite-path attenuation visible. The fixture has no arbitrary scene fields or named attributes.
Missing context produces a local preview warning/fallback even if the material
is valid on an artist's geometry. A broken world uses the configured neutral
background; a broken volume keeps its density-based diagnostic cloud. Switching
domains clears the previous lighting/transparency state and retires unused
resources while reusing one renderer process. Broad device/memory/physical-screen
certification remains separate work.

Independent surface edits no longer force true-displacement rebuilds, including
materials recovered for missing geometry attributes. Actual height dependencies,
geometry and newly required tangent attributes still cause their required work.
CPU, 4090 and 3080 Ti development checks and their limits are recorded in
`artifacts/material-thumbnails-evidence-20261018.json`. Current world/volume
and warning-window checks are in `artifacts/material-preview-domains-evidence-20261018.json`.

## Diagnostic materials and light sampling

Host `20261017T192527960` keeps diagnostic surface markers out of mesh-light sampling.
They remain visible in darkness through the camera-only branch and retain their
source/Cryptomatte identities. Other rays see their existing diffuse response;
the marker provides no emissive illumination. This avoids building large light
trees merely because an unfamiliar shader was assigned to dense geometry.

Repairing the assignment restores ordinary sampling for a real emissive material.
The policy resets across same-identity changes and pooled shader ownership.
Independent tests check neighbor illumination and normal/depth passes on CPU and
both OptiX GPUs, plus CPU final rendering. Native pig and live assignment repair
checks retain source diagnostics until repair. See
[performance evidence](../dev/performance.md#diagnostic-surfaces-and-mesh-light-cost---october-17-2026).

## Connected input defaults

Connecting a shader input disables its default parameter in Houdini's native
parameter pane, including every component of a color or vector tuple. The wire
supplies the value. Saved defaults and expressions are retained; disconnecting
the wire, deleting its source or undoing the connection restores editability.
Hidden operation-dependent inputs keep their saved wires and disabled defaults.
Non-linkable configuration controls and ramp keys remain editable. HIP reload
restores these states without selecting the node first.

CPU and RTX 4090 OptiX each pass 12 independently specified rendered cases and
native connect/disconnect checks for 265 fields across 69 reflected node types.
Fifteen accepted live viewer states per device cover native parameter keyboard
edits, wiring, Undo/Redo, source deletion, bypass and color tuples. Existing
221-variant layout checks, 15 rendered layout cases and nine layout GUI states
also pass on both devices. Geometry conversions remain unchanged through these
shader edits. These tests use public wiring/Undo APIs; physical wire dragging
and physical display certification remain separate.

Restart Houdini to load host `20261017T140730162`. See the
exact sources and results (`artifacts/verification/shader-input-states-20261017/index.json`).

## Adaptive controls and shader sections

Native parameter panes now retain Blender's authored sections: Principled BSDF
has Diffuse, Subsurface, Specular, Transmission, Coat, Sheen, Emission and Thin
Film; Glass and Metallic have Thin Film; Volume Coefficients has Absorption,
Scatter and Emission. These are native Houdini collapsible folders. Common
Principled controls stay above the folders, and labels inside them omit repeated
section names. Engine socket/parameter names are unchanged.

Operation-dependent inputs and their parameter rows adapt on Math, Vector Math,
scalar/vector Map Range, Noise, Voronoi, White Noise, Gabor, Vector Rotate,
Mapping, Hair Principled, Metallic, volume phase functions, image box projection,
Wave, Tangent and normal/vector-displacement coordinate controls. For example,
Vector Math Multiply shows two vectors, Scale shows one vector plus Scale, and
Round shows one vector. Inactive inputs are hidden without deleting or
renumbering their saved connections or values. Output socket identities remain
stable. Native controls and existing bypass/preview behavior are retained.

The same authored availability rules govern graph capture. Dormant inputs and
parameters are excluded from translation: a missing image or broken subnet on an
unused operation input does not invalidate the material. Making that input active
restores its dependency and normal diagnostic/repair behavior. Operation
expressions evaluate at the requested render time, independently of the visible
Houdini frame. HIP reload refreshes socket visibility without opening a parameter
pane first.

The reference audit uses Blender 5.2.0 LTS `fbe6228777e7` factory socket
availability and its authored shader declarations. The
[Principled declaration](https://projects.blender.org/blender/blender/src/commit/fbe6228777e7/source/blender/nodes/shader/nodes/node_shader_bsdf_principled.cc)
defines the grouping, while Houdini's public
[VOP interfaces](https://www.sidefx.com/docs/hdk/class_v_o_p___node.html)
provide native connector visibility and folders. This does not change the pinned
standalone Cycles engine revision.

Exact sources and verification (`artifacts/verification/shader-layout-20261017/index.json`)
record 221 independent availability cases across 17 native node types, 15 rendered
edit/recovery cases and nine native GUI edit/Undo states per device on CPU and
RTX 4090 OptiX. Dormant wiring/values survive HIP reload, geometry conversion
counts stay unchanged across these shader edits, and one worker/viewer remains
active. Related subnet, bypass/preview, combined media, sparse-volume recovery
and COP regressions passed. GUI keyboard tests use a published numeric parameter
driving the operation expression; direct dropdown mouse selection and physical
screen/latency certification are separate. Broader device certification remains
open. The original availability evidence used host `20261017T122259683`;
current connected-input behavior and its runtime are described above.

Native Cycles surface materials now render through the OBJ/SOP ROP, native
IPR, dedicated panel and SOHO batch path. The development context exposes
versioned surface engine node types, with typed connections, animated parameters,
parameter promotion and saved network identities. CPU and RTX 4090 OptiX
checks establish selected shading and lifecycle behavior. Native typed
subnetworks now flatten into the same owned graph. Materials can be assigned
to objects, polygon faces, sphere points and complete curves. Detail attributes
and nested packed primitives supply fallbacks for those assignments.
Complete material authoring and production certification retain the release
gates below.

Material Output now supports Surface and Volume together, or Volume-only bounds
on closed polygon meshes. The tested slice includes absorption, transparent/glass
boundaries, true displacement and packed sharing. See [closed mesh media](../dev/notes/native-mesh-media.md)
for authoring, coordinate rules, live updates and the remaining certification limits.

A volume shader on a polygon mesh works exactly as in Blender: an attribute the shader reads
(Principled Volume's Density Attribute "density", an Attribute or Volume Info node) does not exist
in a mesh medium, so the node's own parameter is used, and when such an attribute is plugged in,
the parameter only multiplies it. Mesh point, primitive and vertex attributes are not volume
fields, so a `density` attribute on the mesh is not read (an info note says so); use a volume
(VDB or Houdini volume) for attribute-driven density.

The native volume context adds Absorption Volume, Scatter Volume, Principled
Volume and Volume Info, sharing utilities with the surface and world contexts.
Sparse VDB graphs use the same subnets, promotion, ramps and rendering
interfaces. See [native volume material authoring](../dev/notes/native-volume-materials.md)
for field semantics, assignment rules, independent images and lifecycle checks.

Image Texture, UV Map, Mapping, Bump and Normal Map now provide file images,
authored coordinates and derivative/normal interpretation. File revisions
update retained shaders and textured displacement; native IPR and the panel
watch disk-only changes and missing-file recovery. The fixed-configuration
input-color route and upstream cache have CPU/RTX 4090 evidence. See
[native textures](native-textures.md) for precise semantics and remaining gates.

The surface context adds Displacement and Vector Displacement, an optional
vector output on Surface Output, and a Bump/True/Both method on the builder.
See [native displacement](native-displacement.md) for source ownership,
instance variants, seams, UV tangent axes and current motion limits.

Glass BSDF, Transparent BSDF and Holdout now have authored surface adapters;
[film and coverage semantics](native-transparency.md) have separate CPU/OptiX,
IPR and batch evidence. [Noise Texture](../dev/notes/native-noise.md) adds procedural
coordinates, color and true displacement. The current expansion is described in [authored material nodes](../dev/notes/native-material-extensions.md)
and [native world materials](native-world.md).

## Material settings

**Cycles Material** has, below a separator after Pass Index, the settings Blender
shows under Material > Settings, with Blender's names and defaults; **Cycles
Volume Material** has the volume settings only. They are not in a folder: Houdini
duplicates a VOP node's built-in folders when its parameter interface is edited
(for example by promoting a parameter), and such a scene then warns on load. The
defaults are what every material used before these
settings existed, so older scenes render unchanged. Diagnostic marker surfaces
always keep Emission Sampling None.

| Setting | Values (default first) | When to change it |
|---|---|---|
| Emission Sampling | Auto, None, Front, Back, Front and Back | None for many small or faint emitters (heavily instanced emissive shapes): they still glow and light the scene through bounces but are no longer sampled lights, which saves light-tree time and memory and can render faster; Front or Back for a one-sided emitter so light sampling does not waste samples on the dark side. |
| Transparent Shadows | On | Off to make a material with a Transparent BSDF cast an opaque shadow and render a little faster. |
| Bump Map Correction | On | Off only to match an older look; it removes the dark faceting that bump maps cause at the shadow terminator of low-poly meshes. |
| Volume Sampling | Multiple Importance, Distance, Equiangular | Equiangular for thin volumes around a light inside or near them; Distance for dense volumes lit from outside. |
| Volume Interpolation | Linear, Cubic | Cubic for smoother low-resolution grids (VDB and Houdini volumes); slower. |
| Volume Step Rate | 1 | Lower for more accurate, slower ray marching. Only used when **Biased Volume Ray Marching** is on in the ROP. |

Blender 5.3 has no **Homogeneous** volume setting any more, so it is not offered.
Blender materials imported in Link or Append mode carry these settings from the
`.blend` file (see [Blender import](blender-import.md)).

## Author a surface material

Create **Cycles Material** in `/mat`. A new builder contains a connected
Principled BSDF and Surface Output. Its child menu contains the supported
Cycles nodes. Connect a shader, color/vector or scalar to Surface Output.
Numeric inputs use Cycles' implicit unit-strength emission conversion; scalar
values become grayscale. Volume retains its shader-only connection contract.
Assign the builder with the OBJ Material parameter or a Material SOP's
primitive groups. **Use object and geometry materials** is enabled by
default on the Cycles Native IPR ROP. Unassigned geometry falls back to the
ROP's diffuse, principled, emission or volume controls.

To override the entire selected source, set **Material override (optional)**
on the ROP to a builder. Relative paths are supported there. The override
also applies inside packed geometry. Disabling **Use object and geometry
materials** uses the ROP's uniform controls throughout the source. Surface
graphs cannot currently replace a volume material.

Unconnected normal and tangent ports retain Cycles' automatic defaults.
They do not acquire a host constant parameter that would replace those
defaults. Scalar and RGB constants preserve HDR values. The Attribute node
has the authored Houdini default `Cd` and offers `point_random` and
`curve_random`. Named numeric fields, additional UV/color maps and `rest`
coordinates now use the [native attribute adapter](native-attributes.md), with
local recovery for missing or incompatible inputs. Deformation mode samples
compatible named fields, point UV/Cd, hair key colors and hair-root UV across the
shutter, including named mesh displacement dependencies.
Mesh **Tangent**, **Normal Map** and tangent **Vector Displacement** accept named
UV sets, including compatible shutter samples. Their native controls disable
irrelevant UV/basis values without discarding saved settings. Shared
surface/displacement nodes use the correct basis for each output. See
[tangent authoring and recovery](native-attributes.md#mesh-tangent-bases-and-normal-maps).
The reflected engine default for Attribute remains an empty name.

The Python helpers are available in Houdini's Python shell:

```python
import cycles_houdini as ch

material = ch.create_material(name="pyro_embers", shader="emission")
emission = material.node("surface_shader")
emission.parmTuple("color").set((0.2, 0.4, 0.8))
emission.parm("strength").set(2.0)
ch.promote_parameter(material, emission, "strength",
                     name="intensity", label="Emission Intensity")
```

Promotion copies the existing values and animation to the builder and
creates channel references from the original parameters. Connected inputs
must be disconnected before their constant parameter can be promoted.
The material evaluates parameters at the requested render time without
moving Houdini's current frame. Network dots preserve their source output
indices during extraction.

## Assignment precedence and sharing

For polygon geometry, a nonempty primitive `shop_materialpath` takes
precedence over a detail binding, the OBJ Material parameter and the ROP
fallback, in that order. The detail binding is an explicit convention of
this integration. For packed geometry, embedded bindings take precedence
over the packed primitive's fallback. Fallbacks propagate through nested
packing. This follows the [documented packed material precedence](https://www.sidefx.com/docs/houdini/nodes/sop/pack.html).

Geometry variants include the bindings actually used by the prototype.
A partially assigned prototype creates separate variants when its used
fallbacks differ. A fully assigned prototype can share geometry despite
different outer fallbacks; unused missing fallbacks do not cause failure.
Shader parameter edits preserve the geometry snapshot and binding scan.

Sampled packed prototypes share root SOP shutter acquisition across these binding
variants. Ordinary material differences do not require recooking animation once
per instance. Matching complete bindings reuse one retained sampled geometry
resource. Different surface palettes retain separate logical binding records,
but compatible records now share one physical Cycles mesh/BVH through per-object
shader palettes. Assignment identity, uniform fields and missing-field recovery
retain their original context. True displacement, volume closures, incompatible
geometry fields and transform-dependent adaptive subdivision still require the
rendered variants they need. See [physical mesh sharing](../dev/notes/native-mesh-sharing.md)
for the current ownership, UV-output and device verification. Earlier CPU/RTX
4090 tests cover separate surface
and displacement bindings, compatible reassignment without new shutter cooks,
and combined packed deformation/subdivision/temporal displacement. See
packed motion evidence (`artifacts/verification/packed-deformation-20261017/index.json`).

Geometry string attributes must contain absolute paths, with an optional
`op:` prefix. Relative OBJ paths are resolved through the object's context.
Referenced nodes must be version 1 Cycles Material or Cycles Volume Material builders. Houdini's
Material SOP can skip nonexistent paths and report its own warning; an
actual stale string stored on the geometry produces a renderer diagnostic.
The [Material SOP documentation](https://www.sidefx.com/docs/houdini/nodes/sop/material.html)
describes the host assignment attributes.

Owned references combine the builder's saved GUID with its host session ID.
The GUID distinguishes deleted/recreated nodes because Houdini can reuse a
session ID immediately. The session ID distinguishes copied builders that
retain the same saved GUID. Ordinary renames preserve material identity.
Cached references follow a renamed builder only after verifying its saved GUID
and session ID. Frozen binding identities keep a material rename alone from
forcing fresh point/curve deformation samples; replacement and recovery
diagnostics still invalidate affected bindings.

Native sphere points accept point `shop_materialpath`; the Material SOP's
**Point Attributes** mode and point groups can author it. For particle
primitives, a point binding wins over the containing primitive's binding,
followed by detail, OBJ and ROP fallbacks. This extends that host attribute
to the integration's native spheres; SideFX documents point assignment
primarily for instancing. Loose points outside particle vertex lists do not
render and their bindings are ignored. A shared point renders once. When
overlapping particle primitives supply different effective bindings, the point
keeps the first containing primitive's binding and the renderer reports the
affected point numbers. Geometry continues rendering. An explicit point binding
chooses the intended material and clears the conflict after repair. Ignored
owners do not leave unused shader slots or diagnostics in the rendered palette.

Hair accepts primitive `shop_materialpath` for each complete curve. One curve
uses one shader over all its keys. Unsupported point/vertex bindings on hair or
vertex bindings on particle primitives are reported and ignored while retaining
supported underlying bindings. Material SOP Local Overrides support promoted scalar,
color/vector, menu and string parameters, whole ramp count/stop controls and
complete shader curve channels; see [native overrides](native-material-overrides.md)
for precedence, recovery and remaining limits. Object/detail/primitive and packed
material stylesheet rules, ROP scene selection and watched imports are supported;
see [native stylesheets](native-material-stylesheets.md) for categories and limits.
Unsupported layers are diagnosed and omitted locally. VDB fields support one effective native volume
material per collection; see the separate volume guide. An explicit uniform ROP override intentionally
bypasses native bindings and override/style attributes.

Point and curve palettes remain within one engine geometry resource. Packed
point/hair prototypes follow the same used-fallback variant policy as
polygons. Simulation `age`, `life`, `dead` and `stopped` attributes do not filter
the particle population. Deformation correspondence compares material keys after
available point/curve correspondence, so reordered palettes do not change meaning.
Changing bindings over the shutter retains requested-frame geometry and reports
omitted incompatible deformation blur. Velocity mode uses the nominal palette.

The low-level API makes the binding policy explicit:

```python
from cycles_houdini import native_scene

request = ch.geometry_request(obj, sop, material="fallback",
                              material_mode="native")
session.apply({
    "geometry": [request],
    "materials": [{"key": "fallback", "kind": "emission",
                   "color": (0.4, 0.1, 0.5)}],
    "material_resolver": native_scene.resolve_materials,
})
```

The default low-level policy remains `reject` for legacy callers. `native`
enables supported bindings and `uniform` explicitly overrides them. These
policies use the same in-process session and owned geometry path.

## Preview, bypass and node colors

Inside a Cycles Surface or World Material, **Ctrl+Shift+left-click** a node to
connect it to the material's Surface/Background output. Repeat to cycle its
compatible outputs, or click a specific output connector with the same modifiers.
Vector Math chooses its active scalar or vector output. This is an ordinary
connection edit: click the original shader to restore shading, or use **Ctrl+Z**.
Each preview is one Undo operation. Volume and Displacement connections remain
available while Surface is previewed.

The shortcut also works inside nested Cycles subnetworks. It creates one typed
**Preview** output per enclosing subnet, reuses those ports for later previews,
and removes newly created ports when their creation is undone. It uses Houdini's
documented network event hook and delegates unrelated events to existing hooks.

Bypass now forwards the node's primary data input: Math uses Value 1, Map Range
uses Value, and mixes use their first data/shader input. Unconnected inputs keep
their authored defaults; bypassed Bump keeps the geometric normal. Unused inputs,
textures and parameters of a bypassed operation are not evaluated. Nodes without
a pass-through use their output type's zero value. Subnet outputs follow compatible
inputs where available. The material builder and final output require explicit
rewiring when disabled.

The bug icon is Houdini's **Debug** flag, with native per-output execution,
pass-through and constant controls. Its default Execute mode evaluates the node.
It is separate from the preview gesture. See [Houdini's VOP flags](https://www.sidefx.com/docs/houdini/network/flags#vop-flags).

Factory node colors follow Blender 5.2's categories: Shader green, Texture orange,
Input red, Output dark red, Color yellow, Converter blue, Vector purple and Group
olive. Native ports use the interface color. User-assigned colors remain editable.
All 99 native operator styles are checked against the recorded factory palette
and upstream node classifications; this does not imply all Blender nodes exist.

CPU and RTX 4090 OptiX checks cover analytic linear pixels, live bypass/texture
repair, actual native Ctrl+Shift clicks, output cycling, nested type changes and
Undo/Redo with one **Cycles Render** panel. Geometry conversion counts stay fixed during
these shader-only edits. Exact sources and run reports are in
the shader editing index (`artifacts/verification/shader-editing-20261017/index.json`).
Physical-screen capture and production stability remain separate certification.

## Subnetworks and their interfaces

Create **Cycles Subnetwork** inside a material or another Cycles subnetwork.
Create **Cycles Subnet Connector** inside it to declare an input, output or
combined input/output port. Supported types are float, RGB/vector triples
and BSDF. Connectors and shader ports use Houdini's native BSDF type within
the Cycles context. A root Surface Output belongs directly in the material.
Groups and connectors use Houdini's built-in operators, initialized through
the Cycles creation tools. Their dynamic interfaces remain available when the
renderer is absent. Creation assigns stable graph identities and factory colors;
loading a graph does not reset artist colors.

An unconnected subnet input evaluates its connector default at render time.
A matching promoted parameter on the subnet supplies that default instead;
an external connection overrides either default. A BSDF input has a null
closure default. The adapter preserves that null link rather than creating
a replacement shader. A subnet output needs an internal connection, and the
material's final surface accepts a closure or the numeric preview conversion above.

`create_subnet` and `create_connector` expose the same native operators:

```python
group = ch.create_subnet(material, "shading")
tint = ch.create_connector(group, "tint", type="color", default=(0.2, 0.4, 0.8))
surface = ch.create_connector(group, "surface", kind="output", type="bsdf")
```

Promote a connector's `floatdef` or `colordef` parameter to control its
default from the subnet. The promotion helper also accepts a subnet's
parameter, so an existing subnet control can be promoted again to the
material while preserving its channel-reference chain and animation.
Port names, types and roles must stay static; port values may animate.

Use reported port names and indices when wiring through HOM. A combined
input/output connector named `gain` publishes the parent input `gain` and
output `_gain` in this Houdini SDK. Ordinary output connectors publish
their declared names. Multiple outputs and nonzero source indices retain
their meaning through nested boundaries and network dots.

Extraction accepts at most 1,024 host nodes and 32 nested subnet levels,
then enforces the compiler's 256-node and 2,048-link limits on the lowered
graph. Defaults become owned Value/Color nodes; subnetworks and connectors
never become engine nodes. Unsupported port types, duplicate declarations,
type mismatches, missing output connections and recursive
interfaces fail explicitly. Stock VEX subnets and indirect-input nodes do
not have a Cycles adapter. The native context filters them out.
Validation follows reachable ports; disconnected declarations do not invalidate
an otherwise usable material. A duplicate declaration used by the shader still
reports an error.

## Supported node catalogue

| Category | Authorable nodes |
| --- | --- |
| Shader | Principled BSDF, Diffuse BSDF, Emission, Mix Shader, Add Shader |
| Input | Color, Value, Attribute, Fresnel, Layer Weight, Geometry, Object Info, Light Path, Normal |
| Converter | Math, Vector Math, Combine XYZ, Separate XYZ, RGB to BW |
| Color | Mix (Float, Vector, Color) |

`cycles_houdini.shader_catalogue()` reflects registered engine definitions
and sockets from the pinned engine, including non-authorable private helpers.
It records actual constructed defaults, types, implicit links and
enum tokens. Its `surface_graph_adapter` field identifies the definitions
accepted by the owned surface graph compiler. The expanded catalogue is
described in [authored material nodes](../dev/notes/native-material-extensions.md). Reflection alone does not
certify every socket's Houdini data semantics or device behavior.

Saved operators use names such as `cycles::principled_bsdf::1`; graph
definitions use `cycles/principled_bsdf/1`. Enum parameters store named
tokens rather than engine numeric values. Newly created nodes receive a
saved identity. Ordinary renames preserve their graph identity; copied
duplicate identities are disambiguated within their material without
modifying the host scene during extraction. Upstream socket changes will
can change the current definition schema. Earlier plugin versions are not
migrated; generated fixtures are rebuilt from the current source.

## Float and color ramps

Create **Cycles Color Ramp** inside a material or subnetwork. Connect its
Factor, or use the constant Factor parameter. Cycles Float Ramp was retired on
2026-10-29: **Cycles Float Curve** (inline native ramp, Range 0..1, Extension
Horizontal, Factor 1) replaces it; see [shader curves](../dev/notes/native-shader-curves.md).
Color ramps offer RGB, HSV, HSL and LAB through **Color Mode**. HSV/HSL expose
Blender's four **Hue Interpolation** choices and blend stops linearly.
Color ramps output separate Color and Alpha.
They start with a two-key linear ramp from zero to one; Color Ramp starts
black to white with an opaque Alpha ramp. Color and Alpha are inline native
ramps with independent stops.

Float ramps and RGB/LAB color ramps support Constant, Linear, Catmull-Rom, Monotone Cubic,
Bezier, B-Spline and Hermite interpolation with
1..32 keys. Positions must be strictly increasing in `[0,1]` at float32
precision. The curve holds its first and last values outside the authored
key range. A key's basis governs the interval to its right. Constant
intervals change to the next value at the next key. These conventions
follow [Houdini's ramp semantics](https://www.sidefx.com/docs/houdini/hom/hou/Ramp.html).

The native interpolation menu retains all Houdini bases for these modes.
HSV/HSL ignore and disable the Color basis menu and permit coincident stops.
The explicit Color Mode controls renderer evaluation; the inline Color gadget
previews RGB blending. Alpha follows its own per-stop bases (Blender RGB modes
apply to it too). See [inline Color and Alpha ramps](../dev/notes/native-color-ramp-rgba.md).

Promote the `ramp` parameter with `promote_parameter` to expose the complete
Color Ramp (Color and Alpha ramps and global menus) on the material or subnet.
Float Curve channel ramps promote as native scalar `hou.Ramp`s. References
preserve animation, key additions/deletions, renaming and HIP storage. Individual
Factor, Color stop or Alpha stop parameters can also be promoted; script Alpha
through `hou.Ramp` on `alpha_ramp`. Material SOP's
native count/stop overrides resolve owned controls without changing the authored
builder; see [native overrides](native-material-overrides.md).
Replacing animated knot values with constants requires removing their
existing animation, as with other native Houdini parameters.

Color Ramp (including hue and alpha), Float Curve, RGB Curves and Vector Curves
use the same lookup-table resolution as Blender's Cycles. The adapter samples
the controls at 257 positions across the range, including both endpoints.
Stock Cycles nodes evaluate those values during shading. A narrow peak can
disappear between samples or widen around a sample it hits. A peak centered
at 0.5 on a 0-1 ramp lands exactly on a sample. For animated displacement,
each shutter sample uses its own table. Imported Blender curves use the table
captured from Blender while their inline controls match the import. Editing
a curve rebuilds it from the Houdini controls. An imported curve with more
than 32 points per channel shows a reduced set of editable points.

Grouped native shader nodes place their main **Surface** inputs before the
specialized panels. Principled, Glass and Metallic therefore avoid Houdini's
automatic trailing **Other** group. Invisible labels provide grouping metadata
for implicit normal/tangent ports without introducing editable constants.
Socket IDs and saved wiring remain stable. Verification covers 221 input-availability
variants, independent rendered edits, connected-input checks and live viewers.

## Ownership and update contract

The native adapter reads the VOP network on Houdini's host thread and
constructs owned values, nodes and links. Cycles' private graph types stay
inside the shared core. Render threads never traverse changing Houdini
material nodes. Public host notifications track parameter, connection,
creation, deletion and network changes for both viewers.

The compiler accepts schema version 1, at most 256 nodes and 2048 links.
It validates definitions, socket names, finite values, enum tokens, link
types, duplicate inputs, cycles and output domains before mutating the live
scene. Material Output can connect Surface, Volume and vector Displacement;
Surface may be disconnected for a closed mesh medium. Sparse grids use a
Volume-only builder/output. Volume-only displacement remains unsupported.
Bypassed operations lower to their used input or constant before engine validation.
The strict backend API rejects invalid candidate graphs while preserving the
accepted scene. Native source translation uses local diagnostic shading for
recoverable material errors, keeping usable geometry visible; a repaired source
restores the intended shader automatically.

Exactly unchanged graph payloads preserve the compiled material. Material
edits retain geometry unless the new graph changes its required attributes.
A new `Cd` demand uploads the retained attribute data. Normal geometry
variant and resource lifetime rules still apply.

Material metadata is cached by Houdini attribute data IDs. An ID change
scans active string handles, including defaults, and copies each distinct
interned value once. Camera and shader parameter edits reuse that metadata.
The current bound is 4,096 distinct strings per material attribute on a
detail. Converted primitives use a compact shader palette; render threads
never retain attribute handles or host node pointers.

After geometry capture, a synchronous host-thread resolver compiles exactly
the referenced graphs. Reentry and close during the transaction fail
explicitly. Observers follow referenced builders and the parent networks of
missing builders so creation can recover an interactive failure. An
authoritative geometry update retires unused integration-managed native
materials. Explicit low-level materials retain caller-controlled lifetimes.

The pinned [Cycles shader deletion path](https://github.com/blender/cycles/blob/a456b761034dda42c32eef9f4aae0fa5a5c9f604/src/scene/scene.cpp)
retains engine Shader nodes until the scene closes. The integration clears
retired graphs and reuses those slots for new native materials. This stops
continuing slot growth during repeated deletion/recreation; the historical
peak slot count remains until session close. Status reports engine shader
count, pooled retired count and reuse count. This does not establish that
every physical allocation immediately shrinks after a large scene contracts.

Shader parameters evaluate at nominal render time. Deformation samples
require matching face, point and curve material identities. Velocity blur freezes bindings
and shading attributes at nominal time. Shader animation is not integrated
across the shutter in this slice.

`shader_graph_preparation_seconds` measures validation and engine graph
construction before cancellation and core scene mutation. It is included
in host `apply` latency and lies outside the existing completed-generation
core elapsed timer. Compare total host latency when assessing material
edit performance; the nested phase counters cannot be summed as disjoint
timings.

## Verification

The catalogue test checks independent pinned defaults and exact agreement
with the matched harness catalogue. The graph test checks independent HDR
emission and Lambertian irradiance, closure mixing and addition, new
attribute uploads, invalid transactions, repeated edits and retirement on
CPU and RTX 4090 OptiX. Its independently authored harness reference uses
the engine's uniform emission path rather than replaying the graph adapter.

The native VOP test adds 12 host check groups and five rendering/update
groups per device. Saved HIP networks preserve sockets, definitions,
identities, dots, promoted animation and three independently evaluated
SOHO batch frames. Actual GUI tests add 15 lifecycle events per device:
native radiance, shared ownership, edits, promotion, undo/redo, animation,
rename, creation/rewiring, deletion and missing-material diagnostics,
recovery, viewer closure and continued panel rendering.

The subnet test adds six host groups and ten rendering/update groups per
device. Independent radiance checks cover nested defaults, external
overrides, nonzero outputs, combined input/output computations, null
closures and animated promotion. Repeated edits retain geometry and stable
tracked allocations; shutdown retires tracked buffers. Saved networks
retain the exact flattened graph. Separate actual GUI runs add 14 events
per device covering both viewers, nested edits, animation, undo/redo,
interface errors, recovery and normal shutdown. These counters certify
the fixtures, not exact physical VRAM or every possible material network.

Ramp checks add six host groups and 27 rendering/update groups per device.
Piecewise references independently specify nonuniform keys, held ends,
constant discontinuities and scalar/RGB values. Spatial checks exercise
varying factors through shader execution against an independent camera
projection, rather than relying only on constant folding. Whole-ramp
promotion, animated knots, subnets, strict bounds, graph expansion,
recovery, tracked retirement, HIP reload and three SOHO batch frames pass.
Actual ramp GUI runs add 15 events per device: shared ownership, palette
promotion, key/interpolation edits, undo/redo, held segments, animated
knots, invalid key/basis diagnostics, recovery, rename, viewer closure and
continued panel rendering. Both applications exit normally.

Viewer sharing checks compare session, generation and resource ownership
while rendering is active, then fence rendering before comparing exact
allocation totals. A zero-viewer control reproduced growing OptiX work
buffers during progressive batches. Instantaneous equality across a live
panel-opening operation was therefore an invalid assertion. The failed
aggregate run and corrected checks remain documented in implementation
status; completed-generation retirement tests keep their exact plateau
and close assertions.

Native assignment checks add 12 rendering/update groups per device, host
observer checks, HIP reload and three independently evaluated batch frames.
References cover polygon subsets, default string handles, OBJ/detail/packed
precedence, partial and complete prototype assignments, unused fallbacks,
material variants, rename/delete/recreate, failed transactions, graph edits
and retirement. Actual GUI runs add 18 events per device covering both
viewers, automatic assignment and shader updates, undo/redo, missing/invalid
graph recovery and shared ownership. The saved example is
`artifacts/stage3/materials/assignments/native-material-assignments.hiplc`.

Cross-kind shader retirement checks exercise 32 lifetimes per device across
Cd-driven meshes, absorption volumes, point spheres and linear hair.
Independent radiance and Beer-Lambert references pass through 31 slot
reuses. Warm tracked allocations remain stable within each phase; closing
retires the pool and tracked engine buffers. These checks establish slot
reuse correctness for the supported graphs; physical peak reclamation and
larger shader inventories retain their own measurement gates.

The strand assignment test adds 14 check groups per device. Independent
colors cover point and curve palettes, Material SOP point groups, overlapping
particle owners, ignored loose points, nominal dead filtering, `Cd` demand,
packed variants and complete prototype sharing. Shader/camera/transform
edits retain geometry and metadata scans. Reordered deformation samples,
cached fallback renames, animated-binding rejection, velocity snapshots,
malformed/unsupported attributes, uniform override, recovery, warm retirement
phases and HIP reload pass. The saved example is
`artifacts/stage3/materials/strands/native-strand-materials.hiplc`.

Independently constructed matched-harness scenes use uniform engine emission
materials and a different palette ordering. All four native/reference RGBA
images match exactly on the tested CPU and RTX 4090 configurations. The
harness also passes 26 checks per device for radiance, palette/slot bounds,
missing keys, surface/volume typing, bound-material retirement, first-slot
defaults, legacy uniform compatibility and nonmutating failure. Actual GUI
runs add 16 events per device for point groups, graph edits, undo/redo,
sphere-to-hair replacement, curve palettes, missing-builder creation/deletion
and shared-view continuation. This evidence remains limited to the fixtures.

Reproduce these tests with the development package:

```powershell
./scripts/run-hython.ps1 -HythonArguments 'tests/houdini_shader_catalogue_smoke.py'
./scripts/run-hython.ps1 -HythonArguments 'tests/houdini_shader_graph_smoke.py'
./scripts/run-hython.ps1 -HythonArguments 'tests/houdini_material_vop_smoke.py'
./scripts/run-hython.ps1 -HythonArguments 'tests/houdini_material_subnet_smoke.py'
./scripts/run-hython.ps1 -HythonArguments 'tests/houdini_material_ramp_smoke.py'
./scripts/run-hython.ps1 -HythonArguments 'tests/houdini_material_assignment_smoke.py'
./scripts/run-hython.ps1 -HythonArguments 'tests/houdini_shader_retirement_smoke.py'
./scripts/run-hython.ps1 -HythonArguments 'tests/houdini_strand_material_smoke.py'
./scripts/run-hython.ps1 -HythonArguments 'tests/core_strand_material_smoke.py'
./scripts/run-hython.ps1 -HythonArguments 'tests/houdini_volume_material_smoke.py'
./scripts/run-hython.ps1 -HythonArguments 'tests/core_volume_material_smoke.py'
./scripts/run-hython.ps1 -HythonArguments 'tests/houdini_named_volume_regression.py'
./scripts/run-gui-test.ps1 -Test houdini_material_ipr_smoke.py -Device CPU
./scripts/run-gui-test.ps1 -Test houdini_material_subnet_ipr_smoke.py -Device CPU
./scripts/run-gui-test.ps1 -Test houdini_material_ramp_ipr_smoke.py -Device CPU
./scripts/run-gui-test.ps1 -Test houdini_material_assignment_ipr_smoke.py -Device CPU
./scripts/run-gui-test.ps1 -Test houdini_strand_material_ipr_smoke.py -Device CPU
./scripts/run-gui-test.ps1 -Test houdini_volume_material_ipr_smoke.py -Device CPU
```

Evidence and the saved example live under `artifacts/stage3/materials`.
GPU GUI checks use an explicit OptiX device ID. These small fixtures do not
establish arbitrary material performance or complete shader-port parity.

## Remaining material release gates

Float Curve, RGB Curves and Vector Curves are edited as inline native ramps (2026-10-29), with scoped
CPU/both-GPU/live-viewer evidence; see [shader curves](../dev/notes/native-shader-curves.md).
Color Ramp hue modes, Alpha and the renderer-matched RGBA editor now have
independent CPU/both-GPU and live-viewer checks; see
[RGBA ramps](../dev/notes/native-color-ramp-rgba.md). Blender's five RGB interpolation modes
and whole-RGBA promotion also have [verified coverage](../dev/notes/native-blender-rgb-ramps.md).
Object/detail/primitive and packed stylesheet assignments/parameter rules,
scene selection, watched file/registered sources, local CVEX rule recovery and
independent surface/displacement parameter categories
are now [implemented and scoped-verified](native-material-stylesheets.md).
SOP-authored and object-level animated rules now have analytic shutter tests,
including local overrides, assignment changes, packed ownership and live
off-frame import/material repair. Requested-time/shutter CVEX channel execution
and observation of resolved channel operators are now verified for object,
SOP and packed rules. Unresolved dynamic paths and arbitrary scripted resource
dependencies remain open.
Finish texture coordinates and
asset-sequence behavior, remaining attribute/coordinate contexts, per-key hair
assignment semantics, remaining override/style-sheet forms, broader preview contexts, broader
combined-medium certification, remaining subdivision policies, production-scale
sampled-displacement workloads and version migrations.
Geometry outputs need documented coordinate conventions; Object Info and
Light Path outputs need independent host-data tests. Broader texture interpretation,
working-space invalidation, production UDIM workloads and Rec.709/ACEScg render certification
remain part of the complete color pipeline. Native materials still require
Solaris shader discovery and USD authoring as a separate adapter.

The context uses the public [HDK VOP interface](https://www.sidefx.com/docs/hdk/_h_d_k__op_basics__vops.html)
and [custom-context extension points](https://www.sidefx.com/docs/hdk/_v_o_p_2_v_o_p__custom_context_8h-example.html).
Connection handling follows [HOM's documented endpoint and index conventions](https://www.sidefx.com/docs/houdini/hom/hou/NodeConnection.html).
Promotion uses [supported parameter channel references](https://www.sidefx.com/docs/houdini/hom/hou/Parm.html).
