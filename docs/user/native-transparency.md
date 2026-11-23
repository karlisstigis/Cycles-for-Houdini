# Native film, texture coverage and additive emission

The native OBJ/SOP ROP exposes a **Transparent** header checkbox under
**Image Output → Film**, with **Transparent Glass** and **Roughness Threshold**
inside the section. Environment illumination and reflections
remain enabled when its camera coverage is hidden. Glass coverage is enabled
only with transparent film; its roughness threshold defaults to 0.1 on the ROP.
Cycles Transparent BSDF, Glass BSDF and Holdout have authored surface adapters.
Surface closures cannot feed a volume material.

Beauty and float32 EXR output retain associated RGBA. Filtered edges carry
fractional coverage; glass tint includes attenuation in that coverage. An
emission-only volume can have positive RGB with zero alpha. Working-space and
display conversions preserve those RGB values rather than multiplying them
away. Renderer alpha remains independent of an OCIO transform's fourth channel.
This follows the zero-alpha preservation behavior described in
[OpenImageIO's unpremult/repremult operations](https://openimageio.readthedocs.io/en/latest/imagebufalgo.html).

The panel offers RGBA, RGB and Alpha inspection plus a checkerboard. RGB
inspection reveals additive emission regardless of coverage. Alpha inspection
bypasses exposure and OCIO color processing. RGBA composites the image over the
viewer background or checker in scene-linear light before the display
transform, View(RGB + (1 - alpha) * background), as compositing applications do
with premultiplied images; additive RGB at zero alpha shows added to the
background. Exposure changes the image, not the background colour.
RGB, R, G and B with the ×A toggle off show the image over black, opaque:
View(exposure * RGB), the associated colour as Nuke's RGB view shows it (R, G
and B take one channel of that picture). Opaque pixels look the same as in
RGBA; additive emission at zero alpha shows as View(RGB). With ×A on they
blend over the background in display values, as before. Limitation: a snapshot or compare image keeps the composite made over
the background of the moment it was taken; after the background, checker or
theme changes, it is shown with the older display-space blend, and its JPEG
Save composites its displayed bytes on black. JPEG Save of the live image and
ROP JPEG output show the image over black composited in scene-linear light.

Texture color conversion now gives OCIO an RGB view of interleaved RGBA.
Patch 0013 fixes the reproduced input-opacity error: a valid colorspace matrix
previously changed alpha 0.375 to 0.046875. The derived-cache key is versioned
so previously transformed-coverage derivatives cannot be reused. Alpha data
channels remain artist-authored in uncached and cached CPU/OptiX paths.

The pinned Cycles Image Texture node unassociates Color when Alpha is used in
the contributing graph; unused Alpha leaves associated color. Channel-packed
inputs bypass that association. Straight inputs are premultiplied on loading,
so their RGB at alpha zero becomes zero. Associated input files may already
carry additive RGB at zero coverage and retain it. This describes audited
[pinned source behavior](https://github.com/blender/cycles/blob/a456b761034dda42c32eef9f4aae0fa5a5c9f604/src/scene/shader_nodes.cpp);
the Blender manual's summarized connection-dependent wording differs from this
implementation. Tests explicitly exercise both contributing graph states.

## Verification

- `houdini_transparency_smoke.py`: eleven groups per CPU/RTX 4090 device cover
  filtered coverage, environment illumination, closure mixing, white/tinted/rough
  glass, holdout, exact float32 EXR, additive volumes and retained source frames.
  Glass compares separately constructed headless Cycles scenes; diffuse
  illumination and the density-masked emission integral have analytic expectations.
- `houdini_texture_alpha_smoke.py`: three alpha conventions at 0, 0.375 and 1,
  both cache routes and both devices, with Alpha, Color-with-Alpha and Color-only
  graphs. The 108 renders compare color with independent OCIO RGB processing.
- `houdini_transparency_ipr_smoke.py`: actual native IPR/shared-panel tests pass
  on both devices, including film edits, RGB/Alpha inspection, additive volume
  pixel data, working-space replacement and shutdown.
- `houdini_transparency_batch_smoke.py`: eight native SOHO batch outputs cover
  animated film coverage on surface and volume fixtures. The sparse boundary
  uses the same 1024-sample convergence budget as its analytic headless test.

Evidence and preserved failure logs are in `artifacts/stage3/color/transparency/`.
The original alpha-changing-input and zero-alpha-display failures are retained
separately from fixture corrections: omitted default emission strength, float32
sample accumulation, default glass tint, an unmasked volume domain, per-device
glass state, unused/constant-folded Alpha dependencies and low-sample volume
integration. These corrections do not establish additional renderer defects.
The feature does not certify every opacity, transmission, filtering or compositor
combination, and does not finish the complete native color/output release gate.
