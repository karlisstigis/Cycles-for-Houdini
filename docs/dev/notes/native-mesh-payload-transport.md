# Native mesh payload transport and constant styles

Current protocol-48 per-field transport and shared-array ownership are documented in [per-field mesh updates](native-mesh-field-updates.md). Earlier measurements below retain their original scope; no older transport protocol is accepted by the current runtime.

Mesh payload reuse was introduced in protocol 46. Protocol 47
retained it and added unique physical-family ownership counters. An unchanged
mesh payload can be reused when only its material bindings change. Bindings
keep their own resource keys, palettes, face slots, instance-field layouts and
Cryptomatte identities. This reduces transport; compatible surface variants
also now [share physical meshes/BVHs](native-mesh-sharing.md).

## Ownership and validation

A packet can contain a full mesh body, refer to an earlier body in that packet,
or refer to a key/revision in the last accepted scene on the same channel.
Deleting that source in the new update is safe: references resolve against the
previous accepted scene before its owners retire. The receiver commits its
reference ledger only after the engine accepts the update.

Candidate buckets only bound the search. Exact bitwise field comparisons prove
reuse, including positions, topology, normals, UVs, Cd, named fields, motion,
nominal poses, Pointiness and subdivision topology/settings/creases. Changed
fields send a new body. Signed zero is retained. Triangle and subdivision face
bindings additionally have explicit default, constant and nonuniform encodings.

Decoded bodies own their arrays. The worker's accepted-source ledger reuses
the renderer's immutable source owners, avoiding an additional retained copy.
Initial updates and binding handoffs isolate mutable caller input. Derived
displacement and engine buffers never serve as transport sources. Camera and
object-only updates leave the mesh ledger untouched.

Missing/stale/forward references, wrong ownership, unsupported tags, truncated
packets and excessive expanded allocations remain explicit internal errors.
The decoder charges referenced mesh copies against its 8 GiB allocation budget.
Source recovery continues before these strict contracts; malformed transport is
not treated as an artist material warning.

There is one artist-facing persistent-worker IPR path. The encoder's optional
full-body switch exists for same-runtime measurements; there is no new viewer
or ROP execution mode. Ordered final rendering keeps its existing policy.

## Constant native stylesheet evaluation

An inherited constant primitive target can initially report a partial STY
match even though every primitive receives the same result. Previously the
adapter evaluated and stored that result separately for every prototype face.

The fast path matches one actual native primitive/point subject when all rules
are provably unconditional and constant. It retains native priority and saved
descendant behavior. Local stylesheet text, imports/dependencies, scripts,
target requirements, prebindings and subtargets retain the general evaluator.
Native subjects and closures stay alive only during host evaluation; only
owned values enter conversion and the worker.

On the recorded dense CPU fixture, split/edit host preparation fell from
4.642/4.621 seconds to 2.994/2.408 milliseconds. The before/after source manifests
differ only in `src/houdini/material_stylesheets.cpp`. These are two observations
per probe, not a statistical latency certificate.

## Verification and measured scope

Evidence: `artifacts/mesh-payload-evidence-20261020.json` and the source-audited
archive `artifacts/verification/mesh-binding-transport-20261020/index.json`.
Thirty-nine selected passing runs share the same compiled artifacts, kernels,
patches and immutable host runtime. Test-only staging proofs retain that exact
runtime. Historical failing iterations remain archived.

The fixture has one 1,048,576-triangle prototype copied twelve times, four
uniform emissive styles, 192x144 pixels, one sample and two worker threads.
Each device runs five states in both full-body and reference modes. Color/data
images match, Cryptomatte and material IDs retain exact bits, and independent
position masks check each instance's expected color.

| Device | Edit bytes, full / reference | Encode ms, full / reference | Decode ms, full / reference |
| --- | --- | --- | --- |
| CPU | about 75.62 MB / 50.18 KB | 36.38 / 4.29 | 31.72 / 10.30 |
| RTX 4090 OptiX | about 75.62 MB / 50.18 KB | 36.86 / 4.45 | 30.93 / 9.89 |
| RTX 3080 Ti OptiX | about 75.62 MB / 50.18 KB | 36.97 / 4.35 | 31.07 / 10.06 |

Timings are medians of two style edits per mode/device. Complete updates still
take roughly 8-9 seconds in this deliberately dense emissive fixture; lighting
work dominates. This table does not establish camera/material preview budgets.

Each device also passes seventeen binding/recovery states and five ordered
output frames. Cases include changed positions, nonuniform face assignments,
missing/recreated materials, ignored exports, exact material mattes and pass
IDs. Both viewers pass fourteen live states, six queued navigation callbacks,
retained prototype cooks, camera/object edits without geometry conversion, and
zero tracked geometry/device allocations after close. The native viewer checks
actual displayed pixels. A fresh, saved-and-reloaded courtyard additionally
passes twelve active camera edits, pause, Stop/restart and a fresh-render match.

Retained regressions cover packed Cd/numeric fields, shading/displacement,
imported and scripted styles, shutter/volume contexts, 100,000-object snapshots,
owned images, compact passes and strict internal contracts. Protocol checks read
the public declaration instead of assuming a historical protocol number.

## Remaining work

The earlier protocol-46 fixture used four physical meshes/BVHs and 197,304,432
tracked geometry bytes versus 49,326,108 for one style. Subsequent
[physical sharing](native-mesh-sharing.md) and
[shared family metadata](native-mesh-family-scaling.md) increments remove that
duplication for compatible surface bindings. Host snapshots and receiver mesh
bodies still retain per-binding arrays. True displacement continues creating
distinct shapes when required.

Primary validation/matching now uses
[cooperative acquisition](native-cooperative-acquisition.md). The recorded live
face-region heartbeat gap falls from roughly 0.7 seconds to 0.156 seconds after
pooling, then to 0.039 seconds after cooperation in Cycles Viewer. Initial
material-motion matching, general binding walks and structural shutter recipes
also resume across host turns; the dense motion profiles record roughly 23-26 ms
maximum turns. Distinct/deep packed hierarchies and many graph requests still need
profiling; startup and physical input are separate measurements. Dense emitter changes still need
lighting-update work. Mixed-device, long-duration memory, monitor/physical-input
and reproducible-release certification remain open. No Loop subdivision support
was added.

Shared packed leaf capture now uses immutable row tables and resumable source
groups. Transform-only shutter hierarchies also use templates; population,
placement and pure pose expansion run under converter ownership. The 1k/10k/100k
capture inventory and full million-triangle/100k-placement viewer checks are
recorded in [cooperative acquisition](native-cooperative-acquisition.md).
That capture increment improved host preparation before the subsequent physical
sharing work. Commit retirement is also
[implemented and scoped-tested](native-capture-retirement.md). Dense-emitter
synchronization, host body sharing and incremental family maintenance still
need work; extended mixed-device and memory certification remain separate from
the passing selected checks.
