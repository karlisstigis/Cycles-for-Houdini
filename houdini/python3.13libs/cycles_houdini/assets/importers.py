# SPDX-License-Identifier: Apache-2.0
"""Import Blender library assets into the Houdini scene (HOM, host thread only).

Every public action is exactly one undo group. Objects and collections become
a Cycles Blender File node (`blender.blend_convert.build_import`): in Link mode, reading
the .blend (Link), or in Converted mode with geometry written or stashed by the converter
(Convert & Append, internal mode "append"); materials are rebuilt natively under /mat through
`blender.blend_library_materials`; worlds become an Environment Light in /obj or a
Cycles World Material in /mat through `blender.blend_worlds`. Textures stay where the .blend points; only
packed images are extracted to ``<asset root>/<blend>/tex`` (the asset root is
``$HIP/assets_blender``, or ``<cache>/assets_blender`` for an unsaved scene).
Importing this module does not import `hou`, so the pure helpers are testable anywhere.

Report callbacks are ``report(text, warning)``; warnings (a missing texture, a
texture of the library missing after a reload) never fail an import.
"""

import os
import re

REFERENCE_SOP = "cycles::blender_reference"
ASSET_KEY_USER_DATA = "cycles_asset_key"
IMPORT_MODES = {"link": "Link (reference)", "append": "Convert & Append"}
REFERENCE_TYPES = ("OBJECT", "COLLECTION")
# Valid import modes per Blender ID type, in preference order. Objects and
# collections: Link = a Blender File node in Link mode; Append = a Blender File node in
# Converted mode (packed .bgeo.sc files or Stash in .hip, materials appended). Materials
# and worlds: Link = one shared, locked node per (source, name); Append = a new editable
# copy each time.
_VALID_MODES = {
    "MATERIAL": ("link", "append"),
    "WORLD": ("link", "append"),
    "OBJECT": ("link", "append"),
    "COLLECTION": ("link", "append"),
}
# Asset types the browser can import (badges, Import button).
IMPORTABLE_TYPES = frozenset(_VALID_MODES)
# Shown when Append is unavailable for an object or collection (kept for callers).
APPEND_OBJECTS_TOOLTIP = "Convert & Append is not available for this asset; use Link (reference)."
MISSING_TEXTURE = "material.missing_texture"
# Asset-browser Append of objects/collections: the converter's options.
APPEND_STORAGE, APPEND_STRUCTURE, APPEND_MATERIALS = "files", "packed", "append"


class AssetImportError(ValueError):
    """A user-facing reason why an asset was not imported."""


def valid_modes(id_type):
    return _VALID_MODES.get(id_type, ())


def effective_mode(id_type, requested):
    """The requested mode when valid for the type, else its only valid mode, else None."""
    modes = valid_modes(id_type)
    if requested in modes:
        return requested
    return modes[0] if modes else None


def unsupported_reason(record):
    """Return "" when the record's type can be imported, else a clear message."""
    id_type = record.get("id_type", "")
    if id_type in _VALID_MODES:
        return ""
    label = id_type.lower().replace("_", " ") or "unknown"
    return (
        f"Blender {label} assets cannot be imported into Houdini yet. "
        "Supported: materials, worlds, objects (mesh geometry) and collections."
    )


def node_name(name, fallback="blender_asset"):
    """A valid Houdini node name derived from a Blender datablock name."""
    value = re.sub(r"[^A-Za-z0-9_]+", "_", name or "").strip("_")
    if not value:
        value = fallback
    if value[0].isdigit():
        value = "_" + value
    return value[:120]


def free_name(parent, name):
    """node_name(name), suffixed _1, _2, ... when taken (never Houdini's trailing-number bump)."""
    from ..blender.blend_library_materials import free_child_name

    return free_child_name(parent, node_name(name))


def target_token(name):
    """The token the reference node's own name menu writes for `name`.

    The underscore alias ("Crate 01" -> Crate_01), quoted as an exact name only
    when the alias contains target syntax (wildcards, quotes, backslashes).
    """
    from ..blender.blend_targets import menu_entries

    return menu_entries([name])[0]


def _source(record):
    source = (record.get("ch") or {}).get("source") or ""
    if not source:
        raise AssetImportError("The asset record has no source .blend file")
    if not os.path.isfile(source):
        raise AssetImportError("Source .blend file not found: " + source)
    # Houdini convention: forward slashes in path parms, also on Windows.
    return source.replace("\\", "/")


def scene_is_untitled():
    """HOM: True for a new, never-saved scene (its $HIP is only the start-up folder)."""
    import hou

    hip_file = getattr(hou, "hipFile", None)
    try:
        if hip_file is not None and hip_file.isNewFile():
            return True
        path = hip_file.path() if hip_file is not None else ""
    except Exception:
        return True
    if not path or not os.path.isfile(path):
        return True
    hip = hou.text.expandString("$HIP")
    return os.path.normcase(os.path.abspath(os.path.dirname(path))) != os.path.normcase(os.path.abspath(hip))


