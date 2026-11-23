# Background final rendering

The viewer's clapperboard starts a **final render of the ROP's frame range** in a
separate matching Houdini batch process. It runs the ROP's normal render callbacks,
Final Render settings and Output Picture against a private HIP snapshot. Cooking
and rendering happen outside the artist process. Saving that initial snapshot
still runs on Houdini's main thread and can briefly pause very large scenes.

The **Cycles Render** panel updates its image as each frame finishes and shows live
render progress, with **Pause** and
**Stop**, without an Interrupt window or console. An authenticated local pipe
carries bounded frame/status replies on a background thread; image conversion
also stays off the GUI. Only the selected pass is streamed. A held image retains
the rendering process for AOV selection and additional EXR saves; **Play**, viewer
closure or scene replacement releases it. Parent loss also retires that process.
The viewer render's own GPU scheduling favors interaction; ordinary disk and farm
renders retain their existing policy. No global driver preferences are changed.
Stop first requests cooperative cancellation. A cook or device initialization
that does not acknowledge it within three seconds is terminated in the owned
batch process. EXR output uses staged atomic replacement, preserving previously
completed images. Hidden viewers poll status without transferring image planes.

Scene edits do not restart a held final image. **Play** returns to IPR. The
clapperboard uses the ROP's Valid Frame Range and start/end/increment settings;
Any Frame renders the current frame. The viewer shows the current frame with
frame and overall progress, retaining the last image while the next frame cooks.
Stop cancels the remaining sequence and preserves completed files. It holds
only the current image, without an animation playback cache. Persistent Data
retains its configured behavior. Job workspaces and logs use `$HIP/cycles_background`,
as described below. Foreground ROP rendering still uses Houdini's native Interrupt.

Restart Houdini to load the new development host. Use **Cycles Tools → Render ROP** to add a **Cycles Render** output node.
Set the camera, output EXR and frame range as usual. **Controls → Render to Disk
in Background** starts an independent Houdini batch job. It uses the ROP's Final
Render settings, including Persistent Data, regardless of the viewer's quality
selection. Continue editing the foreground scene while it renders.

**Controls → Cancel Background Render** requests cancellation of the ROP's most
recently submitted job. Foreground **Render to Disk** uses
Houdini's standard **Interrupt** panel (or Escape), with no extra Cycles progress
window. Houdini cooks and captures the scene, polls worker progress, and owns
the ROP frame range. The child renderer writes the requested EXRs directly and
acknowledges each write before the ROP advances. A persistent sequence reuses
the child across frames. A headless farm ROP renders in its own hython process.
Cancellation stops
the remaining sequence, preserves completed images and does not write the
unfinished frame. Native progress updates check for interruption without copying
image buffers into Python. Cooking, initialization and driver operations may delay
cancellation until they yield; background jobs also offer Force Stop below.

Foreground SOHO output, including Houdini's license notice, is retained in
`$HOUDINI_TEMP_DIR/cycles-<process-id>-<rop-id>-<frame>.log`. Routine output does not open
the Houdini Console. Background SOHO output stays in the job's `render.log`.

**Controls → Background Renders** opens a live, non-modal project job list with
state, frame, progress, scene/ROP and start time. Select an active job and click
**Cancel Render** directly. An owned job stuck in a cook or driver operation
offers **Force Stop** after cancellation is requested. Already written frames remain on disk. After reopening
Houdini, the saved job record still supports a stop request; forced termination
requires the original process handle. Interrupted jobs are detected using PID
and process creation time rather than trusting a reused PID.

Host `20261018T005418397` fixes the earlier 15.24-second Stop race: the quick-cancel
request now stays latched until the session thread is idle. The final regression
trials acknowledge Stop within 21.4 ms on CPU and 64.8/64.5 ms on the RTX
4090/3080 Ti. Five heavy-scene restarts per device retain geometry and finish a
valid image. These observations cover the explicit regression fixture; cooking,
initialization or driver work can still limit cancellation. The foreground does
not wait for acknowledgement, and Force Stop remains available for an owned job.
See `artifacts/latched-cancel-evidence-20261018.json`.

The launcher uses a private HIP copy, preserving the artist scene's filename,
HIP/HIPNAME/JOB, take, current frame and active OCIO configuration. Relative LUT
search paths retain their original directory. The batch process uses the same
Houdini build and loaded Cycles host/resources; other package directories are
inherited. Unsaved scenes work without saving over the artist's file. The batch
process continues if the launching Houdini closes.

The submitted HIP is a snapshot. Later parameter or node edits in the artist
session do not alter it. External files, third-party dependencies and runtime
Python state are not packaged or frozen. Keep required assets accessible to the
batch process. Snapshotting itself still runs on Houdini's host thread; large
HIP saves can pause the UI. Scene cooking, renderer synchronization and rendering
then run in the separate process.

Recoverable materials/geometry continue rendering with the same fallbacks and
diagnostics as foreground rendering. Per-frame diagnostics remain in the EXR and
the job's `frames.jsonl`. Output, device and essential configuration failures
remain failures. A job is complete only after its final ordered render reports
successful output or explicit existing-file skips. The child uses a normal
ordered final session, separate from the persistent-worker IPR path.

New job workspaces are stored under **`$HIP/cycles_background`**, or
`CYCLES_HOUDINI_BACKGROUND_ROOT` when configured. Each job owns `job.json`, a HIP,
an OCIO snapshot, `status.json`, `frames.jsonl` and `render.log`. Progress records
have bounded size per job; full frame records are streamed to the journal.
Saved HIPs and reports remain on disk for inspection. In memory, one lightweight
watcher releases exited process handles automatically and retains only active
jobs plus 32 finished metadata entries. The native job list shows up to 50 saved
history entries per page, with Older/Newest navigation; all currently owned
active jobs remain available on its newest page. Labels show the scene, ROP,
frame and time. Damaged reports remain inspectable without breaking other rows.
Old job folders are not moved or deleted. Project changes switch the saved-job
history root; currently owned active jobs remain accessible across projects.

