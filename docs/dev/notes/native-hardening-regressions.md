# Native hardening regression checks

Run native checks with an explicit Houdini target and immutable staged build.
The launchers check the SDK version, payload hashes and the runtime actually
loaded inside the Houdini process. A relocated package must contain the exact
staged build manifest in its provenance. Its shipped Python, native library,
HDA, resources and release ID are checked before a fixture executes. Unrelated
studio packages can remain in the selected package directory.

```powershell
pwsh -File scripts/verify-native-release.ps1 -Cases tests/native-hardening-cases.json -HoudiniRoot "C:/Program Files/Side Effects Software/Houdini 22.0.466" -BuildDirectory build/windows-h22.0.466-release -Name hardening
```

The selected matrix exercises COP resolution changes followed by spaced camera
edits, cached tessellation and explicit refresh during IPR, and warm camera,
object and material edits on a million-triangle fixture. The COP check retains
name-only ROP identity interests while requiring zero camera-driven pixel cooks
and zero extra image acquisitions. Genuine source edits still change the image.
Tessellation checks assert the existing update policy and persistent worker.
The response fixture checks geometry retention, first image paint, settled
results and clean shutdown, and records UI heartbeat delays separately.

The existing latency targets are camera p95 at most 150 ms and transform/material
p95 below 500 ms. Reports retain the measured values and whether each target
was met. A successful correctness check alone does not certify its latency.
The fixtures use programmatic parameter edits and Qt events. Physical input and
monitor presentation require the separate native viewport drag workflow.

Use fresh per-run preferences containing `__HVER__`. GUI checks accept explicit
`-PackageDirectory` and `-UserPrefDirectory` overrides, which permits qualifying
the same fixture on a relocated installation. Tests archive their process exit
code, fresh report and loaded runtime identity. Run source audits before editing
the checked sources. The serialized build/test queue remains the owner of all
native executions.

`tests/houdini_point_instancing_perf_benchmark.py` provides a separate 100k-point
instancer and packed-twin comparison. Its transform edits must reuse all three
prototypes without geometry conversions. It measures authoritative acquisition
and first rendered frame, independently of Viewer painting. This distinction
must be retained when comparing it with the GUI latency results.

The maintained release command also checks installed ROP layout, expressions,
animation, custom grouping, existing-HIP loading, worker recovery, saved-HIP
farm output, and real Image Iris renders on CPU and GPU. Historical rollback
payloads, when explicitly supplied, are checked against their own manifest and release identity.

`tests/houdini_session_soak.py` repeatedly changes the scene, renders, closes the
Viewer and clears the HIP in an owned GUI process. It records active and retired
worker allocation ledgers, host and worker memory, and process retirement. The
November 7 CPU run covered three minutes, 156 checkpoints and 13 clean worker
retirements. This is a bounded small-scene lifecycle check, not proof against
all long-duration or GPU memory growth.

`tests/houdini_background_upgrade_smoke.py` installs two real, different
same-target releases in disposable roots. A long submitting-runtime render must
continue accumulating samples after active registration switches; a
fresh process must load the other release and render Image Iris. Completion,
saved output and Stop are checked separately. This fixture must never use artist
preferences or activate an artist update.

`tests/houdini_system_input_fixture.py` exposes a disposable viewport and Viewer
for native system-mouse transform/rotate and camera navigation checks. Its pose,
generation and geometry-conversion evidence is separate from synthetic parameter
tests. OS-injected gestures do not measure in-drag presentation latency or prove
physical monitor scanout.

The system-input OptiX qualification on November 7 used a 102,832-triangle toy,
one RTX 4090 worker and native camera-pivot/object transform handles. Over 84
seconds, 11 of 12 observed strokes met the repeated pose-change, new physical
render revision and matching painted-image contract. Geometry converted once.
The final live frame and painted canvas matched a fresh capture within the
recorded tolerances. The test does not qualify Alt/Space tumble chords or
physical in-drag latency. Actual report (`artifacts/stage1/gui/sustained-system-input-optix05/houdini_system_input_fixture.json`)

The large soak profile retains 526,336 unique triangles and 4,096 packed
instances (8,912,896 effective triangles), a 1024-square file image and a
512-square COP image. Sustained same-worker dwells alternate with scene edits,
source repairs, Viewer closure and HIP clearing. Image checks use analytic
reference colors at the display buffer's precision. Host/worker/device growth
allowances derive from retained scene/image sizes; selected-GPU telemetry must
exist and be nonzero. A bounded soak is not a guarantee against all later leaks.
