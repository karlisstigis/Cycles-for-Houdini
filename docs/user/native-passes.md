# Native render passes

The ROP selects from the pinned engine's supported pass catalogue. Currently
selectable: Combined, Depth, Normal, Position, UV, Sample Count, Roughness, Mist,
Object Index, Material Index and Shadow Catcher,
Diffuse Direct/Indirect/Color, Glossy Direct/Indirect/Color, Transmission
Direct/Indirect/Color, Volume Direct/Indirect, Emission, Environment and Ambient
Occlusion, Motion Vectors, Noisy Beauty and independent Denoising Albedo/Normal/Depth. Their filtering,
channel counts and display eligibility are recorded by the catalogue.

The dedicated panel uses DisplayDriver for beauty and progressive OutputDriver
delivery for selected other passes. Output updates are throttled to ten per
second. GPU packed film data is read once per output callback before extracting
CPU planes; this is not GPU zero-copy AOV display. Changing the selected panel
pass preserves the scene and render generation. Native IPR can display all
enabled planes from the shared session.

EXRs use lossless storage with optional half precision for color; data and IDs
retain float32. Data passes bypass color transforms. See
[vectors, denoising and output](../dev/vectors-denoising-output.md) for timing, channel
conventions, supported controls and output policy.

## Custom shader AOVs

Add entries under **Passes → Shader AOVs** on the native Cycles ROP. Each has an
enable checkbox, name and Color/Value type. Inside a Cycles surface material,
create **AOV Output** and enter the matching case-sensitive name. Feed its Color
or Value input; it does not connect to Surface Output. The ROP's type determines
which input is used. Unregistered writers are ignored; enabled passes without
writers are zero.

Color is filtered scene-linear RGBA, displayed through OCIO. Value is filtered
scalar data and bypasses color transforms, exposure and gamma. Neither is
denoised. EXR channels are `<name>.R/G/B/A` or `<name>.X`, float32 with lossless
ZIP compression. Pass selection changes the progressive preview without
resetting samples; native displays can receive all enabled planes.

Names use ASCII letters, digits and underscores, start with a letter or
underscore, and have at most 128 characters. Built-in pass names and bare R/G/B/A
channels are reserved. This initial integration allows at most 64 entries and
one writer per name in each material. These are integration boundaries, not a
claim about every Blender workflow. World and volume AOV authoring remain
unavailable here.

Changing the list or its types rebuilds affected saved shader definitions,
including inputs previously removed by engine optimization. Unchanged geometry
is retained. The persistent worker carries these definitions in protocol 14.

## Light groups

Enable named outputs under **Passes → Light Groups** on the ROP. Add optional
Cycles properties from the top menu, then enter the matching **Light Group** on
a light's Cycles Light tab or an emissive geometry object's Cycles tab under
Shading. Every component and packed child of that geometry object inherits its
assignment. The ROP **Scene → Environment → Light Group** assigns the world.

Each output is `lightgroup_<name>` with filtered scene-linear RGB. It contains
that group's lighting contribution, including direct and indirect illumination;
it has no alpha and the engine's normal pass denoiser does not denoise it.
Groups do not alter Beauty or light linking. Ungrouped/unregistered emitters
still illuminate Beauty; enabled groups without emitters are zero. When every
emitter and the world belongs to an enabled group, the raw group RGB sum
reconstructs raw Beauty RGB. Shadow-catcher workflows require separate validation.

Names are case-sensitive ASCII identifiers of 1–128 characters. The initial
integration supports up to 64 enabled group outputs, independently of 64 shader
AOVs. A shader AOV cannot use the same output name as an enabled light group.
Changing assignments retains geometry; changing group outputs reallocates film
passes. Both displays and native batch EXR output share this definition.

The viewer uses per-frame pass metadata: Color AOVs support RGB/alpha inspection;
RGB light groups disable Alpha; scalar/vector data disables color controls.
Exposure, gamma and OCIO controls are also disabled during alpha inspection.

## Cryptomatte

Enable **Object**, **Material** and/or **Asset** under the ROP's **Passes →
Cryptomatte**. Levels selects 2–16 ID/coverage pairs; two fit each RGBA layer, so
odd values round up. Outputs use standard `CryptoObject00`, `CryptoMaterial00`,
`CryptoAsset00` names with successive numbered layers.

Object names default to OBJ paths, shared by mesh, curve and point components
inside an OBJ. Packed children append their stable primitive/point name or id;
index fallback has the same temporal limitations as normal packed identity.
Material names use native material-builder paths; fallback materials use the
ROP path plus `/FallbackSurface`. Asset names default to the owning OBJ path.
Optional Cycles geometry tabs offer object/asset name overrides. A common
asset name combines several OBJs and packed children into one matte.

