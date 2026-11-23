# Recorded upstream patches

The engine remains at the commit in `source.lock.json`; each modification is applied explicitly and hashed in the build manifest. These patches are local and have not been represented as accepted upstream fixes.

| Patch | Purpose | Verification |
| --- | --- | --- |
| `0001-integration-build-hook.patch` | Attach the integration targets to the official engine build with its configured dependencies. | Official executable, harness, HDK/Python module and delegate compile together. |
| `0002-houdini-usd-link-libraries.patch` | Supply the Houdini USD 26.05 HD/SI and MaterialX link dependencies required by the pinned delegate. | Official Hydra delegate links; independent CPU husk triangle smoke passes. |
| `0003-point-nonuniform-normals.patch` | Correct transformed point-sphere normals. Nonuniform object scale creates an ellipsoid, whose world-space normal requires the inverse transpose of its object-space normal. | Analytic sphere/ellipsoid position, depth and off-axis normal checks on CPU/RTX 4090 OptiX, including mirrored transforms; independent matched reference scenes. |
| `0004-volume-shutter-and-bounds.patch` | Carry the native volume's actual normalized shutter reference and conservative velocity envelope through volume meshing and shading. Preserve upstream defaults for other adapters. | 31 native and nine independent harness checks per CPU/RTX 4090 device, including long trails, outliers, affine maps, sparse scalar merging, emission and transitions; live volume-motion IPR. |
| `0005-point-lifetime-intersections.patch` | Add per-point existence intervals to CPU/custom GPU intersections and Embree filters. Read stationary OptiX ray time from the original Cycles ray payload. | Historical interval tests: exposure/shadow integrals, offsets, validation and retirement. Native automatic lifetime mapping was removed; this remains dormant low-level engine support. |
| `0006-opt-in-phase-diagnostics.patch` | Allow quiet, accumulated engine update statistics, add a complete scene-update timer and expose an optional logical buffer-copy callback. Existing default reporting remains unchanged. | CPU/RTX 4090 radiance, independent index bytes, camera retention, new material attribute requests, geometry uploads and generation-reset checks. Physical transfer/VRAM claims are excluded. |
| `0007-retain-clean-shader-buffers.patch` | Clear shader/SVM buffer modified flags after successful delivery; retain pending flags on a device error. Actual shader rebuilds still mark buffers dirty. | CPU/RTX 4090 diagnostics verify no shader-buffer requests on camera edits and uploads on changed graphs/attribute demand. The 37-case FX/native lifecycle suite passes after relinking all engine consumers. Other backends remain uncertified. |
| `0008-bounded-subdivision.patch` | Add optional post-tessellation seam/metric publication, per-mesh triangle guards and cancellation checks in splitting/dicing/interpolation. Upstream consumers retain unlimited budgets unless explicitly configured. | Independent coarse/reference cube, native subdivision/displacement seams, allocation-failure recovery, IPR updates and retirement pass CPU/RTX 4090. OpenSubdiv refiner construction is not internally cancellable; large temporary allocations remain unbounded by the triangle count. |
| `0009-smooth-subdivision-color.patch` | Read float3 face-varying values with packed storage and enable the existing smooth evaluator for packed float3. | Native discontinuous Cd agrees with six-level independent refinement; a continuous affine field satisfies its independently specified position relationship. UV and Cd seams pass CPU/RTX 4090. The engine's adaptive isolation default remains unchanged. |
| `0010-texture-resource-revisions.patch` | Include owned resource revisions in image identity and numeric internal sockets in shader/displacement identity; isolate derived cache lookup by revision/configuration; propagate image-load failures to render progress. Unconfigured upstream resources retain the default cache lookup. | Independent HDR/OCIO colors, UV orientation, UDIMs, textured displacement, normal/bump behavior, replacement plateaus, actual cache files and real IPR recovery pass CPU/RTX 4090 targeted checks. The complete patch stack reproduces from the pinned commit. Working-space revisions now have independent native checks; late tiled decode failures, disk budgets and large-scene performance need separate evidence. |