def asset_root(source, library_root=None):
    """HOM. (unexpanded asset root, expanded root, notice) for an import from ``source``.

    ``$HIP/assets_blender``; an unsaved scene uses ``<cache>/assets_blender`` and the notice
    says so. A root inside the .blend's folder or ``library_root`` is refused with
    LibraryMaterialUnavailable: nothing is ever written into a library.
    """
    from ..blender import blend_library_materials as library

    text, notice = library.asset_root_text()
    try:
        root = library.resolve_asset_root(text, source, library_root or None)
    except library.AssetRootError as error:
        raise library.LibraryMaterialUnavailable(str(error)) from error
    return text, str(root), notice


def in_place_textures(record):
    """Distinct texture paths of a prepared material record that stay where the .blend points."""
    from ..blender.blend_library_materials import record_images

    return sorted(
        {
            str(image["filename"])
            for image in record_images(record or {})
            if image.get("packed") is False and image.get("filename")
        }
    )


def material_report(prepared, diagnostics):
    """[(text, warning)] for one built material: each missing texture (a warning, never a
    failure) and one line when textures stay in the library (with the Collect hint)."""
    from ..blender.blend_convert import TEXTURES_IN_PLACE

    lines = []
    for diagnostic in diagnostics or ():
        if diagnostic.get("code") == MISSING_TEXTURE:
            text = diagnostic.get("message") or "Texture not found: " + str(diagnostic.get("path") or "")
            lines.append((text, True))
    count = len(in_place_textures((prepared or {}).get("record")))
    if count:
        lines.append((TEXTURES_IN_PLACE.format(count=count), False))
    return lines


def _say(report, lines):
    report = report or _status_report
    for text, warning in lines:
        report(text, warning)


def shared_question(shared):
    """The Replace / Cancel question (blend-import.md: Import record) for blend_convert.shared_scenes entries."""
    lines = []
    for entry in shared or ():
        scenes = ", ".join(os.path.basename(str(scene).replace("\\", "/")) or str(scene) for scene in entry["scenes"])
        name = entry.get("asset") or entry.get("name")
        lines.append(f'"{name}" is also used by: {scenes}. Replacing it changes that scene too.')
    return "\n".join(lines)


def append_shared_scenes(snapshot):
    """Host thread, before the background prepare: blend_convert.shared_scenes for a Convert &
    Append snapshot that writes files ([] for Stash in .hip, which replaces no files)."""
    if not isinstance(snapshot, dict) or snapshot.get("storage") != "files":
        return []
    from ..blender import blend_convert

    return blend_convert.shared_scenes(snapshot)


def _category(name):
    import hou

    getter = getattr(hou, name, None)
    try:
        return getter() if getter else None
    except Exception:
        return None


def _child_category(network):
    try:
        return network.childTypeCategory()
    except Exception:
        return None


def _type_category(node):
    try:
        return node.type().category()
    except Exception:
        return None


def _mark(node, record):
    try:
        node.setUserData(ASSET_KEY_USER_DATA, (record.get("ch") or {}).get("key", ""))
    except Exception:
        pass


def _place(node, position):
    if position is not None:
        try:
            node.setPosition(position)
            return
        except Exception:
            pass
    node.moveToGoodPosition()


def _import_reference(record, network, position, viewport_shape=None, report=None):
    """Link an object/collection (inside the caller's undo group): a Cycles Blender File node in
    Link mode through the converter (blend_convert.build_import), in a new geo object, or
    directly in a SOP network it is dropped into. Without the node assets the converter's own
    fallback creates a plain reference SOP. Returns the top-level nodes and, for a new geo
    object, the node inside it."""
    import hou

    from ..blender import blend_convert

    source = _source(record)
    obj = _category("objNodeTypeCategory")
    sop = _category("sopNodeTypeCategory")
    parent = network if network is not None else hou.node("/obj")
    if _child_category(parent) not in (obj, sop):
        parent = hou.node("/obj")
    kind = "collection" if record["id_type"] == "COLLECTION" else "object"
    snapshot = blend_convert.import_snapshot(
        source,
        [(kind, record["name"])],
        storage="reference",
        parent=parent,
        position=position,
        # Set before the node's first refresh so that one evaluation produces the hull.
        viewport_shape=viewport_shape if viewport_shape in ("bounds", "hull") else None,
    )
    nodes = blend_convert.build_import(blend_convert.prepare_import(snapshot), undo=False, report=report)
    created = []
    for node in nodes:
        created.append(node)
        # A new geo object: also return the node inside it. Not for the Blender File node itself
        # (a SOP asset, whose own children are SOPs too).
        if node is not parent and _type_category(node) == obj and _child_category(node) == sop:
            created.extend(
                child
                for child in node.children()
                if child.type().name().split("::")[:2] in (["cycles", "blender_file"], ["cycles", "blender_reference"])
            )
    for node in created:
        _mark(node, record)
    if created:
        try:
            created[0].setCurrent(True, clear_all_selected=True)
        except Exception:
            pass
    return created


