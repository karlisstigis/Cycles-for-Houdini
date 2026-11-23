# Native Cycles denoising

Denoising is optional and defaults off. Select the Cycles ROP in Houdini's native
parameter editor, then enable **Denoising** in its Rendering tab. Native IPR, the dedicated
panel and SOHO batch rendering share these settings.

The native milestone pins Open Image Denoise to the matching Houdini 22.0.429
SDK provider, version 2.3.3, for both in-process rendering and workers. SDK DLLs
are not redistributed. Worker denoising previously used the vendor 2.5 runtime;
the unified provider intentionally changes that version and does not claim
identical models, image quality or performance. Actual loaded DLL paths,
versions and hashes are checked by `tests/houdini_oidn_provider_smoke.py` on a
noisy diffuse scene, including each quality, prefilter and guide selection.

## Device policy and controls

Automatic uses OpenImageDenoise on the selected render device when supported, with
OptiX as the supported OptiX-device alternative. CPU rendering uses CPU
OpenImageDenoise. An explicit unsupported request fails visibly.

**Use GPU for denoising** selects the supported render GPU for OpenImageDenoise.
Turning it off explicitly chooses CPU OpenImageDenoise while GPU rendering
continues. OptiX requires the selected OptiX GPU. This does not silently change the
render device. GPU denoising uses additional memory.

The ROP's Devices tab supports multiple checked GPUs through one CUDA or OptiX
backend. Cycles chooses a supported member of the same render MultiDevice for
denoising, reusing its contexts. Status diagnostics identify the actual denoising
card; CPU denoising remains an explicit choice.

OIDN quality offers High, Balanced and Fast. Guide prefilter offers Accurate, Fast
and None; None assumes clean guides. The IPR start sample defaults to 16. Cycles
schedules progressive denoising, and the final image is denoised even when the
total sample count is smaller.

Changing denoising settings resets sampling while retaining compatible geometry.
It can change the native viewer's plane schema, so the tile display is reopened
with the matching planes.

## Pass behavior

With denoising enabled, `combined` is denoised beauty and `noisy_combined` retains
the noisy beauty. Both update progressively through Cycles' OutputDriver at
float32 precision; its DisplayDriver cannot overwrite these planes with a
single half-float result. Alpha coverage is retained.

The current final pass set also includes depth, normal, UV, position,
`denoising_albedo` and `denoising_normal`. These raw data/guide planes are available
on completion. This increment does not implement progressive arbitrary AOVs.
Normals and other data bypass OCIO view transforms and denoising.

## Evidence and limits

`tests/houdini_denoising_smoke.py` checks CPU OIDN, RTX 4090 GPU OIDN,
explicit CPU OIDN with GPU rendering, and OptiX. Noisy beauty is compared to the
same sampling without denoising; data passes, finite pixels, exact alpha coverage,
live disable, geometry retention and zero tracked allocations after closure are
checked. Results are in `artifacts/stage3/denoising/`.

`tests/houdini_denoising_ipr_smoke.py` exercises native IPR, the shared panel and a
native parameter editor. It enables/disables denoising during tracing, compares
native pixels to the engine's progressive float32 output, verifies the noisy
reference and plane schema, and requires an advancing live render generation
after Stop/restart. Its small constant-emission scene is a lifecycle/pixel-transfer
fixture, not a denoising-quality benchmark.

Statistical image-quality improvement, temporal stability, broad FX denoising
costs and additional GPU/platform certification remain open.
