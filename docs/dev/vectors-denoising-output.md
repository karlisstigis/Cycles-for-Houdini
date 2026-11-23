# Motion vectors, denoising data and image output

## Artist controls

Add **Data / Motion Vectors** under **Image Output / Passes** to export four float32 channels. Turn off
shutter Motion Blur to use them; the pinned engine cannot supply this pass while
integrating shutter blur. The checkbox is disabled and the pass omitted when
Motion Blur is enabled, while preserving the selection for later use.

Samples are explicitly evaluated at frame - 1, frame and frame + 1, using Houdini's
FPS. Both final and interactive renders use these animation samples, never the
previous viewport edit. X/Y are previous minus current raster position; Z/W are
current minus next raster position. Units are rendered pixels, with Y upwards.
The EXR writer flips image rows, not the vector coordinate convention. Compositors
may require a channel shuffle and Y/sign conversion. These are Cycles vectors,
not an advertised universal Nuke/After Effects vector format.

Object and camera transforms contribute. Geometry uses the ROP/object motion
method: Transform Only, Deformation, or Velocity. Velocity remains units per
second. Missing or invalid velocity retains the object with static deformation;
transform and camera motion can still contribute. Correspondence limitations
of deformation blur remain applicable. Volumes have no surface vectors. Changing
camera intrinsics across the sampled frames remains unsupported.

The **Denoising** entries in **Add Pass** independently export Noisy Beauty, Denoising Albedo,
Denoising Normal and Denoising Depth. Beauty denoising can be off. Guides remain
raw, filtered engine data; they bypass display grading and are never themselves
denoised. Depth is a filtered denoiser guide, distinct from the ordinary Depth
pass. With denoising off, Noisy Beauty aliases the raw Combined buffer.
Implicit denoising dependencies remain available for rendering and comparison,
but are excluded from EXR output unless their pass toggles are explicitly enabled.

Packed point `v` also supplies per-frame surface vectors without authored IDs or
shutter SOP cooks. Invalid velocity produces zero local motion for that instance
while retaining visible geometry; repairing it restores vectors. CPU/RTX 4090
checks independently expect (-3.2, 0, -3.2, 0) pixels for 2.4 units/second at
24 FPS, 128-pixel width and four-unit camera width. Sparse volumes still have no
surface-vector pass. See packed velocity evidence (`artifacts/verification/packed-velocity-20261017/index.json`).

## Resolution and output

The viewer status row shows **Mem** and **GPU** beside render time and samples.
Mem sums Houdini and renderer working sets (shared pages may be counted twice).
Each GPU gets its own value, separated by a slash; these values are not pooled.
Hover the readout for card names, individual process figures, budgets and scope.
The diagnostics menu's **Memory** checkbox hides or shows the readout; it is on
by default. First-image, cook, extraction and buffer diagnostics remain available
on the Samples tooltip rather than occupying the status row.
GPU usage includes only the active session's render devices.
IPR queries its render worker asynchronously; final renders query their
batch process. GPU identity uses the PCI address and Windows adapter LUID, so
matching does not depend on card names or enumeration order. The figures are
Windows process memory usage, including driver overhead, rather than total card
usage or exact physical residency. Unselected cards and Houdini viewport GPU
allocations are excluded. Missing counters are shown as unavailable, not zero.

The viewer status shows **Time** beside the render state and percentage, using
`MM:SS.cc` (or `HH:MM:SS.cc` for long renders). IPR uses the renderer's elapsed
time, which resets for each render update and stays fixed after completion or
Stop. Final animation renders also show **Last**, the previous successfully
rendered frame's duration. Final frame timing starts before scene preparation
and ends when its outputs are written, including time spent paused. Each frame
resets Time; the final duration remains visible after the sequence finishes.
Skipped outputs are not recorded as rendered-frame durations. Per-frame timings
are retained in the background job's `frames.jsonl` report.

