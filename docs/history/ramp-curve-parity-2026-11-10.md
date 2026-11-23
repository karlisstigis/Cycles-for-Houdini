# Host-baked ramp and curve retirement gate

**Historical K32 result, superseded by [K33 in ADR 0005](../decisions/0005-cycles-fork-policy.md).** At the time of this comparison the decision was to keep 0045 and 0053 in production. Option A failed the continuous-ramp, shader-curve and hue checks on both devices. No engine patch was changed or archived. Option B requires a separate decision.

The candidate used the private Houdini 22.0.466 build from `ac553896` and the standard Cycles RGB Ramp, Float Curve, RGB Curves, Vector Curves and Math nodes. The probes linked against the same engine and ramp preparation libraries as the reference. They exercised varying shader inputs through object color on CPU and RTX 4090 OptiX. The chosen numeric bound was the current ramp preparer's comparison bound, absolute 2e-6 plus relative 2e-6, with absolute 4e-6 for hue. The decision note specified parity without a numerical tolerance. The continuous-ridge failure is visible even with a 1% tolerance.

| Case | Candidate representation | CPU and OptiX results |
| --- | --- | --- |
| Continuous linear ridge with black, white, black stops at 0.499, 0.500 and 0.501 | Uniform tables of 4,096 and 16,384 entries baked from authored stops | **Fail at both sizes.** At the apex, reference 1.000000, candidates 0.877898 and 0.969480. Inputs one representable float below and above the stop also failed. |
| Float, RGB and vector shader curves with the same ridge and straight vector handles | Same sizes, sampled directly from the authored straight segments. RGB used an identity combined curve. | **Fail at both sizes.** All three produced the same apex results as the linear ramp. At 0.4995 the 16,384-entry candidate is within 1e-6 of the authored value 0.5, while the maximum difference from the current sampled curve is 0.3720. Matching the authored shape and matching the current 0053 appearance are different requirements on this fixture. |
| HSV hue wrap in near, far, increasing and decreasing directions | Host HueRamp evaluator baked to 4,096 and 16,384 | **Fail at both sizes.** Four ordinary between-sample probes were within 1e-6 at 4,096. Explicit stop and hue-sector probes exposed maximum 16,384-entry errors of 0.0000778, 0.0003715, 0.0000678 and 0.0003017 respectively. |
| Constant displacement band 0.00001 wide, at three off-grid positions | Two stock less-than comparisons and one subtraction | **Pass.** The beauty and depth buffers matched exactly. Depth across the left neighbor, left stop, right neighbor of the left stop, and exclusive right stop was 12, 11.75, 11.75, 12. |
| Animated linear ramp driving true displacement on a sloped mesh | Three independent 4,096-entry tables assigned to the engine's shutter-sample displacement shader slots | **Pass.** The entire 32-sample motion-blurred frame matched the owned path. Freezing all three tables at the middle shutter value changed the mean absolute frame value by 0.01838. |
| Saved and reopened Blender 5.2.0 LTS material with a three-stop HSV ramp | Exported material controls baked into a stock Cycles node; compared four linear-RGB patches against Blender's own EXR | **256 stored entries failed** with maximum error 0.001397. **257 passed** with maximum error 2.98e-8. Blender's 256 denotes intervals and its exporter adds the endpoint. [Blender exporter](https://github.com/blender/blender/blob/v5.2.0/intern/cycles/blender/util.h#L122-L136) |

The peak at 0.5 is halfway between table entries for either permitted size. Linear interpolation between two samples below the peak cannot recover its height. It is a continuous feature, so the exact discontinuity route in option A does not cover it. Extending stock-node lowering to continuous linear segments would be a separate candidate to test. It would not by itself address general curved handles or hue-sector transitions.

These were isolated shader and displacement comparisons using production libraries. The Blender fixture exercised material export, table reconstruction and a real Blender render. It did not install a candidate Houdini importer. During fixture development a disconnected mesh with a displacement shader caused invalid native runs. The final displacement fixtures use only the per-object meshes and were rerun on both devices.

## Recheck the saved frames

From the project root run `python tests/ramp_curve_parity_gate.py artifacts/ramp-curve-gate`. It compares native float buffers, checks that the narrow displacement band is visible, checks the shutter freeze control and reads Blender's reference pixels. Its expected exit code for this capture is **1**. The 256-entry Blender negative control is reported separately from the 257-entry compatibility result. Per-case results (`artifacts/ramp-curve-gate/verdict.json`) · Native probes, bake scripts, .blend, EXR and logs (`artifacts/ramp-curve-gate`)

## K33 production comparison

The reference for K33 is a linear, single-sample render from Blender 5.2.0 LTS
(`fbe6228777e7`). The focused gate imports its material records into Houdini
22.0.466 and renders them with the corresponding backend. It checks the input
values, neutral emission, actual device and sample count. The cases cover
linear and constant RGB ramps, four hue directions, constant-mode HSV/HSL
color and alpha, and Float, RGB and Vector Curves. The nonlinear RGB case
includes an edited Combined curve. See [the focused test instructions](../../tests/TESTING.md).

| Comparison | Observed result |
| --- | --- |
| RTX 4090 OptiX against Blender OptiX | All 15 materials matched bit-for-bit at every probe. |
| Default CPU against Blender CPU | 14 materials matched bit-for-bit. Two probes on nonlinear RGB Curves differed by one float32 step. Maximum absolute difference was 5.9604645e-8. |
| CPU diagnostic with AVX2 disabled for one process | All 15 matched Blender CPU bit-for-bit. The production CPU configuration retains AVX2. |
| Animated ramp driving true displacement | The Houdini ramp matched the independently animated scalar on CPU and OptiX. Freezing the endpoint at the middle shutter value changed the mean absolute output by 0.0602 and 0.0601 across the affected pixels respectively. |

At 257 entries the original ramp peak at 0.5 hits a table sample and reaches
1.0. Its flank at 0.4995 widens to 0.8720. Moving the peak to 0.5007 lowers
its apex to 0.24624. Those are measured Blender values. The location of a
narrow feature relative to the sampling grid matters as much as its width.

The CPU discrepancy is reproducible with the exported float32 table itself.
For the probe at 0.912339985, rounding both weighted products before adding
gives 0.859138608. A fused multiply/add gives 0.859138668. The AVX2
diagnostic isolates the difference to the CPU execution path. Both GPU
implementations give the latter value.

The owner approved the measured fused CPU rounding at those two probes. The gate
accepts the adjacent float32 value recorded by Blender OptiX there and continues
to report the error against Blender CPU. All other probes retain the 3e-8 limit.
