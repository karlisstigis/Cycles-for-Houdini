# Color Ramp in RGB mode: Constant, Linear, Ease, B-Spline, Cardinal (behaviour specification)

Status: behaviour description for a separately written replacement. Written 21 November 2026.
Scope: the function `rgb_band` in `src/core/owned_ramp.cpp`: the value of a Blender-style Color Ramp in
RGB colour mode at a factor, for the five Blender interpolations. Everything else in that file (native
Houdini ramps through `UT_Ramp`, HSV/HSL through `HueRamp`, linear scalar stops, imported Blender tables,
the 257-entry table and its lookup) stays as it is.

Rules that exist only because Blender behaves that way and scenes must render the same are marked
**[interop]** and listed again in section 5.

## 1. Interface (keep it)

`Color rgb_band(std::span<const HueRampKey> keys, int method, float x)` in the anonymous namespace of
`src/core/owned_ramp.cpp`, where `Color` is `std::array<float, 3>` and `HueRampKey` is
`{float position; std::array<float, 3> color;}` (`src/core/hue_ramp.h`).

- `keys`: 1 to 32 stops, positions finite, in [0, 1], non-decreasing (equal positions allowed); colours
  finite and possibly outside [0, 1] (HDR, negative). The caller (`sample_owned_ramp`, source kind 5)
  calls it once for the Color ramp with RGB stops and once for the Alpha ramp with (a, a, a) stops.
- `method`: 0 Constant, 1 Linear, 2 Ease, 3 B-Spline, 4 Cardinal (the order of
  `RGB_INTERPOLATIONS` in `houdini/python3.13libs/cycles_houdini/materials/ramps.py`; Blender Python enum
  `ColorRamp.interpolation`: `CONSTANT`, `LINEAR`, `EASE`, `B_SPLINE`, `CARDINAL`).
- `x`: already clamped to [0, 1] by the caller. Clamping first gives the same values as Blender for
  factors outside [0, 1] (the fixture includes -0.25 and 1.25).
- Errors: a non-finite spline result throws `RampSourceError` ("RGB spline interpolation exceeded finite
  precision") (own rule; the caller also rejects non-finite results).
- The caller builds the 257-entry table at i/256 and interpolates it linearly, except for Constant, where
  it takes the entry below (own code, kept).

Numeric type: float32. The file is compiled with `/fp:precise` on MSVC. No operation order is pinned:
any ordinary float32 evaluation of the formulas below meets the tests (section 4).

## 2. The interval

Manual (Color Ramp Widget, https://docs.blender.org/manual/en/latest/interface/controls/templates/
color_ramp.html, manual 5.3, read 21 November 2026): "Color Interpolation: The interpolation method to use
across the ramp"; Linear "Uses a linear interpolation for the color stops".

- One stop: its colour, for every x.
- Constant, Linear, Ease: if x <= the first stop's position, the first stop's colour; if no stop has a
  position greater than x, the last stop's colour **[interop: ends are held, and an x exactly on the last
  group of stops gives the last colour]**.
- Otherwise the right stop R is the first stop whose position is greater than x, and the left stop L is
  the stop before it **[interop: with coincident stops the jump happens at their position, and the last
  of a coincident group is the left stop]**.
- B-Spline and Cardinal also use an interval outside the stops **[interop]**: before the first stop, L is
  a virtual stop at position 0 with the first stop's colour and R is the first stop; after the last stop,
  L is the last stop and R a virtual stop at position 1 with the last stop's colour.
- The local parameter is u = (x - pL) / (pR - pL), clamped to [0, 1]. If pL = pR (only possible with a
  virtual stop on top of a real one): u = 0 when R is the virtual stop at position 1, else u = 1
  **[interop]**.

## 3. The five interpolations

- **Constant**: the colour of L.
- **Linear**: C(u) = cL + u (cR - cL) (any algebraically equal form).
- **Ease**: Linear with u replaced by the smoothstep 3u^2 - 2u^3 (textbook Hermite smoothstep; for
  example K. Perlin, "An image synthesizer", SIGGRAPH 1985, or any graphics text) **[interop: that this is
  what "Ease" means]**.
- **B-Spline**: the uniform cubic B-spline segment with control values A, cL, cR, D, where A is the colour
  of the stop before L (L itself when L has no predecessor, or is virtual) and D the colour of the stop after R
  (R itself when R has no successor, or is virtual) **[interop: the end rule]**:
  C(u) = ((1-u)^3 A + (3u^3 - 6u^2 + 4) cL + (-3u^3 + 3u^2 + 3u + 1) cR + u^3 D) / 6.
  Textbook: the uniform cubic B-spline basis (R. H. Bartels, J. C. Beatty, B. A. Barsky, *An Introduction
  to Splines for Use in Computer Graphics and Geometric Modeling*, 1987; J. D. Foley et al., *Computer
  Graphics: Principles and Practice*, 2nd ed., 1990, section 11.2, parametric cubic curves; the subsection
  could not be confirmed from a reliable description of the book's contents on 21 November 2026, so only
  the section is cited). It does not pass through the stops.
