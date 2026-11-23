# Owned lighting-tree range uploads

Patch 59 uploads changed regions of retained lighting-tree node and emitter
arrays. It uses the exact row comparisons and completed-buffer ownership from
the earlier retention work. The persistent worker remains the single
artist-facing IPR path; no viewer or ROP execution mode is added.

The subsequent [patch 60 borrowed emitter views](native-cached-emitter-views.md)
remove the warm source-emitter construction/retirement costs documented here.
The patch 59 measurements below retain their original verified scope.

The verified runtime is `hosts/20261020T131314529/resources`, with protocol 47
and the same six kernels as patch 58. Thirty selected source-audited runs pass
on CPU, each OptiX GPU and the installed mixed-device configuration. See the
measurements and selected runs (`artifacts/light-tree-ranges-evidence-20261020.json`)
and source/run archive (`artifacts/verification/light-tree-ranges-20261020/index.json`).

## Ownership and bounds

Concurrent exact comparisons mark one bit per 256 rows. Adjacent blocks are
coalesced, with at most 64 transfers; dense or fragmented edits use the full
upload. The mask needs about 4 KiB for the profile fixture's eight million
node rows. It retains no source or device pointers.

An explicit device-memory operation validates every byte range before copying.
Ranges must be ordered, disjoint, nonempty and inside the host owner. The
operation requires an existing allocation with completed initialization and no
pending replacement. Unsupported allocations/backends use the ordinary full
copy. Malformed bounds remain programming errors.

CPU buffers keep their existing host/global bindings. CUDA and OptiX preserve
the ordinary mapped-memory rules, including shared host aliases and separate
resident allocations. MultiDevice preflights all owning islands, translates
its virtual keys through the existing pointer maps and restores the original
owner on exit. Range copies do not allocate, change global bindings or invent
aliased memory owners. Peer data uses the existing owning island.

New/resized arrays and interrupted updates still require full initialization.
Completed-buffer state and reusable contexts are committed only after every
required upload succeeds. Device errors remain genuine failures. The original
partial-preparation interruption fixture and the default retained path both
verify repair and resumed reuse.

## Measurements

The fixture has one 1,048,576-triangle prototype, twelve packed placements,
four surface palettes, two worker threads, 192x144 output and one sample.
Five warm observations after descriptor establishment give these medians on
the same compiled runtime:

| Configuration | Full-copy control | Changed regions |
| --- | ---: | ---: |
| CPU | 176 ms | 177 ms |
| RTX 4090 OptiX | 241 ms | 166 ms |
| RTX 3080 Ti OptiX | 242 ms | 176 ms |
| Both OptiX GPUs | 297 ms | 176 ms |

Warm node requests fall from 671,090,960 bytes in one upload to 84,240 bytes
in five spans. Unchanged emitter rows need no upload. CPU timing is essentially
unchanged because its device buffers already alias host memory.

These small sequential samples include native capture, transport, preparation
and completed-image receipt. They exclude physical input and GUI paint, and
are not production p95 acceptance. Copy diagnostics report logical requests,
not physical bus traffic: CPU/shared aliases need no transfer, while multiple
independent GPU owners can each copy the requested bytes.

## Verification

Direct byte/canary readbacks pass 100 CPU, 266 per-GPU and 374 mixed-device
contracts. Each GPU configuration exercises four mapped allocations and two
shared host aliases, as well as resident buffers, concurrent dirty marks,
misaligned byte ranges, malformed bounds, replacement and resize. Mixed tests
read back every resident owner; these are checks of the installed topology,
not certification of every possible peer layout or backend.

Four dense suites each pass forty comparisons against independent uncached
render histories and five ordered output frames. Six lifecycle runs cover
partial preparation, cancellation, resize, repair, compact caches, tree
disable and resource retirement. Analytic/mesh lighting passes sixteen states
on both individual GPUs and mixed devices. Core cache contracts pass 713
checks. Both viewers on each GPU pass fourteen states and six queued
camera/object edits. Exercised session closures report zero tracked geometry
and device allocations.

The first development build failed because the new contract harness had a
malformed array initializer. Its source and log are preserved in
`artifacts/light-tree-ranges-build-a-failed-sources/`; the corrected final
runtime passes all thirty selected runs.

CH_LIGHT_TREE_PHASES now reports requested bytes, span counts and partial/full
selection for nodes and emitters. The internal
`CYCLES_HOUDINI_DISABLE_LIGHT_TREE_RANGE_UPLOAD=1` control provides the full-copy
comparison without changing preparation or the artist interface.

## Remaining work

The warm fixture still spends roughly 90 ms preparing cached triangle emitters
and 30 ms retiring them. Those constructed lifetimes are the next measured
target. Triangle/other lookup uploads and roughly five-second cold tree
construction also remain. See the
emitter lifecycle follow-up (`artifacts/light-tree-emitter-lifecycle-plan-20261020.json`).
Host body/field sharing, incremental/incompatible-field family maintenance,
additional feature contexts and physical-input/memory/device/release
certification remain part of the broader production plan.
