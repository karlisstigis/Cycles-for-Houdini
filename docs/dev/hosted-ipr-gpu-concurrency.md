# Hosted interactive GPU concurrency

Blender 5.3 can grow concurrent GPU path states into spare device memory. On the
isolated candidate, the hosted scene measured approximately 9.8 GiB with growth
and 1.64 GiB with `CYCLES_CONCURRENT_STATES_GROW_PERCENT=0`; growth improved final
render throughput. These are diagnostic measurements from before this policy,
not qualification results for the patch.

Hosted interactive sessions now set `SessionParams::use_gpu_concurrent_states_growth`
to false. The value is copied before rendering to the session's root device and
each physical render device. Its only queue effect is to default `grow_percent`
to zero before the existing environment overrides. Normal device concurrency
remains the baseline, and the existing memory-budget calculation can still
shrink it when necessary. Reserve, min/max clamps, factor overrides, logging and
statistics retain their existing behavior. Denoising-only contexts do not use
the integrator-state calculation and need no policy change.

The session and device flag defaults remain true, preserving standalone Cycles
and final-render growth. A policy change is included in `SessionParams::modified`
so it recreates the session rather than reusing an already allocated state
capacity. No environment variables or other process-global settings are changed
by the integration. A user-provided `CYCLES_CONCURRENT_STATES_GROW_PERCENT` takes
precedence, including a positive value for an interactive session.

`tests/core_gpu_concurrency_smoke.py` launches three isolated harness processes.
The harness uses the production `DeviceQueue::num_concurrent_states` with mock
devices and controlled memory, requiring no GPU context. It checks native
defaults, session recreation, baseline versus growth, low-memory shrink,
device isolation, and the growth, baseline, min/max, reserve and factor overrides.
All three modes passed against the isolated candidate native harness, nine
checks per mode. The candidate's `qa/gpu-concurrency-mock-contracts.json` receipt
records the executable hash and confirms it was unchanged during the run.
The parent qualification run must additionally compare the real hosted scene's
memory and throughput for interactive/final and explicit growth controls on the
selected GPU devices; the mock fixture makes no GPU performance claim.