**Image Output / Passes** uses one native Houdini multiparm. Add a Cycles pass,
enable it, and optionally change its Layer Name. Shader Color/Value AOVs and
Light Groups expose a Source Name; Cryptomatte exposes Levels. These entries
replace the previous checkbox/custom-AOV/light-group parameter schemas.

Each entry has a collapsible **Output** panel. **Separate File** excludes that
pass from the main Output Picture. Its File, EXR Compression
and DWA Compression Level apply to that file. An empty File inserts the layer
name before the main EXR extension. Matching paths combine the selected passes
in one multilayer EXR; conflicting file settings and duplicate layer names are
rejected before rendering. Precision is editable for color, numeric data and
Cryptomatte. Precision is independent for each layer, including in a
shared file; every pass defaults to float32 and permits an explicit half choice.
Cryptomatte Layer Name
renaming updates its numbered channels, manifest name and metadata namespace
without changing IDs. Only manifests for included Cryptomatte layers are saved.

For example, use main Beauty half/DWAB and a separate Cryptomatte entry with
float32/ZIP or PIZ. Both files come from the same completed native render.
Final ROP rendering, viewer clapper sequences, background/farm renders and the
viewer Save button share this plan. A frame completes only after all requested
files are written. Each file uses atomic replacement; this is not an atomic
transaction across multiple filesystem targets. Skip skips a frame only when
every requested output exists, otherwise it renders once and writes missing
files, preserving existing ones. Output-routing/encoding edits do not restart IPR.

**Common Settings** contains the base Resolution and Resolution %.
**Rendering / Sampling / Interactive Render** contains IPR Resolution %.
**Image Output / File** contains Pixel Aspect. Percentages apply to the base width/height, round down and
clamp each dimension to at least one pixel. IPR 0 follows the final percentage;
otherwise it is an absolute percentage of the base resolution, used in both
Interactive and Final viewer sampling modes. Changing the viewer percentage
never changes Common Settings Resolution %, which controls disk/farm/clapper
rendering. ROP % in the viewer explicitly follows that final percentage. Changing actual
dimensions recreates the session. Pixel aspect changes the camera viewplane,
EXR metadata and the viewer's display proportions.

**Image Output / File** provides:

- **Passes to Separate Files** (off by default) writes every enabled pass to its
  own EXR, leaving Beauty in the main output. It overrides individual Separate
  File toggles without changing their stored values; turning it off restores
  those individual choices. Newly added passes are included automatically.
  Existing per-pass paths, compression and precision remain effective;
  explicit matching paths can still group passes into one file. Disabled passes
  are not written. This output-only toggle does not restart IPR.
  Standalone single-pass EXRs use unprefixed component names (for example,
  `R`, `G`, `B`, `A` for a Color AOV). Multilayer and multipart files retain
  pass prefixes, as does Cryptomatte even with only one numbered layer.
  Standalone vector data uses RGB for the first three components; a scalar
  uses luminance Y. A fourth data component such as Motion W keeps its name
  rather than becoming coverage alpha. Multilayer/multipart component names
  remain unchanged. Numeric values, precision and color processing do not
  change; interpretation in the receiving application remains the user's choice.
- **Pass Folders** (off by default) puts the main output in `beauty/` and
  automatically named separate outputs in folders matching their Layer Name.
  For example, `$HIP/render/shot.$F4.exr` becomes
  `$HIP/render/beauty/shot.0001.exr`, with a separate Normal pass at
  `$HIP/render/normal/shot.normal.0001.exr`. Folder names use the resolved layer
  name, including custom names and the shared root for all Cryptomatte layers.
  Explicit per-pass File paths take precedence. This does not turn on Separate
  File; passes with it off remain in the main multilayer EXR. Missing folders
  are created during writing. The same policy applies to viewer Save, clapper,
  Render to Disk and farm renders. Toggling it does not restart IPR.
