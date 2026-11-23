# Native world materials, HDRI, sky and world volumes

## World volume

Inside a **Cycles World Material**, add Volume Absorption, Volume Scatter or
Principled Volume and connect it to **World Output → Volume**. Keep Background
connected separately, or use a volume-only world. Select the material under
**ROP → Scene → Environment → Cycles World Material**. Both viewers and ordered
final rendering use this definition. Restart Houdini to load the current host.

A world medium is unbounded: it does not need a SOP volume. Use constant density
or procedural inputs evaluated in world space. Geometry Position can drive a
density network; image density requires explicit coordinates and correct input
color interpretation. New Principled Volume nodes clear their grid attribute
names in the World context. A copied sparse-volume shader may still name grids:
clear those names to use its constant inputs. No arbitrary SOP fields are
available in the world. Such unavailable inputs disable the medium with a
diagnostic while retaining its independent background and scene geometry.

An infinite absorbing medium can extinguish an infinitely distant environment;
the homogeneous absorption test has a black miss ray. This is distinct from a
failed render. Use bounded SOP volumes for bounded fog. Native world-medium
thumbnails use an emissive reference sphere to make finite-path attenuation
visible. Viewer warnings identify the World Material path.

Uniform absorption, world-position procedural density and emitting Principled
Volume have independent CPU/4090/3080 Ti image references. Two five-frame
sequences per device verify ordered output and a recoverable middle-frame field
failure with diagnostics/repair. CPU and 4090 native parameter/camera edits pass
without geometry reload. Broader scattering/phase-function/production FX
certification remains open. Worlds retain requested-frame sampling when their
values vary over the shutter; world motion blur remains unsupported.

World source-path metadata was introduced in protocol 36; the current matched
linking host/worker uses protocol 38. Do not mix
old workers/hosts. The evidence is in `artifacts/world-volumes-evidence-20261018.json`.

The separate courtyard example is
`artifacts/examples/sop-fx-courtyard/sop-fx-courtyard-world-volume.hiplc`.
Open it with the existing launcher:

```powershell
.\scripts\launch-sop-showcase.ps1 -SceneFile .\artifacts\examples\sop-fx-courtyard\sop-fx-courtyard-world-volume.hiplc
```

Edit `/mat/courtyard_world_fog/world_fog` Density during IPR. Disconnect its
Volume wire to compare with clear air. Clear/fog EXRs and OCIO display PNGs are
in the adjacent `world-volume/` directory. The original courtyard HIPs are intact.

Native OBJ/SOP rendering supports Houdini Environment Lights, file HDRIs and an
authored **Cycles World Material** context. The Cycles panel, native IPR and SOHO
batch rendering share the same world definition. This is a tested development
feature; stage 3 and production certification remain in progress.

## Use an Environment Light

Restart Houdini to load the rebuilt package, then enable the **Cycles Tools**
shelf if hidden. **Environment Light** creates Houdini's native `envlight` object
with its normal environment icon and an optional **Cycles Light** tab. Existing
Environment Lights work without that tab; **Add Native Light Tab** adds the extra
controls. Native Transform, Light and Shadow tabs remain alongside it.

1. In the native **Light** tab, choose a file environment map and set Color,
   Intensity and Exposure. Leave the mode at **Direct Lighting**. Enable
   **Render Light Geometry** to see the environment behind the scene.
2. In **Cycles Light**, choose **Input Color Space**. **Auto** uses the
   integration's Houdini OCIO input policy. **Data** bypasses input conversion.
3. Keep **Multiple Importance Sampling** enabled for efficient HDRI lighting.
   **Map Resolution = Automatic (0)** means automatic. Choose a preset or enter
   a value from 16 to 16384. There is no continuous slider through invalid
   values below 16. The number
   sets the importance-map width; it does not resize the source image.
4. On the ROP, open **Scene → Environment** and choose **Scene Environment
   Light**. Lights, Force Lights and Exclude Lights select the environment in
   both OBJ-scene and explicit SOP-source mode.
5. Use either **Cycles Render** or Houdini's **Render View**. Rotate the
   environment, change intensity or replace its image file while rendering.
   Disk-only changes are observed without manual refresh.

Power is `Color * Intensity * 2^Exposure` under the Cycles radiance convention.
The same intensity number is not a brightness match to other renderers. Native
camera visibility and diffuse/specular contribution controls have adapters;
the optional ray-visibility override separates Camera, Diffuse, Glossy,
Transmission and Volume Scatter.

