# Asset browser: implementation contract

Status: implemented 2026-11-12; Houdini/Windows verification pending (see
"As implemented" at the end). Source research: an internal research note of
2026-11-12 (not published).

A dockable Python panel that browses Blender asset libraries as they are on
disk, using Blender (through the existing headless worker) to read `.blend`
files, and Houdini-side caches for metadata and thumbnails. The library folder
is never written to unless the artist explicitly runs the official listing
generator (option B) and confirms it.

`PKG` = `houdini/python3.13libs/cycles_houdini`.

## Verified facts (Blender 5.3 Python module, 2026-11-12)

- `bpy.data.libraries.load(path, link=True, assets_only=True)` lists only
  marked assets. Linked IDs keep `asset_data` (tags, description, author,
  `catalog_id`, `catalog_simple_name`, license, copyright) and their stored
  `preview` (`image_size`, `image_pixels`, `image_pixels_float`).
- Unassigned assets have `catalog_id == "00000000-0000-0000-0000-000000000000"`.
- `preview.image_pixels` is packed RGBA8 per int; `array("i", ...).tobytes()`
  on little-endian gives R,G,B,A byte order. Rows are bottom-up (Blender image
  convention): flip before writing a PNG.
- Objects/collections often have no stored preview (`preview is None`): the
  panel must show a placeholder.

## Module ownership

| Path | Owner | Notes |
|---|---|---|
| `PKG/assets/__init__.py`, `catalogs.py`, `libraries.py`, `records.py`, `cache.py`, `providers.py`, `indexer.py` | core | Pure Python. No `hou`, no Qt. |
| `blender/cycles_blender/blend_asset_scan.py` and `worker_server.py` (Blender component, GPL-3.0-or-later; run inside Blender), additions to `PKG/blender/blend_worker.py`, `PKG/blender/blend_library_materials.py` | worker | Worker commands and the material import entry point. |
| `PKG/assets/panel.py`, `widgets.py`, `importers.py`, `drag.py`, `settings.py` | panel | Qt + HOM. |
| `houdini/addons/asset_browser/**`, installer, staging, release package, user docs | packaging | Installer component and gating. |

Do not edit files owned by another package. If an interface below is
insufficient, keep the stated signature working and add an optional keyword.

## Record shape (internal cache format = Blender remote listing v1 asset entry + `ch` block)

```python
record = {
    "name": "Metal - Galvanized",            # datablock name
    "id_type": "MATERIAL",                   # Blender listing spelling: MATERIAL, OBJECT, COLLECTION, WORLD, NODETREE, ...
    "files": ["materials/metal.blend"],      # POSIX, relative to the library root
    "thumbnail": {"url": "<absolute PNG path in the Houdini cache>", "hash": "<sha256 hex or ''>"} or None,
    "meta": {
        "catalog_id": "uuid" or "",          # "" for unassigned (null UUID normalised to "")
        "tags": ["metal"],
        "author": "", "description": "", "license": "", "copyright": "",
    },
    "ch": {
        "key": "materials/metal.blend|MATERIAL|Metal - Galvanized",   # stable identity
        "source": "<absolute .blend path>",
        "catalog_simple_name": "",
        "object_type": "MESH" or None,       # OBJECT only
        "importable": {"state": "yes" | "no" | "unknown", "reason": ""},
        "provider": "worker" | "listing",
    },
}
```

Helpers live in `PKG/assets/records.py`: `make_record(...)`, `record_key(record)`,
`validate_record(record) -> bool`, `ID_TYPES`, `SUPPORTED_IMPORT_TYPES = ("MATERIAL", "OBJECT", "COLLECTION")`.

## Core interfaces (`PKG/assets`, pure Python)

