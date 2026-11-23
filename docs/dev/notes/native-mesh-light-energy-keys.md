# Exact mesh-light energy keys and packed binding reuse

Patch 61 keys each mesh-light palette slot by the exact finite energy consumed
by the light-tree builder, together with its emission-sampling flags. Hue-only
changes with identical consumed energy can reuse a valid tree while the actual
surface shader updates normally. Geometry, orientation, linking and existing
cache ownership/budget rules still participate.

## Exact consumer contract

The builder consumes `area * average(abs(emission_estimate))`. The cache and
builder now share the same energy helper, including an explicit float-rounding
boundary before area multiplication under the pinned Windows `/fp:fast` build.
Keys compare finite float bits; they do not use a tolerance, an aggregate palette
energy, or a hash as equality. Nonfinite energies cannot match. Per-slot ordering
and sampling flags remain part of the key, so exchanging unequal face energies
cannot match the old distribution. A previously cached exact distribution may
still be reused when it is restored.

Shading retains the signed RGB values. Only light-tree importance bounds use
the already existing absolute-energy definition. Shader identities and material
Cryptomatte names are independent of this acceleration key.

## Binding and source ownership

Live testing exposed an unnecessary conversion when surface colors moved between
existing packed owner groups. The normal exact-owner handoff remains preferred.
A second path may transfer an unclaimed, retiring mesh record to a new binding
when every owned body field and slot layout matches exactly. Each donor transfers
once, and cross-group shader pointers are never aliased. Both palettes must be
surface-only; true displacement, volume shaders, sampled displacement, adaptive
subdivision and changed fields keep their ordinary conversion checks. Caller
isolation is preserved by independent immutable accepted source owners.

Native capture can also seed a new material binding from an existing immutable
full-detail topology recipe. Complete topology/list data IDs, counts and all
subdivision settings must match. Mixed component selections retain their own
point/vertex/face provenance. Current positions and field IDs are still examined;
moved non-triangular faces retain the existing triangulation repair path. Cached
attribute diagnostics are rebound to the current source/resource, and shared
topology bytes are counted once. Position/field extraction and logical source
copies still exist; this is not complete host geometry-body sharing.

## Verification

The verified compiled runtime is `hosts/20261020T145213286/resources`, with
protocol 47, 61 patches and the same six kernels as patch 60. Forty selected
source-audited runs pass: 32 main runs plus eight additional ownership and
attribute-recovery runs. See the evidence (`artifacts/light-energy-evidence-20261020.json`)
and source/run archive (`artifacts/verification/light-tree-energy-20261020/index.json`).

- The energy-key harness passes 817 contracts, including exact finite keys,
  signed/channel permutations, one-ULP differences, flags and per-slot ordering.
- Four analytic/mesh lighting suites each pass 24 independently checked states.
  Four native/per-face suites each pass 17 cache-disabled image/data comparisons
  and write five ordered frames. Native and packed contexts use distinct exact
  energies so compulsory-miss checks cannot encounter a valid earlier entry.
- Four dense suites each pass 44 independent matched-history image comparisons
  and five ordered output frames. The native-to-packed transition now requires
  zero new source conversions and complete cache reuse.
- Both viewers on each GPU pass 16 states and six queued camera/object edits.
  The two final palette edits require unchanged geometry conversions, topology
  builds and triangulated-face counts; accepted images still match all expected
  packed colors and positions. The additional live UV/Cd repair test passes
  12 states with displacement branches kept independent.
- Additional physical-sharing contracts pass 791 checks and 36 independent
  images on CPU and each GPU. Caller mutation cannot change accepted owners;
  changed fields, true displacement and adaptive dicing retain required work.
- Seven lifecycle runs cover cancellation during real source preparation,
  partial writes, resize, compact caches, repair and retirement. Cache-budget,
  linking, strict CPU byte-range and mixed-device UV output checks also pass.

Profile, reference, lifecycle and viewer sessions close with zero tracked
geometry/device ownership. The final native process inventory is empty. This
is not a claim about untracked physical memory or prolonged production use.

Development failures are preserved in the archive. The initial GUI test found
an actual unnecessary body conversion, corrected by the ownership changes.
Later compulsory-miss assertions encountered valid retained native/packed cache
entries; the corrected fixtures now prove fresh unequal distributions miss and
unchanged packed bodies reuse. An incorrect wrapper report path was rerun with
the proper path, and a single-case wrapper array typo was corrected before any
native process launched. Image tolerances were not relaxed.


## Cold work and limits

New phase records separate source planning, extraction, local tree construction,
cache snapshots, transformed measurements and outer construction. The dense
profile uses one 1,048,576-triangle prototype, twelve packed placements, four
emission palettes, two threads, 192x144 output and one sample. These are scoped
observations, not production p95 or physical input-to-paint certification.

| Configuration | Cold image (s) | Local construction (s) | Cache snapshot (s) | Warm complete-image median (ms) |
| --- | ---: | ---: | ---: | ---: |
| CPU | 6.303 | 4.039 | 0.664 | 48.5 |
| 4090 OptiX | 6.686 | 4.191 | 0.673 | 47.9 |
| 3080 Ti OptiX | 6.486 | 4.023 | 0.671 | 47.9 |
| Both OptiX GPUs | 6.878 | 4.023 | 0.673 | 48.4 |

Warm medians use six sequential object observations per configuration. Local
construction accounts for roughly 83% of the cold builder time; source extraction
is about 0.145-0.157 seconds and snapshots 0.664-0.673 seconds. Cold completed
images still take about 6.30-6.88 seconds in this fixture. The next measured work
is recorded in the cold-build follow-up (`artifacts/light-tree-cold-build-followup-20261020.json`).


Cold construction, first descriptor preparation, nonuniform recounts, other
lookup transfers, logical body/field copies, incremental incompatible-field
families, remaining feature contexts and prolonged production/release
certification remain open. The artist-facing IPR path remains the persistent
worker. No new execution mode or legacy compatibility alias is added.