def append_snapshot(
    record,
    *,
    library_root=None,
    network=None,
    position=None,
    storage=None,
    **_,
):
    """Host thread (HOM): converter snapshot for Convert & Append of an object or collection.

    storage: "files" (Save to Disk as .bgeo.sc, under ``$HIP/assets_blender``) or "embedded"
    (Stash in .hip); default: the browser setting append_storage. Before starting the
    background prepare, ask about other scenes with append_shared_scenes (blend-import.md: Import record).
    """
    from ..blender import blend_convert
    from . import settings

    if record.get("id_type") not in REFERENCE_TYPES:
        raise AssetImportError("Only objects and collections are appended as geometry")
    source = _source(record)
    if isinstance(network, str):
        import hou

        network = hou.node(network)
    stored = settings.load_settings()
    storage = storage or stored.get("append_storage") or APPEND_STORAGE
    if storage not in settings.APPEND_STORAGES:
        raise AssetImportError("Append storage must be one of " + ", ".join(settings.APPEND_STORAGES))
    return blend_convert.import_snapshot(
        source,
        [("collection" if record["id_type"] == "COLLECTION" else "object", record["name"])],
        storage=storage,
        structure=APPEND_STRUCTURE,
        materials=APPEND_MATERIALS,
        parent=network,
        position=position,
        library_root=library_root,
    )


def prepare_append(snapshot, cancelled=None):
    """Background thread, no HOM: Blender export and conversion for append_snapshot's snapshot."""
    from ..blender import blend_convert

    return blend_convert.prepare_import(snapshot, cancelled=cancelled)


def _append_geometry(record, network, position, prepared, library_root, report=None):
    """Convert & Append an object/collection (inside the caller's undo group); synchronous
    without `prepared`. The converter's report (missing textures, textures in place) goes to
    `report`."""
    from ..blender import blend_convert

    if prepared is None:
        snapshot = append_snapshot(record, library_root=library_root, network=network, position=position)
        prepared = blend_convert.prepare_import(snapshot)
    nodes = blend_convert.build_import(prepared, undo=False, report=report or _status_report)
    for node in nodes:
        _mark(node, record)
    return nodes


def record_importability(record, cache, state, reason):
    """Store a verdict in the cache (when given) and on the record so the badge updates."""
    _record_importability(record, cache, state, reason)


def is_content_failure(error):
    """True only for verdicts about the material itself, never for transport/infrastructure."""
    from ..blender.blend_library_materials import LibraryMaterialUnavailable
    from ..blender.blend_materials import BlendMaterialError

    return isinstance(error, BlendMaterialError) and not isinstance(error, LibraryMaterialUnavailable)


def material_snapshot(record, *, library_root=None):
    """Host thread (HOM): everything prepare_material needs, captured before the background step.

    Returns {"asset_root_text", "root", "blender", "source_config", "notice", "source", "name",
    "library_root"}: the unexpanded asset root and its expanded folder (asset_root), the
    untitled-scene notice, and the registered library (recorded for Relink).
    """
    import hou

    from ..blender import blend_library_materials
    from ..core import blender_bridge

    source = _source(record)
    text, root, notice = asset_root(source, library_root)
    if not blender_bridge.enabled():
        raise blend_library_materials.LibraryMaterialUnavailable(
            "The Blender bridge is disabled for this installation (Cycles Setup)."
        )
    getenv = getattr(hou, "getenv", None)
    return {
        "asset_root_text": text,
        "root": root,
        "blender": (
            blender_bridge._configured_executable(),
            os.environ.get("PATH", ""),
            os.environ.get("ProgramFiles", "C:/Program Files"),
        ),
        "source_config": getenv("CYCLES_HOUDINI_BLENDER_OCIO") if callable(getenv) else None,
        "notice": notice,
        "source": source,
        "name": record["name"],
        "library_root": (library_root or "").replace("\\", "/") or None,
    }


def _executable(snapshot):
    """Background thread: ``snapshot["executable"]`` or Blender found from the snapshot's inputs."""
    from ..blender import blend_library_materials
    from ..core import blender_bridge

    executable = snapshot.get("executable") or blender_bridge._discover(*snapshot["blender"])
    if not executable or not os.path.isfile(executable):
        raise blend_library_materials.LibraryMaterialUnavailable(
            "Blender not found. Install Blender or set CYCLES_HOUDINI_BLENDER to its executable."
        )
    return executable


def prepare_material(snapshot, cancelled=None, timeout=300):
    """Background thread, no HOM: find Blender (or use ``snapshot["executable"]``), export with
    textures in place, extract packed images. Returns a `prepared` dict for the build."""
    from ..blender import blend_library_materials

    prepared = blend_library_materials.prepare_library_material(
        snapshot["source"],
        snapshot["name"],
        root=snapshot["root"],
        executable=_executable(snapshot),
        timeout=timeout,
        source_config=snapshot.get("source_config"),
        cancelled=cancelled,
        library_root=snapshot.get("library_root"),
    )
    prepared["asset_root_text"] = snapshot["asset_root_text"]
    prepared["notice"] = snapshot.get("notice", "")
    return prepared


