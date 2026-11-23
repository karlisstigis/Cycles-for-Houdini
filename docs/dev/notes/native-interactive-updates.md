# Native interactive rendering: fixes and remaining work

Current artist IPR uses one persistent external worker. The synchronous/queued
experiments below are historical measurements. The current scheduling contract
and calibrated native-mouse results are in [execution policy](native-execution-policy.md)
and [performance evidence](../performance.md).

This increment addresses the reported light/settings crashes and viewport stalls in
native OBJ/SOP rendering. It provides tested fixes for reproduced faults and
measurements of the remaining costs. The original area-light segmentation fault
from Houdini PID 19708 has not been reproduced, so it is not a confirmed fix.

## Host scheduling and lifecycle

Native multiparm counts now invalidate scene capture. Houdini represents these
counts with folder templates, just like UI-only tabs and collapsible headings;
the old observer incorrectly ignored both. This could lose a light contribution,
AOV or light-group edit, then incorrectly accept a later camera-only capture.
The observer now uses `isMultiParmParent()` to distinguish real data changes.
Ordinary pane navigation stays quiet. Twenty-two native callback checks cover
count styles, insertion/removal, camera/object poses and UI-only controls, with
zero geometry cooks in the callbacks.

GUI shutdown remains asynchronous. The native world IPR regression now retains
the retiring session until it reports `closed`, then asserts zero owned geometry
and tracked device bytes. Its callback cleanup is idempotent, preventing the
second removal error in the reported traceback. Both CPU and RTX 4090 OptiX pass
seven heavy-scene states and this final ownership check. This establishes the
targeted close contract, not long-run or physical GPU memory certification. See
exact environment/lifecycle evidence (`artifacts/verification/environment-recovery-20261017/index.json`).

The current heavy animation and isolated baseline separate Houdini calculation
from plugin work. Displayed-SOP resolution takes about 35 ms median. Direct
conversion, retained triangulation, narrower component attribute dependencies
and bounded bulk reads reduce owned capture to 250 ms on already cooked
geometry with no Cycles session or GPU upload. Earlier frozen builds measured
310 ms after direct conversion and 677 ms before it. Changed
conservative IDs require exact connectivity comparison; moving quads/n-gons
still retriangulate when needed. Host capture, remaining copies and safely owned background
preparation remain targets. Native
viewport redraw and scheduling have their own costs; combined heartbeat gaps
must not all be attributed to the renderer. See [timing scopes](../performance.md)
and [task-specific execution policy](native-workflow-policy.md).

