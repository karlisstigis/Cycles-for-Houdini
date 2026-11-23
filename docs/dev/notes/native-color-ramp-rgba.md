# Color Ramp: inline Color and Alpha ramps

The authoring controls described here still apply. The original rendering and
verification notes describe the archived evaluator. For the current 257-entry
table behavior, see the [materials guide](../../user/native-materials.md#float-and-color-ramps).

**Cycles Color Ramp** is edited in the parameter pane, like Float Ramp and
Float Curve. There is no separate editor window.

- **Color** is a native Houdini RGB ramp (`ramp`, stops `ramp#pos`, `ramp#c`,
  `ramp#interp`).
- **Alpha** is a native float ramp (`alpha_ramp`, stops `alpha_ramp#pos`,
  `alpha_ramp#value`, `alpha_ramp#interp`) with its own stops. New nodes are
  opaque (two stops at 1).
- Color and Alpha are separate outputs. Connecting Color to a surface does not
  apply opacity; connect Alpha to the shader's Alpha input or a Mix Shader
  factor.

`hou.Ramp` reads and writes both ramps directly (`node.parm('ramp')`,
`node.parm('alpha_ramp')`).

## Interpolation and preview

**Interpolation** (RGB Color Mode) defaults to **Native (Per Stop)** for new
nodes: each stop's Houdini basis applies to its interval on both ramps, and the
renderer evaluates all seven bases exactly as the gadgets draw them.

Linear, Constant, Ease, B-Spline and Cardinal are Blender's whole-ramp modes and
apply to Color and Alpha together; the per-stop basis menus are disabled. The
Houdini gadget cannot draw these modes, so for Ease, B-Spline and Cardinal the
inline gradient is only an approximation (it shows Houdini's interpolation).
Linear/Constant match it closely, except that Blender modes also accept
coincident stops.

**Color Mode** HSV/HSL blend Color along **Hue Interpolation** (Near, Far,
Clockwise, Counter-Clockwise); the Color gadget still draws RGB blending. LAB
uses native per-stop bases. Alpha never follows the hue path: it uses its own
per-stop bases, and all-Linear Alpha in HSV/HSL accepts coincident stops.
These notes are also in the parameter help.

Unusable controls recover per output: bad Color does not invalidate Alpha (or
Alpha-driven displacement) and vice versa.

## Promotion

`promote_parameter(builder, color_ramp, 'ramp', name='palette')` promotes the
whole Color Ramp: a collapsible panel with Color Mode, Hue Interpolation,
Interpolation and two native ramps, `palette` (Color) and `palette_alpha`
(Alpha), with their animation. The shader's `ramp` and `alpha_ramp` reference
them. Single stops (`ramp2c`, `alpha_ramp2value`) and `alpha_ramp` alone can be
promoted like any parameter. Material SOP Local Overrides address
`palette#pos/c/interp` and `palette_alpha#pos/value/interp` plus both counts;
see [native material overrides](../../user/native-material-overrides.md).

## Scenes saved before the inline ramps

Older builds stored RGBA in the Color stops (`ramp#c` with four components,
Alpha channel `ramp#ca`). Such scenes migrate automatically while loading:

- each stop's Alpha becomes an Alpha-ramp stop at the same position, with its
  value, keyframes or expression (`ramp2ca` -> `alpha_ramp2value`);
- animated positions are duplicated onto the Alpha stops;
- the Alpha stop basis is the Color stop's basis, or Linear for HSV/HSL (where
  legacy Alpha ignored bases); Blender Interpolation modes still apply to both;
- a shared Color Ramp reference `ch(".../ramp")` also shares `alpha_ramp`.

Color and Alpha evaluate identically after migration (checked by
`tests/houdini_color_ramp_migration_smoke.py` against the last RGBA build).
Saving writes the new layout. The retired "Edit Color Ramp" button value is
dropped.

A material promoted with the retired hidden RGBA bundle keeps rendering exactly
as before. Its saved **Edit Color Ramp** button now converts that bundle to
inline `palette`/`palette_alpha` ramps (undoable), or call
`cycles_houdini.ramp_promotion.convert_all_legacy()`. Material SOP overrides
naming `palette#ca` must then be renamed to `palette_alpha#value`.

The 2026-10-19 RGBA editor evidence remains under
`artifacts/verification/color-ramp-rgba-20261019/`.