- **Multipart EXR** (off by default) writes Beauty and every enabled pass into
  one EXR, with each pass's selected compression, DWA level and precision. The
  first part is `Beauty`; later part names use each Layer Name. All numbered
  Cryptomatte layers for one entry stay in one part with its manifest metadata.
  Individual Separate File controls and paths are disabled and ignored while
  retaining their stored values. Passes to Separate Files disables and overrides
  Multipart EXR; clearing it restores the stored multipart choice. Part codecs
  remain explicit, with ZIP as each pass's default. Duplicate part names are
  rejected before writing. Pass Folders places this single output in `beauty/`.
  Viewer Save, clapper sequences and disk/farm rendering share this policy;
  toggling it does not restart IPR. The writer prepares headers first, assembles
  one part at a time and publishes the file only after every part closes.
- Linear multilayer EXR with independent half or full float precision per pass,
  including numeric data, raw guides and Cryptomatte. Each pass's **Precision**
  defaults explicitly to **32-bit Float** and is always editable, including in
  a shared single-part EXR. **Use Main Output** follows the main Precision
  exactly. The main setting controls Beauty. No pass type forces a channel type.
  Half reduces numeric accuracy and can corrupt Cryptomatte IDs; choose Float
  when exact 32-bit values are required. Encoding never changes retained source
  pixels or which passes receive output color-space conversion.
- Separate passes default explicitly to **ZIP** EXR Compression, for every pass
  type. The main EXR Compression controls Beauty; changing it does not change
  separate files' selected codecs. Optional **Use Main Output** follows the main
  codec and DWA level exactly, for every pass type, with no automatic fallback.
  Thus Beauty can use DWAB, AO can explicitly use DWAB and Cryptomatte can use ZIP.
  Every explicit codec choice is honored, including for AO, numeric data and
  Cryptomatte. Lossy encoding can alter numeric values and damage Cryptomatte
  IDs; ZIP/PIZ preserve exact values. A shared single-part file uses one codec
  for all layers; multipart files allow a different codec per part.
- Optional output color-space conversion from the active Houdini OCIO config; only color RGB is transformed, preserving associated alpha. Data, guides and Cryptomatte remain raw.
- None, RLE, ZIPS (one scanline), ZIP (16 scanlines), PIZ, PXR24, B44, B44A,
  DWAA (32 scanlines) and DWAB (256 scanlines). DWA Compression Level defaults
  to 45; larger values trade fidelity for smaller files. All ten codecs are
  selectable for any pass; the output writer does not reject lossy numeric or
  Cryptomatte files. Select a lossless codec (None, RLE, ZIPS, ZIP or PIZ) when
  exact values are required. Unknown codecs still fail explicitly and preserve
  any previous output file.
- Overwrite, Skip Existing Frames, or Report Error for final output. Skip requires a regular, readable EXR with matching dimensions, data/display
  windows, channels and the complete multipart part set. For each visited
  frame, Skip checks chunk extents and decodes every flat level-zero part in
  scanline bands or tiles. Foreground validation runs off the Houdini thread and checks for Stop
  between reads. When all files exist and strict mode is off, Skip normally
  validates once and creates no render session. Strict or partially missing sets
  go through the writer, which validates an existing file again to cover
  concurrent changes and publication races. Other formats retain their header
  checks. Invalid existing outputs
  fail before writing any missing members of a partial output set. Valid existing
  members are preserved. Error and Skip use atomic no-replace publication, including
  files that appear while encoding. Overwrite intentionally replaces them.
- Interrupt or Cancel during a foreground write stops new file and frame
  requests. An already-issued file may finish atomically after Houdini returns
  control. Subsequent final renders in that Houdini process wait interruptibly
  for its acknowledgement so a late write cannot replace output from a retry.
