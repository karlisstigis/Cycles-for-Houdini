# Import Blender .blend files

## Importing a .blend file

Use **File > Import > Blender (.blend)...** (also **Cycles > Import Blender
(.blend)...** and **Import .blend** on the Cycles Tools shelf). The command is
there only when the **Blender .blend asset bridge** component is installed, and
it needs Blender (found on `PATH`, in Program Files, or set with
`CYCLES_HOUDINI_BLENDER`).

1. Choose the `.blend` file with **Browse...**. Blender reads it in the
   background; the file itself is never changed.
2. The list shows the scene's collections (nested, as in Blender's Outliner)
   and their objects, with the object type. Check what to import:
   - Checking a collection imports it as a whole, with everything inside it.
     Its contents then show as checked and greyed.
   - Objects can also be checked one by one.
   - **Geometry Nodes** in the Notes column marks objects with a geometry
     nodes modifier; they are imported as Blender evaluates them.
   - Greyed collections marked **Disabled in the view layer** are excluded
     in Blender's active view layer. Inside a checked parent they are left
     out unless **Include Disabled Collections** is on; checking one directly
     imports it.
3. Pick the options and click **Import**. A Cancel button stops the Blender
   part of the import. When it finishes, the new nodes are selected and listed
   at the bottom of the window.

| Option | Choices |
| --- | --- |
| Mode | **Link**: Cycles Blender File nodes in Link mode that read the file while rendering. **Convert & Append** (default): converted once to Houdini geometry. |
| Save Location | Convert & Append only. **Save to Disk as .bgeo.sc** (default): `.bgeo.sc` files loaded with File nodes. **Stash in .hip**: stored in the .hip, one frame. |
| Structure | **Without hierarchy** (default): one Cycles Blender File node per checked item. **Hierarchy**: one Cycles Blender Hierarchy object. Convert & Append only (Link always imports without hierarchy). To get plain polygons, set **Load As** to **Unpacked** on the Blender File node. |
| Materials | **Link (shared, read-only)** (default), **Append (editable copy)** or **None**. Convert & Append only. |
| Frames | **Current frame** (default) or **Frame range** with start, end and step, filled in from the file. Shown only for Save to Disk; ranges write one file per frame, so they are heavy on disk and slow. |
| Keep Nested Instances | On (default): geometry nodes instances of instances stay packed inside packed, following Blender's instance tree, so a branch of leaves is one shared prototype however each branch is scaled. Off: every instance is a top-level packed primitive (still sharing its geometry). Convert & Append only. |
| Apply Subdivision | Off (default): an object whose last modifier is a Subdivision Surface is imported as its control cage, and Cycles subdivides it at render time. On: the subdivided mesh is baked into the geometry. Convert & Append only. |
| Asset folder | Convert & Append only. Default `$HIP/assets_blender`: geometry in `<blend>/geo/<asset>/`, images packed in the .blend in `<blend>/tex/`. |

Options that do not apply to the chosen mode are hidden. Material textures are
not copied: they stay where the `.blend` points; **Cycles > Collect Textures**
copies them into the project.

The dialog remembers the last options (in `blend_import_settings.json` next to
the Asset Browser settings in your Houdini preferences folder).

## What the options create

Blender evaluates the file first, as it would for a render: modifiers and
geometry nodes are applied. The whole import is one undo step.

### Subdivision

With **Apply Subdivision** off, a final Subdivision Surface modifier is not
applied: the object comes in as its control cage, and its geo object gets the
Cycles tab with **Subdivision** turned on: Catmull-Clark, Levels Render =
Blender's Render level, Levels IPR = Blender's Viewport level, and Blender's
UV Smooth, Boundary Smooth and Use Creases. Edge creases become the vertex
attribute `creaseweight` and vertex creases the point attribute
`cornerweight` (Blender 0..1 is Houdini sharpness 0..10). The viewport shows
the cage; Cycles renders the smooth surface.

