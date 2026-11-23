# Native Bezier and NURBS curves

Native OBJ/SOP rendering now samples open and closed Bezier/NURBS curve primitives
into owned Cycles strands. Polygon hair keeps its existing path. Closed polygons
remain meshes; unsupported primitive vertices are not rendered as loose points.
Primitive circles require a native Convert SOP to Bezier or NURBS curves.

## Controls and appearance

Use **Rendering > Geometry > Points and Curves > Bezier/NURBS Segments**
(`curve_segments`) on the Cycles ROP. The default is 16; the supported range is 1-256. Each nonzero native
knot span gets that many segments. Increase it for tighter bends or close views.
This is fixed curve sampling, independent of mesh adaptive subdivision. It does
not change the geometry shown in Houdini's modeling viewport.

The opt-in object Cycles tab has **Curves > Override Segments** and **Bezier/NURBS
Segments** (`cycles_curve_segments_override`, `cycles_curve_segments`; the labels were
"Override Sampling" and "Segments per Span" before November 16, 2026, the parameter
names did not change). Unchecked objects inherit the ROP. Sampling changes invalidate that
curve representation; polygon curves do not acquire an unnecessary quality key.
Existing curve shape, width, shader, ray visibility and motion controls continue
to apply. Quality participates in packed prototype compatibility.

Rational curves use their authored weights for positions. Width, Cd, UV and named
numeric shader fields follow Houdini's ordinary attribute interpolation. Vertex
fields retain precedence over point/primitive/detail fields. The default UV is
sampled at each curve root; named UV fields remain per key. Only shader-requested
custom fields are captured.

Point velocity extrapolates the control positions before rational evaluation;
it uses the geometric coefficients. Deformation mode samples the native curve
at shutter times using the normal fixed-correspondence checks. Generated sample
identities are internal. No user-authored id/name field is required. Missing or
unusable motion keeps requested-frame geometry and independent transform motion.

Two settings, side by side: **Bezier/NURBS Segments** decides how many straight
pieces each Bezier or NURBS span becomes before the curve reaches Cycles; polygon
curves (hair) skip it. **Curve Subdivisions** (`curve_subdivisions`, Blender's setting,
default 2) is Cycles' `SceneParams::hair_subdivisions`: at render time each curve
segment, from any source, is split into 2^n pieces (clamped to 1-16; on the CPU
Embree's tessellation rate, 2^n + 1) so Rounded Ribbons follow the curve. 3D Curves
and Linear 3D Curves do not change (measured on CPU and OptiX by
`tests/houdini_curve_subdivisions_smoke.py`). It is scene-wide; a change recreates the
render session.

## Recovery, ownership and updates

Invalid bases, non-finite/singular rational weights and unusable sampled positions
omit only the affected curve. Other curves and meshes remain visible. Bad width
uses the configured fallback. Bad shader fields retain the existing local color
diagnostic and normal/displacement branch recovery; they do not move geometry.
Repairing an input restores it during normal IPR and removes its warning.

The adapter checks knot ordering, control counts and the native basis before
evaluation. A single curve is capped at 1,048,576 sampled keys; exceeding it
reports a source diagnostic suggesting lower sampling or simpler geometry.
Malformed knot/control combinations are not treated as valid discontinuities.
Discontinuity splitting and arbitrary spline/surface families are not claimed.

Acquisition stays on Houdini's host thread. Sampling reads retained geometry in
background conversion without evaluating OPs or retaining host pointers in the
worker. Relevant position/weight/attribute changes refresh the curve; camera and
object-only movement reuse its geometry. Shared packed prototypes retain one
converted resource per compatible rendering state. Their diagnostics are emitted
once per resource/source pair and remain available during cache reuse.

Material-only binding changes also refresh the engine's primitive shader IDs.
Previously a newly created/removed recovery binding could leave hair pointing to
an obsolete shader until a geometry recook. The palette change now marks only
the relevant point/hair shader stream; valid/recovered shading switches without
reconverting the curve or point geometry.

## Scoped verification

Host `20261018T204113959`, Houdini 22.0.429/Python 3.13, worker protocol 43; five
compiled artifacts, 44 applied patches and six unchanged kernels.

- 36 independent Bernstein/Cox-de Boor position, width, color, UV, numeric-field
  and control-velocity checks, including rational/periodic/closed/multi-span
  curves; malformed basis recovery and 60 frozen concurrent conversions through
  source edits/deletion. All retained geometry leases retire.
- CPU, RTX 4090 OptiX and RTX 3080 Ti OptiX each pass 30 native-versus-polygon
  reference images, including thick/ribbon shapes and velocity/deformation
  comparisons across five frames; 29 recovery/control steps and five actual
  final output frames with diagnostics. Persistent Data uses one sequence session.
- A heavier native SOP prototype has 1,024 source curves, 41,984 sampled keys
  and 512 boxes. One hundred outer copies contain 200 nested packed containers;
  200 render objects share exactly two geometry resources and represent 102,400
  logical curves. Shape/weight edits, local failure/repair and cached warning
  retention pass on all three devices. Camera and one-instance transforms do
  not reconvert geometry or recook the heavy source.
- Cycles Viewer and native IPR each pass 29 Qt/HOM parameter-entry, control,
  repair and navigation steps, with one Render start, one worker and twelve
  retained behind-camera instances of a million-triangle prototype. Cycles
  canvas size stays stable; native displayed pixels match accepted frames.
- Focused regressions cover polygon hair, mixed/deforming geometry, packed
  deformation, shader fields, object/ROP controls, dense volumes, native world
  IPR and color IPR. Tracked geometry/device allocation reaches zero on close.
- A separate material-only regression performs 25 transitions per device on
  polygon hair, rational NURBS and points. Implicit volume assignments recover
  visibly, valid surfaces return without SOP edits, and geometry conversion/cook
  counts remain unchanged. Repeated valid states have stable tracked allocation.

The fixture captures show the colored circular curve, separate green curve and
green mesh. Native Qt screenshot toolbar artifacts remain a capture limitation;
image pixels are checked separately. These tests do not certify physical input,
RSS/VRAM behavior or production latency. Headless timings in the proof distinguish
capture, engine first image and total wait; they are not input-to-presentation
measurements. Mixed-device certification remains separate.

Source-specific proof is `artifacts/parametric-curves-evidence-20261018.json`;
terminal run reports, failed investigations and frozen source archives are indexed
under `artifacts/verification/parametric-curves-20261018/`. The main runner
registers all five headless curve/strand tests and both viewer workflows.

Artist fixtures are saved as
`artifacts/stage2/hair/parametric/parametric-recovery-cycles.hiplc` and
`parametric-recovery-native.hiplc`. Restart Houdini after rebuilding the plugin;
use one viewer at a time. These deliberately small images test changes and repairs,
not final image quality.

Analytic primitive/surface adapters, broader production curve coverage and release
certification remain open.