def _record_importability(record, cache, state, reason):
    if cache is None:
        return
    try:
        cache.set_importability((record.get("ch") or {}).get("key", ""), state, reason)
    except Exception:
        pass
    ch = record.get("ch")
    if isinstance(ch, dict):
        ch["importable"] = {"state": state, "reason": reason}


def _import_material(record, network, *, cache, executable, timeout, library_root=None, prepared=None, report=None):
    """Build one material (inside the caller's undo group); exports synchronously without
    `prepared`. Reports missing textures (warnings) and the textures-in-place line."""
    import hou

    from ..blender import blend_library_materials
    from ..blender.blend_materials import BlendMaterialError

    parent = None
    if network is not None and _child_category(network) == _category("vopNodeTypeCategory"):
        parent = network
    diagnostics, notes = [], []
    try:
        if prepared is None:
            snapshot = material_snapshot(record, library_root=library_root)
            if executable:
                snapshot["executable"] = str(executable)
            prepared = prepare_material(snapshot, timeout=timeout)
            if prepared.get("notice"):
                notes.append((prepared["notice"], False))
        # Export already ran (in the background for the panel): only the HOM build happens here.
        node = blend_library_materials.build_library_material(prepared, parent=parent, diagnostics=diagnostics)
    except BlendMaterialError as error:
        # Only content verdicts mark the asset; a timeout or missing Blender says nothing about it.
        if is_content_failure(error):
            _record_importability(record, cache, "no", str(error))
        raise
    _record_importability(record, cache, "yes", "")
    _mark(node, record)
    try:
        node.setCurrent(True, clear_all_selected=True)
    except hou.Error:
        pass
    _say(report, notes + material_report(prepared, diagnostics))
    return node


def _link_material(record, network, *, library_root, **build):
    """Link mode: reuse the shared node for (source, name), or build it once and lock it."""
    from ..blender import blend_library_materials

    existing = find_linked_material(record)
    if existing is not None:
        try:
            existing.setCurrent(True, clear_all_selected=True)
        except Exception:
            pass
        return existing
    node = _import_material(record, network, library_root=library_root, **build)
    source = _source(record)
    blend_library_materials.mark_linked(
        node,
        {
            "source": source,
            "name": record["name"],
            "library_root": (library_root or "").replace("\\", "/"),
            "signature": source_signature(source),
            "key": (record.get("ch") or {}).get("key", ""),
        },
    )
    return node


# ---------------------------------------------------------------- worlds (blend-import.md: Worlds)


def _blend_worlds():
    from ..blender import blend_worlds

    return blend_worlds


def _is_world_link(info):
    return bool(info) and info.get("kind") == "world"


def find_linked_world(record):
    """HOM: the shared linked node of the record's (source .blend, world name), or None."""
    source = (record.get("ch") or {}).get("source") or ""
    return _blend_worlds().find_linked_world(source, record.get("name")) if source else None


def existing_environment(record, mode="link"):
    """HOM: the active environment lighting to name in the Replace / Keep existing / Cancel
    question, asked before a world import starts; [] when there is nothing to ask (a linked
    world that would be reused does not count)."""
    return _blend_worlds().existing_environment(_source(record), record["name"], mode=mode)


ENVIRONMENT_EXPLANATION = (
    "Replace: the new one becomes active; existing ones are disabled, not deleted. "
    "Keep existing: the new one is imported disabled."
)


def environment_question(names):
    """The Replace / Keep existing question (blend-import.md: Worlds) for existing_environment's paths."""
    return f"The scene already has environment lighting ({', '.join(names)})."


def world_snapshot(record, *, library_root=None):
    """Host thread (HOM): everything prepare_world needs (material_snapshot's keys and "kind")."""
    snapshot = material_snapshot(record, library_root=library_root)
    snapshot["kind"] = "world"
    return snapshot


def prepare_world(snapshot, cancelled=None, timeout=300):
    """Background thread, no HOM: export the world with textures in place (packed HDRIs to
    ``<blend>/tex``). Returns the `prepared` dict for blend_worlds.build_world."""
    options = {"library_root": snapshot["library_root"]} if snapshot.get("library_root") else {}
    prepared = _blend_worlds().prepare_world(
        snapshot["source"],
        snapshot["name"],
        root=snapshot["root"],
        executable=_executable(snapshot),
        timeout=timeout,
        cancelled=cancelled,
        source_config=snapshot.get("source_config"),
        **options,
    )
    prepared["asset_root_text"] = snapshot.get("asset_root_text")
    prepared["notice"] = snapshot.get("notice", "")
    return prepared


