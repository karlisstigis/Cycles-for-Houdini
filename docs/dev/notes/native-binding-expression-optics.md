# Native binding, expression and optics contracts

The October 21 binding/volume/camera increment extends this earlier milestone with point-subject bindings, tapered volumes and continuous camera intrinsics. Current evidence is in [binding, tapered-volume and camera coverage](native-binding-volume-camera.md); the results below remain attached to their original runtimes.

This increment adds uniform whole-curve key bindings, containing-particle
binding composition, bounded numeric material expressions and continuous depth
of field. It keeps local source recovery in Cycles Viewer, native Houdini IPR
and ordered final rendering. The corrected runtime passes its selected final
matrix; the prior-runtime evidence below remains pinned separately.

## Curve and particle assignments

Native point/vertex `shop_materialpath` and `material_override` layers can
provide one binding for a complete curve. The adapter compares complete final
assignments at authored keys, including inherited layers and the existing
primitive stylesheet priority. Equal effective bindings are accepted on
polygon, sampled Bezier/NURBS and shared-point curves. A higher-priority material
switch can make differing lower-priority inputs irrelevant.

This does not interpolate closures or split curves into differently shaded
segments. Varying vertex bindings fall back to uniform point bindings; if those
also vary, use the primitive/base binding. Report the omitted owner and preserve
usable geometry. Per-key stylesheet rules remain unsupported and diagnosed.
Ordinary varying `Cd`, `uv` and supported named attributes retain their own
attribute paths; this boundary concerns material assignment and override layers.

A point referenced by overlapping particle primitives renders once. Conflicting
containing-primitive bindings keep the first owner with a diagnostic; explicit
point bindings retain their normal precedence. Unused palette entries and their
shader demands are pruned. Repair restores the intended result automatically.

Acquisition retains bounded continuation and the no-key/no-style fast path.
Native override budgets remain 4,096 effective combinations / 8 MiB per component;
curve recovery can omit vertex, point, primitive, then detail layers while
retaining base paths and valid lower-priority layers. The new curve-specific
capacity branch is not yet certified at its own exact population/byte boundary.

## Owned source expressions

