# Shared physical-family metadata

Compatible logical mesh bindings now hold one immutable member list and one
shared physical render state. The render state owns the primary variant,
private variant map, subdivision metrics and retained physical source. Aliases
keep their logical assignments and recovery context. They do not copy these
maps or the family list.

This follows the [physical mesh sharing implementation](native-mesh-sharing.md).
The engine remains at patch 57 with the same six kernels; worker protocol 47
adds explicit unique ownership counters. No artist control or execution mode is
added. A fresh immutable runtime is used for the changed integration ABI.

## Ownership and reconciliation

Physical snapshots retain the shared state rather than copying variant maps.
Retained families are classified once before sorting. Member lookup uses an
index, including when the former leader has been deleted. Compatible field
owners are aggregated across the family; exact shape, field-value, shutter and
UV-output comparisons still decide whether sharing is valid. Generic named
fields and corner colors retain their separate ownership restrictions.

Shader demand union uses indexed pointer membership while preserving duplicate
source slots in the leader prefix. Object users are indexed once per update,
including private displacement families. Physical metrics, motion flags,
retirement and byte counting visit each shared render state once. Deletion and
binding-key handoffs retain the state until every surviving user has rebound.

The worker status includes these counters in both Python and native replies:

| Counter | Meaning |
| --- | --- |
| `mesh_family_resources` | Unique immutable mesh-family member lists |
| `mesh_family_members` | Stored member entries, counted once per family |
| `mesh_variant_bindings` | Primary and private physical variant bindings, counted once |
| `mesh_metric_records` | Unique physical geometry metric records |

Mesh family members count logical mesh resources; point, hair and volume
resources are outside that count. One family can own many adaptive variants.
Mismatched worker protocols remain genuine failures.

## Verified scope

The final runtime is `hosts/20261020T112614459/resources`. Thirty-seven
selected passing, source-audited runs cover CPU, both individual OptiX GPUs,
mixed-device worker rendering, both live viewers and saved-scene loading.
Each individual device passes 126,060 scale checks and 28 visible image states;
the retained surface-sharing suite also passes 681 checks and 31 independent
reference images per individual device. CPU/both-GPU pose tests retain true
displacement and pixel dicing. UV, analytic lighting and paired heavy storage
checks pass on CPU, each GPU and both GPUs together.

Cycles Viewer and native IPR each pass fourteen live states and six queued
camera/object edits on each GPU. Closing releases tracked resources. Saved
scene loading produces no callback dialogs and leaves the saved files unchanged.
One failed initial compile concerned the new test harness's missing color-context
argument; its source and terminal evidence are preserved.

Evidence: measurements and selected runs (`artifacts/mesh-family-scaling-evidence-20261020.json`)
and audited source archive (`artifacts/verification/mesh-family-scaling-20261020/index.json`).

The core scale fixture uses 1,000, 10,000 and 100,000 distinct logical small-quad
resources, four surface palettes and per-object uniform fields. Half the sources
have an additional unused mesh field. It checks one shared physical mesh,
rendered material colors against independent object indices, sparse pose edits,
leader deletion, key handoffs, shape splits/repairs and complete retirement.
A separate 1,024-binding fixture creates 32 adaptive transform variants and
checks temporary fan-out, regrouping and deletion under the same ownership.

Each core-device run verifies 28 visible image states across 32 transactions and
round-trips the ownership counters through the worker wire format. These core
scale tests do not establish capture or UI latency for a 100k-object Houdini
scene, or capacity for 100k distinct shader graphs.

The existing worker and live-viewer checks independently exercise actual
Houdini parameter edits, geometry extraction, image display and Render/Stop.
They retain the heavy million-triangle shared prototype. Surface/volume source
recovery, displacement, UV output, packed fields, transport, data/Cryptomatte,
lighting and strict internal contracts remain covered.

## Response observations

The following are single CPU core-API observations at 128x96 and one sample.
They include the core update and completed image, excluding Houdini cooking,
worker process transport, physical input and GUI paint.

| Logical bindings | Initial image | Leader deletion | Shape repair |
| ---: | ---: | ---: | ---: |
| 1,000 | 34 ms | 31 ms | 30 ms |
| 10,000 | 208 ms | 127 ms | 110 ms |
| 100,000 | 2,318 ms | 1,477 ms | 1,435 ms |

At 100k bindings, sparse pose and pose repair complete in 633/605 ms, with no
geometry conversion. These observations are not p95 budgets or general artist
response guarantees. Whole-process working-set/private-commit telemetry is
retained in the reports and includes fixture, renderer, allocator and driver
state; it does not certify physical GPU residency or prolonged memory growth.

## Remaining work

Full regrouping still visits logical resources after source/binding/material
changes. Stable requirement/palette states may allow narrower invalidation.
Shape buckets with many incompatible field values also need separate profiling
and indexing; the compatible-family scale fixture does not certify that case.

Host source-body copies, automatic Pointiness derivation, cold emitter hierarchy
construction and changed-array lighting transfers remain optimization targets.
Distinct/deep capture, remaining feature contexts, prolonged memory, physical
navigation/monitor changes and reproducible release certification remain open. The full implementation goal is
still active.