def _import_world(
    record,
    *,
    mode,
    cache,
    library_root,
    executable,
    timeout,
    prepared=None,
    existing_environment="replace",
    report=None,
):
    """Build one world (inside the caller's undo group): an Environment Light or a World
    Material, which becomes the active environment unless existing_environment is "keep".
    Link reuses the shared node of (source, world) without an export; otherwise Blender
    exports synchronously when `prepared` is not given. Returns the node."""
    from ..blender.blend_materials import BlendMaterialError

    worlds = _blend_worlds()
    source = _source(record)
    notes, diagnostics = [], []
    try:
        if prepared is None and mode == "link" and worlds.find_linked_world(source, record["name"]) is not None:
            prepared = {"source": source, "name": record["name"], "route": None}  # reused: no export
        elif prepared is None:
            snapshot = world_snapshot(record, library_root=library_root)
            if executable:
                snapshot["executable"] = str(executable)
            prepared = prepare_world(snapshot, timeout=timeout)
            if prepared.get("notice"):
                notes.append((prepared["notice"], False))
        result = worlds.build_world(
            prepared,
            mode=mode,
            existing_environment=existing_environment,
            diagnostics=diagnostics,
            link_info={
                "library_root": (library_root or "").replace("\\", "/"),
                "key": (record.get("ch") or {}).get("key", ""),
            },
        )
    except BlendMaterialError as error:
        # Only content verdicts (not a simple world and no translator route) mark the asset.
        if is_content_failure(error):
            _record_importability(record, cache, "no", str(error))
        raise
    _record_importability(record, cache, "yes", "")
    node = result["node"]
    _mark(node, record)
    warnings = {diagnostic.get("message") for diagnostic in diagnostics}
    _say(report, notes + [(line, line in warnings) for line in result["report"]])
    return node


def import_asset(
    record,
    *,
    mode="link",
    network=None,
    position=None,
    cache=None,
    library_root=None,
    executable=None,
    timeout=300,
    prepared=None,
    viewport_shape=None,
    report=None,
    existing_environment="replace",
):
    """Import one asset as a single undo step; return the created top-level nodes.

    `mode` is "link" (reference) or "append" (Convert & Append). An invalid mode for the
    type raises; callers wanting the fallback use `effective_mode`. `cache` is
    the library's IndexCache, used to record material importability. `prepared`
    (from `prepare_material`, or `prepare_append` for objects/collections, run on
    a background thread) skips the Blender export so only the HOM build runs
    here. `network` may be a node or a path. `report(text, warning)` receives the
    import report (default: Houdini's status bar). Worlds (`prepared` from `prepare_world`)
    go to /obj or /mat whatever `network`; `existing_environment` is "replace" or "keep"
    (blend_worlds.activation_plan; ask with existing_environment() first).
    """
    import hou

    reason = unsupported_reason(record)
    if reason:
        raise AssetImportError(reason)
    id_type = record["id_type"]
    if mode not in valid_modes(id_type):
        if id_type in REFERENCE_TYPES and mode == "append":
            raise AssetImportError(APPEND_OBJECTS_TOOLTIP)
        raise AssetImportError(f"{IMPORT_MODES.get(mode, mode)} is not available for Blender {id_type.lower()} assets")
    if isinstance(network, str):
        network = hou.node(network)
    with hou.undos.group("Import Blender asset " + record["name"]):
        if id_type in REFERENCE_TYPES and mode == "append":
            return _append_geometry(record, network, position, prepared, library_root, report)
        if id_type in REFERENCE_TYPES:
            return _import_reference(record, network, position, viewport_shape, report)
        build = {
            "cache": cache,
            "library_root": library_root,
            "executable": executable,
            "timeout": timeout,
            "prepared": prepared,
            "report": report,
        }
        if id_type == "WORLD":
            return [_import_world(record, mode=mode, existing_environment=existing_environment, **build)]
        if mode == "link":
            reused = find_linked_material(record)
            node = _link_material(record, network, **build)
            if reused is not None:
                return [node]  # the shared node stays where it is
        else:
            node = _import_material(record, network, **build)
        if position is not None:
            _place(node, position)
        return [node]


def selected_objects(nodes=None):
    """Object-level nodes for the selection, walking up from SOPs (as menu actions do)."""
    import hou

    nodes = hou.selectedNodes() if nodes is None else nodes
    result = []
    for node in nodes:
        if isinstance(node, str):
            node = hou.node(node)
        while node is not None and node.type().category() != hou.objNodeTypeCategory():
            node = node.parent()
        if node is not None and node not in result and node.parm("shop_materialpath") is not None:
            result.append(node)
    return result


def source_signature(source):
    """JSON-able signature of a .blend file (host thread; one stat), for stale-link checks."""
    from .cache import normalize_signature
    from .providers import file_signature

    try:
        return normalize_signature(file_signature(source))
    except Exception:
        return None


def _same_file(a, b):
    return bool(a and b) and os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def all_linked_materials(root=None):
    """Linked material nodes directly under /mat (or `root`); linked World Materials are
    worlds (all_linked_worlds)."""
    import hou

    from ..blender.blend_library_materials import link_info

    root = root or hou.node("/mat")
    if root is None:
        return []
    found = []
    for node in root.children():
        info = link_info(node)
        if info is not None and not _is_world_link(info):
            found.append(node)
    return found