| `0011-frozen-color-configuration.patch` | Add an opt-in immutable engine OCIO configuration and complete processor/name/XYZ/interop cache reset without changing the host global config. | Coordinated CPU/RTX 4090 native session epochs, source-tagged frames, input/LUT refresh and displacement luminance pass independent targeted checks. Default upstream behavior remains unchanged; native and Hydra color synchronization have separate gates. |
| `0012-portable-color-identity.patch` | Accept the embedding host's established working ID for derived caches, including unknown/local identities; remove obsolete source primaries and ACES-container flags after conversion. | Independent native input/output and cache-roundtrip checks cover CPU/RTX 4090 and both cache routes. The original conflicting ACES cache declaration is preserved. Unconfigured consumers retain upstream ID selection; cache cleanup applies to converted derivatives. |
| `0013-texture-coverage-alpha.patch` | Process RGB while preserving texture coverage through arbitrary OCIO transforms, and invalidate derivatives produced by the earlier alpha-changing route. | Reproduced 0.375 to 0.046875 opacity failure; independent RGB/alpha expectations pass on CPU/RTX 4090 with cache off/on and associated, straight and packed inputs. This host conversion/cache-key patch leaves kernel sources unchanged. |

The normal fix changes correctness, not the selected geometry representation. The engine's existing transform guard already retains nonuniform point/curve transforms. It does not need generated mesh spheres. The patch uses the existing motion-aware object position/normal transform helpers. Native transform/velocity/deformation fixtures have subsequent evidence; complete animated ellipsoid/large-scene certification remains separate.

Moving native volumes currently use the upstream ray marcher because the pinned octree's density bounds sample a fixed time. Patch 0004 does not supply a motion-aware octree. Patch 0005 clips a retained point population; it cannot discover absent particles. Both limitations are documented in `docs/fx-slices.md` and capabilities. The neutral numeric-light shader fix is integration code, without an upstream patch: the pinned default light graph intentionally has zero emission.

Whenever this engine is rebased, check whether each change is already present, apply only necessary patches, rebuild every shipped kernel and the matched harness, and rerun the relevant regression gates. A newer kernel directory alone is not a supported upgrade.

`scripts/verify-upstream-patches.py` reconstructs the complete pinned tracked tree, applies every locked patch in order, and compares normalized
contents against every tracked file plus nonignored additions and extra files under `src`. Its reports include patch and reconstructed-file
hashes under `artifacts/patch-verification`. Patches 0008–0009 affect host code and
do not change GPU kernel sources.

## Subdivision interruption safety (0018)

The engine allocates final triangle buffers before all dicing tasks finish.
Cancellation or a triangle-budget error must prevent tangent processing and
subsequent geometry/device work from consuming partly filled triangles. Patch
0018 adds cancellation **and error** checks after splitting, before post-dicing
publication, before tangent generation and at the outer update boundary. Split
and dice tasks also stop after an error. It changes host code, with no kernel
change. This fixes a demonstrated unsafe control path; the artist's PID 46016
segmentation fault has not yet been reproduced with a matching stack trace.

## Object motion-buffer layout (0023)

When one packed instance loses or regains transform motion, the number of objects
can stay fixed while the shared array of decomposed transforms resizes. The pinned
engine only forced all decompositions to update when the object array resized;
unchanged instances could therefore read uninitialized motion storage. Equal-size
layouts can also shift an unchanged object's offset when other objects change their
sample counts. Patch 0023 rebuilds decompositions on motion-buffer reallocation
and when an object's offset changes. It also checks the previous/current logical
buffer sizes: `device_vector::alloc()` discards host storage on size changes
without necessarily setting `need_realloc()`. A packed displacement repair exposed
that gap by making an unrelated moving object disappear. Repeated native
CPU/OptiX offset-shutter repairs now preserve the full image. It leaves geometry
buffers and kernels alone.
The motion harness tests repeated one-object disable/repair plus three/five-sample
exchanges at constant total allocation size, checking all output values are finite
and unchanged instances match an independent initial render.

## Sampled displacement (0024)

An opt-in mesh flag evaluates displacement at every shutter pose using the
engine's existing motion-triangle interpolation and sampled object transforms.
Original device geometry remains unchanged during evaluation; results are applied
to the corresponding host position sample afterward. The previous centre-offset
behavior remains the default for other engine consumers. Normal recomputation
continues through the engine's existing per-pose path.

