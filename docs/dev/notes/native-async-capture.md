# Asynchronous native IPR capture

## Shared packed descriptions during conversion - October 18, 2026

Host `20261017T231527050` keeps acquired packed component descriptions shared until complete
shutter profiles are known. It does not clone geometry selection, material lookup,
reference lists and request strings for every nominal instance and discard them
again after grouping. Separate profiles still create explicit compatible variants,
including different shutter contents, bindings and displacement requirements.
Static components retain their multiplicities. Source details remain owned by the
existing acquisition leases; no OP evaluation or packed factory resolution moves
into the converter.

Correspondence performs one lookup per object/sample and reserves transform arrays.
It preserves invalid-population recovery, optional-identity behavior and independently
valid parent/OBJ motion. Camera-only captures still share the accepted object table.
`CYCLES_HOUDINI_PROFILE_PACKED_ASSEMBLY=1` enables phase timings returned by
`native_stats()['packed_assembly']`; ordinary rendering leaves those timers empty.

The 1k/10k/100k capture profiles, independent CPU/both-GPU images, five-frame sequences,
heavy prototype workflow and 100k-instance camera/object overlap are retained in
`artifacts/verification/packed-expansion-20261018/index.json`. These are development
checks, not physical interaction, universal latency or process-memory certification.


Native OBJ/SOP artist IPR uses one persistent external renderer with a bounded
host capture pipeline. Ordered final ROP rendering retains the synchronous
wrapper over the same preparation/conversion implementation.

## Ownership and scheduling

1. On Houdini's host thread, resolve requests, cook nominal SOPs and retain
   immutable details through preserve requests.
2. On the sole conversion thread, classify retained geometry and inspect
   material-binding attributes. Cache identifiers are validated before reuse.
   For packed geometry, return to the host to resolve and retain the next packing
   level, then scan its unique prototypes in the background. Repeat until the
   packing hierarchy is acquired. Instances share retained prototype scans;
   preparation uses the frozen primitive-to-prototype lookup without resolving
   the procedural source again. Superseded jobs can be discarded between levels.
3. Return to the host to acquire shutter
   samples, evaluate native graphs and freeze camera/light/settings values.
4. Convert immutable geometry, compute changed components and construct the
   desired scene state off-thread. No live OP lookup, SOP cooking or material
   callback is allowed in that stage.
5. On the host, finish cached texture/color-policy acquisition and commit the
   owned snapshot. Reject a working-color policy change across acquisition.
   The renderer controller diffs the snapshot against its acknowledged state.

The adapter has one exclusive owner throughout these stages. Status reads a
published statistics copy while its caches are being updated. A job retains its
native handle until completion or retirement. Failed jobs invalidate extraction
caches without replacing the committed scene. Superseded scans may retain their
validated classification caches, but never relabel an old result as a newer edit.

There is at most one active job and one pending request. Pending partial updates
are merged by resource identity, so camera-only edits cannot erase a preceding
geometry or material change. Deletions and later additions preserve update order.
Completed obsolete captures can advance the desired-state baseline without
being sent to the renderer. Camera-only updates bypass the root scanning stage.

Stop drops pending work and retires existing generations immediately. The active
reader is allowed to finish on its thread; the UI never joins it. GUI retirement
queues cleanup and releases the Houdini-facing handle on the host after conversion
and worker shutdown complete. Host polling and process shutdown drain retirement.
Headless callers advance acquisition through status/wait polling.
Host preparation callbacks cannot reenter capture or close the session. Status
polling during a callback reads published state without advancing another job.

## Measured evidence

The pre-change heavy GUI run `20261012T231015949` spent 261–275 ms submitting
deformation changes and 294–339 ms submitting topology changes. The asynchronous
renderer/stopped-control run `20261012T234312228` submits those changes in 5–8 ms.
Its additional host preparation steps take 5–7 ms and completion takes 2–4 ms.
Root scans (88–108 ms) and conversion (198–266 ms) happen off-thread.