def all_linked_worlds():
    """Linked Environment Lights in /obj and linked World Materials in /mat."""
    from ..blender.blend_library_materials import link_info

    return [node for node in _blend_worlds().all_imported_worlds() if _is_world_link(link_info(node))]


def all_linked():
    """Every linked Blender asset node that Reload from Library refreshes: materials, then worlds."""
    return all_linked_materials() + all_linked_worlds()


def find_linked_material(record):
    """The one shared linked node for (source .blend, material name), or None."""
    from ..blender.blend_library_materials import link_info

    source = (record.get("ch") or {}).get("source") or ""
    for node in all_linked_materials():
        info = link_info(node)
        if info["name"] == record.get("name") and _same_file(info["source"], source):
            return node
    return None


def linked_status(record):
    """{"path", "stale"} for the record's linked node, or None. Cheap: /mat (and, for a
    world, /obj) children and one stat."""
    from ..blender.blend_library_materials import link_info

    node = find_linked_world(record) if record.get("id_type") == "WORLD" else find_linked_material(record)
    if node is None:
        return None
    stored = link_info(node).get("signature")
    current = source_signature(link_info(node)["source"])
    return {"path": node.path(), "stale": stored is not None and current is not None and stored != current}


def find_imported_material(record, mode="append"):
    """A material previously imported from this asset, reused instead of re-imported.

    mode "link": the shared linked node; "append": an editable (non-linked) copy.
    Materials imported before the prototype-marker fix are repaired when reused.
    """
    node = _find_imported_material(record, mode)
    if node is not None:
        from ..blender.blend_library_materials import release_prototype_marker

        release_prototype_marker(node)
    return node


def _find_imported_material(record, mode="append"):
    import hou

    from ..blender.blend_library_materials import link_info

    if mode == "link":
        return find_linked_material(record)
    key = (record.get("ch") or {}).get("key", "")
    root = hou.node("/mat")
    if root is None or not (key or (record.get("ch") or {}).get("source")):
        return None
    for node in root.children():
        if link_info(node) is not None:
            continue
        try:
            if key and node.userData(ASSET_KEY_USER_DATA) == key:
                return node
            source = node.userData("cycles_blender_library_source")
            if (
                source
                and node.userData("cycles_blender_library_material") == record.get("name")
                and os.path.normcase(os.path.abspath(source))
                == os.path.normcase(os.path.abspath((record.get("ch") or {}).get("source") or ""))
            ):
                return node
        except Exception:
            continue
    return None


def assign_material(
    record,
    nodes=None,
    *,
    cache=None,
    library_root=None,
    executable=None,
    prepared=None,
    mode="append",
    report=None,
):
    """Import (or reuse) the material and set `shop_materialpath` on objects; one undo step.

    mode "link" reuses or creates the shared linked node; "append" reuses an editable
    copy made from this asset before, or makes one.

    Cycles reads the object-level `shop_materialpath` parameter
    (scene/stylesheets.object_binding), so that is what is set here.
    """
    import hou

    if record.get("id_type") != "MATERIAL":
        raise AssetImportError("Only material assets can be assigned to objects")
    targets = selected_objects(nodes)
    if not targets:
        raise AssetImportError("Select one or more objects (OBJ nodes) to assign the material to")
    with hou.undos.group("Assign Blender material " + record["name"]):
        material = find_imported_material(record, mode)
        if material is None:
            build = {
                "cache": cache,
                "library_root": library_root,
                "executable": executable,
                "timeout": 300,
                "prepared": prepared,
                "report": report,
            }
            if mode == "link":
                material = _link_material(record, None, **build)
            else:
                material = _import_material(record, None, **build)
        for node in targets:
            node.parm("shop_materialpath").set(material.path())
    return material, targets


# ---------------------------------------------------------------- linked material actions


def _node(node):
    import hou

    return hou.node(node) if isinstance(node, str) else node


def append_linked(node):
    """Make Editable (make local): unlock, drop the link data and comment; keeps node and path.
    Linked materials, Environment Lights and World Materials alike. One undo step."""
    import hou

    from ..blender import blend_library_materials

    node = _node(node)
    if blend_library_materials.link_info(node) is None:
        raise AssetImportError("Not a linked Blender asset: " + (node.path() if node is not None else "None"))
    with hou.undos.group("Make linked asset editable " + node.name()):
        blend_library_materials.unmark_linked(node)
    return node


