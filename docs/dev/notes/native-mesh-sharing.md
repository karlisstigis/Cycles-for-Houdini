# Physical mesh sharing across material bindings

Compatible surface material assignments now share the same physical Cycles
mesh and BVH. Artists can use different materials on copies of one prototype
without duplicating its render geometry solely because those bindings differ.
No additional artist control or IPR execution mode is introduced.

This increment was verified on `hosts/20261020T104729015/resources`, with worker
protocol 46 and engine patch 57. The later
[shared metadata increment](native-mesh-family-scaling.md) retains that engine
and adds protocol-47 ownership counters. These are scoped development checks,
not completion of the native production milestone.

## Logical resources and physical geometry

Logical geometry records retain their original assignments, source fields,
uniform instance attributes and recovery context. `geometry_resources` counts
those records; `mesh_variant_resources` counts distinct physical render meshes.
Four different surface palettes can therefore report four logical resources
and one physical mesh. `mesh_variant_bytes` also counts shared physical storage
once. Retained source bodies remain owned per logical record; this change does
not eliminate all host copies.

Shape hashes select candidates only. Full shape, topology, normal, motion,
subdivision, crease and source-slot comparisons determine compatibility. Field
unions preserve equal overlapping values, ownership and complete temporal
bundles. Uniform object fields retain their own context rather than becoming
prototype fields. Missing required attributes continue to recover locally;
another binding's valid fields must not conceal the missing source.

UV output is a rendering demand too. With the UV pass enabled, missing and
provided UV layouts remain separate when sharing would change that pass.
Pass-only edits refresh the retained shading state and regroup geometry.
Disabling the pass permits otherwise compatible field unions again.

True displacement and volume closures retain private geometry. Incompatible
fields and transform-dependent adaptive subdivision also keep the rendered
variants they need. Source capture and shutter acquisition can still be shared
when their source geometry is compatible.

## Object palettes, emitters and ownership

Engine patch 57 retains original triangle slot indices and resolves them through
each object's shader palette. Different assignments, including two slots that
initially resolve to the same shader, keep their meaning after edits. Surface,
motion, transparency, shadow, volume, bevel, MNEE and mesh-light lookup paths use
the effective object palette. Shader demands are unioned on shared geometry.
Object/material Cryptomatte identity remains independent of physical storage.

Mesh-light distributions and trees use the effective palette and linking
membership. Bounded cached working sets can retain several emission palettes
for the same mesh; borrowed entries remain pinned through joined preparation.
Restoring an earlier emission palette can reuse its retained entry.

Shader-only palette changes update their dedicated object fields after shader
preparation. They do not reset geometry offsets through a partial object update.
This fixes a worker failure reproduced during live emission edits. Normal object
updates still follow the complete object synchronization path. Invalid owned
buffers and indices remain strict internal failures.

Shared meshes are retired after all logical records, render variants and
objects have rebound. Deleting the prior leader does not delete a live mesh.
Scene close releases tracked geometry and device allocations in the verified
fixtures. This is separate from whole-process or physical-memory certification.

## Verification

The final runtime has 38 selected passing, source-audited runs. All 57 patches
replay against the pinned engine checkout. All six GPU kernels were rebuilt
and differ from the patch-56 baseline.

- CPU, RTX 4090 OptiX and RTX 3080 Ti OptiX each pass 675 core checks and 31
  independent image comparisons against the same runtime with sharing disabled.
- CPU, both individual GPUs and mixed-device rendering each pass 14 UV-output
  states and 16 analytic lighting/recovery states. These cover UV pass toggles,
  field repairs, emission edits, transforms, transparency, linking and deletion.
- Paired heavy storage tests use one 1,048,576-triangle prototype, twelve packed
  instances and four surface styles. Each device configuration completes twelve
  one-sample frames, preserves data/Cryptomatte identity and closes with zero
  tracked ownership. Camera/object edits perform no geometry conversion/upload;
  the source prototype cooks once.
- Cycles Viewer and native IPR each pass fourteen live states and six queued
  camera/object parameter edits on each individual GPU. Tests exercise actual
  Render/Stop controls and compare displayed image pixels.
- Retained transport, materials, packed colors, combined surface/volume recovery,
  emitter caches, strict contracts and ordered output regressions pass. Reopening
  saved scenes produces no callback dialogs and does not change the saved files.

Ten unsuccessful development native runs, including one rejected source audit,
and the initial failed compile remain recorded in their respective archives.
They are not included in the 38 selected passes.

Evidence: final measurements (`artifacts/mesh-sharing-evidence-20261020.json`)
and run/source archive (`artifacts/verification/mesh-sharing-20261020/index.json`).

## Storage observations

The paired fixture uses 192x144 output and one sample. Both modes run the same
compiled engine, with an internal comparison control disabling sharing in the
reference. Four logical resources and twelve objects remain in both modes;
physical meshes decrease from four to one.

| Configuration | Independent tracked bytes | Shared tracked bytes | Saved | Reduction |
| --- | ---: | ---: | ---: | ---: |
| CPU | 1,095,254,788 | 898,045,476 | 188.07 MiB | 18.01% |
| RTX 4090 OptiX | 2,297,891,384 | 2,027,579,800 | 257.79 MiB | 11.76% |
| RTX 3080 Ti OptiX | 1,882,653,704 | 1,612,342,120 | 257.79 MiB | 14.36% |
| Both OptiX devices | 922,954,500 | 851,577,188 | 68.07 MiB | 7.73% |

These are Cycles tracked device allocations. They are not physical VRAM or
whole-process RAM. Mixed-device accounting is not directly comparable to the
single-device totals. No render-latency improvement is claimed: warm camera and
object observations are similar to the independent control, while cold lighting
work remains substantial.

The live viewer timer observations have approximately 32 ms p95 intervals and
0.54-0.66 s maxima, including startup, cooking and UI operations. They do not
establish physical input-to-paint percentiles or absence of UI stalls. Small
one-sample completed-image observations likewise do not certify the prospective
production response budgets.

## Remaining work

The four-binding fixture above does not certify arbitrary numbers of distinct
logical material resources. The later
[family metadata increment](native-mesh-family-scaling.md) replaces copied
member/variant/metric structures with shared ownership and aggregate/indexed
lookups. Its separate core tests cover 1k/10k/100k logical bindings with four
surface palettes, leader deletion and adaptive variant fan-out. These are
distinct from the many-instance tests and do not certify 100k distinct shader
graphs. Incremental family maintenance and incompatible-field candidate scaling
remain. The original source audit is
recorded here (`artifacts/mesh-sharing-metadata-review-20261020.json`).

Host source-body copies, derived Pointiness work, cold emitter hierarchy
construction, changed-array GPU transfers and distinct/deep capture still need
measured optimization. Remaining feature contexts, prolonged physical memory,
physical navigation/monitor changes and reproducible release certification remain open.
