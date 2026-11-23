# Phase 2 renderer decisions

Measured 2026-11-10 against the private Houdini 22.0.466 renderer built from `ac553896`, with Cycles fork `151c8b05` and upstream base `74e9d9e4`. The tested build manifest SHA-256 was `e95612e0c3c50c116f7627ab78d05b398abd9b148a9320bfa88dd4dfd388a34a`. NVIDIA driver 621.14 ran the OptiX tests.

## Direct-light sampling

Keep **Resampled Light Sampling** as an artist option, with standard Cycles light sampling as the default. The predeclared K26 outcome is **inconclusive**. The two lighting tests that lost would have triggered removal only if the particle scene had also lost or stayed neutral. Its win is reproducible across all five pairs. The evidence supports enabling RIS for shots similar to this particle scene and checking noise on a representative frame of a new shot. [Decision criteria](../dev/heavy-renderer-tests.md)

| Scene | Median RIS / standard RMS noise | Five seed pairs | Median wall time, standard / RIS |
| --- | ---: | --- | ---: |
| Artist particles, Image Iris, 1280 × 720 | 0.619 | 5 wins | 22.57 / 22.61 s |
| 25-area-light room, 640 × 360 | 1.141 | 5 losses | 21.95 / 22.01 s |
| Glossy interior, 640 × 360 | 1.960 | 5 losses | 22.05 / 21.95 s |

The same RTX 4090 and the same renderer ran both modes. Turning RIS off selected the fork's standard Cycles sampling path. Each scene used ten independent seeds per mode, randomized within paired runs, targeting 20 seconds per render. All reported render times fell between 20.14 and 21.00 seconds. An on/off contrast at a fixed sample count exceeded the same-seed off/off repeat noise in every scene. The two authored interiors were already clean in absolute terms. For the glossy scene, pooled scene-linear RMS noise rose from 0.000805 to 0.001565. On the artist scene it fell from 0.5462 to 0.3377.

Four independent 60-second standard-sampler renders per scene provided a second check. The median ratio of RIS error to standard-sampler error against their mean was 0.655 on particles, 1.121 in the many-light room and 1.727 in the glossy interior. All three passed the predeclared regional brightness check. The largest regional residual was 0.097%, on particles. These references have sampling noise, and the regional check has finite sensitivity.

## Interactive light-tree updates

The primary fixture has 1,048,576 triangles in a shared mesh, twelve instances and four emission palettes. The ordinary control uses the same instance and palette setup on a small mesh. The pinned pristine tree does not support per-object material slots. The matched reference uses its construction and flattening algorithms with palette-aware shader lookup, sharing the production fork's other libraries with the optimized arm. A separate uniform-material control runs the genuinely pristine engine. Both timed arms used two render threads on distinct physical cores of a Ryzen 9 7950X. Each measurement runs from submitting an edit to completing a one-sample CPU frame and excludes viewer paint.

| Fixture and edit | Median paired saving | 96% interval for the median |
| --- | ---: | ---: |
| Original, transform | 6,010.58 ms | 5,920.22 to 6,304.20 ms |
| Original, all four emission colors | 28.09 ms | −49.93 to 212.40 ms |
| Ordinary, transform | −0.02 ms | −0.06 to 0.02 ms |
| Ordinary, emission | 0.00 ms | −0.08 to 0.21 ms |

The original K28 experiment used a threshold of 50 ms on **both** frequent edits. Nine randomized pairs place the transform saving well above the threshold. The emission interval spans the threshold. The historical result under that rule was **inconclusive**. K32 was subsequently adopted and K28 amended, as recorded below. An artist-observed abnormal IPR delay can reopen the decision under the existing rule. [Fork policy](0005-cycles-fork-policy.md) · [Earlier Houdini end-to-end measurements](../history/phase1-focused-checks.md)

Changing just one of the four emission palettes saved **4.37 s** (3.89 to 4.56 s). The next transform edit saved **6.31 s** (6.15 to 6.42 s). These are nine paired trials on the same dense mesh. All 306 primary comparisons matched the independently rebuilt image or the corresponding image from the other implementation exactly. The separate uniform-material control used a pristine CPU build with different library and backend settings, which limits attribution of its timings.

