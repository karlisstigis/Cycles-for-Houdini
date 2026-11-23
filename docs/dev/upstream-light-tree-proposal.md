# Draft: retain mesh-local light trees across scene edits

Status: local review packet, never submitted. Target ancestor:
`74e9d9e48f86330987d7392166a51b3652d7c557`. Exact carried diffs/hashes and file
predecessors are in patches/inventory.json; dispositions are in patches/triage.json.
No claim is made that this packet is accepted upstream or that the related smoke
fixtures individually prove every optimization.

## Deferred until after project completion

Revisit mesh-local light-tree reuse on current upstream Cycles. Create a small Blender-native scene with a dense emissive mesh and a diffuse receiver. Measure translation to the first displayed frame showing the changed illumination in the rendered viewport. Record cache memory, cold-build time and full-emission-edit time. Check geometry and emission edits against fresh renders. Use those results to prepare a focused upstream proposal for translation reuse. Keep the draft local until the maintainer decides to submit it. [Existing measurements and fork decision](../decisions/0010-phase2-renderer-measurements.md)

## Proposed cover letter

Repeated interactive updates currently reconstruct or copy emitter/tree data
whose local geometry and consumed emission energy have not changed. We propose
retaining immutable mesh-local tree data with explicit geometry/resource identity,
then rebuilding only instance-dependent roots and transferring only changed
ranges. The uncached path remains the reference and fallback. The estimator and
emitter selection probabilities must remain equivalent; measured host-time wins
alone are insufficient evidence.

The first change introduces a cache keyed after tessellation, displacement and
static transforms, rather than before geometry has reached its consumed state.
Subsequent changes use flat read-only views, deterministic flattening and explicit
allocation/layout ownership. The API separates cache lifetime from an individual
light-manager update and does not lend mutable storage to device work.

## Proposed series boundaries

1. Correctness prerequisites: 0046 (reallocated emitter attributes), 0052 (packed
   attribute span/layout ownership), and the consumed-energy function from 0061.
   These can be reviewed independently of cache performance policy.
2. 0033 introduces `scene/light_tree_cache.{h,cpp}` and geometry revision identity.
   0035 changes cached traversal to flat views. Keep cache-off construction as a
   differential oracle and preserve exact node/emitter ordering.
3. 0047 adds parallel flattening only after serial equivalence is established.
   0056 adds bounded compaction. 0058 retains exact interior ranges while updating
   instance-dependent roots/bounds. These are separate reviewable performance
   commits, not one replacement 19k-line patch stack.
4. 0060 borrows immutable triangle emitters, with cache ownership and cancellation
   lifetime proven first. 0061 avoids invalidation for changed chromaticity when
   the consumed scalar tree energy is exactly unchanged.
5. 0054 retains allocations/uploads. Split 0059 into a generic device range-copy
   API (capability and full-copy fallback) and its tree dirty-range consumer.
   CPU host allocations, CUDA byte offsets and peer/multi-device ownership must
   be reviewed separately. Do not assume a backend supports partial transfer.
6. Split 0062's degenerate/duplicate cold-build optimization from its profiling.
   Integration diagnostic patches 0034/0036 and private log labels stay outside
   the proposed renderer series.

This is a proposed extraction order, not a claim that the original diffs apply
independently. The accepted order and full replay stay unchanged. Rebase the
logical extraction onto a current upstream review branch and use range-diff;
record any prerequisite hunks explicitly rather than dropping conflicting code.

## Review questions

- Does upstream prefer cache ownership in Geometry or in the light manager?
- Which identity invalidates a cached local tree after displacement, shader
  emission changes, material-slot overrides or motion edits?
- Can borrowed emitter views outlive the update while device transfer is pending?
- Should partial-copy capability be virtual per device allocation, with the
  existing full upload used on every unsupported path?
- What minimum cache size pays for identity/range comparisons on small scenes?

## Evidence and acceptance plan

The source-backed related fixtures are `tests/core_light_tree_cache_smoke.py` and
`tests/houdini_retained_emitter_update_smoke.py`. Their exact hashes, invocation
constraints and assertions are inventoried; they are not portable Blender tests.
Port the minimal geometry/emitter builders to an upstream-owned test harness.
Run cache off/on and serial/parallel pairs for transform-only edits, geometry
changes, same-energy color changes, changed energy, attribute insertion/removal,
material overrides, motion, cancellation/restart and forced reallocations.

Assert full node/emitter/index equivalence, correct root bounds, unchanged
selection probabilities and rendered Beauty/light passes within stated numerical
tolerances. Record host phase time, transferred bytes and peak CPU/GPU memory at
several scene sizes. Test CPU, CUDA/OptiX and multi-device/peer allocations. A
mutation test that suppresses one invalidation/dirty range must fail the relevant
assertion. Publish exact versions, inputs, timing exclusions and feature-off
controls with any eventual proposal. None of these new port/backend experiments
was executed merely by preparing this draft.

No historical patch is retired until upstream equivalents plus successor replay
and affected production fixtures pass. Production Image Iris/RLS/DLSS/Gaussian
features must survive that rebase even though they are outside this series.