Cycles subdivides per Houdini object. **Without hierarchy**, objects
with different subdivision (none, or other levels) are therefore split: the
item's geo object holds the unsubdivided geometry and `<item>_subdiv` (then
`_subdiv_2`, ...) the subdivided geometry, each with its own file and its own
Blender File node (its **Part**); **Update** keeps both. In
**Hierarchy** every object already has its own geo object; packed instances
inside an object inherit that object's setting.

If a subdivided part ends up merged with other geometry in one object (for
example a Blender File node dropped into an existing geo object, or merged with
other nodes), or the object's Cycles subdivision no longer matches the part,
the node's **Status** adds a warning: give the part its own object, or turn on
**Apply Subdivision** and press **Update**.

Blender bakes the subdivision anyway, and the import reports why, when the
modifier cannot be reproduced at render time: Simple subdivision, Use Limit
Surface off or Quality other than 3, flat-shaded faces, custom normals, animated or driven
modifier settings, other objects that use this one's geometry, color or other
named attributes on the mesh, fractional crease values, loose edges, or a
linked object. A Subdivision Surface that is not the last modifier is always
applied with the rest of the stack.

**Mode and Save Location**

- **Link** creates a geo object with a Cycles Blender File node in Link mode
  per checked item. Nothing is converted; the file is read while rendering.
- **Convert & Append > Stash in .hip** stores the converted geometry on the
  Blender File node (Stash in .hip on), inside the .hip file.
- **Convert & Append > Save to Disk as .bgeo.sc** writes `.bgeo.sc` files to
  `<asset folder>/<blend>/geo/<item>/` and the Blender File node loads them. `<blend>`
  is the `.blend` file's name. One `cycles_import.json` per blend folder records
  the source file, options and the files written. Importing the same item again (for example
  after the `.blend` file changed) **replaces** its files: they are overwritten
  in place, files the new import no longer needs (fewer frames, removed
  objects) are deleted, and File SOPs already reading them are reloaded (or
  pointed at the new files when a frame range became one frame, or the other
  way round).
  Unchanged geometry is not rewritten. Each structure keeps its own files
  (`<item>.bgeo.sc`, `hierarchy/<Object>.bgeo.sc`),
  so importing one structure never replaces another. If another saved scene
  in the same folder uses the item's files, the import first asks: **Replace**
  (that scene changes too) or **Cancel** (nothing changes). If the scene is not
  saved yet, the files go to `<asset cache>/assets_blender` instead of `$HIP`.

**Structure**

- **Hierarchy**: one **Cycles Blender Hierarchy**
  object (like an Alembic Archive). Inside it, one geo object per Blender object,
  parented as in Blender. Empties (and cameras or lights) become null objects.
  The object transforms are on the Houdini objects. A checked collection gets a
  null with its name as the root. Instances (geometry nodes instances,
  collection instances) stay packed inside their object, sharing one prototype.
  Objects that share one mesh in Blender (linked duplicates) load one shared
  file, `hierarchy/shapes/<name>.bgeo.sc`, as a packed disk primitive, with the
  material assignments inside the file (files storage, single frame).
- **Without hierarchy**: one geo object per checked item with a
  **Cycles Blender File** node. Every Blender object is a packed primitive with
  `name` (the object name) and `path` (`/Collection/Sub/Object`) attributes.
  Copies of the same mesh share one geometry.

**Materials**

