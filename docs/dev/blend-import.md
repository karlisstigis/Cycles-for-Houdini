# Blender import: converter and importer contract

Status: in development (2026-11-13). The rules the import follows are in [Design rules](#design-rules);
code comments and tests name them as `blend-import.md: <rule>`, for example `blend-import.md: Worlds`.

Two parts share this contract:

- **Converter** (`houdini/python3.13libs/cycles_houdini/blender/blend_convert.py`): turns objects and
  collections of a .blend file into Houdini geometry, using the Blender worker's evaluated export.
- **Importer UI** (`houdini/python3.13libs/cycles_houdini/blender/blend_import_dialog.py`, menu
  File > Import > Blender (.blend)): lets the artist pick collections/objects and options, then calls the
  converter. The asset browser's Append mode for objects/collections calls the same converter.

## Design rules

Each rule has a heading of its own so that code can name it; the sections after this one give the details.

### Folder layout

Everything one .blend file produces goes into one blend folder under the asset root:
`<asset_root>/<blend>/geo/<asset>/...` for geometry, `<asset_root>/<blend>/tex/` for images that were packed in
the .blend, and one `cycles_import.json`. `<blend>` is the safe stem of the .blend file name. No path an import
writes may be longer than 240 characters: the import stops before writing anything and names the path. Details:
`storage="files"` in [Converter API](#converter-api).

### Import record

`cycles_import.json` is the only record of a blend folder: per asset its options, frames, files with their
digests and the scenes (.hip files) that imported it; per image its original path and, for an extracted image,
its copy. Before an import or an Update replaces or deletes files that another existing scene uses, the artist
chooses Replace (go on, and the report names those scenes) or Cancel (nothing is written).

### Textures in place

Textures are not copied: imported materials read the image files where the .blend points to them. Only images
packed inside the .blend are extracted, once per content, to the blend folder's `tex/` folder under their real
file name. The import report says when textures were left in place.

### Where imports write

Nothing is ever written inside the .blend file's folder or inside a registered asset library: an asset root
there is refused, with a message naming the folder. An untitled scene whose asset root uses `$HIP` writes to
`<user cache>/assets_blender/` instead, with a notice, and records no scene; Move into Project moves those
files into the project after the scene is saved.

### Collect and relink

Four commands keep imported files usable when folders change (details in the asset browser contract,
[Collect, Reload, Relink, Move into Project](asset-browser.md#collect-reload-relink-move-into-project)):

- **Collect Textures** copies the textures a scene uses into its blend folders (`$HIP/assets_blender/...`), so
  the project no longer depends on the library.
- **Reload from Library** after a collect keeps the collected copies; a copy is refreshed when the library
  file changed and kept, with a warning, when the library file is missing.
- **Relink** finds missing files under a folder the artist chooses, using the records of the blend folders
  first.
- **Move into Project** moves what an untitled scene wrote to the user cache into the project. It is offered
  once per scene after the first save and never moves anything by itself.

### Host menus

The Cycles menu in the network editor's menu bar (`houdini/NetworkViewMenu.xml`) and the Cycles submenu of a
node's right-click menu (`houdini/OPmenu.xml`) belong to the host package, not to the optional asset browser
add-on, so they work without it; the add-on keeps only Cycles > Asset Browser. Details:
[Linked materials and menus](asset-browser.md#linked-materials-and-menus-nov-2026-amendment).

The rules for the two asset nodes, the importer dialog and worlds are in the sections
[Asset nodes](#asset-nodes-blender-file-and-blender-hierarchy), "Importer dialog" and "Worlds" below.

## Worker listing (importer side)

`blend_worker.list_blend_contents(source, executable, timeout=60, cancelled=None) -> dict` (disposable worker,
read-only):

```
{
  "source": str,                          # resolved path
  "signature": [mtime_ns, ctime_ns, size, inode],
  "active_scene": str|None,
  "scenes": [{"name": str, "view_layers": [str], "active_view_layer": str,
              "collections": [str],       # top-level collections (children of the scene collection)
              "objects": [str]}],         # objects linked directly to the scene collection
  "collections": [{"name": str, "parent": str|None, "parents": [str], "children": [str], "objects": [str],
                   "excluded": bool,      # in the active scene's active view layer
                   "in_scene": bool,      # reachable from the active scene's collection tree
                   "hide_render": bool, "library": str|None}],
  "objects": [{"name": str, "type": "MESH"|"CURVES"|"CURVE"|"POINTCLOUD"|"EMPTY"|..., "parent": str|None,
               "collections": [str], "has_geometry_nodes": bool, "material_slots": [str],
               "hide_render": bool, "instance_collection": str|None, "library": str|None}],
  "frame_range": [start, end], "fps": float
}
```

- Worker command `list_blend_contents` (dispatched with the asset commands in `_asset_command` of the
  Blender component's `blender/cycles_blender/worker_server.py`); the in-Blender part is
  `blender/cycles_blender/blend_list_contents.py`. The main file is opened with
  `load_ui=False, use_scripts=False` and never saved; the worker compares the file signature before and
  after, the host compares it again.
- `parent` is the first of `parents` (a collection can have several parents). `excluded` is true when no
  occurrence of the collection in the active view layer is reached through included layer collections
  (Blender propagates exclusion to children). Collections outside the scene have `in_scene: false` and
  `excluded: false`. Objects' `collections` never contain the scene (master) collection.
  `material_slots` holds the slot's material name, `""` for an empty slot.
- `blend_worker.checked_listing(reply)` validates the reply (ValueError on a malformed structure), keeps
  the first entry per (name, library) and drops references to unlisted collections/objects.
- `tests/blend_list_contents_smoke.py` runs the listing in Blender (directly and over the framed worker
  protocol) and accepts extra `--source` files, which it lists from a private copy.

## Converter API

```python
blend_convert.import_blend(
    source,                       # absolute .blend path
    selection,                    # [("collection"|"object", name), ...]
    *,
    storage="files",              # "reference" | "embedded" | "files"
    structure="packed",           # "hierarchy" | "packed"; any other value raises BlendImportError
    materials="link",             # "link" | "append" | "none"
    frames=None,                  # None = current/scene frame; (start, end, step) = sequence (files only)
    include_disabled=False,       # include view-layer-excluded child collections
    asset_root="$HIP/assets_blender",  # the dialog default of earlier builds reads as this
    parent=None,                  # Houdini network for new object nodes (default /obj)
    position=None,                # network position for the first created node
    executable=None, timeout=300,
    report=None,                  # callable(text, error=False) for status messages
    convert_vectors=True,         # extension: rotate FLOAT_VECTOR attributes to Houdini axes (False: keep)
    on_shared="replace",          # "replace" | "cancel": a re-import changing files another scene uses
) -> list[hou.Node]               # created top-level nodes

blend_convert.shared_scenes(source_or_snapshot, selection=None, *, asset_root="$HIP/assets_blender",
                            structure=None, library_root=None)
    -> [{"kind", "name", "asset", "scenes": [other existing .hip paths]}]
```

`shared_scenes` (host thread) is what a UI calls before importing to show "<asset> is also used by:
shot_v01.hip. Replacing it changes that scene too. [Replace] [Cancel]" and then pass `on_shared`
(`build_import(payload, on_shared=...)` overrides the snapshot's value).

- `storage="reference"` creates a Cycles Blender File node in Link mode (stock `cycles::blender_reference`
  SOP when the asset is not installed), one geo per selection item; `structure` is ignored and Blender is
  not started. `include_disabled` sets the row's
  `ref_include_disabled_collections1`.
- `storage="embedded"` keeps the converted geometry inside the .hip (the Blender File node's Stash in .hip;
  a Stash SOP per geo node in the fallback).
- `storage="files"` writes `.bgeo.sc` files and loads them (the Blender File node's inner File SOP; a File
  SOP in the fallback). Layout ([Folder layout](#folder-layout),
  `blender/blend_assets_layout.py`):

  ```
  <asset_root>/<blend>/geo/<asset>/<asset>.bgeo.sc            packed (frame ranges: <asset>.$F4.bgeo.sc)
  <asset_root>/<blend>/geo/<asset>/hierarchy/<Object>.bgeo.sc
  <asset_root>/<blend>/geo/<asset>/hierarchy/shapes/<first object>.bgeo.sc
  <asset_root>/<blend>/tex/<real image file name>             packed images only
  <asset_root>/<blend>/cycles_import.json                     one per blend folder
  ```

  `<blend>` is the .blend file's stem; another .blend with the same stem gets `<stem>__<parent folder>`
  (then `_2`, ...); a folder whose JSON names the source is reused. Asset, object and shape names are the
  Blender names made safe for Windows (`safe_name`: forbidden characters, `$` and backtick become `_`),
  allocated in the JSON (`original` = `"collection:Name"` / `"object:Name"` for assets, the Blender
  object name for objects and shapes): a clash gets `_2`, `_3`, and a name never changes once given. A
  folder on disk that the JSON does not list belongs to someone else and is never written into.
- `cycles_import.json` ([Import record](#import-record)) is the only record of a blend folder (earlier builds wrote
  per-structure manifests and a texture manifest; they are not read). Per asset
  (one record for all structures of a selection item): `options` (import options, `selection`,
  `blender` = version, scene, frame, units, objects, `report` = lossy / skipped / renamed / notes, and
  `structures` = the files each structure wrote), `frames`, `files` (relative path + content digest) and
  `scenes` (the .hip files that imported it; the current one when saved). Per image: original path,
  `packed`, and for extracted images the file in `tex/` and its sha256. The blend folder is locked
  (`cycles_import.lock`) from planning through writing to saving.
- Before anything is written: names are allocated, the longest path is checked against 240 characters
  (`BlendImportError` naming the path), and the shared-scene rule applies: when another existing .hip in
  the asset's `scenes` would see its files replaced or deleted, `on_shared="cancel"` raises
  `BlendImportError` naming the scenes, `"replace"` (default) goes on and reports them.
- A re-import replaces: files are overwritten in place (skipped when the JSON records the same content
  digest); after the JSON is saved, files the JSON lists for the same asset and structure that this import
  no longer writes are deleted (`ImportStore.stale_files` + `delete_stale`; other structures' files are
  kept). A JSON that was missing, unreadable (renamed to `cycles_import.json.bad`) or of a newer schema
  deletes nothing; a newer schema stops the import. File SOPs reading files under a replaced folder get
  Reload pressed, and packed File SOPs whose parm names the previous import's other pattern (frame
  range <-> single frame) are pointed at the new one.
- `structure`:
  - `hierarchy`: one geo object per Blender object, parented like Blender (empties become null objects),
    object transforms on the Houdini objects, geometry in local space. A collection is placed like a
    collection instance (its `instance_offset` moves the objects, not their geometry), the same as
    `packed`; nested collections are not separate nulls (only the selected one is).
  - `packed`: one geo per selection item; every Blender object is a packed primitive with `name`
    (object name) and `path` (`/Collection/Child/Object`) prim attributes, instances share geometry.
  - One converted file per asset, always packed; plain polygons come from the Blender File node's Load As:
    Unpacked (Unpack SOP, Iterations; see [Asset nodes](#asset-nodes-blender-file-and-blender-hierarchy)).
  - Instancing is never lost (artist decision 2026-11-12): `packed` packs everything with shared children;
    `hierarchy` packs other-source instances and, of an object's own geometry-nodes duplis (depth + 1, same
    source), the shapes that repeat; a single own dupli (e.g. a component split) is merged into the object.
    Objects whose only geometry is one shape used by other objects too (linked duplicates, identity local
    transform) get `shape=<key>` instead of `geo`; with files storage and one frame (`share_shapes`) they load
    `hierarchy/shapes/<first object>.bgeo.sc` with File SOP `loadtype=delayed` (packed disk primitive,
    `shop_materialpath` inside). Frame ranges and embedded storage keep one geometry per object.
- Materials: each Blender material slot becomes a primitive group `mat_<Name>`; a Material SOP assigns
  translated `/mat` materials per group (`materials="link"|"append"`, via the existing library-material
  import); the prim string attribute `blender_material` keeps the original name for information. Both modes
  reuse an existing node first (`importers.find_imported_material`: link = the shared node, append = this
  scene's non-linked copy with the same source and Blender name), so the `shop_materialpath` written into
  replaced packed files stays valid for every File SOP that reads them.
- Node names: a taken name gets `_1`, `_2`, ... (`blend_library_materials.free_child_name`), never
  Houdini's own bump of a trailing number (`tree_LOD1` -> `tree_LOD2`, `island_tree_01` -> `island_tree_02`
  read as other Blender assets). This applies to imported objects, references and library materials.
- Two-phase: Blender work runs on a background thread (`prepare_import(...)` -> payload), Houdini nodes
  are built on the host thread in one undo group (`build_import(payload)` -> nodes). `import_blend` is the
  synchronous wrapper. Call it on the host thread: the Houdini Python Shell runs code on another thread,
  so wrap it in `hdefereval.executeInMainThreadWithResult` there (material building checks the thread).

### Converter implementation notes

- `prepare_import(source, selection, **import_blend keywords, cancelled=None, report=None)` uses no HOM
  (safe on a background thread). It also accepts, as `source`, the dict from `import_snapshot(source,
  selection, **keywords)` (host thread: validates and expands folders early; the asset browser uses it).
  `report` must be thread-safe; prepare reports notices with it and stores it for `build_import`.
  `cancelled` is checked between Blender results (frames/items); a running evaluation finishes or times out.
- The asset root is expanded by `build_import` on the host thread (`resolve_folders`, through
  `blend_library_materials.asset_root_text` / `resolve_asset_root`, shared with library materials). An
  untitled scene with `$HIP` in `asset_root` writes to `<asset cache>/assets_blender/...` instead ([Where imports write](#where-imports-write)), with
  a notice, and records no scene; a root inside the .blend file's folder or `library_root` is refused with
  a message naming the folder (same rule).
- Worker requests (one per selection item and frame, through `blend_worker.evaluate_many`, persistent
  workers): `frame` is the frame number or `"scene"` (the file's saved frame), `native_subdivision` =
  `not apply_subdivision` (default True: an eligible final Subsurf comes back as its control cage with
  `subdivision` config and, converter only, `subdivision_viewport_level`), and the converter flags `vertex_groups`, `curves`, `pointclouds`,
  `object_tree` (+ `include_excluded_collections`, `material_textures="in_place"`, and
  `material_export="names"` for later frames or `materials="none"`). Render requests never set these
  flags, so their manifests (and copied textures) are unchanged.
- Converter manifests (`blend_export`) add `converter: true`, `objects` (name, type, Blender parent,
  collection path, `C * matrix_world`), `curves` / `pointclouds` records and, per instance, `owner`,
  `depth`, `dupli_path` and `geometry_type` (`mesh`, `curves`, `pointcloud`). Mesh records add
  `vertex_groups` and `skipped_attributes`; material records add `local_name`, `library` (absolute path
  of a linked library, else "") and `default`. `persistent_id` lists the innermost dupli index first, so
  the selected collection's member is the last used entry. Geometry nodes geometry instances (temporary
  duplis) are read with `to_mesh()`: `to_mesh(preserve_all_data_layers=True, depsgraph=...)` returns the
  instancer's own (often empty) mesh for them. Render (Link) requests do the same since task S1
  (2026-11-13; before, those instances came out with 0 vertices and vanished from Link renders).
- Objects owned by a selection member: geometry whose source is the member and whose depth is at most one
  below the member's is "own" geometry (including geometry-nodes component splits such as a point cloud
  output); deeper or other-source instances are "generated" and stay packed inside the member's geo
  object in `hierarchy`.
- Nested instances (`nested_instances=True`, artist decision 2026-11-12). Worker request flag
  `nested_instances`: for every owner with a geometry nodes modifier whose evaluated geometry has instances,
  `_ConverterState.instance_tree` records `Object.evaluated_geometry()`'s instance tree in the manifest
  (`instance_trees[owner].nodes`: per node `name`, `count`, `ref_index` and `transforms` arrays (row-major
  Blender local matrices; `foreach_get` is column-major) and `references`: child node index for a referenced
  GeometrySet that instances further geometry, else -1). Geometry still comes from the depsgraph duplis.
  `_nest_instances` maps each owned dupli by its `dupli_path` (innermost first: component index, then one
  index per level, then the collection member index) down the tree while references are child nodes; that
  path is its occurrence. Each tree node becomes one prototype (`_Shapes.add_prototype`, content digest incl.
  children) from its first occurrence: leaf shapes relative to the occurrence (used once: unpacked into the
  prototype; repeated: packed) plus packed child occurrences. Other occurrences must match (same shapes and local transforms)
  or the owner stays flat with a note. The owner's top-level occurrences replace their duplis as
  `geometry_type="nested"` instances. Hierarchy children use `_Shapes.closure`; `write_geometry`
  passes material paths down the nesting. Verified against bpy: same world-space points as the flat result.
- Render-time subdivision (`apply_subdivision=False`, artist decision 2026-11-12): `mesh_geo` sets
  `Geo.subdivision` (`subdivision_settings`: type, render_level, ipr_level, boundary, fvar, use_creases);
  Blender `crease_edge` -> vertex `creaseweight` on the corner that starts each edge (Houdini order, after
  the winding flip), `crease_vert` -> point `cornerweight`, both x10. Shapes de-duplicate by content and
  setting. Cycles subdivides per OBJ, so packed frames carry `parts` (one per setting, unsubdivided
  first) built as `<item>`, `<item>_subdiv`, `<item>_subdiv_2` geo objects with files `<stem><suffix>`;
  hierarchy entries carry `subdivision` (own geometry decides; packed instances inherit, noted). The geo
  object gets `object_properties.install` + `cycles_subdivision_*` parms.
- UV maps and UV attributes (task U, 2026-11-13). `blend_export.uv_sets(layer_names, default_layer,
  attributes, referenced)` is the one pure decision, made on the Blender side for every path (Convert &
  Append, Stash, Hierarchy, Link/render). `referenced` = `material_field_names(palette)`: names the
  object's materials read through UV Map (`uv_map`), Attribute (GEOMETRY, `attribute_name`), Normal Map and
  Tangent (`uv_map`) nodes, inside node groups too. A generic attribute is a UV set when it is not
  dot-prefixed, its domain is CORNER or POINT, its type FLOAT2 or FLOAT_VECTOR, and it has "uv" (any
  case) in its name (artist rule change: being read by a material is not enough, so `wind` or `rest`
  never become UVs). Default: the render-active real UV map; with none, a candidate a material reads
  (first by name), then `UVMap`, `uv`, `UV`, `map1` (never matches: no "uv"), then the first by name. Every set keeps its
  own name except the default when no material names it. Record fields: `uv_layers[].keep_name`,
  `attributes[].uv_set` / `keep_name`, `default_uv` (may name an attribute) and `default_uv_source`
  (`uv_layer`, `attribute` or None). Records without `keep_name` keep every name (older records).
  A mesh with a UV attribute adds an info diagnostic `blend.uv_attribute` (`uv_summary`).
  Converter (`blend_convert._uv_sets`, `mesh_geo`): the default is vertex `uv`; kept sets use `uv_set_name`
  (sanitised; `blend_` prefix only for reserved names, so `uv2` stays `uv2`); all are vertex float3 like `uv`
  (FLOAT2 padded with 0, vectors keep z, no axis turn; Point values promoted to vertices); UV attributes
  are no longer written as generic (axis-turned) vectors; the report notes `<mesh>: UVs: uv (from X), ...`.
  The `uv2`, `uv3` numbering is gone. Native Link path (`src/houdini/blend_mesh.cpp`, `capture_blend_mesh`)
  follows the same record fields: the default fills the standard UV stream (from a uv_layer or a
  `uv_set` attribute) and a named field exists only when `keep_name` is true. Named copies are needed
  because a Cycles Attribute/UV Map node resolves a name other than `uv` only through a field of that name
  (the standard UV attribute is named `uv`). Named UV-map copies are stored with 2 components
  (Float2, as Blender stores UV maps), so the Attribute node's Fac is U as in Blender (was (U+V)/3).
  Link renders are Blender-exact with no switch (task S1, artist choices 2026-11-13 and 2026-11-14):
  render requests use `uv_sets(..., promote=False)`, so only real UV maps are UV sets; "uv"-named
  attributes stay plain named attributes, a mesh with no UV map then has no default UV (as in Blender),
  and an info diagnostic `blend.uv_attribute` ("No UV map; X stays a named attribute ...") names them.
  The promote switch S1 added (`CYCLES_HOUDINI_BLENDER_UV_ATTRIBUTES`, request field `uv_attributes`,
  a tenth prototype-key element) was removed; the key is back to its 9 elements. Converter requests
  always promote (Convert & Append, Stash, Hierarchy unchanged).
- Every mesh attribute (artist rule U5, 2026-11-13; Convert & Append, Stash, Hierarchy). Converter
  requests export generic mesh attributes of FLOAT, INT, INT8, BOOLEAN, FLOAT2, FLOAT_VECTOR, FLOAT_COLOR,
  BYTE_COLOR (read through `color`: linear floats), INT32_2D, plus `_MESH_CONVERTER_PROPERTIES`
  QUATERNION, FLOAT4X4, FLOAT4, INT16_2D, and STRING (int32 index array + `strings` table in the record:
  numeric transport only; read per element, StringAttributeValue has no foreach_get). Curves and point
  clouds of converter requests carry the same types (`_generic_attributes(..., extended=True)`,
  `_GENERIC_CONVERTER_PROPERTIES`, task B1 2026-11-14); the Link hair and point records keep
  `_GENERIC_PROPERTIES`. Skipped: dot-prefixed names, `position`, UV maps,
  colour attributes (own path), `custom_normal` (`_TRANSLATED_MESH_FIELDS`: N comes from corner normals),
  `material_index` / `sharp_face` (`_REPRESENTED`). Converter: Point/Corner/Face -> point/vertex/prim;
  EDGE -> `report.skip` unless an edge group (`sharp_edge`, `uv_seam`) or a subdivision crease (no more
  averaging); QUATERNION w,x,y,z -> x,y,z,w with the axis part turned; FLOAT4X4 memory is already Houdini
  order (column-major column-vector = row-major row-vector) and is turned by C * M * C^-1; STRING -> string
  attribute. `attribute_name` and `uv_set_name` prefix reserved names with `blend_` (so they sort together in the spreadsheet; `uv\d*`
  and `mat_` are no longer special). The render colour also keeps its name when `keep_name` (a material
  names it: Color Attribute `layer_name` or Attribute node). The import log line counts lossy and skipped
  items. Task B1 (2026-11-14): `Report.skip_attribute(object, name, detail, reason)` also fills
  `not_imported` {object: {reason: [names]}} (in `cycles_import.json`), and `prepare_import` sends
  `attribute_lines(report)`, one line per object, to the import log (the particle hair channel:
  `payload["report"]` or `payload["messages"]`). Skipped records carry `reason`
  (`blend_export.skip_reason`); records carry `object_name` (first object using them) for the line.
  `Attrib.info` ("quaternion", "matrix", from `_TYPE_INFO`) is written by `write_geometry` as the
  attribute option `type` (Houdini's type info; probe 2026-11-14: `setOption("type", ...)` gives
  `qualifier()` Quaternion / Matrix, survives .bgeo.sc, and the Transform SOP rotates it); the digest
  includes it only when set, so earlier imports keep their digests. Converter Curves export every
  attribute except `position` (and `radius` when it is the width), so `curve_type`, `cyclic`, handles,
  NURBS data and `resolution` arrive as plain attributes. Geometry-nodes instance-domain attributes are
  detected (`_ConverterState.instance_domain`, warning `blend.instance_attributes`) and reported, not
  carried: packed primitives get no per-instance values yet. Proof:
  `tests/pytest_cases/test_blend_all_attributes.py`, live `tests/houdini_blend_converted_attributes_smoke.py`
  (fixture `tests/blend_all_attributes_fixture.py`). The native Link path is unchanged for U5 (follow-up): it already passes FLOAT, INT, INT8,
  BOOLEAN, FLOAT2 and FLOAT_VECTOR Point/Corner/Face fields to the renderer by name.
- Link render parity with Blender's own translator (task S1, 2026-11-13; reference Blender v5.3.0
  `intern/cycles/blender/`, internal sync audit of 2026-11-13 (not published) items 1, 2a, 4, 7, 16,
  17). Geometry-nodes geometry instances: see above. Generated coordinates: every mesh record carries
  `texture_space` = {location, size} read from the evaluated mesh Blender renders (the evaluated object
  data for mesh objects, else the `to_mesh` result): Blender's `BKE_mesh_texspace_get`, i.e. the box
  before modifiers or a custom texture space, a zero-size side counted as 1. `capture_blend_mesh` stores
  it in `MeshData::texture_space` and `core/generated_coordinates.h` applies Blender's
  `mesh_texture_space` + `create_mesh` formula (size' = 0.5 / size, loc' = loc * size' - 0.5,
  generated = P * size' - loc'), so a flat side maps to 0.5 and Array/Mirror/geometry-nodes results
  keep the original box. Houdini geometry keeps its SOP-bounds rule (field empty). The field is part of
  the mesh payload, wire and shape comparisons (a different texture space never shares a Cycles mesh).
  Not done (audit item 10): Blender's orco (undeformed coordinates) for armature / shape-key
  deformation; that needs the exporter to read `CD_ORCO` (not exposed to Python: it would need a
  rest-pose evaluation, e.g. the mesh with deforming modifiers disabled, or the basis shape key) and
  the madd with the texcomesh texture space. Material indices outside the object's slots are clamped as
  in `create_mesh` (`clamp(index, 0, slots - 1)`, negative to 0) in `blender_assets._native_mesh` and
  `blend_mesh.cpp` instead of rejecting the mesh; the slot count joins the mesh key only when clamping
  happens. Objects a Link render cannot carry yet (VOLUME, GREASEPENCIL and Gaussian-splat point
  clouds, including geometry-nodes component splits) are skipped instead of failing the whole link: one
  warning `blend.unsupported_geometry` per type naming the objects (hair and point clouds are carried:
  see "Hair" and "Point clouds" below). A
  selection with nothing else fails with the skipped objects named. Live check: `tests/houdini_blend_sync_parity_smoke.py` renders the same
  asset with Blender's Cycles (the worker's executable) and our Link path and compares the images.
- Per-object values (task S2, 2026-11-14; audit items 3, 5, 6; reference Blender v5.3.0
  `intern/cycles/blender/object.cpp` `sync_object` / `sync_object_attributes`, `util.h`
  `object_ray_visibility`, `intern/cycles/util/hash.h`, `blenkernel/intern/object_dupli.cc` `make_dupli`
  and `BKE_object_dupli_find_rgba_attribute`). Render (Link) requests only: every manifest instance carries
  `blender_object` = {random_id, visibility, holdout, shadow_catcher, pass_index, color [RGBA],
  attributes [{type, name, value RGBA}]} (`blend_export._ObjectValues`). The selection is evaluated as an
  identity collection instance (`_select`), so its members arrive as depth-1 duplis of the selector;
  Blender renders the source collection with them as real objects. A depth-1 dupli (and an object
  selection) is therefore a real object: `random_id = hash_uint2(hash_string(ob.name), 0)` (the name
  without library; Blender builds with unsigned `char`, checked against a Cycles render of a non-ASCII
  name). Deeper duplis recompute `make_dupli`'s number with the selector's persistent_id level dropped
  and the member as root object (`dupli_random_id`); object selections use the depsgraph's
  `random_id`. Visibility = `ray_visibility(ob) & ray_visibility(root)` (Cycles PathRayVisibility bits,
  the same bits as the Houdini mask: camera 1, transmission 2, diffuse 4, glossy 8, volume scatter 16,
  shadow 96, ray cast 128); holdout = the root's view-layer collection holdout (`_layer_flags`: only
  layer collections that link the object directly, as `layer_collection_objects_sync`) or its
  `is_holdout`; indirect-only clears camera unless holdout; shadow catcher = object or root. An instance
  with visibility 0 is not exported (info `blend.invisible_object`). Colour, alpha and pass index come
  from the (evaluated) dupli object. Object/Instancer attributes: only names the instance's materials
  read (`material_object_attributes`, Attribute nodes of type OBJECT/INSTANCER inside groups too); the
  lookup ports `BKE_object_dupli_find_rgba_attribute`: Instancer: particle settings, geometry-nodes
  instance attributes innermost level first (`_ObjectValues._instance_attribute` walks the root's
  `evaluated_geometry()` along persistent_id: a level with own component duplis adds an index level;
  instance `id` values map to indices, a repeated id gets a random id in Blender and is not found here),
  the root object's properties; then (both types) the object's and its data's custom property or RNA
  path, converted as `find_property_rgba`; 0,0,0,0 when not found. `blender_assets._blender_object`
  turns each attribute into a 3-component field `encodeAttrib(object_attribute_field(type, name))`
  (`__cycles_blender_<type>_<name>`) and a 1-component `..._alpha` field; `blend_materials` points the
  Attribute node (and its Alpha output) at the same names. Native side: `python_scene.cpp` parses the
  member's `blender_object` into `GeometryRequest::blender_object` (`BlenderObjectValues`,
  `scene_adapter.h`); the attribute fields are prepended to the member's uniform fields
  (`point_instancer.cpp` placements, the packed Blender branch in `scene_adapter.cpp`), so they are part
  of the binding layout like any instance field and every instance of a mesh still shares it.
  `apply_blender_object` combines with the Houdini object at the three places that create Link member
  objects (packed Blender references, point instancing with and without motion): visibility AND,
  holdout/shadow catcher OR, Houdini pass index wins when not 0, colour/alpha Blender's
  (`ObjectProperties::color/alpha`, new, sent to `ccl::Object::set_color/set_alpha`; wire
  `worker_protocol_version` 78; Houdini objects keep Cycles' 0). Random: the first placement of a member
  (by `object_key`) under a Houdini root keeps Blender's number; later placements XOR it with
  `hash_uint2(hash_string(object key), 0)`, like Blender's root-object XOR. Members' values are part of
  the point-instancer stamp. Not done: `dupli_generated`/`dupli_uv` (Texture Coordinate From Instancer:
  the importer still refuses it), Particle Info, light groups, light/shadow linking, AO distance,
  caustics flags, shadow-terminator offsets, asset name (all Houdini's), View Layer attributes.
  Offline proof `tests/pytest_cases/test_blend_object_values.py`; live
  `tests/houdini_blend_sync_parity_smoke.py` (second asset "ObjectValues").
- Pointiness and Random per Island (task S3, 2026-11-14; audit items 8, 9; reference Blender v5.3.0
  `intern/cycles/blender/mesh.cpp` `attr_create_pointiness`, `attr_create_random_per_island`,
  `util/disjoint_set.h`, `util/hash.h`). Render (Link) mesh records carry `arrays.vertex_normals`
  (`mesh.vertex_normals`, Blender's `vert_normals()`) next to `arrays.edges` (`mesh.edges`, Blender's
  `edges()`: polygon and loose edges in Blender's order); Convert records do not. `capture_blend_mesh`
  stores them in `MeshData::blender_vertex_normals` (pose data: re-read when only the pose changes, and
  excluded from the topology key with positions and normals) and `MeshData::blender_edges` (flat vertex
  pairs). Non-empty vertex normals select Blender's rules: `core/mesh_pointiness.h`
  `blender_pointiness` welds coincident vertices (the shared ordered search, squared distance in float),
  accumulates and normalizes Blender's normals per original, walks the unique welded edges in edge order,
  blurs once, all in float32 with Cycles' SSE4.2 operation order (DPPS dot, `normalize` divides,
  `safe_normalize` multiplies by the reciprocal, NaN clamps to -1); `core/mesh_islands.h` joins the same
  `ccl::DisjointSet` over the edges in order and hashes `find()` of each loop triangle's first corner (or
  each polygon's, for subdivision cages). Houdini meshes (fields empty) keep the face-derived rules (the
  weld's distance test is now float like Blender's, otherwise unchanged). Both fields are part of the
  payload, wire (`worker_protocol_version` 79), shape and island-topology comparisons; validation
  requires one normal per point and edge vertices in range. With deformation blur and an offset
  shutter, the requested frame's normals replace the centre's (`assemble_external_mesh_motion`), since
  Pointiness is computed at the requested frame, as in Blender. Native subdivision: when a palette
  material links Geometry Pointiness or Random Per Island (`blend_export.reads_mesh_intrinsics`, groups
  included), Link falls back to Blender's evaluated (subdivided) mesh (`blend.subdivision_evaluated`),
  because Blender computes both on that mesh. Not exact: acosf may differ by one float step between C
  libraries; a near-duplicate weld among points with equal coordinate sums depends on the sort order
  (Blender's comparator is not a strict weak order). Offline proof
  `tests/pytest_cases/test_blend_mesh_intrinsics.py` (Blender bake, a Python transcription and the C++
  headers compiled with g++); live `tests/houdini_blend_sync_parity_smoke.py` (third asset
  "Intrinsics").
- Hair (task H1, 2026-11-14; audit item 4; reference Blender v5.3.0 `intern/cycles/blender/curves.cpp`
  `sync_hair` / `export_hair_curves` / `attr_create_generic` and `sync_particle_hair` /
  `ObtainCacheParticleData` / `ObtainCacheParticleUV` / `ObtainCacheParticleVcol` /
  `ExportCurveSegments` / `shaperadius`, `object.cpp` `sync_objects` (OB_VISIBLE_PARTICLES),
  `geometry.cpp` `find_used_shaders`, `util.h` `mesh_texture_space`, `util/hash.h`
  `hash_uint2_to_float`, `blenkernel/intern/object_dupli.cc` `make_duplis_geometry_set_impl`). Render
  (Link) requests only. Worker: `blend_export._hair_curves_record` (object type CURVES, the object's own
  evaluated Curves data) and `_particle_hair_record` (a MESH instance with `show_particles` and a
  render-enabled Particle System modifier of type Hair rendered as Path, not a geometry-nodes geometry
  instance) write records to `manifest["hair"]`; their instances carry `mesh_key` = the record key,
  `geometry_type` `curves` / `particle_hair` and the object's (emitter's) `blender_object` values. The
  hair instance of an emitter is exported before the `show_self` test, so Show Emitter off keeps the
  hair. Record: `kind` (`curves` / `particles`), `shape` (`LINEAR` when the first curve is POLY, else
  `scene.cycles_curves.shape`: `THICK` or `RIBBON`), `arrays` {positions (keys, Blender local),
  offsets (curves + 1), radius (`radius`, per point or per curve and of any type `lookup<float>`
  converts, see the radius note below, or 0.005; particles: `shaperadius` over the normalized length
  with root/tip x radius scale / 2 and Close Tip), randoms (`hash_uint2_to_float(curve index, 0)` over
  all curves, numpy `hash_uint2_array`), particles only: material_slots (`settings.material - 1`
  clamped to the palette) and generated (root key * size' - loc' of the emitter mesh's texture
  space)}, `attributes` (Curves: every POINT/CURVE attribute `_generic_attributes` carries except the
  internal ones; particles: the active-render UV map plus the UV maps and byte-colour corner
  attributes the palette reads, at each strand root via `ParticleSystem.uv_on_emitter` /
  `mcol_on_emitter`, colours sRGB -> linear with alpha 1), `uv_attribute` (Curves: the first FLOAT2
  CURVE attribute, Blender's "weak" standard-UV rule; particles: the active-render UV map),
  `max_radius`, `skipped_attributes` (info `blend.hair_attribute`). Particle keys come from
  `ParticleSystem.co_hair(object, particle_no, step)` (`BKE_particle_co_hair`, the render path cache of
  the render depsgraph) for the children when there are any, else the parents, with
  `(1 << render_step) + 1` steps (+ `kink_extra_steps` for spiral kink), times the inverse object
  matrix; a step past a strand's cached keys returns zero through RNA where Cycles keeps the previous
  key, so a zero key repeats the previous one. The palette is `find_used_shaders`'s: the data's
  materials with object-linked slots; Curves objects pass only the first (Blender sets every curve
  shader to 0). Houdini: `blender_assets._native_curves` removes curves with fewer than two keys
  (Cycles builds no segment for them; their randoms stay index-based), expands CURVE attributes per key,
  splits colours into `encodeAttrib(name)` (3) and `__cycles_blender_alpha_<name>` (1) as on meshes,
  and interns the arrays with `_native.blend_curves(record, arrays, palette)` -> `capture_blend_curves`
  (`blend_mesh.cpp`): a `CurveData` carried by a `MeshData` whose `external_curves` is set (no other
  mesh data), so the existing external-mesh path (direct members, packed and point instancing,
  `NativeExternalBindings` variants) carries hair unchanged; the binding copies the curves with the
  resolved palette and instance-field layout, and `NativeSceneAdapter` publishes them in
  `update.curves`. Engine: `CurveData::generated` (explicit per-curve Generated, particle hair) and
  `CurveData::generated_attribute` (false: no Generated attribute, so Cycles' kernel falls back to the
  object-space position as for Blender's Curves objects); wire `worker_protocol_version` 80. Intercept
  and Length are computed by `session_strands.cpp` from the keys with Blender's formula. Bounds:
  `blend_mesh_bounds` grows hair keys by their radius; `blend_bounds.evaluated_record` and
  `blend_viewport` include `manifest["hair"]` (a hair-only object's proxy is the convex hull of its
  keys; bounds add `max_radius`). Not done or not exact: curve deformation and velocity motion blur
  (`assemble_external_mesh_motion` is skipped for carriers: the nominal pose renders, transform motion
  applies); `cycles_curves.subdivisions` is a scene parameter (the ROP's Curve Subdivisions, `curve_subdivisions`, default 2
  as Blender's; info `blend.hair_settings` when the Blender scene differs from 2, since the export cannot see the ROP); FLOAT4, QUATERNION, matrix and string
  attributes; constant attributes are stored per element (Blender: ATTR_ELEMENT_MESH, same values);
  curve vertex normals (`ATTR_STD_VERTEX_NORMAL`). Curves that geometry nodes instance, and curves
  made on a non-Curves object, reach render engines as legacy Curve duplis
  (`make_duplis_geometry_set_impl` with `use_new_curves_type` false), which Blender's Cycles meshes: an
  unbevelled one gives no mesh, which the exporter now skips like Blender (it used to fail the link with
  "Unable to evaluate Blender mesh"). Convert requests are unchanged: `_ConverterState.geometry` keeps
  its `curves` records (polylines with `width`: the radius attribute, or since task B1 Blender's 0.005
  without one, `_converter_radius`: CURVE-domain radii and every type `lookup<float>` converts, see
  the radius note below); point
  clouds likewise get `pscale` 0.01; per-curve
  `material_index` used) and particle hair is not converted (since task H2 with a warning, see "Point
  clouds"). Offline proof `tests/pytest_cases/test_blend_hair.py`; live
  `tests/houdini_blend_sync_parity_smoke.py` (fourth asset "Hair", rendered with Blender's pixel filter).
- Point clouds (task H2, 2026-11-14; audit item 4; reference Blender v5.3.0
  `intern/cycles/blender/pointcloud.cpp` `sync_pointcloud` / `export_pointcloud` / `copy_attributes`,
  `geometry.cpp` `determine_geom_type` / `find_used_shaders` / `get_default_shader`, `object.cpp`
  `object_is_geometry`, `util/hash.h` `hash_uint2_to_float`, `scene/attribute.cpp` standard names,
  `kernel/svm/attribute.h` Generated fallback). Render (Link) requests only. Blender renders every
  object whose evaluated data is a PointCloud (ID_PT) as Cycles points: POINTCLOUD objects and every
  geometry-nodes point cloud, which reaches the depsgraph as a POINTCLOUD dupli of its object (Mesh to
  Points, Distribute Points on Faces, Points, realized point instances; each instance of an unrealized
  instanced point cloud is its own dupli; Instance on Points consumes its points, so only the
  instances render). Worker: `blend_export._point_cloud_record` writes records to `manifest["points"]`
  (conversions keep their own `pointclouds`); instances carry `geometry_type` `points`, `mesh_key` =
  the record key and the object's `blender_object` values (through `hair_instance`, so visibility and
  per-object values work as for meshes). Record: `kind` `points`, `arrays` {positions, radius (POINT
  `radius` of any type `lookup<float>` converts, see the radius note below, or Blender's 0.01),
  randoms (`hash_uint2_to_float(point index, 0)`,
  `ATTR_STD_POINT_RANDOM`)}, `attributes` (every POINT attribute `_generic_attributes` carries, by
  name: FLOAT, FLOAT_VECTOR, FLOAT2, INT, INT8, BOOLEAN, INT32_2D, FLOAT_COLOR, BYTE_COLOR; `radius` is
  Cycles' standard radius attribute, which an Attribute node named "radius" reads in both; `position`
  is copied under its name, as Blender does, only when a material reads it), `max_radius`,
  `skipped_attributes` (info `blend.point_attribute`: FLOAT4, QUATERNION, matrix, string). The palette
  is the first material slot only (`export_pointcloud` sets every point's shader to 0;
  `material_index` is ignored, as in Blender). `PointCloud.type == "GAUSSIAN_SPLAT"` is not rendered
  (Blender 5.3 renders it as Gaussian splats, `RENDER_AS_GSPLATS` with `radiance:base`, `scale`,
  `rotation`, `radiance:sh_*`): skipped with `blend.unsupported_geometry` (`GAUSSIAN_SPLAT`) rather
  than drawn as spheres. Houdini: `blender_assets._native_points` splits colours as on meshes and
  interns the arrays with `_native.blend_points(record, arrays, palette)` -> `capture_blend_points`
  (`blend_mesh.cpp`): a `PointData` carried by `MeshData::external_points` through the same
  external-mesh path as hair (the binding copies it per palette / field variant, `NativeSceneAdapter`
  publishes it in `update.points`). Engine: `PointData::generated_attribute` (false for Link points:
  Blender adds no Generated attribute to point clouds, so Cycles' kernel uses the object-space shading
  position, `svm_node_attr`'s fallback; Houdini points keep their bounds-normalized Generated); wire
  `worker_protocol_version` 81. Bounds: `blend_mesh_bounds` grows every point by its radius,
  `blend_bounds.evaluated_record` by `max_radius`; `blend_viewport` hulls the points grown by
  `max_radius` along the axes and skips empty records (the empty own mesh of a geometry-nodes object
  that outputs only points used to make the whole selection's hull fail). Not done or not exact:
  velocity motion blur (`attr_create_motion_from_velocity`, Blender's three motion steps from a
  `velocity` attribute) and deformation motion (`export_pointcloud_motion`): carriers skip
  `assemble_external_mesh_motion`, so the nominal pose renders and transform motion applies; the
  `velocity` attribute is still carried by name. Single-value (constant) attributes are stored per
  point (Blender: ATTR_ELEMENT_MESH, same values). Convert requests are unchanged: `points_geo` writes
  points with `pscale` from `radius` (no `pscale` when the point cloud has no radius attribute, so the
  Houdini render's point radius applies, not Blender's 0.01) and the attributes by name; QUATERNION,
  FLOAT4 and matrix point attributes are reported as skipped (meshes import them since U5). Convert &
  Append now names legacy particle hair it does not convert: `blend_export` adds a warning
  `blend.particle_hair` (emitters with render-enabled Hair/Path systems, also hidden emitters, which
  import nothing) and `blend_convert` lists it among the item's skipped entries and says it in the
  import log. Performance (3 million points, two attributes, Houdini 22.0.466 CPU): Blender export
  0.11 s offline, 114 MiB of arrays (40 bytes per point); Link import and a first 96 x 96 render 4.8 s,
  owned engine geometry 180 MB, device peak 221 MB, Houdini peak working set 1.26 GiB (10 million: 10.8 s,
  600 MB, 738 MB, 2.6 GiB). `tests/houdini_blend_sync_parity_smoke.py --points-performance N` repeats
  it. Offline proof `tests/pytest_cases/test_blend_pointcloud.py`; live
  `tests/houdini_blend_sync_parity_smoke.py` (fifth asset "Points", rendered with Blender's pixel filter).
- Radius of hair and point clouds (task L1, 2026-11-14; reference Blender v5.3.0
  `intern/cycles/blender/curves.cpp` `export_hair_curves`, `pointcloud.cpp` `export_pointcloud`,
  `source/blender/blenkernel/intern/type_conversions.cc`): Blender reads `radius` through
  `lookup<float>("radius", Point)`, which adapts a CURVE-domain value to its points and converts the
  type: FLOAT, INT, INT8, BOOLEAN as numbers; FLOAT2, FLOAT_VECTOR, FLOAT4, INT32_2D, INT16_2D as the
  mean of their components; FLOAT_COLOR and BYTE_COLOR as their luminance (the OCIO config's default
  luma coefficients). QUATERNION, FLOAT4X4 and STRING do not convert, so the default (0.005 hair, 0.01
  points) applies. Link records (`_hair_curves_record`, `_point_cloud_record`) and Convert & Append
  both use `blend_export._converter_radius`; the conversions were confirmed with Blender 5.3's own
  Cycles renders. Before task L1 Link read only a POINT FLOAT radius. Offline proof
  `tests/pytest_cases/test_blend_radius_lookup.py`; live `tests/houdini_blend_sync_parity_smoke.py`
  (seventh asset "Radii").
- Volume shaders on meshes in Link mode (task L1, 2026-11-14; reference: this repository's Cycles 5.3
  kernel, the same code Blender 5.3 renders with: `kernel/geom/shader_data.h`
  `shader_setup_from_volume` sets `prim = PRIM_NONE`; `kernel/geom/attribute.h` `find_attribute`
  then finds only MESH, VOXEL and OBJECT attributes; `kernel/svm/closure.h`
  `svm_node_principled_volume` multiplies density, colour and temperature by an attribute only when it
  is found; `kernel/svm/attribute.h` `svm_node_attr_volume` returns the Attribute node's Missing value,
  0, otherwise). Blender renders a Principled Volume on an ordinary mesh as fog of the Density input,
  unscaled by the missing `density`, coloured by Color, temperature unscaled, and an Attribute node
  in the volume part returns 0, also for a mesh that has a POINT (or FACE or CORNER) attribute of that
  name. Link mode renders the same, because since task L1 this is the core's only behaviour for
  every mesh medium (native, converted and Link alike: `prepare_shader_graph` keeps the medium's
  attribute lookups; see `docs/dev/notes/native-mesh-media.md`). An earlier version of this task carried a
  per-material flag for Link materials; it was removed when the behaviour became unconditional. Link
  meshes never get the native info note `material.mesh_volume_attribute`. Not covered: Blender's
  object-level attributes of the same name (`ATTR_ELEMENT_OBJECT`, e.g. an object custom property
  "density") and other volume recoveries (an empty Attribute name, UV-based volume textures) still
  differ; Volume Info is not translated by Link at all. Live proof
  `tests/houdini_blend_sync_parity_smoke.py` (sixth asset "Media", and the same cubes as native
  Houdini boxes).
- Axes and winding: Blender local data is multiplied by the exporter's `_CONVERT` (Z-up -> Y-up); a
  transform `C * M_world` becomes `C * M * C^-1`. Polygons are reversed keeping their first vertex (Blender
  counter-clockwise -> Houdini clockwise front faces); corner data follows.
- Identical geometry under different exporter keys (geometry nodes instances, collection instances) is
  de-duplicated by a content digest before packing; packed children are written once and frozen
  (`hou.Geometry.freeze(True)`) so the packed primitives share them.
- Unchanged-file detection uses a canonical content digest of the converted arrays, recorded per file in
  the asset record's `files` (bgeo files carry a creation date, so file bytes never match).
- Packed contents get a primitive `shop_materialpath` written into the embedded geometry (a Material SOP
  cannot reach inside packed primitives); unpacked polygons (hierarchy own geometry) use a Material
  SOP with one entry per `mat_*` group. Top-level polygons of a packed file, if any, get
  `shop_materialpath` in the file too (`_part_frames`), so the Blender File node needs no Material SOP.
- Materials and textures ([Textures in place](#textures-in-place)): the evaluation export runs with
  `material_textures="in_place"`: images on disk keep the resolved absolute path the .blend points to
  (forward slashes, `<UDIM>` for tiles) and are never copied; only packed images are staged.
  `build_import` extracts them with `blend_library_materials.place_packed_textures` into the `tex/` folder
  of the material's own blend folder (the library file for linked-library materials), once per content
  under the real file name (same name, other content: `_2`), records every image in that folder's JSON
  (in-place ones by path, for Collect Textures) and builds the material with `build_library_material`
  (root = the `tex/` folder, so ownership and sha256 checks stay; parms under the asset root use
  `$HIP/assets_blender/...`). A missing file is a warning (`material.missing_texture`) passed to `report`,
  and one `TEXTURES_IN_PLACE` line counts the textures left in the library and names Cycles > Collect
  Textures.
  Link marks materials with `mark_linked` (no `texture_root` any more; old nodes may still carry it) and
  reuses `importers.find_linked_material` matches.
- Frame sequences: frame data is spilled to `.npz` files (numbers only, no pickle) in the staging folder
  between prepare and build. `hierarchy` sequences keep the first frame's object transforms on the nodes
  and carry later object motion in the per-frame geometry.
- Node user data `cycles_blender_import` (JSON: source, selection item, options; files storage adds
  `manifest` = the `cycles_import.json` path, `blend_folder` and `asset_folder`, absolute with forward
  slashes) is set on the created top-level nodes; hierarchy objects carry `cycles_blender_object`.
- Asset browser: Append for objects/collections (`importers.append_snapshot` / `prepare_append`, then
  `import_asset(..., mode="append", prepared=payload)`) uses `structure="packed"`, `storage` from the
  browser setting `append_storage` (`"files"` default, or `"embedded"` = Stash SOP; `settings.APPEND_STORAGES`),
  `materials="append"` (Convert & Append copies everything). The panel runs it like a material import
  (background prepare, one undo group); before the prepare it asks Replace / Cancel when `shared_scenes`
  names other scenes (storage `"files"` only) and then passes `on_shared="replace"`.
- Library materials (`blend_library_materials.prepare_library_material(source, name, root=<asset root>)`)
  export with `textures="in_place"` and place packed images in `<root>/<blend of source>/tex/` the same
  way. The library-name and texture-location keywords and the texture-root functions are removed.

## Asset nodes: Blender File and Blender Hierarchy

Two digital assets carry every import (`blender/blend_file_node.py` holds all their Python; the assets'
PythonModule only forwards `hou.phm().on_parm(kwargs)` to `blend_file_node.on_parm`):

- **Cycles Blender File** `cycles::blender_file` (SOP, `otls/cycles_blender_file.hda`): one object or
  collection, Link or Converted, with its own Update.
- **Cycles Blender Hierarchy** `cycles::blender_hierarchy` (Object, `otls/cycles_blender_hierarchy.hda`):
  the importer's Hierarchy structure; contents unlocked and editable (definition options
  `setUnlockNewInstances(True)`, `setLockContents(False)`); its object transform moves the asset.

Both are generated by `scripts/create-blender-assets.py OUTPUT_FOLDER` (hython, inside the Cycles package
environment, because the SOP wraps the native `cycles::blender_reference`). Staging runs it in the
help-authoring block of `scripts/stage-development.ps1`; `scripts/runtime-assets.py` lists both outputs and
the script as a recipe input. The script verifies its outputs (parameters present, instance locked, Stash
link, Editable Nodes, hide-when table) and exits non-zero on any failure.

### Blender File parameters

Only the parameters of the current state are visible (`HIDE_WHEN`, checked against `visible_parms` for
every state by `tests/pytest_cases/test_blend_file_node.py`). "Source missing" cannot be expressed in a
hide-when condition; Update and Open in Blender hide only for an empty Blender File and report a missing
file when pressed.

| Token | Type | Label | Shown |
|---|---|---|---|
| `file` | file | Blender File | always |
| `kind` + `target` | menu `object`/`collection` + string with menu | Object / Collection, Name | always (one row) |
| `mode` | menu `link`/`converted` | Mode | always |
| `status` | label | Status | always |
| `frame` | float | Source Frame | Link |
| `viewport_shape` + `viewport_wireframe` | menu `bounds`/`hull` + toggle | Viewport Shape, Wireframe | Link (one row) |
| `shape_frame` | float | Shape Frame | Link |
| `max_hull_vertices` | int | Maximum Hull Vertices | Link |
| `refresh` | button | Refresh | Link |
| `load_as` + `unpack_iterations` | menu `packed`/`unpacked` + int (default 1) | Load As, Iterations | Converted; Iterations only Unpacked |
| `stash` | toggle | Stash in .hip | Converted |
| `frames_mode` + `frame_range` | menu `single`/`range` + int x3 (`frame_range1..3`) | Frames, start/end/step | Converted, not Stash; range only for Range |
| `apply_subdivision` | toggle | Apply Subdivision | Converted |
| `nested_instances` | toggle (default on) | Keep Nested Instances | Converted |
| `include_disabled` | toggle | Include Disabled Collections | Name is a collection |
| `part` | menu `base`, `subdiv`, `subdiv_2`.. (`""` = one part) | Part | Converted, more than one part |
| `update` + `open_blender` | buttons | Update, Open in Blender | Converted, Blender File set |
| `files` | label | Files | Converted, not Stash |
| `geometry_data` | geometry data, hidden | | never (the only hidden parameter) |

Inner nodes: `reference` (`cycles::blender_reference`, Editable Node) -> `source` switch input 0;
`converted_file` (File SOP, Editable Node) -> input 1; `stash` (Stash SOP, its `stash` parm linked with
`set(asset.parm("geometry_data"))`, i.e. `ch("../geometry_data")`) -> input 2; `unpack` (Unpack SOP:
`limit_iterations` 1, `iterations` = `ch("../unpack_iterations")`, `transfer_attributes` = `name path`);
`load_as_switch` (source / unpack) -> `output0`.

- The reference node's file, kind and target are written by Python (`push_reference`, on creation and in
  the `file`/`kind`/`target`/`mode`/`refresh` callbacks, followed by the matching `blend_catalogue` call
  with the inner node: `parameters_changed`, `selection_changed`, `viewport_changed`, `refresh_node`).
  Source Frame, the viewport parameters and Include Disabled reach it by channel reference. The `target`
  menu is `blend_catalogue.cached_menu_entries(inner, "ref_target1")`.
- The switches are Python expressions (`blend_file_node.source_index`, `unpack_index`) that read the
  node's parameters and user data only (never Blender, never a file): Link mode, and Converted mode with
  nothing converted yet, show the reference; Converted shows the file or stash the last Update wrote, so
  toggling Stash in .hip changes nothing until Update. Converted -> Link never deletes anything.
- The converted file path is written by Update into the Editable `converted_file` SOP (one mechanism,
  chosen because the names come from `cycles_import.json` and because File SOP scans
  (`_reload_file_sops`, Relink, Move into Project) then cover these nodes too). The Files label shows its
  folder. Changing Part retargets it to that part's file (`part_path`).
- Status (`node_status`): "Source missing", "Not converted. Press Update.", "Source changed",
  "Options changed. Press Update.", "Up to date", in that precedence (pure `status`). Link mode is "Up to
  date" unless the file is missing. The Status label is a backtick expression
  (`` `pythonexprs("...status_text()")` ``) evaluated when the parameter pane draws it (confirmed in
  Houdini 22.0.466), so it is checked when the pane shows the node and never during a cook; the
  network editor's Cycles menu computes it again when it opens.
- Signature: `blend_file_node.source_signature` = `file_signature.native_signature` (Windows), else
  modification time and size.
- Subdivision warning: `status_text` appends `subdivision_problem(node)` to the Status label,
  and Update reports it as an error line. For a Converted node whose record has a subdivision setting
  (or a `subdiv*` part from an earlier record): "merged" when the node reaches its object's render node
  (display node as fallback) through a node with more than one connected input (`upstream_of`), else
  "mismatch" when the object's `cycles_subdivision_override` is off or its type / render level differ
  from the part's (`subdivision_matches`). Both texts name the fix (own object, or Apply Subdivision +
  Update). Checked only when the label draws or Update runs, never in a cook. Not detected: geometry
  joined without a multi-input node on the way (e.g. an Object Merge feeding a Switch). On Update the
  object's subdivision is set only when the part is not merged in its object.
- Open in Blender (both nodes): `open_blender` starts Blender detached with
  `blend_worker.blender_environment(CYCLES_HOUDINI_BLENDER_OCIO)` (Houdini's OCIO and Python variables
  removed).

User data `cycles_blender_update` (JSON; the parameters are the record of the import):
`signature` (of the .blend when the import or Update read it), `time`, `storage` (`"files"`, `"stash"`,
or null for Link), `materials` (the mode new materials are imported in: `link` / `append` / `none`),
`options` (`options_fingerprint` of the conversion parameters, for "Options changed"), `part`,
`subdivision` (the part's render-time subdivision setting, null for none), and on the
hierarchy object `transforms` ({Blender object: 16 floats, the local transform the last Update wrote}).
The old `cycles_blender_import` user data is written only on the stock networks of the fallback.

### Blender Hierarchy parameters

A "Blender" folder before the standard object folders: `file`, `selection` (text `collection:Props
object:"Big Tree"`, `format_selection` / `parse_selection`) + `edit_selection` (Edit...), `frames_mode` +
`frame_range` (hidden with Stash), `stash`, `apply_subdivision`, `nested_instances`, `include_disabled`,
`status`, `update` + `open_blender`. Inside: what `_build_hierarchy` builds (collection null with user
data `cycles_blender_collection`, one geo/null object per Blender object with `cycles_blender_object`,
File / Stash SOPs marked `cycles_blender_source`, Material SOPs marked `cycles_blender_material`).
Edit... calls `blend_import_dialog.edit_selection(source, selection, *, include_disabled=False) ->
[(kind, name)] | None` (a modal `SelectionDialog` with the importer's tree, `_SelectionTree`; the listing
is read with Blender on a thread with a busy state; Cancel, an unreadable file or an empty choice return
None; items no longer in the file are reported and dropped on OK). A text prompt is used only when the
dialog module cannot be imported. A changed selection shows "Options changed. Press Update.".

### Creation API (`build_import`)

`build_import(payload)` creates, when `hou.nodeType` finds the assets (`blend_convert.asset_types()`):

| Snapshot | Result |
|---|---|
| `storage="reference"` | per item a geo object with a Blender File node in Link mode (`viewport_shape` option of `import_snapshot` sets its Viewport Shape); with `parent` a SOP network, the node itself in that network |
| `storage="files"` / `"embedded"`, `structure="packed"` | per item and subdivision part a geo object (`<item>`, `<item>_subdiv`, ...) with a Blender File node in Converted mode; Stash in .hip on for `"embedded"`. With `parent` a SOP network: the first part's node in that network (its object's Cycles settings untouched; a subdivided first part is reported), further parts in geo objects of the object network around it |
| `structure="hierarchy"` with `parent` a SOP network | the object network around it (`object_network`) |
| `structure="hierarchy"` | one Blender Hierarchy object (named after the single item, else the .blend stem) holding the hierarchy |

Without the assets (partially staged builds, tests) it builds the stock File / Stash / reference networks
and reports that the asset is not installed. A `structure` other than `"packed"` / `"hierarchy"` raises
`BlendImportError` (`MERGED_REMOVED` explains Load As for the structure of earlier builds).
The asset browser and the importer dialog both create through `build_import`
(`assets/importers._import_reference`, `_append_geometry`; dialog `HoudiniBackend.build`).

### Update (`blend_convert`)

```python
update_snapshot(nodes, *, on_shared="replace", report=None, **import_snapshot_options) -> snapshot   # host
prepare_update(snapshot, *, cancelled=None, evaluate=None) -> payload                              # any thread, no HOM
sync_import(payload, nodes=None, *, report=None, undo=True, reload_materials=None) -> [{"nodes", "created"}]
apply_update = sync_import                                                                           # host, one undo group
update_import(nodes, *, cancelled=None, report=None, on_shared="replace", reload_materials=None,
              evaluate=None, **import_snapshot_options)                                              # synchronous
```

- Grouping: Blender File nodes with the same source, asset, options and storage are one import; with files
  storage every other converted node reading the same asset's files joins it (subdivision parts, copies).
  Each hierarchy object is one import. Link-mode nodes are skipped (reported); a missing source raises.
- Blender File: files replaced under the layout rules (JSON, stale files, shared-scene rule through
  `on_shared`), the inner File SOP reloaded; Stash in .hip sets `geometry_data` and writes no geometry file
  (one frame); turning it off writes the file and clears the stash. A new subdivision part creates a sibling
  geo object + node (`<item><suffix>`, Load As copied); a part that vanished keeps its node (reported).
- Hierarchy: objects for new Blender objects are created (parented like Blender); objects whose Blender
  object is gone are kept and reported; a transform is rewritten only when it still equals the one the last
  Update wrote (else kept and reported); Material SOP entries: new `mat_*` groups added, vanished `mat_*`
  groups removed, other entries untouched; a source SOP of the wrong kind (stash toggled) is replaced and
  rewired; nodes the artist added are never touched.
- Materials: `_build_materials` reports the scene materials it reused; linked ones are reloaded through
  `reload_materials(nodes, report=...)` (default `assets.importers.reload_linked_async`, imported lazily;
  failure becomes an error line), editable ones are listed "not updated: editable", new ones are imported
  in the node's recorded mode.
- `blend_file_node.start_update(nodes)` is what the Update buttons and Update All call: Replace / Cancel
  when `shared_scenes` names other scenes, `prepare_update` on a thread with an event-loop poll in the UI
  (synchronous in hython), then `sync_import` and one message with the report.
- **Update All Blender Files** (network editor Cycles menu, `cycles_network_update_all_blender`):
  context `blend_file_node.can_update_all(kwargs)` (bridge enabled and a node whose status is "Source
  changed"), action `blend_file_node.update_all(kwargs)`.
- UI startup (`cycles_houdini_startup.install_ui`, from `uiready.py`) calls
  `cycles_houdini_startup.hide_native_reference()` -> `blend_file_node.hide_native_reference()`,
  guarded: it hides `cycles::blender_reference` from the Tab menu when the Blender File asset is installed.
  hython and other no-UI sessions do not hide it (no Tab menu there; `createNode` works either way), so
  old scenes and scripts are unaffected.

## Importer dialog (`blend_import_dialog.py`)

- Entry points: File > Import > Blender (.blend) (`addScriptItem` with parent `import_files`, after
  `h.import_gltf`, in `houdini/MainMenuCommon.xml`; the ids are `FILE_IMPORT_MENU_ID` /
  `FILE_IMPORT_INSERT_AFTER` in the dialog module), Cycles > Import Blender (.blend)..., and the
  **Import .blend** tool on the Cycles Tools shelf. The menu items carry the one-line context
  `__import__("cycles_houdini.core.blender_bridge", fromlist=["enabled"]).enabled()` (evaluated once at
  startup); the shelf tool and `open_dialog()` show a message instead when the bridge is disabled.
  `open_dialog()` keeps one instance, parented to `hou.qt.mainWindow()`.
- Every HOM, Blender and converter call goes through `HoudiniBackend` (GUI thread except
  `resolve_blender`, `list_contents` and `prepare`). Tests replace it.
- The dialog calls `prepare_import(source, selection, **kwargs)` on a background thread with
  `storage, structure, materials, frames, include_disabled, apply_subdivision, nested_instances,
  asset_root, executable` (and `on_shared="replace"` after the user chose Replace) plus `cancelled` (a `threading.Event`) and `report` (`callable(text, error=False)`, thread-safe; the
  dialog shows the text). Keywords the converter's signature does not accept are left out
  (`accepted_kwargs`). `asset_root` is passed unexpanded (default `$HIP/assets_blender`), so a
  background `prepare_import` must not call HOM to expand it. `build_import(payload)` runs on the GUI
  thread; the returned nodes are selected and their paths shown. `parent`/`position` are not passed
  (converter defaults).
- Selection: a checked collection is one `("collection", name)` item; its objects and (included, or all
  with Include Disabled Collections) child collections are shown checked and disabled and are not sent
  again. Objects are `("object", name)` items. Default: the active scene's top-level collections and the
  objects linked directly to the scene.
- Controls: **Mode** Link / Convert & Append (`mode` = `"link"` | `"convert"`) and **Save Location**
  Save to Disk as .bgeo.sc / Stash in .hip (`save_location` = `"files"` | `"embedded"`); `storage_for`
  maps them to the converter's `storage` (`"reference"`, `"files"`, `"embedded"`). Controls that do not
  apply are hidden with their row label: Link shows only the selection and Include Disabled Collections;
  Frames shows only with Save to Disk. **Structure** (`STRUCTURE_CHOICES`): **Without hierarchy**
  (`"packed"`, default: a Blender File node per item) / **Hierarchy** (`"hierarchy"`: one Blender Hierarchy
  object). Structure is hidden with Mode Link (Hierarchy implies Convert & Append) and `converter_arguments`
  sends `"packed"` for Link. There is no Textures option.
- Shared scenes ([Import record](#import-record)): for storage `"files"`, `start_import` calls `backend.shared_scenes(source,
  selection, kwargs)` (`blend_convert.shared_scenes` with `asset_root` and `structure`) on the GUI thread
  before the background prepare; when it names other scenes, `backend.ask_replace(question)` (Replace /
  Cancel) decides between `on_shared="replace"` and not importing.
- After the build, the converter's `payload["build_messages"]` (missing textures, textures in place) are
  appended to the status line and its tooltip.
- `frames` is `(start, end, step)` only for `storage="files"` with Frame range (prefilled from the file);
  otherwise `None`.
- Last-used options are stored in `blend_import_settings.json` beside the asset browser settings
  (`CYCLES_HOUDINI_BLEND_IMPORT_SETTINGS_FILE` overrides): `mode`, `save_location`, ... Files of earlier
  builds load without error: `storage` maps to Mode and Save Location, the structure of earlier builds
  that no longer exists becomes `"packed"`, the old default asset folder becomes `$HIP/assets_blender`, and
  `texture_location` is dropped. A script passing that structure to `converter_arguments` gets a
  `ValueError` with `MERGED_REMOVED`.

## Worlds (`blend_worlds.py`, `blend_world_export.py`)

Blender side: `blend_asset_scan.export_named_worlds(path, names, output_dir,
textures="in_place")` appends each World and calls `blend_world_export.export_world`, which first evaluates
drivers (the world is the scene world for one depsgraph update, so Poly Haven's custom-property sliders
apply) and classifies it. Worker command `export_library_worlds` (host:
`blend_worker.export_library_worlds(source, names, output_dir, executable, timeout=300, cancelled=None,
textures="in_place", source_config=None)`, disposable worker, signature checked before and after, errors per
name, records validated by `_checked_world`).

Record: `{name, key ("blender-world-..."), kind, reasons: [{rule, message}], warnings: [text],
visibility: {camera, diffuse, glossy, transmission, scatter, shadow}, color_management, dependencies}` plus

- `kind: "simple"`: `simple = {image (the material exporter's in_place image record), strength, tint [r, g, b]
  (Blender scene-linear), rotation_z (radians)}`;
- `kind: "material"`: `material` = a material-shaped record (outputs `surface`, `volume` from the World
  Output; a node-less world becomes one Background with World Color);
- `kind: "unsupported"`: `unsupported` = the exporter's reason.

Simple rules (`blend_world_export.SIMPLE_RULES`; every failed one is listed in `reasons`):
`nodes`, `output` (one active World Output, All or Cycles), `volume` (nothing on Volume), `background`
(Surface from a Background node), `strength` (unconnected), `color` (Environment Texture Color, or that times
one constant: Mix Color/Multiply factor 1 no clamp, legacy MixRGB Multiply factor 1 no clamp, or Vector Math
Multiply; the constant is an unconnected socket or an RGB node), `texture` (equirectangular, Linear, a FILE
image, not multiview), `vector` (Vector unconnected, Texture Coordinate Generated, or Generated through one
Mapping; an unconnected Mapping Vector is a constant 0 in Cycles), `mapping` (Point, location 0, scale 1,
rotation X = Y = 0, nothing connected to them), `visibility` (all world ray visibility on). Reroutes and
muted nodes are followed.

Host: `prepare_world(source, name, *, root, executable, timeout=300, cancelled=None, source_config=None)`
(background thread) exports, raises `BlendWorldError` (a `BlendMaterialError`: content verdict) or
`LibraryMaterialUnavailable` (transport), and extracts packed images to `<root>/<blend>/tex`
(`place_packed_textures`). `build_world(prepared, *, mode="link"|"append",
existing_environment="replace"|"keep", diagnostics=None, link_info=None)` (host thread, one undo group)
returns `{node, reused, route ("light"|"material"), report}`.

- Simple: `envlight` in /obj named after the world (`node_name`, `free_child_name`), `light_properties.install`,
  `light_contribprimary=1`, `env_map` in place (a packed image: `tex/` file, `$HIP/...` form when the asset
  root text expands to it), `light_intensity` = Strength, `light_exposure` 0, `light_color` = tint through
  Blender working -> Houdini working, `cycles_environment_colorspace` = `data` for Non-Color, else the Houdini
  OCIO space with the same interop id (`Auto` + warning `world.colorspace` when none), rotation
  `light_rotation_for_blender_mapping(z) = (0, -90 - degrees(z), 0)` (wrapped to [-180, 180)). Missing HDRI:
  warning `material.missing_texture`, the light is still created.
- Other: `blend_materials.import_world_material` builds a `cycles::world_material::1` in /mat. World mode of
  `_Builder`: Background and Sky Texture are accepted (world only), only nodes with `world_graph_adapter`;
  unconnected inputs whose native default is the position or Generated (Environment/Sky Texture Vector,
  procedural textures) are wired from Geometry Position converted to Blender axes; Texture Coordinate
  Generated/Object/Normal/Reflection are converted the same way; Background Color gets the closure colour
  conversion. World ray visibility is not imported (reported).
- Link: `mark_linked` with `{kind: "world", route, source, name, signature, asset_root, ...link_info}`;
  every parameter locked except `light_enable`; reused by `find_linked_world(source, name)`. Every imported
  world carries user data `cycles_blender_world` = `{source, name, route}` (`world_info`).
- Existing environment (Replace / Keep existing): `scene_facts()` (Cycles ROPs = `cycles_ipr` instances with `world_source`, resolved `world_material`
  and the enabled Environment Lights their light patterns select; every enabled `envlight` under /obj),
  `environment_state(facts)` -> `{active, kind, lights, rops}`, `activation_plan(kind, state, choice,
  new_path=)` -> `{enable, disable, rops: [{path, set}], report}`, `apply_plan(plan)` (locked parameters are
  unlocked for the change and locked again). Replace, light: enable it, disable every other enabled
  Environment Light, ROPs not in Scene mode -> Scene. Replace, World Material: `world_source = material`,
  `world_material` on every Cycles ROP; lights untouched. Keep: a new light is disabled, a World Material is
  set on no ROP. Nothing active: Keep acts as Replace. `existing_environment(source, name, mode=)` lists what
  to name in the Replace / Keep existing / Cancel question (the reused linked world excluded).
- Reload: `reload_request(node)` -> `{path, source, name, root}`; `rebuild_linked_world(node, prepared)`
  updates a light in place (name and Enable kept) or swaps a World Material under the same name; a world
  whose route changed is refused. `reload_linked_world(node, *, prepared=None, executable=None)` is the
  synchronous form.

Asset browser hookup: a WORLD record imports through `importers.import_asset(record, mode=, prepared=,
existing_environment=)`. The panel (`AssetPanel.start_world`) first asks, on the GUI thread,
`importers.existing_environment(record, mode)` (the environment lighting a new world would compete with,
the reused linked world excluded); when it names anything it shows Replace / Keep existing / Cancel
(`ask_existing_environment`) and passes `"replace"` or `"keep"`; Cancel starts nothing. Link with an
existing shared node of (source, world) reuses it at once without Blender. Otherwise `world_snapshot`
(host) -> `prepare_world` (thread; packed HDRIs extracted to `<asset root>/<blend>/tex`) ->
`_import_world` -> `blend_worlds.build_world` (host, inside the import's undo group), which also applies
the activation plan. Worlds land in /obj (Environment Light) or /mat (World Material) wherever they are
dropped. Reload from Library and Make Editable act on linked worlds as on materials (`reload_snapshot` /
`_rebuild_world`, `append_linked`); Collect, Relink and Move into Project cover `env_map` and the World
Material's texture nodes.

Orientation gate: `tests/pytest_cases/test_blend_worlds.py` checks the rotation against scene/world.py's own
sampling for marks at +X, -X, +Y, -Y and the zenith at 0, 90 and 215 degrees;
`tests/fixtures/blend_worlds/orientation_reference.py` renders Blender's half (run by
`test_blend_world_export.py` with bpy); `tests/houdini_blend_world_orientation_smoke.py` renders Houdini's.


## Material node settings (`blend_material_export._properties`, `blend_materials._Builder.properties`)

The exporter carries every RNA setting of an exported shader node (types BOOLEAN, INT, FLOAT,
STRING, ENUM; `node_settings`), derived from the node's RNA rather than a list of names, so a
setting Blender adds reaches the importer instead of being dropped. The importer maps each setting to
a native parameter (name aliases, then `_ENUM_TOKENS` where Blender's enum item is another word than
the native token, else a match by name), consumes it (Mix and Map Range data type, which picks
`vector_map_range` in `node_kind`; Attribute type), lists it in `UNREAD_SETTINGS` (MixRGB Use Alpha:
Blender's Cycles reads only the blend type and clamp), or fails naming it (`blender.material.unsupported`).
Units: the Sky Texture's Sun Elevation, Size and Rotation are radians in Blender and degrees on the
native node (`_DEGREE_SETTINGS`). The native Mix has three ports (`fac`, `a`, `b`) for every data type;
`parameter_name` picks `a_float` / `a_vector` / `fac_vector` for an unconnected value.

Refused by name (no native equivalent in the pinned Cycles or no Houdini source): Attribute type View
Layer and Light, Vector Transform Light space, From Instancer on Texture Coordinate and UV Map. Node
types without an import: see `EXPECTED_UNIMPORTED_TYPES` in the coverage test (Particle Info has no
graph adapter in the engine catalogue). Every Blender input socket has a native counterpart except
the unavailable Weight of shader nodes (Cycles' mix weight is internal to the engine graph) and
Attribute / Vector Transform LightIndex (the pinned Cycles has no light attributes or light space);
Fresnel and Layer Weight have a Normal port (after IOR / Blend; unconnected is the shading normal).

Gates: `tests/houdini_blend_node_coverage_smoke.py` (Blender lists every shader node type, setting
and enum item; each is built through `_Builder`; fails on a dropped or unmapped setting, a value that
does not land, an available input that cannot take a wire, two enum items sharing a token, or a stale
list entry) and
`tests/houdini_blend_node_settings_parity_smoke.py` (Link-mode renders against Blender 5.3's Cycles
for the settings that change pixels and for Fresnel / Layer Weight with a Normal Map or Bump wired
into Normal, with control renders that revert each setting or remove the wire and must miss).
