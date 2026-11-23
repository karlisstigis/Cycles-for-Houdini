# Light-tree store cost and the next move

Measured 2026-11-10 for review `648f241`, following K32. Keep the production
engine unchanged. Removing cache storage brings a frame sooner on cold and
full-palette builds, but makes the immediately following move take seconds.
This experiment measures an omission; it does not qualify deferred storage.

## First output and immediate movement

The fixture uses the original 1,048,576-triangle shared mesh, twelve instances,
four emission palettes, 192 × 144 pixels, one sample and automatic threads.
Each process performs cold build → move → all-palette edit → move, without a
sleep between images. Three rotated triples compare the matched stock tree,
production, and production with only `LightTreeMeshCache::store` omitted.
The matched stock tree includes the fixture's per-object palette semantics.

Times begin immediately before `Session::reset` and end at its first complete
one-sample output callback. The fixture also records `Session::wait` completion.
This is renderer output, excluding host extraction and GUI paint. It does not
measure a partially rendered navigation image or an artist's first displayed
pixels. Fresh reference processes warm the kernel cache before timed triples.

| Backend and edit | Matched stock first output | Production first output | Omit store first output |
| --- | ---: | ---: | ---: |
| CPU cold | 2,246 ms | 3,785 ms | 2,537 ms |
| CPU immediate move after cold | 2,566 ms | 74 ms | 2,993 ms |
| CPU all palettes | 2,465 ms | 3,980 ms | 2,982 ms |
| CPU immediate move after all palettes | 2,467 ms | 49 ms | 2,958 ms |
| OptiX cold | 2,582 ms | 3,583 ms | 2,978 ms |
| OptiX immediate move after cold | 2,354 ms | 44 ms | 2,616 ms |
| OptiX all palettes | 2,618 ms | 3,507 ms | 2,836 ms |
| OptiX immediate move after all palettes | 2,668 ms | 48 ms | 2,473 ms |

These are per-arm medians. The paired median savings from omitting storage were
1,169 ms cold and 961 ms all-palette on CPU, and 709 ms cold and 671 ms
all-palette on OptiX. Storage omission was faster in all three pairs for each
of those cases. It also removed the retained data needed by the next move.
Even omitting storage did not bring the full-palette path below matched stock
in this run; it cannot explain the entire production cost.

All 36 history images per backend matched their independently fresh matched
stock image byte for byte: 72 comparisons across CPU and OptiX. Each fresh
reference corresponds to the exact current position and emission state.
These directly visible-emitter images establish incremental consistency for
this diagnostic. The earlier K32 receiver tests remain the transport evidence.
Three triples are focused measurements, not a replacement for the earlier
nine-pair decision evidence or a nightly qualification.

CPU records (`artifacts/review-648f241-light-tree/CPU/CPU.json`) ·
OptiX records (`artifacts/review-648f241-light-tree/OptiX-matched/OptiX.json`)

## Why moving the existing store call is unsafe

`LightTree::build` stores complete local trees before the serial instance loop
overwrites their root measures. During flattening,
`light_tree_leaf_emitters_copy_and_flatten` can swap root node types and child
variants between instances and remove `LIGHT_TREE_INSTANCE`. The pointer that
originally identified the local reference can therefore describe another
variant after flattening. Keeping just its pointer or restoring its measure
would not preserve the original hierarchy.

`LightManager::device_update_tree` owns `LightTree` on its stack. Its roots and
emitter vector are destroyed when the device update returns, before frame
delivery. `LightTreeMeshCache::store` also reads the current scene's object,
mesh, emission palette and light-set membership to create its key. The next
edit may change those fields or remove the mesh. Cache admission, eviction,
borrowed entry pinning, pruning and byte accounting currently run under the
serial scene-update ownership; they are not a concurrent publication API.

A safe deferred design therefore needs to own an immutable pre-mutation local
hierarchy and emitter payload, freeze its revision/palette/membership key, and
publish or discard it under the scene/cache owner. It must retain or validate
mesh identity without dereferencing a deleted scene node, handle Stop and
shutdown, and account for pending snapshot memory in addition to the retained
cache budget. Keeping raw construction trees alive can add memory beyond the
224 MiB retained-entry high-water recorded in K32; that extra cost has not been
measured. Freezing these inputs also has a first-frame cost of its own.

The next move is the essential second measurement. Waiting for a deferred store
there merely moves latency from the emission edit to the gesture; skipping it
produces the omission arm above. Any actual deferred candidate must measure
both timings, freshness, cancellation, mesh deletion, memory and independent
fresh images before production adoption. No such candidate was implemented or
qualified in this follow-up.

## Rejected synchronous triangle-block candidate

A narrower candidate was implemented privately after the omission experiment.
It preserves the existing ownership boundary: encode triangle snapshots in
waves of at most four independent 1,024-record blocks, join before returning
from `store`, and leave node traversal, cache admission and final publication
serial. Each block has its own XOR predictor. A wave runs only if its worst-case
output plus the existing 512 KiB scratch reserve fits without eviction; otherwise
the existing single-block admission behavior applies. Raw entries use the
existing serial path. Roots, scene fields and emitter arrays remain owned by
the synchronous build throughout.

