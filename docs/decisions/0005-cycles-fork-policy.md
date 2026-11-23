# Maintain a two-tier Cycles fork

Accepted 2026-11-10. This decision governs the next rebase and qualifies the earlier decisions on [RIS](0003-direct-light-sampling.md) and [upstream proposals](0004-local-upstream-proposals.md).

The rebase target is the latest stable Cycles release. Houdini integration glue has no numerical cap. Changes to engine rendering behavior require a named feature or a tracked correctness fix. The cap counts approved features, so splitting one feature into several commits does not consume additional entries.

| Standing feature whitelist | Scope |
| --- | --- |
| Nested dielectrics | Priority-based nested media |
| Image Iris | Image-based aperture sampling |
| Fixed and bounded subdivision | Authored subdivision levels and tessellation bounds |
| DLSS Ray Reconstruction | Interactive reconstruction |
| Native Gaussian splats | Splat rendering and retained native attributes |
| Sampled shutter motion | Point lifetimes, sampled displacement, mesh/point/curve attributes, tangent bases, normal mapping, object fields and camera optics/intrinsics |
| Object material slots | Per-object slots on shared geometry and their shared mesh-light semantics |
| Shadow catcher | World background as a shadow-catcher reference |
| Interactive light-tree updates | Retained mesh-light subtrees and their construction, flattening, invalidation, upload and diagnostics for IPR (K32) |

K29–K31 resolve the historical patch review. The eight session/output seams (0016, 0017, 0021, 0022, 0032, 0037, 0072 and 0085) qualify as glue at their reviewed hashes. They touch integrator files to carry host queries, cancellation or progressive output. This scope grants no kernel exception. Patch 0031 belongs to sampled shutter motion because it depends on the sampled tangent-basis API.

[Patch triage](../../patches/triage.json) owns policy. The [source lock](../../source.lock.json) owns replay order and source identity. The agreed CI check must account for every locked patch and check the two records together. Changes under `src/kernel/` or `src/integrator/`, and changes elsewhere that affect rendered behavior, require an approved feature entry or a fix in the upstream queue. Record pending measurements and planned retirements explicitly during migration. Adding a standing feature requires a decision record, a correctness or equal-time case, and relevant furnace/bias validation.

The [session/output grants](0009-integrator-session-output-grants.md) expire on any byte change or upstream rebase. A new grant requires explicit review.

**Pending measurement**

- RIS remains a production option pending an equal-wall-time OptiX comparison against stock light sampling. Declare scenes, reference error, repetitions and the loss criterion before collecting data. A confirmed loss removes RIS in that release and archives the patch on the experiments branch. An inconclusive result leaves the decision open.

**Interactive light-tree updates (K32, amended K28)**

The cache was accepted as the ninth named feature. Keep a light-tree performance change when at least one frequent IPR edit saves 50 ms or more in the paired median on the regression scene and no frequent edit is slower than stock Cycles. Revert when no frequent edit clears 50 ms. Otherwise the result stays open. Use the ordinary scene as a control and check incremental images against fresh builds after emission edits. An artist's observation of abnormal IPR latency takes precedence. Carry the complete feature into the next stable rebase first. Switching to the prepared fallback requires a new owner decision.

**Accepted K32 exception:** the complete production cache remains approved despite
the all-palette emission-edit slowdown at automatic thread count. On the matched
stock-tree comparison, CPU took 2,054 ms versus production's 2,994 ms; OptiX took
2,367 ms versus 2,833 ms. Transforms and single-palette edits retain the measured
interactive benefit. The owner decision explicitly accepts this full-palette
tradeoff; the general no-slower-edit rule above does not overturn that decision.
The slowdown remains an optimization target. Preserve first-frame responsiveness
and the immediately following move when evaluating deferred cache storage.
[First-output and next-move follow-up](../history/light-tree-first-frame-2026-11-10.md)
confirms that simply omitting storage loses the next move's retained benefit;
it qualifies no production engine change.

The [Phase 2 measurements and decisions](0010-phase2-renderer-measurements.md) record the equal-time OptiX results, the matched light-tree comparisons and what to carry in the private build.

**Planned retirement and upstream work**

K33 supersedes the 2026-11-10 ramp qualification decision. Color Ramp, hue ramps, Float Curve, RGB Curves and Vector Curves use Blender's 257-entry convention. The host samples the authored controls and stock Cycles nodes evaluate the tables. Preserve 0045 and 0053 together on a private experiments branch (`experiments/exact-ramps-curves-k33`; commits in [triage.json](../../patches/triage.json), `retired_patches`). An exact implementation can return only as a complete ramps-and-curves pair under a new ADR with a kernel-surface budget. The owner confirmed the existing nine standing features, including interactive light-tree updates. Ramps and curves add no standing feature. The [original qualification results](../history/ramp-curve-parity-2026-11-10.md) document the differences from the archived path.

K22 retires 0011 and 0012 together. The Houdini adapter passes the resolved
OCIO file and context to each renderer worker. The worker owns its configuration.
Patch 0013 remains active for texture alpha. The original host and engine
implementations are archived together for recovery. [Stack and recovery refs](../history/ocio-k22-stack-retirement-2026-11-10.md)

Propose reproducible fixes to stock Cycles upstream. Start with subdivision count overflow (0075) and nonuniform point normals (0003). Each proposal needs an engine-owned reproducer that fails before the isolated fix and passes after it. Features remain in the fork. Submission of any retained light-tree performance work is a separate decision. [Local correctness proposal queue](../dev/upstream-correctness-proposals.md)
