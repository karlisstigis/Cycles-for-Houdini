# SPDX-License-Identifier: GPL-3.0-or-later
# Blender component of Cycles for Houdini (see blender/README.md). This file is one of the scripts
# that run inside Blender and use its Python API, so it is published under the GNU General Public
# License, version 3 or later (blender/LICENSE). The Houdini integration (Apache-2.0) never imports
# it; the two sides exchange only files, command-line arguments and pipe messages.
"""Blender-side asset listing, preview extraction and named material and world export.

Runs inside Blender (worker process or asset-preferences script). Library
files are only read: previews and exported textures go to caller-owned
directories. Linked datablocks and their Library entries are removed after
each file so one worker can scan many files without accumulating data.

The PNG helpers and name hashing are pure Python and import without Blender.
"""

import array
import hashlib
import json
import os
import struct
import sys
import zlib
from pathlib import Path

NULL_UUID = "00000000-0000-0000-0000-000000000000"
MAX_PREVIEW_EDGE = 1024
MAX_ASSETS_PER_FILE = 8192
_SKIP_LISTING = ("version",)


class InvalidAssetFileError(ValueError):
    """A deterministic source-format/scan-limit failure, unlike temporary I/O."""


def validate_blend_header(source):
    """Reject definite non-blend data; let Blender validate supported encodings.

    An open/read failure deliberately remains an OSError. Compressed Blender
    files can have gzip or zstd headers instead of the uncompressed magic.
    """
    with open(source, "rb") as stream:
        header = stream.read(12)
    if not header.startswith((b"BLENDER", b"\x1f\x8b", b"\x28\xb5\x2f\xfd")):
        raise InvalidAssetFileError("Not a Blender file: " + str(source))


# --- pure helpers -----------------------------------------------------------


def _chunk(kind, data):
    return struct.pack("!I", len(data)) + kind + data + struct.pack("!I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def encode_png(width, height, rgba):
    """Encode top-down RGBA8 bytes as a PNG (filter 0 rows, deflate level 6)."""
    if type(width) is not int or type(height) is not int or width <= 0 or height <= 0:
        raise ValueError("PNG size must be positive integers")
    stride = width * 4
    rgba = bytes(rgba)
    if len(rgba) != stride * height:
        raise ValueError("PNG pixel buffer does not match its size")
    raw = b"".join(b"\0" + rgba[row * stride : (row + 1) * stride] for row in range(height))
    header = struct.pack("!IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", header) + _chunk(b"IDAT", zlib.compress(raw, 6)) + _chunk(b"IEND", b"")
    )


def flip_rows(rgba, width, height):
    """Bottom-up (Blender) rows to top-down (PNG) rows."""
    stride = width * 4
    return b"".join(rgba[row * stride : (row + 1) * stride] for row in reversed(range(height)))


def packed_pixels_to_rgba(values):
    """Blender packs RGBA8 per signed int; return R,G,B,A bytes independent of host byte order."""
    data = values if isinstance(values, array.array) and values.typecode == "i" else array.array("i", values)
    if sys.byteorder == "big":
        data = array.array("i", data)
        data.byteswap()
    return data.tobytes()


def signature_of(path):
    """Same stat tuple as blend_worker._signature, as a JSON-able list (or None)."""
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return [stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino]


def thumbnail_name(source, id_type, name, signature):
    """Deterministic preview file name; changes with the source file's signature."""
    identity = os.path.normcase(os.path.abspath(str(source)))
    payload = json.dumps([identity, str(id_type), str(name), signature], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:40] + ".png"


def _write_atomic(path, data):
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp-" + os.urandom(6).hex())
    try:
        temporary.write_bytes(data)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


# --- Blender side -----------------------------------------------------------


def _abspath(bpy, library):
    return os.path.normcase(str(Path(bpy.path.abspath(library.filepath, library=library.parent)).resolve()))


def _drop_libraries(bpy, libraries):
    libraries = [library for library in libraries if library is not None]
    if libraries:
        # Removing a Library also frees every ID linked from it.
        bpy.data.batch_remove(libraries)


def _preview(ident, thumbnail_dir, filename):
    preview = getattr(ident, "preview", None)
    if preview is None:
        return None, None
    width, height = (int(v) for v in preview.image_size)
    if width <= 0 or height <= 0 or width > MAX_PREVIEW_EDGE or height > MAX_PREVIEW_EDGE:
        return None, None
    values = array.array("i", bytes(4 * width * height))
    try:
        preview.image_pixels.foreach_get(values)
    except (AttributeError, TypeError, RuntimeError):
        values = array.array("i", preview.image_pixels)
    if len(values) != width * height:
        return None, None
    rgba = packed_pixels_to_rgba(values)
    if not any(rgba[3::4]):
        # A preview slot that was never rendered is fully transparent.
        return None, None
    target = Path(thumbnail_dir) / filename
    if not target.is_file():
        _write_atomic(target, encode_png(width, height, flip_rows(rgba, width, height)))
    return filename, [width, height]


