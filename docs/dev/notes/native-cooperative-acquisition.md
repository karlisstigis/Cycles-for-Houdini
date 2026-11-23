# Cooperative material and stylesheet acquisition

Native stylesheet validation/matching, general effective-assignment walks,
material-motion slot extraction and structural shutter profiles resume across
host UI turns.
The artist-facing IPR path remains the persistent worker. The accepted scene can
continue providing camera/object previews while a complete geometry update is
being prepared. Pending scene changes remain queued until safely committed.

## Acquisition and ownership

A warm acquisition has a unique capture identity and a 20 ms target turn budget.
Native matching still runs on Houdini's host thread. At a completed group or
validation boundary, a typed continuation retains owned values and owner
indices. The worker controller resumes preparation on a later host poll, with a
short not-before gate to avoid repeatedly spending the budget through adjacent
polls. Frame metadata and image reads stay read-only.

No native STY subject, GA handle, iterator, script closure or graph callback is
retained across a turn. Completed scripted results are keyed by capture identity
and evaluation time, including separate shutter visits. Watched file, operator
and registered-style versions invalidate affected partial/completed results on
repair. Captured geometry roots and packed shutter details retain their owners.
Completed material-motion probes can be reused within that same owned capture.

Motion acquisition shares the adapter's prewarmed partition/binding caches and
owned stylesheet results. Static prototypes reuse matching results across
shutter visits when their evaluated values permit it. Scripted results retain
their separate capture/time ownership. Off-frame SOP details are preserved once;
the requested frame is restored before matching can yield. A resumed acquisition
does not repeatedly cook those same samples.

General styled assignment catalogs advance in groups of 256 owners. Material
slots advance in groups of 512, retaining only owned lookup values and numeric
owner positions. Transient helpers recreate their GA handles on each turn.
Pooled style slots and interned path/override indices resolve repeated effective
bindings once. Structural shutter recipes advance in groups of 2,048 elements.
Their cached tuples contain material-slot numbers, with the immutable binding
record retaining the node identities used by the cache key. They contain no
evaluated graph decisions: current host graph preparation and exact displacement
comparisons run after the structural plan completes.

Validation checks the budget after new source parsing and periodically for
repeated attribute strings. Native matching starts with small groups and adapts
from actual processed subject count, including native-result destruction.
Ordinary groups remain limited to 32,768 subjects and scripted groups to 1,024.
The target duration per group is about 4 ms. These are scheduling targets;
individual host/parser/VEX/cook calls cannot be preempted.

Each preparation attempt resets its temporary updates and graph callbacks while
retaining frozen roots, color policy and job ownership. A continuation keeps the
job in its acquired phase; conversion begins only after preparation completes.
Cancellation/discard retires that state. A continuation does not set the render
session to Failed. Source recovery and genuine programming, ownership, engine
and output failures keep their existing distinct contracts.

Completed continuation parsing tables retire under the exclusive converter
owner, after the preparation owner/serial/consumption check. Acquired requests
retain immutable result tables. Commit preserves the native binding cache's
ownership rules. Transport remains protocol 46, with no engine patch change.

## Primary acquisition measurements

The inherited-rule fixture has one 1,048,576-triangle prototype packed twelve
times. Three edits affect half the faces on one instance. Independent position
masks verify both affected and unaffected halves. The prototype remains at one
cook. The distinct-source fixture has 8,192 primitive-authored sheets with unique
JSON text, three rendered parameter variants, and broken/repaired source rows;
source-coordinate masks independently verify per-face results.

| Fixture | Previous maximum host turn | Cooperative maximum host turn |
| --- | --- | --- |
| Dense inherited region, CPU | 154-193 ms | 23-25 ms |
| Distinct primitive sources, CPU | 79-85 ms | about 22-25 ms |
| Dense inherited region, RTX 4090 host run | 150-161 ms | 24-25 ms |
| Dense inherited region, RTX 3080 Ti host run | 154-162 ms | about 24 ms |

Ranges cover three edit observations per run. These are not p95 latency
certificates, and the fixture resolutions differ from the prospective preview
acceptance fixture. Total host preparation for the dense inherited case rose to
roughly 206-245 ms across the device runs because preparation resumes and
reconstructs transient state. Distinct-source preparation fell from 106-111 ms
to about 81-85 ms. Cooking, conversion, engine synchronization and image delivery
remain separate measurements. Dense-emitter lighting still dominates seconds of
completed-frame wait; no end-to-end preview budget is certified here.

## Primary acquisition verification

The source-audited evidence is
the cooperative acquisition record (`artifacts/stylesheet-cooperative-evidence-20261020.json`).
Forty-two selected passing runs share the final compiled runtime. They record
native matching, imported and scripted styles, time/shutter behavior,
mixed components, volumes, motion, recovery and ordered output across CPU and
both individual OptiX GPUs. Both profiles require multiple resumptions and a
maximum turn below half of their total host preparation. The gate verifies
cooperation without asserting a universal machine-independent millisecond limit.

