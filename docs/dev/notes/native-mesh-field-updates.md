# Owned per-field mesh updates

The current development protocol is 48. Mesh updates can reuse individually
unchanged fields from an acknowledged resource or an earlier resource in the
same packet. A UV, Cd or named-field edit no longer repeats unrelated positions,
triangles, normals, shutter data, nominal poses or subdivision arrays.

Palettes, triangle/face material slots and instance attribute declarations keep
their own logical identity. Shader and Cryptomatte names are never borrowed
from a prototype. Shared physical geometry and local source recovery remain
subject to their existing validation and displacement decisions.

## Ownership and strict boundaries

Managed arrays use immutable sharing with copy-before-mutation. Every operation
exporting a mutable reference, pointer, iterator or vector marks that allocation
as exposed. Later copies clone exposed storage. This also handles references
acquired before a snapshot copy, including references retained through a moved
external vector; ordinary copy-on-write alone would not protect those cases.
Moves and swaps preserve the exposure history. Nested rvalue and iterator-range
imports are also treated as exposed, because moves can preserve pointers into
inner vectors. A standalone probe reproduces all three unsafe import forms
before the correction and none afterward. Unexposed owned fields can be
shared safely between independent immutable snapshots.

Only changed fields are inline. Named attributes retain current order and use
explicit unique source-name/index references; their values and shutter fields
have separate masks. Optional nominal and subdivision data carry explicit
presence and separate field masks. Clearing a field is an update.

Missing owners, stale revisions, forward references, ambiguous named references,
unknown mask bits, references without a base, truncated packets and allocation
bounds remain strict errors. The decoder bounds logical retained payload as
well as new storage, preventing a small reference packet from amplifying owned
resources without limit. Current builds reject old transport protocols.

Native capture can retain positions when both topology and a valid Houdini P
data ID are unchanged. It then avoids moved-point analysis and retriangulation;
changed/invalid IDs keep the existing capture and comparison path. Unchanged
triangles, normals, UVs and colors can retain managed storage. Source memory
accounting deduplicates managed allocations across retained mesh owners and
uses capacity; it does not claim to measure allocator RSS or GPU residency.

## Verification and measurements

| Configuration | Complete image: full / fields (ms) | Reduction | Encode: full / fields (ms) |
| --- | ---: | ---: | ---: |
| CPU | 463.5 / 307.6 | 33.6% | 79.4 / 19.5 |
| 4090 OptiX | 484.3 / 316.1 | 34.7% | 83.7 / 22.4 |
| 3080 Ti OptiX | 502.1 / 307.6 | 38.7% | 82.4 / 23.6 |
| Both OptiX GPUs | 500.9 / 310.9 | 37.9% | 79.9 / 22.5 |

These are medians over eight sequential field edits/repairs per mode and device.
The full-copy control uses the same compiled runtime and native capture cache;
it isolates wire/receiver ownership costs rather than representing an older
application build. Full packets are 157.35 MB; UV updates are 25.21 MB and
color/named-field updates are 37.79 MB (decimal bytes).

All 37 selected native runs pass on `hosts/20261020T170044415/resources`,
protocol 48, with the unchanged 62 engine patches and six kernels. Four paired
device configurations each cover 11 image states, independent color/UV/position
checks and unchanged data/Cryptomatte identity. Both viewers on both individual
GPUs pass 17 live states and six queued camera/object edits per workflow.

The regressions also cover local UV/Cd and named-field repairs, sampled fields,
independent and time-varying displacement, mixed mesh/point/hair motion, constant
White Noise attributes, tangent bases, original triangulation and polygon soups,
1k/10k/100k binding families, five-frame output and zero tracked closure ownership.
Unchanged P IDs, retained face counts and no repeated triangulation are checked
throughout field edits. Camera/object-only edits retain geometry-conversion counts.

Source-audited evidence (`artifacts/mesh-field-reuse-evidence-20261020.json`) and
archived runs (`artifacts/verification/mesh-field-reuse-20261020/index.json`)
retain all development failures. These include fixture/reporting/runner mistakes
and a mixed-device status observation that raced completed-frame delivery. The
final fixture waits for Complete on the same generation. The standalone nested
import probe preserves three failures before the actual ownership correction
and zero afterward; the native contract now covers six nested import/move forms.

The native ownership/transport contracts pass six fresh processes, each with
264 checks and 18 strict rejections. The dense paired fixture uses one
1,048,576-triangle prototype, twelve packed placements, two render threads,
192x144 output and one sample. The internal full-copy switch is a measurement
control in the same persistent-worker execution path.

The eight-observation medians do not certify production percentiles or physical
input-to-paint. Changed fields still require owned buffers and native extraction
can still recapture unchanged named fields. General capture/conversion copies,
incremental family/candidate indexing, remaining lighting/lookup costs and the
production/release gates remain open.

## Follow-up: hash-sensitive displacement

The subsequent dependency audit reproduced stale displacement when a used
attribute changed from positive to negative zero. White Noise hashes the float
bits, so numerically equal values can produce different heights. The retained
image differed from a fresh render by 0.160091 in the independent position pass.
A separate fresh shader-parameter test found that Cycles discarded negative
zero when assigning a socket whose default was positive zero.

Used field comparisons now preserve the authored bits, including UV, Cd, named
fields, pointiness, shutter samples and requested-frame recovery data. Shader
graph parameters and packed uniform attributes use the same rule. Patch 63
aligns Cycles socket setters/equality with the engine's existing bitwise hashes
and default-value comparisons; unused float3 padding remains ignored.

A stronger shutter test also reproduced numeric-equality removal of an
off-frame field that differed only by its zero sign. Native deformation capture
now removes UV/color/named shutter arrays only when their consumed bits match
the centre field. Unused field edits still retain completed displacement.

The final runtime `hosts/20261020T183752557/resources` passes 21 selected
source-audited runs, protocol 48, 63 replayed patches and the same six kernel
hashes. CPU, each individual OptiX GPU and mixed-device rendering each pass 56
retained/fresh/unused-field states, 33 field/shutter recovery states and five
ordered output frames. Invalid fields disable dependent displacement while
preserving the independent surface shader; cached warnings survive unchanged
captures and clear after repair.

Both viewers on CPU and each individual GPU pass 38 live states and six queued
camera/object edits per workflow, with stable worker identity and zero tracked
closure ownership. The perspective fixture changes the visible silhouette;
native viewer pixels and Cycles Viewer presentation generations are checked.
The socket/displacement contracts pass 180 checks in twelve fresh processes;
the ownership contracts pass 264 checks and 18 strict rejections in six.
Existing temporal displacement, mesh/point/hair field motion, constant-field
and tangent regressions also pass.

Displacement evidence (`artifacts/hash-displacement-evidence-20261020.json`)
and archived development runs (`artifacts/verification/hash-displacement-20261020/index.json`)
retain the original failures and fixture corrections. These 64x64 fixtures and
programmatic Houdini edits certify the stated contexts. They do not certify
physical input-to-paint, production percentiles, arbitrary scene-query
displacement or per-ray-time instance fields.
