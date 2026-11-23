# SPDX-License-Identifier: Apache-2.0
"""Import one material from a Blender asset library as a native Cycles material.

Host side (HOM), imported lazily by the asset browser and the converter. Blender
exports the material with ``textures="in_place"`` (blend_material_export._Images):
images on disk keep the absolute path the .blend points to and are never copied.
Only packed images are extracted, once per content, to the flat ``tex/`` folder of
the material's blend folder (``<asset root>/<blend>/tex/<real file name>``; blend-import.md: Folder layout, Textures
in place), with their names and originals recorded in that folder's
``cycles_import.json`` (blend_assets_layout). The library folder is never written.
"""

import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path

from . import blend_assets_layout as layout
from .blend_names import TextureNames
from .blend_materials import BlendMaterialError

_TEXTURE_TYPES = frozenset(("image_texture", "environment_texture"))
DEFAULT_ASSET_ROOT = "$HIP/" + layout.ROOT_FOLDER
UNTITLED_NOTICE = "The scene is not saved yet: Blender assets go to the shared cache "


# ---------------------------------------------------------------- asset root (shared with blend_convert)


class AssetRootError(ValueError):
    """The asset folder cannot be used: an undefined variable, or inside the library (blend-import.md: Where imports
    write)."""


def untitled_scene():
    """HOM: True for a never-saved scene (assets.importers.scene_is_untitled; False if unknown)."""
    try:
        from ..assets.importers import scene_is_untitled

        return scene_is_untitled()
    except Exception:
        return False


def cache_asset_root():
    """``<user cache>/assets_blender`` (blend-import.md: Where imports write), forward slashes."""
    from ..assets.cache import cache_root

    return layout.asset_root(cache_root()).as_posix()


def asset_root_text(text=DEFAULT_ASSET_ROOT):
    """HOM. (unexpanded asset root, notice): a $HIP root of an untitled scene becomes the cache (blend-import.md:
    Where imports write)."""
    text = str(text or DEFAULT_ASSET_ROOT).strip().replace("\\", "/")
    if "$HIP" in text and untitled_scene():
        cache = cache_asset_root()
        return cache, UNTITLED_NOTICE + cache
    return text, ""


def resolve_asset_root(text, source, library_root=None):
    """HOM: expand ``text`` ($HIP-relative when relative) to an absolute folder.

    Raises AssetRootError for an undefined variable, and for a folder inside the
    .blend file's folder or inside ``library_root`` (blend-import.md: Where imports write): nothing
    is ever written there.
    """
    import hou

    expanded = hou.text.expandString(text)
    if "$" in expanded:
        raise AssetRootError("The asset folder contains an undefined variable: " + text)
    root = Path(expanded)
    if not root.is_absolute():
        root = Path(hou.text.expandString("$HIP")) / root
    root = root.resolve()
    protected = [Path(source).resolve().parent] + ([Path(library_root).resolve()] if library_root else [])
    for folder in protected:
        if root.is_relative_to(folder):
            raise AssetRootError(
                f"Blender assets cannot be written to {root.as_posix()}: it is inside {folder.as_posix()}, "
                "the folder of the .blend file or of its asset library. Save the scene in a folder outside "
                "the asset library and import again."
            )
    return root


def blend_signature(source):
    """No HOM: the .blend's native file signature for cycles_import.json (None if unavailable)."""
    from .file_signature import native_signature

    try:
        return native_signature(str(source))
    except OSError:
        return None


# ---------------------------------------------------------------- names


def free_child_name(parent, base, node=None):
    """`base`, or `base_1`, `base_2`, ... when another child already has it.

    Houdini's own unique naming bumps a trailing number (``tree_LOD1`` -> ``tree_LOD2``,
    ``island_tree_01`` -> ``island_tree_02``), which reads like a different Blender asset.
    `node` (being renamed) does not count as taking its own name.
    """
    lookup = getattr(parent, "node", None)
    taken = lookup(base) if lookup is not None else None
    if taken is None or (node is not None and taken.path() == node.path()):
        return base
    index = 1
    while lookup(f"{base}_{index}") is not None:
        index += 1
    return f"{base}_{index}"


