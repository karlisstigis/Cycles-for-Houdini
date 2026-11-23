# Native shader curves (inline native ramps)

Create **Cycles Float Curve**, **Cycles RGB Curves** or **Cycles Vector Curves**
inside a Cycles material/subnetwork. Float Curve is in Converter, RGB Curves in
Color and Vector Curves in Vector. Float Curve also replaces the retired
**Cycles Float Ramp** (2026-10-29).

## Authoring

Every channel is an inline native Houdini float ramp in the parameter pane:
Float Curve has one (**Curve**), RGB Curves has **Combined**, **R**, **G** and
**B**, Vector Curves has **X**, **Y** and **Z**. Multi-channel nodes show the
ramps on one compact tab strip. There is no separate editor window.

Layout, top to bottom (ports use the same order): **Value**, **Range**,
**Extension**, the channel ramp(s), **Factor**.

- Ramp position 0..1 spans **Range**: position 0 is the Range start, 1 its end.
  Defaults are 0..1, and -1..1 for Vector Curves.
- Ramp values are output values. HDR and negative values are kept, although the
  gadget's drawing may clip them.
- Each key's basis (Constant, Linear, Catmull-Rom, Monotone Cubic, Bezier,
  B-Spline, Hermite) sets the shape sampled by the host. A key's basis governs
  the interval to its right, following Houdini's ramp semantics. Ramps take
  1..32 keys, strictly increasing in [0,1] at float32 precision.
- **Extension** handles values outside Range. **Extrapolated** (the default)
  continues each ramp's slope at its first and last key. Linear uses the exact
  segment slope, Constant is flat, and smooth bases use a short one-sided
  difference of Houdini's ramp evaluation. **Horizontal** holds the end values,
  like a native ramp lookup.
- **Factor** blends the result with the original input, unclamped:
  `value + factor * (curve - value)`.
- RGB Curves applies Combined to each component, then the R, G and B ramps.
  Vector Curves maps components independently.
- New nodes start with identity ramps (two linear keys).

Ramp stops are ordinary animatable native parameters (`curve_value`,
`curve_c`/`curve_r`/`curve_g`/`curve_b`, `curve_x`/`curve_y`/`curve_z`; stops
`curve_x1pos`, `curve_x1value`, `curve_x1interp`, and so on).

### Replacing Float Ramp

Float Ramp mapped its Factor through a native ramp. Float Curve with
**Range 0..1**, **Extension Horizontal** and **Factor 1** maps **Value**
identically, including held ends outside 0..1. Factor's role changed: Float
Ramp's Factor is Float Curve's Value. The only difference at default settings is
Extension: new Float Curves default to Extrapolated, which continues the end
slopes instead of holding the end values for inputs outside 0..1.

## Rendering

The adapter bakes the authored channels into 257 table entries and evaluates
them with the stock Cycles curve nodes. This sets the resolution in the
authored range. Two nearby keys can fall between adjacent samples.
See the [materials guide](../../user/native-materials.md#float-and-color-ramps).

## Promotion and Material SOP overrides

Promote a channel ramp like any native ramp:

```python
cycles.promote_parameter(builder, vector_curves, 'curve_x', name='shape_x', label='X Curve')
```

Material SOP Local Overrides then use the native count/stop fields: `shape_x`,
`shape_x1pos`, `shape_x1value` and `shape_x1interp`. Range, Extension, Value and
Factor promote as ordinary parameters. See
[native material overrides](../../user/native-material-overrides.md).

## Recovery

Invalid stops (non-finite, coincident or out-of-range positions, more than 32
keys) and an invalid Range (non-finite, or start not below end) recover locally
with a `material.ramp_unavailable` diagnostic. Independent shading and
displacement are kept, and the warning clears after repair. A constant Factor of
0 skips the controls entirely, and Bypass passes Value through.

## Compatibility

Scenes saved before 2026-10-29 with Blender-style curve points, or with Float
Ramp nodes, are not migrated. Rebuild those nodes.