- **Cardinal**: the cubic Hermite segment from cL to cR with tangents (per unit u)
  mL = k (cR - A) and mR = k (D - cL), with k = 0.71 **[interop: k = 0.71]** and A, D as for B-Spline:
  C(u) = (2u^3 - 3u^2 + 1) cL + (u^3 - 2u^2 + u) mL + (-2u^3 + 3u^2) cR + (u^3 - u^2) mR.
  Textbook: cardinal splines (Foley et al. 11.2; Catmull-Rom is the same with k = 0.5). It passes
  through the stops.
- B-Spline and Cardinal results are clamped to [0, 1] per channel, Alpha included **[interop]** (so HDR
  and negative stops are clipped in these two modes only). Constant, Linear and Ease are not clamped.

## 4. Reference tests

These tests and fixtures judge the replacement. **They must stay as they are.**

- Fixtures `tests/fixtures/blender-rgb-ramps.json` / `-tables.json` (65 cases: 5 interpolations x 13
  palettes: default, non-uniform, overshoot, HDR, coincident first/last/middle/zero/one, all stops at 0,
  all at 1, single, 32 stops; factors include -0.25, 1.25 and the float neighbours of every stop), made by
  `scripts/generate-blender-rgb-ramps.py`; and `tests/fixtures/blender-rgba-ramps.json` / `-tables.json`
  (60 cases, 4 of them RGB mode), made by `scripts/generate-blender-rgba-ramps.py`. Both run inside the
  pinned Blender `fbe6228777e7` (5.2.0 LTS) and read only the public `ColorRamp.evaluate`; the tables
  files hold what a 257-entry table from `evaluate(i/256)` gives at the same factors
  (`scripts/blender_table_reference.py`).
- `tests/houdini_rgb_ramp_smoke.py` (CPU and OptiX, catalogue `headless`): for every case,
  `_native.ramp_preview` (direct evaluation) against `colors`/`alphas` at `atol = rtol = 3e-5`, and
  rendered pixels against the tables file at the same tolerance.
- `tests/houdini_color_ramp_rgba_smoke.py`, `tests/houdini_color_ramp_migration_smoke.py` (headless),
  `tests/houdini_rgb_ramp_gui_smoke.py` (GUI, opens windows, so it is run by hand).
- `tests/core_hue_ramp_smoke.py` (harness `--hue-ramp-contracts`) checks the method range and that the
  RGB spline clamps the factor at zero.

Measured agreement of this description: a throw-away numpy float32 script (not committed) written from
sections 2-3, evaluating the formulas exactly as written above (left-to-right parameter, Hermite form),
gives at most 7.2e-7 (Ease) against `ColorRamp.evaluate` and 4.8e-7 against the tables, over all 65 + 4
RGB cases; Constant is exact. The test tolerance is 3e-5.

How to run on Windows, from a build of the commit to test (see `docs/dev/provenance/curve-mapping.md`
section 8.4 for the build and the device name):

```powershell
./scripts/run-hython.ps1 tests/houdini_rgb_ramp_smoke.py --device CPU
./scripts/run-hython.ps1 tests/houdini_rgb_ramp_smoke.py --device "CUDA_NVIDIA GeForce RTX 4090_0000:03:00_OptiX"
./scripts/run-hython.ps1 tests/houdini_color_ramp_rgba_smoke.py --device CPU
./scripts/run-hython.ps1 tests/houdini_color_ramp_migration_smoke.py --device CPU
./scripts/run-tests.ps1 -Tests core_hue_ramp_smoke.py -BuildDirectory <build> -Artifacts <folder>
```

Linux: the renders need hython; the fixture comparison can be repeated in plain Python with numpy.

## 5. Interoperability facts

| # | Fact | Pinned by |
|---|---|---|
| R1 | Constant/Linear/Ease hold the end colours outside the stops | `*_nonuniform`, `*_hdr` (factors -0.25, 1.25 and below/above the stops) |
| R2 | Right stop = first stop with position > x; coincident stops jump at their position | `*_duplicate_*`, `*_all_zero`, `*_all_one` |
| R3 | Splines use virtual end stops at 0 and 1 with the end colours; u for a zero-length interval | `b_spline_*`, `cardinal_*` with stops away from 0 and 1; `*_duplicate_zero/one` |
| R4 | Spline end neighbours: the end stop itself | `b_spline_*`, `cardinal_*` |
| R5 | Ease = smoothstep 3u^2 - 2u^3 | `ease_*` |
| R6 | Cardinal tangent factor 0.71 | `cardinal_*` |
| R7 | B-Spline and Cardinal clamp to [0, 1], Alpha included; the other modes do not | `*_overshoot`, `*_hdr` |

