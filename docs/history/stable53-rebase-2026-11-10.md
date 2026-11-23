# Blender 5.3.0 stable engine rebase

The production engine now uses official standalone Cycles commit
`77edc33168528e06b35540452bce5cce6068c3aa`, verified against Blender's immutable
`v5.3.0` release commit `02f0899d78e374b63e7181d9061e1f7ab03419cc`.
The [source mapping receipt](../../patches/rebase/blender53/base-provenance.json)
records exact core/runtime directory hashes and the expected standalone build
and packaging differences. This supersedes the earlier same-day availability
assessment in the K22 retirement note. Windows dependency identity remains
`aa0c5db72fa9488bb685bdddbede780d19f483f5`.

All 84 active patches retain their permanent IDs and behavior contracts.
The rebased authoring tip is `e60784f1abfad2a435815e79f12812001f1d38e8`.
Image Iris, colored apertures, DLSS Ray Reconstruction, Gaussian splats, nested
media, RIS, retained light trees and all other existing fixes remain present.
The previously retired OCIO and exact ramp/curve patches remain retired.

Only two patches required substantive adaptations to the stable APIs:

- 0059 resolves private range-upload device pointer keys through the new locked
  `ptr_map_find` helper, preserving stable texture-cache concurrency protection.
- 0074 adds nested state to stable Cycles' exact allocation identity and memory
  estimate, preserves its free-before-estimation and path-count reset behavior,
  and skips nested state allocations while disabled. Its nested counter uses
  the same locked pointer lookup. Texture-cache allocation flags stay intact.

0057 required a context adjustment around `Object::update_motion`'s new argument;
its added material palette methods are unchanged. The additions and deletions
of the other 82 patches, compared per file independently of diff ordering and
metadata, are unchanged. Exported commit messages retain prior commit/patch hash
provenance and carry concrete Rationale, Contract and Tests trailers.
The [ordered replay mapping](../../patches/rebase/blender53/replay.json) binds
previous and new hashes and commits.

Independent replay and engine commit metadata validation passed for 84 linear
commits and all 1,055 source files, first in the isolated workspace and then in
`third_party/cycles`. Reports are under `artifacts/rebase53/independent-replay`
and `artifacts/rebase53/production-replay`. Source equivalence is separate from
runtime qualification; the build and targeted renderer checks are recorded by
the release approval workflow.

The canonical checkout and `houdini/production` point to the rebased tip.
The original engine tip `bff76ec5f4b10e6dda35f053454f06ba7d420a5f` is
preserved on a private archive branch. The
[previous source lock](../../patches/rebase/blender53/previous-source.lock.json)
and local `artifacts/rebase53/previous` snapshot preserve the old patch hashes
and bytes; the integration Git history preserves those same accepted files.
No installed runtime was activated by this source promotion.