This changes the private shader-evaluation input layout. CPU code, CUDA/OptiX
kernels, executable, harness, native module, worker and delegate were built
together. Development staging now stores kernels under each versioned host root,
and records that resource directory in the manifest. An already-open older Houdini
session retains its matching kernels even when another build is staged. Changing
kernels inside an existing host root is rejected.

Eight independently positioned reference meshes, three/five motion samples,
authored normals, world-dependent object motion, subdivision, actual IPR parameter
edits and five-frame native batch output pass on CPU and RTX 4090 OptiX. Animated
shader/image snapshots, sampled arbitrary attributes/tangents, other GPUs and
large-scene performance remain separate gates. At this milestone, all 24 patches reconstruct from
the pinned source; the reconstructed affected files match the build checkout.

## Temporal displacement shaders (0025)

Each displacement evaluation input can select an explicit shader, so owned
per-shutter shader values and image resources can evaluate the corresponding
geometry pose. The mesh stores integer shader slots into Geometry's existing
owned shader array. It does not introduce a second node-pointer socket: that
prototype exposed a derived-array destruction lifetime error on session shutdown.
The integer table avoids that ownership conflict. Shader overrides accept encoded
negative IDs (the smooth-normal flag uses the high bit); only `SHADER_NONE` means
no override. The shader-evaluation input stays 32 bytes. Every CPU/GPU consumer and
kernel was rebuilt together, using versioned resource roots.

The integration captures only reachable displacement dependencies for these
private shaders, retains the nominal surface/material identity, retires surplus
states, and validates uniform odd sample counts before scene mutation. CPU and
RTX 4090 OptiX independent images cover parameters, Scene Time, file selections,
combined deformation, offset shutters and live repair. Warm tracked allocations
plateau across repeated edits; this does not certify physical VRAM or large scenes.

## Subdivision motion normals (0026-0027)

Patch 0026 refreshes the cached object triangle count after tessellation, alongside
the existing vertex-count refresh. Motion corner normals use the triangle count
to find their sample stride. A stale coarse count addressed the wrong sample.

Patch 0027 preserves authored corner normals when the engine requests generated
normals, tolerates a missing vertex-normal array during dicing, and creates any
missing motion normals after successful linear tessellation. The cancellation and
error checks remain in place. These are host changes; they do not change kernels.
Independently constructed moving-normal meshes and actual native subdivision,
three/five motion samples, final rendering and batch tests pass CPU/OptiX. The
complete 27-patch stack reconstructs from the pinned revision and matches the
build checkout.

## Sampled mesh attributes (0028)

The engine already stores generic attribute motion buffers, but its triangle
shader reads previously used only the centre state. Patch 0028 marks sampled
vertex/corner descriptors with the existing motion bit and interpolates their
values at shader time, retaining derivative handling. Position and normal readers
keep their dedicated paths. Packed byte colors are decoded before interpolation.

Subdivision preserves every stored face-varying state. Smooth fields get a channel
and refined values per state, allowing their seam topology to differ. Linear
interpolation remains the fallback when no smooth channel exists. The first
implementation returned too early for linear subdivision and left UV buffers
uninitialized; independent image comparisons exposed this and the corrected
fall-through is part of the recorded patch. Temporary probes were removed.

Native Deformation capture owns aligned mesh UV/Cd samples, drops identical
samples, reports unavailable used fields and includes sampled values in generated
displacement seams. Object-space UV displacement retains its mesh during camera
and object edits. Worker protocol 28 carries these owned arrays and completeness
flags; malformed internal sizes remain strict failures. CPU/CUDA/OptiX kernels
and host consumers were rebuilt together. Thirty independent images per device,
live repair, both subdivision types, final rendering and five native frames pass
on CPU and RTX 4090 OptiX. Other attribute families and large-scene budgets remain
separate gates. All 28 patches reconstruct from the pinned source.

## Sampled point and curve fields (0029)

Generic numeric point, curve-key and curve-root fields now interpolate their
centre-first motion storage at ray time. Spatial interpolation and derivatives
along curve segments remain intact; position, radius and normal readers retain
their dedicated paths. Curve-root storage uses the curve count, so Object packs
and refreshes Hair's primitive count instead of leaving it zero. No kernel object
layout was enlarged. Mesh numeric fields reuse patch 0028, including subdivision
face-varying sample channels and seam handling.

