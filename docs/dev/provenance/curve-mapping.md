# Curve mapping for Float Curve, RGB Curves and Vector Curves (behaviour specification)

Status: behaviour description for a separately written replacement. Written 21 November 2026; revised the same day (direct
evaluation with a tolerance instead of a float32 recipe; who reaches this code).
Scope: the per-channel curve of a Blender CurveMapping, as used by the "version 1" shader-curve source
in `src/core/shader_curves.cpp` (functions `build_channel` and `Channel::evaluate`). Everything else in
that file (source parsing, version 2 and 3 sources, the combination of channels into one table) is not
part of this rewrite and stays as it is.

This document describes behaviour and mathematics only. It is written so that the replacement can be
implemented without reading Blender's source or the previous implementation. Where a rule exists only
because Blender behaves that way and scenes must render the same, it is marked **[interop]** and is listed
again in section 9.

## 1. Where the piece sits

A Blender Float Curve, RGB Curves or Vector Curves node stores a CurveMapping: one curve per channel (1
for Float, 4 for RGB in the order R, G, B, Combined, 3 for Vector in the order X, Y, Z). Each curve is a list
of control points (x, y) with a handle type per point, plus three mapping-wide settings: the clipping
rectangle's X range (`clip_min_x`, `clip_max_x`), and the extension mode (`extend`: `HORIZONTAL` or
`EXTRAPOLATED`). Python API: `bpy.types.CurveMapping`, `bpy.types.CurveMap`, `bpy.types.CurveMapPoint`
(https://docs.blender.org/api/current/bpy.types.CurveMapping.html, API 5.3, read 21 November 2026).

### 1.0 Who reaches this code today

The version 1 source (authored Bezier points) is built in only two places:

- `blend_materials.py` (Blender material import, `curve()`), and only for a CurveMapping record that has
  no `table` key. The Blender-side exporter (`blend_material_export.py`) has written that table, baked
  through Blender's own public `CurveMapping.evaluate`, since commit `152d08d` (10 November 2026); with a
  table the importer stores a version 3 source and this code is not used. So version 1 is reached by
  records exported before 10 November 2026, and by Houdini curve nodes imported before then: the importer
  saves the source in the node's user data (`cycles_blender_curve`), so it lives on in saved .hip files
  until the artist edits that curve.
- `scripts/generate-blender-ramp-curve-gate.py` (developer tool) writes version 1 `.source` files next to
  its reference renders.

Curves authored in Houdini (the native Float Curve, RGB Curves and Vector Curves nodes, created or edited
there) never use it: they are lowered as version 2 sources (native Houdini ramps,
`materials/shader_curves.py`). No current test reaches version 1 (section 8).

If the path were removed instead of rewritten: an affected node would no longer render its imported
Blender curve (it would show the local recovery colour with a diagnostic, or, if the importer's fallback
were changed, use the native ramp approximation that the importer also writes, which is close to but not
equal to Blender's curve). The artist's fix is to re-import the material from its .blend file (Import
Blend or the asset browser): the current exporter then bakes Blender's own table (version 3), which
matches Blender exactly. Removal would also need a small change in `blend_materials.py` (refuse or
convert records without a table) and in the gate generator.

### 1.1 Interface the rest of our code expects (keep it)

In `src/core/shader_curves.cpp`, anonymous namespace:

- `struct XY { float x, y; }` with `+`, `-`, `* float`, `/ float`, `length` and `finite` helpers (generic,
  kept in the stub; use them or not).
- `struct Point { XY p, left, right; int handle; }`: one authored point. `p` is the position,
  `handle` is 0 = Auto, 1 = Auto Clamped, 2 = Vector. `left` and `right` arrive empty; the builder may use
  them for the point's handles or ignore them.
- `Channel build_channel(std::vector<Point> points, float domain_min, float domain_max, bool extend)`:
  prepares one channel. `points` arrive in authored order (the caller has checked that every position is
  finite and every handle is 0, 1 or 2, and that there are 2 to 64 points). `domain_min`/`domain_max` are
  the mapping's `clip_min_x`/`clip_max_x` (finite, `domain_min < domain_max`). `extend` is true for
  `EXTRAPOLATED`.
- `struct Channel` with `float evaluate(float x) const`: the channel's value at any finite `x`. The
  other members of `Channel` are free.
- Failures that come from the artist's data throw `ShaderCurveSourceError` (declared in
  `src/core/shader_curves.h`); the renderer then shows the documented local recovery colour for that node.
  Use it for: two points with the same X after sorting ("finite, distinct X positions"), a chord of an
  Auto or Auto Clamped point whose float32 length is zero (distinct X positions closer than about 1e-22,
  whose squares underflow), an end handle of zero or non-finite length, a non-finite or non-positive table
  step, a Combined curve value that is NaN (it becomes the input of the R, G and B curves), and any
  non-finite sample or table value. These checks are this project's own rules (not Blender's) and should
  stay.