```python
# __init__.py
def enabled() -> bool        # env CYCLES_HOUDINI_ASSET_BROWSER; unset -> True (development); "0/off/false/disabled" -> False

# catalogs.py
CATALOG_FILENAME = "blender_assets.cats.txt"
NULL_UUID = "00000000-0000-0000-0000-000000000000"
class CatalogNode: path: str; name: str; uuids: list[str]; simple_names: list[str]; children: list["CatalogNode"]
class CatalogTree:
    root: CatalogNode; warnings: list[str]
    def node_for_uuid(self, uuid) -> CatalogNode | None
    def subtree_uuids(self, path: str) -> set[str]     # node + descendants; "" = everything
def parse_catalog_text(text: str) -> CatalogTree
def load_catalogs(library_root) -> CatalogTree         # missing file -> empty tree, no error

# libraries.py
class Library: name: str; path: str; enabled: bool = True; import_method: str = "APPEND"; provider: str = "worker"
def registry_path() -> Path    # CYCLES_HOUDINI_ASSET_LIBRARIES_FILE, else <HOUDINI_USER_PREF_DIR>/cycles_houdini/asset_libraries.json, else ~/.cycles-houdini/asset_libraries.json
def load_libraries(path=None) -> list[Library]
def save_libraries(libraries, path=None) -> None   # atomic replace
def merge_blender_libraries(existing, dumped: list[dict]) -> list[Library]   # dedupe by normcase(abs path); keeps existing names/flags

# cache.py
def cache_root() -> Path       # CYCLES_HOUDINI_ASSET_CACHE, else <HOUDINI_USER_PREF_DIR>/cycles_houdini/asset_cache, else ~/.cycles-houdini/asset_cache
class IndexCache:
    def __init__(self, library, root=None)
    thumbnails_dir: Path        # created on demand
    def get(self, blend_path, signature) -> list[dict] | None
    def put(self, blend_path, signature, records, error=None) -> None
    def errors(self) -> dict[str, str]
    def all_records(self) -> list[dict]
    def prune(self, existing_blend_paths) -> None
    def set_importability(self, key, state, reason) -> None   # survives until that file's signature changes

# providers.py
class ScanResult: records: list[dict]; errors: list[dict]; scanned: int; reused: int; blender_missing: bool; message: str
class AssetProvider:            # base class
    kind: str
    def __init__(self, library, cache=None)
    def catalogs(self) -> CatalogTree
    def items(self) -> list[dict]                            # cached records, cheap, never launches Blender
    def scan(self, *, cancelled=None, progress=None) -> ScanResult   # blocking; background thread; never calls hou
class BlenderWorkerProvider(AssetProvider): kind = "worker"   # option A, default
class BlenderListingProvider(AssetProvider): kind = "listing" # option B: reads an existing _asset-library-meta.json / _v1
    def generate_listing(self, executable, *, timeout=600) -> None   # explicit, writes INTO the library; caller must confirm
def make_provider(library, kind=None) -> AssetProvider
PROVIDER_KINDS = ("worker", "listing")

# indexer.py   (thread job; results drained by the panel's QTimer on the host thread)
class IndexJob:
    def __init__(self, provider)
    def start(self) -> None
    def cancel(self) -> None
    def poll(self) -> dict | None   # {"state": "running"|"done"|"failed"|"cancelled", "progress": (done, total), "result": ScanResult|None, "error": str}
```

`BlenderWorkerProvider.scan` resolves Blender with
`core.blender_bridge` (respecting `enabled()` and `require_executable`) and
calls `blend_worker.assets_many` in batches of at most 32 files. A file is
re-read only when `blender.file_signature.native_signature(path)` changed.
When Blender is missing, `scan` returns `blender_missing=True` and the
cached records and catalogs remain usable.

## Worker interfaces (`PKG/blender`)