def reload_snapshot(node):
    """Host thread: what prepare_linked needs to re-export a linked material or world.

    Packed images go to the scene's asset root (asset_root); the "texture_root" and
    "library" entries of links made by earlier builds are ignored. A linked world's
    snapshot comes from blend_worlds.reload_request and has "kind": "world".
    """
    import hou

    from ..blender import blend_library_materials
    from ..core import blender_bridge

    node = _node(node)
    info = blend_library_materials.link_info(node)
    if info is None:
        raise AssetImportError("Not a linked Blender asset: " + (node.path() if node is not None else "None"))
    if _is_world_link(info):
        return _world_reload_snapshot(node)
    if not os.path.isfile(info["source"]):
        raise blend_library_materials.LibraryMaterialUnavailable("Library file not found: " + info["source"])
    text, root, notice = asset_root(info["source"], info.get("library_root") or None)
    getenv = getattr(hou, "getenv", None)
    return {
        "asset_root_text": text,
        "root": root,
        "blender": (
            blender_bridge._configured_executable(),
            os.environ.get("PATH", ""),
            os.environ.get("ProgramFiles", "C:/Program Files"),
        ),
        "source_config": getenv("CYCLES_HOUDINI_BLENDER_OCIO") if callable(getenv) else None,
        "notice": notice,
        "source": info["source"],
        "name": info["name"],
        "path": node.path(),
        "library_root": info.get("library_root") or None,
    }


def _world_reload_snapshot(node):
    """Host thread: reload snapshot of a linked world (blend_worlds.reload_request). Packed
    HDRIs go back to the asset root recorded at link time; its unexpanded form is used
    only while it still names that folder."""
    import hou

    from ..blender import blend_library_materials
    from ..core import blender_bridge

    request = _blend_worlds().reload_request(node)
    if not os.path.isfile(request["source"]):
        raise blend_library_materials.LibraryMaterialUnavailable("Library file not found: " + request["source"])
    text, notice = blend_library_materials.asset_root_text()
    expanded = hou.text.expandString(text)
    same = os.path.normcase(os.path.abspath(expanded)) == os.path.normcase(os.path.abspath(request["root"]))
    getenv = getattr(hou, "getenv", None)
    return {
        "kind": "world",
        "asset_root_text": text if same else None,
        "root": request["root"],
        "blender": (
            blender_bridge._configured_executable(),
            os.environ.get("PATH", ""),
            os.environ.get("ProgramFiles", "C:/Program Files"),
        ),
        "source_config": getenv("CYCLES_HOUDINI_BLENDER_OCIO") if callable(getenv) else None,
        "notice": notice if same else "",
        "source": request["source"],
        "name": request["name"],
        "path": request["path"],
    }


def prepare_linked(snapshot, cancelled=None, timeout=300):
    """Background thread, no HOM: re-export for a reload_snapshot (a world or a material)."""
    if snapshot.get("kind") == "world":
        return prepare_world(snapshot, cancelled, timeout)
    return prepare_material(snapshot, cancelled, timeout)


# Link data written by earlier builds that is no longer written (read without error, dropped).
_OBSOLETE_LINK_KEYS = ("locked_before", "texture_root", "library")


def _rebuild(node, prepared, lines):
    """Swap one linked node for its rebuild; collected texture paths survive (blend-import.md: Collect and relink).

    Appends the report lines (missing textures, Reload-after-Collect warnings) to `lines`.
    """
    from ..blender import blend_library_materials
    from . import collect

    info = blend_library_materials.link_info(node)
    if _is_world_link(info):
        return _rebuild_world(node, prepared, lines)
    parent, name = node.parent(), node.name()
    position = node.position()
    key = _guarded_user_data(node, ASSET_KEY_USER_DATA)
    # Paths Collect Textures wrote into the old node, read before it is destroyed.
    previous = collect.texture_values(node)
    diagnostics = []
    # Build beside the old node under a temporary (unique) name, then swap: object
    # assignments are path strings, so they follow the restored name.
    new = blend_library_materials.build_library_material(prepared, parent=parent, diagnostics=diagnostics)
    node.destroy()
    new.setName(name)
    new.setPosition(position)
    info = dict(info, signature=source_signature(info["source"]))
    for obsolete in _OBSOLETE_LINK_KEYS:
        info.pop(obsolete, None)
    blend_library_materials.mark_linked(new, info)
    if key:
        new.setUserData(ASSET_KEY_USER_DATA, key)
    warnings = collect.reapply_after_reload(new, previous)
    # Reload reports only warnings (missing textures); the textures-in-place line is for imports.
    lines += [(text, True) for text, warning in material_report(prepared, diagnostics) if warning]
    lines += [(warning.get("message") or warning.get("code", ""), True) for warning in warnings]
    return new


def _rebuild_world(node, prepared, lines):
    """_rebuild for a linked world (blend_worlds.rebuild_linked_world): collected HDRI and
    texture paths survive as for materials (blend-import.md: Collect and relink); only warnings are reported."""
    from . import collect

    key = _guarded_user_data(node, ASSET_KEY_USER_DATA)
    previous = collect.texture_values(node)
    diagnostics = []
    new, _ = _blend_worlds().rebuild_linked_world(node, prepared, diagnostics)
    if key:
        new.setUserData(ASSET_KEY_USER_DATA, key)
    warnings = collect.reapply_after_reload(new, previous)
    lines += [(item.get("message") or item.get("code", ""), True) for item in diagnostics + list(warnings)]
    return new


