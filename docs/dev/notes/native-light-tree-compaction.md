# Bounded mesh-light working sets

Patch 56 retains large simultaneous mesh-light working sets as exact scalar
blocks. A transform update can reuse all their local trees within the existing
256 MiB cache budget. Smaller working sets keep direct scalar arrays.

The measured fixture has one 1,048,576-triangle native Grid, twelve packed
instances and four surface styles. Before this change, each local light tree
required about 144 MiB. The cache retained one tree and rebuilt the other three
after moving the instances. The final compact working set retains all four in
235,851,949 bytes, representing 603,979,520 decoded bytes. No capacity increase
or artist setting was added.

## Representation and ownership

Each block holds at most 1,024 records. XOR differences and variable-length
integer words reconstruct every floating-point and integer bit. Incompressible
blocks retain raw bytes. Bounds, weights, angles and bit trails are not
quantized. Headers check the record width and count before writing decoded
records. Invalid shapes, null buffers, malformed masks, overflow, truncation and
trailing bytes are explicit internal failures.

Tree records stream in postorder to avoid a full temporary decoded tree during
packing. The bounded record's node-type word also retains its original preorder
kernel index. Flattening therefore preserves ordinary kernel row order and the
existing exact upload comparisons. Current object IDs, primitive offsets,
visibility, sampling and first-instance reference transfers still come from the
live scene.

Retained entry accounting includes vector capacity and block metadata. Admission
reserves 512 KiB for preparation scratch inside the same payload/entry budget.
Flattening decodes at most 64 KiB per existing Cycles task thread. Allocator
overhead and the rest of the renderer are separate from this accounting.
Borrowed entries cannot be evicted until the builder and its parallel tasks
finish. Partial or cancelled candidates are never published.

Before borrowing begins, a newly pressured working set can invalidate direct
entries and admit compact ones. Completed compact entries remain usable when
pressure later decreases. Oversized decoded entries, byte-budget competition
and the 1,024-entry limit continue through normal rendering without admitting
an unsafe cache entry. There is no disk format or previous-version migration.

## Scoped response observations

The paired CPU fixture uses 192x144 output, one sample and two worker threads.
Times include native capture, conversion, transport, engine synchronization and
completed worker-image readback. They exclude physical input and GUI paint.

| Moving all twelve styled instances | Complete worker frame |
| --- | ---: |
| Earlier CPU runs | 5.69-5.79 s |
| Final CPU run | 0.607 s |
| Final RTX 4090 OptiX run | 0.664 s |
| Final RTX 3080 Ti OptiX run | 0.672 s |

These are individual observations, not percentile budgets. The GPU runs have no
paired earlier GPU measurement. An artist Houdini session could share the
machine during testing. Camera-only updates perform no light-tree rebuild, and
the prototype stays at one cook. Initial tree construction and emission changes
still require construction; they are not accelerated by retaining an unchanged
local tree.

## Verification

The evidence record (`artifacts/light-tree-compaction-evidence-20261020.json`)
selects twenty-two passing source-audited runs on
`hosts/20261020T084730648/resources`. Earlier development runs remain archived.

- 704 block/ownership checks cover 138 blocks, both scalar record widths,
  boundary lengths, signed zero, NaN/Inf bit patterns, arbitrary integer words,
  raw fallback, malformed metadata and protected output ranges.
- CPU and both individual OptiX GPUs each pass forty direct-cache and forty
  compact-cache comparisons against an independent uncached worker receiving
  the same edit history. Combined, Depth, Normal and Position are checked at
  unchanged tolerances. Each suite also writes five ordered images.
- Both representations pass eleven upload-lifecycle states and two interrupted
  updates on each device. The dense four-style fixture passes six states per
  device, including correct face colors, one prototype cook, no camera rebuild
  and no local-tree misses during the transform update.
- Twelve CPU budget states check production compaction, calibrated byte-budget
  eviction, pinned-entry protection, repeated declined admissions, oversized
  sources, 1,025 distinct sources, removal and final retirement. The ordinary
  retained-emitter fixture also passes twenty-one CPU update states.
- Cycles Viewer and native Houdini IPR separately pass fourteen live states on
  the 3080 Ti, including six navigation edits while updates are queued, source
  faults/repairs, current image checks and zero tracked resources on close.
- Fifty CPU strict/local transaction checks and the saved-scene load/render/
  replacement regression on 4090 pass. The latter exits without callback
  dialogs and preserves the saved files.

All 56 patches reproduce the pinned checkout. The six GPU kernels and worker
protocol 46 are unchanged; the host-side cache has a fresh compiled runtime.
Debug-only forced compaction and a smaller cache budget support the independent
verification; neither is an artist-facing execution mode or ROP control.

## Remaining work

The patch-56 fixture described above still used four physical engine meshes and
BVHs for four surface bindings. The later patch-57
[physical sharing implementation](native-mesh-sharing.md) adds object palettes,
slot-aware mesh-light lookup and shared retirement ownership; its paired fixture
uses one physical mesh. The later
[shared metadata increment](native-mesh-family-scaling.md) covers compatible
logical-binding scale and adaptive fan-out separately. Incremental/incompatible
field-family work, dense transform/emitter work, distinct/deep capture, remaining feature
contexts and prolonged memory/device/input/release certification remain open.

The later [prepared-interior retention increment](native-light-tree-interior-reuse.md)
can keep the exact expanded kernel rows after a valid descriptor pass, while
rewriting live instance roots. It preserves this bounded cache representation;
descriptors retain no source pointers or entry owners. Full changed-array GPU
uploads and cold tree construction remain separate costs.
