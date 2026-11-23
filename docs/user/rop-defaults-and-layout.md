# Native ROP layout and defaults

The October 13, 2026 revision keeps common shot settings above the detailed tabs.
**Render with Take** sits directly below **Start/End/Inc** above the tabs.
Single-value collapsible sections are replaced by separators and direct controls.
File encoding and Film have separate tabs under Image Output. Final Render keeps
its sampling and denoising under Rendering / Sampling. Interactive sampling,
resolution and denoising live under Rendering / IPR Settings / Sampling, alongside
the existing tessellation controls. Saved per-node layouts are rearranged on load
without changing parameter names, values, expressions or animation.
The single-SOP testing path remains functional but is hidden; normal authoring uses
object inclusion patterns.

New ROPs default **Output Picture** to `$HIP/render/$HIPNAME.$OS.$F4.exr`.
The wide input connector accepts upstream ROP dependencies. Houdini runs those
inputs before Cycles through its standard ROP execution, including saved-HIP
farm renders. The **Adaptive Subdivision Update** control is in Rendering /
IPR Settings.

**Refit Meshes While Interacting** (`ipr_refit_meshes`, Rendering / IPR Settings, off by
default): in a live render, a mesh whose points move but whose triangles stay the same is
updated in place (an acceleration-structure refit) instead of rebuilt, as Blender's viewport
does for meshes. The first image after a change comes a little sooner (about 10 % on a
5-million-triangle mesh, measured) and the image is identical; GPU memory during the update is
about 6 % higher, because the mesh data stays on the GPU while the structure is updated. Curves,
points, volumes and subdivided or displaced meshes are always rebuilt (refitted curves render
noticeably slower afterwards; Blender's viewport rebuilds them too). A mesh is rebuilt when its
triangles or motion steps change and after 16 refits in a row. Final and batch renders always
rebuild. Changing the checkbox restarts the live render session.

## Performance and transparency

**Performance → Tiling (Final Render)** enables Cycles' final-render tiles and
defaults to a **2048** pixel tile size. Smaller images render as one tile. This
applies to Render to Disk and Render to MPlay, including sequences; IPR remains
progressive and ignores these controls. Smaller tiles reduce rendering-buffer
memory, but do not split scene geometry or displacement into separately loaded
pieces. Cycles uses temporary disk storage and assembles the full image before
final output. Full-frame host output and final denoising still need their normal
memory; GPU denoising can therefore remain a memory limit. Temporary tile files
are removed when processing finishes or the render session closes.

**Texture Cache** has an enable checkbox in its header, **Auto Generate**, and
**Generate / Clear** actions. See [texture-cache actions](native-textures.md#texture-cache-actions)
for their scope. **Image Output → Film → Transparent** uses the same header
checkbox pattern, with **Transparent Glass** and **Roughness Threshold** inside.
These layout changes retain the existing transparency parameter names and values.

## Object selection, render scripts and CPU guiding

On current ROPs the object-pattern parameters are `vobject`, `forceobject` and
`excludeobject`. In older HIPs, shelf tools or expressions, copy any authored
patterns from `objects`, `forceobjects` and `excludeobjects` to their respective
new parameters. The old names have no aliases.

The **Scripts** tab provides Houdini's standard pre-render, pre-frame,
post-frame and post-render hooks with a language selector for each. Houdini
executes these for Render to Disk, `hou.RopNode.render()` and ROP Fetch.
After a script failure, the console and the ROP's
`cachedUserData("cycles_houdini.final_sequence")` give the failing node, frame,
phase and first error.

Select **CPU** in Devices to see **Path Guiding (CPU)** under Lights in the
sampling settings. Guiding starts disabled. **Deterministic Guiding** is in the
advanced sampling controls and defaults on. Changing the device to GPU hides
the controls and retains their values for the next CPU render.

Resolution is one integer vector (`res`, components `resx`/`resy`), as on Houdini's
own render nodes. The mini menu beside it lists Houdini's standard resolutions
(`FBres`, which a studio can override on `HOUDINI_PATH`) and sets width, height and
pixel aspect in one undo group. It leaves final/IPR percentages unchanged.
Environment's Volume Sampling (Distance, Equiangular, Multiple Importance) applies to
a World Material volume and defaults to Equiangular, as Blender's World settings do;
object volumes keep Cycles' Multiple Importance default.
World Material uses the VOP picker because Cycles builders live in VOP networks,
not SHOP networks. Its adjacent list includes only Cycles world builders; Default
Material lists surface/volume builders. The camera picker retains the native camera
filter and SOP/light pickers retain their respective supported host types.

## Blender comparison

The ROP Subdivision panel contains scene dicing policy: **Dicing Rate Render**
(1), **Viewport** (8), **Offscreen Scale** (4), **Max Subdivisions** (12), and
an optional **Dicing Camera**. Geometry opts in through each object's Subdivision
header. Scene rates multiply the object's adaptive Pixel Size or Edge Length;
uniform object levels stay independent of the adaptive scene limit.
An empty Dicing Camera follows the render camera. A separate camera fixes the
dicing reference for IPR and disk renders without replacing the rendered view.
**Limit Subdivision Triangles** is an integration-specific guard at the bottom of
this panel, disabled by default. When enabled, **Triangle Budget** starts at
100 million per mesh and accepts larger values. It reports excess rather than
capping tessellation invisibly.

The ROP Points and Curves panel has **Curve Subdivisions** (`curve_subdivisions`),
Blender's Render Properties > Curves > Curve Subdivisions: default 2, range 0-24, a
whole-scene Cycles setting. Blender shows it only for Rounded Ribbons because its curve
Shape is scene-wide; here Shape is chosen per object, so it is always shown. Changing
it during interactive rendering restarts the render session (Cycles recreates its
scene for a changed scene parameter, as Blender does). **Bezier/NURBS Segments**
(`curve_segments`) below it is this integration's own setting; see
[Hair and curve width](native-object-properties.md#hair-and-curve-width).

Source inspected: [Blender Cycles properties](https://github.com/blender/blender/blob/main/intern/cycles/blender/addon/properties.py),
October 13, 2026. These are source defaults, not the values of a user's startup file.
No new engine feature is inferred from a newer Blender UI property.

New ROPs now use Automatic sampling, adaptive sampling on, 4096 final samples,
1024 interactive samples, and independent interactive settings. Final denoising is
OIDN/high/accurate/albedo-and-normal with CPU denoising by default; interactive
denoising is off by default, with automatic/balanced/fast/albedo and start sample 1
when enabled. Thresholds remain 0.01 final and 0.1 interactive, minimum samples 0.
Maximum bounces are 12 total, 4 diffuse, 4 glossy, 12 transmission, 0 volume and 8
transparent. Glossy filtering is 1 and light sampling threshold 0.01.

Integration-specific choices remain explicit:
GPU selection, geometry conversion guards, motion correspondence,
and Houdini's scene units/OCIO configuration. See "Defaults follow Blender" below.

**Resampled Light Sampling** is optional and selective. At a primary shading point
made entirely of sharp reflective microfacet lobes, it retains standard Cycles
light sampling and the normal reflected-ray light contribution. Both microfacet
alpha axes must be at most 0.01 (isotropic artist roughness about 0.1 or below).
Anisotropic lobes with a broader axis, diffuse/subsurface/transmission mixtures,
and later bounces keep the existing RLS strategy. The decision uses the evaluated
closures, including textures, and is shared by direct sampling and the forward
MIS weight; no transport is discarded. Huang hair retains its separate native
fallback. RLS can improve direct lighting but is not a universal speed or noise win.

## Color meaning

The unassigned-geometry fallback **Material color** defaults to white `(1, 1, 1)`.
Saved colors are retained; **Revert to Defaults** applies white to an existing ROP.
Its optional `Cd` multiplier retains the selected shader/tint on packed instances.


The artist's revised arrangement is recorded in `scripts/rop-layout.json`;
renderer bindings/defaults are applied separately. Devices has its own tab and
Max Samples is inside Final Render. Presets, expressions and the renderer all
read the one Resolution vector.

Interactive Render settings are independent. The old inheritance switch and
Viewer Denoising override have been removed. Start Sample is enabled
when interactive Denoise is enabled. The viewer's Interactive toggle chooses
Interactive Render or Final Render quality settings; its Denoise and Resolution
controls edit the selected set. Auto independently controls scene-following.
Disk renders always use Final Render settings. Final-quality viewing retains
the responsive progressive session; it does not change renderer process modes.

EXR RGB is written in the active Houdini OCIO configuration's scene-linear space,
which may be ACEScg or linear Rec.709. This is not a single fixed "Houdini" color
space. Texture interpretation and viewer display/view transforms use the same
configuration; display grading is not baked into EXR. Data and Cryptomatte remain
raw. Enable Override Color Space under Image Output / File to convert only color passes to a named non-data space in the active configuration. Alpha, numeric passes, denoising guides and Cryptomatte are untouched. The menu refreshes from the active config. If it changes since rendering, save reports that a new render is needed instead of interpreting old pixels using the wrong config.
EXR metadata records the destination space and its interop identity as well as the original render working space. Viewer Save EXR follows the selected ROP output settings. With the override off, output remains scene-linear. Nonlinear output spaces prevent additive light-pass reconstruction; use a linear destination for that workflow.

## Defaults follow Blender

Every default that has a Blender equivalent is Blender 5.3.0's factory default, read
from Blender itself (`blender -b --factory-startup`; for new lights, cameras, objects
and materials the value Blender gives a newly added one). The ROP's DialogScript is
the single source of its defaults. The November 14, 2026 alignment changed:

| Setting | Old default | New default (Blender) |
| --- | --- | --- |
| Film / Pixel Filter | Box | Blackman-Harris |
| Film / Filter Width | 1 px | 1.5 px |
| Advanced Sampling / Sample Length (Sample Subset) | 1 | 2048 |
| Performance / Persistent Data | on | off |
| Passes / Mist Start | 0 | 5 |
| Passes / Mist Depth | 100 | 25 |
| Passes / Mist Falloff | Linear | Quadratic |
| Object / Curves / Shape (and objects without a Cycles tab) | Linear 3D Curves | Rounded Ribbons |
| Object / Subdivision / Boundary Smooth | Keep Corners | Smooth Corners (Blender "All") |
| Object / Subdivision / Edge Length | 0.1 | 0.01 |
| Cycles Light / Radius (point, spot) | 0.1 | 0 |
| Cycles Light / Spot Blend | 0 | 0.15 |
| Cycles Light / Area Shape (also the Area shelf tool) | Rectangle | Square |
| Cycles Light / Area Size, Size Y | 2 | 1 |
| Cycles Light / Ray Visibility / Camera | on | off |

Sampling pattern (Automatic), samples (4096 final, 1024 interactive), adaptive sampling,
denoising, bounces, Filter Glossy (1) and Light Threshold (0.01) already had Blender's
values in the built ROP; the DialogScript now states the same values instead of older
development values that the build replaced.

Saved scenes keep their values. Houdini stores every ROP, Cycles Light and Cycles tab
parameter in the `.hip`, including parameters left at their default, and a Cycles tab
keeps the parameter definitions it was installed with. A scene saved before this change
therefore renders as before, except for one case: curves on objects without a Cycles
object tab now render as Rounded Ribbons. Add the tab and choose Linear 3D Curves to
keep the previous look. **Revert to Defaults** applies the new values.

Deliberate exceptions, where Houdini's convention or the integration wins:

- Resolution (640 x 360), frame range, Output Picture, camera selection, EXR precision
  and compression, and colour management through Houdini's OCIO configuration.
- Render Device defaults to GPU (OptiX); Blender defaults to CPU because no compute
  device is configured in a fresh install.
- Deformation motion blur defaults to Off (transform blur only). Blender deforms every
  object by re-evaluating its scene per step; the equivalent here, Geometry Samples,
  cooks every SOP network at each shutter sample.
- Light brightness (intensity, exposure, Power units) is a separate decision and was
  not changed. The Cycles Light type stays Area: Blender has no default type.
- The native Houdini camera keeps its own focal length, aperture, clipping, focus and
  F-Stop; the Cycles camera tab follows the native camera's Focus Distance and F-Stop.
- The unassigned-geometry fallback material stays white diffuse `(1, 1, 1)`; Blender's
  material-less surface is a fixed grey `(0.8)` with no setting.
- Features without a Blender equivalent keep their own defaults: Nested Dielectrics,
  Resampled Light Sampling, DLSS, the texture cache policy, tiling memory guards,
  Triangle Budget, Bezier/NURBS Segments, point radius and curve width without attributes.

### Shader node defaults

A newly created `cycles::*` shader node has the default Blender 5.3.0 gives the same
node added to a new material (read from Blender itself, every input socket and
property of every node type). Where that differed from the reflected standalone
Cycles default, which Blender's own sync never uses, the November 14, 2026
alignment changed:

| Node / parameter | Old default | New default (Blender) |
| --- | --- | --- |
| Emission / Color, Strength | 0.8, 10 | 1, 1 |
| Glossy BSDF / Distribution | GGX | Multiscatter GGX |
| Glass BSDF / Color, Distribution | 0.8, GGX | 1, Multiscatter GGX |
| Refraction BSDF / Color, IOR, Distribution | 0.8, 0.3, GGX | 1, 1.45, Beckmann |
| Hair BSDF / Roughness U, Roughness V | 0.2, 0.2 | 0.1, 1 |
| Principled Hair BSDF / Model | Huang | Chiang |
| Sheen BSDF / Roughness | 1 | 0.5 |
| Noise, Checker, Wave Texture / Scale | 1 | 5 |
| Wave Texture / Detail Scale | 0 | 1 |
| Checker, Brick Texture / Color1, Color2 | 0, 0 | 0.8, 0.2 |
| Brick Texture / Mortar Smooth | 0 | 0.1 |
| Voronoi Texture / Smoothness | 5 | 1 |
| Bump / Distance | 0.1 | 0.001 |
| Displacement / Scale | 1 | 0.01 |
| Vector Displacement / Scale, Space, Vector | 1, Object, 0 | 0.01, Tangent, 0.8 |
| Normal Map / Base | Original | Displaced |
| Tangent / Axis | X | Z |
| Normal / Normal input, direction | (0, 0, 0) | (0, 0, 1) |
| Vector Transform / Vector | 0 | 0.5 |
| Hue/Saturation/Value, Separate Color / Color | 0 | 0.8 |
| Brightness/Contrast, Gamma / Color | 0 | 1 |
| RGB to BW / Color | 0 | 0.5 |
| Color (RGB) / Value | 0 | 0.5 |
| Blackbody / Temperature | 1200 | 6500 |
| Map Range, Vector Map Range / Clamp | off | on |
| Math / third Value | 0 | 0.5 |
| Switch (Color) / False, True; Index Switch (Color) / items | 0 | 0.8 |
| Enable Output / Enable | on | off |
| Enable Output (Color) / Value | 0 | 0.8 |
| Warning / Message, Severity | "Warning", Warning | empty, Error |

Saved scenes keep their values: Houdini stores every parameter of these nodes in the
`.hip`, so a network saved before the change reopens with the same values and renders
the same. Blender imports set every value the Blender node carries; the material
export now also carries Normal Map Base and Convention and Principled Hair Model, so
an import never takes them from a node default. The material converter sets Normal
Map Base to Original explicitly, as before. **Revert to Defaults** on a node applies
the new values. The Light Material builder still creates its Emission at Color 1,
Strength 1, which is now also the node default.

Deliberate exceptions, where the value differs from Blender's on purpose:

- Attribute / Name defaults to `Cd`, Houdini's colour attribute; Blender's empty name
  reads nothing.
- Color Ramp / Interpolation defaults to Native (Per Stop) with linear stops, and the
  Float, RGB and Vector Curves start with linear keys. Both evaluate exactly as
  Blender's default Linear ramp and default straight curve; Native keeps the Houdini
  ramp's own per-stop basis authoritative once an artist edits it.
- The one Mix node starts in Color mode, as Blender's Add > Color > Mix Color does (a
  bare Blender Mix node starts in Float mode); its Factor is 1, Blender's Mix default.
- Sky Texture keeps its Sky Rotation (90, 0, 0), which places Blender's Z-up sky in
  Houdini's Y up; image and environment textures keep their Houdini colour space,
  alpha and missing-texture controls, which Blender does not have.