Both real viewers exercise six completed states and six edits made at proven
continuation boundaries: camera/object navigation, imported-source failure and
repair, deletion/recreation, cancellation and source removal. Automatic source
submission is disabled in this fixture so each HOM capture occurs at that known
boundary. Normal image delivery remains active, including native displayed-pixel
checks. The fixture verifies current final images, warning clearing, unchanged
worker/prototype cooks and zero tracked allocations after close. Both older
dense viewer workflows also pass fourteen edit/repair states and six queued
navigation callbacks. The dense face-binding edit records a 39 ms heartbeat gap
in Cycles Viewer, versus 156 ms after pooling alone. Other stages and startup
have larger gaps; this is not a whole-viewer latency certificate. A separate
conversion ownership probe checks concurrent edits/deletion and duplicate
consumption rejection. Strict mesh contracts pass 267 checks in each of six
fresh processes.

## Motion-binding measurements and verification

The motion fixture enables transform motion and three shutter samples on the
same 1,048,576-triangle prototype packed twelve times. Four edits cover surface
styles and actual displacement. Independent position masks verify the changed
and unchanged face regions, and the measured height matches the authored plane
normal. A second fixture authors primitive material paths to exercise the
general assignment walk. Both retain one prototype cook.

| CPU fixture | Previous maximum host turn | Current maximum host turn |
| --- | --- | --- |
| Inherited surface style with motion enabled | 963-992 ms | 24-26 ms |
| Inherited style with true displacement | 3,135-3,313 ms | about 24 ms |
| Authored primitive bindings, surface/displacement | No baseline recorded | 23-25 ms |

Across CPU and both individual OptiX devices, the six profiles record maximum
turns of roughly 23-26 ms. Total host preparation is 213-254 ms for the inherited
case and 265-328 ms for the general case. The original inherited CPU totals were
about 1.18-1.22 seconds for surface styles and 3.49-3.69 seconds with displacement.
These are four observations per fixture/device, not latency percentiles.
Complete update waits still range from about 2.25 to 5.53 seconds in this dense
emitter fixture. Device synchronization, lighting work and actual image
presentation are separate from host preparation; the prospective preview budgets
are not certified by these measurements.

The final evidence record (`artifacts/material-motion-cooperative-evidence-20261020.json`)
selects 59 passing runs on the final compiled runtime: CPU/4090/3080 Ti material,
stylesheet, CVEX, shutter, volume, displacement, packed-color and strand checks,
plus seven real GUI/ownership workflows. The CPU-only strict mesh contracts run
267 checks in each of six fresh processes. Ordered output checks remain included.
Test-only staging has an explicit unchanged-binary/runtime proof.

Both viewers pass six dense motion-binding states and six edits at proven
continuation boundaries, with navigation, source break/repair, deletion,
cancellation and removal. Both also pass 34 animated object/shutter states and
five shared-particle conflict/repair states. Native workflows check displayed
pixels. Worker identity, usable geometry, warning removal and allocation cleanup
remain verified. The conversion ownership probe passes four concurrent-edit
checks and retains duplicate-consumption rejection.

Healthy material renames follow their verified saved identity/session ID.
Frozen binding identities keep unchanged point/curve deformation samples reusable
while paths update. Conflicting particle owners now retain the first supported
binding on each shared point and report a local diagnostic. Unused palette slots
are removed; an explicit point binding repairs the conflict without removing
geometry. Supported animated surface bindings render without recovery warnings.

## Packed source acquisition and placement groups

Transform-only packed motion now uses leaf templates, as deformation already
did. Root/prototype acquisition, packed rows, duplicate-identity finalization,
binding-tree children and source grouping resume from numeric positions and
owned values. Completed row tables and ledgers are shared immutably. Placement
and population expansion borrow those rows under converter ownership instead
of cloning each identity and matrix on the host.

A binding capture keeps completed nodes across pauses, including nominal and
off-frame trees. Its private partial nodes never enter a scene. Watched style
resources and current material identities/displacement demand invalidate the
affected work. Current graph callbacks still run on the host; they are never
cached in these trees or grouping keys. A pure pose source retains the owning
record behind structural routing tokens and may expand shutter maps off-thread.
Stale/owner/consumed checks run before continuation retirement. Missing ledger
entries remain internal errors, distinct from explicit unavailable contents.

The prior bulk packed helper was replaced with the owned preparation cache;
there is no previous-signature alias. The persistent worker remains the artist
IPR path, with ordered final rendering and Persistent Data unchanged.

The capture-only fixture uses a 1,036,800-triangle shared prototype at 1,000,
10,000 and 100,000 placements. Eight states per count verify inventory, one
prototype cook, material styles, true displacement, edits, camera-only deltas
and retirement. It excludes transport, engine synchronization, rendering and
image presentation. The original maximum measured preparation call at 100k
was 1.28-2.15 seconds for cold/surface/object states and 2.02 seconds when
enabling displacement. Final nontrivial preparation turns are about 20.1-20.2
ms. Complete capture is about 0.80-1.09 seconds; work moved to conversion is
included in that total. Nonempty commits still take roughly 23-30 ms, and the
final source retirement takes 56 ms. These are scoped observations, not
universal turn limits. A development version replayed a completed tree across
395 pauses and took 21 seconds; that implementation was corrected and its
measurement remains in the evidence.

