# Blender RGB interpolation and whole Color Ramp promotion

The authoring controls described here still apply. The original rendering and
verification notes describe the archived evaluator. For the current 257-entry
table behavior, see the [materials guide](../../user/native-materials.md#float-and-color-ramps).

Select a **Cycles Color Ramp** with Color Mode RGB. **Interpolation** offers
Native (Per Stop, the default for new nodes), Linear, Constant, Ease, B-Spline
and Cardinal. A Blender mode applies to the inline Color and Alpha ramps
together; the inline gadgets cannot draw Ease, B-Spline or Cardinal and show
Houdini's interpolation instead (see [inline ramps](native-color-ramp-rgba.md)).

These modes follow Blender's stop interpolation, including nonuniform and
coincident positions, exact step boundaries, endpoint behavior and spline
clamping. Factor clamps to `[0,1]`. B-Spline/Cardinal extend through the outer
spans using repeated endpoint colors and clamp their interpolated components
to `[0,1]`; other RGB modes preserve HDR values. A single stop retains its
authored value. Blender is used to generate independent references, not as a
runtime dependency.

**Native (Per Stop)** retains Houdini's seven bases for RGB. LAB also uses
native per-stop bases. HSV/HSL retain Blender's hue-direction controls and
per-stop Alpha; their dormant RGB interpolation selection does not contribute.
See [inline Color and Alpha ramps](native-color-ramp-rgba.md).

## Promote the whole ramp

The existing promotion API now promotes the complete Color Ramp bundle:

```python
import cycles_houdini as cycles
cycles.promote_parameter(builder, color_ramp, 'ramp',
                         name='palette', label='Palette')
```

The builder receives a collapsible Palette panel containing Color Mode,
Interpolation, the applicable hue control and two inline native ramps:
`palette` (Color) and `palette_alpha` (Alpha), with all stop positions, values,
bases and animation. The shader's `ramp` and `alpha_ramp` reference them.
Nested shader subnetworks can promote the ramps outward.
Renaming the shader or material and saving/loading the HIP preserve references.
Literal channel references in HScript and Python expressions are rebased when
their channels move. Broader context-dependent promoted expressions remain
part of the production material audit.

Both promoted ramps are ordinary `hou.Ramp` parameters (`palette`,
`palette_alpha`); global controls are `palette_color_space`,
`palette_interpolation` and `palette_hue_interpolation`. A material promoted
before the inline ramps keeps its hidden RGBA bundle (`palette1ca` Alpha) and
renders unchanged; its Edit Color Ramp button converts it to the two ramps.

Material SOP Local Overrides can target these native count, position, color,
Alpha, basis and global-menu controls. A count increase resolves additional
stop fields through owned descriptors; it never expands the authored material's
channels. Native RGB/Float ramps and complete shader curve channels use the
same evaluator. Contributing numeric channel expressions use an explicit safe
subset, and unsupported fields recover locally. See
[native material overrides](../../user/native-material-overrides.md) for authoring names,
bounds and recovery behavior.

Color and Alpha retain independent recovery after promotion. Bad color does
not invalidate valid Alpha-driven displacement; bad Alpha does not invalidate
Color. A failing dependency actually used by displacement disables that branch
and retains geometry. Repairs during IPR restore the output and clear warnings.

## Scoped verification, 2026-10-19

Evidence is recorded in `artifacts/blender-rgb-ramp-evidence-20261019.json` and
`artifacts/verification/blender-rgb-ramp-20261019/index.json`, including the
preserved development failure that identified negative-Factor clamping.

- CPU, RTX 4090 OptiX and RTX 3080 Ti OptiX separately pass 148 image/recovery
  states and five ordered final frames. Sixty-five independent Blender 5.2.0
  LTS cases cover all five RGB modes, HDR/overshoot, coincident/end stops and
  one to 32 stops. Color, Alpha and the renderer-matched preview are checked.
- Both viewers separately pass 34 live states on the 4090: interpolation edits,
  promotion while rendering, gestures, alpha animation, frame changes, finite
  undo/redo, repairs, camera/object changes and renaming. One worker and an
  unrelated 1,048,576-triangle prototype shared twelve times are retained;
  its cook and geometry conversion counts remain unchanged.
- Storage checks cover animated RGBA, relative HScript/Python references,
  insertion/reordering/removal, owner/shader renaming, HIP reload and nested
  promotion. Existing native bases, Float Ramp promotion, hue/Alpha, source
  recovery and storage regressions pass.
- An isolated MSVC AddressSanitizer preparer passes 71 ownership/recovery
  contracts and 550,420 dense independent RGB/Alpha samples. Maximum observed
  absolute error is `2.26498e-6`; the largest program has 135 intervals.
  Houdini/worker integration and leak detection are outside that sanitizer
  scope. Staged runtime binaries are ordinary builds. Sessions close with
  zero tracked owned geometry/device allocations.

The existing 55 engine patches reproduce from pristine pinned sources. This
increment retains the existing kernel program and worker protocol 45.
Physical-input, monitor-change, long-running memory, mixed-device and public
release certification remain open.