def _meta(asset_data):
    catalog = str(asset_data.catalog_id or "")
    return {
        "catalog_id": "" if catalog == NULL_UUID else catalog,
        "tags": [str(tag.name) for tag in asset_data.tags],
        "author": str(asset_data.author or ""),
        "description": str(asset_data.description or ""),
        "license": str(asset_data.license or ""),
        "copyright": str(asset_data.copyright or ""),
    }


def scan_file(path, thumbnail_dir, signature=None):
    """List marked assets of one .blend file and write their previews.

    Returns {"source", "signature", "assets": [...], "warnings": [...]}. Assets
    are linked (never appended), read, and removed with their Library.
    """
    import bpy

    source = str(Path(path).resolve())
    if not Path(source).is_file():
        raise FileNotFoundError("Blender file not found: " + source)
    validate_blend_header(source)
    if signature is None:
        signature = signature_of(source)
    thumbnail_dir = Path(thumbnail_dir).resolve()
    thumbnail_dir.mkdir(parents=True, exist_ok=True)
    key = os.path.normcase(source)
    # A previous failed cleanup must not make the load reuse stale data.
    _drop_libraries(bpy, [library for library in bpy.data.libraries if _abspath(bpy, library) == key])
    before = {library.as_pointer() for library in bpy.data.libraries}
    assets, warnings = [], []
    try:
        with bpy.data.libraries.load(source, link=True, assets_only=True) as (available, selected):
            categories = []
            for attribute in dir(available):
                if attribute.startswith("_") or attribute in _SKIP_LISTING:
                    continue
                names = getattr(available, attribute)
                if isinstance(names, list) and names:
                    categories.append(attribute)
                    setattr(selected, attribute, list(names))
            requested = sum(len(getattr(available, attribute)) for attribute in categories)
            if requested > MAX_ASSETS_PER_FILE:
                raise InvalidAssetFileError(f"Blender file has more than {MAX_ASSETS_PER_FILE} assets: {source}")
        for attribute in sorted(categories):
            for ident in getattr(selected, attribute):
                if ident is None:
                    warnings.append("Could not link an asset from " + attribute)
                    continue
                asset_data = ident.asset_data
                if asset_data is None:
                    continue
                id_type = str(ident.id_type)
                name = str(ident.name)
                record = {
                    "name": name,
                    "id_type": id_type,
                    "meta": _meta(asset_data),
                    "catalog_simple_name": str(asset_data.catalog_simple_name or ""),
                    "object_type": str(ident.type) if id_type == "OBJECT" else None,
                    "preview": None,
                    "preview_size": None,
                }
                try:
                    record["preview"], record["preview_size"] = _preview(
                        ident, thumbnail_dir, thumbnail_name(source, id_type, name, signature)
                    )
                except Exception as error:  # noqa: BLE001 - a bad preview must not hide the asset
                    warnings.append(f"Preview of {id_type} {name}: {error}")
                assets.append(record)
    finally:
        _drop_libraries(bpy, [library for library in bpy.data.libraries if library.as_pointer() not in before])
    assets.sort(key=lambda item: (item["id_type"], item["name"]))
    return {"source": source, "signature": signature, "assets": assets, "warnings": warnings}


def _data_collections(bpy):
    result = []
    for prop in bpy.types.BlendData.bl_rna.properties:
        if prop.type == "COLLECTION" and prop.identifier != "libraries":
            result.append(prop.identifier)
    return result


def _all_ids(bpy):
    found = set()
    for attribute in _data_collections(bpy):
        for ident in getattr(bpy.data, attribute, ()):
            found.add(ident.as_pointer())
    return found


