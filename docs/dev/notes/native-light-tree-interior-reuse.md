# Retained mesh-light interiors

Patch 58 reuses the prepared interior rows of an immutable cached mesh-light
tree when their complete output context is unchanged. Current instance roots
are always regenerated, including world bounds, energy, bit trails and reference
indices. This reduces repeated preparation inside the persistent worker; no
artist control or additional IPR execution mode is added.

The subsequent [patch 59 range uploads](native-light-tree-range-uploads.md)
replace the changed-array full uploads described below. This document retains
the patch 58 implementation and measurements as their own verified scope.

The verified runtime is `hosts/20261020T122826256/resources`. Thirty-two
selected passing, source-audited runs cover CPU, both individual OptiX GPUs,
mixed-device references/cancellation, both viewers on each GPU and saved-scene
loading. Four dense reference suites each pass forty matched-history image
comparisons and five ordered output frames. Core cache checks cover 713 block
and exact-context contracts; surface sharing retains 681 checks and 31 reference
images. Original and compact partial-buffer fixtures pass interruption, resize,
repair and explicit triangle-lookup retirement. Default retained cancellation
passes on CPU, both GPUs and mixed devices. Both viewers pass fourteen states
and six queued camera/object edits on each GPU. Saved-scene loading has no
callback dialogs and leaves saved files unchanged.

Measurements and selected runs (`artifacts/light-tree-reuse-evidence-20261020.json`)
and source/run archive (`artifacts/verification/light-transfer-20261020/index.json`)
retain the successful checks and all three unsuccessful development runs.

## Exact ownership

Each admitted mesh-local cache entry receives a monotonic identity. Clearing or
evicting the cache does not reuse identities. The retained descriptor stores no
source pointers or cache owners. It compares the cache identity, node start and
count, emitter start, object ID, primitive offset, visibility and triangle lookup
offset. Existing shape/emission/linking cache checks remain authoritative.

Reuse requires completed node, emitter and triangle lookup buffers, unchanged
sizes and valid allocations. The triangle lookup now follows the same retained
ownership contract as nodes and emitters. Its pending allocation flag is cleared
only after a successful upload. Disabling the tree frees all three arrays.

Descriptors are committed only after the complete update uploads successfully.
Cancellation leaves the completed-buffer flag false, so the next update prepares
all required rows. Linked trees do not publish reusable descriptors. The first
cache-hit preparation establishes the descriptor and still decodes the range.

Large inner ranges can then keep their interior node, triangle emitter and
triangle lookup rows. The ordinary preparation handles small trees and every
changed context. Shader identity, material palettes and live geometry remain
independent of this optimization.

## Verification and observations

The heavy profile fixture uses one 1,048,576-triangle prototype, twelve packed
placements, four surface palettes, two worker threads, 192x144 output and one
sample. It has 8,388,637 node rows; 8,388,600 interior rows can be retained after
the descriptor is established. The remaining roots/forest rows are regenerated.

Five warm post-descriptor observations per configuration give these completed
image medians in matched runs of the same compiled runtime:

| Configuration | Full preparation control | Retained interiors |
| --- | ---: | ---: |
| CPU | 616 ms | 169 ms |
| RTX 4090 OptiX | 685 ms | 235 ms |
| RTX 3080 Ti OptiX | 673 ms | 233 ms |
| Both OptiX GPUs | No matched control | 297 ms |

The initial pre-change profile separately observed roughly 270 ms warm assembly.
Later full-preparation control medians are about 449 ms, while retained assembly
is below 0.1 ms in this fixture. The reports preserve these separate runs; they
are not pooled into a production distribution or a universal speedup claim.

The same compiled runtime provides an internal full-preparation comparison
control. Small sequential timing samples are observations, not p95 acceptance
or universal artist response guarantees. Profile times include native capture,
transport, engine preparation and completed-image receipt; they exclude physical
input and GUI paint. The first descriptor pass remains a full preparation.

Default fast-path cancellation observes the profiling-only CH_LIGHT_TREE_BEGIN
marker before real emitter/cache preparation, stops the existing worker, then
requires invalidated ownership, a complete repair upload and resumed reuse.
No artificial delay is injected. A separate fixture forces full preparation to
interrupt actual partially rewritten million-node buffers, including resize.
Both require valid images and zero tracked ownership after close.

The first two development required-path runs retained no rows: triangle lookup
storage and its completion flag were missing from the contract. A later old
CPU timing test missed all three attempts because retained assembly completed
too quickly. Those results remain archived. The revised cancellation fixtures
retain the intended partial-buffer guarantee and also exercise the default path.

## Diagnostics and remaining work

CH_LIGHT_TREE_PHASES adds retained row/range counts, previous/current descriptor
counts and triangle lookup comparison state. CH_LIGHT_TREE_STORAGE includes
triangle lookup size/device ownership. The internal
CYCLES_HOUDINI_DISABLE_RETAINED_LIGHT_TREE_RANGES=1 control forces full decoding;
it is used for verification, without a viewer or ROP setting.

The changed node array is still uploaded in full: 671,090,960 requested bytes in
this fixture. GPU transfer time, other lookup uploads and cold emission-tree
construction remain required optimization work. These counters do not measure
physical GPU residency. The six kernels and protocol 47 stay unchanged; the
host integration uses a fresh immutable compiled runtime.

Host body/field sharing, Pointiness, incremental/incompatible-field family work,
remaining feature contexts and prolonged memory/input/monitor/device/release
certification remain open.