```python
# blend_worker.py (host side; disposable worker like catalogue_many)
def assets_many(sources, executable, thumbnail_dir, timeout=120, cancelled=None) -> list[dict]
# per source, in order:
#   {"source": abs, "signature": <JSON-able signature>, "assets": [
#       {"name", "id_type", "meta": {catalog_id, tags, author, description, license, copyright},
#        "catalog_simple_name", "object_type": str|None,
#        "preview": "<file name inside thumbnail_dir>" | None, "preview_size": [w, h] | None}]}
#   or {"source": abs, "error": "message"}
def export_library_materials(source, names, output_dir, executable, timeout=300, cancelled=None) -> dict
#   {"records": [material records from blend_material_export.export_materials], "errors": {name: message}}
def dump_asset_library_preferences(executable, timeout=60) -> list[dict]   # non-factory run; [{name, path, import_method, enabled}]

# blend_library_materials.py (host side, HOM)
def import_library_material(source, name, *, parent=None, executable=None, timeout=300, library_root=None,
                            source_config=None, diagnostics=None) -> hou.Node
#   Exports in the worker with textures="in_place" (textures stay where the .blend points; packed images are
#   extracted to <asset root>/<blend>/tex), imports with blend_materials.import_materials(..., parent=parent or
#   /mat) with undo ENABLED (caller wraps in an undo group). `diagnostics` receives material.missing_texture.
#   Raises blend_materials.BlendMaterialError with the specific reason on unsupported content.
def asset_root_text(text="$HIP/assets_blender") -> (str, str)   # (root, notice); untitled scene -> <cache>/assets_blender
def resolve_asset_root(text, source, library_root=None) -> Path   # refuses the .blend's folder and the library
```

Worker commands added to `_serve()` (`blender/cycles_blender/worker_server.py`): `assets_many`, `export_library_materials`.
`catalogue_many` and `blend_catalogue._valid_record` stay unchanged.

### Material node layout (Blender editor → VOP network)

`blend_material_export` adds to each node record `layout` = [x, y] (top-left corner in
Blender view units, absolute: `location_absolute`, or `location` summed up the frame
`parent` chain before 4.4) and `width`; the record gets `output_layout` (Material Output)
and optional `frames` ([{id, name, label, color|None, parent, nodes, rect=[l, b, r, t]}],
only frames holding an exported node; `nodes` may contain `"output"`). A flattened group's
internals keep their relative layout as one block: right edge on the group node's right
edge, centred on it vertically, nested groups first; items of the containing tree left of
the group node move left by (block width − group node width). `blend_materials._Layout`
scales x and y by 0.01 (raised up to 0.025 if a VOP tile is wider relative to its Blender
node; no y flip), drops nodes that overlap in a column below each other, puts builder-owned
helpers (colour conversions, group defaults, vertex colour alpha) in columns just left of
the node they feed (Blender-placed nodes further left make room) and turns frames into
(nested) network boxes. Records without `layout` (old caches, default surface) keep
`layoutChildren()`. Linked reloads rebuild the same layout from the re-exported record.

## Panel (`PKG/assets/panel.py`)

`AssetBrowserPanel(QWidget)` with `dispose()`, returned by
`houdini/addons/asset_browser/python_panels/cycles_assets.pypanel`.
Blender-like three columns: library dropdown + catalog tree (with "All" and
"Unassigned"), thumbnail grid (`QListView` icon mode over a custom model,
thumbnails loaded lazily), details panel (name, type, source file, catalog,
description, author, license, copyright, tags, importability). Header: import
mode, type filter, search (names and tags), thumbnail size, refresh. `T` and
`N` toggle the side panels. Read-only toward the library; "Open Blend File"
launches Blender on the source file.

Import actions (button, double-click, context menu), each one undo group:
objects/collections -> a Cycles Blender File node (`cycles::blender_file`) created by
`blend_convert.build_import`: Link -> Link mode, Convert & Append -> Converted mode (see
`blend-import.md`), in a new geometry object or, dropped into a SOP network, directly there; materials -> `/mat` via
`import_library_material`; "Assign to selected objects". Drag and drop is an
experimental setting (default off) behind `drag.begin_drag(record)`; the route
is untested in Houdini 22.

## Installer and gating

New optional Setup component `blenderbridge\assetbrowser` ("Asset browser",
child of the Blender bridge, selected in the Full type). The release package
tool takes `--asset-browser enabled|disabled`, records
`components.asset_browser` (legacy receipts: `false`), writes
`CYCLES_HOUDINI_ASSET_BROWSER=1|0`, and appends
`<host>/addons/asset_browser` to the package `hpath` only when enabled, so the
pane tab and shelf exist only for installations that chose it. Disabling the
bridge disables the asset browser.

