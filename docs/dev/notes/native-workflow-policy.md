# Native interactive and final-render execution

Native OBJ/SOP remains the priority. Interactive and final rendering need
different scheduling policies. A process boundary and an update policy are
separate decisions: threading, incremental updates and persistent resources are
possible inside Houdini or inside a worker. The native development path now uses
a persistent worker for IPR and ordered in-process ownership for final ROP frames.
The extra queued in-process IPR backend is retired. Release certification remains
subject to the measured response, correctness, memory and lifecycle gates.

## Policy by task

| Task | Primary objective | Update and resource policy | Execution candidate |
| --- | --- | --- | --- |
| IPR while editing | Short host-thread stalls and prompt current images | Persistent scene; one replaceable pending desired state; retain changes not yet acknowledged; discard stale image generations; commit at safe engine boundaries. | Persistent worker; renderer isolation and host-wait removal are the reasons. Faster edit response remains unproven. |
| Final frame/sequence | Complete, correct outputs with high throughput | Ordered frames, explicit completion and output acknowledgement; retain compatible resources across frames; never coalesce requested frames away. | Cycles inside a headless Houdini batch process is the initial candidate; compare a renderer worker only if it provides a measured benefit or required isolation. |

A headless Houdini process can cook and render a saved scene independently of
the artist's GUI. Cycles being in process there does not mean it runs in the
interactive Houdini process. This avoids renderer IPC for the batch candidate.
The existing supported ROP/batch route runs inside its calling Houdini process;
an automatic background-final-render launcher is not implemented yet. Foreground
ROP execution still waits as required by its dependency chain.

The SOHO final branch now owns an ordered session through public ROP render
events. Persistent Data retains compatible resources across frames; each frame
awaits rendering and successful output before the next submission. Completion,
failure or cancellation releases the sequence. This ownership is separate from
IPR. Real ROP tests cover analytic projection/depth/color, resource retention,
configuration changes and failure cleanup. The five-frame interactive comparison
below remains a different workload from final-render throughput.

The native ROP has no execution-mode selector. See [the task-specific ownership
policy](native-execution-policy.md). Choosing one IPR path does not establish
production performance or remove the remaining host capture cost.

## Renderer references, checked on 2026-10-11

