# Native scene publication and retirement

Successful queued IPR publication now swaps prepared owned state on Houdini's
thread. Reconciliation, cache pruning and destruction of the previous large
scene tables happen under exclusive converter ownership. This replaces the
previous `commit(update)` API directly; there is no compatibility overload.
The persistent worker remains the single artist-facing IPR path. Ordered final
rendering and Persistent Data use the same acceptance contracts.

## Ownership and acceptance

On the converter, logical-source deletions expand to all physical packed objects
and geometry. `prepare_commit(update)` reconciles those deletions and prunes
memoization before acceptance. Deletion-only updates prepare
a private copy of the accepted ledger on the converter. Requested but unusable
volume sources retain the recovery state needed for a subsequent repair.

`commit()` accepts only a reconciled transaction. It moves the old adapter ledger
into one bounded retirement slot and moves the prepared ledger into active
state. The capture handle also retains at most one previous immutable snapshot.
The next exclusive scan/conversion drains those owners. Source metadata and
resource destruction never require an extra renderer execution mode.

Capture-only conversion releases its large geometry/object DTO vectors after
the immutable snapshot owns them. Final rendering retains those vectors until
the engine accepts the update. Image-policy acquisition still receives the
shader, light and world information it needs.

Discarded preparation leaves the accepted scene intact. Stale, foreign,
reentered and consumed transactions remain errors. Unaccepted state cannot be
published, and an occupied retirement slot cannot silently be overwritten by a
new publication. There is no blanket exception handler for internal errors.

`native_stats()['scene_lifecycle']` reports the last preparation, publication and
retirement durations; publication/retirement counts; retained owner counts and
object counts; and whether the last adapter retirement ran on Houdini's host
thread. These are diagnostic counters, not per-frame latency measurements.
Close clears the owners and resets the lifecycle counters.

## Scoped measurements

The capture-only fixture uses one 1,036,800-triangle shared prototype at 1,000,
10,000 and 100,000 placements. Eight workloads at each count pass ownership,
inventory, cook and camera-delta checks. At 100,000 placements:

| Host completion | Previous observation | Current observation |
| --- | ---: | ---: |
| Changed surface binding | 28.8 ms | 2.8 ms |
| Changed object transform | 28.9 ms | 2.6 ms |
| Remove all placements | 55.8 ms | 2.6 ms |

These are separate development runs on the same machine, not a controlled
statistical comparison. All current completion calls are 2.3-2.9 ms; host
preparation reaches about 20.3 ms. Cleanup is transferred, not eliminated:
large adapter-table destruction takes roughly 34-40 ms during conversion.
This headless phase probe deliberately calls conversion on its main thread;
the queued GUI tests verify the production background role separately.
The probe excludes rendering, transport and image presentation.

The 4090 viewer fixture uses the same full shared prototype at 100,000
placements, 960x540, 32 samples and three shutter samples. Houdini's native
viewport shows the containing packed bounding box; Cycles receives the full
geometry. All twenty edits reach actual Qt-painted previews, including all ten
navigation edits made while changed geometry is pending. Settled lifecycle
observations confirm bounded owners and background retirement of 100,000-object
tables. Tracked allocations and retained owner counts reach zero on close.

| First Qt paint, five edits per kind | Median | Observed p95 |
| --- | ---: | ---: |
| Camera | 82 ms | 108 ms |
| All instance transforms | 600 ms | 633 ms |
| Camera during geometry capture | 89 ms | 98 ms |
| All instance transforms during geometry capture | 717 ms | 839 ms |

This increment does not establish a total transform-latency improvement.
These HOM/Qt observations are not physical-input/compositor certification or
certification of the separate prospective warm one-million-triangle budgets.

## Parallel packed conversion (P3, 2026-11-16)

Every capture still rebuilds the complete desired object set, compares it with
the accepted one and publishes ordered tables; nothing is skipped. The
per-instance work inside that is now spread over Houdini's TBB pool with an
output identical to the serial one (`src/houdini/scene_adapter.cpp`):

- Packed leaf records (`convert`): each placement's records (key, `random_id`,
  transform and velocity motion, Cryptomatte name, navigation origin, attribute
  source) are built in parallel into their own slots, sorted by key, then
  inserted serially with position hints into the ordered object, origin,
  alias and attribute-source tables. Velocity diagnostics are reported in
  placement order, so the first invalid instance still names the issue.