def export_named_materials(path, names, output_dir, textures="copy"):
    """Append named materials (link=False) and export each independently.

    Returns {"records": [...], "errors": {name: message}}. One unsupported
    material never fails the others. Appended data is removed afterwards.
    textures: "copy" or "in_place" (blend_material_export.export_materials).
    """
    import bpy

    from . import blend_material_export as exporter

    source = str(Path(path).resolve())
    if not Path(source).is_file():
        raise FileNotFoundError("Blender file not found: " + source)
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    names = list(dict.fromkeys(str(name) for name in names))
    records, errors = [], {}
    ids_before = _all_ids(bpy)
    libraries_before = {library.as_pointer() for library in bpy.data.libraries}
    try:
        with bpy.data.libraries.load(source, link=False) as (available, selected):
            present = set(available.materials)
            wanted = [name for name in names if name in present]
            selected.materials = list(wanted)
        for name in names:
            if name not in present:
                errors[name] = "Material not found in " + source + ": " + name
        for name, material in zip(wanted, selected.materials, strict=False):
            if material is None:
                errors[name] = "Blender could not append material: " + name
                continue
            try:
                result = exporter.export_materials([material], str(output_dir), textures=textures)
                exported = result.get("materials", [])
                if len(exported) != 1:
                    raise ValueError(f"Material export returned {len(exported)} records")
                record = dict(exported[0])
                # The appended copy may be renamed on collision; keep the asset name.
                record["name"] = name
                records.append(record)
            except Exception as error:  # noqa: BLE001 - isolate one unsupported material
                errors[name] = str(error) or type(error).__name__
    finally:
        created = [
            ident
            for attribute in _data_collections(bpy)
            for ident in getattr(bpy.data, attribute, ())
            if ident.as_pointer() not in ids_before
        ]
        if created:
            bpy.data.batch_remove(created)
        _drop_libraries(
            bpy, [library for library in bpy.data.libraries if library.as_pointer() not in libraries_before]
        )
    return {"records": records, "errors": errors}


def export_named_worlds(path, names, output_dir, textures="in_place"):
    """Append named worlds (link=False) and export each (blend_world_export.export_world).

    Returns {"records": [...], "errors": {name: message}}, like export_named_materials. A
    world that is neither simple nor translatable is a record of kind "unsupported", not an
    error; errors are worlds that are missing or could not be appended or read.
    """
    import bpy

    from . import blend_world_export as exporter

    source = str(Path(path).resolve())
    if not Path(source).is_file():
        raise FileNotFoundError("Blender file not found: " + source)
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    names = list(dict.fromkeys(str(name) for name in names))
    records, errors = [], {}
    ids_before = _all_ids(bpy)
    libraries_before = {library.as_pointer() for library in bpy.data.libraries}
    try:
        with bpy.data.libraries.load(source, link=False) as (available, selected):
            present = set(available.worlds)
            wanted = [name for name in names if name in present]
            selected.worlds = list(wanted)
        for name in names:
            if name not in present:
                errors[name] = "World not found in " + source + ": " + name
        for name, world in zip(wanted, selected.worlds, strict=False):
            if world is None:
                errors[name] = "Blender could not append world: " + name
                continue
            try:
                record = exporter.export_world(world, str(output_dir), textures=textures)
                record["name"] = name  # the appended copy may be renamed on collision
                if "material" in record:
                    record["material"]["name"] = name
                records.append(record)
            except Exception as error:  # noqa: BLE001 - isolate one world
                errors[name] = str(error) or type(error).__name__
    finally:
        created = [
            ident
            for attribute in _data_collections(bpy)
            for ident in getattr(bpy.data, attribute, ())
            if ident.as_pointer() not in ids_before
        ]
        if created:
            bpy.data.batch_remove(created)
        _drop_libraries(
            bpy, [library for library in bpy.data.libraries if library.as_pointer() not in libraries_before]
        )
    return {"records": records, "errors": errors}


def dump_asset_libraries():
    """User asset libraries from the loaded preferences (needs a non-factory start)."""
    import bpy

    libraries = bpy.context.preferences.filepaths.asset_libraries
    if not len(libraries) and (not bpy.app.factory_startup or not bpy.app.binary_path):
        # Blender's Python module (no binary path) starts without reading
        # userpref.blend; a non-factory application start already has it.
        try:
            bpy.ops.wm.read_userpref()
        except RuntimeError:
            pass
        libraries = bpy.context.preferences.filepaths.asset_libraries
    result = []
    for library in libraries:
        path = str(getattr(library, "path", "") or "")
        if not path:
            continue
        result.append(
            {
                "name": str(library.name),
                "path": path,
                "import_method": str(getattr(library, "import_method", "APPEND") or "APPEND"),
                "enabled": bool(getattr(library, "enabled", True)),
                "remote": bool(getattr(library, "use_remote_url", False)),
            }
        )
    return result


def print_asset_libraries(marker):
    """Write one marker-prefixed JSON line; Blender's own stdout logs are ignored by the host."""
    try:
        payload = {"ok": True, "libraries": dump_asset_libraries()}
    except Exception as error:  # noqa: BLE001 - reported to the host as data
        payload = {"ok": False, "error": str(error) or type(error).__name__}
    sys.stdout.write("\n" + marker + json.dumps(payload, ensure_ascii=True, separators=(",", ":")) + "\n")
    sys.stdout.flush()
