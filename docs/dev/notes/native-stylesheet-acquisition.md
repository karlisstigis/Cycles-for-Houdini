# Native stylesheet acquisition

Native stylesheet results use owned, immutable value pools and per-owner slot
indices. This removes repeated string conversion and storage on dense geometry
while retaining Houdini's native target matching, priority and CVEX evaluation.
There is one artist-facing persistent-worker IPR path.

## Ownership and binding behavior

Each primitive or point has a 32-bit slot into an exact-value layer table. A
layer contains its material path and general, surface and displacement override
strings. Exact comparisons establish reuse. Uniform results compact to one
slot, and the build dictionary retires when the table is sealed. Frozen tables
cannot be changed.

Packed descendant rules use sparse entries plus a uniform default. Signatures
hash the owned layer table, slots and descendant rules without constructing an
expanded string for every face. A transient native-result lookup avoids
converting identical native values repeatedly within a subject group. Native
name/value pointers stay alive only with that group and never enter conversion
or the worker.

General native matching uses groups of at most 32,768 ordinary or 1,024 scripted
subjects. Native result counts must match subject counts. The subsequent
[cooperative acquisition](native-cooperative-acquisition.md) change adapts group
sizes and resumes primary validation/matching across host turns. The measurements
below describe the preceding pooling increment.

When a full geometry component has a proven uniform underlying assignment,
material lookup resolves the actually used unique layers. Authored element
bindings, mixed/partial selections and point-primitive bindings retain their
owner-specific resolution. Unsupported fields remain local diagnostics;
supported lower-priority assignments, geometry and displacement remain usable.
Repairs continue through the existing watched resources and accepted updates.

## Measurements

The source-audited fixture has a 1,048,576-triangle prototype packed twelve times.
Three edits change a nonuniform face region on one instance. Independent
position masks check both affected and unaffected halves across subject-group
boundaries. The prototype remains at one cook.

| Measurement | Previous CPU layout | Pooled CPU layout |
| --- | --- | --- |
| Maximum preparation turn, three edits | 688-692 ms | 154-193 ms |
| Repeated-value conversion, median | 429.642 ms | 22.939 ms |
| Stylesheet signature, median | 75.137 ms | 0.351 ms |
| Styled material lookup, median | 52.692 ms | 0.019 ms |

The detailed previous-layout medians come from a separately instrumented run
whose preparation turns were 671-702 ms. Pooled preparation turns were 150-161 ms
on the RTX 4090 host run and 154-162 ms on the RTX 3080 Ti host run. These are
three observations per device, not a p95 acceptance certificate.

This deliberately dense emissive fixture still takes seconds to deliver a
completed frame. Engine lighting work dominates that wait. Preparation,
background conversion, engine lighting and the complete-frame wait are recorded
separately. No end-to-end preview budget is certified here.

## Verification

The evidence record (`artifacts/stylesheet-pooling-evidence-20261020.json`)
selects 38 passing runs on the same compiled runtime. Each native run has a
source audit and immutable source archive. Earlier development failures remain
recorded, including a corrected fixture that used primitive targets for points.

CPU and both individual OptiX devices cover dense nonuniform acquisition,
per-owner mesh/point CVEX values, invalid values and repair across group
boundaries, native/imported styles, scripted time/shutter evaluation, component
domains, sparse volumes, geometry motion and object binding shutter samples.
Ordered output checks retain five-frame sequences through source failures and
repairs. Strict transport/ownership contracts pass 267 checks in each of six
fresh harness processes.

Both live viewers pass fourteen dense binding/edit/repair states and six queued
camera/object callbacks, preserving the worker and prototype cooks. Camera and
object-only edits retain geometry conversion counts. The native viewer also
checks actual displayed pixels. Both viewers separately pass 37 scripted source
and data repair states. Tracked geometry and device allocations return to zero
after close.

The pooling increment's dense live face-region edit records a 156 ms heartbeat
gap in Cycles Viewer. Cooperative primary acquisition subsequently reduces that
recorded gap to 39 ms. Other host work and startup have their own larger gaps;
these HOM workflows do not measure physical mouse latency. The subsequent
[motion-binding increment](native-cooperative-acquisition.md) also resumes initial
motion probes, general effective assignments, slot extraction and structural
shutter recipes. Its dense motion fixtures record roughly 23-26 ms maximum host
turns across CPU and both individual OptiX devices. Individual host/graph calls
and large packed traversals still need separate profiling.
Surface-only variants still own separate engine meshes/BVHs; neither change
adds physical engine sharing or a wire-protocol change.