## 6. Provenance

- The previous implementation (`rgb_band` in `src/core/owned_ramp.cpp`, lines 21-68 at `a777b6c`; its file
  header claimed "no copied SDK implementation") followed Blender's GPL-2.0-or-later
  `source/blender/blenkernel/intern/colorband.cc` (RGB branch of the colour band evaluation) and
  `source/blender/blenkernel/intern/key.cc` (spline weights, the 0.71 factor) at `fbe6228777e7`
  closely: same end rules, same coincident-stop rule, same constants. An earlier audit of Blender-derived code had not listed
  it; the author of these descriptions found it while describing the HSV/HSL piece.
- It was removed in commit `b6b834c2c2043b8eff0cb38b400d3e6910ccace0` (title "Remove Blender-derived RGB Color
  Ramp interpolation pending independent rewrite").
- Commit hashes in this section are those of the private branch that integrated the removal and the rewrite on top of the main line (the side branches named here had other hashes for the same commits): removal `b6b834c`, base of the rewrite `a2708bc`, replacement `6fd3fb5` and `0c3d84a`.
- This description was written by an author who had read both the previous implementation and
  Blender's source. It contains no Blender code and no Blender internal names, but its author was not
  isolated from that code. This is not a clean-room process in the legal sense; provenance and code review
  still matter.
- The replacement was written from this description by an implementer (21 November 2026, on a separate branch), using
  only this description, the tests and fixtures, the rest of this project's code at `a2708bc`, Cycles' Apache-2.0 `third_party/cycles/src/util/color.h` and the textbooks named below. It did not open Blender's source (outside `intern/cycles`), the git history that shows the removed bodies, `src/core/shader_curves.cpp` or the main checkout's copies of the rewritten files, and saw no removed code by accident. This is not a legal clean room either.
  Method: one search for the first stop after x, then an explicit interval (real or virtual end stop) with its two
  outer neighbours; Linear as cL + t (cR - cL); Ease with Perlin's smoothstep; B-Spline and Cardinal both written
  as four weights of the control colours (uniform cubic B-spline basis, Bartels/Beatty/Barsky 1987 and Foley et
  al. 11.2.3, corrected to 11.2 on 21 November 2026, see section 3; cardinal spline as a cubic Hermite segment, Foley et al. 11.2, with k = 0.71), then a finiteness
  check and the [0, 1] clip. It encodes R1-R7. Measured (Linux, g++ 11.4, the real `rgb_band`): largest difference
  7.2e-7 against `ColorRamp.evaluate` and 4.8e-7 against the tables over the 65 RGB cases (colour and alpha) and
  the 4 RGB cases of the RGBA fixture; Constant exact. Windows (a build of the replacement, 21 November 2026):
  `houdini_rgb_ramp_smoke.py` passed on CPU and OptiX; the RGBA, migration and `--hue-ramp-contracts` tests
  passed (migration: largest preview and render error 7.2e-7).
- Review against Blender's source for copied structure or expression: a separate reviewer, 21 November
  2026, compared with Blender's `source/blender/blenkernel/intern/colorband.cc` and `key.cc` at
  `fbe6228777e7` and with the removed implementation. Grade: no concern (one citation to correct).
  Details: `docs/dev/provenance/review-2026-11-21.md`, section 5. The citation "Foley 11.2.3" was replaced
  by section 11.2 (the subsection could not be confirmed) in the code comment and in section 3 in a
  follow-up change on 21 November 2026 (commit `8e39856`).

## Performance (21 November 2026)

Table construction timed against the previous implementation (commit `03c6abf`, black box) on Linux, g++ 11.4
-O2 (`table_bench.cpp`, 3 alternating rounds of 7 runs x 300 tables, median): as integrated, geometric mean
1.02 of the previous time, but 2-stop ramps up to 1.41 (Constant) and 1.22 (Cardinal) slower. `rgb_band` now
returns Constant, Linear and Ease directly from the two stops around x, and the spline modes address the four
control colours by pointer instead of copying them (same formulas, same order of operations). After the change:
0.75-1.02 of the previous time (all 15 cases; the 1.02 is 2-stop Constant, 1.52 vs 1.56 us, within run-to-run
spread); tables bit-identical to the rewrite as integrated. Table times not measured with the Windows build (MSVC); with the changed Windows build
`core_hue_ramp_smoke.py`, `houdini_hue_ramp_smoke.py`, `houdini_rgb_ramp_smoke.py`, `houdini_color_ramp_rgba_smoke.py`,
`houdini_color_ramp_migration_smoke.py` and `houdini_ramp_curve_blender_smoke.py` pass.
