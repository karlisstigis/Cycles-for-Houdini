# Color Ramp in HSV and HSL mode (behaviour specification)

Status: behaviour description for a separately written replacement. Written 21 November 2026; revised the same day (textbook
colour model; only the constants the fixture needs are pinned).
Scope: `src/core/hue_ramp.cpp` (class `HueRamp`, declared in `src/core/hue_ramp.h`): the colour of a
Blender-style Color Ramp whose Color Mode is HSV or HSL, at a given factor. The RGB mode, the native
Houdini ramp bases, the 257-entry table and its lookup (`src/core/owned_ramp.cpp`) are not part of this
rewrite.

This document describes behaviour and mathematics only. Rules that exist only because Blender behaves that
way are marked **[interop]** and listed again in section 7.

## 1. Interface the rest of our code expects (keep it)

`src/core/hue_ramp.h` stays unchanged:

- `struct HueRampKey { float position; std::array<float, 3> color; }` (one stop: position and RGB).
- `HueRamp(std::span<const HueRampKey> keys, bool hsl, int direction)`: `hsl` false = HSV, true = HSL;
  `direction` 0 = Near, 1 = Far, 2 = Clockwise, 3 = Counter-Clockwise (the order of Blender's Python enum
  `ColorRamp.hue_interpolation`: `NEAR`, `FAR`, `CW`, `CCW`).
- `std::array<float, 3> evaluate(float position) const`: RGB at `position`.
- `std::span<const float> boundaries() const`: a list of positions. Nothing reads it at present. The stub
  keeps this project's own helper that adds, for each interval, the positions where the interpolated hue
  crosses a multiple of 1/6 and (HSL) where lightness crosses 1/2; call it with the interval's two hues
  after the arc choice of section 4, or leave the list as the stop positions only.
- Private members (`Stop`, `Span`, the vectors) may be changed freely.

The caller (`sample_owned_ramp` in `src/core/owned_ramp.cpp`, source kind 2) guarantees: 1 to 32 keys;
positions finite, in [0, 1] and non-decreasing (equal positions allowed); colours finite; `evaluate` is
called with positions already clamped to [0, 1], at i/256 for i = 0 ... 256 to build the table, and at
single factors for previews. The caller rejects a non-finite result.