def node_name(material_name):
    """Sanitised Houdini node name for a Blender material name."""
    text = re.sub(r"_+", "_", re.sub(r"[^A-Za-z0-9_]", "_", str(material_name))).strip("_")
    if not text:
        return "blender_material"
    return "m_" + text if text[0].isdigit() else text


# ---------------------------------------------------------------- packed textures


def _sha256(path):
    with open(path, "rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _copy_verified(source, destination, digest):
    if destination.is_file() and _sha256(destination) == digest:
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp-" + os.urandom(6).hex())
    try:
        shutil.copyfile(source, temporary)
        if _sha256(temporary) != digest:
            raise ValueError("Texture changed while copying: " + str(source))
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()


def _image_names(image, dependencies):
    """(pattern, tile numbers) of one exported image; refuses unsafe or inconsistent names."""
    tiles = list(image.get("tiles") or ())
    tiled = bool(tiles)
    pattern = Path(str(image.get("filename", ""))).name
    if (
        not pattern
        or TextureNames.safe_name(pattern, "", token=tiled) != pattern
        or (tiled and TextureNames.TOKEN not in pattern)
    ):
        raise ValueError("Unsafe exported texture name: " + repr(pattern))
    numbers = tiles if tiled else [None]
    if len(numbers) != len(dependencies):
        raise ValueError("Exported texture tiles do not match their files: " + pattern)
    names = [TextureNames.tile_name(pattern, number) for number in numbers]
    for name, dependency in zip(names, dependencies, strict=True):
        if Path(str(dependency.get("filename", ""))).name != name:
            raise ValueError("Exported texture file does not match its image name: " + name)
        if not re.fullmatch(r"[0-9a-f]{64}", str(dependency.get("sha256", ""))):
            raise ValueError("Exported texture has no valid sha256: " + name)
    return pattern, numbers


def record_images(record):
    """Each image of a record once (one image dict may be shared by several nodes)."""
    seen, result = set(), []
    for node in record.get("nodes", ()):
        image = node.get("image")
        if image is not None and id(image) not in seen:
            seen.add(id(image))
            result.append(image)
    return result


def _packed_key(digests):
    """Store key of one packed image: its content (all tiles), so equal images share one file."""
    digest = digests[0] if len(digests) == 1 else hashlib.sha256("\n".join(digests).encode()).hexdigest()
    return "packed:" + digest, digest


def _holds(folder, names, digests):
    """True when every file of names exists in folder with exactly these sha256 digests."""
    paths = [Path(folder) / name for name in names]
    return all(p.is_file() for p in paths) and all(_sha256(p) == d for p, d in zip(paths, digests, strict=True))


def library_location(path, source=None, library_root=None):
    """(library_root, relative_path) of an image in place (blend-import.md: Import record): under the registered
    library ``library_root`` when it lies there, else under the .blend's own folder; else
    (None, None). Relink tries ``<chosen folder>/<relative_path>`` first."""
    from ..assets import texture_files as files

    for root in (library_root, Path(source).parent if source else None):
        if root and files.is_under(path, root):
            return files.posix(root).rstrip("/"), files.relative(path, root)
    return None, None


def _record_in_place(store, filename, source, library_root):
    """Record an image left in place. A copy Collect Textures made of it (``file``,
    ``sha256``) stays recorded: a re-import or Reload from Library must not forget it."""
    previous = store.image_record(filename) or {}
    root, relative = library_location(filename, source, library_root)
    if root is None:
        root, relative = previous.get("library_root"), previous.get("relative_path")
    store.record_image(
        filename,
        original=filename,
        packed=False,
        file=previous.get("file") if not previous.get("packed") else None,
        sha256=previous.get("sha256") if not previous.get("packed") else None,
        library_root=root,
        relative_path=relative,
    )


def _place(staging, record, blend_dir, store, source=None, library_root=None):
    tex = layout.tex_folder(blend_dir)
    staging = Path(staging).resolve()
    plans = []
    for image in record_images(record):
        if image.get("packed") is False:
            # In place: untouched; the original is recorded so Collect Textures and Relink find it.
            filename = str(image.get("filename") or "").replace("\\", "/")
            if filename:
                _record_in_place(store, filename, source, library_root)
            continue
        if image.get("packed") is not True:
            raise ValueError("Texture was not exported with textures='in_place': " + repr(image.get("filename")))
        dependencies = image.get("dependencies", [])
        sources = []
        for dependency in dependencies:
            staged = (staging / str(dependency.get("filename", ""))).resolve()
            if not staged.is_relative_to(staging) or not staged.is_file():
                raise ValueError("Missing or escaping exported texture: " + str(staged))
            sources.append(staged)
        pattern, numbers = _image_names(image, dependencies)
        digests = [dependency["sha256"] for dependency in dependencies]
        key, digest = _packed_key(digests)
        name = (store.image_record(key) or {}).get("file")
        if name is None:
            wanted = layout.safe_image_name(pattern)
            if _holds(tex, [layout.image_tile_name(wanted, n) for n in numbers], digests):
                name = wanted  # the same image is already there (another material, an earlier import)
            else:
                name = store.image_name(key, pattern)  # a different file of this name: _2, _3, ...
        names = [layout.image_tile_name(name, number) for number in numbers]
        plans.append((image, dependencies, sources, digests, key, digest, name, names))
    layout.check_path_lengths([tex / n for *_, names in plans for n in names], layout.MAX_PATH)
    for image, dependencies, sources, digests, key, digest, name, names in plans:
        for staged, dependency, file_name, sha in zip(sources, dependencies, names, digests, strict=True):
            _copy_verified(staged, tex / file_name, sha)
            dependency["filename"] = file_name
        image["filename"] = name
        original = image.get("source_path") or None
        store.record_image(key, original=original, packed=True, file=name, sha256=digest)
    return tex


def place_packed_textures(staging, record, blend_dir, *, store=None, source=None, library_root=None):
    """Extract a record's packed images to ``<blend_dir>/tex``; record every image in the JSON.

    No HOM. Packed images (``packed: True``, from textures="in_place") are copied once per
    content under their real file name: a file of that name with the same content is
    reused, a different one gets ``_2``, ``_3`` (blend_assets_layout.ImportStore.image_name).
    The record is rewritten to names relative to the returned tex folder and stays
    ``packed``, so blend_materials._Builder.image, given that folder as its root, still
    checks containment and sha256. Images in place (``packed: False``) are left untouched
    and recorded by their absolute path; a missing one is not an error here.

    ``store``: the caller's open ImportStore of ``blend_dir`` (the caller saves it). Without
    it the folder is locked, its JSON updated (``source`` recorded) and saved here.
    ``source`` and ``library_root`` (the registered library, optional) give in-place images
    their ``library_root`` / ``relative_path`` (library_location).
    """
    if not record_images(record):
        return layout.tex_folder(blend_dir)
    if store is not None:
        return _place(staging, record, blend_dir, store, source, library_root)
    with layout.open_store(blend_dir) as own:
        if not own.writable:
            raise layout.ForeignStoreError(
                f"{layout.json_path(blend_dir)} was written by a newer version of Cycles for Houdini; "
                "this import does not change it."
            )
        if source is not None:
            own.set_source(source, blend_signature(source))
        tex = _place(staging, record, blend_dir, own, source, library_root)
        own.save()
    return tex


# ---------------------------------------------------------------- texture parameters


def _texture_nodes(material):
    for child in material.allSubChildren():
        parm = child.parm("filename")
        if parm is not None and _TEXTURE_TYPES.intersection(child.type().name().split("::")):
            yield parm


def forward_slash_texture_paths(material):
    """Houdini convention: texture file parms use forward slashes, also on Windows."""
    for parm in _texture_nodes(material):
        value = parm.unexpandedString()
        if "\\" in value:
            parm.set(value.replace("\\", "/"))


def _variable_texture_paths(material, root, unexpanded_root, expand):
    """Point texture parms under the absolute asset `root` at `unexpanded_root` ($HIP/...)."""
    root = Path(root).resolve()
    prefix = unexpanded_root.replace("\\", "/").rstrip("/")
    for parm in _texture_nodes(material):
        value = parm.unexpandedString()
        try:
            relative = Path(value).resolve().relative_to(root)
        except ValueError:
            continue
        candidate = prefix + "/" + relative.as_posix()
        # Only keep the variable form when it names exactly the file just written.
        if Path(expand(candidate)).resolve() == Path(value).resolve():
            parm.set(candidate)


# ---------------------------------------------------------------- import


class LibraryMaterialUnavailable(BlendMaterialError):
    """Transport or infrastructure failure: Blender missing, timeout, worker crash, cancellation,
    disk errors or configuration. It says nothing about the material itself, so callers must
    not record it as "not importable"; plain BlendMaterialError is a content verdict."""


def _error_types():
    return BlendMaterialError, LibraryMaterialUnavailable


def prepare_library_material(
    source, name, *, root, executable, timeout=300, source_config=None, cancelled=None, library_root=None
):
    """No HOM; safe on a background thread. Export `name` (textures in place) in a disposable
    worker and extract its packed images to ``<root>/<blend>/tex``. `root` is the expanded
    asset root (``resolve_asset_root``); ``<blend>`` is blend_assets_layout.blend_folder of
    `source`, the library file the material comes from.

    Returns {"record", "root" (the tex folder: the builder's root), "source", "name",
    "asset_root", "blend_folder"} for build_library_material. Content failures (the
    exporter's per-material verdict) raise BlendMaterialError; everything else raises
    LibraryMaterialUnavailable. `cancelled` is a threading.Event.
    """
    from . import blend_worker

    content, unavailable = _error_types()
    source, root = Path(source).resolve(), Path(root)
    if cancelled is not None and cancelled.is_set():
        raise unavailable("Material import cancelled")
    if not executable:
        raise unavailable("Blender not found; install Blender or set CYCLES_HOUDINI_BLENDER")
    options = {"source_config": source_config} if source_config else {}
    with tempfile.TemporaryDirectory(prefix="cycles_blend_library_", ignore_cleanup_errors=True) as staging:
        try:
            exported = blend_worker.export_library_materials(
                source,
                [name],
                staging,
                executable,
                timeout=timeout,
                cancelled=cancelled,
                textures="in_place",
                **options,
            )
        except Exception as error:
            if cancelled is not None and cancelled.is_set():
                raise unavailable("Material import cancelled") from error
            raise unavailable(f"Blender could not export '{name}' from {source}: {error}") from error
        if cancelled is not None and cancelled.is_set():
            raise unavailable("Material import cancelled")
        if name in exported["errors"]:
            raise content(exported["errors"][name])
        records = [record for record in exported["records"] if record.get("name") == name]
        if len(records) != 1:
            raise content("Blender returned no material record for " + name)
        record = records[0]
        blend_dir = layout.blend_folder(root, source)
        try:
            tex = place_packed_textures(staging, record, blend_dir, source=source, library_root=library_root)
        except (OSError, ValueError, RuntimeError) as error:
            raise unavailable(f"Could not place the packed textures of {name} in {blend_dir}: {error}") from error
    return {
        "record": record,
        "root": str(tex),
        "source": str(source),
        "name": name,
        "asset_root": str(root),
        "blend_folder": str(blend_dir),
    }


# blend_materials marks what it builds as a hidden reference-render prototype. The
# scene observer ignores marked nodes, so edits to an artist's library material
# would never reach the IPR. Library materials are ordinary scene materials.
PROTOTYPE_MARKER = "cycles_external_blend"


def release_prototype_marker(material):
    """Make an imported library material an ordinary, IPR-tracked scene material."""
    try:
        nodes = [material, *material.allSubChildren()]
    except Exception:
        nodes = [material]
    for node in nodes:
        try:
            if node.userData(PROTOTYPE_MARKER) is not None:
                node.destroyUserData(PROTOTYPE_MARKER)
        except Exception:
            continue
    return material


def build_library_material(prepared, *, parent=None, diagnostics=None):
    """Host thread (HOM): build the prepared record under parent (default /mat). Undo stays enabled.

    Texture parms below the asset root use its unexpanded form ($HIP/assets_blender/...):
    ``prepared["asset_root_text"]`` when given, else the scene's asset_root_text().
    ``diagnostics`` (list) receives the builder's warnings, e.g. material.missing_texture.
    """
    import hou

    from . import blend_materials

    _, unavailable = _error_types()
    parent = parent or hou.node("/mat")
    if parent is None:
        raise unavailable("No material network to import into")
    record, root, name = prepared["record"], Path(prepared["root"]), prepared["name"]
    # import_materials failures are content verdicts (unsupported nodes and the like).
    paths = blend_materials.import_materials([record], root, parent=parent, diagnostics=diagnostics)
    material = hou.node(paths[record["key"]])
    try:
        material.setName(free_child_name(parent, node_name(name), material), unique_name=True)
        release_prototype_marker(material)
        material.setUserData("cycles_blender_library_source", str(prepared["source"]).replace("\\", "/"))
        forward_slash_texture_paths(material)
        material.setUserData("cycles_blender_library_material", name)
        asset_root = prepared.get("asset_root")
        if asset_root:
            text = prepared.get("asset_root_text") or asset_root_text()[0]
            expanded = hou.text.expandString(text)
            if text.strip() != expanded and Path(expanded).resolve() == Path(asset_root).resolve():
                _variable_texture_paths(material, asset_root, text.strip(), hou.text.expandString)
    except Exception as error:
        material.destroy()
        raise unavailable(f"Could not finish importing {name}: {error}") from error
    return material


def import_library_material(
    source,
    name,
    *,
    parent=None,
    executable=None,
    timeout=300,
    library_root=None,
    source_config=None,
    diagnostics=None,
):
    """Synchronous wrapper: resolve, prepare (blocks on Blender) and build in one call.

    Undo stays enabled: callers wrap this in one undo group. Raises
    blend_materials.BlendMaterialError with the exporter/importer reason, or its subclass
    LibraryMaterialUnavailable for transport/infrastructure failures. `library_root`
    (optional) widens the never-write-into-the-library guard to the whole library.
    Packed images go to the scene's asset root (asset_root_text).
    """
    import hou

    from ..core import blender_bridge

    _, unavailable = _error_types()
    text, _ = asset_root_text()
    try:
        root = resolve_asset_root(text, source, library_root)
    except AssetRootError as error:
        raise unavailable(str(error)) from error
    try:
        blender = blender_bridge.require_executable(executable)
    except ValueError as error:
        raise unavailable(str(error)) from error
    if (parent or hou.node("/mat")) is None:
        raise unavailable("No material network to import into")
    if source_config is None:
        getenv = getattr(hou, "getenv", None)
        source_config = getenv("CYCLES_HOUDINI_BLENDER_OCIO") if callable(getenv) else None
    prepared = prepare_library_material(
        source,
        name,
        root=root,
        executable=str(blender),
        timeout=timeout,
        source_config=source_config,
        library_root=library_root,
    )
    prepared["asset_root_text"] = text
    return build_library_material(prepared, parent=parent, diagnostics=diagnostics)


# ---------------------------------------------------------------- linked materials
#
# A linked material is one shared, read-only node per (source .blend, material name):
# Blender's linked data in Houdini terms. Every parameter of the builder and of all
# its descendants is locked, so Houdini greys them out. Wiring inside a non-HDA
# subnet cannot be fully prevented; locked parameters are the practical read-only.

LINK_USER_DATA = "cycles_asset_link"


def _guarded(call, *args):
    try:
        return call(*args)
    except Exception:
        return None


def _material_nodes(material):
    yield material
    children = getattr(material, "allSubChildren", None)
    yield from (_guarded(children) or ()) if callable(children) else ()


def _material_parms(material):
    """(relative parm path, parm) for the builder and all descendants."""
    prefix = material.path().rstrip("/")
    for node in _material_nodes(material):
        for parm in _guarded(node.parms) or ():
            path = _guarded(parm.path) or ""
            yield path.removeprefix(prefix), parm


def lock_material_parms(material):
    """Lock every parameter; return the relative paths that were locked already (kept on unlock)."""
    already = []
    for path, parm in _material_parms(material):
        if _guarded(parm.isLocked):
            already.append(path)
        else:
            _guarded(parm.lock, True)
    return sorted(already)


def unlock_material_parms(material, keep=()):
    """Exact inverse of lock_material_parms: unlock everything except the parms in `keep`."""
    keep = set(keep or ())
    for path, parm in _material_parms(material):
        if path not in keep and _guarded(parm.isLocked):
            _guarded(parm.lock, False)


def link_info(node):
    """The link record of a linked material node, or None."""
    if node is None:
        return None
    raw = _guarded(node.userData, LINK_USER_DATA)
    if not raw:
        return None
    try:
        info = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(info, dict) or not isinstance(info.get("source"), str) or not isinstance(info.get("name"), str):
        return None
    return info


def _comment_flag(node, shown):
    import hou

    flag = getattr(getattr(hou, "nodeFlag", None), "DisplayComment", None)
    setter = getattr(node, "setGenericFlag", None)
    if flag is not None and callable(setter):
        _guarded(setter, flag, shown)


# Linked materials show as a grey node (Houdini's colour for "not yours to edit").
LINKED_COLOR = (0.45, 0.45, 0.45)


def _set_color(node, rgb):
    import hou

    color, setter = getattr(hou, "Color", None), getattr(node, "setColor", None)
    if color is not None and callable(setter) and rgb is not None:
        _guarded(setter, color(tuple(float(c) for c in rgb)))


def _node_color(node):
    getter = getattr(node, "color", None)
    color = _guarded(getter) if callable(getter) else None
    rgb = _guarded(color.rgb) if color is not None else None
    return [round(float(c), 6) for c in rgb] if rgb else None


def mark_linked(material, info):
    """Make `material` a linked material: link user data, locked parameters, grey node, visible comment."""
    info = dict(info)
    info["source"] = str(info["source"]).replace("\\", "/")
    info["locked_before"] = lock_material_parms(material)
    info.setdefault("color_before", _node_color(material))
    _set_color(material, LINKED_COLOR)
    material.setUserData(LINK_USER_DATA, json.dumps(info, ensure_ascii=False, separators=(",", ":")))
    _guarded(material.setComment, "Linked from " + os.path.basename(info["source"]) + ": " + info["name"])
    _comment_flag(material, True)
    return info


def unmark_linked(material):
    """Make a linked material local (editable): exact inverse of mark_linked."""
    info = link_info(material)
    if info is None:
        return False
    unlock_material_parms(material, info.get("locked_before") or ())
    destroy = getattr(material, "destroyUserData", None)
    if callable(destroy):
        _guarded(destroy, LINK_USER_DATA)
    else:
        material.setUserData(LINK_USER_DATA, "")
    _guarded(material.setComment, "")
    _comment_flag(material, False)
    _set_color(material, info.get("color_before"))
    return True
