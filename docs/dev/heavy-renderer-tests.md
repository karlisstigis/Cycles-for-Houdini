# Phase 1 renderer validation

These are owner-triggered tests. Run
`scripts/run-heavy-renderer-tests.ps1 -Suite furnace|bias|cross-backend|equal-time|nightly|everything`.
The four renderer suites are separately selectable. Nightly is separate. Everything explicitly
selects all five and must never be scheduled by a build or by CI. Reports and linear RGB arrays
go under `artifacts/heavy-renderer/<timestamp>/` by default.

The build under test is selected through the staged build manifest and matching Houdini package.
Bias additionally requires `-StockBuildDirectory` pointing to the output of
`scripts/build-stock-cycles.ps1`. That script exports the pristine Cycles commit pinned in
`source.lock.json`, compiles its standalone CPU CLI and writes an executable hash receipt. It
automatically fetches and SHA-checks its exact pinned Git LFS link libraries and runtime DLLs.
Stock is built with the fork's CPU ray-tracing backend: `-DWITH_CYCLES_EMBREE=ON -DWITH_EMBREE=ON`
with the bundle's Embree (the 5.3 kernel gate is `WITH_EMBREE`, see `cmake/cycles-embree.cmake`).
The script fails unless the CPU kernel's compile definitions contain `WITH_EMBREE` and
`EMBREE_MAJOR_VERSION`, and records the CLI's backend (`bvh_backend`: `embree4.dll` import and the
resolved DLL's SHA-256) in `stock-receipt.json`. BVH2 against Embree differs by last-bit
triangle-intersection results, which the bias rows would report as engine bias (night of
2026-11-15: glass_caustics failed at 0.0018 for this reason alone). The launcher, its `-Preflight`
mode included, therefore runs `scripts/cycles_cli_backend.py check` on both CLIs and refuses
unlike backends or different Embree runtimes; every captured image records its CLI's backend
and the analysis refuses an unlike stock/fork pair again. For default-preservation comparisons, both stock and fork CLIs read the **same** generated XML files,
with seed and standard integrator settings written before either render. The launcher verifies
the stock commit marker, receipt, both executable hashes, and per-image input hash equality.
The CLIs are isolated from Houdini adapter ABI differences. A missing or mismatched reference
is an error, not a pass. The bias launcher then captures the two feature-on/off pairs through
the staged Houdini worker before analysis. It rejects nonempty per-role output directories,
checks XML input, executable and image hashes, and requires a shared run identifier plus the
feature capture's fixture and runtime manifest hashes to match at analysis. The bias capture
uses the selected Houdini target's `hython` with separate stock-bundle and fork/Houdini DLL paths.
The XML inputs are `scene_cube_surface.xml`, `scene_monkey.xml` and `scene_caustics.xml`.
`scene_caustics.xml` places its point light with a `tfm` attribute on `<light>`, which the pinned
XML reader ignores (lights are object geometry; the object takes the enclosing `<transform>`), so
the light would sit at the origin inside the ground plane. Preparation moves every translation-only
light `tfm` into an enclosing `<transform translate>` (here `4 0 4`, as the example intends) and
records it as `light_placements` in `inputs.json`. The other two scenes need no correction; all
three still carry `<camera type="perspective">`, which the reader also ignores (the socket is
`camera_type`) but which equals the default.
With the earlier BVH2 stock build of pin `74e9d9e4` (`build/stock-cycles-74e9d9e4/stock-receipt.json`),
its 8-sample `diffuse_cube-701` smoke and the fork CLI smoke read the same XML (SHA-256
`c62842236ada62e3cadb43e0da62edddc7a66888205edd224b17d662ca14aab0`) and produced
bit-identical finite 96×64 linear RGB arrays under the target's `hython`.