def _guarded_user_data(node, name):
    try:
        return node.userData(name)
    except Exception:
        return None


def rebuild_linked(nodes_and_prepared, report=None):
    """Host thread: swap each linked node for its re-exported build; one undo group for all.

    `nodes_and_prepared`: [(node or path, prepared)]; linked worlds take prepare_world results
    (prepare_linked dispatches). Returns the new nodes (same paths).
    Texture parameters Collect Textures had rewritten keep their collected copies (blend-import.md: Collect and relink);
    its warnings and missing textures go to `report(text, warning)` as warnings.
    """
    import hou

    pairs = [(_node(node), prepared) for node, prepared in nodes_and_prepared]
    pairs = [(node, prepared) for node, prepared in pairs if node is not None]
    if not pairs:
        return []
    from ..blender.blend_library_materials import link_info

    noun = "asset" if any(_is_world_link(link_info(node)) for node, _ in pairs) else "material"
    label = pairs[0][0].name() if len(pairs) == 1 else f"{len(pairs)} {noun}s"
    lines = []
    with hou.undos.group(f"Reload linked {noun} " + label):
        new = [_rebuild(node, prepared, lines) for node, prepared in pairs]
    _say(report, lines)
    return new


def reload_linked_material(node, *, prepared=None, timeout=300, report=None):
    """Synchronous reload from the library (blocks while Blender exports unless `prepared` is given)."""
    node = _node(node)
    if prepared is None:
        prepared = prepare_linked(reload_snapshot(node), timeout=timeout)
    return rebuild_linked([(node, prepared)], report)[0]


def reload_all_linked(nodes=None, *, timeout=300, report=None):
    """Synchronous reload of the given (or all) linked materials and worlds; one undo group.
    Returns new nodes."""
    nodes = [_node(node) for node in (nodes if nodes is not None else all_linked())]
    prepared = [(node.path(), prepare_linked(reload_snapshot(node), timeout=timeout)) for node in nodes if node]
    return rebuild_linked(prepared, report)


def reload_linked_async(nodes, report=None, timeout=300):
    """Re-export linked materials and worlds on a background thread, rebuild on Houdini's
    event loop (no panel needed).

    Without a UI (hython) this runs synchronously. `report(text, error)` runs on the host thread.
    """
    import queue
    import threading

    import hou

    report = report or _status_report
    nodes = [_node(node) for node in nodes]
    nodes = [node for node in nodes if node is not None]
    if not nodes:
        report("No linked Blender assets selected.", False)
        return None
    snapshots = []
    for node in nodes:
        try:
            snapshots.append(reload_snapshot(node))
        except Exception as error:
            report(str(error), True)
    if not snapshots:
        return None
    ui = hou.isUIAvailable() and hasattr(hou.ui, "addEventLoopCallback")
    if not ui:
        done = [(s["path"], prepare_linked(s, timeout=timeout)) for s in snapshots]
        warnings = []
        new = rebuild_linked(done, lambda text, warning: warnings.append(text))
        report(_reloaded_text(len(new), warnings), bool(warnings))
        return new
    results = queue.SimpleQueue()

    def work():
        for snapshot in snapshots:
            try:
                results.put((snapshot["path"], prepare_linked(snapshot, timeout=timeout), None))
            except Exception as error:
                results.put((snapshot["path"], None, str(error) or type(error).__name__))

    collected, errors = [], []

    def poll():
        while True:
            try:
                path, prepared, error = results.get_nowait()
            except queue.Empty:
                break
            (errors.append(path + ": " + error) if error else collected.append((path, prepared)))
        if len(collected) + len(errors) < len(snapshots):
            return
        hou.ui.removeEventLoopCallback(poll)
        warnings = []
        try:
            new = rebuild_linked(collected, lambda text, warning: warnings.append(text))
        except Exception as error:
            report("Reload failed: " + str(error), True)
            return
        report(_reloaded_text(len(new), warnings, errors), bool(errors or warnings))

    threading.Thread(target=work, name="cycles-asset-reload", daemon=True).start()
    hou.ui.addEventLoopCallback(poll)
    report(f"Reloading {len(snapshots)} linked asset(s) from the library...", False)
    return None


def _reloaded_text(count, warnings=(), errors=()):
    """One status line for a finished reload: count, then warnings, then failures."""
    text = f"Reloaded {count} linked asset(s)."
    if warnings:
        text += " Warnings: " + "; ".join(warnings)
    if errors:
        text += " Failed: " + "; ".join(errors)
    return text


def _status_report(text, error):
    import hou

    ui = getattr(hou, "ui", None)
    if ui is not None and hou.isUIAvailable():
        severity = getattr(getattr(hou, "severityType", None), "Warning" if error else "Message", None)
        try:
            ui.setStatusMessage("Cycles: " + text, severity=severity) if severity else ui.setStatusMessage(text)
        except Exception:
            pass