The richer artist workflow reproduced `Infinite recursion in evaluation` while
changing the turbine SOP with a native Scene Viewer open. The panel's Qt timer
could initiate scene capture inside a nested host event/cook. Render button clicks
and automatic panel updates now queue a public
[Houdini post-event callback](https://www.sidefx.com/docs/houdini/hom/hou/ui.html).
Render/capture and registry polling have reentry guards. Scene capture remains
in Houdini's supported host context; render threads consume owned geometry.

Panel updates wait for 80 ms of quiet, or 250 ms of pending edits, before requesting
capture. These are scheduling thresholds, not bounds on total latency. Obsolete
edits are coalesced and image generations identify the current result. Geometry
editing during IPR remains supported.

Native IPR can acknowledge an edit before the panel's timer sees it. The panel now
consumes that acknowledgement, preventing its stale local notification from
restarting a render after native Stop. The denoising UI regression requires a new
generation in `rendering` or `complete` state on restart. Its earlier predicate
incorrectly accepted a retained cancelled frame; that older result is not restart
evidence. The stricter targeted run is
`artifacts/stage1/gui/denoising-native-restart-generation/`.

The [queued-update and persistent-worker experiment](native-worker-prototype.md)
is now implemented and measured on a scaled native courtyard with 950,906
triangles and 352 objects. Ordinary edits in both queued modes avoid full session
cancellation; owned desired snapshots coalesce safely and commit at a render
iteration boundary before sample reset. The worker owns the actual renderer/GPU
scene. This experiment preceded adoption of the persistent worker for artist IPR.

Capture and CPU display work can still delay interaction. The earlier thirty-edit
experiment used the preceding copied-detail converter. Its mesh deformation
host apply took about a second in queued modes. Thirty-edit
median canvas arrival was 752 ms synchronous, 764 ms queued in-process and
812 ms external. Maximum warm Qt heartbeat gaps were 683, 869 and 748 ms.
These exploratory runs establish working persistent updates, not a speed win
or solved viewport lag. In-flight work may finish; only pending state is replaced.

### Interrupted image work and diagnostic snapshots

Before this increment, normal cancellation could still read a full set of
output planes that the generation filter subsequently discarded. The recorded
pre-guard GPU probe measures about 10–20 ms of such output work per edit.
Interrupted output callbacks now reject it before allocating/reading passes;
display callbacks reject stale data before half-to-float conversion. Their
generation checks remain authoritative. The engine's earlier display-buffer
copy still occurs; no claim of zero GPU-to-host copying follows.

Diagnostic collection also used a blocking scene lock, introducing 270–282 ms
of observer delay after some geometry edits. It now attempts the lock without
waiting and reports `engine_statistics_pending` if engine timings cannot be
read safely. This avoids a diagnostic pause; it does not make ordinary scene
application, status control or display conversion universally nonblocking.

The earlier artist evidence records skipped obsolete output/display work on both
devices and diagnostic attempts below 0.001 ms. A genuine GPU edit still waits
506 ms joining cancellation in that synchronous baseline. The queued prototypes
now avoid the full-session join, while safe-boundary waiting, host capture and
display costs remain measured optimization work.

### Opening a matching native IPR view

Native IPR joining an existing panel used to apply an identical scene by cancelling
and restarting Cycles. A recorded RTX 4090 join spent 525 ms in host apply,
including 513 ms waiting for cancellation, although geometry had not changed.
The SOHO adapter now requests reuse specifically when a native view attaches to
an existing panel session. Ordinary Render remains an explicit fresh generation.

The core validates the entire transaction before deciding to reuse. It compares
owned camera, object, visibility, identity, material, light, crop, film, motion
and denoising definitions. Geometry payloads or resource deletions prevent reuse.
Texture-content changes and a pending render-color epoch also prevent it; an
unchanged filename alone is insufficient. Cancelled or failed sessions are not
reused. The retained render keeps its generation, samples, frame and diagnostics.
`unchanged_reuses` records successful reuse. Existing generation diagnostics are
not new cancellation or upload timings for an attachment.

The 23-group CPU/OptiX regression checks completed/live reuse, core pause and
changed-state rejection. The real GUI workflow additionally verifies the known
sky pixel in native IPR, the retained panel generation and a subsequent SOP edit
that advances the render. Paused native-view attachment is not separately
certified. In the earlier 110-case run, attachment apply took 24.4 ms on CPU and
9.4 ms on RTX 4090; actual native pixel availability took 991 ms and 433 ms.
The earlier and current timings are individual observations, not paired
performance certification. No renderer or scene is duplicated for this view.

## Native Houdini controls

The image canvas and render toolbar are a custom Qt Python Panel. ROP/camera
selectors use native `hou.qt.NodeChooserButton`; menus use `hou.qt.ComboBox`.
Render settings and device selection are edited on the ROP in Houdini's native
parameter editor. The panel places Camera beside ROP, uses an icon for the
checkerboard, and restores full-image rendering when Region is unchecked.
EXR saving uses Houdini's file chooser.
Headless tools use small Qt fallbacks because `hou.qt` is unavailable in hython.

The ROP groups supported controls into Rendering, Devices, Scene, Geometry, Motion Blur,
Volumes, Film, Materials, Output and Performance. Sampling and Denoising follow
the organization in
[Blender's Cycles UI](https://raw.githubusercontent.com/blender/blender/main/intern/cycles/blender/addon/ui.py),
with Houdini-specific source/light controls. Parameter names and scene values are
preserved.

The small gear belongs to Houdini's
[Python Panel interface editor](https://www.sidefx.com/docs/houdini/ref/panes/pythonpanel.html).
The panel hides that optional editing toolbar by default; automated tests expose,
open and dismiss its menu during rendering. This is tested behavior, not proof
that the reported gear crash has been diagnosed.

## Native geometry and correctness

The courtyard now uses native Grid, Box, Sphere, Torus, Line, Copy to Points,
Normal and Merge SOPs, with compiled VEX wrangles for procedural placement and
deformation. Only the five-face `PYTHON_TEST_ACCENT` is a Python SOP. The sparse
plume remains a small embedded procedural VDB fixture.

Ground and rocks use renderer Noise Texture displacement and separate fine bump.
Their authored local UV coordinates make the displacement invariant to OBJ
translation. World-position-driven displacement can legitimately require
retessellation or vertex updates after a transform.

The conversion audit exposed reversed face orientation: native Houdini polygon
ordering and Cycles ordering differed. The HDK mesh adapter now reverses corners
and corresponding UV, normal, color and motion data together; crease edges retain
their original undirected association. Open curves are unaffected.
`houdini_native_normals_smoke.py` checks independent Lambertian irradiance,
front/back shader semantics, positive displacement direction and mirrored OBJ
transforms on CPU and RTX 4090. Older Python-plane and Grid fixtures were
reoriented to agree with their independently specified +Z reference surfaces;
their expected radiance and tolerances were retained.

## What is actually copied

The preceding completed-generation probe on the native courtyard, before the
selective component increment, showed:

| Edit | Observed native/engine behavior |
| --- | --- |
| Camera | No native geometry conversion or mesh-buffer upload. |
| Existing area-light movement | No mesh-buffer upload; light, instance/BVH and light-tree updates. |
| Area-light creation | No HDK reconversion; Cycles globally repacks geometry arrays, about 12.2 MiB of logical common-interface copy requests in this run. |
| OBJ transform with local-UV displacement | No triangle vertex/index upload; instance/BVH updates plus a 64 MiB shader-evaluation input request associated with volume preparation. |
| Surface roughness | No SOP geometry conversion or triangle upload; shaders and lighting data update. |
| Turbine position or topology | Four resources of the merged SOP are recaptured; unchanged curves, points and volume data also refresh. The packed prototype is retained. |

The 64 MiB observation is consistent with the pinned engine's heterogeneous-volume
octree construction: depth seven samples 128³ voxels with two shader-evaluation
inputs per voxel. The volume manager conservatively rebuilds spatially varying
volume preparation on object transforms. This is a source-based explanation,
not a driver trace proving physical bus traffic.

Diagnostics count logical requests through the common device-memory interface,
including CPU-host-backed requests. They do not measure physical PCIe bandwidth,
VRAM residency or every backend operation. GPU displacement also needs geometry
during evaluation and its displaced result afterwards; two copies cannot
universally be deleted.

Supported mixed polygon/point/curve selections now check their own values only
after coarse detail IDs become dirty. Unchanged selections retain their resource
revisions, and VDB fingerprints follow the selected grid and anchor state.
The current courtyard deformation recaptures one component rather than four;
topology still recaptures three through conservative index/provenance changes.
Tests also require point-only colors, curve-only widths, shared mesh/curve points
and VDB anchors to refresh their dependent types correctly. Camera edits do not
walk vertices for these signatures. See [mixed SOP ownership](native-mixed-sops.md).

Queued transactions now remove ordinary engine-apply waits from the UI, but
capture_mesh still copies/merges and triangulates the complete selected mesh
component after a position or attribute edit. Next work retains valid topology/
provenance and separates positions, normals and other attributes from topology
changes, with preparation of owned data outside UI work where supported.
Omitting required buffer updates, evaluating
arbitrary HOM nodes on loader threads or treating every material edit as
shader-only would break correctness. Adapter reuse also does not guarantee
object-local physical GPU transfers from Cycles' aggregate device arrays.

## Measured interaction and display

See [performance evidence](../performance.md). In an earlier instrumented 960×540
RTX 4090 artist run, the 16 ms heartbeat recorded a 575 ms maximum gap.
Native turbine deformation reached the current panel image in 855 ms, topology
in 694 ms, camera in 431 ms and OBJ transform in 361 ms. These are exploratory
single-run measurements, not certified p95 budgets. Viewport lag is unresolved.

The earlier 110-case run recorded maximum heartbeat gaps of 800 ms on CPU and 715 ms
on RTX 4090. The GPU maximum occurred during a real SOP edit with both viewers
open: host apply took 647 ms, including a 506 ms cancellation join; the current
image reached the canvas in 1001 ms. These are single-run observations with
different conversion/diagnostic work from the earlier 872/933 ms run. They do
not establish a paired performance improvement or remove the remaining wait.

The pinned Cycles scheduler increases interactive work intervals as rendering
settles, from 0.1 seconds to as much as two seconds. `Session::cancel(true)` still
joins the active render thread. These source facts give a lead for investigating
the remaining waits, not a driver trace proving their exact cause. No scheduler
policy has been changed in this increment. `ready_to_reset()` reports whether
the GPU display drew after reset; it is not a device-idle or scene-mutation gate.
The queued prototypes add a safe render-iteration synchronization hook and
measured delayed reset; observed boundary waiting reached 503 ms on the control
thread. The work-interval scheduler remains unchanged. Replacing pending edits is distinct from freeing
in-flight resources. Keep the session alive and preserve unacknowledged changes;
restart accumulated sampling only after a valid new scene state is committed.

The CPU OCIO display path now processes disjoint RGB rows in a bounded four-thread
TBB arena for larger images. An isolated display probe measured about 49–50 ms
instead of the earlier 132–136 ms for this view; raw display remained about 9 ms.
Alpha is preserved and data passes bypass the view. An independent PyOpenColorIO
comparison checks partial rows, exposure, RGB/RGBA/alpha and raw normal data with
zero byte differences. Display remains synchronous and graphics interoperability
is still future work.

## Verification scope

The full native run `artifacts/verification/20261011T144349084/` passes all
112 cases: 64 headless checks and 24 actual GUI workflows per device. Its
immediate audit matches 269 integration files, all executed entries, five
binaries, fourteen patches and six kernels. The queued-worker headless CPU case covers
the three execution modes; default GUI regressions remain synchronous. Focused
GPU mode comparisons and active worker-failure recovery have separate audits
listed in the worker document. The previous 107/109 full run and
its corrected two-case displacement follow-up remain archived with their audits
and original failed test source; their composed coverage is historical.

Artist workflows run in real isolated Houdini UI processes. QtTest clicks Render,
the gear menu and Render Settings; HOM edits actual lights/SOPs with the native
viewport and light handles visible. Tests cover fifteen light movements, geometry,
displacement, camera, transforms, material parameters, topology, panel reload,
shared IPR and tracked shutdown. This is automated artist-workflow evidence;
physical manual dragging and arbitrary project files remain untested.

Five preceding runs preserve their immediate provenance audits:
`20261011T081334883`, `20261011T081525177`, `20261011T082106174`,
`20261011T082436431` and `20261011T083041148`. Four stopped at old orientation
fixtures; the fourth was deliberately interrupted to strengthen the denoising
restart predicate. None is a completed full-suite pass.

Solaris expansion, progressive arbitrary AOVs, picking, linking, complete passes,
long-sequence memory/performance certification and Redshift comparisons remain
separate work.