Each Blender material slot becomes a primitive group `mat_<Name>`, and the
primitive attribute `blender_material` keeps the Blender name. With **Link**
or **Append** the materials are rebuilt under `/mat`, as in the Asset Browser;
Link reuses an already linked material; Append reuses the editable copy an
earlier import of the same material made in this scene (your edits stay, and a
re-import does not pile up copies). Delete the copy to get a fresh one from
the `.blend` file. The material paths are written into the converted
geometry (`shop_materialpath`), so a Blender File node needs no Material SOP;
to reassign, add an ordinary Material node after it. Inside a Blender Hierarchy
object, a Material SOP assigns them to the groups of each object's own geometry.
**None** creates no materials. Each material's Material > Settings (Emission
Sampling, Transparent Shadows, Bump Map Correction, Volume Sampling, Volume
Interpolation and Volume Step Rate) are set on the Cycles Material, in Link and
Append mode alike (see [material settings](native-materials.md#material-settings)).

Inside a Blender Hierarchy object each geo object ends in a null named `OUT`
with the display and render flags.

## The Cycles Blender File node

One node per imported object or collection (SOP, Tab menu **Cycles > Cycles
Blender File**). Only the parameters that work in the current state are shown.

- **Blender File**, **Object / Collection** and **Name**: what it shows. Edit
  Blender File to re-point a moved `.blend`.
- **Mode**: **Link** reads the `.blend` while rendering (Source Frame, Viewport
  Shape, Wireframe, Shape Frame, Maximum Hull Vertices and Refresh as on the
  old Blender Reference node). **Converted** loads the geometry converted by
  **Update**. Switching never deletes anything; a node switched to Converted
  before its first Update keeps showing the Link result.
- **Status**: **Up to date**, **Source changed** (the `.blend` was saved since
  the last Update), **Source missing**, **Not converted. Press Update.** or
  **Options changed. Press Update.** It is checked when the parameter pane shows
  the node; Blender is never started for it.
- **Load As**: **Packed** (one packed primitive per Blender object) or
  **Unpacked** with **Iterations** (how many levels of packed primitives are
  unpacked; raise it to unpack nested instances). `name`, `path` and the
  material paths stay on the unpacked primitives. No Update is needed to switch.
- **Stash in .hip**: Update stores the converted geometry in the node (one
  frame) instead of a file; the `.hip` grows and is saved again with every
  scene version. Turning it off and pressing Update writes the file again.
- **Frames**, **Apply Subdivision**, **Keep Nested Instances**, **Include
  Disabled Collections**: the conversion options. Changing them shows
  "Options changed"; nothing is written until **Update**.
- **Part**: shown when the asset was split per render-time subdivision setting.
- **Update**: exports the asset from Blender again in the background and
  replaces its files (asking first when another saved scene uses them).
  Linked materials are reloaded from the library; editable (appended)
  materials are never overwritten and are listed in the report; new materials
  are imported the same way as at import. A new subdivision part gets its own
  new geo object; a part that disappeared keeps its node and is reported.
- **Open in Blender** opens the `.blend` in Blender (with Blender's own colour
  management, not Houdini's OCIO settings).
- **Files**: the folder of the converted files.

**Cycles > Update All Blender Files** in the network editor presses Update on
every Blender File node and Blender Hierarchy object whose status is "Source
changed".

## The Cycles Blender Hierarchy object

The Hierarchy structure creates one object (Tab menu **Cycles > Cycles Blender
Hierarchy**) holding the objects of the chosen collections and objects; its own
transform moves the whole asset, and you can dive in and edit or add nodes.
Parameters: **Blender File**, **Selection** (`collection:Props object:"Big
Tree"`; **Edit...** opens the importer's list of collections and objects for
that file, read in the background), **Frames**, **Stash in .hip**, the conversion
toggles, **Status**, **Update** and **Open in Blender**.

**Update** refreshes the objects inside without rebuilding them: new Blender
objects get new objects; objects whose Blender object is gone are kept and
listed; an object you moved keeps your transform (listed), the others follow
Blender; in Material nodes, entries for new material groups are added and
entries for vanished ones removed, the rest is left as you set it; nodes you
added are never touched.

Imports made with earlier builds (plain File, Stash and Blender Reference
nodes) still open and render, but they have no Update; import them again to
get the new nodes. The plain **Blender Reference** node is no longer in the Tab
menu; the Blender File node in Link mode replaces it.

## Attribute mapping

Blender and Houdini use different axes: the geometry is turned from Blender's
Z-up to Houdini's Y-up, and polygon winding is reversed to Houdini's
convention. Vector attributes are turned as well.

| Blender | Houdini |
| --- | --- |
| Vertex position | Point `P` |
| Face corner (custom split) normals | Vertex `N` (always written, so shading matches Blender) |
| UV maps | Vertex `uv` from the active render UV map; other UV maps keep their names. The active map is also kept under its own name only when a material names it (UV Map, Attribute, Normal Map or Tangent node) |
| UV attributes (no UV map entry) | A Face Corner or Point attribute of type Vector or 2D Vector is treated as a UV map when its name contains "uv" (any case; an attribute a material reads but without "uv" in its name, such as `wind`, stays a plain attribute): it becomes a vertex attribute in the same form as `uv` (not turned with the axes). With no UV maps, one of them also becomes `uv` (see below) |
| Active render color attribute | `Cd` (point or vertex, as in Blender), plus `Alpha` when the alpha is not 1 everywhere; also under its own name when a material names it (Color Attribute or Attribute node); other color attributes keep their names (RGBA) |
| Point / face corner / face attributes (meshes) | Point / vertex / primitive attributes with the Blender name; every type arrives (see "Attribute types" below) |
| Face corner 2D vectors | Blender treats every Face Corner 2D Vector as a UV map: a vertex UV like the other UV maps |
| Edge attributes | Not imported (Houdini has no edge attributes); named in the import log |
| Instance attributes (geometry nodes) | Not imported yet (packed primitives get no per-instance values); named in the import log |
| `sharp_edge`, `uv_seam` | Edge groups `sharp_edges`, `seams` |
| Vertex groups | Point float attributes with the group name (0 on points not in the group) |
| `velocity`, `rest_position`, integer point `id` | `v`, `rest`, `id` |
| Material slots | Primitive groups `mat_<Name>` and string `blender_material` |
| Hair curves | Open polylines through the control points; `radius` becomes `width` (2 x radius, Houdini width is a diameter); without a radius, `width` 0.01 (Blender renders them with radius 0.005). Point attributes become point attributes, curve attributes primitive attributes, every type; Blender's own curve data (`curve_type`, `cyclic`, handles, NURBS weights, `resolution`) arrives as plain attributes |
| Point clouds | Points without primitives; `radius` becomes `pscale`; without a radius, `pscale` 0.01 (Blender's radius for them). Every point attribute arrives |
| Edges without faces | Open two-point polylines |

Names are cleaned up to letters, digits and `_` (the rename is listed). A
Blender attribute that would collide with a Houdini attribute that means
something else (for example `P`, `N`, `Cd`, `uv`, `name`, `path`, `pscale`,
`width`, `rot`, `orient`) gets a `blend_` prefix, such as `blend_P`, so renamed attributes sort together in the Geometry Spreadsheet.
Attributes whose names start with "." are Blender internals and are not
imported. Earlier versions used a `blender_` prefix and averaged edge
attributes to points; updating an earlier import renames or removes those.

### Attribute types

| Blender type | Houdini attribute |
| --- | --- |
| Float | float |
| Integer, 8-Bit Integer | integer |
| Boolean | integer 0 / 1 (not a group) |
| 2D Vector, 4D Vector | 2 / 4 floats, not turned |
| Vector | 3 floats, turned to Y-up like `P` |
| Color, Byte Color | 4 floats (RGBA) in Blender's scene linear space, 0-1; byte colours are converted from their sRGB bytes to linear, as Cycles reads them |
| Quaternion | 4 floats with type info Quaternion, reordered from Blender's w, x, y, z to Houdini's x, y, z, w, the axis turned to Y-up |
| 4x4 Matrix | 16 floats with type info Matrix, turned to Y-up |
| 2D Integer, 2D 16-Bit Integer | 2 integers |
| String | string |

Each import writes one line per object to the import log for attributes that could not be
carried, with the reason, for example
`Rock: attributes not imported: wear (Houdini has no edge attributes)`.

### UV maps and UV attributes

Some assets (for example Poly Haven models made with geometry nodes) keep
their UVs as a plain attribute instead of a UV map; the material reads it with
an Attribute node. A Face Corner or Point Vector / 2D Vector attribute whose
name contains "uv" is imported like a UV map, under its own name. When the mesh
has no UV map at all, one of these attributes also becomes `uv`, so textures
without a UV input still line up: the one a material reads first, then one
named `UVMap`, `uv` or `UV`, then the first by name.

**Link mode follows Blender exactly here.** Blender itself never uses such an
attribute as a UV map, so a Link render (the Blender File node in Link mode)
keeps it as a plain named attribute: an Attribute node that reads it works as in
Blender, but an image texture without a UV input on a mesh with no UV map looks
the same as in Blender (one flat colour), not mapped. The node's diagnostics
note "No UV map; ... stays a named attribute". There is no switch for this in
Link mode; use Convert & Append, which always makes these attributes UVs, if
you want them mapped.

A mesh with one UV map gets only `uv`, unless its material names the map.
`cycles_import.json` notes the result per mesh, for example
`UVs: uv (from UVMap), UVMap`.

Earlier versions also wrote `uv2`, `uv3`, ... for the other UV maps and always
kept the active map under its own name. Updating an earlier import removes
those attributes; networks that read them should use the Blender names
(other maps) or `uv` (the active map).

## Link mode and Blender's own render

In Link mode the asset is meant to render as it does in Blender's Cycles:

- Geometry-nodes instances (Instance on Points with a primitive, Object Info
  with As Instance off) render; earlier versions left them out.
- **Generated** texture coordinates use the object's texture space, as Blender
  does: the box of the mesh before modifiers (so an Array or Mirror copy runs
  past 1) or the custom texture space set in Blender, and a flat side maps to
  0.5. Textures that move with an armature or shape keys can still slide on the
  surface (Blender uses the undeformed shape there; not supported yet).
- A face whose material index is above the object's material slots uses the
  last slot (a negative index the first), as in Blender, instead of failing.
- **Hair** and **point clouds** render in Link mode as in Blender (see "Hair in
  Link mode" and "Point clouds in Link mode" below). Volumes, Grease Pencil and
  Gaussian splat point clouds are not rendered in Link mode yet. They are
  skipped with one warning per type naming the objects, and the rest of the
  selection renders.
- The Attribute node's Fac output on a UV map gives U, as in Blender.
- **Geometry > Pointiness** and **Geometry > Random Per Island** give
  Blender's numbers: Pointiness is worked out from the mesh's real edges and
  Blender's vertex normals (quad and n-gon diagonals no longer count), and an
  island is everything connected by edges, including loose edges, with the
  same random value as in Blender. Edge-wear masks look like Blender's, and
  random colours per island are the same colours as in Blender (earlier
  versions gave different colours). An object whose final Subdivision Surface
  modifier would be rendered from its cage uses Blender's subdivided mesh
  instead when its materials read either output, because Blender computes both
  on the subdivided mesh. Houdini geometry keeps its own rule (the polygons'
  edges, including diagonals of triangulated faces).

### Per-object values (Object Info, visibility, holdout)

Each Blender object and instance brings Blender's own per-object values into a
Link render:

- **Object Info > Random** is Blender's number: for an object, a hash of its
  name; for a geometry-nodes or collection instance, Blender's own per-instance
  number. Placing the asset once in Houdini gives exactly Blender's values.
  When Houdini places the same Blender asset several times (several Blender
  File nodes in one object, or copies on points or packed primitives), the
  first placement keeps Blender's values and every further placement gets its
  own different values, the way Blender varies the instances of a collection
  instance. Two separate Houdini objects that each place the asset once both
  show Blender's values (they look the same).
- **Visibility > Ray Visibility** (Camera, Diffuse, Glossy, Transmission,
  Volume Scatter, Shadow, Ray Cast) of the object, and of the object that
  instances it, is kept: an object hidden from the camera in Blender stays
  hidden, but still shows in reflections and casts shadows. Houdini's own
  visibility on the object node (Render Visibility, Phantom, Cycles visibility)
  can hide more, never show what Blender hides. An object with every ray type
  off, or with only camera rays in an indirect-only collection, is not rendered
  at all, as in Blender; the node's diagnostics name it.
- **Holdout** (object or view-layer collection) and **Shadow Catcher** come
  from Blender; turning them on in Houdini also works (either side can turn
  them on). A view-layer collection set to **Indirect Only** hides its objects
  from the camera, as in Blender.
- **Object Info > Color and Alpha** are the object's Viewport Display colour.
  **Object Index** is the object's Pass Index, unless the Houdini object's
  Cycles Pass Index is set (not 0), which then wins.
- An **Attribute** node of type **Instancer** reads per-instance attributes
  made by geometry nodes (for example a colour stored on the instances), then
  custom properties of the instancing object, then the object's own; type
  **Object** reads the object's custom properties or properties such as
  `color`, as Blender does. Convert & Append imports such materials too, but
  there the values are 0 (only Link carries them). Type View Layer is not
  supported.

Not carried yet: Texture Coordinate "From Instancer", Particle Info, light
groups, light and shadow linking, AO distance, caustics and shadow-terminator
settings from Blender (Houdini's settings apply).

### Hair in Link mode

Hair renders the way Blender's Cycles renders it, for both kinds of Blender
hair:

- **Hair curves** (Curves objects, including geometry-nodes hair such as
  Blender's hair assets): every control point is a hair key, with its
  **radius** (Blender's default 0.005 when the curves have none). Smooth
  (Catmull-Rom, Bezier, NURBS) curves render as Blender's **Curves > Shape**
  scene setting says (3D Curves or Rounded Ribbons); poly curves render as
  straight segments. As in Blender, only the object's **first material** is
  used, cyclic curves render open, and curves with one point render nothing.
- **Particle hair** (a Hair particle system rendered as Path): the strands
  Blender renders, with the render Steps, Children (Interpolated or Simple:
  only the children are rendered, like Blender), root and tip radius, radius
  scale, shape and Close Tip, and the material slot of the particle settings.
  The emitter mesh renders unless **Show Emitter** (Render) is off; its hair
  renders either way.
- Materials: **Hair Info** Intercept, Length and Random, **Texture Coordinate
  UV** and the **UV Map** node give Blender's values. For hair curves the UV is
  the first 2D vector attribute on the curves (Blender's hair keeps the surface
  UV in `surface_uv_coordinate`); for particle hair it is the emitter's UV at the
  root of each strand, and colour attributes of the emitter are read at the root
  too. Other attributes of the curves (per point or per curve) are read by name
  with the **Attribute** node. 4D vectors, quaternions, matrices and strings are
  not carried (the node's diagnostics name them). **Generated** is the
  object-space position on hair curves and the root's place in the emitter's
  texture space on particle hair, as in Blender.
- Object Info, ray visibility, holdout and the other per-object values above
  apply to hair too; particle hair has the emitter's values.
- **Curve Subdivisions** of the Blender scene is a setting for the whole render;
  Houdini renders use the ROP's **Curve Subdivisions** (Rendering > Geometry >
  Points and Curves), whose default 2 is Blender's. The node's diagnostics say
  when the Blender scene uses another number; set the ROP to that number to
  match. It only refines Rounded Ribbons.
- In the viewport the Blender File node's hull shows the outline of the hair
  keys (the convex hull of all strand points); the bounds include the strands'
  radius.
- Curves that geometry nodes instance (or curves made by geometry nodes on a
  mesh object) are not hair in Blender's render: Blender turns them into meshes,
  which are empty unless the curves have a bevel. Link mode does the same, so
  they do not render, as in Blender.
- Not yet: motion blur of moving or deforming hair (the hair keeps its pose at
  the frame; the object's own movement still blurs), and hair velocity.

Convert & Append writes hair curves as polylines (see below). It does not
convert particle hair: only the emitter mesh arrives, and the import log and
`cycles_import.json` name the emitters whose hair was left out.

### Point clouds in Link mode

Point clouds render the way Blender's Cycles renders them:

- **Point cloud objects** and point clouds made by **geometry nodes** (Mesh to
  Points, Distribute Points on Faces, the Points node, instanced or realized
  point clouds) render as spheres at the points, with each point's **radius**
  (Blender's default 0.01 when the points have none). Instance on Points uses
  up its points, so only the instances render, as in Blender.
- As in Blender, only the object's **first material** is used (a
  `material_index` on the points is ignored).
- Materials: **Point Info** Position, Radius and Random give Blender's values.
  Other point attributes (float, integer, boolean, 2D and 3D vectors, colours)
  are read by name with the **Attribute** node; an Attribute node named
  `radius` reads the radius and one named `position` the point's position. 4D
  vectors, quaternions, matrices and strings are not carried (the node's
  diagnostics name them). **Generated** is the object-space position, as in
  Blender (point clouds have no texture space).
- Object Info, ray visibility, holdout and the other per-object values above
  apply to point clouds too.
- In the viewport the Blender File node's hull shows the outline of the points
  grown by the largest radius; the bounds include every point's radius.
- Gaussian splat point clouds (Blender 5.3) are skipped with a warning: Blender
  renders them as splats, which Link mode does not carry yet.
- Not yet: motion blur from a `velocity` attribute or from moving points (the
  points keep their place at the frame; the object's own movement still
  blurs).
- Millions of points are fine: three million points with two attributes link
  and render a first image in about five seconds.

## What is lost or changed

`cycles_import.json` in the blend folder lists the lossy and skipped items of
each import.

- Edge attributes other than sharp edges and seams are not imported (Houdini has no edge
  attributes). The import log names them per object.
- Instance attributes of geometry-nodes instances (for example from Store Named Attribute on
  the Instance domain) are not imported yet; the import log names them. Link mode renders
  them for materials (Attribute node, type Instancer).
- Hair curves become polylines through their control points: Blender's smooth
  Catmull-Rom interpolation, Bezier handles and NURBS weights are not kept.
  Cyclic curves are written open. Particle hair is not converted; only the emitter mesh arrives
  (Link mode renders it), and the import log names the emitters.
- Quaternion and matrix attributes keep their values when an object's transform is baked into
  the geometry (frame sequences of the hierarchy structure, flattened instances); vectors are
  turned.
- Point cloud materials are not assigned (points have no primitives).
- Volumes and grease pencil objects are skipped with a note.
- Subdivision is applied as in a Blender render, so the result is the
  subdivided mesh, not a subdivision cage.
- Frame ranges write one file per frame. With the hierarchy structure, object
  nodes keep the first frame's transforms and later movement is in the
  geometry.

## Asset Browser: objects and collections

In the Asset Browser, **Convert & Append** for an object or collection converts
it in the background into Houdini files in `$HIP/assets_blender/<blend>/geo/`, as
packed primitives (a Cycles Blender File node in Converted mode), with appended (editable)
material copies. **Link (reference)** creates a Cycles Blender File node in Link
mode, with the viewport shape chosen under **Options > Imported Reference
Viewport**. Dropped into a geometry network, the node is created there.

To keep the geometry inside the scene instead, choose **Options > Save Location >
Stash in .hip**. The converted geometry is then stored on the Blender File node and saved
with the `.hip`; no geometry file is written (images packed in the `.blend` are
still extracted to `$HIP/assets_blender/<blend>/tex/`). The default, **Save to
Disk as .bgeo.sc**, keeps the `.hip` small and lets a re-import replace the files
for every scene that uses them (after asking, when another scene uses them).

Textures stay where the `.blend` points. The import report says how many, and
that **Cycles > Collect Textures** copies them into the project.