- Fail on Missing Render Inputs is off by default. Enable it for final disk/farm
  renders to reject recovery diagnostics before publishing any output. It checks
  current inputs even when Skip could otherwise reuse every existing output.
  Strict Skip also requires certified clean provenance on every existing EXR
  part. Old fallback pixels and external/legacy files without certification
  require Overwrite, including files that win a concurrent publication race.
  Source diagnostic records retain their frame, source path, code and action in
  the sequence/farm report, including earlier frames and reused output metadata.
- Normalized Left/Right/Bottom/Top render region. Crop Output to Region saves a
  smaller image at origin; otherwise EXR preserves the region offset and the
  full image display window. An explicit ROP region takes precedence over the
  camera crop in SOHO. Interactive viewer regions remain session overrides.

The output path must end in `.exr`. Writes use a temporary file beside the
destination and replace the destination only after encoding succeeds. This
milestone does not introduce PNG/JPEG/video encoding, display/view baking, tile-file
rendering or distributed file reservations.

## Cycles viewer

**Interactive** chooses the ROP's independent Interactive Render quality settings;
turning it off chooses Final Render settings. **Denoise** and the editable
**Resolution** percentage edit the selected profile, shared by viewers. Auto
separately controls scene following. ROP % in the interactive profile inherits
the final resolution percentage; final quality requires an explicit percentage.
Existing denoiser selection, device, guide and quality settings are respected.

The **Keep Image Size** icon beside Resolution preserves the displayed image size
and pan across resolution changes. Zoom then refers to the resolution visible
when enabled, or to the first received image if enabled before rendering. Turning
it off preserves the current view and returns zoom to native render-pixel units;
choosing 100% maps one render pixel to one physical display pixel. Fit continues
to fit the image to the window. These controls do not resize saved output files.

The chain between viewer Resolution and Zoom optionally matches the IPR buffer
to the displayed footprint. It uses the ROP Width/Height as a fixed reference:
50%, 87.5% and 176% zoom request those same fractional resolution percentages;
Fit follows the canvas size and screen density. The viewer's existing maximum
is 400%. Updates wait 300 ms after navigation settles, and unchanged pixel
dimensions do not restart rendering. Linked mode holds Keep Image Size on and
disables that button with a neutral gray background. Unlink restores its prior
setting and retains the last render resolution without restarting. Stop, Pause,
snapshots and final renders do not resume merely because the view changes.
Only one viewer can drive a given ROP's linked resolution at a time.

A viewer-selected render region follows resolution changes proportionally.
Its original pixel bounds and image dimensions remain the reference, so a
zoom-out/zoom-in round trip does not accumulate rounding or expand the region.
Resetting Region discards that reference and restores full-frame rendering.

The continuous control group is ordered Keep Image Size, Resolution, chain,
Zoom, 1:1 and Fit. Linked mode and the manual size-lock preference are saved
with the HIP. Viewer resolution is independent of disk/clapper output settings.

**Show Noisy Beauty** beside channel selection switches between retained noisy
and denoised pixels; it does not restart rendering or change ROP settings. It is
available on live Beauty when a denoised comparison exists. **Refresh IPR** applies
the latest scene even with Auto off. Render mode stays active after sampling
finishes, so Render stays disabled and Pause/Stop stay available until Stop.

## Verification

`tests/houdini_vectors_output_smoke.py` checks known pixel displacements,
transform/camera/deformation/velocity/packed motion, five-frame sequences,
independent guides, EXR type/value readback, pixel aspect, both crop policies,
final percentage, existing-file behavior and actual Qt viewer controls.
Certification is limited to the recorded CPU/OptiX runs, not arbitrary production
scenes or every topology-changing workflow.

Reference: [Blender pass documentation](https://docs.blender.org/manual/en/latest/render/layers/passes.html).
Engine semantics were inspected in the pinned `scene/scene.cpp`, `scene/film.cpp`,
`scene/object.cpp`, `scene/camera.cpp` and `kernel/camera/camera.h` sources.
