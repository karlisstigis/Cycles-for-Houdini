# Portable input conversion and TX costs

Portable source spaces missing from the artist's OCIO configuration are
converted in the host image loader. Ordinary named spaces continue through the
stock Cycles loader. The portable derivative remains float32 RGBA OpenEXR with
64 × 64 tiles and mipmaps, including for RGB or grayscale sources. This preserves
HDR values and alpha without introducing half-float quantization.

The converter now submits RGB blocks of 4096 pixels to OCIO instead of making
one `applyRGB` call per texel. Its scratch allocation is bounded at 48 KiB. Alpha
is never passed through the color processor. Associated RGB is unassociated only
for positive coverage, transformed, and associated again. Zero and negative
coverage keep their additive RGB; channel-packed and ignored alpha do not cause
unassociation. The existing finite-output sanitization remains in place.

The opt-in Texture Inputs view and the synchronous/worker reports expose
`portable_decode_bytes` and `portable_cache_format`. The byte count is the nominal
full-resolution float4 decode size, summed across tiles. It excludes scratch,
mipmaps, file compression and backend allocations; it does not claim to measure
current resident memory or indicate whether a derivative was used. The format
describes the derivative that this loader can create.

## Measurement procedure

Run `scripts/run-hython.ps1 tests/houdini_portable_texture_benchmark.py` against
the chosen staged build. `CYCLES_HOUDINI_TEST_ARTIFACTS` selects the evidence root.
The fixture defaults to a deterministic 4096² float RGBA EXR, a nonconstant smooth
ramp with fine noise, portable AP0 input, and a custom AP1 working space absent
from the input's declarations. A 32² emission render checks the material path.

Each trial creates a new render session. Uncached trials decode and convert the
source; cold TX trials remove only this invocation's derivative and regenerate
it; warm TX trials disable generation and reuse that derivative. Timings cover
scene application through the completed frame. Session construction and fixture
writing are excluded. OS filesystem caches are retained. Cold TX and warm TX
filter mip levels, so a small image difference from the uncached nearest-texel
render is expected. Independent constant-texel render checks establish color
and alpha correctness.

`CYCLES_HOUDINI_TEXTURE_BENCHMARK_SIZE` and
`CYCLES_HOUDINI_TEXTURE_BENCHMARK_REPEATS` override the 4096/3 defaults. The output
records every trial, source/TX bytes and backend allocation counters. CPU and
OptiX timings must run without another rendering benchmark. Three trials provide
a local range, not a workstation-independent performance guarantee. The first
trial can include backend startup work.

## Recorded results

The comparison used Houdini 22.0.466 and RTX 4090 OptiX. The scalar baseline
host was `20261110T100746426`; the revised production build was
`20261110T103908906`. Both opened the custom `Benchmark Working` color space.
The source was 178,883,556 bytes (170.6 MiB), and the complete mipmapped TX was
244,718,258 bytes (233.4 MiB). Both byte counts were unchanged after batching.
The nominal source decode was 268,435,456 bytes (256 MiB); the cache occupies
about 37% more disk than this compressed source.

Times below are seconds, shown as median [minimum–maximum].

| Backend and route | Scalar baseline | Batched conversion |
| --- | --- | --- |
| CPU uncached | 0.3204 [0.2899–0.3679] | 0.2817 [0.2394–0.3255] |
| CPU cold TX | 0.8534 (one trial) | 0.8139 [0.6926–0.8526] |
| CPU warm TX | 0.0113 [0.0112–0.0117] | 0.0112 [0.0109–0.0114] |
| OptiX uncached | 0.4748 [0.4661–0.4827] | 0.4502 [0.4273–0.4569] |
| OptiX cold TX | 0.8040 (one trial) | 0.9268 [0.7653–0.9599] |
| OptiX warm TX | 0.1471 [0.1470–0.1689] | 0.1794 [0.1693–0.1797] |

The uncached medians fell by 12% on CPU and 5% on OptiX. The CPU ranges
overlap, and warm OptiX also drifted despite doing no portable conversion.
These runs therefore establish local costs and functional cache reuse, with
limited evidence for the magnitude of an end-to-end batching improvement.
Cold generation is substantially more expensive than warm reuse and remains
synchronous during image preparation. The baseline cold timings have only one
trial each, so their apparent before/after differences have little significance.

The backend allocation counter was 270,165,772 bytes uncached and 6,492,632
bytes with TX on CPU. On OptiX it was 1,374,336,720 and 1,110,663,580 bytes.
These counters include each backend's other allocations; they are distinct from
the nominal decode estimate exposed in Texture Inputs. The smooth-ramp TX
render differed from uncached sampling by at most 0.0010672 on both builds,
consistent with mip filtering.

Evidence: scalar trials (`artifacts/portable-tx-baseline/portable-texture-benchmark/1794306793220245700/report.json`),
working-space confirmation (`artifacts/portable-tx-baseline-confirm/portable-texture-benchmark/1794307002062342100/report.json`),
and batched trials (`artifacts/portable-tx-batched/portable-texture-benchmark/1794307320585328200/report.json`).

## Qualification

The native `--owned-image-contracts` check compares 81,970 batched components
against scalar OCIO conversion using a nonlinear transform with a nonidentity
alpha operation. It covers complete blocks and an uneven tail, scalar and RGBA
storage, associated, ignored and channel-packed alpha, and zero, negative,
partial, full and tiny positive coverage. Alpha remains exact. The same check
round-trips the new diagnostic fields through a worker reply.
The rebuilt CPU check passed all 81,970 component comparisons, exact alpha
checks, seven rendered texels, seven safe rejections and 17 image-delta checks.
Native result (`artifacts/portable-tx-batched/stage3/textures/owned-engine/report.json`)

`houdini_color_interop_smoke.py` independently renders portable inputs on CPU
and OptiX with caching enabled and disabled. It checks associated,
unassociated and channel-packed alpha, UDIMs, unwritable-cache fallback, output
color identity and the derivative's actual float4/tiled format. Cache generation
is optional: a failed write retains authoritative source conversion.
All four CPU/OptiX × cached/uncached cases passed on the rebuilt runtime.
Independent render results (`artifacts/portable-tx-batched/stage3/color/portable/houdini-color-interop-smoke.json`)