The adapter preserves Houdini's Y-up map convention, object/parent rotation and
local +Y hemisphere clipping. Translation does not move an infinite environment.
These conventions follow [SideFX's Environment Light documentation](https://www.sidefx.com/docs/houdini/nodes/obj/envlight.html).
Zero-scale or sheared orientation produces a diagnostic.

Cycles has one world. Selecting several enabled environments identifies them in
a diagnostic. Select one with the light patterns, or combine textures in a World
Material. File and owned `op:` images are supported. Houdini's native environment
procedural-sky controls, portals, AO/background modes, glossy filtering and
contribution/shadow overrides require further adapters. Stock Houdini controls
remain available for other renderers. Unsupported optional settings now retain
the supported image/color lighting and report `world.unsupported_setting`, naming
the omitted control. AO/background modes use Direct Lighting; native portals,
glossy filtering, contribution lists and unsupported shadow overrides are ignored
locally. Supported visibility, intensity, orientation, MIS and shadow controls
continue working. Disabled native controls are dormant and produce no warning.
Repairs and deletion clear the corresponding diagnostics.

An unusable nominal source, including native procedural sky without an adapter,
still uses the configured constant background with `world.unavailable`. It does
not silently use an inactive environment-map path. Invalid power/exposure is
handled locally before strict engine validation. Ignored optional animation does
not invent world-motion warnings or duplicate diagnostics per shutter sample.

CPU and RTX 4090 OptiX each pass 48 focused recovery checks, five native ROP
frames and 22 accepted live **Cycles Render** states. Background radiance,
Lambertian illumination, Depth and Position have independent expectations.
Seven heavy native Render View states per device retain one SOHO helper and
renderer worker, with unchanged geometry conversions. The fixture contains
1,053,818 input triangles, 352 objects, curves, points and three VDB fields.
Closure waits for asynchronous retirement and verifies `closed` with zero owned
geometry and tracked device bytes. These are targeted development checks;
physical display/latency and broader device certification remain separate. See
environment recovery evidence (`artifacts/verification/environment-recovery-20261017/index.json`).

## Import a Blender world

A World asset from a Blender library imports in one of two ways:

- **A simple HDRI world** becomes an **Environment Light** in `/obj` named after the
  world. Simple means: an Environment Texture (equirectangular, Linear) feeding the
  Background Color directly or through one constant colour multiply, a constant
  Strength, an optional Texture Coordinate Generated and one Mapping node that only
  rotates about Z, no volume and default ray visibility. The light's Environment Map is
  the HDRI where the `.blend` file finds it (a packed HDRI is extracted to the blend
  folder's `tex/`), Intensity is the Strength, Color is the tint, and the Mapping
  rotation turns the light (Transform tab), so the HDRI lines up with geometry
  converted from the same file. Sliders driven by drivers (Poly Haven's rotation and
  strength) are read with their current values.
- **Any other world** becomes a **Cycles World Material** in `/mat`, translated node by
  node. Nodes the World Material cannot evaluate refuse the import with the reason. The
  report says which simple-world condition failed. World ray visibility is not
  imported; set it on the ROP under Scene > Environment.

Link imports one shared, locked node per world (grey, comment *Linked from <file>:
<world>*); only Enable stays editable. Convert & Append creates an editable copy each
time. Cycles has one world, so when the scene already has environment lighting the
import asks: **Replace** (the new world becomes active; other Environment Lights are
disabled, not deleted; a World Material is set on every Cycles ROP), **Keep existing**
(the new light is imported disabled, or the World Material is not set on a ROP), or
**Cancel**.

## Author a World Material

Create **Cycles World Material** in `/mat`. Its default **Background** connects
to **World Output**. Add **Environment Texture**, choose its file/color space and
connect Color to Background Color. Background Strength controls radiance. On the
ROP choose **Cycles World Material** as World Source and select the builder. Its
unused fallback Background Color is disabled in the native editor.

Supported texture, arithmetic, color, vector and closure-combining nodes can
build a world graph. Typed subnets, reroutes, ramps, promotion and HIP persistence
use the surface-material services. Object-specific inputs, surface BSDFs,
and displacement require a different material domain. World volume outputs
are supported as described above. Native graph
authoring does not establish Blender node-tree import compatibility.

World Material coordinates use Cycles' Z-up direction convention. Use explicit
direction/Mapping nodes when authoring orientation; the native Environment Light
adapter performs its own Y-up conversion. Combined textures share one World
Light Group and Shadow Catcher Reference policy.

In Houdini's Python shell:

```python
import cycles_houdini as ch
world = ch.create_world_material(name="studio_world", texture="C:/assets/studio.exr")
rop = hou.node("/out/cycles_sop_courtyard")
rop.setParms({"world_source": "material", "world_material": world.path()})
```

**Constant Color** restores the legacy background. Scene Environment Light also
uses that fallback when no environment is selected. File names and graph values
evaluate at the requested frame. Animated HDRIs work across batch frames. World
changes within a motion-blur shutter retain requested-frame lighting and report
`world.motion_ignored`; world motion blur is not implemented. Static COP sources
stay valid when geometry motion blur is enabled, and pinned-frame sources remain
static. Invalid nominal world sources use the configured background. Repairing
the source or disabling blur clears the corresponding diagnostic.

Owned legacy COP/COPernicus worlds and native Environment Lights have independent
illumination, automatic MIS dimension, live repair and five-frame batch checks
on CPU and RTX 4090 OptiX. World metadata capture adds no COP pixel cooks. See
COP world/shutter evidence (`artifacts/verification/cop-world-shutter-20261017/index.json`)
and [COP image behavior and limits](../dev/notes/native-cop-images.md). World volumes are
described above; environment UDIMs remain separate work.

## Author a procedural sky

Use **Cycles Tools → Sky World**, then select the new `/mat/cycles_sky_world`
builder under **ROP → Scene → Environment → Cycles World Material**. Open its
**Sky Texture** node to edit the native Houdini parameters. The shelf action
creates a connected Sky Texture, Background and World Output; it leaves ROP
selection to the artist. Existing worlds are preserved.

The Type menu contains the four models in the pinned engine: **Multiple
Scattering**, **Single Scattering (Legacy)**, **Preetham (Legacy)** and
**Hosek / Wilkie (Legacy)**. The controls follow
[Blender's Sky node](https://raw.githubusercontent.com/blender/blender/main/source/blender/nodes/shader/nodes/node_shader_tex_sky.cc):
modern models expose Sun Disc, angular size, intensity, elevation, rotation,
altitude and air/aerosol/ozone densities. Legacy models expose direction and
turbidity; Hosek/Wilkie also exposes ground albedo. Irrelevant controls hide,
while their stored values are retained when switching models.

Sun Size, Sun Elevation, Sun Rotation and **Sky Rotation** use degrees in the
Houdini interface; the owned engine graph stores radians. Altitude uses metres
above sea level, independently of camera position. Sky Rotation defaults to
`(90, 0, 0)` to place the sky above Houdini's Y-up horizon. `(0, 0, 0)` uses the
engine's Z-up convention. Legacy Sun Direction is in the sky model's local Z-up
coordinates and is normalized before rendering.

Sky Rotation uses an authored `sky_rotation` parameter, translated into the
engine's `tex_mapping.rotation` socket. Unlike older hidden dot-named texture
fields, it retains promoted expressions and keyframes through HIP reload.
Sun controls are also promotable and animated. This is authored integration
metadata; the reflected engine catalogue retains its original socket IDs and
radian defaults. The Blender source was used as a reference, not copied.

Leave Vector unconnected for world-ray coordinates and efficient direct solar
sampling. The modern sky uses a 512×256 importance map alongside its sun
sampler; legacy skies use the 1024×512 procedural fallback. A linked coordinate
graph can require the engine's larger transformed-sky fallback. Changing camera
position retains the world graph, sky data and importance map. Sky edits rebuild
dependent lighting resources while retaining meshes and displacement.

Modern skies can produce high linear values. Adjust the render panel's display
Exposure or the Background Strength to inspect the result. A camera looking
below the sky horizon can see a dark background while the scene still receives
sky lighting. Exposure changes keep path tracing; Background Strength edits
update lighting.

Sky Texture also supplies colors to surface and volume graphs. On surfaces an
unconnected Vector uses bounds-normalized nominal SOP coordinates, which are
not unit directions. Link an explicit unit direction when directional evaluation
is required. Volume use requires explicit coordinates; an unused Sky node adds
no coordinate requirement. Invalid parameters rejected before a renderer scene
transaction produce a diagnostic and preserve the accepted scene and image.
The queued and external backends retain their GPU resources and snapshot
acknowledgement; repairing the edit reuses the same renderer. The accepted scene
can continue tracing or remain paused while the rejected edit is shown. Device,
transport or transaction failures still require backend recovery. See
[error and recovery semantics](../dev/notes/native-worker-prototype.md#rejected-edits-and-renderer-failures).

Independent reference scenes exercise all four models, sun/atmosphere controls,
orientation, surface emission and real SOP diffuse illumination. Volume emission
is compared with independently rendered sky radiance and a known density
integral. The heavy artist workflow executes the actual shelf action and types
35 degrees into the native sun-elevation field. It switches models while one
Cycles panel renders and checks retained geometry, displacement and worker
identity. Frozen run reports and immediate provenance audits are indexed by
`artifacts/stage3/world/sky/verification-summary.json`. These checks do not
establish full Blender feature parity or production performance.

## Update cost and automatic sampling

Environment edits retain mesh conversion and displacement. Camera edits retain
the world graph and importance map. Sampling/visibility settings retain the
graph; texture revisions and graph edits rebuild dependent lighting resources
at a safe engine boundary. Native tab/panel navigation does not queue a render
update. The current payload protocol is 16: owned world definitions introduced
in version 15 remain supported, with an explicit pre-transaction rejection flag.

The pinned automatic-resolution path applied its multiple-Sky-Sun 4096×2048
fallback even when no Sky Sun existed. The same condition was observed in the
[official Blender source](https://raw.githubusercontent.com/blender/blender/main/intern/cycles/scene/light.cpp).
Patch `0020-world-auto-map-resolution.patch` restricts that fallback to multiple
suns. Ordinary HDRIs use image dimensions, combined environments use the largest
dimensions, and procedural worlds without an image use the existing 1024×512
fallback. Manual resolution remains authoritative. Transformed-sky and
multiple-sun fallbacks are preserved. Sky authoring has its separate focused
evidence described above.

The regression first reproduced the oversized map. After the fix, its 128×64
HDRI requests 128 KiB of shader-evaluation input instead of 128 MiB. In the heavy
artist fixture's 256×128 HDRI, that request drops to 512 KiB, and its conditional
CDF drops from 67,125,248 to 263,168 bytes. One before/after observation recorded
light preparation at 136.5 ms and 28.6 ms. The comparison is saved in
`artifacts/stage3/world/world-auto-map-comparison.json`.

Those October 12 input-buffer observations used a 16-byte shader-evaluation
input. The current temporal-shader ABI uses 32 bytes (patches 0024-0025): a
128x64 map requests 256 KiB and a 256x128 map requests 1 MiB. CDF dimensions and
their allocation formula are unchanged. The historical timing observations are
not new measurements of the current ABI.

These are logical copy/allocation requests and individual diagnostic timings,
not physical PCIe measurements or a general speed comparison. Lighting tables
can still update across the scene while vertex/index buffers remain retained.
Large HDRIs, physical GPU residency, geometry-edit viewport stalls and
Blender/Redshift comparisons retain separate gates.

The pinned sampler also wraps linear filtering across the panorama's first/last
rows at the exact polar singularity. Direction tests use independently labeled
polar caps away from that seam; this increment does not fix it.

## Previous world/HDRI verification evidence

Frozen manifest:
`90de54e05bd31d6ba648417c822d33405d2dfdd5aaaad2ba0fa59ac736f3bdeb`.
It records 330 integration sources, 20 reproduced patches, six kernels and five
binaries. Each run below exited 0 and passed its immediate source/binary audit:

| Check | Run under `artifacts/verification` |
| --- | --- |
| Twelve world/color/update checks, CPU sync | `prototype-sync-CPU-20261012T022639443` |
| Twelve world checks, RTX 4090 OptiX worker | `prototype-worker-CUDA_NVIDIA_GeForce_RTX_4090_0000_03_00_OptiX-20261012T022647207` |
| Seven sampling/copy/lighting contracts, CPU sync | `prototype-sync-CPU-20261012T022545503` |
| Seven sampling contracts, OptiX worker | `prototype-worker-CUDA_NVIDIA_GeForce_RTX_4090_0000_03_00_OptiX-20261012T022558381` |
| Five ordered ROP frames with animated HDRI/deformation | `prototype-worker-CUDA_NVIDIA_GeForce_RTX_4090_0000_03_00_OptiX-20261012T022704776` |
| Heavy native IPR, persistent pipe/worker | `prototype-worker-CUDA_NVIDIA_GeForce_RTX_4090_0000_03_00_OptiX-20261012T022710995` |
| Heavy Cycles-panel artist workflow | `prototype-worker-CUDA_NVIDIA_GeForce_RTX_4090_0000_03_00_OptiX-20261012T022841291` |
| Independent analytic-light regression | `prototype-worker-CUDA_NVIDIA_GeForce_RTX_4090_0000_03_00_OptiX-20261012T023052784` |

GUI fixtures contain 1,053,818 input triangles, 352 rendered objects, 605 curves,
1,200 points and three VDB fields. The panel executes the real shelf action,
clicks the native Environment header and verifies expansion without a tracing
reset, then edits rotation, files and World Material strength. Explicit SOP
enable/edit/create/delete cases pass. Only explicit snapshots enter history.
Native IPR independently reads expected linear pixels through its image interface.
Each workflow uses one render viewer, retains geometry and one renderer worker
through environment edits, and closes cleanly.

The five-frame 128×96 fixture is a correctness sequence, separate from the heavy
GUI scene. Static geometry converts once; the deforming mesh converts each frame.
Conversion counts are `[2, 3, 4, 5, 6]`, world builds are `[1, 2, 3, 4, 5]`, and
the worker persists. EXR world radiance and unit-Lambertian illumination match
independent expectations. Tracked allocations reach zero on closure.
