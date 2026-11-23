# Reduced cold lighting-tree split work

Patch 62 reduces repeated CPU work while building a new mesh-light hierarchy.
It skips duplicate split costs across empty buckets and retains only the
measures needed from right prefixes. It does not approximate bounds, energy,
slot identities or split selection, and adds no artist-facing execution mode.

## Preserved split calculation

Crossing a bucket with zero emitters changes neither cumulative measure nor
the left emitter count. Its cost equals the preceding split's cost, which
cannot improve the strict minimum a second time. The first split is always
evaluated, preserving the original first-tie rule. A populated bucket with
zero estimated energy still follows the ordinary path.

Right prefixes contribute only their measure to split costs. Their accumulation
keeps the original right-to-left arithmetic; unused counts and linking metadata
are no longer copied or merged there. Left-prefix counts, node linking and node
measures keep their original calculation.

The exact helper contracts cover all 4,096 occupancy masks with varied bounds,
cones, energies, populated zero-energy buckets and linking sets. They compare
original and optimized measures, costs, selected split and tie behavior.

## Measurements

The paired controls use the same compiled runtime. Both modes use measure-only
right prefixes, so the paired timing isolates duplicate cost skipping rather
than quantifying the right-prefix saving separately. The fixture uses one
1,048,576-triangle prototype, twelve placements, four emission palettes, two
threads, 192x144 output and one sample.

| Configuration | Local control / optimized (s) | Local time reduction | Complete image control / optimized (s) |
| --- | ---: | ---: | ---: |
| CPU | 4.032 / 3.168 | 21.4% | 6.320 / 5.430 |
| 4090 OptiX | 4.045 / 3.232 | 20.1% | 6.614 / 5.572 |
| 3080 Ti OptiX | 4.050 / 3.164 | 21.9% | 6.704 / 5.574 |
| Both OptiX GPUs | 4.025 / 3.226 | 19.9% | 7.012 / 5.993 |

Warm complete-image medians remain about 48 ms over six sequential observations
per configuration. These cold timings are scoped measurements, not production
percentiles or physical input-to-paint certification. Cold local construction,
snapshots, first descriptor preparation, other lookups and host field/body work
still remain.

## Optional attribution

`CYCLES_HOUDINI_PROFILE_LIGHT_TREE_DETAIL=1` adds internal stage attribution.
Ranges of at least 1,024 emitters are fully timed; smaller nodes use a bounded
deterministic 1/256 sample. Counters are atomic, and the task pool joins before
the profiler is destroyed. The decision stage includes its sub-stages. Estimated
CPU work includes timer overhead and is not additive wall time. The internal
`nodes_created` field uses the engine's logical node counter, including retained
cached interiors; it is not an allocation counter. The allocation stage samples
actual new recursive children.

The production path has no fine timing allocation or clock calls when this flag
is absent. Coarse and detailed logs report the actual duplicate-skip policy.
`CYCLES_HOUDINI_DISABLE_EMPTY_LIGHT_TREE_SPLIT_SKIP=1` is only an internal
measurement control. It leaves the artist's persistent-worker IPR path intact.

## Verification and precision scope

The verified runtime is `hosts/20261020T153940856/resources`, protocol 47,
62 patches and six unchanged patch-61 kernels. Forty-six selected source-audited
runs pass: 32 main runs, ten timing/image controls and four identical-source
paired-worker runs. See the evidence (`artifacts/cold-tree-evidence-20261020.json`)
and source/run archive (`artifacts/verification/light-tree-cold-build-20261020/index.json`).

- The core suite passes 13,112 contracts, including all 4,096 bucket layouts.
- Four dense suites each pass 44 independent matched-history image/data checks
  and five ordered frames. Four additional control suites use the original
  duplicate-cost evaluation. Tree node/emitter counts match across all applicable
  paired states; ordinary image tolerances remain unchanged.
- Analytic/mesh illumination, per-face ordering, negative/nonuniform transforms,
  emission edits, linking and material/geometry repairs pass on CPU, both GPUs
  and mixed devices. Per-face suites each compare 17 images and write five frames.
- Seven interruption/lifecycle runs cover real source preparation, partial
  uploads, resize, compact caches, repair and closure. Cache budgets and strict
  owned byte ranges also pass.
- Both viewers on each GPU pass 16 states and six queued camera/object edits.
  The packed palette edits retain source conversions, topology and triangulation.
- The paired identity fixture captures each edit once, sends identical serialized
  packets and update history to persistent workers, fixes CPU affinity, verifies
  packet immutability, and tests 32 states per device configuration. CPU pixels
  match bit-for-bit. GPU depth and position match exactly; beauty/normal differences
  stay below 1.5e-6, with comparable variation in a same-policy GPU repeat. GPU
  bounds are atol=2e-6/rtol=2e-6, tighter than the existing dense comparison.

Exploratory cross-process hashes differed in normals/beauty even in a state with
light-tree rendering disabled. The initial overstrict GPU byte-identity fixture
also failed on small differences and is preserved in the archive. The final
fixture controls source packets and measures a same-policy repeat. It makes no
universal byte-identity claim for GPU pixels and does not relax the existing
independent numerical checks.

Relevant profile/reference/lifecycle/viewer sessions close with zero tracked
geometry/device ownership; paired fixtures close all two/three workers. The
native process inventory is empty. This is not a physical-memory or prolonged
production certificate.

The overall native implementation goal remains active. The current production
audit already records scoped implementations of COP images, world volumes,
textured/IES lights, parametric curves, camera/shutter models, previews and
background final rendering. Their remaining contexts and certification gates
must not be confused with those whole features being absent. Next work is
recorded in the follow-up (`artifacts/native-next-work-after-split-20261020.json`).