- The caller then builds the node's table from `evaluate` as described in section 7. That part stays.

Numeric type: float32 (`float`) throughout, as Blender stores and evaluates these curves in float32. The
file is compiled with `/fp:precise` on MSVC (`src/CMakeLists.txt`). No order of operations is pinned: the
result is judged with a tolerance (section 8.1).

## 2. Ordering

Sort the points by X (ascending). After sorting, X must be strictly increasing; otherwise throw
`ShaderCurveSourceError`. Below, the sorted points are P0 ... P(n-1), n >= 2, Pi = (xi, yi).

Basis: Blender keeps CurveMap points sorted by X, and a mapping curve "can't have overhang: each X value
must correspond to exactly one Y value" (Blender manual, Curve Widget,
https://docs.blender.org/manual/en/latest/interface/controls/templates/curve.html, manual 5.3, read
21 November 2026).

## 3. Handles of each point

Each point gets a left handle Li and a right handle Ri (points in the plane). They define the cubic Bezier
segments of section 5. Compute them in three passes: 3.1 for all points, then 3.2 (Auto Clamped
adjustment, part of the same per-point computation), then 3.3 (end correction) after all points are done.

### 3.1 Neighbours, chords, and the two rules

For point Pi use its neighbours P(i-1) and P(i+1). At the ends a missing neighbour is replaced by the
mirror image of the existing one through the point: before P0 use 2*P0 - P1; after P(n-1) use
2*P(n-1) - P(n-2). **[interop]**

Let a = Pi - P(i-1) (the incoming chord) and b = P(i+1) - Pi (the outgoing chord), with Euclidean lengths
|a| and |b|.

- **Vector** (handle 2): Li = Pi - a/3 and Ri = Pi + b/3 **[interop: the handle length 1/3 of the
  chord]**. The handles point one third of the way along the chords, which makes each segment between two
  Vector points a straight line (standard property of a cubic Bezier whose control points are equally
  spaced on a line; see any CAGD text, for example G. Farin, *Curves and Surfaces for CAGD*, 5th ed., 2002,
  ch. 4-5). Manual: "Vector Handle: Results in straight lines and a sharp corner." The straight line alone
  does not fix the length: any two handles on the chord give it; Blender's handles lie at one third,
  which also makes the segment's parameter run uniformly along it, and the 32 samples of 5.2 depend on
  that.
- **Auto** and **Auto Clamped** (handles 0 and 1): the tangent direction is the sum of the two unit chord
  vectors, t = a/|a| + b/|b| (the bisector of the angle between the chords). Let k = 2.5614 * |t|
  **[interop: the factor 2.5614]**. Then Li = Pi - t * (|a| / k) and Ri = Pi + t * (|b| / k). So each handle
  is parallel to t and its length is the adjacent chord length divided by 2.5614 (for |t| = 2, that is the
  chord divided by 5.1228). Manual: "Auto Handle: Results in a smooth curve without the need to manually set
  up handles." With strictly increasing X, t is never zero; if you add a guard for |t| = 0, leave the handles
  undefined only in that impossible case.

