# Blender 5.3 engine port

For the production release, see the [Blender 5.3.0 stable rebase](../history/stable53-rebase-2026-11-10.md).

## Historical release candidate

This isolated candidate pins standalone Cycles `74e9d9e48f86330987d7392166a51b3652d7c557` (2026-10-26) in place of `a456b761034dda42c32eef9f4aae0fa5a5c9f604`. It is a release-candidate integration; the final Blender 5.3 release has not been substituted. The production checkout and artist installation are separate and unchanged by this candidate.

All 76 existing renderer patch contracts remain in order. Patches were reconstructed as exact old-base commits, cherry-picked onto the RC and regenerated against each preceding new-base patch. The full-source replay report in `artifacts/patch-replay-rc/report.json` passed with no missing, extra or changed engine source before the additional RC patches. `scripts/verify-upstream-patches.py` remains the authoritative source-consistency check after every later change; source replay does not qualify a renderer backend.

Important rebase decisions:

- Preserve the sphere inverse-transpose normal fix within the new GSplat normal branch.
- Keep RC `KernelObject.mesh_volume` storage, while retaining independent generic sampled-attribute strides and host volume shutter offset.
- Port particle lifetime filtering to the RC Embree object primitive-offset lookup and combined standard attribute type/element API.
- Keep the RC attribute deduplication while retaining independent backing-buffer copy decisions and emitter source-dirty semantics.
- Retain RC interactive DLSS first-sample restarts while allowing latched host interruption to cancel promptly.
- Allocate distinct shader/runtime flag bits for wavelength dependence, nested dielectrics and sampled-object displacement.
- Preserve anisotropic Principled transmission, Glass BSDF anisotropy/tangent/rotation, the new microfacet implementation and new GPU queue/kernel paths.
- Keep the Houdini SDK OIDN provider, all Image Iris patches and `CH_IMAGE_IRIS=ON` defaults.

## Spectral nested media

Patch 0078 evaluates an absolute medium IOR at the path's shared wavelength before storing it in the priority stack. The interface then uses `n_material(lambda) / n_opposite(lambda)`. Applying Cauchy dispersion directly to `n_material(d) / n_opposite(d)` is not equivalent and is explicitly avoided.

An entry's negative sign marks a wavelength-dependent medium; its magnitude is the positive physical IOR. This marker survives the existing CPU/GPU array copies, priority reordering, camera-inside probe and subsurface replay without changing the path-state memory layout. It is decoded at an active interface. A non-dispersive closure against a dispersive opposite medium also receives spectral throughput weighting. Camera, subsurface and MNEE entry evaluations initialize the same path wavelength, including graph-driven IORs. Constant d-line IORs with dispersion force probe evaluation instead of using the non-dispersive constant shortcut.

The native nested-media MNEE guard is retained: actual non-air/buried nested interactions use ordinary path transport rather than the air-interface MNEE solver. This is fallback behavior, not a newly implemented nested MNEE manifold solver. Existing BSSRDF/thin-wall participation restrictions remain. OSL is disabled for the native build and has not been extended or qualified for the custom nested path.

The independent `ch_harness --dispersion-contracts` checks the real shared Cauchy helper against d-line/Abbe identities and Snell's law through two distinct media. `tests/houdini_spectral_nested_smoke.py` renders zero-dispersion controls, two Abbe media, camera-inside/procedural IOR cases, MNEE guard activation and live edits.

The image fixture also contains an explicit-interface oracle independent of the implementation: choosing

`Abbe_inner = Abbe_outer * ((n_inner(d)-1) * n_outer(d)) / ((n_outer(d)-1) * n_inner(d))`

makes the two Cauchy functions proportional at every wavelength. Their exact interface ratio is therefore the constant `n_inner(d)/n_outer(d)`. A camera inside those strongly dispersive media must match a separate render using that known interface. The reference retains spectral RGB integration with an effectively infinite Abbe number so both images receive the same per-path wavelength weights. The test records image RMSE and peak errors.

## Qualification evidence

Record actual CPU, CUDA and OptiX results in the candidate QA reports before calling any backend qualified. Shader socket exposure and patch replay alone establish no render qualification. Required candidate checks include the explicit-interface oracle, zero-scale/zero-Abbe controls, glass anisotropy/rotation and native Image Iris rendering. No artist installation should be activated from these source-only results.

## Recorded isolated optics qualification

The bounded optics matrix passed on CPU, OptiX RTX 4090 and CUDA RTX 3080 Ti: twenty renders per backend, including zero-dispersion/zero-Abbe controls, an independent fresh-session same-seed repeat, two different Abbe media, camera-inside and procedural IOR probes, MNEE guard activation, live edits/restoration, Glass anisotropy/rotation and active RGB Image Iris in both CDF and Hierarchical modes. GPU controls use 3e-6 absolute/relative float-accumulation tolerance and record the observed error separately; CPU controls are exact. Uniform RGB pupil transmission factors through nested spectral transport within 8.59e-6, with unchanged coverage alpha. The stricter pupil factorization fixture disables roulette to keep path survival independent of filter tint.

At 512 samples the independent proportional-Cauchy interface image oracle records CPU RMSE 1.1237e-4/peak 8.6499e-3, OptiX4090 RMSE 1.1906e-7/peak 1.9074e-6 and CUDA3080Ti RMSE 4.3827e-7/peak 1.5498e-6. All bounded checks passed without unresolved camera/SSS probes in normal scenes.

An official portable Blender 5.3 RC f78a5e153839 was downloaded from Blender's HTTPS build service and matched its official SHA-256. A shared explicit planar-prism/slit/camera rig produced matched linear EXRs. Official vs native CPU enabled-dispersion profiles have NRMSE 5.14e-6 and centroid error1.93e-5 pixels. Direct official/native OptiX4090 profiles are identical with dispersion disabled; enabled NRMSE is 5.18e-8 and centroid error6.87e-8 pixels. Both also pass an independent analytic Snell image-location oracle. See candidate QA `blender-reference/README.md`, `comparison.json` and `comparison-optix4090.json`.

Existing nested plumbing passes 18 checks, including intentional depth/probe-limit warnings and structured per-frame farm provenance. CPU IPR passes eight paced mid-accumulation edits plus six burst edits; 31 published frames remain monotonic, and the settled final image is bit-identical to a fresh final-state render. Native host contracts pass 71 dispersion checks,212 subdivision checks plus 32 overflow checks,71 hue checks and64 IES profiles with stable interned-string ownership.

`../../qa/engine53-qualification.json` binds the selected raw reports by hash. These results qualify their named feature/backend executions on the recorded candidate binaries. They do not activate an artist installation. The official OCIO snapshot limitation and the canonical matched-reference control are explicitly retained in the reference README.
