# Borrowed cached triangle emitters

Patch 60 removes repeated construction and retirement of source emitter
objects when their mesh-local cache entries are already valid. The builder
borrows immutable triangle records for the duration of one update. It does
not retain another large emitter array between updates or add an artist-facing
execution mode.

The verified runtime is `hosts/20261020T134725099/resources`, with protocol 47
and the same six kernels as patch 59. Thirty-one selected source-audited runs
pass on CPU, both individual OptiX GPUs, mixed-device rendering and both viewers
on each GPU. See the
measurements and selected runs (`artifacts/emitter-views-evidence-20261020.json`)
and source/run archive (`artifacts/verification/light-tree-emitter-views-20261020/index.json`).

## Source ownership and indexing

Cache entries are pinned before construction, using the existing exact
geometry/emission/linking keys and cache budget. Borrowed entries cannot be
evicted or mutated until the builder and flattening finish. No borrowed source
owner survives the update. Cold construction still creates ordinary live
`LightTreeEmitter` objects; no nontrivial objects are left unconstructed.

The dense vector contains cold triangles and the small outer emitter forest.
Sorted spans preserve the original logical triangle order independently of
physical dense positions. Cold leaf indices are rebased, cache snapshots remain
local, and outer/distant emitter indices keep the same logical offsets. Mixed
cache hits and misses therefore use the same ordering as uncached construction.

Ordinary, linked and debug consumers use a checked logical accessor. Its view
borrows a current builder-owned root for mesh instances and contains no root
ownership for triangles. Packed triangle reads use a lazy 16 KiB block per
active reader, with entry identity and block checks. Each parallel flattening
task has its own reader. Unchanged cached interiors need no triangle decoding.

Cold construction, geometry/material repair, eviction, tree disable and scene
closure follow the existing ownership rules. Kernel-buffer validity is still
committed only after the complete update succeeds. Source views do not weaken
the full initialization required after cancellation or resize.

## Measurements

The fixture uses one 1,048,576-triangle prototype, twelve packed placements,
four emission palettes, two worker threads, 192x144 output and one sample.
After the interior descriptors are established, each update constructs only
twelve outer emitters and borrows 4,194,304 cached triangles. Five warm
observations per configuration on the same compiled runtime give these medians:

| Configuration | Materialized-record control | Borrowed records |
| --- | ---: | ---: |
| CPU | 168 ms | 48 ms |
| RTX 4090 OptiX | 165 ms | 48 ms |
| RTX 3080 Ti OptiX | 167 ms | 48 ms |
| Both OptiX GPUs | 165 ms | 49 ms |

Warm construction and retirement are each below 0.1 ms in this fixture.
Controls spend roughly 86 ms constructing and 29-30 ms retiring source
emitters. Existing partial kernel uploads remain at 84,240 node bytes in five
spans. The control changes source materialization only.

These sequential samples include capture, transport, engine preparation and
completed-image receipt. They exclude physical input and GUI paint, and do
not establish production percentiles or universal artist response. The first
cache-hit descriptor pass still prepares the required kernel rows. Cold tree
construction remains roughly five seconds in this fixture.

## Verification

Four suites each pass forty-four independent matched-history image comparisons
and five ordered output frames. New cases specifically rebuild an earlier
emitter while a later one is cached, rebuild a later emitter after cached
triangles, then repair both sources. Beauty, depth, normal and position outputs
retain their independent comparisons.

Seven lifecycle runs cover source rebuild cancellation, original partial-row
preparation, resize, repair, compact caches and retirement. Each default source
cancellation run passes six image states and proves invalid buffers are fully
initialized before reuse. A profiling-only source-plan marker identifies real
uncached work, including when a fast transform preview finishes first; it
introduces no delay. Both viewers on each GPU pass fourteen states and six
queued camera/object edits. Linked emitters pass twenty-one checks on each GPU;
analytic/mesh lighting passes sixteen states per GPU and mixed devices. Cache
budget, 713 cache contracts and 100 CPU device-range contracts also pass.
Profile/reference/lifecycle/viewer closures report zero tracked geometry and
device ownership.

Two unsuccessful development fixtures remain archived. One expected a cache
build after an unchanged scene; it now makes a real object edit. Another tried
to invalidate an already completed transform preview; it now interrupts the
actual queued source rebuild. Both corrections preserve the intended image and
interrupted-buffer assertions. All selected final-runtime runs pass.

CH_LIGHT_TREE_PHASES adds dense/cached source emitter and span counts;
CH_LIGHT_TREE_CACHE reports borrowed/materialized triangle counts. The internal
`CYCLES_HOUDINI_DISABLE_LIGHT_TREE_EMITTER_VIEWS=1` control restores materialized
source records for comparison without changing the persistent-worker IPR path.

## Remaining work

Cold construction, first descriptor preparation, nonuniform transformed measure
recounts and remaining lookup transfers still need attention. The
measured cold-work follow-up (`artifacts/light-tree-cold-work-followup-20261020.json`)
records the current breakdown and an overbroad mesh-emission key candidate.
Host body/field deltas, incremental/incompatible-field family maintenance,
remaining adapter contexts and physical-memory/input/device/release
certification remain in the broader native production plan.