The local Blender 5.2.0 LTS executable reports commit `fbe6228777e7`, branch
`blender-v5.2-release`, built on 2026-07-14. Its
[Cycles session source](https://github.com/blender/blender/blob/fbe6228777e7/intern/cycles/blender/session.cpp)
owns an in-process Session and starts a render thread. Viewport synchronization
records change flags first, defers if readiness or a nonblocking scene lock
fails, then synchronizes and resets accumulated sampling when necessary. Final
rendering starts and waits; Persistent Data permits session/resource retention.
`ready_to_reset()` is not proof that the GPU is idle. These are source findings,
not a measured Houdini/Blender speed comparison.

[Blender's change tracking](https://github.com/blender/blender/blob/fbe6228777e7/intern/cycles/blender/sync.cpp)
separates shading, transform and geometry recalculation. Adaptive subdivision
can make a transform or dicing-camera change affect geometry. Its
[object synchronization](https://github.com/blender/blender/blob/fbe6228777e7/intern/cycles/blender/object.cpp)
accepts a geometry task pool but excludes some instance work because it accesses
geometry. Parallel conversion therefore needs ownership and API-specific
constraints; moving all live Houdini access onto loader threads is not implied.

[Redshift's Houdini ROP documentation](https://help.maxon.net/r3d/houdini/en-us/Content/html/Redshift%2BROP%2BNode.html)
describes nonblocking current-frame rendering, multithreaded scene loading and
several sequence update modes. Its fastest transform/parameter mode excludes
SOP deformation; the time-dependent-node mode reloads affected objects. This is
useful behavior to compare, but does not reveal private process/thread ownership.
Our required automatic updates must preserve deformation without depending on an
artist choosing an unsafe transform-only mode.

[Octane's Houdini settings](https://docs.otoy.com/houdini/OctanePluginSettingsMainTab.html)
document updating animated transforms, deformation, materials and instances
without reloading the entire GPU scene. This is the same incremental-resource
objective. That page does not establish the Houdini plugin's process boundary.

These references support selective updates and persistent data in both workflows,
with scheduling adapted to the task. They do not prove that an external worker
is faster for Cycles in Houdini.

## What the current measurements establish

The same heavy native fixture contains 950,906 triangles, packed instances,
particles, curves, three VDB fields, a deforming turbine and five changing
displacement-image assets. The restarted five-frame GUI comparison uses one
Cycles panel and the viewport. Total times including completed images and output
are now 9.118 s synchronous, 9.413 s queued in-process and 13.346 s worker,
after direct conversion, triangulation retention and component-specific bulk
reads. These are single interactive sequences, not final-render throughput or Blender
benchmarks. Those three-mode results precede retirement of the local queued
backend. Native IPR now uses the worker; see [the test drive](../../user/try-native-sops.md).

Three alternating five-frame viewport-only and capture-only sequences isolate
plugin CPU work. Warm displayed-SOP resolution now takes a median 36.7 ms
without capture and 34.8 ms with capture. Capturing already cooked geometry adds
250.4 ms median, including 153.7 ms of measured adapter conversion. Earlier
frozen builds measured 310.0/213.9 ms after direct conversion and
677.3/572.8 ms before it. The capture
handles create no Cycles rendering session and perform no GPU upload. Adapter
cook waits remain negligible on already cooked data. This identifies a substantial plugin cost before
rendering. It does not mean all native viewport redraw is only 40 ms: maximum
warm Qt event gaps are 389 ms without capture and 378 ms with it. Independent
maxima must not be subtracted as a causal GPU-loading measurement.

Exact reports and frozen sources are in
`artifacts/performance/native-component-capture-20261011T153020318/`. The earlier baseline
and render comparison remain under `native-viewport-20261011T135904408/` and
`native-animation-20261011T134218875/` in the same performance directory.
[Performance evidence](../performance.md) records scope, stage timing and image checks.

## Ownership before background conversion

The installed HDK and [public detail-handle documentation](https://www.sidefx.com/docs/hdk/class_g_u___detail_handle.html)
distinguish lifetime from immutability. A retained handle preserves lifetime,
but SOPs can reuse its detail in later cooks unless a preserve request is
registered. The legacy read/write lock methods are not enforced.
[Node-evaluation thread-safety guidance](https://www.sidefx.com/docs/hdk/_h_d_k__node_intro__thread_safety.html)
also rules out casually evaluating nodes or parameters from conversion threads.

Background preparation therefore needs an explicit retained immutable source
or owned buffers, host evaluation in a supported context, bounded pending
versions and balanced retirement. Preserving a detail can increase the next
cook's copying and memory cost, which must be measured. This ownership path
has been audited as a candidate; background HDK conversion is not implemented.

## Next implementation and comparison

1. Further reduce synchronous capture. Valid triangulation/provenance and
   unchanged supported normal/UV/color arrays are now retained. Narrow the
   remaining component scans and copies, measure orchestration separately,
   and prepare safely owned data in background tasks where supported. Preserve
   correctness for moving
   concave polygons, topology, subdivision, displacement and attribute changes.
2. Measure both host extraction and engine/device synchronization. A single
   changed adapter resource can contain unchanged branches of a merged SOP;
   counters are not proof of object-local physical GPU copies or BVH refits.
3. Repeat heavy worker IPR edits with the viewport baseline and one render viewer.
   Preserve historical comparisons and use the matched engine harness for new
   attribution. Measure worst host-thread intervals, input response and
   current-image latency separately. Process isolation does not fix host capture.
4. Test real final ROP execution for at least five animated frames, with ordered
   output and sequence persistence, then compare headless in-process and worker
   costs without an interactive display.
5. Use Blender itself as the integration benchmark: matched evaluated geometry,
   displacement assets, camera, materials, passes, device and workloads; measure
   rendered-viewport edits and final sequences separately. Record native host
   evaluation separately so Houdini cooking is not blamed on Cycles integration.

The installed Blender build uses a different engine revision and dependency
configuration from this standalone Cycles 5.3 research baseline. Its integration
behavior remains the reference, but raw render-time differences cannot be
attributed entirely to this plugin. The matched standalone harness remains a
second control for engine overhead, alongside the actual Blender comparison.
