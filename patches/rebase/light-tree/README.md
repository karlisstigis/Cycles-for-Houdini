# Interactive light-tree updates at rebase

K32 keeps the complete feature. Replay it first. If conflicts make it impractical, the maintainer decides whether to use the smaller alternative. The fallback keeps the material semantics from 0057 and the two cold split loops from 0062. The original baseline is upstream Cycles `74e9d9e48f86330987d7392166a51b3652d7c557`. [Decision](../../../docs/decisions/0010-phase2-renderer-measurements.md)

## Carrying the complete feature

At the position of the locked 0062 patch, apply `0062a-split-loops.diff`, then `0062b-diagnostics.diff`. The first contains the algorithm and its two helpers. The second adds profiling, the diagnostic switch and reporting. Together they reproduce the three original Git blobs byte for byte, including the profiling header. The production source lock pins the previously qualified combined patch. These two files are its separated form for rebase review.

## Preparing the smaller alternative

1. Port the approved per-object material slots in 0057, including the object and shader-palette APIs. For the stock tree implementation, `reference-transplant-vs-pristine.diff` records the effective shader lookups and the per-palette subtree identity. It uses `pristine/` and `reference/` prefixes for files under `src/scene`. Its diagnostic print identified the benchmark arm. The companion `0057-fallback-light-flatten.diff` supplies the effective shader lookup used when flattening emitters into the device representation.
2. Port only the two split loops to the resulting builder. The stock builder has different surrounding code from the full patch stack. `build_split.py` contains the exact transformations exercised in both the stock and palette-aware control builds. The archived protocols describe the original comparison. With the original build directories and Visual Studio compiler environment available, `python patches/rebase/light-tree/build_split.py artifacts/phase2-decisions/light-tree` relinks the historical controls in that benchmark directory.
3. On the target release compare edits against fresh builds, including the indirectly lit receiver, shared geometry with distinct emission palettes, and CPU and OptiX. Preserve the distinction between edit-to-first-updated-image and completion of a whole frame. Return the conflicts, image checks, edit timings and cache memory to the maintainer before selecting the fallback.