The source analysis identifies a feasible implementation, not a performance
guarantee. **Do not promote this candidate.** Three alternating same-binary CPU
pairs lost cold first-output time in every pair: 186–336 ms slower, with a paired
median loss of 275 ms. Full-palette savings were 91, 145 and −218 ms. Immediate
moves retained their approximately 37 ms behavior, but that does not compensate
for the repeated cold regression.

| Focused run and edit | Production median | Candidate median | Paired median saving |
| --- | ---: | ---: | ---: |
| CPU cold | 3,071 ms | 3,258 ms | −275 ms |
| CPU all palettes | 3,189 ms | 3,098 ms | 91 ms |
| OptiX, warmed repeat, cold | 3,257 ms | 3,466 ms | −209 ms |
| OptiX, warmed repeat, all palettes | 2,969 ms | 3,381 ms | −514 ms |

These measurements use a separate timing window from the omission table.
Compare arms within a pair; the per-arm medians need not subtract to the median
of paired differences. An initial OptiX pilot had a first-process kernel-cache
warmup in its first cold baseline. It suggested a palette benefit, but a repeat
with an explicit untimed warmup reversed that result. The warmed repeat's cold
savings were 379, −373 and −209 ms; its palette savings were 190, −514 and
−515 ms. No further timing search was performed. Warmed OptiX immediate-move
paired differences stayed within 8 ms. The data support retaining production,
without claiming a backend-specific win.

All timing images matched the corresponding independently fresh reference.
A separate CPU integrity check compared exact encoded-payload hashes, entry
sizes and cumulative cache-owned bytes between serial and parallel paths.
At the default budget, six admitted snapshots exercised 6,144 parallel triangle
blocks. Their payloads and accounting matched exactly, with the same
235,230,930-byte retained high-water. At a 1 MiB budget both arms declined the
same sixteen admissions; every output still matched its fresh reference.
The four-worker encoder buffers fit within the existing scratch reserve by
construction; pending-memory RSS was not separately profiled.

An injected Stop immediately after the first joined four-block wave published
no partial entry: the interrupted build reported zero entries and zero bytes.
The same session then recovered, completed both source states and retained both
immediate moves, with all four recovered images matching fresh references.
Cycles can send a cleared output tile on cancellation, so an initial harness
assertion that a cancelled pixel vector must be empty was incorrect. Validation
was corrected to check cancellation, absence of cache publication and recovered
image identity. This test cancels at a wave boundary; it does not qualify
arbitrary cancellation races inside an encoder task.

The rejected override, generator, executable, raw logs and exact hashes remain
under `artifacts/review-648f241-light-tree/parallel-candidate`. There is no
production integration patch, lock change or triage entry: the experiment did
not earn promotion. The original cache and its accepted K32 policy remain in
effect.

CPU pairs (`artifacts/review-648f241-light-tree/parallel-candidate/CPU-timing.json`) ·
OptiX warmed pairs (`artifacts/review-648f241-light-tree/parallel-candidate/OptiX-warmed-timing.json`) ·
Integrity and Stop recovery (`artifacts/review-648f241-light-tree/parallel-candidate/CPU-integrity-validate.json`) ·
Rejected diagnostic diff (`artifacts/review-648f241-light-tree/parallel-candidate/rejected-candidate.diff`)

## Reproduction and resource identity

The standalone [fixture](../../tests/light_tree_first_frame_benchmark.cpp) and
[runner](../../tests/light_tree_first_frame_benchmark.py) reuse the frozen K32
matched-reference compile/link recipes. They build diagnostics under the output
directory and leave production libraries and installed workers alone. The
runner verifies that the old omission-probe source equals the current cache
source except for its single bypass switch. The compile recipe retains
`WITH_IMAGE_IRIS`. Run in a Visual Studio developer environment (`<build-directory>` is the
build folder of the `windows-h22.0.466-release` preset):

```powershell
python tests/light_tree_first_frame_benchmark.py --device CPU --resources <build-directory>/hosts/20261110T100746426/resources --output artifacts/light-tree-first-frame-cpu
python tests/light_tree_first_frame_benchmark.py --device OptiX --resources <build-directory>/hosts/20261110T100746426/resources --output artifacts/light-tree-first-frame-optix
```

Use the resource root matching the linked Cycles libraries; the build manifest
records it relative to the build directory. The completed OptiX run used
`20261110T100746426`, whose compressed OptiX kernel SHA-256 was
`f806a82bc7b4e9d4d510751f25c54f83ca8c1fff1f5c5c2a3cb3288352536b02`.
Its output records fixture and executable hashes. The CPU run used the earlier
K32 resource root; this fixture executes its CPU kernels from the linked
libraries and its 36 independent image comparisons passed.

An initial OptiX attempt reused K32's older `20261110T053919431` resources with
current libraries and failed with CUDA illegal access before its first fresh
frame. It produced no accepted timings. Matching the current kernel resources
resolved that mismatch; all accepted OptiX results are under `OptiX-matched`.
