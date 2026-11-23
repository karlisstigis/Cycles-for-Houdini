# Draft: explicit priority-based nested dielectric media

Status: local design/review packet, not transmitted. Source ancestor:
`74e9d9e48f86330987d7392166a51b3652d7c557`. Primary diffs are 0074 and the
spectral-medium portions of 0078; original bytes and hashes remain locked.
The maintained fork classification reflects a new transport/API feature requiring
design agreement, not a statement that upstream would reject it.

## Proposed cover letter

Overlapping dielectric surfaces need a deterministic active-medium rule rather
than treating every geometric boundary as an isolated air/material interface.
The carried implementation adds explicit medium priority/state, tracks occupied
media across camera, surface, shadow, volume, SSS and MNEE paths, and forms the
interface IOR ratio from the two active absolute medium IORs. State allocation is
opt-in on GPU; the feature-off path must keep its previous behavior and memory
budget. Bounded state overflow is diagnosed rather than silently corrupting the
medium stack.

For dispersive media, store absolute wavelength-dependent IOR in the medium state.
A ratio computed too early cannot be reused as an absolute IOR when a later probe
enters another medium. Patch 0078 corrects that distinction through camera-origin,
SSS and MNEE/shadow probes. Its unrelated Blender 5.3 Quaternion/attribute/shadow
result type adaptations must be extracted into a separate compatibility commit.

## Proposed review series

1. Specify the scene contract: enable option, medium identity, signed/relative
   priority ordering, tie behavior, initial camera medium, overlap and removal
   semantics, and the exact bounded-overflow policy. Preserve upstream defaults.
   Agree this contract before proposing the host's current private sockets.
2. Introduce the shared medium-state helpers and CPU reference behavior from
   `kernel/integrator/nested_dielectric.h`, including stack update/selection and
   tests for entering/leaving overlapping surfaces in every order.
3. Integrate closest-hit/surface/volume-stack and shadow transport. Distinguish
   null boundaries from scattering events and retain alpha/visibility semantics.
4. Add camera-inside-medium, SSS and MNEE probing. Keep each path's state ownership
   explicit: a probe must not overwrite its parent path's active medium.
5. Add GPU main/shadow path state and feature-dependent allocation. Extract
   `Device::mem_read_uint32_counter` and multi-device diagnostic reduction into
   a separate support commit; count a shared peer allocation only once.
6. Add absolute spectral IOR evaluation (`kernel/closure/dispersion.h`) and the
   0078 propagation correction. Keep wavelength state, interface ratio and
   throughput weights distinct. Submit analytic slab/overlap comparisons before
   performance claims.

The accepted two patch files span many transport paths; the proposed split is a
review plan, not a claim that arbitrary hunks are independent. Preserve original
provenance trailers during extraction and record range-diff against the accepted
fork. UI labels, worker snapshots, Houdini priorities and private logging remain
integration changes unless upstream chooses the same public API.

## Review questions

- Is priority per object, material or explicit medium, and how are ties resolved?
- Should overflow reject the render, fall back to a documented model or emit a
  bounded diagnostic? How does that apply to shadow and camera-initialization
  paths?
- Does the agreed model support nested volumes as well as surfaces, and how are
  thin/transparent boundaries and backfaces classified?
- How should SSS/MNEE retain the parent's absolute medium state while probing?
- Which GPU state specialization preserves feature-off occupancy and memory?

## Evidence and acceptance plan

Related source fixtures are `tests/houdini_nested_dielectrics_plumbing_smoke.py`
and `tests/houdini_spectral_nested_smoke.py`; inventory lists their exact hashes,
required arguments and assertion scope. Plumbing coverage is not a proof of
transport correctness. Port scene construction into upstream tests and compare
analytic air/glass/liquid slabs, camera-inside cases, equal/unequal priorities,
overlap enter/exit permutations, reflective/transmissive shadows, spectral slabs,
SSS, MNEE, motion and deliberate overflow.

Record feature-off Beauty and pass parity, CPU/CUDA/OptiX agreement within stated
tolerances, GPU allocation/occupancy with feature off/on, counter accuracy for
peer/multi-device allocations and restart behavior after overflow/cancellation.
Deliberately replacing an absolute IOR with an interface ratio must fail a nested
spectral test. Preserve seeds, wavelength configuration, reference method and raw
linear output; rendered plausibility alone is insufficient. Preparing this packet
did not run these new upstream/backend experiments.

Keep 0074/0078 maintained until the agreed upstream implementation and all
production backend/feature-off checks pass. Image Iris and production RLS must
remain supported with nested media where the current contract allows them.
