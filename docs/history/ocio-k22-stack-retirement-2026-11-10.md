# K22 OCIO patch-stack retirement

The production source retires 0011 and 0012 under K22. Houdini supplies the
resolved OCIO filename, context and working directory to each renderer worker.
The resource gate exercised two simultaneous worker configurations, relative LUTs,
working-linear EXRs, numeric AOVs and independent viewer transforms on CPU and
OptiX before removing the global cancellation gate. A separate retained-session
suite covers rejected edits, changed and missing LUTs, old-frame output and
pause/cancel recovery. [Runtime contract and fixtures](../dev/notes/native-color.md)

The locked upstream base remains `74e9d9e48f86330987d7392166a51b3652d7c557`.
The 82-patch authoring tip is
`b2a9a712f76d938a6b7209324f219d5e223a149a`. All retained patch bytes and permanent
IDs are unchanged. The rebase of the 72 successor commits completed without a
conflict. Independent replay of all 82 patches reconstructs every engine source
file exactly, including ignored source extras: `artifacts/ocio-k22/replay/report.json`.
Triage, inventory and engine commit metadata also validate for all 82 entries.

The engine difference from the previous tip is confined to
`src/util/colorspace.cpp`, `src/util/colorspace.h` and `src/util/image_maketx.cpp`.
The first two return to stock Cycles configuration ownership, retaining the
RGB-only coverage correction from 0013. The third restores stock TX metadata
handling: 0012 had removed `chromaticities` and `acesImageContainer` after
conversion. Resource qualification must cover those tags and resulting working
space identity. Patch 0013 still preserves texture alpha during OCIO conversion
and prevents reuse of derivatives made by the former alpha-changing path.
No later patch changes these three files. Image Iris, DLSS Ray Reconstruction,
native Gaussian splats and every unrelated fix remain in the locked stack.

The private integration and engine archives retain the branch named in
`patches/triage.json` (`experiments/frozen-ocio-k22`): integration commit
`13c0ad5a5336bda19cdcc02acbedc148f61d9bc3`, engine commit
`46abe436f57f3006fa2c9e477c0a4cee3b53cd45`. The retired records preserve original
hashes, branch names and recovery commits. Public clones do not contain these archive objects. In the
private archives, recover both patches together with
their corresponding host ownership model; restoring the engine API alone does
not restore the complete runtime behavior.

On 2026-11-10 the official Cycles mirror's `release/5.3` branch resolved to the
current pinned base. Its latest stable tag was `v5.2.0` at
`3b97e190c5ff1a2ed2160d879ad5bf95bea7b8ba`. Blender 5.3 introduces the
native Gaussian splat support used by this integration. Keep the current pin
until an immutable stable 5.3 release is available. At that point, compare the
tag with the pin, replay the stack and qualify the result.
[Release status](https://developer.blender.org/docs/release_notes/compatibility/) ·
[Blender 5.3 rendering changes](https://developer.blender.org/docs/release_notes/5.3/rendering/)
A new upstream base expires all eight integrator/session-output grants in ADR
0009 even if their patch bytes replay unchanged. The same-base K22 retirement
does not change those grants. No external proposal or nightly job was created.
