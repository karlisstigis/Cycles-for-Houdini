# Blender 5.3 stable source rebase

Reviewed 2026-11-10 under [ADR 0005](0005-cycles-fork-policy.md), after the decision to proceed with the 5.3 rebase while preserving the production features. This is source approval for a private release candidate. Runtime qualification remains a separate required gate; no nightly qualification is claimed.

The new upstream is the exact Cycles mirror of Blender v5.3.0. [Base provenance](../../patches/rebase/blender53/base-provenance.json) records the official Blender tag and the matching Cycles source tree. The [previous lock](../../patches/rebase/blender53/previous-source.lock.json) and [replay record](../../patches/rebase/blender53/replay.json) preserve the old base, fork and patch hashes. The production engine tip is `e60784f1abfad2a435815e79f12812001f1d38e8`. Independent replay reproduced all 84 retained commits and compared 1,055 engine files. The parked ramps/curves and color-session patch pairs remain parked.

Upstream Cycles commit `77edc33168528e06b35540452bce5cce6068c3aa`

The rebase preserves the substantive additions and deletions of these eight session/output seams. Review checked their full diffs against the previous accepted bytes and their placement in the stable host interfaces. Changes to offsets, index hashes and exported commit/provenance headers do not expand their scope. This decision issues new grants for the following exact bytes on the new base; [ADR 0009](0009-integrator-session-output-grants.md) remains the historical grant for the old base.

| Patch | Granted SHA-256 |
| --- | --- |
| `patches/0016-output-sample-statistics.patch` | `8e527b2d5cee07ed7dd67b7fa1975fe33b635935b50e07cc0572f8f1e236d60c` |
| `patches/0017-output-display-ownership.patch` | `9f46b4a0d6c40c088a1782ba42434bd6b05928828009a2d8cd2beb8e94c97efc` |
| `patches/0021-scene-sampling-interruption.patch` | `55c297db52e710205133cf72116ab9c3942aeac81533f43ff4a98c3a6a6e7239` |
| `patches/0022-progressive-output-layout.patch` | `471583fcc6cdcb17ef041a165058dfee4fa365e582259055a987483cceb8b145` |
| `patches/0032-compact-output-passes.patch` | `f6a3ba17df3bfd85f1511a8fc583a6c484f13e93ea4c70d98f8b9379157bf006` |
| `patches/0037-latched-quick-cancellation.patch` | `ca130e31e0ff9697e971a5db55bd13a98c1a972842c0533e4b66ba7ea7ff47a8` |
| `patches/0072-interactive-adaptive-target.patch` | `5b2091290764c59f32188f2417cc429c18f4ed3b3b429a5f77505a9ccb15cedd` |
| `patches/0085-temporal-output-sample-count.patch` | `55bdd8ee007c510e01777b780da059ed582c6431ca103e4023b7e127532b25cd` |

0016 exposes sample statistics through the output tile. 0017 lets a host suppress a redundant display transfer while preserving the default display path. 0021 publishes a scene-update interruption under the transaction mutex and clears it at the safe render boundary; it requires resetting accumulation before accepting the new scene. 0022 exposes the effective output extent, handles denoised buffer layout and expands preview pixels without interpolating IDs. 0032 adds an opt-in compact output API while retaining the existing full-tile contract. 0037 latches explicit host cancellation until the session is idle and propagates atomic cancellation through CPU/GPU work. 0072 lets the host retain its authored adaptive threshold, with the stock progressive-noise-floor default unchanged. 0085 reports cumulative temporal progress for output metadata. These are narrow host scheduling and output grants; none authorizes a kernel change as glue.

Any changed patch bytes, conflict resolution or later upstream rebase expires each grant. A new explicit review must identify the patch, hash and target commit. Editing triage, inventory, the lock or this decision's historical rows cannot renew a grant.

The genuine stable-API adaptations are confined to the retained feature families: 0057 uses the stable object-material-slot context; 0059 and 0074 use the stable device-pointer map lookup and mutex interface; 0074 uses stable integrator-state allocation/free/reestimate while retaining the texture-cache state flags. These remain within interactive light-tree updates, object material slots and nested dielectrics, with no new standing feature. Image Iris, DLSS Ray Reconstruction, Gaussian splats, sampled motion, all other approved features and the existing fixes remain in the locked stack.

The [release receipt](../../patches/rebase/blender53/release-approval.json) binds this decision's hash, the exact previous qualified build manifest, new upstream/tip, ordered patch hashes and clean engine fingerprint. Only that explicit receipt permits source and recompiled kernel changes during baseline comparison. SDK/API, feature flags, dependency revisions and kernel architecture names must still match. The private installer must pass the normal build/stage and installed CPU/OptiX Image Iris renders, feature checks and previous-package rollback. This decision grants no artist activation or public publication.
