# Native ramp interpolation

**Cycles Float Curve / RGB Curves / Vector Curves** channel ramps (which replace the retired Float Ramp) and Native RGB/LAB **Cycles Color Ramp** support Houdini's Constant,
Linear, Catmull-Rom, Monotone Cubic, Bezier, B-Spline and Hermite bases. Each has
1..32 finite keys, strictly increasing in `[0,1]` at renderer float precision.
Endpoints hold outside the authored key range. A key's basis governs the interval
to its right, including exact constant-step boundaries.

Color ramps offer an explicit **Color Mode** menu: RGB, HSV, HSL or LAB.
RGB defaults to Blender Linear and offers all five Blender interpolation modes;
select Native (Per Stop) for the seven Houdini bases described here. See
[Blender RGB interpolation and whole RGBA promotion](native-blender-rgb-ramps.md).
HSV/HSL use Blender hue interpolation and linear stop blending; see
[hue modes and recovery](native-hue-ramps.md). This setting controls renderer
evaluation. The native ramp widget retains its
RGB swatch/editor representation; assigning a color type to a HOM ramp does not
reliably preserve that setting through this Houdini parameter API. Whole-ramp
promotion and requested-frame evaluation retain their existing native semantics.

K33 uses host-baked tables with 257 entries. Stock Cycles samples the table
in the shader. The host interpolation rules above describe the controls at
bake time. See the [materials guide](../../user/native-materials.md#float-and-color-ramps)
for the effect of table spacing on narrow features.

## Historical verification of the archived implementation

The original ramp milestone used host `20261018T214925629`, protocol 43 and patch
45, with five compiled artifacts and six kernels. CPU and each NVIDIA GPU
separately passed 55 image/recovery checks, five ordered output frames and 21
ownership/boundary/recovery checks. Both viewers passed 21 live states separately,
with a retained million-triangle prototype shared twelve times. Shutdown returned
owned/device allocations to zero. Shader, bypass, world and color regressions
passed in the same 13-run source-audited round.

A separate public-API numerical probe covered 196 cases and 2,352,000 comparisons:
all seven bases and alternating pairs, scalar/RGB HDR and positive HSV/LAB inputs.
The largest program had 294 intervals; maximum observed absolute error was
`5.48363e-6`. This is evidence for those cases, not a guarantee for arbitrary
controls. An intentionally difficult valid curve also verifies bounded local
recovery when capacity is exhausted.

Exact reports, sources and numerical-probe files are recorded in
`artifacts/ramp-interpolation-evidence-20261018.json`. The subsequent protocol-44
packed-field round rechecks the 55 CPU ramp cases and five output frames.

Blender's RGB/Float Curve editor adapters, its separate hue-direction/HSL
semantics, broader graph contexts and production-scale certification remain
separate work. Native ramp interpolation does not establish those capabilities.

## Blender shader curves, 2026-10-19

The RGB/Float Curve editor gap listed above is now implemented, together with
Vector Curves. These use their own Bezier controls and the pinned Cycles lookup
semantics; the native Houdini ramps and seven bases retain their existing
behavior. See [shader curves](native-shader-curves.md) for controls, limits,
recovery, reference generation and CPU/both-GPU/live-viewer evidence. Blender
Color Ramp HSL and hue-direction semantics are implemented in the later
[hue-mode milestone](native-hue-ramps.md). Alpha and a renderer-matched RGBA
editor follow in [their own increment](native-color-ramp-rgba.md). Global
Blender RGB interpolation semantics remain separate work.