Names hash their UTF-8 bytes using MurmurHash3_32, seed zero, with the standard
uint32-to-float32 exponent repair. EXRs include matching `cryptomatte/<key>/name`,
`hash`, `conversion` and JSON `manifest` attributes. Names containing quotes,
backslashes, control characters or Unicode are JSON escaped correctly. Storage
is float32 with lossless ZIP compression. RG/BA are ID/coverage pairs, so these
planes bypass alpha processing, denoising and color transforms. The output
contract follows the [Cryptomatte specification](https://github.com/Psyop/Cryptomatte/blob/master/specification/cryptomatte_specification.pdf)
and the pinned engine's surface-data and pass-conversion implementations.

The panel displays selected ID layers as weighted false colors. Color, exposure,
gamma and alpha controls are disabled for these data planes. Selecting a layer
retains samples. Full manifests accompany completed frames and outputs only;
progressive tiles do not resend potentially large manifests. Name-set changes
rebuild manifests; camera, transform and shader-value changes retain them.
`session.output_metadata()` exposes completed-frame metadata on demand.

The focused fixture `tests/houdini_cryptomatte_smoke.py` checks independent hash
vectors, transparent and motion-blurred coverage, JSON escaping, live names,
level changes, denoising isolation, exact EXR ID bits, native batch and HIP reload.
It also loads the EXR through a native File COP in Raw mode and compares masks
extracted by Houdini's Cryptomatte COP, including manifest-based selection.
The heavy native GUI workflow checks ID previews and mixed/packed names alongside
normal AOVs. Coverage remains a development certification: volume-only masks,
refracted-object selection, depth truncation in many overlapping layers and large
manifests need their own feasibility and production tests. Volumes do not gain
surface mattes from their OBJ name.

## Object and material indices

Enable **Passes → Indexes → Object Index / Material Index**. Set **Pass Index**
in the optional geometry OBJ Cycles tab or
surface Cycles Material builder. The ROP Materials tab has **Fallback Pass
Index** for surfaces without an assigned material. Values are explicit integers
from 0–32767, with zero meaning unassigned. An OBJ index is inherited by its
mesh, curve, sphere-point and packed components. Analytic lights write emission
and continue the ray without writing surface index passes, even when visible.
Their emission does not replace an underlying surface's index. Use emissive
geometry when a luminous surface needs an index or Cryptomatte mask.

Outputs are unfiltered float32 `object_id.X` and `material_id.X`. They obey the
ROP's pass alpha threshold, bypass denoising and color processing, and preserve
exact integers in lossless EXRs. Volume-only rendering has no surface index.
The panel uses stable false colors for inspection, with zero black; EXRs and
native image planes retain the numeric IDs. Use Cryptomatte for anti-aliased,
transparent and motion-blurred masks. Index edits retain geometry and do not
rebuild a material graph or displacement.

`tests/houdini_index_passes_smoke.py` checks explicit values, mixed geometry,
packed inheritance, surface indices through analytic emission, opacity thresholds, live edits, malformed
values, progressive displays, denoising isolation, native batch and HIP reload.
The heavy SOP GUI test also switches to Object Index without restarting tracing.

## Shadow catcher

Add the optional Cycles tab to a receiving OBJ and enable **Shading → Shadow
Catcher Surfaces**. It applies to meshes, curves and sphere points, including
packed children; volume components keep their ordinary rendering. For plate
compositing, enable transparent film and **Passes → Other → Shadow Catcher**.
The `shadow_catcher.R/G/B` output is an RGB multiplier: one leaves the plate
unchanged, while shadows darken it and reflected contributions can increase it.
Beauty supplies the premultiplied foreground RGBA. In the linear render working
space, composite as `Beauty.rgb + (1 - Beauty.a) * Plate.rgb * ShadowCatcher.rgb`.
The pinned engine already maps Beauty to its foreground matte when catchers
exist; a duplicate named foreground plane would waste image storage.

With the separate pass disabled, Cycles approximates the shadow directly in
Beauty's alpha for quick viewing/compositing. Enabling the pass switches to the
explicit multiplier workflow. With no catcher objects, the multiplier is one
and Beauty remains ordinary. Toggling a catcher does not reconvert geometry.
The factor bypasses display color/exposure/gamma, has no alpha, and can use the
engine's supported pass denoising; Beauty retains its normal color policy.

Real lights participate in the catcher reference by default, following Blender's
world-light synchronization and the pinned engine's light visibility semantics.
**Cycles Light → Shadow Catcher Reference** and the ROP's **Environment → Shadow
Catcher Reference** can exclude added virtual lights or environments from that
reference without disabling their ordinary illumination. A reference with no
lighting uses Cycles' identity fallback; this does not produce a usable physical
shadow factor. Include the real plate lighting when setting up compositing.

`tests/houdini_shadow_catcher_smoke.py` compares direct-light compositing with an
ordinary physical ground render, exercises both workflows, denoising, last-
catcher removal, progressive/native planes, EXRs, batch and HIP reload. Colored
indirect reflections, light-group reconstruction with catchers, volume-only
catching and complex plate production scenes require additional certification.
The engine behavior is specified by the pinned Film, BufferParams, pass accessor
and shadow-catcher film conversion implementations, rather than a host-side
estimate of shadows.