The shared Material SOP/stylesheet evaluator supports literal `ch`, `chf`,
`cht`, direct string `chs`, numeric arithmetic, `$F/$FF/$T`, and the fixed
function allowlist in [native overrides](../../user/native-material-overrides.md#contributing-channel-expressions).
It evaluates at requested/shutter time without changing the timeline.
Explicit sampled dependencies, lazy first-query links and delete/recreate
repairs remain observed. Constant overrides replace unused source animation.

Known syntax, arity, domain, missing-channel and unsafe numeric failures omit
only contributing fields with `material.override_ignored`. Usable bindings,
independent shading and displacement remain active; inactive controls stay quiet.
No authored Python, arbitrary function, dynamic path or script is executed by
the owned substitution adapter. `chi` is not stock HScript in the pinned SDK.

Native destination Int rounding precedes Toggle/Menu and strict-bound clamps;
the final value must fit int32. Unsafe int64 conversion is rejected locally.
HScript `int()` truncates toward zero. Expressions are bounded by 4,096 UTF-8
bytes, 256 AST nodes and depth 32, with a 256-entry syntax cache. Native literal
layers retain 512 fields / 64 KiB / 4,096 AST nodes. Generated stylesheet layers
retain their separate 128-field / 64-KiB limit.

## Continuous depth of field

Protocol 50 / patch 0067 samples aperture size, focus distance, rotation and
ratio through the shutter, preserving requested-frame optics separately.
Invalid requested optics use a pinhole; invalid off-frame optics hold the
requested values. Discrete blade animation holds the requested count while
valid continuous optics remain active. Source diagnostics name the camera and
affected controls. Independent camera/object motion survives and repair clears
warnings. See [camera motion](native-camera-motion.md#current-continuous-depth-of-field).

Arbitrary animated projection/clipping/panoramic intrinsics, stereo and custom
lenses are not introduced. Malformed internal messages remain strict failures.

## Corrected-runtime verification

The staged runtime is `hosts/20261021T020131005/resources`, protocol 50,
with 67 engine patches. Its archived evidence (`artifacts/technical-features-final-evidence-20261021.json`)
contains 74 passing executions covering 63 test selections: forty feature cases,
23 regression/contract selections and eleven repeated qualification checks. Fifty-eight
passes belong to the corrected C runtime; D changes only the test harness protocol
assertion from 49 to 50. Exact renderer-source equality, timestamp-normalized native
PE comparisons and explicitly timestamp-normalized generated-asset comparisons
justify carrying those results forward; D supplies the five remaining
selections and eleven harness/asset rechecks. The feature matrix covers CPU, RTX 4090 OptiX, RTX 3080 Ti OptiX
and mixed GPUs, including 600 live viewer states and 52 final frames. The two
displacement-hash selections both execute CPU contracts, regardless of their
selection labels; they are not separate GPU hash attestations.

Surface-only palette expansion, swapping and contraction now retain completed
private displacement. The mesh-sharing contracts compare retained and fresh
triangle/subdivision images on CPU and RTX 4090, while actual height and normal
changes still rebuild. Cross-object sharing remains strict. Both viewers pass
the scoped stylesheet break/repair checks. This evidence establishes the selected
automated correctness checks, not production latency, physical input, prolonged
process/GPU memory or a clean-machine release certificate.

## Pinned prior-runtime evidence

The prior build manifest is
`f98aef19e4c410fa7df3e6acb74a85aac445a2d224f7ed2e8f0cb88c7280d9ad`.
Its forty-case feature milestone (`artifacts/technical-feature-b4-prior-runtime-milestone/feature-milestone.json`)
passed on CPU, RTX 4090 OptiX, RTX 3080 Ti OptiX and mixed GPUs. Per target:

- Core DOF: seven image comparisons and sixteen explicit input rejections.
- Native DOF: 22 checks / five frames; each viewer: twenty states.
- Geometry bindings: 27 checks / five frames; each viewer: 27 states.
- Material expressions: 54 checks / three frames; each viewer: 28 states.

Each executed source is archived. Viewers check actual pixels, warning repair,
retained worker/cook/geometry invariants and owned close counters. These are
controlled HOM/UI workflows, not physical-input or whole-process-memory proof.
Ten separate regression children passed before the eleventh exposed a
displacement-rebuild regression. That
failed parent and partial observations (`artifacts/technical-feature-b4-prior-runtime-milestone/partial-regression-observations.json`)
remain excluded from the forty-case count. A subsequent runtime correction
requires its own feature/regression rerun; these results do not certify it.

Expression references contain 320 numeric/time and seventy destination-control
SDK cases, including explicit negative rows. DOF expectations use independent
authored-equation pupil/exposure integration and static-camera quadrature.
Only the invalid-ratio fixture camera softens its local strict minimum before
session creation, allowing a real invalid value to reach recovery; production
templates are unchanged. Raw-value-only snapshots avoid priming lazy source
dependencies. Unsafe SDK integer-overflow sentinels are not reproduced.

## Remaining technical gates

- Refresh the internal package for this runtime and repeat relocated checks;
  earlier package results remain attached to their original binaries.
- Keep true varying key bindings, conflicting shared-particle owners and
  unsupported dynamic/scripted contexts explicit until deliberately represented.
- Finish incremental family/candidate maintenance, remaining field/body
  transfers and cold lighting work.
- Certify physical interaction, prolonged memory, defined preview budgets and
  clean-machine installation; complete redistribution review before publication.

Viewer layout is a separate later pass. Solaris, baking, deep output, arbitrary
LPEs, OSL and broader platforms remain outside the native milestone.