All arithmetic in float32; the order of operations is free (section 8.3 gives the measured effect).

### 3.2 Auto Clamped adjustment (interior points only)

For an Auto Clamped point that has two real neighbours (not the first and not the last point), adjust only
the Y coordinates of the two handles computed in 3.1. Let u = y(i-1) - yi and v = y(i+1) - yi.

- If u and v are both <= 0, or both >= 0 (the point is a local extremum or a flat spot, ties included):
  set both handle Y values to yi (horizontal tangent).
- Otherwise the curve passes through the point monotonically. If u <= 0 (rising): the left handle's Y
  becomes max(y(i-1), its Y) and the right handle's Y becomes min(y(i+1), its Y). If u > 0 (falling): the
  left handle's Y becomes min(y(i-1), its Y) and the right handle's Y becomes max(y(i+1), its Y).

So no handle overshoots the neighbouring point's height. Manual: "Auto Clamped Handle: Like Auto Handle,
but also prevents overshoot." The exact rule above (extremum test with ties, clamp to the neighbours' Y) is
**[interop]**. End points of type Auto Clamped are not adjusted.

### 3.3 End correction for Auto end points (only when n > 2)

When there are more than two points, an end point of type **Auto** (not Auto Clamped, not Vector) re-aims
its handles at the nearest handle of its neighbour instead of along the mirrored chord. **[interop]**

