# Cycles Blender Reference

For new work, use the **Cycles Blender File** node in **Link** mode (Tab menu
**Cycles > Cycles Blender File**, or Link in the Asset Browser and the importer):
it holds this node inside and adds Update and Converted mode. With the Blender
File node installed, the Blender Reference node is no longer listed in the Tab
menu; scenes that contain it still load and render, and everything below still
applies to it. [The Cycles Blender File node](blender-import.md#the-cycles-blender-file-node)

Create a **Cycles Blender Reference** SOP inside a geometry object. Choose a
`.blend` file and add references to the list. Each row has an enabled toggle,
an **Object / Collection** selector, **Name** and **Source Frame**. New rows use
source frame `1`; enter `$FF` to follow Houdini's timeline, including subframes.
The slider spans `1`–`100`; type any fractional or out-of-range frame directly.
Saving the geometry preserves `$HIP` paths. The SOP
evaluates backtick channel expressions in the file parameter when it cooks.

Enter one or more Blender names in **Name**, or choose names from its menu.
Menu choices append to the current selection. Quote names containing spaces;
wildcards can select several names from the cached catalogue. Output pieces use
the Blender name with spaces replaced by underscores. The packed reference keeps
the exact Blender name for rendering. A name alias is accepted only when it
identifies one Blender target; collisions are reported rather than guessed.
One node supports at most 128 resolved targets across its enabled rows.

A collection reference leaves out child collections that the `.blend` file
excludes from its view layer (the checkbox in Blender's outliner), such as rig
widget shapes. Rendering and the viewport box or hull both follow this. Each
skipped collection is listed as an info diagnostic. If the referenced
collection is itself excluded, or is in no scene, all of its children are used.
Collections hidden for rendering stay hidden, as in Blender. To keep the
excluded children of one collection row, enable its **Include Disabled
Collections** checkbox (shown for collection rows below **Hull per member**);
rendering, bounds and the viewport hull then include them. The checkbox is part
of the reference (`file.blend#collection+disabled=Name`), so its cached bounds,
previews and render prototypes stay separate from the default selection.

The output contains native packed primitives with a primitive `name` attribute.
Copy to Points, Merge and ordinary packed transforms can place them alongside
Houdini geometry. Use the primitive's normal packed transform to translate,
rotate or scale it, including negative scale. Rendering evaluates the selection
through the existing Blender worker pool, shared with `s@instance` references
to the same source and frame.

For Edit SOP rotation or scaling, select the packed **primitive**, rather than
only its point. Point selection moves its point, as it does for ordinary packed
geometry. The box corners are a preview, not editable Blender vertices.

Disabled rows do not load. Duplicate output piece names produce a warning;
use distinct selections when Copy to Points must distinguish them. On the
template points, author matching `s@name` values and set Copy to Points'
**Piece Attribute** to `name`. Different piece names can share the same Blender
prototype in saved scenes with explicit piece-name overrides.
Piece names select geometry; they are not unique instance identities after
copying. For animated point reordering, give the template points a unique `id`
and include `id` in Copy to Points' **Attributes from Target** transfer.
Without an ID, packed references use point order for motion correspondence.

Press **Refresh** to update the available names and evaluate bounds for the
enabled selections asynchronously. Target discovery also runs when selecting a
file. Opening a name menu reads the cached catalogue only. The last catalogue
is saved in the HIP for immediate reuse; a stale catalogue previews the names
previously available. Manually typed names and expressions are preserved even
when a target is missing. Discovery reads Blender library metadata without
evaluating its meshes or animation frames.

Choose **Bounding Box** or **Convex Hull** in the **Viewport Shape** menu. Convex Hull
shows a lightweight convex preview of the evaluated source. **Wireframe** is on
by default and draws only hull edges; turn it off for shaded hull faces. This
affects the node and its packed copies without changing global viewport shading
or rendered geometry.

**Shape Frame** selects the Blender timeline sample for both box and hull
previews. It defaults to 1; its 1–100 slider is a convenience, and finite
fractional or out-of-range samples are accepted. Keep Shape Frame fixed to
freeze the preview while animating **Source Frame** for rendering. Source Frame
selects the sample used by Cycles and its conservative render/culling bounds.

**Maximum Hull Vertices** defaults to 64, with a convenience slider from 8 to
256. Explicit values from 8 to 4096 are accepted. The same maximum applies
independently to each selected object, each collection member with **Hull per
member** enabled, or the single whole-collection hull when it is disabled.
Changing the maximum rebuilds the requested detail from the evaluated source;
it does not refine a previously reduced hull. For a collection,
enable **Hull per member** below Source Frame to show separate member hulls;
leave it off for one hull around the whole collection. These controls change
the viewport preview; Cycles still renders the original Blender geometry.
Copies reuse the saved hull rather than evaluating the source again.
Low-poly previews use the exact convex hull when it fits the requested maximum.
Dense sources use deterministic reduced previews. With the normal 8-or-higher
setting, reduction preserves axis extremes and distributes samples over the
surface; it can omit smaller surface detail. Each collection member keeps its
own preview. This cosmetic hull does not replace the complete geometry or the
conservative bounds used for rendering and culling.

Before the first successful evaluation, the preview is a unit box and
`cycles_blender_bounds_known` is false. Refresh requires the
optional Blender bridge. The SOP cook, scene load, bounds queries and viewport
never launch Blender. A successful refresh recooks the SOP on Houdini's main
thread to retain the result. Rendering makes Source Frame bounds available for
rendering and geometry saves. Preview snapshots belong to individual nodes, so
different Shape Frames and hull limits coexist for the same target. Saved previews reopen without
starting Blender. A missing, stale or oversized hull falls back to the box
preview; use Refresh to rebuild it after changing the source.
An already displayed box can retain its previous size until the SOP recooks;
stale geometry loses render-bound authority immediately.
Degenerate sources also fall back to a box. Saved hull previews are limited to
128 members, 16,384 vertices and 32,768 triangles across the whole and member
hulls, with a 2 MiB compact hull metadata budget. Oversized selections report
a warning and use a box; they never silently lower the per-member maximum.
Split the selection or lower Maximum Hull Vertices to fit. The node retains at
most 128 snapshots and 16 MiB of preview metadata.

Existing HIP files retain their single-reference selections, channel expressions,
per-row file overrides and explicit piece-name overrides. Compatibility
parameters remain stored even though the simpler interface hides them.
Editing a migrated row's Name starts the new selection syntax and automatic
piece naming; its previous piece-name override remains in migration metadata.

Known local bounds and their source/frame identity are saved with the primitive
and in a bounded metadata record on the SOP. A reopened procedural node can
therefore show its saved box immediately without starting Blender. The box is
initially unverified while dependency fingerprints are checked asynchronously.
A changed or missing asset retires the saved box and reports it as stale; use
**Refresh** to evaluate it again. Unverified or stale boxes are never
authoritative render bounds. A different source frame with no saved record uses
the unit-box preview. True material displacement can make bounds unknown.
Cycles still renders the evaluated geometry.

Blender materials are rebuilt as editable Cycles material networks. Blender is
Z-up and Houdini is Y-up: geometry converts with Houdini (x, y, z) = Blender
(x, z, -y). World-space shader values therefore get small axis adapters
(Separate XYZ, Math, Combine XYZ nodes named `blender_axes*` / `houdini_axes*`)
wherever they meet ordinary graph math, so a Blender graph that reads Geometry
Position, Normal, True Normal, Tangent or Incoming, Texture Coordinate
Reflection, Object Info Location, or a world-space Vector Transform, Normal Map
or Vector Displacement computes the same values as in Blender. Native normals
that feed a shader directly (Normal Map or Bump into a BSDF) need no adapter.
World positions match Blender while the reference's own Houdini transform is
the identity; moving it in Houdini moves its world values, as moving the
object does in Blender. **Object Info Random** renders with Cycles for
Houdini's own per-object random value: it still varies per object and per
instance, but the values differ from Blender's, and a warning says so.

A material that cannot be translated (for example an unsupported node or a
missing texture) never stops the reference: Refresh still builds the bounds and
hull, and rendering keeps the geometry. Faces using that material render with
the Houdini material assigned to the reference, or the ROP's fallback material,
and the render reports a warning naming the material and the reason. Importing
a material from the asset browser stays all-or-nothing.

Resident prototypes are shared across points, nodes and render consumers. With
an explicit `f@index`, changing Houdini's frame, point order or transforms does
not reload that Blender sample. Without `index`, attribute instancing follows
the Houdini frame. Packed references use **Source Frame**; a `$FF` expression
follows the timeline, while new rows start at frame `1`. Newly requested source
frames and changed files or
evaluation settings require evaluation; motion blur requests its source samples
explicitly. Compatible source deformation retains its sampled positions and
smooth normals. If topology or target membership changes across the shutter,
the renderer reports a motion diagnostic and holds the requested-frame geometry
instead of connecting unrelated vertices. Inactive prototypes may be evicted by the existing cache budget
(1 GiB by default). Live consumers retain their samples until retirement.

The host cache avoids repeated Blender evaluation across render restarts.
Cycles persistent data separately reuses compatible native and device resources
in a surviving renderer session. A new renderer still needs its initial upload.
Bounds, catalogues and lightweight hull previews are saved in the HIP; the full
Blender source meshes and resident prototype cache are not serialized there.

The primitive type is `CyclesBlenderReference`. Its authoring intrinsics are
`cycles_blend_file`, `cycles_blend_target_kind` (`object`, `collection`, or
`collection+disabled` for a collection that includes its disabled children),
`cycles_blend_target` and `cycles_blender_frame`. The read-only
`cycles_blender_reference` contains the equivalent `file.blend#object=Name`,
`file.blend#collection=Name` or `file.blend#collection+disabled=Name`
reference; `s@instance` accepts the same spellings. The
`cycles_blender_bounds` string contains the validated bounds serialization.
When replacing an existing Source Frame expression with a constant through HOM,
first call `node.parm("ref_frame1").deleteAllKeyframes()`, then set its numeric value.

For a `.bgeo.sc` containing these references, read it as geometry with a File
SOP so Cycles can discover the custom primitives. Unpack an outer packed-disk
container before rendering embedded Blender references. Ordinary disk geometry
and Alembic use their native Houdini packed primitives.
