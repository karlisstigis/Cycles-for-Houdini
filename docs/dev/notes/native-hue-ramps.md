# Color Ramp hue modes and local control recovery

The authoring controls described here still apply. The original rendering and
verification notes describe the archived evaluator. For the current 257-entry
table behavior, see the [materials guide](../../user/native-materials.md#float-and-color-ramps).

Cycles Color Ramp has **Color Mode** choices RGB, HSV, HSL and LAB. HSV/HSL
expose **Hue Interpolation**: Near, Far, Clockwise and Counter-Clockwise. As in
the pinned [Blender color-band evaluator](https://github.com/blender/blender/blob/fbe6228777e7/source/blender/blenkernel/intern/colorband.cc),
these spaces blend stops linearly; the per-key native interpolation control is
disabled and ignored. RGB/LAB and Float Ramp retain their seven Houdini bases.
The hue control is hidden and ignored in RGB/LAB. A connected Factor disables
its unused parameter default.

Hue ramps accept 1..32 finite, nondecreasing stops in `[0,1]`, including
coincident stops. At the first stop's exact position the first color wins;
immediately after coincident stops the following interval is used. Far makes
a full hue turn when both hues agree. HDR nonnegative colors are supported
where their hue conversion is finite. Negative HSV/HSL colors and singular
HSL conversions recover locally; RGB/LAB retain their native signed controls.

Color Ramp alpha and its renderer-matched RGBA editor are implemented in the
[subsequent increment](native-color-ramp-rgba.md). Blender's global RGB
interpolation semantics remain separate work; full Color Ramp parity is not
claimed. Newly authored HSV uses Blender hue rules;
private version-one owned recipes retain their earlier native HSV evaluation.

## Preparation and recovery

The host captures owned controls; the persistent worker prepares the existing
bounded polynomial program. No Blender runtime, host node, callback or sampled
UI image reaches the render thread. Hue conversion and color-wheel boundaries
are prepared outside the UI thread. RGB/LAB native spline evaluation retains
the public Houdini math API. The hue helper uses standard color mathematics
and Cycles' Apache-licensed color utilities.

The existing program format stays unchanged: at most 2,048 intervals, 24 words
per interval, with exact endpoint/knot values. Hue preparation checks its
quantized program against float evaluation using `4e-6` absolute and `2e-6`
relative tolerances; native spline preparation retains `2e-6` for both. This
avoids fitting float-rounding stair steps into thousands of tiny intervals.
Independent Blender image/numerical acceptance remains `3e-5` absolute/relative.
These are measured approximation checks, not a universal mathematical proof.

Known host control failures have a typed, bounded fault recipe: count/arity,
nonfinite values, unsupported ordering/domain, interpolation, Color Mode,
Hue Interpolation or color tuple shape. The worker resolves that node locally.
Independent color, normal and displacement branches remain usable. Dependent
color uses diagnostic shading; dependent displacement is omitted rather than
receiving magenta heights. World failures use the configured background.
Repairing the controls restores the graph and clears accepted diagnostics.
Malformed recipe headers, fault ownership and private programs still fail
explicitly. Disconnecting a field consumer can change captured field layout;
this does not imply a displacement rebuild.

Patch 55 also fixes scalar Attribute output: repeated scalar components are
no longer averaged again. That averaging could change their last float bit,
selecting the wrong ramp side or changing a White Noise hash. Scalar values
and derivatives retain exact equal components; genuinely varying vectors keep
their ordinary conversion. See [constant attributes](native-constant-attributes.md).

## Scoped verification, 2026-10-19

The ordinary native build uses host `20261019T144558161`, worker protocol 45,
55 reproduced engine patches, six GPU kernels and five compiled artifacts.
Seventeen passing source-audited runs are indexed alongside five preserved
failed development runs in `artifacts/verification/hue-ramps-20261019/index.json`.
The consolidated report is `artifacts/hue-ramp-evidence-20261019.json`.

- CPU, RTX 4090 OptiX and RTX 3080 Ti OptiX each pass 126 hue/precision/recovery
  checks, five world checks and five ordered final frames. References cover
  all four directions in HSV/HSL, gray/HDR colors, half turns, hue seams,
  coincident stops and 1..32 stops. Independent position/normal passes and
  displacement rebuild counters verify branch-local recovery.
- A further 15 checks per device cover scalar/RGB host-control faults, excess
  stops, disconnected/connected Factor defaults and displacement-only repair.
- Both viewers pass 24 live states separately on the 4090. Native parameter
  edits, source repair, camera/object navigation and renaming retain a
  1,048,576-triangle prototype shared twelve times at one cook and unchanged
  geometry conversions. One viewer is open at a time. Both close with zero
  tracked owned geometry/device allocations. Native IPR pixels are checked
  independently of Qt screen grabs.
- The pinned Blender 5.2.0 LTS public API fixture (`fbe6228777e7`) reproduces
  byte-for-byte. A denser independent reference covers 474,208 RGB samples;
  the largest prepared hue program uses 288 intervals and observed maximum
  absolute error is `5.60284e-6`.
- Forty-nine strict ownership/recovery checks pass. The same checks and dense
  reference pass an isolated MSVC AddressSanitizer preparer. Houdini, worker
  integration and leak detection are outside that sanitizer scope; the normal
  staged binaries are not instrumented.
- Native ramps (41 checks/five frames), native ramp contracts (21), constant
  attributes (42 checks/five frames on each device), shader input states,
  adaptive layouts and bypass/node-color regressions pass.

This is development evidence. Physical mouse latency, monitor changes,
prolonged memory behavior, mixed devices and a reproducible public release
remain production certification work.

Blender RGB interpolation and whole-RGBA promotion are now covered in
[their verified increment](native-blender-rgb-ramps.md).