| Suite | Authored cases | Reference and verdict |
|---|---|---|
| Furnace | White diffuse, glossy, glass, principled, refraction, transparent and translucent spheres inside a closed unit-emissive sphere; nested normal-incidence glass slabs; a homogeneous absorption grid; CPU, CUDA, OptiX | Unit-radiance expectation for lossless cases, normal-incidence slab transmission for nested interfaces, Beer–Lambert `exp(-9/8)` for absorption. Two independent seeds per backend calibrate regional signed RGB error; 0.02 absolute floor and 0.02 noise ceiling make high-noise cases inconclusive. Sheen, toon, subsurface and other absorbing closures still need separate references; the suite does not claim full BSDF coverage. |
| Bias | Three common XML scenes on pristine and fork defaults; packaged fork worker on a two-area-lamp courtyard with RIS on/off and CPU guiding on/off; eight matched seeds and 16,384 samples per image | Full-image bit identity is reported separately for default preservation. The maximum signed difference over sixteen regions and RGB channels uses all 256 within-seed label swaps, with p ≤ 0.01 and absolute error > 0.01 as failure criteria. Maximum paired standard error > 0.01 is inconclusive. Identical feature-on/off images are inconclusive because activation is unproven. The worker uses MIS-enabled area lamps and native light objects with shadow-catcher participation, which the standalone XML light parser does not expose. |
| Cross-backend | Authored Huang hair strands, a metallic glossy interior and a pillar courtyard at 4,096 samples and two seeds on CPU, CUDA, OptiX | Finite, shape-checked linear RGB with existing RMSE/SSIM helpers plus sixteen regional RGB means. Maximum relative regional error must be ≤ 0.02 plus three times same-backend relative seed noise, with a 0.03 radiance floor for dark regions. Relative seed noise > 0.02 is inconclusive. RMSE ≤ max(0.025, three times same-backend image noise) and SSIM ≥ 0.95 remain additional checks. A synthetic 20% energy mutation fails the regional gate. |
| Equal-time | OptiX: the audited artist particle RIS scene, a 25-area-light room and a broad glossy interior. CPU: Huang hair, the glossy interior and pillar courtyard. Ten independent seeds, 20 seconds of tracing per mode, 640×360 authored or 1280×720 artist-particle resolution | Across-seed pixel variance directly measures equal-time RMS noise, in five disjoint seed pairs per arm. Median wall times must differ by ≤ 2 seconds and each trace must stay within 18–24 seconds. The paired median RMS ratio and five paired losses/wins are recorded. A separate 32-sample OptiX RIS on/off contrast must exceed five times an off/off same-seed repeatability control with eligible MIS area lights in all three RIS scenes. |

The bias permutation p-value is exact only when the matched-seed differences are symmetric under the equal-estimator null. Equal expected radiance alone does not guarantee that condition for estimators with different, skewed sampling distributions. The 16,384-sample requirement and 0.01 paired-noise ceiling guard the comparison; reports retain all signed regional RGB residuals and the largest-effect coordinate. A passing or inconclusive result has finite detection power and does not prove unbiasedness. Review skewed or high-noise cases with additional independent seeds and convergence evidence before a release claim.

**RIS rule fixed before measurements:** A scene is an RIS loss when its median of five
paired RMS variance ratios exceeds 1.10 and at least four of five disjoint seed pairs
also lose by more than 10%. A win uses the reciprocal threshold and four paired wins.
A neutral scene has all five ratios within those reciprocal bounds; any other effect is
unresolved. RIS is marked for removal under K26 when at least two of three scenes lose,
no scene wins or is unresolved, and all OptiX timing and activity checks pass. Retain
requires all three scenes to be wins or well-bounded neutral. Mixed or unresolved evidence
is inconclusive. The runner records the decision but does not edit the fork. CPU guiding
timing and variance are reported independently and never enter the RIS release rule.

The artist particle HIP is the unchanged audit copy under
`artifacts/exec-audit-20261029/scenes/particles_rls.hiplc`, pinned to SHA-256
`a60fafbf32203da3dc77f99c9c2762aca889ee0e13e391ae5b1ec17a72d64eb7`. It and its
private RGB Image Iris JPG are not published as source. The suite fails clearly if either is
missing or altered. Use `-RisScene` and `-RisIrisImage` for relocated copies with the same
hashes. The short staged OptiX probe measured on/off RMS contrasts of 3.744, 0.0250 and
0.0400 on those three scenes, versus off/off repeats of at most 1.01e-7; this establishes
active estimator contrast within the tested runtime, without a full equal-time result.

The separate `tests/houdini_cpu_guiding_probe.py` is a focused staged-runtime check. It requires
`capabilities().path_guiding` and Image Iris, renders 256 CPU samples with guiding enabled,
and checks the worker trace for a valid trained OpenPGL field. This is intended to qualify the
packaged CPU path without triggering the four heavy suites.
