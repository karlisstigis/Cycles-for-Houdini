# SPDX-License-Identifier: Apache-2.0
"""Asset records: a Blender remote listing v1 asset entry plus a `ch` block."""

import os

from .catalogs import normalize_uuid

# Blender `ID.id_type` spellings, as used by the remote listing `id_type` field.
ID_TYPES = (
    "ACTION",
    "ARMATURE",
    "BRUSH",
    "CACHEFILE",
    "CAMERA",
    "COLLECTION",
    "CURVE",
    "CURVES",
    "FONT",
    "GREASEPENCIL",
    "GREASEPENCIL_V3",
    "IMAGE",
    "KEY",
    "LATTICE",
    "LIBRARY",
    "LIGHT",
    "LIGHT_PROBE",
    "LINESTYLE",
    "MASK",
    "MATERIAL",
    "MESH",
    "META",
    "MOVIECLIP",
    "NODETREE",
    "OBJECT",
    "PAINTCURVE",
    "PALETTE",
    "PARTICLE",
    "POINTCLOUD",
    "SCENE",
    "SCREEN",
    "SOUND",
    "SPEAKER",
    "TEXT",
    "TEXTURE",
    "VOLUME",
    "WINDOWMANAGER",
    "WORKSPACE",
    "WORLD",
)
SUPPORTED_IMPORT_TYPES = ("MATERIAL", "OBJECT", "COLLECTION")
IMPORTABLE_STATES = ("yes", "no", "unknown")
PROVIDERS = ("worker", "listing")
META_TEXT_FIELDS = ("author", "description", "license", "copyright")


def _text(value):
    return value if isinstance(value, str) else ""


def default_importability(id_type, object_type=None):
    """Static verdict from the type alone; deeper checks happen at import time."""
    if id_type not in SUPPORTED_IMPORT_TYPES:
        return {"state": "no", "reason": "Blender " + id_type.lower() + " assets cannot be imported into Houdini"}
    if id_type == "OBJECT" and object_type not in (None, "MESH"):
        return {"state": "unknown", "reason": "Only mesh geometry is referenced; this is a " + object_type.lower()}
    return {"state": "unknown", "reason": ""}


def make_key(rel_file, id_type, name):
    return rel_file + "|" + id_type + "|" + name


def make_record(
    name,
    id_type,
    rel_file,
    *,
    source,
    meta=None,
    thumbnail=None,
    thumbnail_hash="",
    catalog_simple_name="",
    object_type=None,
    provider="worker",
    importable=None,
    files=None,
):
    """Build a record. `rel_file` is POSIX and relative to the library root;
    `thumbnail` is an absolute image path or None."""
    meta = meta if isinstance(meta, dict) else {}
    id_type = str(id_type).upper()
    tags = meta.get("tags")
    record_meta = {
        "catalog_id": normalize_uuid(meta.get("catalog_id")),
        "tags": [tag for tag in tags if isinstance(tag, str)] if isinstance(tags, list) else [],
    }
    for name_field in META_TEXT_FIELDS:
        record_meta[name_field] = _text(meta.get(name_field))
    rel_file = rel_file.replace("\\", "/")
    object_type = object_type.upper() if isinstance(object_type, str) and object_type else None
    return {
        "name": name,
        "id_type": id_type,
        "files": [rel_file] + [f for f in (files or [])[1:] if isinstance(f, str)],
        "thumbnail": {"url": os.fspath(thumbnail).replace("\\", "/"), "hash": _text(thumbnail_hash)}
        if thumbnail
        else None,
        "meta": record_meta,
        "ch": {
            "key": make_key(rel_file, id_type, name),
            # Houdini convention: forward slashes in every path parm and record, also on Windows.
            "source": os.fspath(source).replace("\\", "/") if source else source,
            "catalog_simple_name": _text(catalog_simple_name),
            "object_type": object_type if id_type == "OBJECT" else None,
            "importable": dict(importable) if importable else default_importability(id_type, object_type),
            "provider": provider if provider in PROVIDERS else "worker",
        },
    }


def record_key(record):
    ch = record.get("ch") if isinstance(record, dict) else None
    if isinstance(ch, dict) and isinstance(ch.get("key"), str):
        return ch["key"]
    return make_key(record["files"][0], record["id_type"], record["name"])


def validate_record(record):
    """Shape check for records read back from cache files."""
    try:
        if not isinstance(record["name"], str) or not record["name"]:
            return False
        if not isinstance(record["id_type"], str) or not record["id_type"]:
            return False
        files = record["files"]
        if not isinstance(files, list) or not files or not all(isinstance(f, str) and f for f in files):
            return False
        thumbnail = record["thumbnail"]
        if thumbnail is not None and not (
            isinstance(thumbnail, dict)
            and isinstance(thumbnail.get("url"), str)
            and isinstance(thumbnail.get("hash"), str)
        ):
            return False
        meta = record["meta"]
        if not isinstance(meta.get("catalog_id"), str) or not isinstance(meta.get("tags"), list):
            return False
        if not all(isinstance(tag, str) for tag in meta["tags"]):
            return False
        if not all(isinstance(meta.get(name), str) for name in META_TEXT_FIELDS):
            return False
        ch = record["ch"]
        if not isinstance(ch.get("key"), str) or not isinstance(ch.get("source"), str):
            return False
        if not isinstance(ch.get("catalog_simple_name"), str):
            return False
        if ch.get("object_type") is not None and not isinstance(ch["object_type"], str):
            return False
        importable = ch.get("importable")
        if not isinstance(importable, dict) or importable.get("state") not in IMPORTABLE_STATES:
            return False
        if not isinstance(importable.get("reason"), str):
            return False
        return ch.get("provider") in PROVIDERS
    except (KeyError, TypeError, AttributeError):
        return False
