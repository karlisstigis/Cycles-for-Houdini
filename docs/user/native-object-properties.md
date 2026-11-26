# Optional Cycles object and light properties

For acceleration blur, see [Motion blur: Deformation](#motion-blur-deformation).

Standard Houdini objects remain renderer-neutral. Select geometry objects (or
their SOPs) and use **Cycles Tools → Add Object Tab**. Select native lights
and use **Add Native Light Tab**. **Remove Tabs** removes the
integration's spare tab while preserving Houdini's native parameters and values.
Installation is idempotent. Tabs already saved in a HIP remain saved; newly
created objects acquire no Cycles tab automatically.

Installing properties keeps **Transform, Render and the other native tabs**
and adds **Cycles** beside them on geometry objects, or **Cycles Light** on
lights. Existing values, expressions, animation
and native Render/Light menus are retained. Removing the properties restores the
original parameter layout. The current interface adds peer tabs directly;
previous plugin layouts are not migrated. Enable the **Cycles Tools**
shelf from the shelf selector. **Render ROP** adds a **Cycles Render** output node.
**IPR** opens the **Cycles Render** panel in a separate window and starts an
interactive render of the selected output node or the scene's first one. It
creates `/out/cycles_render` if there is none. **Cycles → Render View** docks
the **Cycles Render** panel beside the Scene View without starting a render.

These are native Houdini parameter templates, with normal animation, expressions,
undo and HIP persistence. Render settings and device selection use the Cycles
ROP in Houdini's native parameter editor. The dedicated image panel uses Houdini's Qt styling, native node choosers
and wheel-safe combo boxes; its image canvas remains a custom display widget.

This follows Redshift's documented architecture: optional OBJ spare properties,
common native lights, and dedicated editable light assets. Redshift documents a
shelf action for object properties; Cycles exposes equivalent actions through its
tools shelf. See [Redshift object/camera parameters](https://help.maxon.net/r3d/houdini/en-us/Content/html/Object%2BCamera%2BParameters.html)
and [Redshift Houdini lights](https://help.maxon.net/r3d/houdini/en-us/Content/html/Redshift%2BLights.html).
This is an integration pattern, not rendering-feature parity.

## Geometry object tab

**Subdivision** enables per-object Catmull-Clark or Simple subdivision.
Turning its header checkbox off renders the original polygons; the ROP never enables subdivision globally.
Fixed subdivision has separate IPR and final levels; level zero disables it.
Adaptive subdivision exposes Pixel/Object space and a per-object Pixel Size or Edge Length, multiplied by the ROP Render/Viewport dicing rate.
The fixed levels are hidden while Adaptive Subdivision is enabled and retain their previous values.
**Cache Subdivision Topology** defaults to on and reuses OpenSubdiv topology for adaptive Catmull–Clark in IPR.
Cycles still recalculates the tessellation and rendered detail. Compatible instances sharing tessellation
can use the cache when any of their source objects enables it. The ROP's **Rendering > Geometry > Subdivision > IPR Topology
Cache Limit (MiB)** sets the shared host-memory budget. It defaults to 512 MiB. Zero disables and clears the cache,
and closing IPR releases the retained topology.
The ROP supplies the adaptive maximum level. UV smoothing,
boundary interpolation and use of Houdini crease attributes are explicit.

Fixed levels tessellate the control mesh inside Cycles, leaving SOP geometry and
viewport tessellation unchanged. Camera movement retains fixed tessellation.
Adaptive pixel dicing can retessellate inside the renderer on camera changes
without converting the SOP again. Interactive camera updates reuse the last
complete dicing camera while the mouse is down or camera changes are continuous.
After release and 150 ms without a new camera update, one refinement uses the
latest camera. An optional ROP **Dicing Camera** instead supplies a fixed reference,
so navigating the render camera does not change tessellation. **Offscreen Scale**
uses Cycles' native coarser dicing outside that reference view.
The optional **Limit Subdivision Triangles** guard is in the same ROP Subdivision
panel, disabled by default. Its budget limits generated triangles per mesh,
not total physical memory; exceeding an enabled budget reports an error rather
than silently reducing quality. Integer indexing guards always remain active.
Blender modifier options without an equivalent engine
implementation are not exposed as inactive promises.

The tab groups Meshes, Packed Instances, Curves, Points, Volumes, Shading,
Visibility and Motion. Component render toggles apply independently inside a
mixed geometry object. Curves support linear 3D, cardinal 3D and rounded ribbon
rendering. Shading includes holdout, shadow terminator offsets, Fast GI distance
and shadow caustic caster/receiver flags. Points use native Cycles spheres, with
Houdini pscale as radius. Volumes expose object-space density behavior.
It also contains optional ray visibility, object motion policy and geometry
motion mode, plus point-radius, curve-width and volume-step fallbacks. Native
Render Visibility/Phantom still apply. The overrides do not bypass engine limits
or invent deformation correspondence for topology-changing geometry.

### Motion blur: Deformation

Turn on **Motion Blur** on the ROP (**Camera Effects**). Its **Shutter Offset** places the shutter around the frame, **Samples** sets the number of steps across it (odd, 3 to 129) and **Deformation** chooses how shapes blur. Camera and object transform blur always apply. An object can override it under **Cycles → Motion Blur**: **Motion Blur** follows the ROP or turns blur off for the object, and **Deformation** is **From ROP** or one of the choices below (hidden while the object's Motion Blur is off).

| Deformation | What blurs the shape | Reads (point attributes of this frame) | Cooks at other times |
|---|---|---|---|
| **Off** | nothing: the frame's shape moves with its object; packed primitives keep their animated transforms | – | when the SOP is animated |
| **Velocity** | straight paths | `v` (units per second); `w` on packed and instance points (radians per second, turning about the point) | no |
| **Acceleration** | curved paths: P + v·dt + ½·accel·dt² | `v`, `accel` (units per second²), `w` as for Velocity | no |
| **Geometry Samples** | the geometry itself at each shutter sample | positions, normals, UVs, colours, numeric fields | yes, once per sample |
| **Velocity if Available** | Velocity where `v` is there, Geometry Samples elsewhere | as Velocity | only for objects without `v` |

* **Velocity** follows Karma and Redshift. A static object with `v` renders blurred along `v`. Packed primitives and point instances move by the `v` of their packed or instance point and turn by its `w` (a vector: the axis is its direction, the speed its length) about their pivot, the packed point position. Nested packed levels each use their own points' `v` and `w`. Shading attributes keep their values of the frame. Points and curves use three steps (shutter open, centre, close), as Blender does; meshes and instance transforms use all **Samples**. Geometry without `v` gets no deformation blur. Volumes use their `vel`/`velocity` field.
* **Acceleration** adds `accel` to Velocity, the same for points, curves, meshes, packed primitives and point instances; `w` still turns them at a constant rate. The curved path is drawn with all **Samples** as straight pieces, so a strongly curved path needs more samples. Without `accel` it renders exactly as Velocity; without `v` (or `w`) there is no deformation blur. Volumes use their velocity field only. A point instance without `N` or `orient` is aimed along `v` by Houdini; on a curved path that aim turns, but Velocity and Acceleration keep the frame's orientation (only `w` turns an instance), so give such points `N` or `orient`, or use Geometry Samples.
* **Geometry Samples** (formerly *Deformation*) cooks the SOP at each shutter sample and interpolates corresponding positions, normals, UVs, colours and numeric fields.
* **Velocity if Available** decides per object, from the top level of the object's render geometry at the frame: if its points carry `v`, or it holds packed primitives or is an Instance object whose points carry `v` or `w`, or it has a `vel`/`velocity` volume, the whole object uses Velocity and nothing is cooked for it; otherwise the whole object uses Geometry Samples. Packed contents and instanced objects follow their object's decision.

If an object itself is set to Velocity or Acceleration (not From ROP) and its geometry has no `v`, the render reports one `motion.missing_velocity` warning naming the object and renders it without deformation blur. An object set to Acceleration whose points have `v` but no `accel` gets an informational note. With the ROP's own setting such objects are rendered without a message, so static scenery needs no `v`.

Houdini's standard object parameter **Geometry Velocity Blur** (`geo_velocityblur`) is not used; use **Deformation**. Scenes saved with the former **Geometry Motion** values keep them: Transform Only is now **Off** and Deformation is **Geometry Samples**.

Left as they are: a time-dependent material binding can still cook its SOP at shutter times in Velocity and Acceleration mode.

A spin with `w` is drawn by interpolating the instance's rotation between motion steps along the shorter way round. If it turns half a turn or more between two steps, the instance renders without its packed velocity and reports `motion.packed_velocity`; raise the ROP's **Samples**. When a packed primitive's pivot is not at its prototype origin, the instance origin moves on an arc that is drawn as straight segments between steps; more **Samples** make it closer.

Changing a material, or capturing again without a change, does not re-send geometry in any mode. Shutter cooks give Houdini's packed prototypes new identities; the renderer recognises unchanged prototypes by their contents.

### Hair and curve width

Select the geometry object containing the open curve strands and choose **Cycles Tools → Add Object Tab**. Under **Cycles → Curves**, enable **Render Curves** and choose a **Shape**. **Rounded Ribbons**, the default (as in Blender), renders camera-facing ribbons through cardinal interpolation of the SOP keys; objects without a Cycles tab use it too. **3D Curves** gives the strands a round cross-section with the same interpolation. **Linear 3D Curves** follows the SOP segments exactly.

Use the scalar float attribute `width` for strand diameter in object space. To taper a strand, vary the width along its vertices or points. Cycles checks vertex, point, primitive and detail owners in that order. The object's **Curve Width** supplies the diameter where the selected attribute is missing or invalid.

Two settings refine curves, and they do different things. **Curve Subdivisions** on the ROP (Rendering > Geometry > Points and Curves; Blender's setting of the same name, default 2) splits every curve segment into 2^n pieces while rendering, so Rounded Ribbons follow the curve smoothly (0 = 1 piece, 2 = 4, 4 = 16; higher values act as 4). It does not change 3D Curves or Linear 3D Curves, and it applies to the whole scene; changing it during interactive rendering restarts the render. **Bezier/NURBS Segments** (ROP, or per object with **Override Segments**) applies only to Houdini Bezier and NURBS curve primitives: it sets how many straight pieces each span becomes when the curve is converted for rendering. Polygon curves, such as hair, ignore it.

## Cycles lights

Development staging treats each versioned host as immutable, including generated
light/ROP assets, Python, plugin metadata and GPU kernels. An unchanged restage
reuses the existing files. Changed inputs or damaged/missing runtime files require
a fresh build host; staging never repairs a loaded asset in place. The asset
inventory is sealed before publishing the package pointer. This removes a
plausible development-only hazard, but does not establish the cause of the
reported fatal drag crash.

The focused parented-light workflow can check CPU, individual GPUs or an explicit
GPU group. Device IDs in the accepted session status are authoritative. Saved
multi-GPU checkboxes are the sole saved GPU selection; there is no hidden
single-device ID or migration channel. Scripts choose an exact device set with
`cycles_houdini.device_selection.set_devices(rop, ids)`.

The October 17 focused follow-up records 15 passing regressions and one
intentional GUI-failure control in
the immutable-runtime light index (`artifacts/verification/parented-lights-immutable-20261017/index.json`).
It includes exact CPU, RTX 4090, RTX 3080 Ti and mixed OptiX selections, plus
1080p/4K canvas delivery, live Clean Transform, parenting and handle edits.
Two restages during live handle use preserve all 69 frozen runtime files' bytes
and modification times. Native mouse events, transform agreement and matching
canvas paint metadata are checked. Desktop captures are blank even in a stock
Houdini control without Cycles, while its native viewport export is valid;
physical screen presentation remains unverified. These checks do not establish
input latency or the cause of the reported fatal crash.

The **Cycles Tools** light shortcuts create one `cycles::light::1` OBJ asset
with different presets. The Type menu offers Point, Spot, Area and Sun. Area
has Square, Rectangle, Disk and Ellipse shapes; Sun with Angle zero is Distant.
New lights take Blender's defaults: Square area of Size 1, point/spot Radius 0,
Spot Blend 0.15, and Camera ray visibility off.
The asset uses Houdini transforms and a referenced native Light 2.0 child for
viewport visualization. Cycles consumes only the outer asset, avoiding duplicate
preview lights. It does not copy Houdini's operator definitions.
The asset uses the native Light 2.0 bulb icon and declares that child as its
representative light. The **Environment Light** shortcut creates a native Houdini
environment object with the environment icon and optional Cycles Light settings.
File HDRIs and native world authoring are described in [the world guide](native-world.md).

Select a Cycles light at OBJ level and press **Enter** in the viewport to use
its native handles:

- **Area:** drag the native dimension rectangle's edges or corners. It surrounds
  the native emitter outline, including Disk/Ellipse. Square/Disk use X for both
  dimensions and preserve the unused Y parameter.
- **Point/Spot:** drag the native dimension box to change emitter radius.
- **Spot:** use the native handle menu or **Z** for Cone mode, then drag the
  cone ring. **O** returns to orientation. The adapter converts Houdini's inner
  angle and outer falloff into Cycles Spot Angle and Blend.
- **Position/aim:** use the native light transform handle. A small direction
  guide follows the actual facing axis, including reversed and two-sided area
  emission.

These use Houdini's installed handles through public HDA viewer-state bindings.
Houdini owns drawing, picking and dragging. Non-spot lights use its OBJ transform
handle with direct channel bindings. Spotlights create a native light handle only
while that type is active, translating pose and coupled cone changes into the
authored object. Shape callbacks convert dimensions into the renderer's parameter
conventions and group each drag into one undo operation. HDA Handle
Bindings and Python state bindings can both use the same native handle library;
dynamic bindings are needed here for scale conversion and coupled cone values.
See [SideFX's handle binding documentation](https://www.sidefx.com/docs/houdini/hom/state_handles.html).

Area dimensions follow object/parent axis scale. Point/Spot radius is a scalar
in scene units, independent of OBJ scale, matching the pinned Cycles engine.
Parent and pre-transform changes update the handles without cooking a SOP.
The viewport light forwards the asset input, so an external Null moves its
outline as well as the rendered emitter. Dynamic shape and spotlight controls
use the parameter frame; Houdini applies the pre-transform and parent/subnet
transform once. Clean Transform changes invalidate the cached handle frame even
when the rendered world transform stays unchanged.
Inactive shape controls are disabled and hidden.

The parented-light regression uses the saved adaptive showcase, actual viewport
picks and handle drags, size changes, parent rotations, reparenting and the
native Move selection workflow while one IPR worker stays active. Clean
Transform is exercised both before rendering and while the handles are active. Headless
checks separately compare the glyph with an independent native Houdini light
and verify handle coordinates, dimensions and world-space point/spot radii.
See the targeted run evidence (`artifacts/verification/parented-lights-20261017/index.json`).
These checks do not establish the cause of the October 17 fatal crash: no
matching native crash stack has been reproduced. Native emitter outlines
remain approximations for inherited nonuniform scale/shear; the headless
glyph comparison follows Houdini Light 2.0 scale-inheritance behavior.
Shape handles are hidden for zero-scale or nonorthogonal/sheared transforms;
the viewport prompt directs the artist to the parameter editor in that case.
This is a handle limitation, not a claim that the renderer rejects all sheared
light transforms.

The visible native tabs are **Transform** and **Cycles Light**. Inside Cycles
Light, parameters use collapsible panels following [Blender's Cycles layout](https://raw.githubusercontent.com/blender/blender/blender-v5.2-release/intern/cycles/blender/addon/ui.py):

- **Light:** Enable, Type, Color, Power/Strength, Exposure and Normalize, followed
  by Radius/Soft Falloff, area Shape/Size X/Y, or Sun Angle for the selected type.
- **Beam Shape:** Spot Angle/Blend, or Area Spread, Single Sided/Reverse.
- **Settings:** Max Bounces, Cast Shadow, Multiple Importance Sampling and
  Shadow Caustics.
- **Ray Visibility:** Camera, Diffuse, Glossy, Transmission and Volume Scatter.
- **Light Groups:** output grouping and Shadow Catcher Reference.

The tab itself is a native tab, not a collapsible panel. Subnet internals are
hidden; settings stay in Transform and Cycles Light. Type offers Point, Spot,
Area and Sun. An ideal distant source is Sun with Angle zero. Square/Disk use X
for both dimensions; Rectangle/Ellipse use both. Camera visibility and Cast
Shadow drive both the renderer and native proxy directly. No duplicate hidden
visibility/shadow fields or old type tokens remain. Emission supports a material,
color texture and IES profile; see [light shaders](../dev/notes/native-light-shaders.md).
Temperature, portals and arbitrary projector controls still require adapters.
The internal native preview approximates features such as area spread.

Power is `Color * Intensity * 2^Exposure` under the documented Cycles radiometric
convention. The same numeric native Houdini intensity is not a brightness match
to Mantra or Redshift. A normalized two-sided area divides its total power between
two analytic sides. Rendering the emitter and lighting the scene are independent:
Camera ray visibility controls emitter visibility while light enable controls
emission.

Native Light 2.0 Point/Grid/Disk/Sphere/Distant/Sun lights work without a Cycles
tab. A sphere uses native Area Size as diameter and currently requires equal
dimensions; native ellipsoid lights need another adapter. Cone Angle, Cone Delta
and Cone Rolloff are evaluated in the emitter's local coordinates and apply to
finite native lights. Native Environment Lights have their own HDRI/world adapter.
Line/tube/geometry lights and textured analytic lights,
IES, projected-light and area-edge-ramp semantics still require additional
adapters. Stock menus and controls remain available for other renderers;
unsupported nondefault settings produce a specific diagnostic. Opt-in light
properties add supported engine controls. Linking and animated light properties across the shutter remain
separate work. Unsupported native settings continue to produce diagnostics.

## Artist scene and verification

The artist's original `sop-fx-courtyard.hiplc` is preserved. Its normal Objects
pattern was empty and Force Objects contained only `fx_courtyard`; the split
ground therefore was not selected. The generated fixture had also skipped native
OBJ initialization. `sop-fx-courtyard-artist-fixed.hiplc` changes Objects to `*` and
restores missing native Render properties on the courtyard. It preserves the
artist's saved lighting, including the disabled original key light. The new
generator writes `sop-fx-courtyard-current.hiplc` with normal OBJ initialization
instead of overwriting the artist's file.

Headless checks compare subdivision with an independently refined Houdini mesh
and area/sun illumination with analytic expectations. Separate CPU/OptiX tests
exercise sync, queued and external-worker paths. Actual GUI workflows use a
single Cycles image panel plus the viewport, test native object parameters, and
verify the saved scene's ground, emitter visibility, illumination and light
creation/movement/deletion. The revised light UI fixture runs registered shelf
tools through Houdini's public viewport API and inspects/captures the native
parameter editor. Physical light checks compare against independent irradiance
and test shape/beam/power updates. This does not constitute broad production
certification.

The native-handle GUI workflow passes in
`prototype-worker-CUDA_NVIDIA_GeForce_RTX_4090_0000_03_00_OptiX-20261012T010641155`.
It sends actual viewport mouse/key events to the native handles, observes size,
radius and cone edits and undo, and checks parent/pre-transform, nonuniform scale
and reflected facing-axis conventions. It also adds, edits and deletes the
selected light during a 1,053,818-triangle SOP IPR with 352 objects, 605 curves,
1,200 points and three VDB fields. Geometry conversions stay at five and the
renderer worker persists through the light updates. This does not measure UI
latency. The independent light-property/rendering regression passes in
`prototype-worker-CUDA_NVIDIA_GeForce_RTX_4090_0000_03_00_OptiX-20261012T010939601`;
both runs have completed source/binary provenance audits.

## Panel monitor scaling

The Cycles image panel corrects Houdini's global pixel-sized Qt theme locally
using destination-screen logical DPI relative to the theme's reference screen.
The application font, theme and DPI policy are not changed. Qt configurations
with constant logical DPI already handle device-pixel scaling and get no extra
UI scaling. Native fields, theme dimensions, margins, icons and status-line
height follow the same local factor. Changes reuse the original metrics to
avoid cumulative scaling. Image zoom uses device pixel ratio independently;
100% remains one render pixel per physical screen pixel. Monitor changes do
not restart rendering or resize its buffers. The monitor GUI fixture visits
all available screens and returns, checking native controls, image zoom,
render generation and unmodified host preferences. Other hardware setups still
require platform certification.

## Fullscreen image and snapshot history

The icon beside image zoom opens the existing image canvas fullscreen on its
current monitor. Top and bottom controls appear when the pointer reaches their
screen edge, overlaying the image without changing its size. They hide 450 ms
after the pointer leaves, while open menus and the color picker keep them visible.
Escape, the revealed fullscreen button, or closing the window returns the canvas
and every control to the panel. Houdini-owned fullscreen exits through Houdini's
native fullscreen API; the viewer does not stack a second fullscreen window on it.
The image canvas owns its arrow/tool cursors rather than inheriting a hidden
native viewport cursor. It paints opaquely, and monitor polling leaves fullscreen
and maximized geometry alone; unchanged minimum widths are not rewritten.

Viewer controls use the project's own 26-icon CC0 SVG set, tinted from the active
Houdini theme for normal, hover, checked and disabled states. A contrast fallback
keeps low-contrast custom theme colors readable. SVGs fit uniformly within their
icon slots; neither non-square artwork nor non-square slots stretch the drawing.
Toolbar buttons centre the icon/text group independently of native style
padding, while keeping the theme's button surface and hover/checked behavior.
Cycles node/shelf icons have a dark rounded backing so node colors cannot hide
the artwork. Render View uses the supplied clapper badge; Render ROP keeps its
previous artwork with the same dark backing. IPR uses Blender's camera-back
"scene" icon on the same backing.
Pan, zoom and progressive updates use the same
canvas and render session. Opening fullscreen does not submit a scene update
or allocate another renderer.

**Snapshot** explicitly captures the displayed image. Renders and interactive
updates do not save snapshots automatically. The horizontal integer slider
beside **Live** browses snapshots from oldest to newest; its rightmost position
returns to Live. Live continues refreshing while a snapshot is selected.
The dropdown and slider select the same image. History is limited to eight
images and 64 MiB, evicting the oldest; oversized images are rejected before
copying. If the selected oldest snapshot is evicted, the panel returns to Live.

Snapshots preserve the display image and its view transform. Pass, exposure,
gamma and color controls, and linear EXR saving, use Live and are disabled while
a snapshot is selected. Snapshots are held in panel memory, not written to disk
or saved in the HIP. They are released when the panel closes.

The GUI snapshot/fullscreen fixture uses the 1,053,818-triangle SOP courtyard
with packed instances, curves, particles and volumes on the external RTX 4090
OptiX worker. It verifies explicit captures only, live edits while browsing,
deformation in fullscreen, keyboard/mouse navigation, restoration, history
bounds and deferred renderer retirement. The monitor fixture opens fullscreen
on each available destination screen and returns with unchanged rendering and
physical image zoom. Screen metadata is cached without retaining native
QScreen objects across display changes.
