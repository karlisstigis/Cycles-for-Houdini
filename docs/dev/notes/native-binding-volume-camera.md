# Native bindings, tapered volumes and sampled camera intrinsics

This document describes the October 21 binding/volume/camera increment for
native Windows OBJ/SOP rendering, worker protocol 51 and engine patch 0068.

## Point-subject bindings

Native GSTY point subjects now contribute to particles and uniform complete
polygon, Bezier and NURBS curves, including mixed SOPs and loose points.
Each containing primitive/source-point pair owns its native evaluation context.
Explicit id/name attributes are not required.

The complete native parent-to-point result replaces the earlier primitive
context rather than applying global rules twice. Native priorities are retained:
a primitive rule can mask a local point rule, and an explicit root/self rule
can mask a primitive rule even when its numeric value equals a default.
Primitive-to-point subtargets remain active. Direct typed-point root rules
pruned by primitive matching contribute through an explicit independent fallback
that fills fields not already claimed by the native ancestor context.
Path changes preserve existing override-reset semantics; separate material,
surface, displacement and material-parameter categories retain their ownership.

A whole curve must have one effective complete-key binding. A uniform higher
layer may mask lower variation; otherwise only the varying layer is omitted
with a source diagnostic. Supported lower bindings and the curve remain visible.
Incompatible shared-particle parents retain the existing first-owner result and
conflict diagnostic. Vertex stylesheet subjects and truly varying shader
identities along one curve are not introduced by this adapter.

Relevant source-point scope acquisition resumes through numeric cursors,
segmented owned queues and ordered merging; borrowed GA iterators do not survive
host turns. The 20 ms preparation budget and expired-turn owner limit apply to
this acquisition phase, not to complete cooking, styling or visible latency.
Unneeded mesh-only point styles and no-style fast paths remain quiet.

The existing native material_override owner policy permits 4,096 effective
variants and 8 MiB of owned literals per component. Excessive contributing
owner layers are omitted locally, retaining supported lower layers, paths and
geometry. This is not a universal cap on all stylesheet-generated variation.
Exact-boundary, over-boundary, cache reuse, palette contraction and repair cases
also retain an independent true-displaced control mesh.

## Tapered and frustum Volume fields

Finite nonlinear Houdini Volume maps are resampled to the existing affine
OpenVDB representation. Host capture freezes value-only SDK transforms and typed
copy-on-write handles after existing host-only synchronization. Sampling,
validation and sparse-grid assembly run on the converter without GU/OP access.
Affine fields retain their existing tile-aware fast path.

Float, integer and native vector data use Houdini's typed trilinear sampler at
target voxel centres. Integer truncation, source border behavior and finite
support remain explicit; a streak border cannot invent density throughout the
surrounding affine box. The oriented lattice preserves translation, rotation,
reflection and shear. Positive, zero-apex and signed/hourglass maps are covered.

This is a finite approximation. Positive tapers use half the smallest end-cell
spacing; collapsed and signed maps use half nominal spacing and omit only
exactly collapsed zero-measure planes. Each target is bounded to 2,048 cells
per axis and 67,108,864 voxels. A required larger lattice emits
volume.resample_budget and omits that field instead of silently coarsening it.
Invalid fields recover locally, empty valid density stays empty, and absent
usable density does not create a substitute medium. Independent fields and
other geometry continue rendering.

Successful conversion records informational volume.resampled diagnostics with
source/target dimensions and voxel count. These remain in diagnostics and
output metadata without inflating the warning count. Frozen data survives
recooks and source deletion; camera-only reuse retains converted fields and
their diagnostics. Changed fields still use the existing whole-field GPU
transport contract; this increment does not claim per-field GPU reuse.

## Continuous camera intrinsics

Protocol 51 and patch 0068 retain requested-frame optics separately from odd
3..129 chronological shutter samples. Continuous controls in the existing
perspective, orthographic and seven monoscopic panoramic models include zoom,
orthographic width, clipping and the applicable panorama lens/sensor/range/
polynomial parameters. Perspective interpolation uses tan(FOV/2). Existing
continuous depth of field, rolling shutter and exposure curves remain active;
ray-time projection and differentials use the same sampled optics.

Discrete projection/sensor-fit changes hold requested values with diagnostics.
Known unsafe source groups recover independently; unrelated valid lens/DOF and
camera/object motion remain active. Malformed internal tables and ownership
remain strict failures. Vector-only endpoint optics do not replace nominal
beauty optics.

Complete camera transform arrays describe chronological shutter poses; their
middle need not equal the requested-frame transform when the shutter is shifted.
Object/deformation transform contracts retain their own strict centre rule.
Window-coordinate displacement uses requested-frame camera dependencies and
does not rebuild solely because shutter optics change.

Camera bounds include sampled orthographic pixel aspect and perspective near
extent. Adaptive dicing uses a conservative sampled projection bound; animated nonlinear
polynomial projection uses bounded configured subdivision rather than an unsafe
linear estimate. Tiny-FOV estimates are capped before integer conversion.
Camera motion now initializes the volume stack even when nominal viewplane
bounds miss a local volume that contains the actual shutter ray origins.
An isolated before/after probe and nine-render tests compare this against eight
independent static poses on each selected device configuration.

## Verification and remaining boundaries

The final evidence (`artifacts/binding-volume-camera-final-evidence-20261021.json`)
records 74 accepted source-audited native runs: 44 feature cases,
four capacity cases and 26 affected regressions. CPU, RTX 4090 OptiX, RTX 3080 Ti
OptiX and mixed devices each pass headless rendering and both viewer workflows.
The feature cases contain 1,304 live image/edit states and 60 ordered
final frames. The separate curve capacity matrix contains 36 boundary/repair
states and twenty final frames. Camera controls include 52 exposure comparisons
against 2,496 independently rendered static cameras and 80 strict input
rejections; local-volume comparisons contain 36 renders with zero mean error.

The current staged resource root is `hosts/20261021T042433502936/resources`. Forty-four retained
worker results use exact production renderer/DLL/kernel/asset identity; four
camera-core and 26 regression runs qualify the corrected harness. Only the
proved harness source/binary, new-host recipe marker and plugin ResourcePath
metadata differ. Original manifests and source archives remain attached to
each result; superseded core results and failed development parents are
excluded from accepted counts. The positive-finite panoramic pole assertion is
unchanged: its raw fixture now initializes the active nominal camera fields.

The final binding, volume and camera audits are pinned in the evidence. They
check source priority, local omissions, repair/deletion, actual target IDs,
retained worker/cook/conversion invariants, final output and zero owned/device
allocations after close. The unchanged 68-patch replay and separate SDK/pure
references retain their original measurement boundaries.

Automated HOM edits, status and presented-image assertions establish the scoped
live workflows. They do not certify physical mouse input, compositor latency,
prolonged process/GPU memory, production preview budgets or clean-machine/public
distribution. Source/SDK checks stay separate from native rendering evidence.
Viewer interface redesign remains deferred. The broader finishing gates remain
in native-release-acceptance.md; no new renderer execution mode is introduced.