### What to carry at the rebase

**Keep the two cold-construction loop changes in patch 0062.** One limits the right-hand bucket accumulation to the values needed for split cost. The other skips evaluating duplicate costs at empty buckets. The isolated implementation cleared the 50 ms threshold on both dense edits. The recommendation covers these two changes. Timing instrumentation in 0062 was outside the comparison.

| Added optimization | Median transform saving (96% interval) | Median all-palette emission saving (96% interval) |
| --- | ---: | ---: |
| Cold split loops over the matched reference | 885 ms (852 to 1,025) | 775 ms (577 to 1,355) |
| Cache, flattening and upload changes over the cold split loops | 4,758 ms (4,654 to 5,456) | −699 ms (−1,169 to −513) |

**K32 supersedes the original retirement recommendation. Keep the complete light-tree cache family and carry it into the next rebase.** The maintainer confirmed that moving objects is a frequent IPR operation and that a fresh image in milliseconds matters to artists. The split-only arm isolated the cost of caching during a full rebuild. It was an experimental comparator. The policy comparator is the stock tree with the material-slot semantics required by this shot. Against that reference production saved 6.01 s on transforms and 4.37 s on single-palette edits. The all-palette median also favored production, though its interval includes a possible slowdown. K32 settles the carry decision. The eleven patches share one standing feature, **Interactive light-tree updates**. If replay conflicts make the complete feature impractical, bring the split loops and the 0057 material-slot adaptation back for a new decision. [Amended K28](0005-cycles-fork-policy.md) · [Fallback recipe](../../patches/rebase/light-tree/README.md)

The attribution experiment ran nine additional randomized triples on the original fixture. A repeat of the entire bundle against the reference still gave an inconclusive full-emission saving of 27 ms (5 to 351 ms). A separate dense receiver test hid the emitters from the camera and measured the light on a diffuse surface. All 27,648 pixels received light, changing a palette changed the illumination, and all 20 incremental/fresh and cross-implementation comparisons matched exactly.

### Houdini artist-path check

I repeated the edit sequence on RTX 4090 OptiX through the installed private package using isolated Houdini preferences. The same-binary controls gave 7.405 s uncached and 50.5 ms retained from transform edit to completed frame. The full-palette emission edit took 7.603 s uncached and 6.845 s retained. Both arms matched the fresh final render exactly across beauty, position, depth, material ID and three Cryptomatte planes. This single pair illustrates the responsiveness artists have today. It measures the full host path and uses a different control from the nine CPU triples. This installed test did not include a split-only worker. The subsequent native OptiX comparison below isolates that variant. Preserve transform responsiveness when qualifying the rebased feature. [Houdini fixture](../../tests/light_tree_edit_benchmark.py)

## K32 follow-up at automatic thread count

Three rotated triples on the dense fixture used the normal Cycles thread setting (0, automatic), with all 32 logical CPUs available. Each arm used the same one-sample frame and edit history per device. The table reports median edit-to-completed-frame time. Each edited image matched across all three arms on its device. These focused trials show workload and thread-count sensitivity. The nine-pair two-core results above remain the original decision evidence.

| Backend and edit | Matched stock tree | Split loops only | Production |
| --- | ---: | ---: | ---: |
| CPU transform | 2,071 ms | 2,010 ms | 1.5 ms |
| CPU one palette | 2,052 ms | 2,020 ms | 907 ms |
| CPU all palettes | 2,054 ms | 2,008 ms | 2,994 ms |
| OptiX transform | 2,218 ms | 2,116 ms | 4.5 ms |
| OptiX one palette | 2,340 ms | 2,380 ms | 983 ms |
| OptiX all palettes | 2,367 ms | 2,415 ms | 2,833 ms |

At automatic thread count all three paired full-palette edits were slower in production than in the matched stock tree on each backend. **The K32 owner decision keeps the cache. The full-palette slowdown remains an optimization target.** A separate installed Houdini OptiX worker pair took 3.004 s uncached and 49.1 ms retained on a transform, 3.018 and 3.027 s on all palettes, and 2.982 and 2.919 s on one palette. That installed pair compared two controls in the shipping binary and checked seven image planes, including a fresh final reference. The earlier installed pair used two worker threads. The installed worker timings and the native timings exclude GUI paint. CPU triples (`artifacts/phase2-decisions/k32/CPU.json`) · OptiX triples (`artifacts/phase2-decisions/k32/OptiX.json`) · Installed worker (`artifacts/phase2-decisions/k32/installed.json`)

