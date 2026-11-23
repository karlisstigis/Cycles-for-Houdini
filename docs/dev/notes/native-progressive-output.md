# Correct progressive pass layout

## Focus picking before rendering completes - October 18, 2026

The Cycles viewer can pick focus while a long beauty render is still refining,
including when denoising is disabled. Picking requests only Position and Depth,
keeps one immutable buffer for the accepted physical render revision and refreshes
it when effective resolution improves. Ordinary beauty frames cannot overwrite it.
A pixel is sampled directly from its compact plane storage, using the same crop,
origin and nearest-cell placement as the displayed image. A changed camera or a
different displayed physical revision rejects the click until the current view is
available. Background clicks report no surface and preserve camera focus.

The ROP's saved Position/Depth pass toggles are unchanged. The tool's preview
preference does not itself restart rendering; enabling previously unconfigured
picking passes still uses the supported full host capture. Turning the tool off
retires the picking cache. Native multi-plane viewing and final output retain
their existing pass behavior. Short completed renders reuse their existing
Position/Depth output without restarting or encoding the same frame twice.

CPU, RTX 4090 OptiX, RTX 3080 Ti OptiX and 4090 1080p/4K development checks pass.
These use scripted canvas events, not physical-input or release certification.
See `artifacts/verification/progressive-focus-20261018/index.json` for the failed
baseline, exact tested sources and regression runs.


The camera-navigation corruption reported on 2026-10-12 is reproduced and fixed.
The captured symptom was repeated, vertically compressed scene strips while the
rest of the image appeared empty. This was not sampling noise, camera export or
adaptive subdivision. The ordinary half-float DisplayDriver route already used
the effective resolution correctly. The float OutputDriver route used for
denoising and selected AOVs did not.

## Cause and implementation

Cycles retains full-sized allocations while its navigation scheduler temporarily
renders a smaller packed image. `PathTraceWork::get_render_tile_pixels()` passed
the allocation's buffer parameters to the float accessor, which interpreted the
effective pixels using full-sized rows. Our integration enabled these reads for
progressive output, where the final-render assumption no longer held.

Recorded engine patch `0022-progressive-output-layout.patch` makes those reads
use the effective window and stride. It selects noisy or denoised buffer
parameters for the actual requested pass and assembles device slices using the
effective output width. `PathTraceTile` expands the small image to the existing
full-tile output contract using nearest sampling. This avoids interpolation of
IDs/data channels and leaves full-resolution output unchanged. A pass-specific
render-size query supplies accurate beauty-preview resolution metadata.

This keeps progressive rendering enabled. Depending on its timing estimates,
Cycles can show a coarse image or a full-resolution image with very few samples.
The adaptive turbine still retains tessellation while the mouse is held and
refines once after release. The fix is shared by native display and the panel;
it does not change execution mode or claim lower scene-conversion latency.

## Evidence

The independent quadrant fixture fails on the previous reader with denoising
enabled: its first preview returns black where a known red emitting surface
must be present. The regular DisplayDriver cases preceding that failure pass.
The failed reproduction is preserved as
`prototype-sync-CUDA_NVIDIA_GeForce_RTX_4090_0000_03_00_OptiX-20261012T210833204`.

The corrected fixture checks actual pixel contents during camera edits at
641×361 and a crop of 509×281 at (13,19), with denoising on/off and beauty/normal
selection. CPU, RTX 4090 OptiX and combined RTX 4090/3080 Ti OptiX runs pass.
The multi-device run observes reduced resolution in all four combinations.

The real Houdini camera-export workflow uses one panel and the million-triangle
FX courtyard with the independently subdivided turbine, true ground
displacement and denoising. In the checked GUI run, 60 continuous camera exports
produce 27 painted previews while the mouse is held. Tessellation counts remain
2 during the drag and advance to 3 after release. Captured coarse and low-sample
navigation images were visually inspected and have a single correctly composed
scene. The first attempt at this GUI assertion was too strict: it required a
coarse frame during the drag even when Cycles supplied valid full-resolution
one-sample frames. The revised test accepts both valid preview forms. A later
run on the packed acquisition build painted 41 previews, including 26
full-resolution one-sample frames, but no coarse frame. Its remaining GUI
assertion requiring any coarse frame was also incorrect and has been removed.
That failed test run is retained as
`prototype-sync-CUDA_NVIDIA_GeForce_RTX_4090_0000_03_00_OptiX-20261012T221544401`.
The independent pixel-content test still requires actual reduced-resolution
coverage; the artist workflow requires low-sample or coarse feedback during the
drag. This change does not modify renderer scheduling.

Frozen reports, source audits, manifests, the failed reproduction and screenshot
paths are indexed in `artifacts/stage1/progressive-layout/verification-summary.json`.
Focused regressions cover final denoising invariance, progressive AOV/native
delivery, material bindings and motion. This is a layout-correctness check, not
a new performance benchmark or long-duration certification.

The fix includes native binaries. Existing Houdini sessions must be restarted
to load it; fresh sessions use the staged development package.