## As implemented (differences from the contract above)

- Material import is two-phase so Houdini never waits on Blender:
  `importers.material_snapshot` (host: `importers.asset_root` = `asset_root_text` + `resolve_asset_root`) ->
  `importers.prepare_material` / `prepare_library_material` (background thread: worker export with
  `textures="in_place"`; only packed images are extracted, once per content, to `<asset root>/<blend>/tex/`)
  -> `build_library_material(prepared, parent=, diagnostics=)` (host, one undo group). Without `prepared`,
  `import_asset` runs the same three steps synchronously.
- Import report: `import_asset(..., report=)`, `assign_material(..., report=)` and
  `rebuild_linked(pairs, report)` call `report(text, warning)`; default Houdini's status bar. Material
  imports report each `material.missing_texture` as a warning (never a failure) and one
  `blend_convert.TEXTURES_IN_PLACE` line ("Textures stay in place: N texture file(s) ... Cycles > Collect
  Textures copies them into the project."); Convert & Append passes `report` to `build_import`, which reports
  the same. The panel shows the lines in the status (warning colour when any is a warning) and its tooltip.
- Shared scenes ([Import record](blend-import.md#import-record)): before the background prepare of a Convert & Append, the panel calls
  `backend.shared_scenes(snapshot)` (= `importers.append_shared_scenes`: `blend_convert.shared_scenes` for
  storage `"files"`, `[]` for `"embedded"`). Non-empty: `panel.ask_replace_shared(importers.shared_question(...))`
  (Replace / Cancel, GUI thread); Replace sets `snapshot["on_shared"] = "replace"`, Cancel starts nothing.
- Material export uses the same OCIO source config as reference evaluation
  (`CYCLES_HOUDINI_BLENDER_OCIO`, `export_library_materials(..., source_config=)`).
- Every Blender process the bridge starts gets `blend_worker.blender_environment(source_config)`: the
  process environment without Houdini's `OCIO`, `PYTHONPATH` and `PYTHONHOME`, and `OCIO` =
  `CYCLES_HOUDINI_BLENDER_OCIO` when set. Workers, listings, Open Blend File (`CoreBackend.launch_blender`) and
  Open in Blender on the Blender File / Hierarchy nodes (`blend_file_node.open_blender`, detached) all use it,
  so a file saved in that Blender keeps Blender's colour space names.
- `library_root`: the library folder an asset was imported from (`_import_options()["library_root"]`). It is
  recorded in the link data and in the JSON image records (`library_root` + `relative_path`) and is what
  Relink tries first after a library moved; nothing is ever written under it (`resolve_asset_root` refuses).
- `LibraryMaterialUnavailable(BlendMaterialError)` marks infrastructure failures
  (Blender missing, timeout, cancel, disk). Only content failures set the
  importability badge to "no".
- Texture location "hip" falls back to the shared cache while the scene is
  untitled, with a status-line notice.
- `IndexCache(namespace=...)` keeps worker and listing records apart; the
  worker signature is the stat tuple (`blend_worker._signature`), the provider
  cache key is `file_signature.native_signature` with a stat fallback.
- Every `assets_only` type is listed (5.3 also lists marked images); types
  outside MATERIAL/OBJECT/COLLECTION are shown with a "cannot import" badge.
- Option B: "Generate Blender Listing..." (confirmation required) runs
  `blender -b --factory-startup -c asset_listing generate <library>`, which
  writes `_asset-library-meta.json`, `_v1/` and `<file>_thumbnails/` into the
  library. Not cancellable.
- Assets whose catalog UUID is missing from `blender_assets.cats.txt` are shown
  under "Unassigned".

Verified in this repository snapshot without Houdini: pure Python and offscreen
Qt tests; Blender 5.3 (bpy module) for scanning, previews, cleanup, material
export, the framed worker protocol and the official listing reader. Not run:
Houdini 22 (panel inside Houdini, HOM calls, importers, drag), Windows Blender
executable, Inno Setup, PowerShell. Manual checks: `tests/houdini_asset_browser_smoke.py`
(hython) and `scripts/test-installer.ps1`.

## Design language (shared with the render viewer)

The asset browser uses the render viewer's theme machinery rather than its own
colours: `viewer.panel_theme.PanelTheme` reads Houdini 22 theme roles
(`viewer_theme.read_theme`, legacy colour-scheme names as fallback), applies
the viewer's chrome stylesheet scoped to the panel
(`viewer_layout.scoped_style("cycles_asset_panel")`) plus asset-only rules in the
same `$role` vocabulary (`assets.widgets.asset_style`), scales metrics with the
monitor and Houdini's global UI scale (`viewer_chrome.apply_dimensions`), and
re-tints glyphs (`viewer_theme.tint_glyphs`). It re-applies when Edit Theme
replaces the application palette. Toolbar controls are the viewer's
`ToolbarRow`, `cluster`, `ViewerCombo` and `ViewerToolButton`. New Cycles panels
should use `panel_theme.install(panel, "<scope>")` the same way.

## Linked materials and menus (Nov 2026 amendment)

- `importers.valid_modes`: `MATERIAL`/`OBJECT`/`COLLECTION -> ("link", "append")`; labels
  `importers.IMPORT_MODES` = "Link (reference)" / "Convert & Append" (objects: converted geometry,
  `blend-import.md`; settings key `append_storage` = `"files"` | `"embedded"`, Options menu **Save Location** >
  **Save to Disk as .bgeo.sc** / **Stash in .hip** (`settings.SAVE_LOCATION_LABELS`), passed by `start_append` as
  `storage=`). The `texture_location` setting and Options > Material Textures are gone; old settings files
  load and drop it. The header
  dropdown alone picks the mode, and the Import button never shows it.
- Objects and collections: Link runs `importers._import_reference` = `blend_convert.build_import(
  blend_convert.prepare_import(blend_convert.import_snapshot(source, [(kind, name)], storage="reference",
  parent=, position=, viewport_shape=<Options > Imported Reference Viewport>)), undo=False, report=)` inside the
  import's undo group (no Blender is started for Link), then marks the created nodes with `cycles_asset_key`.
  The result is `[geo object, Blender File node]`, or `[Blender File node]` when `network` is a SOP network (a
  drop). Convert & Append runs `append_snapshot` (storage from Save Location: `"files"` or `"embedded"` = Stash
  in .hip) -> `prepare_append` (thread) -> `build_import`; a drop into a SOP network puts the first part's node
  there and further subdivision parts in geo objects of their own in the object network around it. Without the
  node assets installed, `build_import`'s own fallback creates the stock networks (a plain
  `cycles::blender_reference` SOP for Link) and reports it.
- Link: `import_asset(record, mode="link")` and `assign_material(..., mode="link")` reuse the node
  `find_linked_material(record)` (a `/mat` child whose user data `cycles_asset_link` names the same source and
  material). Only without one does the panel run the two-phase export and build.
  `blend_library_materials.mark_linked` writes `cycles_asset_link` = JSON {source, name, library_root,
  signature, key, locked_before}. Links of earlier builds may also carry `library` and `texture_root`: read
  without error, ignored, and dropped on the next reload. It locks every parm of the builder and its descendants
  (`parm.lock(True)`), sets the comment "Linked from <file>: <material>" and turns on `nodeFlag.DisplayComment`
  (guarded). `unmark_linked` is the exact inverse: parms listed in `locked_before` stay locked.
- Reload: `reload_snapshot(node)` (host) -> `prepare_material` (thread) -> `rebuild_linked([(node, prepared)])`
  (host, one undo group). The rebuild builds under a unique name beside the old node, destroys the old node, and
  then takes over its name and position. Object assignments are path strings, so they survive. Plan 5.2:
  `collect.texture_values(old)` is read before the old node is destroyed and
  `collect.reapply_after_reload(new, previous)` runs after `mark_linked(new, info)`; collected copies are kept,
  re-collected when the library file changed, or kept with the warning `material.library_texture_missing`.
  `reload_linked_material` and `reload_all_linked` are synchronous. `reload_linked_async` runs the export on a
  thread and rebuilds from `hou.ui.addEventLoopCallback`; without a UI it runs synchronously.
  `linked_status(record)` returns `{path, stale}` from one stat of the source file.
- Make Editable (menu label since Nov 13; formerly "Append"): `importers.append_linked(node)` / `menus.append_selected(kwargs)`, one undo group.
- Wiring inside a non-HDA subnet cannot be prevented. Locked parms are the practical read-only state, and a
  reload discards local wiring changes.
- Menus ([Host menus](blend-import.md#host-menus)): the node right-click submenu (`houdini/OPmenu.xml`, `cycles_asset_link_menu`) and
  the network editor menu bar entry (`houdini/NetworkViewMenu.xml`, `cycles_network_view_menu`) are host files,
  staged with `MainMenuCommon.xml` by `scripts/stage-development.ps1` (so they reach the host runtime and the
  release package). The add-on keeps only Cycles > Asset Browser. Houdini 22 ignores a `<context>` on a menu-bar
  entry (checked: the expression is never called), so the network menu is present in every network editor,
  also with the Blender bridge disabled; every item's context starts with `menus.bridge_enabled()` and then
  checks the network shown (`_network_kind(kwargs)`: `vop` for `/mat`-like networks, `obj`, `sop`):
  Collect for Selected/All in VOP and OBJ networks, Relink and Move into Project in VOP, OBJ and SOP networks,
  Make Selected Editable / Reload in material networks with linked materials (`network_has_linked`,
  `network_any_linked`). The OPmenu submenu context is `menus.node_menu(kwargs)` (bridge enabled and the clicked
  or selected nodes include a builder, texture node or Environment Light, or a linked material). Item contexts
  are evaluated each time the menu opens, with kwargs `editor`, `altclick`, `ctrlclick`, `shiftclick`,
  `cmdclick`. All context expressions are one-line Python *expressions* (Houdini evaluates them in eval mode, so
  no `return`) that call `PKG/assets/menus.py`, which guards every HOM call and returns False on error.
  `linked_candidates` also counts the network shown (`kwargs['editor'].pwd()`), so the actions work from
  inside a linked material. **Update All Blender Files** (`cycles_network_update_all_blender`) is the
  first item of the Blender files group: context `blend_file_node.can_update_all(kwargs)`, action
  `blend_file_node.update_all(kwargs)`.
- Main menu: the host `houdini/MainMenuCommon.xml` defines `cycles_main_menu` (Render View); the add-on's
  `MainMenuCommon.xml` declares the same id with Asset Browser and Houdini merges them. Both use Labs' placement,
  `modifyItem/insertBefore` = `menus.HELP_MENU_ID` (checked in Houdini 22.0, where `<insertAfter>labs_menu`
  landed after Help because Labs' menu file loads later). Main-menu items carry no context: Houdini 22
  evaluates main-menu contexts once at startup, not each time the menu opens.
- The host `toolbar/Cycles.shelf` defines the base Cycles Tools buttons. The optional
  `toolbar/CyclesAssets.shelf` defines `cycles_asset_browser` and appends it with Houdini's
  `toolshelfEdit` and `addMemberTool` elements. The installer exposes that file and the tool icon
  through the add-on's Houdini path when the component is enabled. Disabled sessions load only
  the base shelf.

## Collect, Reload, Relink, Move into Project

Modules (`PKG/assets`): `texture_files` (pure: patterns, digests, safe copies), `scene_paths` (thin `hou`
layer: `PathEntry` records, `apply_rewrites`), `collect`, `relink`, `move_project`. Decisions are made by pure
functions on `PathEntry` lists and folders; the `hou` layer gathers parameters and writes the planned texts in
one undo group (a locked, linked parameter is unlocked for the write and locked again; the link data stays).

- Scanned parameters: `filename` of `cycles::image_texture` / `cycles::environment_texture` nodes (found via
  `NodeType.instances()` or inside the selected nodes), `env_map` of Environment Lights, `file` of File SOPs
  (including `converted_file` inside a Blender File node and the File SOPs inside a Blender Hierarchy object),
  and `file` of `cycles::blender_reference`, `cycles::blender_file` and `cycles::blender_hierarchy` (looked up by
  type name). The reference SOP inside a Blender File node is skipped: `apply_rewrites` pushes a rewritten
  `file` of a Blender File node to it (`blend_file_node.push_reference`; a script write runs no callback).
- Collect report: `scene_paths.linked_asset_count()` counts plain reference SOPs (old scenes) and Blender File
  nodes whose `mode` is `link` (not the reference inside a Blender File node): "N linked assets still read
  their .blend at render time. Switch them to Converted to make them portable." Paths are read unexpanded and expanded with the pattern tokens kept
  (`expand_keeping_tokens`); empty and `op:` paths are skipped. Patterns: `<UDIM>`, `%(UDIM)d`, `$F`, `$Fn`,
  `${Fn}`, `<F>`; one token, in the file name only; anything else is reported as unsupported.
- Owner: the nearest node up the parents with `cycles_asset_link` (source), `cycles_blender_library_source`,
  or `cycles_blender_library_material` (Blender, source lost); else the nearest builder or light.
- Collect destination: `blend_folder($HIP/assets_blender, source)/tex` (store: `image_record` of the
  original first, then a same-named file with equal content in the `_2` family, else `image_name`; recorded
  with `record_image(key, original, packed=False, file, sha256)`, `key = texture_files.image_key(original)`;
  `file` holds the name with `<UDIM>` standing for the varying part, also for frame sequences);
  `$HIP/assets_blender/_unknown_source/tex` and `$HIP/tex/<safe material name>` without a store
  (`unique_name` on the listing). A recorded copy of the same original whose content changed is refreshed in
  place (it is that original's copy); any other existing file is never overwritten. A blend folder whose JSON
  was deleted belongs to an unknown .blend (`blend_assets_layout.blend_folder` rule), so a later collect
  writes `<stem>__<parent>`.
- Reload after a collect: `importers._rebuild` (and `_rebuild_world`) read `collect.texture_values(node)`
  before destroying the old node and call `collect.reapply_after_reload(new, previous)` after
  `mark_linked(new, info)`. The table is `collect.after_reload_action`; warnings use
  `material.library_texture_missing`.
- Relink: `relink.plan_relink(entries, folder, hints=...)` walks the folder once (cap `MAX_WALK` files,
  reported) only when an entry has no usable hint. Hints come from the blend folders' JSON image records
  (`relative_path`) and from link data (`library_root`). The chooser is pre-filled with a registered library
  of the same name as a moved `library_root`. Ambiguous matches go through `hou.ui.selectFromList`; the
  confirmation is `hou.ui.displayMessage` with details. A missing converted file of a Blender File node is a
  geometry entry (`converted_file/file`, under `assets_blender`); after its relink the node's Status reads the
  new path (`blend_file_node._converted_present`). Relinking the `.blend` of a converted node to a copied file
  can show "Source changed" (the signature includes the file identity on Windows); Update clears it.
- Move into Project: the cache root is `assets.cache.cache_root()/assets_blender` ([Where imports write](blend-import.md#where-imports-write)).
  `move_project.move_blend_folder` moves the folder when the project has none for the same source, otherwise
  merges (assets through `asset_name`/`record_asset`, images through `image_record`/`record_image`; a
  conflicting asset folder or image group is kept as `_2`, unrecorded). `maybe_offer_move()` is the "after the
  first save" notice (once per .hip per session), called from the `hipFileEventType.AfterSave` callback that
  `cycles_houdini_startup.install_ui` registers once per UI session (`install_move_offer`). The move rewrites
  every parameter under the cache root, including the inner `converted_file` of Blender File nodes made in an
  untitled scene.