These are three observations per edit, not a p95 or a universal speed claim.
The same visible viewport with rendering stopped and Auto disabled still produces
252–268 ms deformation timer gaps and 366–418 ms topology gaps, with no scene
submissions. Active IPR's corresponding gaps are 297–488 ms and 386–439 ms.
The control runs afterward with warm caches, so it does not attribute every
remaining millisecond. UI timer gaps include cooking, viewport redraw, polling
and scheduling; shared GPU contention has not been isolated.

`houdini_async_capture_ipr_smoke.py` deliberately holds the scan/conversion thread
while the host edits geometry and cameras, deletes a source, processes UI timers,
replaces pending requests, stops/restarts and retires the session. A finite barrier
detects accidental host joins. The fixture has over a million rendered triangles
and only one render viewer. `houdini_host_cooking_response_benchmark.py` records
the heavy edit workflow plus the renderer-stopped control.

Complete run identities, manifests, failures and measurements are recorded in
`artifacts/stage1/geometry-ownership/async-verification-summary.json`.

## Limits and further profiling

Houdini cooking and host-only graph/packed/shutter acquisition remain host work.
The nominal packed hierarchy participates in background prewarming. Shared leaf
source rows, bindings and grouping now resume across host turns; owned placement
and pose expansion run with the converter. Individual procedural, graph and
shutter-source calls can still be expensive. Successful publication now uses
[bounded converter-owned retirement](native-scene-publication.md).
[Queued cancellation and close](native-capture-retirement.md) use the same
converter, with host finalization afterward. This does not permit
arbitrary procedural execution on background threads. Custom
packed procedurals, external-file reload ownership and live `op:` texture sources
need their own adapters/fixtures. Repeated edits are bounded, but retained details
can impose copy-on-write costs on the next Houdini cook; measure both sides.

The pipeline removes the measured synchronous renderer conversion pause. It does
not establish Blender parity, universal zero-stutter interaction, or faster total
geometry-to-image latency. Render preparation and BVH rebuilds still take time
while the host remains available.

## Heavy packed follow-up

The courtyard benchmark now includes a 721 by 721 native Grid prototype
(1,036,800 triangles), UV attributes, true displacement, and 16 shared instances
through two packing levels. It changes prototype positions and topology three
times each, submitting object and camera edits while capture is in flight.
The before/after runs are `20261013T004219467` and `20261013T004524031`.
The earlier path spent 68–76 ms preparing changed packed prototypes on the host.
The staged path moves those scans to the conversion thread; concurrent camera
submissions took 0.36–0.64 ms and object submissions 7.3–8.4 ms in that run.
Full geometry-to-image latency remained around 1.1–1.3 seconds. This is an
interaction improvement, not evidence of a total-render speedup or a stable p95.

The packed lifecycle fixture holds the acquired leaf prototype before scanning
while geometry is edited, a source is deleted, and Stop/restart/retirement run.
A separate five-frame fixture combines animated packed geometry, the adaptive
turbine and changing displacement maps. Audited run identities and failures are
recorded in `artifacts/stage1/geometry-ownership/packed-async-verification.json`.

## Nonlinear packed cooking control - October 19, 2026

`houdini_heavy_packed_cooking_response_benchmark.py` adds a native VEX wave to the
million-triangle prototype. It repeats the same shape/topology parameters in the
visible packed SOP viewport with rendering active and then stopped, with Auto off
and no scene submissions in the control. The final run has thirty edit cases,
six concurrent navigation cases and six stopped controls. These are Qt/HOM
observations with warm/order differences, not physical-input attribution.

Topology gaps remain comparable with rendering stopped. Shape edits still have
some additional active-run delay. Host acquisition/preparation/completion calls
are timed separately from background scans/conversion. Details and actual image
observations are in [the current performance note](native-parallel-light-tree.md).
The independent five-frame fixture also checks the adaptive turbine and animated
displacement, writing every requested frame through one persistent worker.
