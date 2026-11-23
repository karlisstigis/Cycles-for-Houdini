# Dedicated panel GPU display conversion

## Viewer background composited in scene-linear light - November 17, 2026

Before this change the viewer unassociated colour (RGB / alpha), applied
exposure and the OCIO view to that straight colour, and then blended it over the
background or checker with alpha in display values (Qt premultiply and
SourceOver). Opaque pixels were right. Partly transparent pixels were far too
dark: RGB 0.45 at alpha 0.13 tone-maps 3.4 to nearly white and is then
multiplied by 0.13 in display values. Compositing applications show
View(RGB_premult + (1 - alpha) * background) instead.

The viewer now shows, for every colour view with alpha (RGBA, or RGB with ×A):

    displayed = (View(2^exposure * RGB_premult + (1 - alpha) * B_linear))^(1 / gamma)

- B_linear is the background in the working space, chosen so that an empty
  pixel shows exactly the chosen bytes: the inverse of the same view and display
  (after undoing the display gamma), corrected by up to eight
  B += Vinv(target) - Vinv(View(B)) steps when a view's inverse misses a byte
  (ACES 1.0 SDR Video did for one saturated colour). A view without an inverse
  falls back to the inverse of its display colour space.
- Exposure applies to the image only; the background stays the chosen colour.
- Each background colour that can be under a pixel gets its own opaque
  composite: one for a solid background (the chosen colour or the theme's
  canvas colour), two for the checker (its dark and light colour). The checker
  stays pinned to the screen; the canvas takes the light composite on the light
  cells. `display_image` returns them as `composites` next to the unchanged
  straight `rgba`; `display_backgrounds_linear` computes B_linear once in C++ for
  both routes.
- The GPU route draws the straight image and the composites as stacked rows of
  one framebuffer: still one draw and one readback per frame. The CPU route
  transforms the straight image and the composites as one buffer.
- RGB, R, G and B without ×A (`channels="rgb"`, the `opaque` uniform on the
  GPU route) show the image over black since November 17, 2026: the CPU route
  and the shader no longer divide by alpha for that view, so it is
  (View(2^exposure * RGB_premult))^(1 / gamma), opaque, as Nuke's RGB view;
  the component views take one channel of it. Opaque pixels and alpha-0
  additive RGB give the same bytes as before.
- The straight `rgba` keeps its meaning: PNG Save, the pixel readout, R/G/B
  component views with ×A, RGB without ×A (over black, above), Alpha, raw, data and Cryptomatte are
  unchanged and get no composites. Frames from another rendering policy (the
  interchange route) get none either.
- The canvas (GPU shader, and QPainter `paint_scene` for the native and classic
  modes and the GL fallback) draws the straight image as before and then the
  composites over it. Composites are transparent outside the converted region,
  so those pixels keep the straight blend. They are used only while the
  viewer's backgrounds equal the ones they were made over; otherwise the
  display-space blend of the straight image is shown.
- A background, checker or theme change reconverts the live frame.
- Snapshots, the A/B compare image and history keep the composites made over
  the backgrounds of the moment they were taken. They show them while the
  viewer has the same backgrounds (the checker composites follow the screen
  checker, since each checker colour has its own composite); after a background
  or theme change they show the old display-space blend. No float data is kept.
  The 64 MiB history budget counts the composites: at 2048 x 2048 it holds 4
  items without composites, 2 with a solid background and 1 with the checker.
- JPEG Save of the live frame converts it over black at save time,
  View(RGB_premult). A snapshot JPEG keeps the display-space composite on black
  of its straight bytes. ROP JPEG output (both the default view and Output Color
  Override) feeds RGB_premult to OCIO without dividing by alpha. PNG keeps
  straight alpha: View(RGB / alpha) bytes with alpha.

Measured on the 2048 x 2048 Threads frame (ACES 2.0 SDR view, RTX 4090, single
run of 9 warm conversions after 2 warm-ups, median ms):

| Route | No background (as before) | Solid | Checker |
| --- | --- | --- | --- |
| GPU, whole conversion | 11.3 | 14.9 | 16.9 |
| CPU fallback | 410 | 834 | 1211 |