- Instance attributes (`assemble_instance_attributes`): field reads per
  instance run in parallel; failures, roots and field roots are merged
  afterwards in key order. The layout pass resolves each distinct
  (root, component, layout) once, at its first object in key order, and
  reuses the cache entries it found (records are never erased in that pass).
- Object comparison: the ordered walk is split into key ranges; each range owns
  the accepted keys below the next range's first key, and the records are
  concatenated in range order.
- `snapshot_state` (`src/core/snapshot.cpp`): the walk validates and plans
  every record, records are built in parallel and moved into the table in order.

Each task runs with the calling thread's SSE control word, so floating point
results are bitwise those of a serial run. What stays serial, and why: the
inserts into the ordered maps and sets (one shared tree each), the diagnostics
and failure aggregation (first-occurrence wording), the layout bookkeeping
(shared cache entries and population counters), and the comparison's
flattening of both tables.

`CYCLES_HOUDINI_SERIAL_CONVERSION=1` runs the same code on the calling thread
only. It is a diagnostic for equivalence tests, not a user setting.
`tests/houdini_packed_parallel_conversion_smoke.py` renders frame changes, an
attribute edit, deletions, a reorder, a prototype edit, a material change and a
return to an earlier frame under both schedules and requires bitwise equal
images and identical update counters. On the Threads scene (500,000 instances)
a build of the previous code and this one produce byte-identical worker
payloads and identical snapshot object, pose and navigation digests over the
same sequence, with and without blur (internal probe script, not published).

`convert_apply` on that scene (seconds, before -> after; 16-core host):

| Case | No blur | Blur |
| --- | ---: | ---: |
| First render | 1.97 -> 1.27 | 2.52 -> 1.52 |
| Frame change | 2.13 -> 1.33 | 3.35 -> 1.90 |
| Frame change, 50,000 instances | 0.18 -> 0.12 | 0.27 -> 0.19 |

Shared object table. The adapter's object table is copy-on-write
(`NativeObjectTable`: reads never copy, every write goes through `edit()`, which
first takes a private copy when the table is shared). In capture-only mode the
snapshot now takes the prepared table itself instead of rebuilding an equal one
from the update (`NativeSceneAdapter::snapshot_objects`, the three-argument
`snapshot_state`). This is done only when it is provably the same table: the
previous snapshot holds the accepted table (pointer identity, or both empty) and
no deleted key is also upserted, posed or attributed in the update. Otherwise
the snapshot builds its own copy as before. A superseded or failed capture
leaves the accepted table and the published snapshot untouched. A recreated
session starts from two empty tables. `CYCLES_HOUDINI_COPY_SNAPSHOT_OBJECTS=1`
always builds the copy; it is a diagnostic switch for A/B runs. On the Threads
scene (six runs per case) it takes `convert_apply` on a frame change from 1.37
to 1.05 s without blur and from 1.75 to 1.49 s with blur. Peak host memory
falls from 4.35 to 3.52 GiB and from 5.01 to 3.94 GiB. `encode_snapshot` becomes
0.05 to 0.07 s slower: its walk now runs over the adapter-built table rather
than a freshly copied one. Worker payloads are byte-identical with and without
the switch.

The stage now averages about 4 to 5 busy cores (it was 1.0); the serial inserts
dominate what remains. A pose-only shortcut that would skip rebuilding the
tables for frames where only transforms change was evaluated and not built:
proving "only poses changed" needs every attribute value read anyway, and the
adapter and the snapshot each own an immutable ordered table that a pose change
has to rebuild, so it would save little beyond this and carry a large proof
surface.

## Recycled packed tables (N2, 2026-11-16)