The maximum cache-owned entry storage was **235,230,930 bytes (224.33 MiB)** against a 256 MiB budget. This is a high-water measurement after every insertion in a separate profiled CPU run. It includes owned entry records and payload capacities. Process RSS, temporary working storage and device buffers are separate costs. On the original two-core pilot, storing snapshots took 645 ms on cold build and 644 ms on the full-palette edit. The automatic-thread CPU profile measured 654 and 646 ms respectively. The duplicate-split optimization was enabled in both profiles. Cache accounting and phase trace (`artifacts/phase2-decisions/k32/retained-peak.log`) · Original two-core trace (`artifacts/phase2-decisions/light-tree/pilot/regression-palette-patched.log`)

To isolate snapshot work, a diagnostic build bypassed only the cache store on a fresh dense full build. With both paths in the same CPU executable, bypassing the store saved 608, 289 and 566 ms in three paired runs (median **566 ms**). All three images matched exactly. The store is a confirmed contributor. The rest of the production-versus-split difference has not been isolated. Skipping the store would also discard the data needed for fast subsequent moves. Store-only experiment (`artifacts/phase2-decisions/k32/store-probe.json`)

## What Blender’s rendered viewport does

Checked Blender 5.2.0 LTS, the installed release with build hash `fbe6228777e7`. Its dependency graph sends ordinary mesh transforms through the object update path. Geometry changes and transform-dependent adaptive subdivision can also invalidate the mesh. The viewport collects those flags before trying the scene mutex and schedules another update if synchronization would block. Drawing can proceed using the available display image while a newer scene update is pending. Measuring a redraw alone cannot establish when the edited lighting first became visible. [Blender dependency-graph sync](https://github.com/blender/blender/blob/v5.2.0/intern/cycles/blender/sync.cpp#L126-L174) · [Blender viewport update and draw](https://github.com/blender/blender/blob/v5.2.0/intern/cycles/blender/session.cpp#L740-L905)

Blender uses the scene’s light-tree setting in rendered view. Moving a mesh whose shader participates in light sampling can tag its lights for update. On the tree update path, Blender constructs a tree and flattens it into device buffers. I found no viewport-specific subtree reuse on that path. The scheduler can deliver lower-resolution feedback during navigation. Our interactive session already enables this scheduler and our IPR queue coalesces edits. For an artist-facing comparison, measure from a drag event to the first displayed pixels with the **new position and new illumination**. A diffuse receiver makes that distinction visible. The measurements above cover completed renderer frames. A matched live Blender/Houdini drag was outside these timings. [Blender integrator settings](https://github.com/blender/blender/blob/v5.2.0/intern/cycles/blender/sync.cpp#L391-L410) · [Emissive object invalidation](https://github.com/blender/blender/blob/v5.2.0/intern/cycles/scene/object.cpp#L221-L237) · [Tree construction](https://github.com/blender/blender/blob/v5.2.0/intern/cycles/scene/light.cpp#L972-L1021) · [Blender navigation sampling](https://github.com/blender/blender/blob/v5.2.0/intern/cycles/integrator/render_scheduler.cpp#L799-L809) · [Houdini session settings](../../src/core/session.cpp) · [Houdini IPR queue](../../houdini/python3.13libs/cycles_houdini/ipr/queued.py)

## Local evidence

The original captures, frozen protocols and independent checks are kept on the development workstation in `artifacts/phase2-decisions` (not part of the repository). The artist HIP and its aperture texture are private.

- RIS captures and reproduction instructions: `ris/run-20261110/README.md`; independent RIS results: `ris.json` in the checks folder
- Light-tree evidence index: `light-tree/evidence-index.json`; independent primary timings and split and receiver checks: `light-tree.json` and `split.json` in the checks folder
- Installed Houdini OptiX check (`optix-host.json`) and the paired trial plot (`results.png`), in the checks folder