- First point P0: let h = |R0 - P0| (its right handle's length from 3.1). Let q be the left handle L1 of
  P1 (as computed in 3.1 and 3.2), with its X replaced by max(q.x, x0). Let d = q - P0. If |d| is greater
  than the float32 machine epsilon (2^-23): scale d to length h, i.e. d' = d * (h / |d|), and set
  R0 = d' + P0 and L0 = P0 - d'. Otherwise leave P0's handles as they are.
- Last point P(n-1): the same with the roles mirrored: h = |L(n-1) - P(n-1)|; q = R(n-2) with its X
  replaced by min(q.x, x(n-1)); d = q - P(n-1); if |d| > 2^-23: d' = d * (h / |d|), L(n-1) = d' + P(n-1),
  R(n-1) = P(n-1) - d'.

For n = 2 there is no end correction.

## 4. Extension directions

After section 3 (and before any change made in section 5), the two end directions are unit vectors:

- e_in = (L0 - P0) / |L0 - P0| (points backwards from the first point along its left handle),
- e_out = (P(n-1) - R(n-1)) / |P(n-1) - R(n-1)| (points backwards from the last point, opposite its
  right handle).

If either length is zero or not finite, throw `ShaderCurveSourceError` (own rule).

Only the slope e.y / e.x of these vectors matters (section 6), except when e.x is exactly zero.

## 5. The sampled polyline

Each pair of consecutive points (A = P(j-1), B = Pj) is joined by a cubic Bezier segment with control
points A, H1 = R(j-1), H2 = Lj, B (standard cubic Bezier: B(s) = (1-s)^3 A + 3(1-s)^2 s H1 +
3(1-s) s^2 H2 + s^3 B for s in [0, 1]; the Bernstein form, Farin, 5th ed., ch. 5, or any graphics text).

### 5.1 Keeping X monotone in each segment

Before sampling a segment, make sure its handles do not reach past each other in X. Let d1 = A - H1 and
d2 = B - H2, s = |d1.x| + |d2.x| and w = B.x - A.x. If s > w (strictly), scale both handle offsets by
f = w / s: H1 = A - f * d1 and H2 = B - f * d2. (If s is zero there is nothing to do.) This keeps x(s)
monotone, so each segment is a function y(x). The proportional scaling rule is **[interop]**. This change
applies only to the segment being sampled; it does not change the e_in/e_out of section 4 (those use L0
and R(n-1), which no segment correction touches).

### 5.2 The rendered curve is a polyline through 32 samples per segment

Blender does not render the exact Bezier curve. It replaces each segment by the 32 points at the
parameter values s = k/31, k = 0 ... 31 (both ends included), joined by straight lines, and builds its
table from that polyline **[interop: 32 samples per segment at equal parameter steps, linear between
them]**. Concatenate the samples of all segments in order, keeping both copies of each shared end point
(they have the same X, which matters in 6.2). The difference matters: using the exact curve y(x) instead
of the polyline changes table values by up to 0.06 (case `float_nonuniform`, a steep segment) and by about
1e-3 elsewhere (measured, section 8.3).

Evaluate the Bezier segment at those parameter values with any textbook method of your choice
(Bernstein form, de Casteljau's algorithm, or Horner's rule on the power form; for example G. Farin,
*Curves and Surfaces for CAGD*, 5th ed., 2002: de Casteljau's algorithm in ch. 4, section 4.2; the
Bernstein form in ch. 5. Chapter numbers checked on 21 November 2026 against the publisher's table of
contents, https://shop.elsevier.com/books/curves-and-surfaces-for-cagd/farin/978-1-55860-737-8, and the
5th-edition catalogue records of the Texas A&M and Max Planck Digital Library catalogues; the author's
own web page, farinhansford.com/books/cagd/toc.html, shows an older chapter layout under the 5th-edition
title, in which de Casteljau's algorithm is chapter 3). Blender computes the 32 points by repeated float32
addition (forward differencing), which accumulates rounding; that is the only reason a direct evaluation differs
from the fixture, by at most 1.5e-5 relative to max(1, |value|) (measured, section 8.3).

## 6. The 257-entry table and evaluation

### 6.1 Table range

Let t_min = min(domain_min, x0) and t_max = max(domain_max, x(n-1)) (the clipping X range widened to the
points). Let step = (t_max - t_min) / 256 (in float32 this equals multiplying by 1/256 exactly). If step is
not finite or not positive, throw `ShaderCurveSourceError` (own rule). The table has 257 entries
**[interop: 256 intervals, 257 entries]** at X positions X_a = t_min + step * a, a = 0 ... 256 (multiply,
then add).

### 6.2 Table values

Let Q0 ... Q(m-1) be the polyline of 5.2 (X non-decreasing). The table entry at X_a is the value at X_a of
the polyline continued beyond its ends by the extension of 6.4, with the end samples snapped:

- X_a < Q0.x: Q0.y if Q0.x - X_a <= 1e-6, otherwise E_before(X_a) with end point Q0
  **[interop: tolerance 1e-6]**;
- Q0.x <= X_a < Q(m-1).x: the straight-line interpolation in X between L, the last polyline point with
  L.x <= X_a, and U, the first with U.x > X_a (where shared end points coincide, L is the later copy);
- X_a >= Q(m-1).x: Q(m-1).y if X_a - Q(m-1).x <= 1e-6, otherwise E_after(X_a) with end point Q(m-1).

The tolerance keeps a steep or vertical end handle from being extrapolated right at the end point. Any
non-finite table value throws `ShaderCurveSourceError` (own rule). How the points L and U are found is
free (both X_a and the polyline increase, so one forward pass suffices).

### 6.3 Evaluate at x

`Channel::evaluate(x)` is the piecewise linear function through the 257 table points (X_a, table[a]),
continued by the extension of 6.4 from the table's own end entries. With r = (x - t_min) / step
(dividing; see the last case):

- r < 0 (that is, x < X_0): E_before(x) with end point (X_0, table[0]);
- 0 <= r <= 256: table[i] + (table[i+1] - table[i]) * (r - i) with i = floor(r), and table[256] at
  r = 256;
- r > 256: E_after(x) with end point (X_256, table[256]). In float32, r formed by dividing makes r > 256
  imply x >= X_256, so the extension is always taken on its own side of the end point. (With the
  reciprocal, an x within rounding of X_256 can fall on either side; that changes nothing for a sloped end
  handle, but with a vertical one it switches between table[256] and the fixed step of 6.4.)

x is any value except NaN (an infinite x falls under the first or the last case).

### 6.4 Extension

Manual: "Controls how the curve is extended before the first point and after the last point. Extend
Horizontal: Causes the curve to 'go flat.' Extend Extrapolated: Causes the curve to maintain its
direction." The exact rule **[interop]**: beyond an end point (ex, ey), the curve is

- with Horizontal, the constant ey;
- with Extrapolated, the straight line through the end point along the end handle,
  E(x) = ey + u.y * (x - ex) / u.x, where u is the end's unit direction pointing away from the curve:
  u = e_in before the first point and u = -e_out after the last (section 4; the slope u.y / u.x is the
  same for u and -u);
- with Extrapolated and a vertical end handle (u.x exactly 0), the constant ey + u.y * 10000
  **[interop: 10000]**, a fixed step along the handle (before the first point ey + e_in.y * 10000, after
  the last ey - e_out.y * 10000). A vertical end handle does occur: the end correction of 3.3 can make it
  vertical; the fixture's `*_auto_extrapolated` cases contain one.

E_before and E_after denote this function for the first and the last end point. Which end applies is
known from where x lies (6.2, 6.3); the extension is never asked for a point between the two ends.

## 7. What the caller does with the channels (unchanged, for orientation)

`prepare_shader_curve` (kept) forms the node table exactly as Blender's Apache-2.0 Cycles exporter does
(Blender v5.3.0 `intern/cycles/blender/util.h`, `curvemap_minmax_curve`, `curvemapping_minmax`,
`curvemapping_float_to_array`, `curvemapping_color_to_array`, lines 149-277; `intern/cycles/blender/
shader.cpp` lines 368-410; SPDX-License-Identifier Apache-2.0): lo = smallest first-point X over all
channels, hi = largest last-point X over all channels (the clipping range is not used here); 257 rows at
t = lo + (i / 256) * (hi - lo); Float: the curve's value; Vector: each channel at t; RGB: each of R, G, B
evaluated at the Combined curve's value at t. The stock Cycles nodes then interpolate that table and
extrapolate linearly beyond it when `extrapolate` is set (Cycles kernel `third_party/cycles/src/kernel/svm/
ramp.h`, Apache-2.0).

## 8. Reference tests

These tests and fixtures judge the replacement. **They must stay as they are.**

### 8.1 Fixture: `tests/fixtures/blender-shader-curves.json`

- Produced by `scripts/generate-blender-shader-curves.py`, run inside the pinned Blender build
  `fbe6228777e7` (5.2.0 LTS) with `blender --background --factory-startup --python-exit-code 1 --python
  scripts/generate-blender-shader-curves.py`. The script only sets points through the public Python API
  (`CurveMapping.initialize`, `CurveMap.points.new`, `handle_type`, `extend`, `clip_min_x/max_x`,
  `update`) and reads values with `CurveMapping.evaluate`.
- 27 cases: Float, RGB and Vector; Auto, Auto Clamped and Vector handles; both extension modes;
  identity; mixed handle types with non-uniform spacing; points outside the clipping range ("hdr_domain").
  Each case has the authored points per channel, `minimum`/`maximum` (lo/hi of section 7) and a 257-row
  `table`, where row i was evaluated at x = minimum + i/256 * (maximum - minimum) computed in float64 by
  the script and then rounded to float32 by Blender's API.
- **No current test reads this fixture.** (It was checked by a curve-preparer harness at the time it was
  made, 19 October 2026, which no longer exists in the tree.) The live tests below only reach the version 1
  path indirectly or not at all. A possible addition: a new pure test that builds version 1 sources from this fixture and
  compares `cycles_houdini._native.shader_curve_table(source, kind)` with `table` is the natural judge; it
  needs hython (the `_native` module). Recommended acceptance: `|ours - fixture| <= 2e-5 * max(1,
  |fixture|)` per value (numpy `assert_allclose(..., atol=2e-5, rtol=2e-5)`). Why that is enough and
  safe: the measured worst case of a direct evaluation is 1.51e-5 on that scale (section 8.3), so 2e-5
  leaves a margin without hiding a wrong rule (a wrong handle, sample count or extension rule changes
  values by 1e-3 or more); and 2e-5 is about 1/200 of one 8-bit display step (1/255) and below the
  precision of a half-float near 1 (about 5e-4), so no difference can be seen in a render. The table is
  linearly interpolated afterwards, which does not amplify the error; only far extrapolation multiplies
  it by the distance, which the relative term covers.

### 8.2 Tests that run the shader-curve code

- `tests/houdini_ramp_curve_blender_smoke.py` (K33 gate, CPU and OptiX; fixture
  `tests/fixtures/blender-ramp-curve-gate.json`, made by `scripts/generate-blender-ramp-curve-gate.py`
  from real Blender renders): imports Blender materials and compares rendered pixels with Blender's at
  3e-8 (`tests/ramp_curve_parity_gate.py`). Its curve cases carry Blender's own table (version 3), so they
  do not run `build_channel`; they guard the surrounding code.
- `tests/houdini_shader_curves_smoke.py` (CPU and OptiX): native-ramp curves (version 2). Does not run
  `build_channel`.
- `tests/houdini_pybind_front_contract.py`: argument checking of `shader_curve_table`.

### 8.3 Measured agreement of this description with the fixture

Throw-away numpy float32 scripts (not committed) written from sections 2-6 and 7, over all 27 cases
(20817 values):

| Variant | Largest difference, relative to max(1, value) |
|---|---|
| direct evaluation of the samples (de Casteljau), all other free choices of this document taken the "other" way (dividing instead of multiplying, extension from t_max instead of X_256) | 1.51e-5 |
| Blender's own float32 forward differencing (for comparison only) | 2.7e-7 (bit-equal with the fixture's X positions) |
| exact curve y(x) instead of the 32-sample polyline (wrong) | 0.06 absolute (`float_nonuniform`) |

### 8.4 How to run

Windows, from a build of the commit to test (`./scripts/build.ps1`; a full build takes about an hour; pass
`-BuildDirectory <build>` to `run-hython.ps1` for a build in another folder):

```powershell
./scripts/run-hython.ps1 tests/houdini_ramp_curve_blender_smoke.py --device CPU
./scripts/run-hython.ps1 tests/houdini_ramp_curve_blender_smoke.py --device "CUDA_NVIDIA GeForce RTX 4090_0000:03:00_OptiX"
./scripts/run-hython.ps1 tests/houdini_shader_curves_smoke.py --device CPU
./scripts/run-hython.ps1 tests/houdini_pybind_front_contract.py
```

The OptiX device name is the development machine's; use the name of your own GPU. The K33 gate needs the saved
`.blend` files under `artifacts/ramp-curve-k33` (made by the gate generator). Linux: none of these can run (they
need hython and the built renderer); the fixture comparison of 8.3 can be repeated in plain Python.

## 9. Interoperability facts (no basis other than "Blender does this")

| # | Fact | Pinned by |
|---|---|---|
| C1 | Missing neighbour of an end point = mirror of the other neighbour through the point | fixture, all Auto cases |
| C2 | Auto handle length = chord length / (2.5614 * |sum of unit chords|) | fixture, Auto and Auto Clamped cases |
| C3 | Auto Clamped: extremum test with ties (both <= 0 or both >= 0), clamp of handle Y to the neighbours' Y, interior points only | fixture, `*_auto_clamped_*`, `*_nonuniform` |
| C4 | End correction of Auto end points for n > 2 (aim at the neighbour's near handle, X clipped, keep length, epsilon 2^-23) | fixture, `*_auto_*`, `*_nonuniform` |
| C5 | Handle overlap correction by proportional scaling when the X offsets sum to more than the segment width | fixture, `*_nonuniform` (0.2 and 0.205) |
| C6 | The rendered curve is the polyline through 32 samples per segment (s = k/31), shared end samples kept | fixture, all cases (exact curve differs by up to 0.06) |
| C7 | Table: 257 entries over [min(clip_min_x, x0), max(clip_max_x, x_last)] | fixture `*_hdr_domain` |
| C8 | Table lookup: end-sample tolerance 1e-6; coincident samples take the right one; linear in X otherwise | fixture |
| C9 | Evaluate: outside the table use the extension from the table's end entries | fixture (extrapolated cases) |
| C10 | Extension: Horizontal holds the end value; Extrapolated follows the end handle's slope; vertical end handle gives +/-10000 * e.y | fixture `*_extrapolated`, `*_hdr_domain` |
| C11 | Vector handles lie one third of the way along the chords to the neighbours | fixture `*_vector_*`, `*_nonuniform`, `*_hdr_domain` |

Not covered by this description (not used by our sources): Blender's wrapping option for cyclic curves
(`use_wrapping`, used by Hue Correct), clipping of the curve values (`use_clip`), and the "Free" and
"Aligned" handle types of profile curves.

## 10. Provenance

- The previous implementation (`build_channel` and `Channel` in `src/core/shader_curves.cpp`, commit
  `a777b6c` and earlier) followed Blender's GPL-2.0-or-later `source/blender/blenkernel/intern/
  colortools.cc` and `curve.cc` (at `fbe6228777e7`) closely: same constants and same branch structure.
- It was removed in commit `e9fb7c281f35ef2043eef14d4f4d810bb5abd0b7` (title "Remove Blender-derived curve, hue
  and object-value code pending independent rewrite"). The hash is that of the private development branch; in a
  repository with new history it names the commit that removed the code.
- Commit hashes in this section are those of the private branch that integrated the removal and the rewrite on top of the main line (the side branches named here had other hashes for the same commits): removal `e9fb7c2`, base of the rewrite `a2708bc`, replacement `bb71f80` and `74c1c56`.
- This description was written by an author who had read both the previous implementation and
  Blender's source, in order to state the behaviour completely. It contains no Blender code and no Blender
  internal names, but its author was not isolated from that code. This is not a clean-room process in the
  legal sense; provenance and code review still matter.
- The replacement was written from this description by an implementer who worked only from the material listed
  here, 21 November 2026, on a separate branch from the base commit `a2708bc` (commit `bb71f80` adds this paragraph; files `src/core/shader_curves.cpp`,
  new test `tests/houdini_curve_mapping_table_smoke.py`). The implementer used only this document, the fixture
  `tests/fixtures/blender-shader-curves.json`, this project's own code at `a2708bc` (the stub file and its
  header, the `shader_curve_table` binding, the version 1 source layout written by `blend_materials.py`,
  existing tests and the catalogue scripts) and G. Farin, *Curves and Surfaces for CAGD*, 5th ed., ch. 4.
  The implementer did not open Blender's source (no `source/blender/...`, no mirror, no web search for it), did not open
  the previous implementation (no `git show`/`log -p`/`diff`/`blame` of the old bodies, not the main
  checkout's copy of the file, not other working copies), and did not open Apache-2.0 Cycles source for
  this piece either. Method: the handle rules of section 3 and the table and extension rules of section 6
  as stated; the 32 samples per segment are computed directly with de Casteljau's algorithm (Farin, ch. 4),
  in float32, not by forward differencing. Free choices taken: division by the step in `evaluate`;
  extension outside the table from the table's own end entries (X_256 = t_min + 256 * step); "within 1e-6"
  read as `<=`. Measured against the fixture (all 27 cases, 20817 values): largest difference 1.51e-5
  relative to max(1, |value|) (`rgb_auto_extrapolated`, row 256 of R: 0.700015 against 0.7), the same in a
  Linux g++ build of the file and in the Windows run of `tests/houdini_curve_mapping_table_smoke.py`; the
  K33 gate (CPU and OptiX), `tests/houdini_shader_curves_smoke.py` and
  `tests/houdini_pybind_front_contract.py` passed on Windows.
- Review against Blender's source for copied structure or expression: a separate reviewer, 21 November
  2026, compared with Blender's `source/blender/blenkernel/intern/colortools.cc` and `curve.cc` at
  `fbe6228777e7` and with the removed implementation. Grade: minor (cosmetic changes, none changing the
  computed values). Details: `docs/dev/provenance/review-2026-11-21.md`, section 3. The cosmetic follow-ups
  were applied in a follow-up change on 21 November 2026: commit `fee73dc` (the extension
  takes the end chosen by its caller and applies one formula, with no dummy value; a zero float32 chord is
  a `ShaderCurveSourceError`; Farin's chapters) and commit `df9cc37` (this description: sections 3.1,
  5, 5.2, 6.2-6.4, 8.4, 9 and 10). These leave the computed values unchanged: bit-identical tables for the
  27 fixture cases (Linux g++, and on Windows the same digest of all 27 tables before and after, two
  runs) and for 199810 of 200000 random sources on Linux; the 190 others are
  degenerate sources with points closer than about 1e-22, which now give the source error instead of a
  table. The follow-up's check of Farin found de Casteljau's algorithm in chapter 4 of the 5th edition, not chapter 3
  (see the editorial note in section 3 (d) of the review). The later performance change (below) moves
  values in the last bits only.
- Performance (the project's rule that a replacement must not be slower; follow-up change, 21 November 2026,
  commit `d63d246`). Measured: the rewrite as integrated built a table in 1.04 to 1.24 times the time of
  the code it replaced (`03c6abf`, run as a black box, not read), the most for curves with many points
  (Linux, g++ -O2, repeated alternating runs). Phase timers showed that sampling the 32 points per segment
  took two thirds of a 64-point table. Changed: each segment's X and Y are rewritten once in the power
  basis and the 32 samples evaluated by Horner's rule at fixed parameters (the Bernstein form, Farin,
  5th ed., ch. 5; Horner's rule, D. E. Knuth, *The Art of Computer Programming*, vol. 2, section 4.6.4),
  with the end samples set to the end points, into separate X and Y arrays that the table lookup walks
  forward once. Evaluation keeps dividing by the step (section 6.3). Result (Linux): 0.73 (64 points) to 0.94
  (the fixture cases) of the old code's time, geometric mean 0.90 over the source set of that
  measurement; fixture worst deviation unchanged, 1.508e-5. On random sources the result stays as close to the
  old code's tables as the de Casteljau version was.
  Windows (MSVC, through `cycles_houdini._native.shader_curve_table`, the same 27 fixture cases and nine
  synthetic curves of 4, 16 and 64 points, median of 7 rounds of 400 calls per curve, one job per build):
  each call costs about 225-260 microseconds, mostly the binding's own conversion of the source and the
  result, so the call cannot resolve differences of a few microseconds in the table itself and the Linux
  numbers above are the measured basis. Only the 64-point RGB and Vector curves show a difference
  larger than the run-to-run spread: old code 256.0 and 256.6 (RGB, two runs), 249.4 and 249.3 (Vector);
  as integrated 270.7 and 260.5; after this change 258.8 and 249.4 microseconds per call (two runs on
  the old build, one as integrated, one after this change).