One real Cycles Viewer test renders the full shared million-triangle prototype
at 100,000 placements, 960x540, 32 samples and three shutter samples. Houdini's
native viewport shows the containing packed bounding box to isolate renderer
scheduling from drawing 100 billion viewport triangles. Twenty scripted HOM
edits reach actual Qt-painted pixels. All ten navigation edits made during
geometry capture show their first preview while capture is still pending.

| Edit | Median first paint | Observed p95, five edits |
| --- | --- | --- |
| Camera | 80 ms | 85 ms |
| All 100k instance transforms | 527 ms | 545 ms |
| Camera during geometry capture | 95 ms | 97 ms |
| All transforms during geometry capture | 574 ms | 641 ms |

Changed geometry settles roughly 4.3-5.2 seconds after those navigation edits.
The emitter-heavy fixture still spends substantial time updating the light
tree. Active heartbeat gaps reach about 120 ms, including host SOP cooking;
initialization reaches 744 ms. The ordinary await-paint heartbeat p95 is about
17 ms. Neither these scripted observations nor five-edit percentiles certify
physical input, compositor latency or the prospective production budgets.

Two cache defects found during validation were corrected: packed roots rejoin
one validated geometry resource after their displacement bindings become equal;
searching entirely absent outer fields no longer disables a static recipe.
Missing-field warnings survive warm reuse without extra shutter cooks, and SOP
cook/dependency changes still invalidate the recipe when the field is repaired.
Authored outer instance fields still require reacquisition. A positive material
probe also rechecks previously surface-only prototypes when displacement is
enabled on them.

The packed evidence record (`artifacts/packed-motion-cooperative-evidence-20261020.json`)
selects 48 passing runs on one compiled runtime, including CPU, both individual
OptiX cards and a mixed 4090/3080 Ti render. It covers motion, mixed geometry,
92 independent binding checks, 100 packed attribute checks, 128 Cd checks,
source/crease recovery, 27 retention/repair cases, animated object/CVEX/scoped
shutter values and ordered output. Seven GUI/ownership workflows include both
cooperative viewers, concurrent edits/deletion, the heavy preview and live
packed attribute/Cd repairs. The native workflows check displayed pixels;
workers remain stable and tracked allocations reach zero on close. Test-only
staging preserves binaries, kernels, patches and immutable runtime contents.
All development failures remain source-audited and archived.

## Capture pump budget: tried and not kept (2026-11-16)

Chaining host steps within 45 ms per `_pump_capture` call (instead of one step
per call, with the not-before gate cleared between steps) was measured on the
Threads scene (500,000 moving packed instances, OptiX) and not kept. Median time
to capture done, old against chained:

| Path (poll interval) | Blur, frame change | Blur, first render | No blur, frame change |
| --- | --- | --- | --- |
| Native Render View (about 54 ms), 6 pairs | 5.71 s / 4.78 s (-16 %) | 5.77 s / 4.81 s (-17 %) | 3.80 s / 3.39 s (-11 %) |
| Cycles viewer panel (16 ms timer), 2 pairs | about 4.1 s both | 3.7-4.4 s both | 3.0 s / 2.9-4.6 s (not shown not slower) |
| hython, emulated 66 ms rhythm | 5.34-5.62 s / 4.34-4.48 s | 4.39 s / 3.38 s | 2.77-3.12 s / 2.03-2.31 s |

The Cycles viewer panel, the artist path, gains nothing because it already pumps
about every 14 ms. In the native view the gain is under 15 % without blur. The
longest pump per update rose from 33 ms to 44 ms (median), which costs interface
responsiveness. Render to Disk and MPlay poll every 20 ms; background and hython
renders never use this pump. The probes and raw results are internal and not published.

## Remaining work

The 20 ms target is not a guarantee for every host operation. Individual
SOP/parser/VEX/graph calls cannot be preempted. Many distinct graph requests, distinct/deep packed hierarchies, new shutter
source work still need profiling. Successful scene publication now has
[bounded converter-owned retirement](native-scene-publication.md).
[Queued cancellation and close](native-capture-retirement.md) also release owned
state on the converter. Raw abandonment and individual host-only operations
still need separate profiling. The measured shared-leaf
path now resumes acquisition and grouping, and expands owned placement/pose
tables with the converter. Structural recipe storage is bounded by captured geometry and
retires under exclusive converter ownership after validation; this is not an
extended memory certificate. Physical mouse navigation, extended mixed-device testing and
reproducible releases remain separate work. Physical surface-only engine
mesh/BVH sharing is unchanged. No Loop subdivision work was added.
