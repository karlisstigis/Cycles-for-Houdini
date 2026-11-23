# Native textures: development implementation

File images now use the pinned Cycles image manager in native OBJ/SOP renders.
This is an implemented and tested material slice. It does not complete the
production color, texture, material or performance gates in the integration plan.
Owned `op:` images from COPernicus and legacy COPs are also connected to native
capture and persistent rendering; see [native COP images](../dev/notes/native-cop-images.md).

## Artist controls

### Texture-cache actions

In the ROP's **Performance → Texture Cache** section, the header checkbox enables
use of generated derivatives. **Auto Generate** creates missing derivatives while
rendering. **Generate** prepares the current scene's file textures using the same
renderer color and alpha policy, without rendering an image. This includes
resolved Blender materials; collecting them can require loading the referenced
asset. In-memory and directly usable source textures do not require a derivative.

**Clear** removes only generated derivatives associated with the scene's texture
sources whose ownership records and file contents still match. Original images,
source `.tx` files, unrelated files, linked paths, modified derivatives and older
unmarked caches are not deleted. The result reports skipped files and errors.
New automatically generated caches receive the same ownership records as manual
generation. Clearing disk derivatives does not evict images already resident in a
running render session. If Auto Generate is enabled, a subsequent load may create
the derivatives again.

### Image nodes

Inside a Cycles material, create **Image Texture**, choose an image, and connect
Color or Alpha to the graph. An unconnected Vector reads the default Houdini
`uv` attribute. **UV Map** and **Mapping** provide explicit coordinate control.
Mapping rotation is in radians. Missing required UVs produce a local material
diagnostic while usable geometry continues rendering.

The input-space menu offers **Auto from Houdini OCIO and file**, **Data**, **Render
working space**, and the current active OCIO color-space names. Native Auto
resolves filename rules, metadata and format fallback using the rendering
configuration. Its menu follows OCIO reloads and accepts saved aliases/inactive
names explicitly. Use Data for height and normal maps. See
[native Auto policy and evidence](../dev/notes/native-texture-auto.md) for mode behavior,
intentional evaluator differences, UDIM constraints and configuration tests.
Render working space means that the file values already use the active
scene-linear role. Internal engine storage tokens are rejected as artist inputs.
The reflected engine default and the authored Houdini Auto default are recorded
separately; saved parameters use named tokens.

Filename expressions are evaluated at the requested render time. Relative
native VOP paths are HIP-relative. `<UDIM>` and `<UVTILE>` are supported in the
basename. Empty Tiles discovers existing matching files; a space-separated list
restricts loading to unique IDs in 1001..9999. The integration passes those IDs
to the upstream image node, retaining its culling and sampling implementation.

**Normal Map** supports tangent/object/world spaces and the pinned conventions.
The tested tangent route uses the default polygon UV map; OpenGL and DirectX
reverse the expected green-channel direction. **Bump** evaluates height
derivatives through Cycles. Engine-generated derivative sample parameters are
excluded from authoring and cannot be set through the graph API.

The **Texture Cache** and **Auto Generate** settings recreate the shared session
and also apply to SOHO batch rendering. Automatic generation is off by default.
Reading and generating tiled images uses Cycles and OpenImageIO; the actions
above manage those derivatives rather than adding another pixel cache.

## Updates and ownership

The core records canonical filenames, selected files, sizes, modification
times and the OCIO configuration ID. Warm updates inspect metadata without
hashing image pixels or mesh vertices. Changed reachable images open headers
before live mutation. Missing files, unreadable headers, invalid input spaces,
invalid tiles and missing coordinates produce a visible error.

Each image revision contributes two internal numeric shader sockets. They
participate in engine graph equality and displacement hashing, without adding
one permanent interned string per revision. The image-manager key and derived
cache namespace use owned strings. An unchanged camera retains the existing
shader/image resources. Surface-only texture edits retain unchanged true
displacement; height-file edits rebuild displacement from the owned original
mesh, including its UV seams.

Disk-cache lookup is isolated by source revision and OCIO configuration.
Namespaced resources cannot fall back to a generic `blender_tx` cache whose
same-named color transform may have been generated with another configuration.
The opt-in hook preserves the stock fallback behavior for upstream clients.
Image-load failures encountered by the engine update also set render progress
to an error; cache-generation failure may use the upstream uncached fallback.

The Houdini event loop inspects image-file revisions approximately once per
second. A disk-only edit can refresh native IPR or a panel-only render. The
notification ledger includes failed candidate filenames so creating or repairing
an image permits automatic retry. Failed updates retain the committed scene and
previous image. The HDK adapter conservatively invalidates its host extraction
cache on transaction failure, so recovery may recapture geometry once.

Metadata inspection cannot detect an in-place edit that deliberately preserves
both file size and modification time. Such workflows need an explicit reload
operation or stronger asset identifiers before certification. Animated unique
filenames also inherit the upstream filename-interning behavior and need a
long-sequence host-memory assessment.

## Evidence

The aggregate runner includes three headless texture cases and one real GUI
case per configured device. Targeted CPU and RTX 4090 OptiX checks cover:

- Independently specified linear/HDR texels, source row orientation and UV
  mapping, plus an OCIO Python reference for input conversion.
- UDIM discovery, explicit tile lists, UVTILE names, retained tile-file updates,
  unused nodes, precommit failures and native VOP path/array authoring.
- Image-driven true displacement and surface-only edits that retain it.
- Twenty-four fixed-path image replacements with logical device-buffer and
  whole-process private-commit samples, followed by zero session allocations
  on close. This is a short fixture, not broad physical-memory certification.
- Cache disabled, enabled with generation, and enabled without generation;
  actual `.tx` files, revision isolation, HDR values and warm camera retention.
- Independent tangent/world normal expectations and the analytic bump slope
  `du/dx = 1/8`, including inversion.
- Actual native IPR file-only edits, missing-file appearance/recovery, shared
  panel ownership, rename, viewer closure and panel-only disk refresh.

The runnable example is `artifacts/stage3/textures/native-textures.hiplc`.
Reports and image outputs are under `artifacts/stage3/textures`; GUI reports
and panel captures are under `artifacts/stage1/gui`. The complete 75-case run
`artifacts/verification/20261010T214000475/summary.json` passes on CPU and
RTX 4090 OptiX. Its `source-audit.json` verifies the frozen build/test sources
before documentation updates. The earlier run records the crop-reset failure
that this complete rerun verifies as corrected.

## Remaining release requirements

Native rendering-policy changes now join all sessions before color-cache
replacement, refresh image revisions and shader matrices, rebuild affected
displacement, and retain source color metadata for older images. Same-name
input transforms pass both cache paths, and working-LUT replacement/repair
has independent checks. See `docs/native-color.md` for supported behavior
and the remaining production color gates. The original pre-fix old-frame
evidence is preserved separately; it is no longer the current behavior.

Broader generated/reference/instancer coordinate contexts, texture previews,
explicit reload, disk-retention controls and long-sequence certification remain
open. Named fields, native world authoring and owned Houdini `op:` images have
implemented slices with their own documented limits. Large texture-heavy fixtures, cold/warm performance, shader-parameter
drags, cache/UDIM interactions, cancellation during I/O and decode races need
separate release evidence. Cache timings in the small functional fixture are
diagnostic and cannot establish a production speed advantage.
