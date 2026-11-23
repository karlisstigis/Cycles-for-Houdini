# Native COP images

Image Texture and Environment Texture accept owned `op:` images from legacy
COPs and COPernicus in native OBJ/SOP rendering. Use **Data** for masks, height
and normal maps, or explicitly select the active OCIO input space for color.
Auto accepts a qualified captured input space; an unknown or generic `linear`
label produces a local diagnostic rather than guessing color primaries.

## Capture, updates and ownership

Houdini's public COP resolver acquires and freezes pixels on the host thread at
the requested render time. Owned conversion and validation run on the existing
background conversion executor. Render threads never traverse Houdini COP nodes.
Two-component untagged vector rasters become `(X, Y, 0)`; tagged luminance/alpha
images retain their channel interpretation. Scalar images become grayscale.

The persistent worker keeps an immutable image ledger. Protocol 34 transports
only image additions, changed revisions and retirements. Camera-only edits send
no image pixels. A rejected update retains the previous accepted resources;
subsequent edits compare against that accepted state. Closing the session clears
the host image cache after safely retiring outstanding conversions.

Source and upstream dependencies, missing-path parents and COP projects are
watched without cooking pixels in observer callbacks. Parameter edits, source
rewiring, deletion/recreation and timeline changes update a running viewer.
Explicit pinned-frame paths retain their specified time. Batch captures evaluate
each requested frame without changing the global frame during extraction.

Missing or invalid color images use magenta locally. Invalid height inputs omit
that displacement branch. Independent valid displacement survives a broken color
source, and repaired sources clear diagnostics automatically. Internally malformed
buffers, revisions and transport messages remain strict failures.

## World lighting and sampled displacement

Static COP worlds remain valid when geometry motion blur is enabled. Owned-image
evaluation time is capture context, not an animated shader socket. Actual world
animation uses requested-frame lighting and reports omitted world motion blur;
missing nominal world sources use the configured background. Repairs clear the
diagnostic. Pinned-frame sources remain static. Animation metadata is retained
from the completed pixel cook, so metadata capture adds no COP pixel cooks.

COPernicus image displacement evaluates fractional shutter samples. Legacy COP2
uses integer image-frame sampling; ordinary subframe reads can hold one image.
Explicit pinned-frame sequences are supported, without inventing interpolation.
Independent analytic geometry images verify three/five shutter samples, sampled
SOP deformation, subdivision vertices and compatible packed sharing. World-space
displacement can require per-instance variants; compatible UV-space copies share
one variant. Broken shutter-only height inputs retain requested-frame displaced
geometry, and independent color failures retain valid displacement. Live repairs
restore the sampled result.

World/shutter sources and verification (`artifacts/verification/cop-world-shutter-20261017/index.json`)
record 19 world checks, 40 shutter checks and 14 accepted native GUI edit states
per CPU and RTX 4090 OptiX device. Each device also writes five world frames and
five animated displacement frames. Displacement centroids are checked against an
independent shutter integral. Tests verify real illumination, owned-image MIS
dimensions, native parameter repairs, retained camera edits and short logical
resource plateaus. These small fixtures do not certify heavy-scene performance.

## Verification and limits

Exact sources and run index (`artifacts/verification/cop-native-images-20261017/index.json`)
record CPU and RTX 4090 OptiX checks: 26 end-to-end source/color checks on each
device, 14 accepted native GUI edit states on each device, and five native ROP
batch frames per device including a failed source and its repair. The GUI tests
use native parameter edits and timeline changes, one viewer and one retained
worker; accepted canvas revisions and raw pixels verify the results.

Core tests cover seven analytic image cases, seven malformed resources and 17
delta/ownership contracts. Nine native capture checks cover raster lifetime and
explicit evaluation time. File textures, world rendering, compact image buffers,
100,000-object pose deltas and shader previews also passed their regressions.

This is functional development evidence. Physical display/latency certification,
large-image memory and performance budgets, rotated rasters, arbitrary layer
selection and tiled `op:` images remain separate gates. Legacy COP2 continuous
subframe image interpolation is not implemented. The RTX 3080 Ti and mixed
devices require certification. Restart Houdini to load the new sealed runtime.
