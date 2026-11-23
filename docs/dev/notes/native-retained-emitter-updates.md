# Retained mesh-light updates

Engine patch 46 separates **source data changed** from **device buffer needs a
copy**. Repacking a shared device buffer previously marked every contributing
CPU position attribute as edited. A small receiver or shader dependency change
therefore invalidated the cached light tree of an unchanged heavy mesh.

The table builder now passes the copy requirement explicitly. Required device
copies still happen, including combined point/hair position-radius buffers and
motion steps. CPU source flags retain their original meaning. Actual position,
topology, shader assignment, emission, transform and linking changes continue
through the existing invalidation policy. No artist setting is needed.

This changes scene synchronization in the persistent worker. It adds no viewer
execution mode and leaves strict scene/transport ownership contracts intact.

## Response observations

The paired RTX 4090 OptiX probe uses one retained 1,048,576-triangle prototype,
twelve copies, a 960×540 image, one sample and two worker threads. The prototype
is behind the camera and remains emissive. This stresses light sampling setup,
not representative FX ray-tracing cost. Each row below is one paired observation
including native capture, submission and complete worker-frame readback.

| Edit | Before | After |
| --- | ---: | ---: |
| Small mesh deformation | 2.525 s | 0.325 s |
| Restore small mesh | 2.910 s | 0.276 s |
| Recover unsupported shader output | 2.718 s | 0.328 s |
| Repair shader output | 2.709 s | 0.323 s |

Nested light-update time dropped from roughly 2.36–2.77 seconds to 0.16–0.17
seconds in those observations. The unchanged heavy local tree is reused rather
than reconstructed. Cached tree flattening and whole-array uploads remain a
measurable cost; the change does not establish zero scene/device traffic.

Separate warm tests run five camera, transform, geometry and material actions
per device. They record full readback distributions and engine/transport data.
The Cycles Viewer also records actual first paint after native parameter edits
in a separate 22-state Qt/HOM workflow at 960×540:

- Camera edits: 55 and 67 ms.
- Object movement: 231 ms.
- Geometry, shader and recovery changes: approximately 328–419 ms.

Paint records require a new render revision. Retained old pixels do not count
as an updated image. A 16 ms event-loop probe observed a warm maximum gap of
55 ms, including test validation work. These are scoped automated observations,
with small action counts, not physical-input or universal p95 certification.

## Correctness and ownership evidence

Host `20261018T233558847`, Houdini 22.0.429/Python 3.13, worker protocol 44,
46 pinned patches, five compiled artifacts and six unchanged kernels have
15 passing source-audited runs (`artifacts/emitter-dirty-evidence-20261019.json`).
The archive index (`artifacts/verification/emitter-dirty-state-20261019/index.json`)
retains the failing pre-fix regression and the rejected first timing-hook run.
The corrected hook handles optional startup metadata and turns callback errors
into test failures; final run output is checked for unexpected tracebacks.

CPU, RTX 4090 OptiX and RTX 3080 Ti OptiX each pass 40 lighting/cache checks
against fresh cache-disabled images, including five ordered output frames.
Receiver position and topology edits require every unchanged emitter hierarchy
to hit its cache, while real light deformation, material, displacement,
subdivision, transforms and visibility retain their established rules.

Each device separately passes 21 warm retained-emitter states. Both viewers
separately pass 22 live edit/repair states with one viewer open. The heavy source
cooks once, tracked geometry ownership returns to its initial size after repair,
and owned/device allocation counters return to zero on close. Cache byte/entry
limits, borrowed views, oversized sources and complete retirement pass their
11-case regression. These counters do not certify whole-process RSS or physical
GPU residency.

Additional regressions cover packed scalar/integer/vector fields, points/hair,
displacement and five-frame output on all three devices; mesh islands and local
subdivision recovery; and 104 volume-vector update/recovery states with five-frame
output on CPU and RTX 4090. All 46 locked patches reproduce the engine checkout.

## Remaining work

[The parallel-flattening follow-up](native-parallel-light-tree.md) reduces the
remaining measured CPU conversion cost, adds independent large-reference images
and records real nonlinear packed edits with stopped-renderer controls. Further
navigation bursts, initial construction, nonuniform recounts and whole-array GPU
uploads remain. Longer sessions, physical input, 1080p/4K, mixed devices and broader
production/release certification remain separate gates.

Reproduction uses `houdini_retained_emitter_update_smoke.py`,
`houdini_light_tree_cache_smoke.py` and the one-viewer mesh-island workflows in
`tests/`. Set `CYCLES_HOUDINI_ISLAND_WIDTH=960`,
`CYCLES_HOUDINI_ISLAND_HEIGHT=540` and `CYCLES_HOUDINI_ISLAND_TRACE=1` for the
Cycles Viewer paint trace. Normal runtime tracing remains off.


## Completed-upload ownership follow-up

[Retained light-tree uploads](native-light-tree-uploads.md) adds exact row comparison and device-storage reuse after the source-invalidation fixes above. Cancellation, resizing, light-tree disable, emission retirement and final closure have focused CPU/both-GPU checks. This does not introduce another IPR execution path.