Native Deformation capture aligns requested field samples with geometry
correspondence, owns all buffers, and collapses constant samples. Missing/invalid
shutter data retains usable centre shading and recovers only dependent
displacement motion. Protocol 31 carries these arrays and typed field diagnostics.
Host dependency versions/connections also invalidate inactive-at-centre branches;
this is integration code, not an engine patch.

Eight analytic routes across mesh/point/hair, twenty independently constructed
named-displacement images per device, three/five shutter samples, both subdivision
schemes, live native parameter edits, final/batch output and strict owned-buffer
rejections pass on CPU and RTX 4090 OptiX. CPU/CUDA/OptiX kernels and every consumer
were rebuilt together; old versioned kernels were checked unchanged. All 29
patches reconstruct from the pinned commit. Broader GPU/curve/coordinate support,
physical memory and large-scene latency remain separate certification work.

## Named and sampled tangent construction (0030)

MikkTSpace now receives corresponding position, normal and UV inputs for every
stored shutter state. Float2 and float3 corner maps are supported; float3 uses XY
directly, avoiding a duplicate UV alias. Tangent/sign buffers gain motion storage
only when the mesh needs sampled inputs, and generation checks cancellation/error
between poses. Undisplaced position/normal attributes preserve their motion data.

Tangent and Normal Map gain an internal, clone-safe context-basis mask. Surface
and displacement compilation choose their respective displaced/original bases,
so one node can feed both outputs without duplicating an artist's graph. The
internal mask is hidden from native authoring and rejected as an external graph
parameter. Only contributing requested bases are generated. The integration
length-prefixes engine field names to keep user names disjoint from generated
suffixes; that naming change is integration code, not an engine patch.

Thirty-two primary images and 28 independent recovery references per device
cover sampled default/named UVs and geometry, float2/float3 maps, mirroring,
normal conventions, both normal bases and shared displacement/shading inputs.
The references use explicit geometry and vector algebra with no UV maps,
Mikk-generated basis or normal/displacement nodes. Subdivision matches a separate
default-UV route. CPU and RTX 4090 OptiX live edits, repairs, final and five-frame
native batch checks pass. OSL remains disabled and unverified in this build.

## Motion normal-map basis (0031)

The pinned SVM Normal Map Displaced path combined a sampled tangent with a
centre-frame triangle normal. Moving triangles now use the existing motion
triangle-normal reader at ray time, converting back to object normal space when
required. Static triangles keep their previous path. Position buffers are not
fetched for this correction. A shared surface/displacement normal-map reference
exposed the error; its CPU mean beauty difference falls from about 0.002289 to
0.000144 after the fix. This describes the pinned engine path, not an audit of
every Blender release.

CPU/host consumers and all six CUDA/OptiX kernels were rebuilt together. The
31-patch stack reconstructs from the pinned source and its affected files match
the build checkout. Older versioned kernels remain unchanged. Worker protocol
stays 31 because neither patch changes transport. The current focused evidence
is `artifacts/verification/tangent-bases-20261017/index.json`; large-scene latency,
physical-memory plateaus and other devices remain separate gates.

## Displacement dependency hash (0038)

The pinned engine hashes links using global shader-node IDs. Removing an
independent surface/normal branch therefore changes an otherwise identical
displacement hash. This local host-only patch assigns dependency-local indices
in input socket order and includes the selected displacement root output. It
preserves parameter, connectivity, output socket and OSL bytecode invalidation.
It does not change kernel code or graph execution order. Recovered binding
tagging is fixed separately in integration code.

Eight engine contracts cover irrelevant normal/surface changes, construction
order, real height edits, root sockets, disconnection and rewiring. Independent
native images verify valid and recovered material bindings retain displacement
through surface edits and update after height edits on CPU and both OptiX GPUs.
The 38-patch stack reconstructs from the pinned commit; no upstream acceptance
is claimed. See `artifacts/material-thumbnails-evidence-20261018.json`.

The maintained [76-patch inventory](renderer-patch-contracts.md) records current source hashes, exact touched files, file-overlap predecessors, backend scope and existing related fixture assertions. It explicitly identifies missing targeted behavioral regressions; source replay alone does not certify rendering behavior.