A frame change used to build a new object table, origin map, alias sets and
attribute-source map for every packed instance (serial ordered inserts, 0.30 s
on the Threads scene) and then free the generation before last on Houdini's
main thread (0.15 to 0.25 s). Now the retired generation's object table,
origin map and leaf alias sets are kept (`NativeSceneAdapter::PackedSpare`)
and the next conversion merges its keys into them (`convert`, packed leaves):
records whose key survives are overwritten in place, dead keys are erased,
born keys inserted. Every value is written by the same per-placement code as
the full path (`write_placement`), and instance fields, layouts and geometry
variants are computed afterwards by the unchanged `assemble_instance_attributes`,
so nothing but the keys is carried over from the earlier frame. The worker
payload is unchanged (stage 1 of P3's design: Houdini side only).

Safety. A read-only planning pass runs first: owner counts (the table, the
navigation state and its origin map must have no other owner: a snapshot
still alive keeps its table and navigation, so it is never written), the
table's identity and revision (`NativeObjectTable::revision()`, bumped by
every `edit()`, so any insert, erase or value write since the generation was
recorded is detected; erasing an absent key is no longer a write), all leaf
keys (sorted), duplicates and key collisions with prepared records. Any doubt
gives the full path. The spare is taken out of the adapter before the first
write, so an exception leaves no half-written generation for a later frame.
Captures with direct instances or retained packed roots (which insert after
the leaves) use the full path. `ExtractionStats::packed_recycled_objects` and
`packed_recycle_fallback` (session status) say which path ran and why.
`CYCLES_HOUDINI_FULL_PACKED_CONVERSION=1` forces the full path; it is a
diagnostic switch for A/B runs. Order and values do not depend on the
schedule; the three container merges run concurrently, each serially in key
order. The comparison reuses the key-ordered node lists recorded with each
generation instead of walking both tables.

Proof. `tests/houdini_packed_recycled_conversion_smoke.py` converts every step
of a sequence (births and deaths every frame, only births, only deaths, zero
deaths, attribute values and layout, zero-scale instance, renamed last
instance, reorder, index identities, prototype, material and light edits,
everything dying and coming back, duplicate names; nested packs and packed
fragments; no blur, velocity, acceleration and geometry-sample blur) in three
sessions of one process from the same cooked data: recycled parallel,
recycled serial, forced full path. Payloads, snapshot digests and counters
are equal on every step. A second sequence keeps every snapshot alive and
must never recycle. An internal probe script (not published) does
the same on Threads (two copies of the ROP in memory). Object digests are not
comparable between processes (prototype detail identities in some keys), so
A/B runs use one process.

Threads frame change (frame 275, 380,505 objects, 251,269 pose and 129,235
pose+attribute records; frames 2 to 6 after the first render, means of two
runs of five frame changes each; seconds after apply, Houdini cook excluded):

| | No blur, full | No blur, recycled | Blur, full | Blur, recycled |
| --- | ---: | ---: | ---: | ---: |
| convert_apply | 0.94 | 0.48 | 1.09 | 0.58 |
| busy cores in convert_apply | 3.6 to 5.2 | 7.0 to 9.0 | 3.9 to 5.2 | 6.1 to 8.5 |
| capture done | 1.58 | 0.96 | 2.43 | 1.72 |
| committed | 3.44 | 2.82 | 4.52 | 3.78 |
| first image | 4.75 | 4.03 | 6.18 | 5.25 |

Memory is unchanged: the retired generation was already held until the start
of the next frame; it is now reused there instead of freed (peak RSS within
0.1 GiB of the full path in every run).
The first frame change after a session starts always takes the full path (the
retired generation is the empty initial scene).

## Verification and remaining scope

The evidence record (`artifacts/scene-publication-evidence-20261020.json`)
selects nineteen passing source-audited runs on one compiled runtime. Coverage
includes CPU and both individual OptiX GPUs, mixed-device packed velocity, both
viewers, concurrent source edits/deletion, cancellation during held conversion,
packed and volume break/repair, and independent binding/displacement motion.
Eleven new publication contracts run on CPU and both GPUs. They check rendered
color/data planes as well as inventory through discarded edits, explicit and
authoritative removal, reintroduction, and an essential camera error.
Eight final-sequence cases verify sixteen written images, Persistent Data,
configuration changes, real failure/cancellation cleanup and current HIP reload.

Development failures are retained with their source archives. Fixture fixes
replace outdated shadow-mask expectations with an actually unsupported partial
shadow control, and explicitly disable denoising for analytic final-output pixel
checks. Test/document-only staging preserves the five binaries, six kernels,
55 engine patches and 77 immutable host-runtime entries.

[Queued cancellation and capture close](native-capture-retirement.md) now release
owned data on the converter, with terminal guards and Render-button recovery.
Raw abandoned capsules outside that protocol, many distinct graphs and
deep/distinct packing still need profiling. Large transform extraction,
serialization/core reconciliation and dense-emitter synchronization remain
expensive. Physical mesh/BVH sharing across different surface-only materials is
unchanged. Remaining feature contexts, extended memory/device/input/monitor
checks and reproducible release certification remain open. No Loop subdivision
work is included.
