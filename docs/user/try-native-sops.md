# Try Cycles in native Houdini SOPs

The editable courtyard and development package are available on this workstation
for Houdini Indie 22.0.466 and RTX 4090 OptiX. Native OBJ/SOP rendering is the
current priority.
This is a development test drive. To install a qualified package into your
normal Houdini instead, use the Windows Setup described in
[packaging](../dev/notes/native-release-packaging.md#windows-installer).

From PowerShell, in the folder of your copy of this repository, run:

```powershell
& '.\scripts\launch-sop-showcase.ps1'
```

This opens a fresh Houdini instance, loads the example and starts the **Cycles
Render** panel. It uses the workspace package, prebuilt kernels and example OCIO
configuration. Each launch gets isolated preferences, worker logs and `last-launch.log`
under a unique `_work/test-runs/showcase-sessions`
directory. The artist does not
need a compiler or CUDA toolkit.

The launcher opens `sop-fx-courtyard-adaptive.hiplc` by default. Use `-SceneFile`
to open another scene directly, preserving its `$HIP` location. A companion
`showcase.ocio` beside that scene takes precedence over the generated default.
Saving in
Houdini updates that requested scene; the launcher does not rewrite it.
The default Houdini target comes from `source.lock.json`; the matching
staged manifest, package and kernels are checked before starting. Use
`-HoudiniRoot` and `-BuildDirectory` to select an explicit matching build.

The Cycles panel has **Render**, **Pause**, **Stop** and pass/view controls.
ROP and Camera dropdowns sit together on the top row. Device selection and render settings live
on the ROP. Automated viewport/capture diagnostics
deliberately open only a Scene Viewer, with no render session; that test window
is labeled **Cycles diagnostic: viewport only - rendering disabled**. The latest
five-frame heavy test verifies actual completed images in one Cycles panel and
saves the panel image. A panel turning into a viewport during an artist-started
render has not been reproduced.

## Use the viewer in your own Houdini session

The redesigned viewer is an actual Qt Python Panel, not an HTML window. It can
render your own OBJ/SOP scenes using the same package as the showcase. To launch
the current workspace build with an empty scene and fresh isolated preferences,
run this in a new PowerShell window, in the folder of your copy of this repository:

```powershell
& '.\scripts\launch-normal-build.ps1'
# Or open a new working copy of your scene:
& '.\scripts\launch-normal-test.ps1' -SceneFile 'C:\path\to\scene.hiplc'
```

These launchers select the current version's staged development package and
validate it against its build manifest. Each session has its own preferences;
the scene launcher copies the requested HIP again on every launch, so a previous
test copy cannot hide later source edits. Your active OCIO configuration stays
unchanged. In the test copy, `$HIP` points to its new run directory; assets and
includes stored relative to the source HIP need absolute paths or copies beside
the test HIP. To keep the source scene's `$HIP` location, launch an empty session
and open the original scene yourself, or use the showcase launcher's direct
`-SceneFile` behavior. Open your scene, use **Cycles Tools → Render ROP**, set
the camera and devices, then add a **Python Panel** and select **Cycles Render**.
Choose the ROP in the viewer and click **Render**. Start with one render viewer.
The panel can be docked or floated like other Houdini panes. Restart Houdini
after a new build so it loads the matching immutable host and Python sources.

Focus picking uses the crosshair icon; clicking a rendered surface sets focus
and enables camera depth of field. Toggling the icon only selects the tool.
Escape cancels it, Space temporarily switches to image navigation, and the
fullscreen icon shows the existing canvas with Escape returning to the panel.
Exposure, Gamma, channels, display transforms and image navigation are display
controls; they do not restart rendering. Render resolution and display zoom
remain separate controls placed beside one another.

## Persistent IPR worker

The panel and native IPR now use a persistent external renderer by default.
There is no execution-mode selector and no environment variable is required.
Matching viewers share one registry entry and worker. The old queued in-process
prototype has been retired; preserved comparisons are historical evidence.
See [the execution policy](../dev/notes/native-execution-policy.md).

Ordinary edits reset samples at a safe engine boundary while retaining compatible
resources. Continuous camera drags retain adaptive tessellation and refine once
after release. New previews are painted before another queued edit supersedes
them; display conversion runs on a background thread. The repeated heavy
comparison favors the worker's camera tail latency, while in-process rendering
has faster medians for several other edits. Houdini cooking and owned capture
still cause measurable pauses.

## What to try

1. The panel selects `/out/cycles_sop_courtyard`. **Render**, **Automatic updates**,
   **Pause**, **Stop** and the native camera chooser control its
   persistent session. The saved defaults are 960×540 and 128 samples.
2. Select the ROP in Houdini's native parameter editor. **Devices** contains
   **Render Device** (CPU or GPU Compute), a **GPU Backend** dropdown (CUDA or
   OptiX), and one checkbox per physical GPU. Check both cards to render together;
   changing the backend preserves checked cards. HIP files retain choices by PCI
   identity. **Refresh Devices** retains unavailable saved selections so they
   produce a clear error until corrected. No requested GPU falls back to CPU.
   Rendering contains Sampling and Denoising; Scene contains
   camera, geometry, lights and Environment. **Cycles Tools → Environment Light**
   creates a native HDRI light; Scene/Environment also accepts a Cycles World
   Material. See [the world/HDRI test guide](native-world.md).
   Expand the native Sampling/Denoising groups to
   access their controls. Denoising defaults off; its controls are shared with
   native IPR and batch. See [denoising behavior](native-denoising.md).
3. In `/mat/courtyard_noise_displaced_stone`, change **Large noise scale** or
   **True displacement scale**. Ground and rocks use renderer noise displacement
   with local UV coordinates. Dive inside for Noise Texture, the ramp and bump.
4. In `/obj/fx_courtyard/TWISTED_TURBINE`, edit **Blade twist**, **Sculpture Height** or
   **Blade count**. Ground divisions and rock, vegetation and spark counts have
   their own native SOP controls. Leave the viewport open while testing edits.
5. In the adaptive showcase, move `/obj/light1`, the Cycles area light. The
   world material `/mat/cycles_sky_world` also contains a Sky Texture with its
   own sun. That sun produces the dominant shadows and does not follow the
   area light. To isolate area-light movement, temporarily set the ROP's
   **Environment > World Source** to **Constant Color**, with a black background.
   Restore **Cycles World Material** afterward, or edit the Sky Texture's sun
   direction to move the daylight shadows. The older courtyard example uses
   `/obj/courtyard_key_light`. OBJ light selection is `*` by default.
6. Inspect Beauty, Depth, Normal, UV and Position. Change exposure or the OCIO
   display/view, take a **Snapshot**, or draw a **Region**. Uncheck Region to
   restore the whole image; Shift-drag also selects and checks Region. The
   checkerboard icon toggles the transparency background. Presentation controls
   retain the tracing generation. Supported data and lighting passes update progressively;
   noisy and denoised beauty are progressive when denoising is enabled.
7. To use Houdini's **Render View**, close the Cycles panel first, then select
   `/out/cycles_sop_courtyard` in Render View and start its preview. Use either
   render viewer for normal work. Opening both is an optional compatibility test:
   matching settings share the existing session/generation and scene allocations.
   Explicit Render starts a fresh render.
8. Use **Save linear EXR** after completion; it opens Houdini's file chooser and saves
   in the background, reporting completion or failure in the status line.
   Batch rendering uses the same ROP and frame range, with default output
   `$HIP/courtyard.$F4.exr`.
9. Use **Fit** on the right to keep the image fitted to the canvas, or select
   **100%** in the adjacent zoom dropdown for actual pixels. The dropdown offers
   12.5–800% presets and also shows the current fitted or wheel zoom. Wheel zoom
   anchors at the cursor; middle-drag pans; right-drag zooms;
   Ctrl+right-drag box-zooms. Space+middle/right also navigate. Shift+left-drag
   selects a render region, and Shift+right-click restores the full frame.
   Home or Shift+F fits; F shows 100%. Progress and memory messages occupy
   fixed-height rows, with full diagnostics in hover text. Navigation retains
   the tracing generation and survives scene updates.
   Region sits on the left, with Exposure and Gamma in the middle. The grayscale
   checkerboard button sits immediately to the right of RGBA. Use the
   labels to drag horizontally (Shift for finer changes), type a number, or use
   the native middle-mouse value ladder.
   These adjustments affect the displayed color only; data, alpha and linear
   EXRs retain their values. ROP and Camera share the selection row; compact icon
   actions, Interactive/Auto/Denoise and Resolution occupy the rendering row.
   Controls wrap in narrower panes. Render is disabled while render mode is active,
   including after sampling finishes; Pause and Stop are disabled after stopping.
   Refresh IPR applies current edits with Auto off. Stop holds automatic updates until
   Render is clicked again. The Live dropdown selects the current image;
   Snapshot adds frozen images to that same comparison selector.
   Use Camera Lock to disconnect viewport navigation from the selected render
   camera. Pick Focus prepares rendering guides; click a rendered surface in the
   current image to enable camera DOF and set its focus distance. Background,
   stale-camera images and coarse progressive buffers produce a diagnostic.
   Keep Image Size beside Resolution preserves display framing across resolution
   changes. The chain between Resolution and Zoom optionally matches IPR
   resolution to the displayed zoom or Fit size, including fractional values
   and zoom above 100% (up to 400%). It holds the size lock on while linked;
   unlinking restores the previous lock setting. The link is saved in the HIP
   and does not affect disk or clapper resolution. Color-management and
   diagnostics controls are in popup menus.
10. Select geometry and choose **Cycles Tools > Add Object Parameters**, or select a
    native light and choose **Add Light Parameters**. Properties are opt-in.
    **Create Light** offers dedicated Cycles light assets; **Remove Cycles
    Parameters** removes optional spare tabs. See [object/light controls](native-object-properties.md).

The example contains 54 genuinely packed bolted panels, 360 sparks, 265 open
curves, glass instruments, a twisted metal sculpture, masonry and sparse VDB
fields. Grid, Box, Sphere, Torus, Line, Copy to Points, Normal and Merge SOPs,
with compiled VEX wrangles, build the substantive geometry. Only
`PYTHON_TEST_ACCENT`, a five-face marker, is a Python SOP. The plume is an embedded
procedural demonstration field, not a simulated pyro performance fixture.

The renderer uses five geometry resources for 58 objects. Generated artifacts
are in `artifacts/examples/sop-fx-courtyard/`: `sop-fx-courtyard-current.hiplc`,
`courtyard-optix.png`, scene-linear float32 `courtyard-optix.exr` and JSON reports.
The current preview is 1280×720 at 256 samples. Scene preparation is recorded in
`scene.json`; rendering/output timing is separate. These are single example
measurements, not performance certification.

## Current limits and verification

The example now defaults to [native OBJ scene selection](../dev/notes/native-object-scenes.md),
using native Light 2.0 objects. Explicit-SOP mode remains available. Render SOPs
supply geometry directly through HDK without USD or Solaris. Point, Grid, Distant
and Sun lights have development tests; additional types and linking are separate
work. Unsupported configurations produce diagnostics.

Packed primitives render as instances (Pack, Copy to Points with *Pack and
Instance*, Assemble, nested packs). **Packed fragments** render too: the pieces
that Assemble makes with *Create Packed Fragments* on (its default) and that
Pack makes with *Pack by Name*, as in RBD fracture setups. Each fragment renders
exactly as the same piece would after an Unpack SOP, with its own transform
and pivot, velocity and angular velocity blur from the packed point (`v`, `w`),
attributes and material on the packed primitive, and attributes, UVs, normals
and several materials inside the piece. Fragments may sit inside packed
geometry and contain packed geometry. When only the pieces move between frames,
the renderer updates their positions without sending geometry again; when the
fractured source itself changes, the pieces are rebuilt. A fragment whose name
matches no primitive renders nothing and shows a warning naming it; the other
pieces keep rendering. A fractured source is held in memory twice while it is
rendered (Houdini's copy and one per piece), plus the renderer's own copy.

IPR geometry editing is supported and updates are coalesced. It still has UI
pauses: current measured artist evidence and upload behavior are in
[native interactive updates](../dev/notes/native-interactive-updates.md). Supported mixed-SOP
edits now retain unchanged components; conservative fallbacks remain for some
geometry and topology changes. Artist IPR uses one persistent external worker.
The reported light-drag segmentation fault has no matching reproduced stack;
parent inheritance and dynamic handle coordinate defects are corrected and
have separate focused regression coverage.

An archived full native run passed all **112 cases**, with a matching immediate
source/binary audit: 64 headless checks and 24 actual GUI workflows on each of
CPU and RTX 4090. It includes real light/settings, viewport and shared-viewer
workflows, checking an actual native IPR image pixel without restarting the panel.
The queued-worker headless CPU check exercises all three modes. The new cache
regression checks CPU and OptiX against independent triangulation and analytic
AOVs. The full GUI group uses the
synchronous baseline; focused OptiX and heavy GUI prototype checks are separate.
The earlier thirty-edit worker comparison recorded a 748 ms maximum warm UI
heartbeat gap. That predates the capture optimization and is not a current
latency guarantee. Earlier
full runs and corrected fixtures remain archived; current evidence
and remaining work are documented in implementation status.

Texture `op:` sources, more curve bases, displacement across sampled deformation,
motion-blurred lights, picking, the complete pass catalogue, broader AOV certification, long sequences
and production/performance certification remain open. Denoising is spatial;
broad image-quality and temporal certification are unfinished. Closing the last
viewer retires its renderer resources. There is no Redshift-parity claim.

To regenerate the example in a fresh process:

```powershell
.\scripts\run-hython.ps1 -HythonArguments @('examples/sop-fx-courtyard/build_scene.py', '--render', '--width', '1280', '--height', '720', '--samples', '256')
```

Regeneration writes the example file again, so preserve artist changes separately.
For the maintained selected checks on the current target, run:

```powershell
.\scripts\verify-native-release.ps1 -Cases tests/native-hardening-cases.json -Name native-hardening
```

This runs the cases explicitly listed in `tests/native-hardening-cases.json`
against the matching staged manifest. It is a focused gate, not full native
certification. The broad `run-tests.ps1` suite still contains fixtures that
launch a fixed historical harness; it only accepts the historical Houdini
22.0.429 build and rejects the current target before dispatching tests.

Development builds stage the native DLL under a versioned host directory so a
loaded Windows DLL does not block the next build. An already open Houdini process
keeps its loaded version; launch a fresh session to test a newer staged build.
# Adaptive showcase update

The launcher prefers `sop-fx-courtyard-adaptive.hiplc` when prepared. It is a new
copy of the earlier artist scene. Existing artist HIPs are preserved.
`/obj/courtyard_turbine` has opt-in Cycles object properties with renderer adaptive
Catmull-Clark subdivision enabled. Its source cage remains in the editable SOP
network. `/obj/ground` uses `/mat/courtyard_ground_noise`: UV-based 4D noise, a
separate geometric displacement height, and an animated fourth coordinate.
The renderer changes vertices; the ground stays flat in Houdini's raster viewport.
Set Ground Displacement Height to zero to compare.

Prepare that copy with `scripts/run-hython.ps1` and
`examples/sop-fx-courtyard/adaptive_scene.py`. The position-pass fixture
`tests/houdini_showcase_displacement_smoke.py` checks actual rendered height,
animation, unchanged SOP positions and turbine tessellation before making a
working-feature claim.



## Current generated showcase

The current generator creates a Cycles Light and current shader operators. Adaptive preparation now starts from the generated current file; an explicit `--source` can select another compatible HIP. It no longer silently prefers an old artist-fixed copy or suppresses operator-load failures. The launcher uses the generated adaptive file by default and has no fallback
to an older artist-fixed or current file. Explicit scene paths remain available.

Regenerate in fresh processes with:

```powershell
.\scripts\run-hython.ps1 -HythonArguments @('examples/sop-fx-courtyard/build_scene.py')
.\scripts\run-hython.ps1 -HythonArguments @('examples/sop-fx-courtyard/adaptive_scene.py')
```

These commands and the launcher share the authored default
`artifacts/examples/sop-fx-courtyard` directory; generated launch evidence and
preferences stay outside the checkout. For an isolated example, set
`CYCLES_HOUDINI_TEST_ARTIFACTS` to the same external directory before both
generation commands and the launcher (or pass that directory as launcher
`-Artifacts`). These commands replace generator targets; keep artist edits separately. The October 19 refresh preserved previous files under the example's `history` directory; its backup record (`artifacts/showcase-node-refresh-backup-20261019.json`) lists their hashes. Current rendering evidence and remaining certification are in [retained light-tree uploads](../dev/notes/native-light-tree-uploads.md).