The Houdini Render Scheduler manages Houdini-owned render processes. These
independent batch jobs are not registered there through an unsupported private
API. The live job window exposes their actual progress and cancellation; jobs
start immediately and are not represented as a serial scheduling queue.

## Saved HIP rendering on a farm

The installed host includes `scripts/render_hip.py`. Run it with the matching
Houdini `hython` and the Cycles package enabled on the render node:

```text
hython "<Cycles host>/scripts/render_hip.py" "<project>/scene.hiplc" --rop /out/cycles1
hython "<Cycles host>/scripts/render_hip.py" "<project>/scene.hiplc" --rop /out/cycles1 --start 1 --end 100 --step 1 --report "<logs>/render.json"
```

Without an explicit range it uses the ROP's saved frame settings. `--output`
optionally overrides Output Picture and supports Houdini variables. HIP/HIPNAME
come from the original saved scene, even when the farm starts in another working
directory. No background snapshot or job folder is created. The same ROP can be
rendered directly with `hou.hipFile.load(...)` and `hou.node(...).render(...)`.
The wrapper emits Alfred-style progress and exits nonzero on missing scenes or
ROPs, renderer failures, incomplete sequences or failed output writes. Recoverable
source warnings retain normal rendering fallbacks and per-frame EXR diagnostics
by default. Enable the ROP Fail on Missing Render Inputs option or pass
`--fail-on-missing-inputs` to fail before publishing a frame with missing or
unusable authored inputs. `--no-fail-on-missing-inputs` explicitly restores the
permissive policy. Informational source notices, successful volume resampling and
engine quality-limit warnings do not fail this policy. Strict Skip verifies the
current scene by rendering before validating/reusing existing output. It also
requires every existing EXR part to carry certified clean input provenance;
previous fallback pixels remain rejected after the scene is repaired. External
or legacy EXRs without this certification must be regenerated with Overwrite
for strict use. Permissive Skip still accepts their matching readable headers. The JSON
report retains frame/path/code diagnostics from the whole sequence on success or
failure; permissive Skip recovers diagnostics stored in valid existing EXRs.
Install the matching Houdini build and Cycles plugin on each node; make external
assets and the intended OCIO configuration available there. This entry point
does not package dependencies or submit to a particular farm scheduler.

Stop immediately reports Stopping. Repeated requests retain their first request
time. Explicit Force Stop reports cancellation, while an unexpected crash or
termination remains a failure. Finishing between a status read and an exit check
preserves the child's successful final report. Disk-history pruning remains
production-operations work. Bounded surface, world and volume material previews
are described in `native-materials.md`;
broader contexts and device/memory certification remain open.

The visible background action is an authored native Houdini parameter button.
The inherited reserved SOHO background action is kept out of the parameter
layout: its tested behavior did not invoke this batch launcher. Scene copying
uses documented [mwrite -n](https://www.sidefx.com/docs/houdini/commands/mwrite.html);
the child renders through [RopNode.render](https://www.sidefx.com/docs/houdini/hom/hou/RopNode.html).

Development checks pass on CPU, RTX 4090 OptiX and RTX 3080 Ti OptiX. They cover
five-frame material failure/repair, relative-LUT OCIO output, saved scene
context, changes after submission, data passes/object Cryptomatte, output skip
and error, active stop, interrupted jobs, parent closure and unsaved scenes.
Color, depth, UV and Cryptomatte are bit-identical to their foreground reference;
filtered GPU normals allow measured float32 rounding between independent renders.

A 4090 GUI check renders five frames of a 1,036,800-triangle prototype with 16
instances while accepting five native parameter keyboard edits and scripted
viewport navigation. It displays a proxy in the foreground to separate native
Houdini cooking from renderer work. This run observed a 127 ms launch, Qt
heartbeat p95 of 16.5 ms and a 188 ms maximum. It does not certify physical-input
latency, memory plateau, mixed GPUs or arbitrary scenes. Normal and deliberately
failed GUI-fixture cleanup exit cleanly; a progressive worker IPR/focus regression
also passes. Earlier failed investigations and their source snapshots are kept.

Evidence: `artifacts/background-final-evidence-20261018.json` and
`artifacts/verification/background-final-20261018/index.json`.

The current ownership regression launches 144 real children, confirms automatic
handle release with a stable resident history, and reaches 1,253 reports through
26 pages. Heavy cancellation, five-frame output and lifecycle checks pass on CPU
and both OptiX devices. Native ROP/menu dispatch also passes with dialog selections
simulated: the RE modal list blocks the in-process Qt driver, and screen capture
showed blank contents. The failed investigation is retained; visual dialog and
physical-input validation remain unverified. Separate real parameter keyboard
edits and viewport navigation during heavy background rendering pass on the 4090.

The background folder is a private job workspace, not a duplicate of the project
or the destination for rendered images. It contains the scene snapshot, launcher,
pinned package settings, OCIO configuration, progress and diagnostic logs. Images
still go to **Output Picture**, with the original `$HIP`, `$HIPNAME` and `$JOB`
context. External assets stay referenced in their original locations. Retaining
the workspace lets the job survive closing Houdini and keeps failed-job evidence.
See `artifacts/background-jobs-evidence-20261018.json`.
