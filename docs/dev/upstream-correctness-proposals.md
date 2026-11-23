# Local correctness proposal queue

These source-backed drafts are prepared for local review. Nothing has been sent
externally. They identify concrete extraction boundaries and missing upstream
reproducers; they do not label existing smoke tests as completed upstream
qualification. All exact patch bytes, files, fixtures and hashes are in
patches/inventory.json. Retain accepted patch ordering during extraction.

| Proposed subject | Carried patches and defect | Minimal upstream reproduction and acceptance |
|---|---|---|
| Correct normals for nonuniformly transformed point spheres | 0003 uses the normal transform appropriate to an ellipsoid. | Compare a scaled point sphere against equivalent explicit ellipsoid geometry under a directional light; assert normals and Beauty on CPU/Embree/OptiX, with uniform-scale control. Related fixture: houdini_points_smoke.py. |
| Preserve packed float3 face-varying subdivision color | 0009 enables packed reads and the smooth evaluator rather than treating packed storage as padded vectors. | Subdivide a mesh with distinct corner float3 values and compare each channel against scalar interpolation; test smooth/linear modes. Related fixture: houdini_subdivision_smoke.py. |
| Refresh all object motion records after buffer layout changes | 0023/0026 fix variable-step motion offsets and normal stride; 0027 fixes linear subdivision motion normals. | Change motion step counts on the first of two objects while retaining the second, then assert second-object transforms and normals at multiple shutter times. Separate host offset/stride fixes from subdivision interpolation. Related fixture: houdini_velocity_normal_smoke.py. |
| Preserve panorama inverse branches and motion seams | 0043/0044 select the configured angular branch and wrap motion by the actual full-turn pixel period. | Round-trip directions for partial/offset longitude ranges, near-pole samples and seam crossings; assert signed motion passes for a moving camera/object. Related fixture: houdini_panorama_smoke.py. |
| Preserve the full interval for uniform camera shutter sampling | 0042/0066 use endpoint knots rather than contracting the CDF around midpoints. | Empty/constant shutter curves must sample the full open-close interval with expected mean; nonuniform curve and endpoint-evaluation controls must stay valid. Related fixture: houdini_camera_sampling_gui_smoke.py, which needs a portable non-GUI test before upstream submission. |
| Reject subdivision integer count overflow before allocation | 0075 checks signed additions in split/subpatch construction. | Force near-INT_MAX vertex/triangle counts without allocating huge buffers, assert clean error and safe retained-session reuse; test just-below-limit controls. Related fixture: core_subdivision_count_overflow_smoke.py. |
| Refresh retained native Gaussian quaternion and SH attributes on relocation | 0081 forces copies after shared buffer reallocation/layout changes. | Insert/remove unrelated objects/attributes while retaining Gaussian data; compare quaternion orientation and SH radiance to forced rebuilds. Related fixture: houdini_blender53_gsplats_smoke.py. |
| Preserve independent light-pass fractions at float precision | 0083 replaces independent half-rounded diffuse/glossy fractions in main/shadow GPU state with PackedSpectrum. | A zero-transmission glossy/diffuse scene must reconstruct Beauty without a fabricated transmission remainder; compare CPU/OptiX, RLS on/off, main/shadow paths, and GPU state bytes/performance. Related fixture: houdini_selective_rls_smoke.py; isolate the generic pass case from RLS before proposal. |
| Avoid quadratic object-volume overlap flag queries | 0087 replaces the all-object/all-volume-object bounds search with a conservative spatial broad phase and exact leaf predicates. | Compare every query against the original ordered brute-force predicate, including self exclusion, touching bounds, overlapping volumes, rounded center/size arithmetic, invalid/nonfinite fallback and repeated packed-flag updates. Related fixture: core_object_volume_bounds_smoke.py. Extract this host scene-update optimization independently of Houdini and the retained light-tree feature; qualify current CPU/OptiX flags and images separately before submission. |
| Static normal-map primitive classification | 0088 corrects our sampled normal-map extension, whose combined motion-triangle mask also matched static triangles. | Keep this regression correction local. Neutral normal maps must preserve static authored corner normals and Blender shading on CPU and OptiX. Existing motion-normal interpolation must continue to pass. An upstream proposal requires an independently applicable upstream change. |

Suggested first submission candidates are the small geometry/interpolation and
count-overflow corrections. Inspect the latest upstream pin locally before
preparing an external submission, since no current-upstream duplicate search or
maintainer discussion was performed in this offline source task. Build a minimal
engine-owned fixture for each, demonstrate that reverting the candidate causes
its regression assertion to fail, and attach exact raw outputs and versions.

The 0083 extraction must remove unrelated Image Iris/nested/RLS state context from
the review diff while retaining the accepted fork implementation unchanged. The
light-tree and nested-medium packets describe separate larger design series.
No fork patch is retired by this queue.

## Local reproductions on 74e9d9e

The isolated 0075 candidate is available in the local Cycles repository on a
local proposal branch at `f83b015` (fix `090aa07e`). For a quad with edge
factor 32,767, both builds counted 2,147,352,578 triangles. At 32,768 stock
returned −2,147,483,648 and the candidate returned 2,147,483,648. At 65,536
the counts were zero and 8,589,934,592 respectively. These probes call the
engine's count methods without allocating the resulting geometry. The
candidate checks the widened count before converting to an allocation index
and reports overflow through Cycles progress. Both changed production
translation units compiled. The standalone probe ran with MSVC. The proposal's
`Subd.LargeEdgeFactors` GoogleTest passed with GoogleTest 1.15.2 for triangle
and quad factors. Four allocator tests passed at the real signed-index limit.
A separate test reduced the ceiling to 100 in its test build so a small mesh
could reach the real geometry-manager rejection path. One CPU session
rejected and recovered three times with the same mesh, device and scene.
Each rejected render produced zero samples. Cycles still flushed an output
callback with its error flag set. The reference mesh reached 0.7 in RGB, and
each repaired 8 × 8 RGBA image matched that reference exactly. The report of
the independent check is kept with the local evidence below.

Local evidence is under `artifacts/phase3-proposals/` in `subd-before.log`,
`subd-after.log` and `compile-subd.log`. Inspect the patch and its test with
`git -C third_party/cycles show f83b015`.

For 0003 a local proposal branch contains the
isolated fix at `ea3282ca`. Its standalone CPU fixture invokes the real Cycles point intersection
and shading kernels. Stock failed 144 of 504 checks, with all control checks
passing. The isolated fix passed all 504. An independent CPU render compared
native points with an explicit smooth ellipsoid under a directional sun. The
maximum Beauty discrepancy for nonuniform scaling fell from 0.61472 to
0.001137 and the maximum normal error fell from 0.86545 to 0.001411.
These are comparisons to a finite triangle mesh. Independent stock and
candidate Embree and OptiX image comparisons remain open. [Kernel reproduction and exact source revisions](../history/upstream-point-normal-reproducer-2026-11-10.md)