Errors (this project's own rules, keep them):

- empty `keys`, more than 32 keys, or `direction` outside 0 ... 3: throw `std::invalid_argument`
  ("Malformed hue ramp ownership");
- any colour channel below zero: throw `RampSourceError` ("HSV/HSL Color Ramp requires non-negative RGB
  stops, as in Blender; RGB and LAB retain signed values"); this check is kept in the stub;
- a non-finite H, S or V/L after conversion: throw `RampSourceError` ("HSV/HSL Color Ramp conversion is
  non-finite for these stops").

Numeric type: float32 throughout. `hue_ramp.cpp` is compiled with `/fp:precise` on MSVC
(`CMakeLists.txt`); keep ordinary IEEE float semantics.

## 2. Behaviour in one paragraph

Each stop's RGB colour is converted to (H, S, V) or (H, S, L), with H as a fraction of a turn in [0, 1).
Between two neighbouring stops the three components are blended linearly in the factor; the hue is blended
along one of the two arcs of the colour wheel, chosen by the Hue Interpolation setting; the result is
converted back to RGB. Before the first stop and after the last stop the ramp holds the end stop's RGB
colour exactly. Manual (Color Ramp Widget,
https://docs.blender.org/manual/en/latest/interface/controls/templates/color_ramp.html, manual 5.3, read
21 November 2026): "HSV/HSL: Blends colors by first converting to HSV or HSL, mixing, then combining
again"; "Clockwise: Clockwise interpolation around the HSV/HSL wheel"; "Counter-Clockwise:
Counterclockwise around the HSV/HSL wheel"; "Near: Nearest route around the wheel"; "Far: Furthest route
around the wheel". In HSV/HSL mode Blender always blends linearly between stops; the RGB interpolation
setting (Ease, Cardinal, B-Spline, Constant) has no effect (the fixture generator checks this, section 6).

## 3. Converting a stop

### 3.1 HSL

Use Cycles' `rgb_to_hsl` (Apache-2.0, `third_party/cycles/src/util/color.h` lines 173-204; SPDX-
FileCopyrightText 2011-2022 Blender Foundation), already available to this file through
`#include "util/color.h"`. If the resulting H is >= 1, subtract 1 once.

### 3.2 HSV: the hexcone model

Use the standard hexcone model (A. R. Smith, "Color Gamut Transform Pairs", *Computer Graphics* 12(3),
SIGGRAPH 1978, pp. 12-19), in float32: V = max(R, G, B), C = V - min(R, G, B), S = C / V (0 when V = 0),
and, for C > 0, the hue as a fraction of a turn by the largest channel:

- red largest: H = (G - B) / (6C), plus 1 if that is negative;
- green largest: H = 1/3 + (B - R) / (6C);
- blue largest: H = 2/3 + (R - G) / (6C);
- C = 0 (grey): H = 0.

How the largest channel is chosen when two are equal, and whether the fraction is formed as
(difference) / (6C) or ((difference) / C) / 6, is free: both pass every fixture case (measured, section 6).
If H comes out >= 1, subtract 1.

One interoperability fact **[interop]**: the offset of the blue sector, "2/3", must be the float32 number
0x3F2AAAAA (0.66666662693...), that is the float32 result of 1 - float32(1/3), one unit in the last place
below the float32 value nearest 2/3 (0x3F2AAAAB). The other offsets are the float32 values nearest 1/3
(0x3EAAAAAB) and 1. Reason: with 0x3F2AAAAB a pure blue stop and a pure yellow stop are exactly half a turn
apart (0.6666667 - 0.16666667 = 0.5), with Blender's value they are 0.49999994 apart, and the Far rule of
section 4 then picks the other arc. Measured: with 0x3F2AAAAB the fixture cases `hsv_far_half_turn_offset`
and `hsv_far_duplicate_interior` come out with the wrong arc (error 1.0); with 0x3F2AAAAA all 112 cases are
right.

## 4. Choosing the arc between two stops

For each pair of neighbouring stops, left (lower position) with hue hL and right with hue hR (both in
[0, 1) after section 3), let d = hR - hL (float32). Choose the arc by adding one whole turn to one of the
two hues **[interop: the comparisons, including which side gets the turn and the strictness of each
test]**:

| Hue Interpolation | Rule |
|---|---|
| Near (0) | if d > 0.5: hL += 1; else if d < -0.5: hR += 1; else nothing |
| Far (1) | if d == 0 or -0.5 < d < 0: hR += 1; else if 0 < d < 0.5: hL += 1; else nothing |
| Clockwise (2) | if d < 0: hR += 1; else nothing |
| Counter-Clockwise (3) | if d > 0: hL += 1; else nothing |

Consequences, for orientation: a difference of exactly one half turn is not "more than half a turn" for
either rule, so neither Near nor Far adds a turn **[interop: how an exact half-turn tie is resolved]**;
Far with two equal hues makes a full turn; Clockwise means the hue never decreases from left to right,
Counter-Clockwise means it never increases.

## 5. Evaluating at a position x

1. If there is one stop, or x <= the first stop's position: return the first stop's RGB colour unchanged.
2. Let the right stop be the first stop whose position is greater than x. If there is none, return the
   last stop's RGB colour unchanged. (With equal positions this makes the left stop the last of the
   coincident group, and a jump happens exactly at that position.)
3. Otherwise, with left = the stop before it, positions pL < pR, hues hL', hR' after section 4, and the
   other two components (sL, vL) and (sR, vR) (v stands for V or L), the result is the linear blend of
   the two stops in (hue, s, v): with t = (x - pL) / (pR - pL), the weight of the right stop, and 1 - t
   that of the left stop,
   - h = (1 - t) hL' + t hR', reduced by 1 once if it is 1 or more; s = (1 - t) sL + t sR;
     v = (1 - t) vL + t vR;
   - converted back to RGB with Cycles' `hsv_to_rgb` (HSV) or `hsl_to_rgb` (HSL)
     (Apache-2.0, `third_party/cycles/src/util/color.h` lines 118-171 and 206-231).

How the weights and the weighted average are computed (which weight is formed first, the order of the
products and sums) is free: different orders of the same blend differ by a few units in the last place,
well inside the test tolerance (section 6). The back-conversion of Cycles differs from
Blender's own by at most 3.6e-7 on the fixture (measured).

## 6. Reference tests

These tests and fixtures judge the replacement. **They must stay as they are.**

- Fixtures `tests/fixtures/blender-hue-ramps.json` and `tests/fixtures/blender-hue-ramps-tables.json`,
  produced by `scripts/generate-blender-hue-ramps.py` inside the pinned Blender build `fbe6228777e7`
  (5.2.0 LTS) with `blender --background --factory-startup --python-exit-code 1 --python
  scripts/generate-blender-hue-ramps.py`. The script sets stops through the public Python API
  (`ColorRamp.elements`, `color_mode`, `hue_interpolation`, `interpolation`) and reads
  `ColorRamp.evaluate`. 112 cases: HSV and HSL, all four directions, 14 palettes (red-blue both ways, half
  turns, the hue seam, equal hues, grey stops, non-uniform spacing, coincident first/interior/last stops, a
  single stop, 32 stops, HDR values). The main file holds `ColorRamp.evaluate` at the factors
  (`colors`); the tables file holds what a 257-entry table built from `evaluate(i/256)` and interpolated
  linearly gives at the same factors (`scripts/blender_table_reference.py`, `sampled_rgba`).
- `tests/houdini_hue_ramp_smoke.py` (CPU and OptiX, catalogue `headless`): renders every case through the
  native Color Ramp node and compares the pixels with the tables file at `atol = rtol = 3e-5`
  (`tests/hue_ramp_fixture.py`, `inspect`); also checks local recovery, displacement and five final frames.
- `tests/core_hue_ramp_smoke.py` (catalogue `headless`, runs `ch_harness.exe --hue-ramp-contracts`,
  `src/harness/hue_ramp_contracts.h`): malformed sources are rejected with `std::invalid_argument`,
  negative or unusable colours give `RampSourceError`, all 2 x 4 space/direction combinations give finite
  values, coincident stops use the uniform table, at least 30 checks.
- Also built on `hue_ramp_fixture.py` or on HSV/HSL sources: `tests/houdini_color_ramp_rgba_smoke.py`,
  `tests/houdini_color_ramp_migration_smoke.py`, `tests/houdini_ramp_control_recovery_smoke.py`,
  `tests/houdini_ramp_contracts_smoke.py`; GUI (they open windows, so they are run by hand):
  `tests/houdini_hue_ramp_gui_smoke.py`, `tests/houdini_hue_ramp_native_gui_smoke.py`.
- Not a judge of this piece: the K33 gate (`tests/houdini_ramp_curve_blender_smoke.py`) imports Blender
  materials whose ramps carry Blender's own table, so `HueRamp` does not run there.

Measured agreement of this description: a throw-away numpy float32 script (not committed) written from
sections 3-5 (textbook sectors chosen by the largest channel, red first on ties) picks the right arc in
all 112 cases; the largest difference against `ColorRamp.evaluate` or the tables file is 3.6e-7 with the
fraction formed as difference / (6C) and 3.9e-7 with (difference / C) / 6. The test tolerance is 3e-5.

How to run on Windows, from a build of the commit to test (see `docs/dev/provenance/curve-mapping.md`
section 8.4 for the build and the device name):

```powershell
./scripts/run-hython.ps1 tests/houdini_hue_ramp_smoke.py --device CPU
./scripts/run-hython.ps1 tests/houdini_hue_ramp_smoke.py --device "CUDA_NVIDIA GeForce RTX 4090_0000:03:00_OptiX"
./scripts/run-tests.ps1 -Tests core_hue_ramp_smoke.py -BuildDirectory <build> -Artifacts <folder>
./scripts/run-hython.ps1 tests/houdini_color_ramp_rgba_smoke.py --device CPU
```

The third line runs the harness check `ch_harness.exe --hue-ramp-contracts`. Linux: the renders need hython and
the built renderer; the fixture comparison above can be repeated in plain Python 3 with numpy.

## 7. Interoperability facts (no basis other than "Blender does this")

| # | Fact | Pinned by |
|---|---|---|
| H1 | Blue-sector hue offset 0x3F2AAAAA (float32 of 1 - float32(1/3)), not float32(2/3) | `hsv_far_half_turn_offset`, `hsv_far_duplicate_interior` |
| H2 | Hue wrap: H >= 1 becomes H - 1, once, for stops and for blended hues | `hsv_*_hue_seam`, half-turn cases |
| H3 | Arc table of section 4: an exact half turn adds no turn for Near or Far; Far makes a full turn for equal hues | all `*_near_*`, `*_far_*`, `*_cw_*`, `*_ccw_*`; `*_far_equal_hue` |
| H4 | Outside the stops, and for one stop, the end stop's RGB is returned unconverted | `*_single`, `*_duplicate_first`, `*_duplicate_last`, `*_nonuniform` |
| H5 | Right stop = first stop with position > x (coincident stops jump at their position) | `*_duplicate_*` |
| H6 | HSV/HSL ignore the RGB interpolation setting and blend linearly | generator assertion |

## 8. Provenance

- The previous implementation (`hsv_components`, the arc choice and `HueRamp::evaluate` in
  `src/core/hue_ramp.cpp`, commit `a777b6c` and earlier) followed Blender's GPL-2.0-or-later
  `source/blender/blenlib/intern/math_color.cc` and `source/blender/blenkernel/intern/colorband.cc` (at
  `fbe6228777e7`) closely.
- It was removed in commit `e9fb7c281f35ef2043eef14d4f4d810bb5abd0b7` (title "Remove Blender-derived curve, hue
  and object-value code pending independent rewrite"). The hash is that of the private development branch; in a
  repository with new history it names the commit that removed the code.
- Commit hashes in this section are those of the private branch that integrated the removal and the rewrite on top of the main line (the side branches named here had other hashes for the same commits): removal `e9fb7c2`, base of the rewrite `a2708bc`, replacement `6fd3fb5` and `0c3d84a`.
- This description was written by an author who had read both the previous implementation and
  Blender's source, in order to state the behaviour completely. It contains no Blender code and no Blender
  internal names, but its author was not isolated from that code. This is not a clean-room process in the
  legal sense; provenance and code review still matter.
- The replacement was written from this description by an implementer (21 November 2026, on a separate branch), using
  only this description, the tests and fixtures, the rest of this project's code at `a2708bc`, Cycles' Apache-2.0 `third_party/cycles/src/util/color.h` and the textbooks named below. It did not open Blender's source (outside `intern/cycles`), the git history that shows the removed bodies, `src/core/shader_curves.cpp` or the main checkout's copies of the rewritten files, and saw no removed code by accident. This is not a legal clean room either.
  Method: Smith's hexcone (1978) for RGB to HSV, written fresh (largest channel picks the sector, red then green on
  ties, hue = difference / (6 C)); Cycles' `rgb_to_hsl`, `hsv_to_rgb`, `hsl_to_rgb` for the rest; ordinary convex
  linear interpolation (right weight (x - pL) / (pR - pL), left weight 1 minus that). Of section 7 it encodes H1
  (as a checked constant), H2, the arc table H3, H4 and H5. Measured (Linux, g++ 11.4, the real `hue_ramp.cpp`
  with the three Cycles functions): all 112 cases pick the right arc; largest difference 1.5e-6 against
  `ColorRamp.evaluate` and 1.4e-6 against the tables file (`hsv_far_hdr`; with the weight order of section 5 it
  would be 1.8e-7). With the blue offset set to 0x3F2AAAAB the two cases named in H1 fail with error 1.0.
  Windows (a build of the replacement, 21 November 2026): `houdini_hue_ramp_smoke.py` passed on CPU and OptiX,
  `--hue-ramp-contracts` passed (70 checks), and the RGBA, migration, control-recovery and ramp-contract tests
  passed on CPU.
- Review against Blender's source for copied structure or expression: a separate reviewer, 21 November
  2026, compared with Blender's `source/blender/blenlib/intern/math_color.cc` and
  `source/blender/blenkernel/intern/colorband.cc` at `fbe6228777e7` and with the removed implementation.
  Grade: no concern. Details: `docs/dev/provenance/review-2026-11-21.md`, section 4. Its optional
  suggestion (state the weighting of section 5 step 3 as a result, not a recipe) was applied in a
  follow-up change on 21 November 2026 (commit `8e39856`).

## Performance (21 November 2026)

Table construction (`prepare_owned_ramp`, 257 samples) timed against the previous implementation (commit
`03c6abf`, run as a black box) on Linux, g++ 11.4 -O2, standalone benchmark (`table_bench.cpp`, 3 alternating
rounds of 7 runs x 300 tables, median): as integrated, 2 and 8 stops equal, 32 stops 4-13 % slower (about
0.4 us per table). The extra time was the interval split points of `boundaries()`, which nothing reads; the
interface section allows the list to hold the stop positions only, so it now does. After the change: 2 stops
0.96-1.00, 8 stops 0.91-0.96, 32 stops 0.83-0.87 of the previous time; tables bit-identical to the rewrite as
integrated. Table times not measured with the Windows build (MSVC); with the changed Windows build
`core_hue_ramp_smoke.py`, `houdini_hue_ramp_smoke.py`, `houdini_rgb_ramp_smoke.py`, `houdini_color_ramp_rgba_smoke.py`,
`houdini_color_ramp_migration_smoke.py` and `houdini_ramp_curve_blender_smoke.py` pass.
