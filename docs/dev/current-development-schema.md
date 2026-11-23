# Current development schema

The native plugin targets the current definitions. Earlier plugin versions,
saved layouts and obsolete parameter formats do not receive migration code.
Corrections may change schemas; generated examples and fixtures are rebuilt.
Regular save/reload, animation, expressions and device choices within the current
schema remain supported.

The October 20 cleanup removes the development-only `cycles_native` ROP and
keeps `cycles_ipr` as the artist render node. Houdini's required DSO version entry
now has its own plugin source. The ROP exposes one CPU/GPU mode, one backend and
physical GPU checkboxes. Scripts use
`cycles_houdini.device_selection.set_devices(rop, ids)`; the hidden single-device
parameter and its migration state are gone. The unused viewer denoising override
and interactive inheritance parameter are also removed.

ROP acquisition reads its authored current controls. Sampling, denoising, Film,
output, passes, AOVs, light groups, Cryptomatte, geometry defaults, stylesheet
selection and ROP camera policy have no missing-parameter defaults for earlier
definitions. Missing required configuration reports the parameter path/name and
requires recreating that ROP from the current definition. Optional Cycles
properties on ordinary Houdini objects and cameras retain their native defaults.

Pass indices, pass names, AOV definitions, light-group names and Cryptomatte
settings use native parser validation before reserving a worker generation.
Invalid pass edits retain the active generation, image/sequence, geometry
conversion count and worker identity. Existing cross-setting and engine
contracts still validate at capture/commit.

Cycles Light uses Point, Spot, Area and Sun, with area shapes in Shape. Camera
visibility and Cast Shadow directly control rendering and the native viewport
proxy. Old Disk/Distant type tokens, duplicate hidden controls and OnLoaded
migration are removed. Optional native-Houdini light properties still support
Houdini's own current light types.

Color Ramp stores RGBA. Native current RGBA references and whole owned RGBA
promotion work; an RGB-only reference receives local recovery, never an invented
opaque Alpha. Houdini's current `hou.Ramp` RGB editing API remains usable on the
native ramp; author Alpha through its four-component stops. Texture coordinates
use Mapping nodes. Old hidden per-texture transforms are removed. Sky Rotation
retains the supported native Y-up transform and direct solar-disc sampling.

The current ROP layout is authored from `scripts/rop-layout.json`; its layout
script and JSON participate in immutable asset recipes. No empty historical UI
containers or layout repair code are retained. The showcase's current and
adaptive HIPs are regenerated from current definitions and load without warnings.
The default launcher opens the generated adaptive file.

Verification covers CPU and both individual OptiX GPUs, CUDA/OptiX multi-GPU
selection/rendering, current save/reload and animation, material Mapping, RGBA
promotion and recovery, moving-light shadows, sky reference images and local
volume-coordinate recovery, and both live ramp viewers. Automated light-handle,
parenting and current-showcase workflows are included. Each native run has an
immutable source archive; earlier failed development attempts are retained.
See the evidence record (`artifacts/current-schema-evidence-20261020.json`).

The ROP contract follow-up (`artifacts/rop-current-contract-evidence-20261020.json`)
adds thirty selected passes across CPU and both individual OptiX GPUs. It checks
current layout/save/reload, raw and color-transformed EXR output, motion vectors,
denoising guides, AOVs, light groups, Cryptomatte, depth of field and stylesheets.
Viewer startup uses a newly built scene and checks invalid-device repair,
convergence, camera edits, pause/resume and Stop. Live pass switching retains
the rendering generation. Native IPR passes fourteen geometry/binding states,
six queued navigation callbacks, displayed-pixel checks and zero tracked
allocations after close. The Cryptomatte progressive test polls the current
worker's host status and checks the requested image generation.

This is schema cleanup, not completion of the production milestone. Remaining
work includes distinct/deep packed hierarchies and graph requests, incremental
family maintenance and incompatible-field candidate indexing, host body/field sharing,
lighting transfers, unresolved dynamic dependencies and remaining geometry
contexts, followed by extended memory/device/input/release certification.
Compatible surface-only physical mesh/BVH sharing is now
[implemented and scoped-tested](notes/native-mesh-sharing.md).
Shared family metadata also has separate
[1k/10k/100k core-resource and adaptive-variant checks](notes/native-mesh-family-scaling.md).
Protocol 47 retains the mesh payload contract and adds explicit unique ownership
counters; no previous-protocol compatibility path is added.
The subsequent [cached-interior retention](notes/native-light-tree-interior-reuse.md)
uses the same protocol and kernels with a fresh compiled host. It adds no artist
setting and preserves local source recovery and strict internal ownership.
Repeated-value stylesheet pooling is
[implemented and measured](notes/native-stylesheet-acquisition.md). Primary validation
and matching, material-motion probes, effective bindings and structural shutter
profiles now [resume across host turns](notes/native-cooperative-acquisition.md).
That increment has 59 selected passing native runs and retains strict ownership
checks. Its remaining blocking operations and certification limits are documented
separately. Healthy material renames reuse deformation samples; ambiguous
particle owners render with a diagnostic and can be repaired during IPR.

Successful publication now swaps owned state with
[bounded converter-owned retirement](notes/native-scene-publication.md), verified
through source removal, discarded edits, live repairs and final output.
Queued cancellation and close now use
[explicit owned retirement](notes/native-capture-retirement.md). A failed capture
requires a fresh session on Render; safe edit rejection and renderer-only
recovery retain valid captured state. No old capture API or execution mode is
restored.

The later packed acquisition increment replaces the bulk helper with owned,
resumable rows and binding trees; it adds no previous-signature alias. It has
48 selected passing runs on one compiled runtime, including both individual
OptiX GPUs, mixed rendering and actual 100k-instance viewer previews. See the
updated [cooperative acquisition record](notes/native-cooperative-acquisition.md).
Remaining raw-capsule cleanup and dense-emitter transform costs are stated there;
the native production goal remains open.

Three obsolete whole-scene rejection tests are removed. Current transaction and
individual-viewer fixtures verify strict internal rejection alongside local
source fallback and live repairs. Scene callbacks now detach on ROP deletion
and accept Houdini's file-event metadata; no previous-version alias is added.
See [scene callback ownership](notes/native-scene-lifecycle.md). The Sky Texture
numeric/direction source-recovery gap is implemented, and its physical-reference,
domain, analytic-light and individual-viewer tests are registered in the main
runner. See [Sky source recovery](notes/native-sky-source-recovery.md); malformed
internal graph contracts remain strict, with no compatibility path added.

The October 22 correction replaces the ROP's separate Width and Height with one
Resolution vector (`res`). Scenes saved before it load with Houdini's
"unrecognized parameter" warning and the default resolution. Convert them once,
in hython with the current package:
`scripts/run-hython.ps1 -HythonArguments @('scripts/convert-rop-resolution.py','<scene.hiplc>')`. It
reads the saved values, sets Resolution, saves in place and keeps
`<name>.pre-res.hiplc`; animated or expression-driven values are reported for
manual conversion. The example scenes are converted.