Headless test: `tests/houdini_viewer_linear_background_smoke.py` (CPU and GPU
routes against an independent OCIO reference, four views, exposure and gamma,
black, orange and checker backgrounds). The CPU route equals the reference
within 1/255; the GPU route equals the CPU route within 1/255.

## Owned input/readback buffers - October 17, 2026

Host `20261017T194800117` replaces the original per-frame float copy with a read-only
buffer exporter that owns the immutable source frame. The internal native helper
retains its copied-byte default; the installed converter requests the owned view.
Each consumer holds its owner through Python's
[buffer protocol](https://docs.python.org/3.13/c-api/buffer.html).
Frame and color metadata outlive renderer/capsule retirement.

GPU readback is retained as a sealed allocation behind a read-only byte view.
Full-size display output avoids another 8-bit copy under the GIL. Compact output
still expands into independent bytes. Input/output lifetime, forbidden writes,
invalid storage, odd crops, context failure and viewer retirement are verified.
One active conversion, one replaceable pending input and one completed result
remain bounded; GL resources retain their existing thread ownership.

At 4K the warm background beauty conversion probe changes from 54.0 to 23.4 ms;
foreground image assembly remains roughly 2.4 ms. These are owned-frame conversion
measurements, not live IPR or physical presentation certification. Upload and
readback remain. See [current evidence](../performance.md#owned-display-copies-and-data-packing---october-17-2026).
The original implementation and measurements below predate this buffer change.

The dedicated Cycles panel automatically uses an offscreen OpenGL context for
supported OCIO color views. The render engine and the panel still communicate
through owned CPU image buffers. This change accelerates color conversion; it
does not implement direct Cycles/graphics interoperability or change the artist
IPR execution mode. Native Houdini IPR retains its existing tile-device route.

## Why this change

Profiling the existing 960×540 CPU display route after closing the renderer
separated display cost from tracing. Thirty warm calls per case measured median
view conversion at 37.42 ms: 34.78 ms in OCIO, 0.73 ms preparation and 1.72 ms
packing. Raw and alpha display took approximately 2.5 ms. During the heavy IPR
benchmark, total display conversion was approximately 51 ms. That evidence
favored accelerating the view transform before attempting a new shared-image
transport or replacing the geometry system.

Cycles' own DisplayDriver supports graphics interoperation, and its Hydra driver
implements an OpenGL buffer/texture route. The current native panel uses OCIO's
generated GPU shader with the existing immutable-frame contract. It retains the
panel's QImage canvas, navigation, snapshots and monitor scaling behavior.

## Ownership and color semantics

- Houdini's GUI thread creates an offscreen surface. A bounded display worker
  owns the GL context, shader, LUTs, float input texture and RGBA8 framebuffer.
  There is one active conversion, one replaceable pending input and one completed
  result. Widgets and final painting stay on the GUI thread.
- The native bridge retains the source frame and copies its float pixels into
  owned bytes, releasing the Python GIL during the copy. It supplies a shader
  generated from the immutable current OCIO configuration. A single shared
  transform cache is keyed by configuration, working policy, display and view.
- The shader unassociates nonzero coverage, applies exposure, the OCIO view and
  display gamma, then packs the result. Additive RGB at zero coverage survives.
  Since November 17, 2026 the same draw also writes the scene-linear background
  composites (see the section at the top).
  Coverage is independent of OCIO's output alpha. Direct pixel readback avoids
  Qt's premultiplied-image interpretation. Linear render buffers and EXRs are
  unchanged.
- Raw, alpha, data and Cryptomatte views use CPU conversion. Retained frames
  with a different rendering policy also use the established CPU interchange
  route. GPU shaders needing dynamic uniforms or 3D LUTs currently fall back to
  CPU. Invalid controls retain the existing validation errors.
- A graphics initialization/conversion failure disables GPU conversion for
  that converter and uses CPU output. Closing the panel retires pending work,
  destroys GL resources on the conversion thread, then queues surface destruction
  on the GUI thread. A new panel owns a new converter.

The graphics adapter is selected by Qt/the system, independently of the Cycles
render-device selection. Diagnostics record the actual GL renderer; the measured
setup used RTX 4090. This does not certify other GPU/driver or multi-GPU setups.
Upload, readback and host copies remain. Cold context/shader setup also costs
time: the worker benchmark's maximum conversion was 130 ms, versus a warm median
of 2.75 ms. Direct engine-to-viewer sharing remains separate work.

## Evidence and performance

The matched GUI pair uses the same 960×540 fixture: 1,041,314 source triangles,
354 objects, 605 curves, 1,200 points, three volume fields, a separate adaptive
turbine and animated true displacement. It records actual Qt painting for 54
edits plus two input bursts, on RTX 4090 OptiX without denoising.

| Measurement | Artist worker | In-process test reference |
| --- | --- | --- |
| Camera median / p95 | 94.6 / 122.2 ms | 74.6 / 99.6 ms |
| Transform median / p95 | 249.1 / 366.5 ms | 218.0 / 232.6 ms |
| Surface color median / p95 | 204.0 / 210.8 ms | 188.2 / 208.6 ms |
| Display conversion median / p95 | 2.75 / 3.84 ms | 3.08 / 4.66 ms |
| Camera-burst Qt timer gap p95 / maximum | 37.4 / 49.4 ms | 38.6 / 48.7 ms |
| Distinct previews during camera input | 19 | 24 |

Both paths retained subdivision while the mouse was held and refined once after
release. Normal edits did not issue explicit Stop. All measured display calls
used OpenGL. The preceding CPU-display worker pair measured camera median/p95 at
159/175 ms. These are exploratory, sequential measurements with small sample
counts, not guaranteed production percentiles or a Blender comparison. The
in-process reference remains faster in this pair. Worker isolation alone is
not evidence of lowest latency. Host geometry capture remains a source of UI
pauses and needs its own next optimization.

Frozen benchmark manifest:
`9468ffb40b4d893ca652ac7c19279c382eb7dc94ad8bfc72f3b0ae896e7f3e80`.
Runs under `artifacts/verification`:

- Worker: `prototype-sync-CUDA_NVIDIA_GeForce_RTX_4090_0000_03_00_OptiX-20261012T201643739`.
- In-process reference: `prototype-sync-CUDA_NVIDIA_GeForce_RTX_4090_0000_03_00_OptiX-20261012T201827828`.
- CPU profile: `prototype-sync-CPU-20261012T195634322` (earlier instrumentation manifest).

The full native GUI suite `20261012T202130296` passed all 34 cases with source and
binary provenance verified against manifest
`aa7fbcb94c8ee247d282c4ad206a1425327084605ce411114aaca29f6c942278`.
It includes active shutdown, geometry/material/texture/volume edits, denoising,
navigation, AOVs, color-policy changes and adaptive camera interaction. The
installed GPU converter checks full/cropped RGB/RGBA frames after renderer
closure against the CPU route. GPU results differ by at most one 8-bit level in
these fixtures; raw/data/alpha/old-policy CPU fallbacks match exactly. Six
independent HDR/negative/zero-alpha cases cover linear Rec.709 and ACEScg input,
exposure and gamma with the installed GL executor. Their maximum RGB and alpha
differences are one byte level, not a promise of universal bitwise equivalence.

`artifacts/stage1/gpu-display/verification-summary.json` indexes the increment's
completed runs, including focused failure/cleanup and final-package checks.
Failed development attempts are preserved separately. Long-duration VRAM stress,
other graphics drivers and direct GPU interoperation remain unverified.

Sources:
[Cycles display interface](https://github.com/blender/cycles/blob/a456b761034dda42c32eef9f4aae0fa5a5c9f604/src/session/display_driver.h),
[Hydra graphics route](https://github.com/blender/cycles/blob/a456b761034dda42c32eef9f4aae0fa5a5c9f604/src/hydra/display_driver.cpp),
[OCIO GPU shader API](https://opencolorio.readthedocs.io/en/v2.5.1/api/shaders.html),
[Qt framebuffer readback semantics](https://doc.qt.io/qt-6/qopenglframebufferobject.html#toImage).
