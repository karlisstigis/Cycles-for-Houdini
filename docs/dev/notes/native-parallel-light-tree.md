# Parallel cached mesh-light updates

Houdini 22.0.429/Python 3.13, host `20261019T002425132`, protocol 44,
47 pinned patches, five compiled artifacts and six unchanged kernels.

## Change

Patch 0047 parallelizes conversion of large retained mesh-light trees to the
kernel representation. It reserves each complete node range before dispatch and
writes disjoint node, emitter and triangle-lookup slots. Cached leaves contain
triangles only: they cannot recurse or modify the shared instance-reference map.
The current scene still supplies object IDs, primitive offsets, visibility,
emission sampling and root measures, including changed first-instance ordering.

Ranges below 4,096 nodes stay serial. Larger ranges use the existing Cycles task
arena and thread limit, with 1,024-node grains. All tasks join before borrowed
cache records or scene owners can retire. The 256 MiB/1,024-entry cache limits,
kernel data, scene transport and artist-facing execution path remain unchanged.

`CYCLES_HOUDINI_PROFILE_LIGHT_TREE=1` reports `parallel_cached_nodes` and
`parallel_cached_ranges`. The internal
`CYCLES_HOUDINI_SERIAL_LIGHT_TREE_FLATTEN=1` switch provides a serial diagnostic
control; it is not an artist-facing render mode.

## Scoped performance observations

The matched exploratory fixture uses a retained 1,048,576-triangle emitter,
twelve copies, 960×540 output, one sample and two worker threads on RTX 4090
OptiX. Its heavy prototype is behind the camera. These are update/ownership
stress measurements, not representative FX ray cost or production p95.

| Warm action | Complete worker frame before | After |
| --- | ---: | ---: |
| Object transform | 264 ms | 203 ms |
| Small mesh deformation | 331 ms | 260 ms |
| Mesh repair | 321 ms | 263 ms |
| Shader fallback | 321 ms | 259 ms |
| Shader repair | 334 ms | 274 ms |

Cached flattening itself takes 58–80 ms versus 99–119 ms before the change.
Whole-array uploads remain about 24–27 ms in this paired run. Camera-only edits
do not rebuild the tree. Initial construction does not use the cached path.

A separate 22-state live Cycles Viewer run at 960×540 records actual first paint:
camera 58 ms (54 ms while navigating recovered subdivision), object 183 ms, and
geometry/material/recovery approximately 296–400 ms. The warm 16 ms timer probe
has a maximum gap of 57 ms, including test validation. One observation per varied
state is not a latency distribution or physical-input certificate.

## Real changing-prototype control

`houdini_heavy_packed_cooking_response_benchmark.py` uses the actual courtyard
plus a 721×721 native Grid, a nonlinear native VEX wave and sixteen packed copies
through two levels. It performs thirty edit cases and six concurrent object/camera
updates, then repeats the shape/topology values with rendering stopped and Auto
disabled. Only one render viewer is open.

In the final run, topology timer gaps are 181–197 ms with rendering active and
194–233 ms in the stopped control. Shape gaps are 106–176 ms active and 100–105 ms
stopped. The earlier baseline's measured host renderer calls are at most 20 ms.
The stopped controls run afterward with warm caches; they do not isolate every
millisecond. Cooking, viewport draw, copy-on-write and scheduling still need
separate attribution before claiming zero stutter.

Camera first paints take 65–72 ms in this courtyard run. Shape previews take
272–354 ms and geometry refinement 1.07–1.17 s; topology previews take 362–378 ms
and refinement 1.27–1.29 s. Concurrent submission times are not independently
measured navigation-to-paint latency.

The five-frame heavy animation fixture separately combines the adaptive turbine,
changing packed prototype and five distinct displacement maps. All five EXRs and
display images complete without source warnings in one persistent worker. Frame
times including output are 5.095, 1.857, 1.771, 1.976 and 1.857 seconds; cumulative
time is 12.833 seconds. This includes the first frame's initialization.

## Correctness and limits

The evidence index (`artifacts/parallel-light-tree-evidence-20261019.json`)
contains fifteen passing source-audited runs on the final test manifest:

- CPU and both OptiX devices each pass forty small fresh-reference lighting
  comparisons and forty large comparisons against a separate uncached worker
  receiving the same edit history. Eighteen large comparisons per device execute
  the parallel path. Each suite also writes five ordered frames.
- The same devices pass twenty-one warm retained-emitter states. Cache limits,
  eviction/retirement and mesh/world linking retain their independent checks.
- Both viewers separately pass twenty-two live edit/repair states, retain one
  heavy source cook, and close with zero owned geometry/device allocations.
- The nonlinear cooking control and five-frame heavy animation pass. All
  forty-seven locked patches reproduce the engine checkout.

The large reference preserves edit history because fresh dense displacement can
round positions and recomputed normals differently from retained geometry even
with caching disabled. The isolated diagnosis has bit-identical cached/uncached
retained images and data passes; fresh geometry differs by about 6e-8 in Position
and up to .005 in Normal. The original failed fresh comparison is retained in
the archive. Pixel tolerances have not been relaxed. Dense displacement numerical
stability remains a separate investigation.

Nonuniform instance measure recounts, initial construction and whole-array GPU
uploads remain. Further navigation bursts, prolonged memory, 1080p/4K, mixed
devices, remaining native adapters and release certification remain open.


## Retained upload follow-up

Patch 54 additionally retains completed light-tree node/emitter arrays and skips exact unchanged uploads. The earlier measurements above describe their own builds; current scope and paired feedback measurements are in [retained light-tree uploads](native-light-tree-uploads.md). Changed rows, lookup arrays and initial/nonuniform construction still need profiling.
