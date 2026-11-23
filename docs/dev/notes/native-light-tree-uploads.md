# Retained light-tree uploads

Patch 54 introduced retained device and host storage of the light-tree node and emitter
arrays across scene updates. A shading-field edit no longer frees and uploads
those large arrays when their rendered contents have not changed. This applies
inside the persistent worker without adding an artist setting or another IPR mode.
The later [interior retention increment](native-light-tree-interior-reuse.md)
also retains triangle lookup ownership and avoids preparing unchanged cached
interior rows. Changed arrays are still uploaded in full.

## Ownership and correctness

The engine still constructs current lighting bounds, weights, indices and linking
state. Each prepared kernel row has canonical zeroed padding and inactive union
bytes. An exact byte comparison can skip rewriting unchanged rows and, when every
row matches, skip the array's device copy. No probabilistic hash decides equality.

Comparison requires a completed previous upload, the same row count, an existing
device allocation and no pending reallocation. Final packed-instance reference
and bit-trail fields are included before comparing their root rows. Linked light
trees conservatively upload their specialized node arrays; their emitter rows
can still be compared.

Ownership is invalidated before construction. Interrupted preparation cannot
certify partially rewritten host rows as device contents. Stop is checked again
before uploading, and the next accepted update restores a complete upload before
reuse. A restart can first resume the previously accepted transaction, then apply
the newer edit; verification checks every actual engine update.

Disabling the tree consults the newly selected setting and releases both arrays
immediately. Removing all emission, resizing, structural edits and final closure
retain the existing allocation and retirement contracts. Object/light lookup
arrays are still rebuilt/uploaded normally. Triangle lookup rows can now retain
their completed ownership under the exact cached-interior context. Exact
comparisons do not suppress real light,
emission, geometry, visibility, displacement or linking edits.

## Scoped response measurements

The same ordinary native build was tested in two alternating runs per mode on
RTX 4090 OptiX. Each run has three warmup edits followed by eight measured edits.
The fixture uses four shared copies of a 1,036,800-triangle subdivided/displaced
mesh, 960x540 output, 16 samples and two worker threads. Viewer feedback/polling
is enabled; receipt is acknowledged without GUI painting.

| Measurement, 16 warm edits per mode | Full-upload control | Retained arrays |
| --- | ---: | ---: |
| First worker image, median / observed p95 | 203 / 215 ms | 144 / 167 ms |
| Completed worker frame, median / observed p95 | 214 / 233 ms | 167 / 187 ms |
| Node/emitter copy bytes per unchanged edit | 198,853,488 | 0 |

These times include native capture, preparation, transport, engine work and
worker image receipt. They exclude physical input and GUI paint. The small,
sequential samples are observations, not production latency guarantees. Earlier
runs without viewer feedback mixed preview and completed-image scheduling and
are retained separately; they are not combined with this comparison.

## Verification

The evidence (`artifacts/retained-light-upload-evidence-20261019.json`) selects
26 passing, source-audited runs with identical compiled/runtime artifacts:

- CPU, RTX 4090 OptiX and RTX 3080 Ti OptiX each pass 40 small fresh-reference
  comparisons and 40 dense retained-history comparisons, including Combined,
  Depth, Normal and Position. Each suite writes five ordered images.
- The same devices each pass 11 buffer lifecycle states, interruption of both
  same-size and resized million-node updates, 21 heavy retained-emitter states
  and 21 independent mesh/world/packed linking cases.
- Cache byte/entry limits, retirement and analytic light settings pass their
  focused checks. Exact buffer ownership verifies tree retirement separately
  from the replacement light distribution's allocations.
- Both live viewers separately pass 22 heavy-scene edit/repair states at
  960x540. Cycles Viewer repeats those states at 1920x1080 and 3840x2160. All four
  close with zero tracked device and owned geometry bytes.
- The live light workflow records 30 events covering picking, handle dragging,
  automatic image changes and the saved adaptive showcase.

Single live Cycles Viewer camera/object actions reach first paint in 54/182 ms
at 960x540, 182/322 ms at 1080p and 166/299 ms at 4K. These varied single-action
observations are not percentile distributions or physical-input certification.

The stale showcase used by the first light run contained a removed legacy Mix
node. Existing files were backed up before regenerating the owned example from
current source; the generator now creates a Cycles Light directly. The two
earlier lifecycle assertion failures concerned per-apply counter attribution
and replacement-allocation accounting. All failed runs remain in the archive.

## Diagnostics and remaining work

With `CYCLES_HOUDINI_PROFILE_LIGHT_TREE=1`, `CH_LIGHT_TREE_PHASES` includes
comparison, upload-request and completed-buffer-validity flags.
`CH_LIGHT_TREE_STORAGE` reports row counts and device ownership after retention
or release. `CYCLES_HOUDINI_DISABLE_RETAINED_LIGHT_UPLOAD=1` is an internal
full-upload comparison control, not an artist execution mode.

Changed-array subranges, lookup-map transfers and initial/nonuniform tree work
remain candidates for measured optimization. Host/wire field deltas, scene-query
displacement dependencies, physical input, monitor changes, long-duration
physical memory, mixed-device rendering and release certification remain open.

The later [bounded working-set increment](native-light-tree-compaction.md)
keeps four dense material-variant trees under the same cache limit with exact
blocks. It preserves preorder kernel rows and these upload contracts. The
subsequent [physical mesh/BVH sharing increment](native-mesh-sharing.md) shares
compatible surface binding storage. Its
[shared family metadata](native-mesh-family-scaling.md) now scales compatible
logical resources without copied maps. Changed-array subrange transfers and
incremental/incompatible-field family work remain separate targets.

Patch 58 additionally [retains exact prepared mesh-light interiors](native-light-tree-interior-reuse.md).
Thirty-two selected runs cover CPU, both GPUs, mixed-device references and
cancellation, both viewers and saved-scene loading. Warm post-descriptor image
observations are 169 ms on CPU and about 235 ms on each individual GPU in the
documented four-palette fixture. The node array still requests a full 640 MiB
upload on changed roots; partial GPU transfer and cold construction remain open.
