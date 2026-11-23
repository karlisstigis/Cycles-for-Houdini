# Packed fragments

A packed fragment (`PackedFragment`, made by Assemble with **Create Packed
Fragments** and by Pack with a name attribute) does not own geometry. It refers
to the primitives of one *shared* detail whose fragment attribute (usually
`name`) holds the fragment's value; thousands of fragments share that detail.
`GU_PrimPacked::getPackedDetail()` gives the adapter nothing for them, which is
why fragments used to be omitted with `geometry.packed_unavailable`.

## Representation

`packed_contents()` (`src/houdini/packed_fragments.h`) returns the contents of
any packed primitive as one in-memory detail. For packed geometry it is the
primitive's own detail. For a fragment it is a detail holding exactly the
fragment's primitives with their points, vertices, attributes and materials:
what an Unpack SOP produces before it applies the packed transform.
From there a fragment is an ordinary packed instance: its own prototype, its
own transform (full transform with pivot, from the packed primitive), point
`v`/`w`/`accel` motion, attributes and material on the packed primitive,
attributes and several materials inside, nesting in both directions, the
existing content addressing, population reuse and diagnostics.

One renderer geometry per fragment is deliberate. Each fragment is distinct
geometry with its own transform, which is what per-piece motion blur and
rigid-body animation need; content addressing still merges fragments whose
contents are bit-identical. Rendering the shared detail once instead would need
a different transform for different primitives of one renderer geometry, which
Cycles geometry does not have.

## Partition and batches

The naive way (for every fragment scan the shared detail, which the HDK's own
`GU_PackedFragment::getPrimitiveRange()` does) is quadratic. Instead:

1. Each shared detail is grouped once per fragment attribute: keys (string
   index or integer) are read in parallel, `(key, primitive index)` pairs are
   sorted in parallel, and runs are cut. Ties cannot occur, so the result does
   not depend on the schedule.
2. On a miss, the fragment and the following unbuilt fragments of the same
   packed selection (up to 8,192 fragments or 2M source primitives) are built
   in one parallel batch (`GEO_Detail::mergePrimitives` with the run's offsets
   into a new detail per fragment). The new details are allocated on one
   thread in fragment order before the parallel fill: a detail's unique id
   comes from a process-wide counter and becomes the prototype identity, so
   allocating inside the parallel loop made the snapshot depend on the
   schedule.
3. Partitions and built fragments stay cached, keyed by the shared detail's
   unique id and stamped with its topology, primitive list, meta cache count
   and every attribute's data id. A frame where only packed transforms move
   (rigid pieces, the normal RBD case) keeps the shared detail, so the same
   fragment details (same unique ids) come back and the adapter sends poses
   only. A deforming or recooked source builds new fragments; unchanged
   contents are still recognised by content addressing.
4. A partition is dropped when nothing but the cache references its shared
   detail any more (the SOP recooked or was deleted).

Fragment attributes that are not primitive string or integer attributes use
the HDK's range, once per fragment value (`fallback_ranges`).
`CYCLES_HOUDINI_CHECK_FRAGMENTS=1` compares every built fragment with that HDK
range and raises on a difference (the fragment smoke test sets it).
`CYCLES_HOUDINI_SERIAL_CONVERSION=1` builds on one thread.

The cache is guarded by one lock and callable from any thread; its parallel
steps run inside `UTisolate`, so a thread holding the lock never picks up an
outer task that could wait on it. Counters are published to a separately
locked copy (`native_stats()["packed_fragments"]`, cumulative per process:
partitions, fragments built, batches, seconds, reuses, empty fragments,
fallbacks, evictions, checked ranges).

## Empty fragments

A fragment whose value matches no primitive, or whose shared detail lacks the
attribute, renders nothing and reports `geometry.packed_unavailable` with
"Packed fragment (name = "x") matches no primitive of its shared geometry" (or
"... without the primitive attribute ..."). Other fragments keep rendering.

## Memory

Built fragments are copies: while a shared detail lives, its fragments'
geometry exists twice on the host (Houdini's shared detail and the per-fragment
details), in addition to the renderer's converted geometry.

## Measurements (2026-11-16, Ryzen 9 7950X, OptiX 4090; K1 Assemble scene)

Internal probe script (not published), `objects <n> [packedgeo] static`: spheres
assembled into n pieces, fractured source static, pieces rotate per frame.

| | first render (cook + capture to first pixel) | transform-only frame |
|---|---|---|
| 2,000 fragments | 0.14 + 3.48 s | 0.18 s, 233 KB, 0 conversions, 0 fragments built |
| 2,000 packed geometry | 0.26 + 3.43 s | 0.15 s, 233 KB, 0 conversions |
| 20,000 fragments | 0.44 + 21.0 s | 0.99 s, 2.36 MB, 0 conversions, 0 fragments built |
| 20,000 packed geometry | 1.97 + 19.5 s | 1.00 s, 2.36 MB, 0 conversions |

At 20,000 the fragment build is 0.63 s of the first render (partition 0.04 s)
at about 17 cores busy; the Assemble cook is 1.5 s shorter with fragments than
with packed geometry, so the totals are equal. When the fractured source
recooks every frame (the K1 scene without `static`), the fragments are rebuilt
(0.6 s at 20,000, about 10 cores), the converted geometry is still recognised
by content and only poses are sent; packed geometry pays a 1.9 s Assemble cook
instead.

## Tests

`tests/houdini_packed_fragment_smoke.py` (CPU and OptiX) compares fragments with
the same Pack set to packed geometry and with Houdini's Unpack.
