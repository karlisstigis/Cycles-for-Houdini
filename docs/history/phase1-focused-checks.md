# Phase 1 focused checks

## Light-tree edit latency

Five paired CPU trials per scene used the original four-palette regression fixture and a small control with the same instances and edits. Each arm started a fresh worker, warmed the transform path, moved the object, then changed the emissive material. The timer includes Houdini scene capture through completion of a one-sample frame at 192 × 144. Each worker used two CPU render threads. Positive paired savings mean the retained implementation was faster.

| Scene and edit | Median paired saving | Paired range | Uncached p95 | Retained p95 |
| --- | ---: | ---: | ---: | ---: |
| 1,048,576 triangles, transform | 6,992.6 ms | 6,866.5 to 7,660.1 ms | 7,712.6 ms | 54.4 ms |
| 1,048,576 triangles, emission | 227.1 ms | −458.7 to 766.4 ms | 7,861.4 ms | 7,608.7 ms |
| Ordinary, transform | −1.9 ms | −4.3 to −0.7 ms | 54.2 ms | 55.3 ms |
| Ordinary, emission | −1.2 ms | −3.8 to 10.5 ms | 66.3 ms | 65.5 ms |

The reference used the production worker with seven light-tree optimizations disabled. All seven output planes matched a fresh uncached render after the emission edit on both fixtures. A separate receiver-lighting test passed 44 checks. The dense transform result clears the 50 ms threshold across all five pairs. The emission range crosses zero. The K28 decision remains open pending paired stock/patched evidence. The benchmark measures worker completion and excludes viewer paint.

The original scene assigns four emissive palettes to twelve instances sharing one mesh. At the pinned upstream commit, `LightTree` resolves the shader from the mesh and shares a subtree by mesh identity. Patch 0057 adds object shader slots and distinguishes emitters by palette. Duplicating meshes to reproduce the four colors upstream changes the construction and memory costs being measured. For a pristine comparison, use a uniform-material shared-mesh control and report it as a separate scene. A fork comparison retaining 0057 on both arms can address the original fixture directly. [Patch 0057](../../patches/0057-object-material-slots-and-shared-mesh-lights.patch)

Reproduce with `scripts/run-hython.ps1` on the staged build, passing `tests/light_tree_edit_benchmark.py --scene regression --device CPU --pairs 5 --output <new-report.json>`. Use `--scene ordinary` for the small control. The JSON records the manifest hash, both timings for each randomized pair and the maximum error for each image plane.

## Native Render View

`scripts/run-gui-test.ps1 -Test houdini_render_view_controls_gui_smoke.py -Device CPU` starts an isolated Houdini Render View. With Depth selected, an animated camera moves the center pixel from 4.5 on frame 1 to 5.5 on frame 2. The check applies a ROP region of 48 × 32 inside a 96 × 64 image, returns to Beauty and stops the render. It exercises ROP-driven cropping. A mouse-drag region gesture is outside this check.

The first run found that the ROP suspended IPR from its own PreRender callback before a worker could submit an image. The ROP now acquires suspension when SOHO begins a local final. The fast regression covers PreRender and the GUI case exercises the full path.

`houdini_render_view_active_quit_gui_smoke.py` waited for a live 1920 × 1080 Render View, changed the camera, then requested a normal Houdini quit immediately after submission of the next Beauty and Depth frame. Windows recorded process exit 1.32 s later, exit code 0, with the render worker gone. This exercises normal viewer shutdown. It does not establish a shutdown bound for a peer that stops reading.

## Houdini 22.0.466 staged runtime

- The CPU worker trained a valid OpenPGL field 65 times in a 256-sample render. The ROP preserved the saved CPU guiding choice across a GPU round trip.
- Image Iris rendered on CPU and RTX 3080 Ti OptiX with both CDF and hierarchical sampling. The largest measured energy error was 2.27 × 10⁻⁷. The same OptiX worker rendered native splats with DLSS and Image Iris together. Editing splat radiance changed the image over 222 support pixels.
- The stock Geometry ROP and the Cycles ROP executed the same six script calls across a two-frame HOM render. Two ROP Fetch work items each wrote their requested frame. Pre-frame, post-frame and post-render failures retained the original exception and identified the node, script phase and frame in the Cycles summary. A cancelled script left zero active sequences. The native TOP failure log identified the ROP and exception, with the frame available in the Cycles summary.
- The startup check found the studio's `456.py` and observed its `ready.py` and `123.py` alongside the package startup hook. A material-only HIP retained its archive byte-for-byte through an unavailable-renderer save and reopen. An ordinary HIP saved without a Cycles archive.
