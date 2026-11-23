# SPDX-License-Identifier: Apache-2.0
"""Convert Blender objects and collections into native Houdini geometry.

Contract: docs/dev/blend-import.md. Three steps, so Blender never blocks Houdini:

- ``import_snapshot(source, selection, **options)``: host thread (HOM). Validates
  the options, expands folders ($HIP, untitled-scene fallback) and captures the
  Blender discovery inputs.
- ``prepare_import(snapshot, cancelled=None)``: any thread, no HOM. Runs the Blender
  worker (evaluated depsgraph: modifiers, geometry nodes; material textures in place)
  and converts its arrays to Houdini-ready arrays (axes, winding, attribute names).
  Messages are collected in the payload, never reported from here.
- ``build_import(payload)``: host thread (HOM), one undo group. Writes geometry and
  creates the nodes: a Cycles Blender File node per asset part (Link or Converted) or
  one Cycles Blender Hierarchy object; stock File / Stash / reference networks when
  those assets are not installed. Builds the materials.

Update (blend-import.md: Asset nodes): ``update_snapshot(nodes)`` (host) reads the nodes' parameters,
``prepare_update`` (any thread) runs the same export, ``sync_import(payload, nodes)``
(host, one undo group) rewrites the files and updates the existing nodes instead of
creating them. ``update_import(nodes)`` runs the three steps synchronously.

Files go to ``<asset root>/<blend>/geo/<asset>/`` with one ``cycles_import.json`` per
blend folder (blend_assets_layout; blend-import.md: Folder layout, Import record). Packed images go to
``<asset root>/<blend>/tex/``; every other texture stays where the .blend points.

``import_blend(...)`` runs the three steps synchronously.

Geometry conventions: Blender local coordinates are converted with the exporter's
axis matrix (Z-up -> Y-up), polygon winding is reversed (Blender counter-clockwise,
Houdini clockwise front faces) keeping each polygon's first vertex, and object or
instance transforms become ``C * M * C^-1``.
"""

import contextlib
import hashlib
import json
import math
import os
import re
import shutil
import tempfile
from pathlib import Path

import numpy as np

from . import blend_assets_layout as layout
from . import blend_file_node as file_node
from .blend_file_node import MATERIAL_USER_DATA, SOURCE_USER_DATA

STORAGES = ("reference", "embedded", "files")
STRUCTURES = ("hierarchy", "packed")
MERGED_REMOVED = (
    'The Merged structure was removed: import "packed" and set Load As to Unpacked on the '
    "Cycles Blender File node (Unpack Iterations controls nested instances)."
)
MATERIAL_MODES = ("link", "append", "none")
ON_SHARED = ("replace", "cancel")
DEFAULT_ASSET_ROOT = "$HIP/" + layout.ROOT_FOLDER
# obsolete: the importer dialog's stored default until the importers switch; read as DEFAULT_ASSET_ROOT.
OBSOLETE_ASSET_ROOT = "$HIP/geo/assets_blender"
UNDO_LABEL = "Import Blender assets"
NODE_USER_DATA = "cycles_blender_import"
SEQUENCE_WARNING = "Frame sequences write one geometry file per frame and can be large and slow."
# Why geometry-nodes instance attributes do not arrive (blend_export: blend.instance_attributes).
INSTANCE_ATTRIBUTES_REASON = "instance attributes are not carried onto packed primitives yet"
# The import report's line when textures were left where the .blend points (blend-import.md: Textures in place).
TEXTURES_IN_PLACE = (
    "Textures stay in place: {count} texture file(s) are read from where the .blend points. "
    "Cycles > Collect Textures copies them into the project."
)

# Exporter axis matrix (column vectors, row-major): Blender (x, y, z) -> Houdini (x, z, -y).
_CONVERT = np.array(
    ((1.0, 0.0, 0.0, 0.0), (0.0, 0.0, 1.0, 0.0), (0.0, -1.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0)), dtype=np.float64
)
_CONVERT_INV = np.linalg.inv(_CONVERT)

# Houdini attribute names with a fixed meaning. A Blender attribute of the same
# name is written as blend_<name> unless it is the same meaning (_MAPPED).
RESERVED = frozenset(
    (
        "P N Cd uv Alpha id pscale width orient name path v up rot scale trans pivot transform instance "
        "instancefile instancepath piece class material shop_materialpath material_override "
        "material_stylesheet rest Pw w accel force mass age life pstate blender_material creaseweight cornerweight"
    ).split()
)
_UV_NAME = re.compile(r"uv\d*")
# (Blender name, domain, data type) -> Houdini name with the same meaning.
_MAPPED = {
    ("id", "POINT", "INT"): "id",
    ("velocity", "POINT", "FLOAT_VECTOR"): "v",
    ("rest_position", "POINT", "FLOAT_VECTOR"): "rest",
}
# Boolean edge attributes that become edge groups.
EDGE_GROUPS = {"sharp_edge": "sharp_edges", "uv_seam": "seams", "seam": "seams"}
# Represented otherwise: material_index by mat_* groups, sharp_face by the vertex normals.
_REPRESENTED = {("material_index", "FACE"), ("sharp_face", "FACE"), ("material_index", "CURVE")}
_TYPES = {
    # data type -> (kind, width, vector)
    "FLOAT": ("float", 1, False),
    "FLOAT2": ("float", 2, False),
    "FLOAT_VECTOR": ("float", 3, True),
    "FLOAT_COLOR": ("float", 4, False),
    "BYTE_COLOR": ("float", 4, False),
    "INT": ("int", 1, False),
    "INT8": ("int", 1, False),
    "INT32_2D": ("int", 2, False),
    "BOOLEAN": ("int", 1, False),
    "INT16_2D": ("int", 2, False),
    "FLOAT4": ("float", 4, False),
    "QUATERNION": ("float", 4, False),
    "FLOAT4X4": ("float", 16, False),
    "STRING": ("string", 1, False),
}
_DOMAINS = {"POINT": "point", "CORNER": "vertex", "FACE": "prim", "CURVE": "prim"}
# Houdini type info for Blender types that have one (written by write_geometry).
_TYPE_INFO = {"QUATERNION": "quaternion", "FLOAT4X4": "matrix"}
_NULL_TYPES = frozenset(("EMPTY", "ARMATURE", "CAMERA", "LIGHT", "LIGHT_PROBE", "SPEAKER", "LATTICE"))


class BlendImportError(ValueError):
    """A user-facing reason why a Blender import could not run."""


class ImportCancelled(RuntimeError):
    """The artist cancelled the import while Blender was working."""


# ------------------------------------------------------------------ names


def node_name(name, fallback="blender_asset"):
    """A valid Houdini node name for a Blender name (spaces and symbols become "_")."""
    value = re.sub(r"[^A-Za-z0-9_]+", "_", name or "").strip("_")
    if not value:
        value = fallback
    if value[0].isdigit():
        value = "_" + value
    return value[:120]


def free_name(parent, name, fallback="blender_asset"):
    """node_name(name), suffixed _1, _2, ... when taken (never Houdini's trailing-number bump)."""
    from .blend_library_materials import free_child_name

    return free_child_name(parent, node_name(name, fallback))


def safe_component(value, fallback):
    """A plain [A-Za-z0-9_.-] name component (folder and file names use blend_assets_layout)."""
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "")).strip("._-")
    return text[:64] or fallback


def _identifier(name, fallback="attribute"):
    value = re.sub(r"[^A-Za-z0-9_]+", "_", name or "").strip("_") or fallback
    return "_" + value if value[0].isdigit() else value


def attribute_name(name, used, *, mapped=None):
    """Houdini name for a Blender attribute: sanitised, blend_<name> when it is a reserved name
    (the converter's own P, N, uv, Cd, Alpha, ... are all reserved), then unique (_2, _3, ...)."""
    if mapped is not None and mapped not in used:
        used.add(mapped)
        return mapped
    value = _identifier(name)
    if value in RESERVED:
        value = "blend_" + value
    candidate, index = value, 2
    while candidate in used:
        candidate, index = value + "_" + str(index), index + 1
    used.add(candidate)
    return candidate


def uv_set_name(name, used):
    """Houdini name for a UV set kept under its Blender name: sanitised, unique, blend_
    prefixed only for reserved names (uv2, uv3, ... stay: the converter no longer numbers maps)."""
    value = _identifier(name, "uvmap")
    if value in RESERVED or value.startswith("mat_"):
        value = "blend_" + value
    candidate, index = value, 2
    while candidate in used:
        candidate, index = value + "_" + str(index), index + 1
    used.add(candidate)
    return candidate


def material_group(name, used=None):
    """Primitive group name for a Blender material slot: mat_<SanitisedName>."""
    value = "mat_" + _identifier(name, "material")
    if used is None:
        return value
    candidate, index = value, 2
    while candidate in used:
        candidate, index = value + "_" + str(index), index + 1
    used.add(candidate)
    return candidate


# ------------------------------------------------------------------ report


class Report:
    """What the conversion changed or dropped, per selection item (stored in the manifest)."""

    def __init__(self):
        self.lossy, self.skipped, self.renamed, self.notes = [], [], {}, []
        self.not_imported = {}  # object -> {reason: [attribute names]} (attribute_lines)

    @staticmethod
    def _add(target, text):
        if text not in target:
            target.append(text)

    def lossy_item(self, text):
        self._add(self.lossy, text)

    def skip(self, text):
        self._add(self.skipped, text)

    def skip_attribute(self, owner, name, detail, reason):
        """An attribute that does not arrive: listed among the skipped items ("name (detail):
        reason") and, per object, in the line the artist sees (attribute_lines)."""
        self.skip(f"{name} ({detail}): {reason}")
        names = self.not_imported.setdefault(owner or "?", {}).setdefault(reason, [])
        if name not in names:
            names.append(name)

    def note(self, text):
        self._add(self.notes, text)

    def rename(self, blender, houdini):
        if blender != houdini:
            self.renamed[blender] = houdini

    def as_dict(self):
        return dict(
            lossy=list(self.lossy),
            skipped=list(self.skipped),
            renamed=dict(self.renamed),
            notes=self.notes,
            not_imported={owner: dict(reasons) for owner, reasons in self.not_imported.items()},
        )


def attribute_lines(report):
    """One line per object naming the attributes that were not imported and why, e.g.
    "Rock: attributes not imported: wear, seam_id (Houdini has no edge attributes)".
    report: a Report or its as_dict()."""
    data = report.as_dict() if isinstance(report, Report) else (report or {})
    lines = []
    for owner, reasons in (data.get("not_imported") or {}).items():
        parts = [", ".join(names) + " (" + reason + ")" for reason, names in reasons.items() if names]
        if parts:
            lines.append(f"{owner}: attributes not imported: " + "; ".join(parts))
    return lines


# ------------------------------------------------------------------ geometry model


class Attrib:
    """One attribute: owner point|vertex|prim, kind float|int|string, values (n, width) or [str].
    info: Houdini type info written with it ("quaternion", "matrix") or None."""

    __slots__ = ("owner", "kind", "values", "normal", "vector", "default", "info")

    def __init__(self, owner, kind, values, *, normal=False, vector=False, default=None, info=None):
        self.owner, self.kind, self.normal, self.vector, self.info = owner, kind, normal, vector, info
        if kind == "string":
            self.values = list(values)
        else:
            array = np.asarray(values, dtype=np.float64 if kind == "float" else np.int64)
            self.values = array.reshape(len(array), -1) if array.ndim != 2 else array
        self.default = default

    @property
    def width(self):
        return 1 if self.kind == "string" else int(self.values.shape[1])

    def fill(self):
        if self.default is not None:
            return self.default
        return "" if self.kind == "string" else 0


class Geo:
    """Houdini-ready geometry: points, closed polygons, open polylines, packed primitives.

    Primitive order is polygons, polylines, packed. Every packed primitive owns one
    point (``packed_points``) and one vertex; its transform (4x4, column vectors)
    excludes the translation, which is the point position.
    """

    def __init__(self):
        self.P = np.zeros((0, 3), dtype=np.float64)
        self.poly_counts = np.zeros(0, dtype=np.int64)
        self.poly_points = np.zeros(0, dtype=np.int64)
        self.line_counts = np.zeros(0, dtype=np.int64)
        self.line_points = np.zeros(0, dtype=np.int64)
        self.packed_children = []  # child keys
        self.packed_points = np.zeros(0, dtype=np.int64)
        self.packed_matrices = np.zeros((0, 4, 4), dtype=np.float64)
        self.attribs = {}
        self.prim_groups = {}  # name -> prim indices
        self.edge_groups = {}  # name -> (k, 2) point index pairs
        self.material_groups = {}  # group name -> Blender material key (not written)
        self.subdivision = None  # render-time subdivision settings of an unapplied Blender Subsurf

    @property
    def num_points(self):
        return len(self.P)

    @property
    def num_prims(self):
        return len(self.poly_counts) + len(self.line_counts) + len(self.packed_children)

    @property
    def num_vertices(self):
        return len(self.poly_points) + len(self.line_points) + len(self.packed_children)

    def count(self, owner):
        return {"point": self.num_points, "vertex": self.num_vertices, "prim": self.num_prims}[owner]


def _blank(attrib, count):
    if attrib.kind == "string":
        return [attrib.fill()] * count
    values = np.empty((count, attrib.width), dtype=attrib.values.dtype)
    values[:] = attrib.fill()
    return values


def _concat(attrib, chunks):
    if attrib.kind == "string":
        return [value for chunk in chunks for value in chunk]
    return np.concatenate(chunks, axis=0) if chunks else attrib.values[:0]


def winding_permutation(counts):
    """Corner order that reverses every polygon but keeps its first corner: [0, n-1, ..., 1]."""
    counts = np.asarray(counts, dtype=np.int64)
    total = int(counts.sum())
    if not total:
        return np.zeros(0, dtype=np.int64)
    starts = np.repeat(np.cumsum(counts) - counts, counts)
    local = np.arange(total, dtype=np.int64) - starts
    sizes = np.repeat(counts, counts)
    reversed_local = np.where(local == 0, 0, sizes - local)
    return starts + reversed_local


def _apply_points(points, matrix):
    return points @ matrix[:3, :3].T + matrix[:3, 3]


def _apply_vectors(vectors, matrix):
    return vectors @ matrix[:3, :3].T


def _apply_normals(normals, matrix):
    linear = matrix[:3, :3]
    try:
        normal_matrix = np.linalg.inv(linear).T
    except np.linalg.LinAlgError:
        return normals
    result = normals @ normal_matrix.T
    length = np.linalg.norm(result, axis=1, keepdims=True)
    return np.divide(result, length, out=np.zeros_like(result), where=length > 0)


def matrix_from_flat(flat):
    return np.asarray(flat, dtype=np.float64).reshape(4, 4)


def houdini_world(blender_transform):
    """Houdini world matrix (column vectors) for an exporter transform C * M_world: C * M * C^-1."""
    return matrix_from_flat(blender_transform) @ _CONVERT_INV


def row_matrix(matrix):
    """Houdini's hou.Matrix4 layout (row vectors) for a column-vector matrix."""
    return [[float(value) for value in row] for row in np.asarray(matrix).T]


def transformed(geo, matrix):
    """A copy of geo with points, normals and vector attributes transformed."""
    if np.allclose(matrix, np.eye(4)):
        return geo
    result = Geo()
    result.__dict__.update({key: value for key, value in geo.__dict__.items() if key != "attribs"})
    result.P = _apply_points(geo.P, matrix)
    result.attribs = {}
    for name, attrib in geo.attribs.items():
        copy = Attrib(
            attrib.owner, attrib.kind, attrib.values, normal=attrib.normal, vector=attrib.vector, info=attrib.info
        )
        copy.default = attrib.default
        if attrib.normal and attrib.width == 3:
            copy.values = _apply_normals(attrib.values, matrix)
        elif attrib.vector and attrib.width == 3:
            copy.values = _apply_vectors(attrib.values, matrix)
        result.attribs[name] = copy
    if len(geo.packed_children):
        result.packed_matrices = np.array([matrix @ m for m in geo.packed_matrices])
        result.packed_matrices[:, :3, 3] = 0.0
    return result


def merge(parts, report=None):
    """Merge Geo parts (already transformed) into one; attributes are unioned with defaults."""
    parts = [part for part in parts if part is not None]
    result = Geo()
    if not parts:
        return result
    point_offsets = np.cumsum([0] + [part.num_points for part in parts])
    result.P = np.concatenate([part.P for part in parts], axis=0)
    result.poly_counts = np.concatenate([part.poly_counts for part in parts])
    result.poly_points = np.concatenate(
        [part.poly_points + offset for part, offset in zip(parts, point_offsets[:-1], strict=True)]
    )
    result.line_counts = np.concatenate([part.line_counts for part in parts])
    result.line_points = np.concatenate(
        [part.line_points + offset for part, offset in zip(parts, point_offsets[:-1], strict=True)]
    )
    result.packed_points = np.concatenate(
        [part.packed_points + offset for part, offset in zip(parts, point_offsets[:-1], strict=True)]
    )
    result.packed_children = [child for part in parts for child in part.packed_children]
    result.packed_matrices = (
        np.concatenate([part.packed_matrices for part in parts], axis=0)
        if result.packed_children
        else np.zeros((0, 4, 4))
    )
    # Attribute layout: union, first definition wins; a conflicting one is reported and dropped.
    layout = {}
    for part in parts:
        for name, attrib in part.attribs.items():
            known = layout.get(name)
            if known is None:
                layout[name] = attrib
            elif report is not None and (known.owner, known.kind, known.width) != (
                attrib.owner,
                attrib.kind,
                attrib.width,
            ):
                report.lossy_item(f"Attribute {name} has different types on different objects; kept the first")

    # Prim/vertex rows: polygons, then polylines, then packed, for every part in turn.
    def rows(part, owner, values):
        if owner == "point":
            return [values]
        if owner == "vertex":
            sections = (len(part.poly_points), len(part.line_points), len(part.packed_children))
        else:
            sections = (len(part.poly_counts), len(part.line_counts), len(part.packed_children))
        bounds = np.cumsum((0,) + sections)
        return [values[bounds[i] : bounds[i + 1]] for i in range(3)]

    for name, template in layout.items():
        per_section = [[], [], []] if template.owner != "point" else [[]]
        for part in parts:
            attrib = part.attribs.get(name)
            if attrib is None or (attrib.owner, attrib.kind, attrib.width) != (
                template.owner,
                template.kind,
                template.width,
            ):
                attrib = Attrib(template.owner, template.kind, _blank(template, part.count(template.owner)))
            for index, chunk in enumerate(rows(part, template.owner, attrib.values)):
                per_section[index].append(chunk)
        merged = Attrib(
            template.owner,
            template.kind,
            _concat(template, [chunk for section in per_section for chunk in section]),
            normal=template.normal,
            vector=template.vector,
            default=template.default,
            info=template.info,
        )
        result.attribs[name] = merged
    # Prim index remapping: new order is all polygons, all polylines, all packed.
    poly_offsets = np.cumsum([0] + [len(part.poly_counts) for part in parts])
    line_offsets = np.cumsum([0] + [len(part.line_counts) for part in parts])
    packed_offsets = np.cumsum([0] + [len(part.packed_children) for part in parts])
    total_polys, total_lines = int(poly_offsets[-1]), int(line_offsets[-1])
    for index, part in enumerate(parts):
        polys, lines = len(part.poly_counts), len(part.line_counts)
        for group, members in part.prim_groups.items():
            members = np.asarray(members, dtype=np.int64)
            mapped = np.where(
                members < polys,
                members + poly_offsets[index],
                np.where(
                    members < polys + lines,
                    members - polys + total_polys + line_offsets[index],
                    members - polys - lines + total_polys + total_lines + packed_offsets[index],
                ),
            )
            result.prim_groups[group] = np.concatenate([result.prim_groups.get(group, np.zeros(0, np.int64)), mapped])
        for group, pairs in part.edge_groups.items():
            shifted = np.asarray(pairs, dtype=np.int64).reshape(-1, 2) + point_offsets[index]
            previous = result.edge_groups.get(group, np.zeros((0, 2), np.int64))
            result.edge_groups[group] = np.concatenate([previous, shifted])
        result.material_groups.update(part.material_groups)
    settings = {json.dumps(part.subdivision, sort_keys=True) for part in parts if part.num_prims}
    if len(settings) == 1:
        result.subdivision = next(part.subdivision for part in parts if part.num_prims)
    return result


def set_prim_strings(geo, name, value):
    """Give every primitive of geo the string attribute value (name/path per object)."""
    attrib = Attrib("prim", "string", [value] * geo.num_prims, default="")
    geo.attribs[name] = attrib
    return geo


def with_prim_strings(geo, **values):
    copy = Geo()
    copy.__dict__.update(geo.__dict__)
    copy.attribs = dict(geo.attribs)
    for name, value in values.items():
        set_prim_strings(copy, name, value)
    return copy


def packed(children, keys, matrices, strings=None):
    """Geo of packed primitives: children[key] are shared embedded geometries."""
    geo = Geo()
    matrices = np.asarray(matrices, dtype=np.float64).reshape(-1, 4, 4)
    geo.P = matrices[:, :3, 3].copy() if len(matrices) else np.zeros((0, 3))
    geo.packed_children = list(keys)
    geo.packed_points = np.arange(len(keys), dtype=np.int64)
    geo.packed_matrices = matrices.copy()
    if len(matrices):
        geo.packed_matrices[:, :3, 3] = 0.0
    for name, values in (strings or {}).items():
        geo.attribs[name] = Attrib("prim", "string", values, default="")
    return geo


# ------------------------------------------------------------------ Blender records -> Geo


class _Builder:
    """Per-Geo attribute naming state and helpers."""

    def __init__(self, geo, report, convert_vectors=True):
        self.geo, self.report, self.convert_vectors = geo, report, convert_vectors
        self.used = set()

    def add(
        self,
        blender_name,
        owner,
        kind,
        values,
        *,
        mapped=None,
        normal=False,
        vector=False,
        default=None,
        label=None,
        info=None,
    ):
        name = attribute_name(blender_name, self.used, mapped=mapped)
        self.report.rename(label or blender_name, name)
        self.geo.attribs[name] = Attrib(owner, kind, values, normal=normal, vector=vector, default=default, info=info)
        return name

    def fixed(self, name, owner, kind, values, **options):
        self.used.add(name)
        self.geo.attribs[name] = Attrib(owner, kind, values, **options)
        return name

    def generic(self, field, values, owner):
        """One Blender generic attribute (exporter record) already reordered for its owner."""
        data_type = field["data_type"]
        kind, _, vector = _TYPES[data_type]
        if data_type == "STRING":
            # values: the strings (object array; padding rows of polylines are not strings -> "").
            strings = [value if isinstance(value, str) else "" for value in np.asarray(values).reshape(-1)]
            return self.add(field["name"], owner, "string", strings, default="")
        values = np.asarray(values).reshape(len(values), -1)
        if vector and self.convert_vectors:
            values = _apply_vectors(values.astype(np.float64), _CONVERT)
        if data_type == "QUATERNION":
            # Blender w, x, y, z -> Houdini x, y, z, w; the axis part is turned like a vector.
            values = values.astype(np.float64)
            axis = _apply_vectors(values[:, 1:4], _CONVERT) if self.convert_vectors else values[:, 1:4]
            values = np.concatenate([axis, values[:, :1]], axis=1)
        elif data_type == "FLOAT4X4":
            # Blender column-major column-vector matrices; Houdini matrix4 is row-major with row
            # vectors, so the memory order is the same. The axis change is C * M * C^-1.
            values = values.astype(np.float64)
            if self.convert_vectors and len(values):
                columns = values.reshape(-1, 4, 4).transpose(0, 2, 1)
                values = (_CONVERT @ columns @ _CONVERT_INV).transpose(0, 2, 1).reshape(-1, 16)
        mapped = _MAPPED.get((field["name"], field["domain"], data_type))
        return self.add(
            field["name"],
            owner,
            kind,
            values,
            mapped=mapped,
            vector=vector and self.convert_vectors,
            info=_TYPE_INFO.get(data_type),
        )


def _field_values(field, arrays):
    """A generic attribute's exported values; strings resolved from their table."""
    values = np.asarray(arrays[field["array"]])
    if field["data_type"] == "STRING":
        values = np.asarray(list(field.get("strings", ())), dtype=object)[values.reshape(-1).astype(np.int64)]
    return values


def _materials_for(builder, material_keys, indices, material_names, prim_offset=0, prim_total=None):
    """mat_<Name> prim groups + blender_material strings from per-primitive slot indices."""
    geo = builder.geo
    prim_total = geo.num_prims if prim_total is None else prim_total
    names = np.full(prim_total, "", dtype=object)
    if not len(indices) or not material_keys:
        return list(names)
    slots = np.clip(np.asarray(indices, dtype=np.int64), 0, len(material_keys) - 1)
    groups_used = set(geo.prim_groups)
    for slot in np.unique(slots):
        key = material_keys[int(slot)]
        name = material_names.get(key)
        if name is None:
            continue  # the default surface (empty slot)
        members = np.nonzero(slots == slot)[0]
        group = next((g for g, k in geo.material_groups.items() if k == key), None)
        if group is None:
            group = material_group(name, groups_used)
            geo.material_groups[group] = key
        previous = geo.prim_groups.get(group, np.zeros(0, np.int64))
        geo.prim_groups[group] = np.concatenate([previous, members + prim_offset])
        names[members + prim_offset] = name
    return list(names)


def subdivision_settings(native, viewport_level=None):
    """Object-tab subdivision values for an exporter subdivision config (Blender Subsurf)."""
    level = int(native["level"])
    return dict(
        type=native.get("type", "catmull_clark"),
        render_level=level,
        ipr_level=int(viewport_level) if viewport_level is not None else level,
        boundary=native.get("boundary", "edge_and_corner"),
        fvar=native.get("fvar", "boundaries"),
        use_creases=bool(native.get("use_creases", True)),
    )


def _creases(geo, builder, name, weights, edges):
    """Blender crease (0..1) -> Houdini sharpness (x10, as the reference path): edges become the
    vertex creaseweight of the polygon corner that starts each edge, vertices point cornerweight."""
    if name == "crease_vert":
        builder.fixed("cornerweight", "point", "float", (weights * 10.0).reshape(-1, 1))
        return
    n = max(len(geo.P), 1)
    sharp = {int(min(a, b)) * n + int(max(a, b)): float(w) * 10.0 for (a, b), w in zip(edges, weights) if w > 0}
    points = geo.poly_points
    starts = np.repeat(np.cumsum(geo.poly_counts) - geo.poly_counts, geo.poly_counts)
    local = np.arange(len(points)) - starts
    following = np.where(local + 1 == np.repeat(geo.poly_counts, geo.poly_counts), starts, np.arange(len(points)) + 1)
    a, b = points, points[following]
    keys = np.minimum(a, b) * n + np.maximum(a, b)
    values = np.array([sharp.get(int(key), 0.0) for key in keys], dtype=np.float64).reshape(-1, 1)
    if len(geo.line_points):
        values = np.concatenate([values, np.zeros((len(geo.line_points), 1))])
    builder.fixed("creaseweight", "vertex", "float", values)


def _uv_sets(record, arrays, point_count, vertex_rows):
    """{Blender name: (vertex rows (n, 3), keep_name)} for a mesh record's UV sets, real UV
    maps first. Point-domain UV attributes are promoted to vertices; vectors keep their z
    (no axis conversion: UVs are not spatial)."""
    corner_points = np.asarray(arrays[record["arrays"]["vertex_indices"]], dtype=np.int64).reshape(-1)
    result = {}
    fields = [(layer, "CORNER") for layer in record.get("uv_layers", [])]
    fields += [(field, field["domain"]) for field in record.get("attributes", []) if field.get("uv_set")]
    for field, domain in fields:
        values = np.asarray(arrays[field["array"]], dtype=np.float64)
        values = values.reshape(len(values), -1)[:, :3] if values.ndim > 1 else values.reshape(-1, 1)
        if values.shape[1] < 3:
            values = np.concatenate([values, np.zeros((len(values), 3 - values.shape[1]))], axis=1)
        if domain == "POINT":
            if len(values) != point_count:
                continue
            values = values[corner_points]
        result[field["name"]] = (vertex_rows(values), bool(field.get("keep_name", True)))
    return result


def mesh_geo(record, arrays, material_keys, material_names, report, *, convert_vectors=True):
    """Houdini Geo (local space, Houdini axes) for one exporter mesh record."""
    label = record.get("object_name") or record.get("name", "mesh")
    geo = Geo()
    builder = _Builder(geo, report, convert_vectors)
    get = record["arrays"]
    positions = np.asarray(arrays[get["positions"]], dtype=np.float64).reshape(-1, 3)
    geo.P = _apply_points(positions, _CONVERT)
    native = record.get("subdivision")
    if native:
        # An unapplied final Subsurf: the control cage, subdivided by Cycles at render time.
        geo.subdivision = subdivision_settings(native, record.get("subdivision_viewport_level"))
    counts = np.asarray(arrays[get["polygon_counts"]], dtype=np.int64).reshape(-1)
    loops = np.asarray(arrays[get["vertex_indices"]], dtype=np.int64).reshape(-1)
    perm = winding_permutation(counts)
    geo.poly_counts = counts
    geo.poly_points = loops[perm] if len(perm) else loops[:0]
    edges = np.asarray(arrays[get["edges"]], dtype=np.int64).reshape(-1, 2)
    # Edges that belong to no polygon (wires, curve meshes without bevel) become open polylines.
    loose = np.zeros(len(edges), dtype=bool)
    if len(edges):
        n = max(int(positions.shape[0]), 1)
        if len(loops):
            starts = np.repeat(np.cumsum(counts) - counts, counts)
            local = np.arange(len(loops)) - starts
            following = np.where(local + 1 == np.repeat(counts, counts), starts, np.arange(len(loops)) + 1)
            a, b = loops, loops[following]
            poly_keys = np.minimum(a, b) * n + np.maximum(a, b)
        else:
            poly_keys = np.zeros(0, dtype=np.int64)
        edge_keys = edges.min(axis=1) * n + edges.max(axis=1)
        loose = ~np.isin(edge_keys, poly_keys)
        if loose.any():
            geo.line_counts = np.full(int(loose.sum()), 2, dtype=np.int64)
            geo.line_points = edges[loose].reshape(-1)
            report.note(f"{int(loose.sum())} loose edge(s) written as open 2-point polylines")
    line_vertices = len(geo.line_points)

    def vertex_rows(corner_values):
        values = np.asarray(corner_values).reshape(len(corner_values), -1)[perm]
        if line_vertices:
            pad = np.zeros((line_vertices, values.shape[1]), dtype=values.dtype)
            values = np.concatenate([values, pad])
        return values

    def prim_rows(face_values):
        values = np.asarray(face_values).reshape(len(face_values), -1)
        if len(geo.line_counts):
            pad = np.zeros((len(geo.line_counts), values.shape[1]), dtype=values.dtype)
            values = np.concatenate([values, pad])
        return values

    if "normals" in get and len(loops):
        normals = np.asarray(arrays[get["normals"]], dtype=np.float64).reshape(-1, 3)
        builder.fixed("N", "vertex", "float", vertex_rows(_apply_normals(normals, _CONVERT)), normal=True)
    # UV sets (blend_export.uv_sets decides): real UV maps plus generic attributes detected
    # as UVs, all as vertex float3 (u, v, 0 or the vector's z). The default is uv; a set
    # keeps its own name unless it is the default and no material names it.
    point_count = len(positions)
    uv_sets, uv_names = _uv_sets(record, arrays, point_count, vertex_rows), []
    default_uv = record.get("default_uv")
    if default_uv in uv_sets:
        builder.fixed("uv", "vertex", "float", uv_sets[default_uv][0])
        uv_names.append("uv (from " + default_uv + ")")
    for name, (values, keep) in uv_sets.items():
        if name == "uv" and default_uv == "uv":
            continue  # already the default
        if keep or name != default_uv:
            houdini = uv_set_name(name, builder.used)
            builder.fixed(houdini, "vertex", "float", values)
            report.rename(name, houdini)
            uv_names.append(houdini)
    if uv_sets:
        report.note(record.get("name", "mesh") + ": UVs: " + ", ".join(uv_names))
    # Colour attributes: the active render colour is Cd (+ Alpha when not all 1), others by name.
    for field in record.get("color_attributes", []):
        values = np.asarray(arrays[field["array"]], dtype=np.float64).reshape(-1, 4)
        owner = "point" if field["domain"] == "POINT" else "vertex"
        rows = values if owner == "point" else vertex_rows(values)
        if field["name"] == record.get("default_color") and "Cd" not in builder.used:
            builder.fixed("Cd", owner, "float", rows[:, :3], default=1.0)
            if not np.allclose(rows[: len(values), 3], 1.0):
                builder.fixed("Alpha", owner, "float", rows[:, 3:], default=1.0)
            report.rename(field["name"], "Cd")
            if field.get("keep_name", False):  # a material names the render colour
                builder.add(field["name"], owner, "float", rows, default=1.0)
        else:
            builder.add(field["name"], owner, "float", rows, default=1.0)
    # Generic attributes.
    for field in record.get("attributes", []):
        domain, data_type, name = field["domain"], field["data_type"], field["name"]
        if (name, domain) in _REPRESENTED or field.get("uv_set"):
            continue
        if geo.subdivision and name in ("crease_edge", "crease_vert") and geo.subdivision["use_creases"]:
            _creases(geo, builder, name, np.asarray(arrays[field["array"]], dtype=np.float64).reshape(-1), edges)
            continue
        if data_type not in _TYPES:
            report.skip_attribute(label, name, f"{domain} {data_type}", "attribute type is not supported")
            continue
        values = _field_values(field, arrays)
        if domain == "EDGE":
            if data_type == "BOOLEAN" and name in EDGE_GROUPS:
                pairs = edges[np.asarray(values).reshape(-1).astype(bool)]
                group = EDGE_GROUPS[name]
                previous = geo.edge_groups.get(group, np.zeros((0, 2), np.int64))
                geo.edge_groups[group] = np.concatenate([previous, pairs])
                report.rename(name, "edge group " + group)
                continue
            # Houdini has no edge attributes (artist rule U5, 2026-11-13: skip and report).
            report.skip_attribute(label, name, f"EDGE {data_type}", "Houdini has no edge attributes")
            continue
        owner = _DOMAINS.get(domain)
        if owner is None:
            report.skip_attribute(label, name, f"{domain} {data_type}", "no Houdini equivalent for this domain")
            continue
        if owner == "vertex":
            values = vertex_rows(values)
        elif owner == "prim":
            values = prim_rows(values)
        builder.generic(field, values, owner)
    _report_skipped(report, label, record)
    for group in record.get("vertex_groups", []):
        weights = np.asarray(arrays[group["array"]], dtype=np.float64).reshape(-1, 1)
        builder.add(group["name"], "point", "float", weights, label="vertex group " + group["name"])
    material_indices = (
        np.asarray(arrays[get["material_indices"]], dtype=np.int64).reshape(-1)
        if "material_indices" in get
        else np.zeros(len(counts), dtype=np.int64)
    )
    names = _materials_for(builder, material_keys, material_indices, material_names)
    if any(names):
        builder.fixed("blender_material", "prim", "string", names, default="")
    return geo


# The report notes for curves and point clouds without a radius (blend_export._converter_radius).
_DEFAULT_WIDTH_TEXT = {
    "curves": "0.01 (Blender renders such curves with radius 0.005)",
    "points": "0.01 (Blender renders such points with radius 0.01)",
}


def _report_skipped(report, label, record):
    """The attributes the exporter did not export (record["skipped_attributes"]), with its reason."""
    for skipped in record.get("skipped_attributes", []):
        if (skipped["name"], skipped["domain"]) in _REPRESENTED:
            continue
        report.skip_attribute(
            label,
            skipped["name"],
            f"{skipped['domain']} {skipped['data_type']}",
            skipped.get("reason") or "not exported",
        )


def curves_geo(record, arrays, material_keys, material_names, report, *, convert_vectors=True):
    """Hair/geometry-nodes curves as open polylines through their control points."""
    label = record.get("object_name") or record.get("name", "curves")
    geo = Geo()
    builder = _Builder(geo, report, convert_vectors)
    get = record["arrays"]
    positions = np.asarray(arrays[get["positions"]], dtype=np.float64).reshape(-1, 3)
    geo.P = _apply_points(positions, _CONVERT)
    offsets = np.asarray(arrays[get["offsets"]], dtype=np.int64).reshape(-1)
    geo.line_counts = np.diff(offsets) if len(offsets) > 1 else np.zeros(0, np.int64)
    geo.line_points = np.arange(int(offsets[-1]) if len(offsets) else 0, dtype=np.int64)
    if "radius" in get:
        radius = np.asarray(arrays[get["radius"]], dtype=np.float64).reshape(-1, 1)
        builder.fixed("width", "point", "float", radius * 2.0, default=0.0)
        if record.get("radius_source") == "default":
            report.note(f"{label}: no radius attribute; width {_DEFAULT_WIDTH_TEXT['curves']}")
        else:
            report.rename("radius", "width (diameter = 2 x radius)")
    types = set(record.get("curve_types", [0]))
    if types & {2, 3}:
        report.lossy_item("Bezier/NURBS curves written as polylines through their control points")
    if 0 in types:
        report.lossy_item("Catmull-Rom curves written as polylines through their control points (no smoothing)")
    if record.get("cyclic_count"):
        report.lossy_item(f"{record['cyclic_count']} cyclic curve(s) written as open polylines")
    material_indices = None
    for field in record.get("attributes", []):
        domain, name = field["domain"], field["name"]
        if name == "material_index" and domain == "CURVE":
            material_indices = np.asarray(arrays[field["array"]], dtype=np.int64).reshape(-1)
            continue
        if field["data_type"] not in _TYPES or domain not in _DOMAINS:
            report.skip_attribute(label, name, f"{domain} {field['data_type']}", "attribute type is not supported")
            continue
        builder.generic(field, _field_values(field, arrays), _DOMAINS[domain])
    _report_skipped(report, label, record)
    if material_indices is None:
        material_indices = np.zeros(len(geo.line_counts), dtype=np.int64)
    names = _materials_for(builder, material_keys, material_indices, material_names)
    if any(names):
        builder.fixed("blender_material", "prim", "string", names, default="")
    return geo


def points_geo(record, arrays, material_keys, material_names, report, *, convert_vectors=True):
    """Point clouds as points without primitives; radius -> pscale (Blender's 0.01 without one)."""
    label = record.get("object_name") or record.get("name", "points")
    geo = Geo()
    builder = _Builder(geo, report, convert_vectors)
    get = record["arrays"]
    geo.P = _apply_points(np.asarray(arrays[get["positions"]], dtype=np.float64).reshape(-1, 3), _CONVERT)
    if "radius" in get:
        builder.fixed("pscale", "point", "float", np.asarray(arrays[get["radius"]], dtype=np.float64).reshape(-1, 1))
        if record.get("radius_source") == "default":
            report.note(f"{label}: no radius attribute; pscale {_DEFAULT_WIDTH_TEXT['points']}")
        else:
            report.rename("radius", "pscale")
    for field in record.get("attributes", []):
        if field["data_type"] not in _TYPES:
            report.skip_attribute(
                label, field["name"], f"{field['domain']} {field['data_type']}", "attribute type is not supported"
            )
            continue
        builder.generic(field, _field_values(field, arrays), "point")
    _report_skipped(report, label, record)
    if any(material_names.get(key) for key in material_keys):
        report.lossy_item("Point cloud materials are not assigned (points have no primitives)")
    return geo


def geo_digest(geo, children=None):
    """Canonical content hash (bgeo files carry a date header, so file bytes are not comparable)."""
    digest = hashlib.sha256()

    def feed(label, value):
        digest.update(label.encode() + b"\0")
        if isinstance(value, np.ndarray):
            array = np.ascontiguousarray(value)
            digest.update(str(array.dtype).encode() + str(array.shape).encode())
            digest.update(array.tobytes())
        else:
            digest.update(json.dumps(value, sort_keys=True, default=str).encode())

    for field in ("P", "poly_counts", "poly_points", "line_counts", "line_points", "packed_points", "packed_matrices"):
        feed(field, np.asarray(getattr(geo, field)))
    for name in sorted(geo.attribs):
        attrib = geo.attribs[name]
        meta = [attrib.owner, attrib.kind, attrib.normal, attrib.vector]
        feed("attrib:" + name, meta + [attrib.info] if attrib.info else meta)
        feed("values:" + name, attrib.values if attrib.kind != "string" else list(attrib.values))
    for name in sorted(geo.prim_groups):
        feed("group:" + name, np.sort(np.asarray(geo.prim_groups[name], dtype=np.int64)))
    for name in sorted(geo.edge_groups):
        feed("edges:" + name, np.asarray(geo.edge_groups[name], dtype=np.int64))

    def child(key):
        value = (children or {}).get(key)
        if value is None:
            return key
        return value if isinstance(value, str) else geo_digest(value)

    feed("children", [child(key) for key in geo.packed_children])
    return digest.hexdigest()


# ------------------------------------------------------------------ manifest -> frame data


def _owner_depth(manifest):
    return 1 if str(manifest.get("selector_type", "")).upper() == "COLLECTION" else 0


def _object_path(entry):
    return "/" + "/".join([*entry.get("collection_path", []), entry["name"]])


class _Shapes:
    """Converted local geometry per exporter record key, with content de-duplication."""

    def __init__(self, manifest, arrays, material_names, report, options):
        self.manifest, self.arrays = manifest, arrays
        self.material_names, self.report, self.options = material_names, report, options
        self.records = {record["key"]: ("mesh", record) for record in manifest.get("meshes", [])}
        self.records.update({record["key"]: ("curves", record) for record in manifest.get("curves", [])})
        self.records.update({record["key"]: ("pointcloud", record) for record in manifest.get("pointclouds", [])})
        self.geos, self.canonical, self.by_digest, self.digests = {}, {}, {}, {}

    def get(self, instance):
        key = instance["mesh_key"]
        material_keys = tuple(instance.get("material_keys", ()))
        cache_key = (key, material_keys)
        if cache_key not in self.canonical:
            kind, record = self.records[key]
            build = {"mesh": mesh_geo, "curves": curves_geo, "pointcloud": points_geo}[kind]
            geo = build(
                record,
                self.arrays,
                list(material_keys),
                self.material_names,
                self.report,
                convert_vectors=self.options.get("convert_vectors", True),
            )
            digest = geo_digest(geo)
            if geo.subdivision:  # the same cage subdivided differently is a different shape
                digest = hashlib.sha256((digest + json.dumps(geo.subdivision, sort_keys=True)).encode()).hexdigest()
            # Geometry nodes geometry instances arrive as separate records with identical content.
            canonical = self.by_digest.setdefault(digest, "g" + digest[:16])
            self.geos.setdefault(canonical, geo)
            self.digests.setdefault(canonical, digest)
            self.canonical[cache_key] = canonical
        return self.canonical[cache_key]

    def add_prototype(self, geo):
        """Register a built (nested) prototype; identical prototypes share one key."""
        digest = geo_digest(geo, {key: self.digests.get(key, key) for key in geo.packed_children})
        canonical = self.by_digest.setdefault("p" + digest, "p" + digest[:16])
        self.geos.setdefault(canonical, geo)
        self.digests.setdefault(canonical, digest)
        self.canonical[(canonical, ())] = canonical
        return canonical

    def closure(self, keys):
        """{key: Geo} for keys and every packed child below them (nested prototypes)."""
        result, pending = {}, list(keys)
        while pending:
            key = pending.pop()
            if key in result:
                continue
            result[key] = self.geos[key]
            pending.extend(result[key].packed_children)
        return result


def _nest_instances(instances, trees, arrays, objects, shapes, depth, report):
    """Rebuild geometry nodes instance nesting (packed inside packed) from the exported trees.

    The depsgraph flattens instances; each dupli's persistent id still names its path through
    the instance tree (innermost first: component, then one index per level, then the
    collection member). A dupli walks the tree from its owner's root while the referenced
    geometry instances more geometry; that path is the occurrence it belongs to. Every
    occurrence of one tree node becomes one shared prototype: its own geometry plus packed
    child occurrences at their instance transforms. Occurrences of a node must hold the same
    shapes at the same local transforms; otherwise the owner stays flat (reported).
    """
    result = []
    by_owner = {}
    for instance in instances:
        owner = instance.get("owner")
        if owner in trees and instance.get("depth", depth) > depth:
            by_owner.setdefault(owner, []).append(instance)
        else:
            result.append(instance)
    for owner, owned in by_owner.items():
        nested = _nest_owner(owner, owned, trees[owner], arrays, objects, shapes, depth, report)
        result.extend(owned if nested is None else nested)
    return result


def _nest_owner(owner, owned, tree, arrays, objects, shapes, depth, report):
    nodes = tree["nodes"]
    ref_index = [np.asarray(arrays[node["ref_index"]]).reshape(-1) for node in nodes]
    local = [np.asarray(arrays[node["transforms"]], dtype=np.float64).reshape(-1, 4, 4) for node in nodes]
    entry = objects.get(owner) or {}
    if entry.get("transform") is None:
        return None
    owner_world = _CONVERT_INV @ matrix_from_flat(entry["transform"])  # Blender world of the owner
    occurrences = {(): {"node": 0, "leaves": [], "children": []}}
    worlds = {(): owner_world}
    keep = []
    for instance in owned:
        path = list(instance.get("dupli_path") or [])
        if depth:
            path = path[:-1]  # the collection member index
        if len(path) < 2:
            keep.append(instance)  # the owner's own components
            continue
        indices = path[:0:-1]  # outermost first, without the component index
        node, consumed = 0, ()
        for index in indices:
            if index >= len(ref_index[node]):
                report.note(f"{owner}: instance tree does not match Blender's instances; kept flat")
                return None
            child = nodes[node]["references"][int(ref_index[node][index])]
            if child < 0:
                break
            parent = consumed
            consumed = consumed + (int(index),)
            if consumed not in occurrences:
                occurrences[consumed] = {"node": child, "leaves": [], "children": []}
                occurrences[parent]["children"].append((consumed, int(index)))
                worlds[consumed] = worlds[parent] @ local[node][index]
            node = child
        if not consumed:
            keep.append(instance)  # an instance of geometry that instances nothing further
            continue
        remainder = tuple(indices[len(consumed) :]) + (path[0],)
        key = shapes.get(instance)
        if not shapes.geos[key].num_points:
            continue
        blender = _CONVERT_INV @ matrix_from_flat(instance["transform"])
        relative = np.linalg.inv(worlds[consumed]) @ blender
        occurrences[consumed]["leaves"].append((remainder, key, _CONVERT @ relative @ _CONVERT_INV))
    if len(occurrences) == 1:
        return None
    # One prototype per tree node, built deepest first from its first occurrence.
    order = sorted((path for path in occurrences if path), key=len, reverse=True)
    prototypes, signatures = {}, {}
    for path in order:
        occurrence = occurrences[path]
        node = occurrence["node"]
        leaves = sorted(occurrence["leaves"], key=lambda leaf: leaf[0])
        children = sorted(occurrence["children"], key=lambda child: child[1])
        signature = (
            [(remainder, key) for remainder, key, _ in leaves],
            [(index, prototypes[child]) for child, index in children],
            [matrix for _, _, matrix in leaves],
        )
        if node in signatures:
            first = signatures[node]
            if first[:2] != signature[:2] or not all(
                np.allclose(a, b, rtol=1e-4, atol=1e-5) for a, b in zip(first[2], signature[2], strict=True)
            ):
                report.note(f"{owner}: instances of one geometry nodes reference differ; kept flat")
                return None
            prototypes[path] = signatures[node][3]
            continue
        counts = {}
        for _, key, _ in leaves:
            counts[key] = counts.get(key, 0) + 1
        parts = [transformed(shapes.geos[key], matrix) for _, key, matrix in leaves if counts[key] == 1]
        shared = [(key, matrix) for _, key, matrix in leaves if counts[key] > 1]
        nested = [
            (prototypes[child], _CONVERT @ _local_of(worlds, path, child) @ _CONVERT_INV) for child, _ in children
        ]
        packed_keys = [key for key, _ in shared + nested]
        if packed_keys:
            parts.append(packed(shapes.geos, packed_keys, [matrix for _, matrix in shared + nested]))
        geo = merge(parts, report)
        prototype = shapes.add_prototype(geo)
        prototypes[path] = prototype
        signatures[node] = signature + (prototype,)
    result = list(keep)
    for path, index in occurrences[()]["children"]:
        name = nodes[occurrences[path]["node"]].get("name") or "instance"
        flat = (_CONVERT @ worlds[path]).reshape(-1)
        result.append(
            dict(
                name=name,
                source_name=name,
                mesh_key=prototypes[path],
                material_keys=[],
                transform=[float(value) for value in flat],
                owner=owner,
                depth=depth + 2,
                dupli_path=[],
                geometry_type="nested",
                is_instance=True,
            )
        )
    report.note(
        f"{owner}: nested instances kept: {len(order)} packed occurrences of "
        f"{len(set(signatures))} instanced geometries"
    )
    return result


def _local_of(worlds, parent, child):
    """A child occurrence's transform relative to its parent occurrence (Blender axes)."""
    return np.linalg.inv(worlds[parent]) @ worlds[child]


def _setting_key(setting):
    return json.dumps(setting, sort_keys=True) if setting else ""


def _repeated(keys):
    """Shape keys used more than once."""
    seen, repeated = set(), set()
    for key in keys:
        (repeated if key in seen else seen).add(key)
    return repeated


def _collection_offset(instances, objects, worlds, depth):
    """instance world @ inverse(object world) of the objects' own geometry, when it is not identity."""
    found = None
    for instance in instances:
        owner = instance.get("owner") or instance["source_name"]
        # Only the member's own evaluated geometry: its duplis carry their own instance transforms.
        if instance["source_name"] != owner or owner not in worlds or instance.get("depth", depth) != depth:
            continue
        if objects.get(owner, {}).get("transform") is None:
            continue
        try:
            candidate = houdini_world(instance["transform"]) @ np.linalg.inv(worlds[owner])
        except np.linalg.LinAlgError:
            continue
        if found is None:
            found = candidate
        elif not np.allclose(found, candidate, atol=1e-5):
            return None  # not one shared offset: leave transforms as exported
    if found is None or np.allclose(found, np.eye(4), atol=1e-6):
        return None
    return found


def assemble(manifest, arrays, structure, material_names, report, options=None, reference=None):
    """Turn one exporter result into frame data for one selection item.

    packed: {"geo": Geo, "children": {key: Geo}, "parts": [{"subdivision", "geo"}]}.
    hierarchy: {"objects": [{"name", "node_type", "blender_type", "parent", "local", "geo", "children"}]}.
    ``reference`` (hierarchy frame sequences): the first frame's object worlds; later frames
    keep those node transforms and carry any object motion in their geometry.
    """
    options = options or {}
    shapes = _Shapes(manifest, arrays, material_names, report, options)
    instances = manifest.get("instances", [])
    objects = {entry["name"]: entry for entry in manifest.get("objects", [])}
    for diagnostic in manifest.get("diagnostics", []):
        if diagnostic.get("code") in ("blend.unsupported_object", "blend.particle_hair"):
            report.skip(f"{diagnostic.get('source')}: {diagnostic.get('message')}")
        elif diagnostic.get("code") == "blend.instance_attributes":
            for name in diagnostic.get("attributes", ()):
                report.skip_attribute(diagnostic.get("source"), name, "INSTANCE", INSTANCE_ATTRIBUTES_REASON)
        elif diagnostic.get("code") == "blend.subdivision_evaluated" and options.get("render_subdivision"):
            report.note(f"{diagnostic.get('source')}: subdivision applied in Blender ({diagnostic.get('message')})")
    depth = _owner_depth(manifest)
    trees = manifest.get("instance_trees") or {}
    if trees and options.get("nested_instances", True):
        instances = _nest_instances(instances, trees, arrays, objects, shapes, depth, report)

    def path_of(instance):
        owner = instance.get("owner") or instance["source_name"]
        entry = objects.get(owner)
        base = _object_path(entry) if entry else "/" + owner
        if instance["source_name"] == owner and instance.get("depth", depth) <= depth + 1:
            return base
        return base + "/" + instance["source_name"]

    if structure == "merged":
        raise BlendImportError(MERGED_REMOVED)
    if structure == "packed":
        keys, matrices, names, paths = [], [], [], []
        for instance in instances:
            key = shapes.get(instance)
            if not shapes.geos[key].num_points:
                continue  # e.g. a geometry nodes object whose output is only instances
            keys.append(key)
            matrices.append(houdini_world(instance["transform"]))
            names.append(instance["source_name"])
            paths.append(path_of(instance))
        # Cycles subdivides per Houdini object, so shapes with different render-time subdivision
        # (none, or another Subsurf setting) go to separate parts, each its own geo object.
        groups = {}
        for key, matrix, name, path in zip(keys, matrices, names, paths, strict=True):
            setting = shapes.geos[key].subdivision
            groups.setdefault(_setting_key(setting), (setting, []))[1].append((key, matrix, name, path))
        parts = []
        for _, (setting, members) in sorted(groups.items(), key=lambda item: (item[0] != "", item[0])):
            # Every primitive is packed and carries name and path, so the Blender File node's
            # Unpack SOP can transfer them (blend-import.md: Asset nodes).
            columns = list(zip(*members, strict=True))
            geo = packed(shapes.geos, columns[0], columns[1], {"name": list(columns[2]), "path": list(columns[3])})
            geo.subdivision = setting
            parts.append({"subdivision": setting, "geo": geo})
        if not parts:
            parts.append({"subdivision": None, "geo": Geo()})
        return {"geo": parts[0]["geo"], "children": dict(shapes.geos), "parts": parts}
    if structure != "hierarchy":
        raise BlendImportError("Unknown structure: " + repr(structure))
    worlds = {}
    for name, entry in objects.items():
        worlds[name] = (
            matrix_from_flat(reference[name]) if reference and name in reference else houdini_world(entry["transform"])
        )
    if not reference:
        # A collection is evaluated as an instance, so its instance offset is in every instance
        # transform but not in the objects' own matrix_world. Move the objects instead of
        # baking the offset into their geometry (pivots stay where Blender has them), so
        # hierarchy places the collection like packed does.
        offset = _collection_offset(instances, objects, worlds, depth)
        if offset is not None:
            worlds = {name: offset @ world for name, world in worlds.items()}
    owned = {}
    for instance in instances:
        owner = instance.get("owner") or instance["source_name"]
        if owner not in objects:
            # An owner the exporter could not name: give it its own root object.
            objects[owner] = dict(name=owner, type="MESH", parent=None, collection_path=[], transform=None)
            worlds[owner] = np.eye(4)
        owned.setdefault(owner, []).append(instance)
    # Duplis of an object's own geometry (geometry nodes geometry instances) are merged into it
    # unless the same shape repeats: then they stay packed, sharing one prototype.
    repeated_own = {}
    for name, members in owned.items():
        dupli_keys = []
        for instance in members:
            if instance["source_name"] == name and depth < instance.get("depth", depth) <= depth + 1:
                key = shapes.get(instance)
                if shapes.geos[key].num_points:
                    dupli_keys.append(key)
        repeated_own[name] = _repeated(dupli_keys)
    # Objects that are only one shape shared with other objects (Blender linked duplicates) load
    # one shared file as a packed disk primitive instead of a copy each (files storage, one frame).
    single_shape = {}
    if options.get("share_shapes"):
        for name, members in owned.items():
            if len(members) == 1 and members[0]["source_name"] == name and members[0].get("depth", depth) == depth:
                key = shapes.get(members[0])
                if shapes.geos[key].num_points:
                    single_shape[name] = key
    shared = _repeated(single_shape.values())
    shared_geos = {}
    result = []
    for name, entry in objects.items():
        world = worlds[name]
        inverse = np.linalg.inv(world)
        key = single_shape.get(name)
        if key in shared and np.allclose(inverse @ houdini_world(owned[name][0]["transform"]), np.eye(4), atol=1e-6):
            shared_geos[key] = shapes.geos[key]
            parent = entry.get("parent") if entry.get("parent") in objects else None
            result.append(
                dict(
                    name=name,
                    node_type="geo",
                    blender_type=entry.get("type", "MESH"),
                    parent=parent,
                    local=(np.linalg.inv(worlds[parent]) @ world) if parent else world,
                    world=world,
                    geo=None,
                    children={},
                    shape=key,
                    subdivision=shapes.geos[key].subdivision,
                )
            )
            continue
        own, generated = [], []
        for instance in owned.get(name, []):
            key = shapes.get(instance)
            if not shapes.geos[key].num_points:
                continue
            local = inverse @ houdini_world(instance["transform"])
            is_own = instance["source_name"] == name and instance.get("depth", depth) <= depth + 1
            if is_own and instance.get("depth", depth) > depth and key in repeated_own.get(name, ()):
                is_own = False  # a geometry nodes instance repeated by this object: keep it packed
            if is_own:
                own.append(transformed(shapes.geos[key], local))
            else:
                generated.append((key, local, instance["source_name"]))
        parts = [merge(own, report)] if own else []
        # Render-time subdivision is an object setting: the object's own geometry decides; its
        # packed instances inherit it in Cycles (reported when their own setting differs).
        settings = {_setting_key(shapes.geos[key].subdivision) for key, _, _ in generated}
        subdivision = parts[0].subdivision if parts else None
        if not parts and len(settings) == 1:
            subdivision = shapes.geos[generated[0][0]].subdivision
        if own and len({_setting_key(geo.subdivision) for geo in own}) > 1:
            report.note(f"{name}: parts with and without render-time subdivision; the object is not subdivided")
        if generated and settings - {_setting_key(subdivision)}:
            report.note(f"{name}: packed instances inherit the object's render-time subdivision setting")
        if generated:
            parts.append(
                packed(
                    shapes.geos,
                    [key for key, _, _ in generated],
                    [local for _, local, _ in generated],
                    {"name": [source for _, _, source in generated]},
                )
            )
        geo = merge(parts, report) if parts else None
        if geo is not None and "name" not in geo.attribs:
            set_prim_strings(geo, "name", name)
        elif geo is not None:
            values = geo.attribs["name"].values
            geo.attribs["name"].values = [value or name for value in values]
        parent = entry.get("parent") if entry.get("parent") in objects else None
        local = (np.linalg.inv(worlds[parent]) @ world) if parent else world
        children = shapes.closure(key for key, _, _ in generated)
        blender_type = entry.get("type", "MESH")
        if geo is None and blender_type not in _NULL_TYPES:
            report.note(f"{name}: no render-visible geometry; created as a null")
        if blender_type in ("CAMERA", "LIGHT", "LIGHT_PROBE", "SPEAKER"):
            report.skip(f"{name}: Blender {blender_type.lower()} converted to a null object")
        result.append(
            dict(
                name=name,
                node_type="geo" if geo is not None else "null",
                blender_type=blender_type,
                parent=parent,
                local=local,
                world=world,
                geo=geo,
                children=children,
                subdivision=subdivision,
            )
        )
    return {"objects": result, "shapes": shared_geos}


# ------------------------------------------------------------------ frame-data spill (sequences)


def _spill(frame, path):
    """Write frame data to an npz (numbers only; no pickle) and return its path."""
    arrays, geos = {}, []

    def store(geo):
        index = len(geos)
        prefix = "g%d/" % index
        header = dict(attribs={}, prim_groups=[], edge_groups=[], packed_children=list(geo.packed_children))
        header["material_groups"] = dict(geo.material_groups)
        header["subdivision"] = geo.subdivision
        for field in ("P", "poly_counts", "poly_points", "line_counts", "line_points", "packed_points"):
            arrays[prefix + field] = np.asarray(getattr(geo, field))
        arrays[prefix + "packed_matrices"] = np.asarray(geo.packed_matrices)
        for name, attrib in geo.attribs.items():
            meta = dict(owner=attrib.owner, kind=attrib.kind, normal=attrib.normal, vector=attrib.vector)
            meta["default"] = attrib.default
            meta["info"] = attrib.info
            if attrib.kind == "string":
                meta["strings"] = list(attrib.values)
            else:
                arrays[prefix + "a/" + name] = attrib.values
            header["attribs"][name] = meta
        for name, members in geo.prim_groups.items():
            header["prim_groups"].append(name)
            arrays[prefix + "pg/" + name] = np.asarray(members)
        for name, pairs in geo.edge_groups.items():
            header["edge_groups"].append(name)
            arrays[prefix + "eg/" + name] = np.asarray(pairs)
        geos.append(header)
        return index

    def walk(value):
        if isinstance(value, Geo):
            return {"__geo__": store(value)}
        if isinstance(value, np.ndarray):
            return {"__matrix__": value.tolist()}
        if isinstance(value, dict):
            return {key: walk(item) for key, item in value.items()}
        if isinstance(value, list):
            return [walk(item) for item in value]
        return value

    tree = walk(frame)
    header = json.dumps(dict(tree=tree, geos=geos)).encode("utf-8")
    arrays["__header__"] = np.frombuffer(header, dtype=np.uint8)
    with open(path, "wb") as stream:
        np.savez(stream, **arrays)
    return str(path)


def _unspill(path):
    with np.load(path, allow_pickle=False) as data:
        arrays = {key: data[key] for key in data.files}
    header = json.loads(arrays.pop("__header__").tobytes().decode("utf-8"))
    geos = []
    for index, meta in enumerate(header["geos"]):
        prefix = "g%d/" % index
        geo = Geo()
        for field in ("P", "poly_counts", "poly_points", "line_counts", "line_points", "packed_points"):
            setattr(geo, field, arrays[prefix + field])
        geo.packed_matrices = arrays[prefix + "packed_matrices"]
        geo.packed_children = list(meta["packed_children"])
        geo.material_groups = dict(meta["material_groups"])
        geo.subdivision = meta.get("subdivision")
        for name, attrib in meta["attribs"].items():
            values = attrib["strings"] if attrib["kind"] == "string" else arrays[prefix + "a/" + name]
            geo.attribs[name] = Attrib(
                attrib["owner"],
                attrib["kind"],
                values,
                normal=attrib["normal"],
                vector=attrib["vector"],
                default=attrib["default"],
                info=attrib.get("info"),
            )
        geo.prim_groups = {name: arrays[prefix + "pg/" + name] for name in meta["prim_groups"]}
        geo.edge_groups = {name: arrays[prefix + "eg/" + name] for name in meta["edge_groups"]}
        geos.append(geo)

    def walk(value):
        if isinstance(value, dict):
            if set(value) == {"__geo__"}:
                return geos[value["__geo__"]]
            if set(value) == {"__matrix__"}:
                return np.asarray(value["__matrix__"], dtype=np.float64)
            return {key: walk(item) for key, item in value.items()}
        if isinstance(value, list):
            return [walk(item) for item in value]
        return value

    return walk(header["tree"])


def load_frame(frame):
    return _unspill(frame) if isinstance(frame, str) else frame


# ------------------------------------------------------------------ options


def _selection(selection):
    if not selection:
        raise BlendImportError("Nothing selected to import")
    result = []
    for item in selection:
        try:
            kind, name = item
        except (TypeError, ValueError):
            raise BlendImportError('Selection items are ("collection"|"object", name) pairs') from None
        kind = str(kind).lower()
        if kind not in ("collection", "object") or not isinstance(name, str) or not name:
            raise BlendImportError("Invalid selection item: " + repr(item))
        if (kind, name) not in result:
            result.append((kind, name))
    return result


def frame_list(frames):
    """None -> [None] (scene frame); (start, end, step) -> integer frames, inclusive."""
    if frames is None:
        return [None]
    try:
        start, end, step = frames
    except (TypeError, ValueError):
        raise BlendImportError("frames must be None or (start, end, step)") from None
    values = [float(start), float(end), float(step)]
    if not all(math.isfinite(v) for v in values) or values[2] <= 0 or values[1] < values[0]:
        raise BlendImportError("Invalid frame range: " + repr(frames))
    if any(v != int(v) for v in values):
        raise BlendImportError("Frame sequences use whole frames ($F4 file names)")
    start, end, step = (int(v) for v in values)
    return list(range(start, end + 1, step))


def validate_options(storage, structure, materials, frames):
    if structure == "merged":
        raise BlendImportError(MERGED_REMOVED)
    if storage not in STORAGES:
        raise BlendImportError("storage must be one of " + ", ".join(STORAGES))
    if structure not in STRUCTURES:
        raise BlendImportError("structure must be one of " + ", ".join(STRUCTURES))
    if materials not in MATERIAL_MODES:
        raise BlendImportError("materials must be one of " + ", ".join(MATERIAL_MODES))
    if frames is not None and storage != "files":
        raise BlendImportError('Frame ranges need storage="files" (one file per frame)')
    return frame_list(frames)


def asset_original(kind, name):
    """Stable identity of a selection item in cycles_import.json ("collection:Props")."""
    return f"{kind}:{name}"


# ------------------------------------------------------------------ snapshot


def _scene_path():
    """HOM: the saved scene's .hip path (forward slashes), or None for an untitled scene."""
    import hou

    from .blend_library_materials import untitled_scene

    if untitled_scene():
        return None
    try:
        return hou.hipFile.path().replace("\\", "/") or None
    except Exception:
        return None


BRIDGE_DISABLED = "The Blender bridge is disabled for this installation (Cycles Setup)."


def _bridge_enabled(value):
    return str(value).strip().lower() not in ("0", "off", "false", "disabled")


def plain_snapshot(
    source,
    selection,
    *,
    storage="files",
    structure="packed",
    materials="link",
    frames=None,
    include_disabled=False,
    asset_root=DEFAULT_ASSET_ROOT,
    parent=None,
    position=None,
    executable=None,
    timeout=300,
    convert_vectors=True,
    library_root=None,
    apply_subdivision=False,
    nested_instances=True,
    on_shared="replace",
    viewport_shape=None,
):
    """No HOM: validated options. Folders stay unexpanded until resolve_folders (host thread).

    The blend folder is named after the .blend file; textures stay in place. ``on_shared``:
    "replace" or "cancel" when a re-import would change files another scene uses (blend-import.md: Import record).
    """
    source = str(Path(source).resolve()).replace("\\", "/")
    if not os.path.isfile(source) or not source.lower().endswith(".blend"):
        raise BlendImportError("Blender source must be an existing .blend file: " + source)
    selection = _selection(selection)
    frame_values = validate_options(storage, structure, materials, frames)
    if not (isinstance(timeout, (int, float)) and math.isfinite(timeout) and timeout > 0):
        raise BlendImportError("timeout must be positive")
    if on_shared not in ON_SHARED:
        raise BlendImportError("on_shared must be one of " + ", ".join(ON_SHARED))
    root_text = (asset_root or DEFAULT_ASSET_ROOT).strip().replace("\\", "/")
    if root_text.rstrip("/") == OBSOLETE_ASSET_ROOT:
        root_text = DEFAULT_ASSET_ROOT
    # Process environment only (no hou.getenv off the host thread); import_snapshot also asks Houdini.
    if storage != "reference" and not _bridge_enabled(os.environ.get("CYCLES_HOUDINI_BLENDER_BRIDGE", "1")):
        raise BlendImportError(BRIDGE_DISABLED)
    notices = [SEQUENCE_WARNING] if len(frame_values) > 1 else []
    from .blend_file_node import source_signature

    return dict(
        source=source,
        # Taken before Blender reads the file: the Blender File node's Status compares it later.
        source_signature=source_signature(source),
        viewport_shape=viewport_shape if viewport_shape in ("bounds", "hull") else None,
        selection=[list(item) for item in selection],
        storage=storage,
        structure=structure,
        materials=materials,
        frames=list(frames) if frames is not None else None,
        frame_values=frame_values,
        include_disabled=bool(include_disabled),
        apply_subdivision=bool(apply_subdivision),
        nested_instances=bool(nested_instances),
        on_shared=on_shared,
        library_root=(library_root or "").replace("\\", "/"),
        asset_root=root_text if storage != "reference" else None,
        asset_root_abs=None,
        scene=None,
        parent=parent.path() if hasattr(parent, "path") else (parent or "/obj"),
        position=list(position) if position is not None else None,
        executable=str(executable) if executable else None,
        blender=None,
        source_config=os.environ.get("CYCLES_HOUDINI_BLENDER_OCIO") or None,
        timeout=float(timeout),
        convert_vectors=bool(convert_vectors),
        notices=notices,
        resolved=False,
    )


def _needs_asset_root(snapshot):
    """Files storage writes geometry; any converted import may extract packed images."""
    return snapshot["storage"] == "files" or (snapshot["storage"] != "reference" and snapshot["materials"] != "none")


def resolve_folders(snapshot):
    """Host thread (HOM): expand the asset root (untitled scene -> <cache>/assets_blender; blend-import.md: Where
    imports write).

    Refuses a root inside the .blend file's folder or its library (blend-import.md: Where imports write). Records the
    scene's .hip path. Returns the notices it added. Idempotent: a resolved snapshot is left unchanged.
    """
    if snapshot.get("resolved"):
        return []
    from . import blend_library_materials

    notices = []
    if _needs_asset_root(snapshot):
        root_text, notice = blend_library_materials.asset_root_text(snapshot["asset_root"])
        if notice:
            notices.append(notice)
        try:
            root_abs = blend_library_materials.resolve_asset_root(
                root_text, snapshot["source"], snapshot.get("library_root") or None
            )
        except blend_library_materials.AssetRootError as error:
            raise BlendImportError(str(error)) from None
        snapshot.update(asset_root=root_text, asset_root_abs=str(root_abs), scene=_scene_path())
    snapshot["notices"] = list(snapshot.get("notices", ())) + notices
    snapshot["resolved"] = True
    return notices


def import_snapshot(source, selection, *, report=None, **options):
    """Host thread (HOM): plain_snapshot + resolved folders and Blender discovery inputs.

    Accepts the keywords of import_blend. Optional: callers on the host thread get folder
    errors (undefined variables, a folder inside the library) before Blender starts.
    """
    import hou

    from ..core import blender_bridge

    snapshot = plain_snapshot(source, selection, **options)
    if snapshot["storage"] != "reference" and not blender_bridge.enabled():
        raise BlendImportError(BRIDGE_DISABLED)
    resolve_folders(snapshot)
    getenv = getattr(hou, "getenv", None)
    snapshot["blender"] = (
        blender_bridge._configured_executable(),
        os.environ.get("PATH", ""),
        os.environ.get("ProgramFiles", "C:/Program Files"),
    )
    if callable(getenv):
        snapshot["source_config"] = getenv("CYCLES_HOUDINI_BLENDER_OCIO") or snapshot["source_config"]
    if report is not None:
        for text in snapshot["notices"]:
            report(text, False)
        snapshot["notices_reported"] = True
    return snapshot


# ------------------------------------------------------------------ prepare (no HOM)


def _check(cancelled):
    if cancelled is not None and cancelled.is_set():
        raise ImportCancelled("Blender import cancelled")


def requests_for(snapshot):
    """Worker requests: one per (selection item, frame), items outer, frames inner."""
    requests = []
    for kind, name in snapshot["selection"]:
        for index, frame in enumerate(snapshot["frame_values"]):
            request = dict(
                selector_type=kind.upper(),
                selector=name,
                frame="scene" if frame is None else float(frame),
                # Unless asked to apply it, a final Subsurf is exported as its control cage and
                # subdivided by Cycles at render time (object tab).
                native_subdivision=not snapshot.get("apply_subdivision", False),
                nested_instances=bool(snapshot.get("nested_instances", True)),
                vertex_groups=True,
                curves=True,
                pointclouds=True,
                object_tree=True,
                include_excluded_collections=bool(snapshot["include_disabled"]),
                # Textures stay where the .blend points; only packed images are staged (blend-import.md: Textures in
                # place).
                material_textures="in_place",
            )
            if snapshot["materials"] == "none" or index > 0:
                request["material_export"] = "names"
            requests.append(request)
    return requests


def _resolve_executable(snapshot):
    if snapshot.get("executable"):
        return snapshot["executable"]
    from ..core import blender_bridge

    inputs = snapshot.get("blender") or (
        os.path.expandvars(os.path.expanduser(os.environ.get("CYCLES_HOUDINI_BLENDER", ""))),
        os.environ.get("PATH", ""),
        os.environ.get("ProgramFiles", "C:/Program Files"),
    )
    executable = blender_bridge._discover(*inputs)
    if not executable or not os.path.isfile(executable):
        raise BlendImportError("Blender not found. Install Blender or set CYCLES_HOUDINI_BLENDER to its executable.")
    return executable


def _material_entry(record, source, staging):
    """A material to build later: its record and the staging folder holding its packed images.

    ``source`` is the .blend the material comes from: a linked library file, else the import's."""
    if record.get("default"):
        return None
    record = dict(record)
    library = record.pop("library", "") or ""
    return dict(
        key=record["key"],
        name=record.get("name"),
        local_name=record.get("local_name") or record.get("name"),
        source=(library or source).replace("\\", "/"),
        unsupported=record.get("unsupported"),
        record=record,
        staging=str(staging),
    )


def prepare_import(
    source,
    selection=None,
    *,
    storage="files",
    structure="packed",
    materials="link",
    frames=None,
    include_disabled=False,
    asset_root=DEFAULT_ASSET_ROOT,
    parent=None,
    position=None,
    executable=None,
    timeout=300,
    convert_vectors=True,
    library_root=None,
    cancelled=None,
    report=None,
    evaluate=None,
    apply_subdivision=False,
    nested_instances=True,
    on_shared="replace",
):
    """Any thread, no HOM. Blender export + conversion; returns the payload for build_import.

    Call it as ``prepare_import(source, selection, **import_blend_keywords)`` or with the dict
    from ``import_snapshot`` as ``source``. ``cancelled`` (threading.Event) is checked between
    Blender results (a running evaluation finishes or times out first). ``report(text, error)``
    must be thread-safe; it is also used by build_import. Folders are expanded by build_import.
    ``evaluate(source, executable, requests, texture_directory, timeout, source_config, consume)``
    replaces ``blend_worker.evaluate_many`` (tests).
    """
    if isinstance(source, dict):
        snapshot = source
        if "selection" not in snapshot:
            raise BlendImportError("prepare_import needs a .blend path or the dict from import_snapshot")
    else:
        snapshot = plain_snapshot(
            source,
            selection,
            storage=storage,
            structure=structure,
            materials=materials,
            frames=frames,
            include_disabled=include_disabled,
            asset_root=asset_root,
            parent=parent,
            position=position,
            executable=executable,
            timeout=timeout,
            convert_vectors=convert_vectors,
            library_root=library_root,
            apply_subdivision=apply_subdivision,
            nested_instances=nested_instances,
            on_shared=on_shared,
        )
    _check(cancelled)
    messages = [] if snapshot.get("notices_reported") else list(snapshot.get("notices", ()))
    if report is not None:
        for text in messages:
            report(text, False)
        messages = []
    payload = dict(snapshot=snapshot, items=[], materials={}, messages=messages, staging=None, report=report)
    storage, structure = snapshot["storage"], snapshot["structure"]
    if storage == "reference":
        for kind, name in snapshot["selection"]:
            payload["items"].append(dict(kind=kind, name=name, asset=name, frames=[]))
        return payload
    executable = _resolve_executable(snapshot)
    staging = tempfile.mkdtemp(prefix="cycles_blend_convert_")
    payload["staging"] = staging
    try:
        requests = requests_for(snapshot)
        frame_count = len(snapshot["frame_values"])
        spill = frame_count > 1
        # asset: the Blender name; the folder name is allocated in cycles_import.json by build_import.
        items = [
            dict(kind=kind, name=name, asset=name, frames=[], report=Report(), info={})
            for kind, name in snapshot["selection"]
        ]
        material_names = {}
        references = {}

        def consume(index, result):
            _check(cancelled)
            item = items[index // frame_count]
            frame_index = index % frame_count
            if isinstance(result, Exception):
                raise BlendImportError(f"Blender could not export {item['kind']} {item['name']}: {result}")
            manifest, arrays = result
            report = item["report"]
            if frame_index == 0:
                # Legacy particle hair is not converted: said in the import log too, not only
                # counted among the skipped items of the report.
                for diagnostic in manifest.get("diagnostics", ()):
                    if diagnostic.get("code") == "blend.particle_hair":
                        text = f"{item['name']}: {diagnostic.get('message')}"
                        if payload["report"] is not None:
                            payload["report"](text, False)
                        else:
                            payload["messages"].append(text)
                request_dir = Path(staging) / ("request-" + str(index))
                for record in manifest.get("materials", []):
                    if record["key"] in payload["materials"] or snapshot["materials"] == "none":
                        continue
                    entry = _material_entry(record, snapshot["source"], request_dir)
                    if entry is not None:
                        payload["materials"][record["key"]] = entry
                for record in manifest.get("materials", []):
                    if not record.get("default"):
                        material_names.setdefault(record["key"], record.get("name") or record["key"])
                item["info"] = dict(
                    blender_version=manifest.get("blender_version"),
                    scene=manifest.get("scene"),
                    frame=manifest.get("frame"),
                    units=manifest.get("units"),
                    objects=[
                        dict(name=entry["name"], type=entry["type"], parent=entry.get("parent"))
                        for entry in manifest.get("objects", [])
                    ],
                )
            frame = assemble(
                manifest,
                arrays,
                structure,
                material_names,
                report,
                dict(
                    convert_vectors=snapshot.get("convert_vectors", True),
                    share_shapes=storage == "files" and frame_count == 1,
                    render_subdivision=not snapshot.get("apply_subdivision", False),
                    nested_instances=bool(snapshot.get("nested_instances", True)),
                ),
                reference=references.get(item["name"]),
            )
            if structure == "hierarchy" and frame_index == 0:
                references[item["name"]] = {
                    entry["name"]: [float(v) for v in np.asarray(entry["world"]).reshape(-1)]
                    for entry in frame["objects"]
                }
            if spill:
                path = Path(staging) / (f"frame-{index:06d}.npz")
                frame = _spill(frame, path)
            item["frames"].append(frame)

        if evaluate is None:
            from . import blend_worker

            evaluate = blend_worker.evaluate_many
        results = evaluate(
            snapshot["source"],
            executable,
            requests,
            staging,
            snapshot["timeout"],
            snapshot.get("source_config"),
            consume,
        )
        _check(cancelled)
        for result in results or ():
            if isinstance(result, Exception):
                raise BlendImportError(f"Blender export failed: {result}")
        for item in items:
            if len(item["frames"]) != frame_count:
                raise BlendImportError(f"Blender returned no geometry for {item['kind']} {item['name']}")
        for item in items:
            item["report"] = item["report"].as_dict()
            # Attributes that did not arrive: one line per object, in the import log (like the
            # particle hair warning above), not only in cycles_import.json.
            for text in attribute_lines(item["report"]):
                if payload["report"] is not None:
                    payload["report"](text, False)
                else:
                    payload["messages"].append(text)
        payload["items"] = items
        payload["material_names"] = material_names
        return payload
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


# ------------------------------------------------------------------ HOM writer


def _hou_attrib_type(hou, owner):
    return {
        "point": hou.attribType.Point,
        "vertex": hou.attribType.Vertex,
        "prim": hou.attribType.Prim,
        "detail": hou.attribType.Global,
    }[owner]


def write_geometry(hou, geo, children=None, material_paths=None, _frozen=None):
    """hou.Geometry for a Geo; packed children are written once, frozen and shared.

    material_paths {Blender material key: /mat path}: packed children get a primitive
    shop_materialpath (a Material SOP cannot reach inside packed primitives).
    """
    children = children or {}
    frozen = {} if _frozen is None else _frozen
    result = hou.Geometry()
    points = result.createPoints(np.asarray(geo.P, dtype=np.float64).tolist()) if geo.num_points else ()
    polygons, polylines = (), ()
    if len(geo.poly_counts):
        polygons = result.createPolygons(_point_lists(geo.poly_points, geo.poly_counts), True)
    if len(geo.line_counts):
        polylines = result.createPolygons(_point_lists(geo.line_points, geo.line_counts), False)
    packed_prims = []
    for index, key in enumerate(geo.packed_children):
        if key not in frozen:
            child = children[key]
            if material_paths:
                child = _with_material_paths(child, material_paths)
            frozen[key] = write_geometry(hou, child, children, material_paths, frozen).freeze(True)
        prim = result.createPacked("PackedGeometry", points[int(geo.packed_points[index])])
        prim.setEmbeddedGeometry(frozen[key])
        matrix = np.array(geo.packed_matrices[index], dtype=np.float64)
        matrix[:3, 3] = geo.P[int(geo.packed_points[index])]
        prim.setTransform(hou.Matrix4(row_matrix(matrix)))
        packed_prims.append(prim)
    for name, attrib in geo.attribs.items():
        kind = attrib.kind
        if kind == "string":
            default = attrib.fill() or ""
        else:
            fill = attrib.fill()
            caster = float if kind == "float" else int
            default = caster(fill) if attrib.width == 1 else tuple(caster(fill) for _ in range(attrib.width))
        added = result.addAttrib(_hou_attrib_type(hou, attrib.owner), name, default, attrib.normal, False)
        if attrib.info:
            # Houdini's type info (the Geometry Spreadsheet's qualifier; Transform SOPs then turn
            # quaternions and matrices). Stored as the attribute option "type".
            added.setOption("type", attrib.info)
        setter = {
            ("point", "float"): result.setPointFloatAttribValues,
            ("point", "int"): result.setPointIntAttribValues,
            ("point", "string"): result.setPointStringAttribValues,
            ("vertex", "float"): result.setVertexFloatAttribValues,
            ("vertex", "int"): result.setVertexIntAttribValues,
            ("vertex", "string"): result.setVertexStringAttribValues,
            ("prim", "float"): result.setPrimFloatAttribValues,
            ("prim", "int"): result.setPrimIntAttribValues,
            ("prim", "string"): result.setPrimStringAttribValues,
        }[(attrib.owner, kind)]
        if kind == "string":
            setter(name, [str(value) for value in attrib.values])
        else:
            dtype = np.float64 if kind == "float" else np.int64
            setter(name, np.asarray(attrib.values, dtype=dtype).reshape(-1).tolist())
    prims = tuple(polygons) + tuple(polylines) + tuple(packed_prims)
    for name, members in geo.prim_groups.items():
        group = result.createPrimGroup(name)
        group.add([prims[int(i)] for i in members])
    for name, pairs in geo.edge_groups.items():
        group = result.createEdgeGroup(name)
        edges = [result.findEdge(points[int(a)], points[int(b)]) for a, b in np.asarray(pairs).reshape(-1, 2)]
        edges = [edge for edge in edges if edge is not None]
        if edges:
            group.add(edges)
    return result


def _point_lists(points, counts):
    """Per-primitive point number lists (Python ints) for hou.Geometry.createPolygons."""
    bounds = np.cumsum(counts)[:-1]
    return [chunk.tolist() for chunk in np.split(np.asarray(points, dtype=np.int64), bounds)]


def _with_material_paths(geo, material_paths):
    if not geo.material_groups:
        return geo
    values = [""] * geo.num_prims
    for group, key in geo.material_groups.items():
        path = material_paths.get(key)
        if path:
            for member in geo.prim_groups.get(group, ()):
                values[int(member)] = path
    if not any(values):
        return geo
    copy = Geo()
    copy.__dict__.update(geo.__dict__)
    copy.attribs = dict(geo.attribs)
    copy.attribs["shop_materialpath"] = Attrib("prim", "string", values, default="")
    return copy


# ------------------------------------------------------------------ files


def _sha256_file(path):
    with open(path, "rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _structure_files(store, original, structure):
    """(previous, kept, structures) of an asset's record for an import of `structure`.

    All structures of one asset share one record and one folder. ``options["structures"]`` lists the files each structure
    wrote; ``previous`` {path: digest} are those of `structure` (and any file of no listed
    structure), ``kept`` those of the other structures, which this import never deletes.
    """
    record = store.asset_record(original) or {}
    listed = (record.get("options") or {}).get("structures")
    structures = {}
    if isinstance(listed, dict):
        for name, paths in listed.items():
            if isinstance(name, str) and isinstance(paths, list):
                structures[name] = [path for path in paths if isinstance(path, str)]
    others = {path.casefold() for name, paths in structures.items() if name != structure for path in paths}
    previous, kept = {}, {}
    for path, digest in store.asset_files(original).items():
        (kept if path.casefold() in others else previous)[path] = digest
    return previous, kept, structures


def _scene_names(scenes):
    return ", ".join(Path(scene).name or scene for scene in scenes)


class _Session:
    """One files-storage import's blend folder and its cycles_import.json (blend-import.md: Import record).

    The folder lock is held from planning (names, the previous records) through writing
    the files to saving the JSON. Only then are files of a previous import that this one
    no longer writes deleted: those the JSON lists for the same asset and structure, and
    only when the JSON was read intact (ImportStore.stale_files). A JSON of a newer
    schema stops the import before anything is written.
    """

    def __init__(self, snapshot, report):
        from .blend_library_materials import blend_signature

        self.snapshot, self.report = snapshot, report
        self.blend_dir = layout.blend_folder(snapshot["asset_root_abs"], snapshot["source"])
        self.json_path = layout.json_path(self.blend_dir)
        self.written, self.saved, self.done = False, False, []
        self.created = not Path(self.blend_dir).exists()
        self._stack = contextlib.ExitStack()
        try:
            self.store = self._stack.enter_context(layout.open_store(self.blend_dir))
        except layout.ImportLockError as error:
            raise BlendImportError(str(error)) from None
        if not self.store.writable:
            self.close()
            raise BlendImportError(
                f"{self.json_path.as_posix()} was written by a newer version of Cycles for Houdini and is left "
                "unchanged; nothing was imported. Update the plug-in or import into a scene in another folder."
            )
        for note in self.store.notes:
            report(note + " Files of earlier imports into this folder are not deleted.", True)
        self.store.set_source(snapshot["source"], blend_signature(snapshot["source"]))

    def close(self):
        self._stack.close()
        if self.created and not self.saved:
            # The lock created the folder for an import that wrote nothing: an empty folder
            # without a JSON would otherwise read as another .blend's (blend_folder).
            with contextlib.suppress(OSError):
                os.rmdir(self.blend_dir)

    def plan(self, item, frames):
        """Allocate the item's folder and file names (nothing is written). Stored as item["plan"]."""
        snapshot, store = self.snapshot, self.store
        structure = snapshot["structure"]
        original = asset_original(item["kind"], item["name"])
        asset = store.asset_name(original, item["name"])
        previous, kept, structures = _structure_files(store, original, structure)
        frame_values = snapshot["frame_values"]
        sequence = len(frame_values) > 1

        def files(stem):
            if sequence:
                return [layout.geometry_file(stem, frame) for frame in frame_values]
            return [layout.geometry_file(stem)]

        names = {}  # ("flat", part suffix) | ("object", Blender name) | ("shape", shape key) -> [relative paths]
        if structure == "hierarchy":
            for entry in frames[0]["objects"]:
                if entry.get("shape"):
                    key = ("shape", entry["shape"])
                    if key not in names:
                        names[key] = [layout.shape_file(store.shape_name(asset, entry["name"]))]
                elif entry["node_type"] == "geo":
                    stem = store.object_name(asset, entry["name"])
                    names[("object", entry["name"])] = [layout.HIERARCHY_FOLDER + "/" + n for n in files(stem)]
        else:
            for suffix in _part_suffixes(frames[0].get("parts") or [None]):
                names[("flat", suffix)] = files(asset + suffix)
        hip = snapshot.get("scene") or ""
        plan = dict(
            original=original,
            asset=asset,
            folder=layout.asset_folder(self.blend_dir, asset),
            names=names,
            previous=previous,
            kept=kept,
            structures=structures,
            # A re-import replaces or deletes files of this structure: other scenes see the change.
            shared=store.other_scenes(original, hip) if previous else [],
        )
        item["plan"] = plan
        return plan

    def check(self, plans):
        """Before writing: the path-length limit (blend-import.md: Folder layout) and the shared-scene rule
        (blend-import.md: Import record)."""
        paths = [self.json_path] + [
            plan["folder"] / name for plan in plans for v in plan["names"].values() for name in v
        ]
        try:
            layout.check_path_lengths(paths, layout.MAX_PATH)
        except layout.PathTooLongError as error:
            raise BlendImportError(str(error)) from None
        for plan in plans:
            if not plan["shared"]:
                continue
            text = f'"{plan["asset"]}" is also used by: {_scene_names(plan["shared"])}.'
            if self.snapshot.get("on_shared") == "cancel":
                raise BlendImportError(text + " Import cancelled; nothing was changed.")
            self.report(text + " Replacing it changes that scene too.", False)

    def finish_item(self, payload, item, sources):
        """Record the item's files and options; returns the node user data entries."""
        snapshot, plan = self.snapshot, item["plan"]
        structure = snapshot["structure"]
        files = {**plan["kept"], **sources.files}
        structures = {name: paths for name, paths in plan["structures"].items() if name != structure}
        structures[structure] = sorted(sources.files)
        options = _asset_options(payload, item)
        options["structures"] = structures
        stale = self.store.stale_files(plan["original"], files)  # read before the record is replaced
        self.store.record_asset(
            plan["original"],
            options=options,
            frames=list(snapshot["frames"] or []),
            files=files,
            scene=snapshot.get("scene"),
        )
        self.done.append((plan, sources, stale))
        return dict(
            manifest=self.json_path.as_posix(),
            blend_folder=Path(self.blend_dir).as_posix(),
            asset_folder=plan["folder"].as_posix(),
        )

    def commit(self, payload):
        """Save the JSON, then delete the stale files of the previous imports (never before)."""
        self.store.save()
        self.saved = True
        for plan, sources, stale in self.done:
            folder = plan["folder"]
            try:
                result = layout.delete_stale(folder, stale)
            except layout.UnsafePathError as error:
                self.report(f"Old files of {folder} were not deleted: {error}", True)
                result = layout.DeleteResult([], [])
            for name, error in result.failed:
                self.report(f"Could not delete the old file {(folder / name).as_posix()}: {error}", True)
            sources.finish(result.removed)

    def save_partial(self):
        """After a failure once files were written: keep the names given (folders stay owned)."""
        if self.written and not self.saved:
            with contextlib.suppress(Exception):
                self.store.save()
                self.saved = True


def _write_atomic(hou, geometry, destination):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(".tmp-" + os.urandom(6).hex() + "-" + destination.name)
    try:
        geometry.saveToFile(str(temporary))
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()


def _variable_path(root_text, root_abs, absolute):
    """Use the unexpanded root ($HIP/...) in parms when it names exactly the written file."""
    absolute = Path(absolute)
    if root_text and root_abs and "$" in root_text:
        try:
            relative = absolute.resolve().relative_to(Path(root_abs).resolve())
        except ValueError:
            return absolute.as_posix()
        if root_text.rstrip("/") != Path(root_abs).as_posix().rstrip("/"):
            return root_text.rstrip("/") + "/" + relative.as_posix()
    return absolute.as_posix()


# ------------------------------------------------------------------ build (HOM)


def _set(node, name, value):
    parm = node.parm(name)
    if parm is None:
        raise BlendImportError(f"{node.path()} has no parameter {name}")
    parm.set(value)


def _place(node, position):
    if position is not None:
        try:
            node.setPosition(position)
            return
        except Exception:
            pass
    try:
        node.moveToGoodPosition()
    except Exception:
        pass


def _user_data(node, payload, item, extra=None):
    """The import record of the stock networks (fallback when the node assets are not installed)."""
    snapshot = payload["snapshot"]
    data = dict(
        source=snapshot["source"],
        kind=item["kind"],
        name=item["name"],
        storage=snapshot["storage"],
        structure=snapshot["structure"],
        materials=snapshot["materials"],
        frames=snapshot["frames"],
        include_disabled=snapshot["include_disabled"],
        apply_subdivision=snapshot.get("apply_subdivision", False),
        nested_instances=snapshot.get("nested_instances", True),
        **(extra or {}),
    )
    try:
        node.setUserData(NODE_USER_DATA, json.dumps(data, ensure_ascii=False, separators=(",", ":")))
    except Exception:
        pass


def _material_record(entry):
    return {"name": entry["local_name"], "ch": {"source": entry["source"], "key": ""}, "id_type": "MATERIAL"}


def _build_materials(hou, payload, used, report, session=None, reused=None):
    """{Blender material key: /mat node path} for the used, translatable materials.

    Packed images go to ``<asset root>/<blend>/tex`` of the material's own .blend (through
    the import's open session when that is the imported .blend); other textures stay in
    place. A missing texture is reported as a warning (material.missing_texture).
    ``reused`` (list) receives (key, node, mode) for materials found in the scene and reused
    (Update reloads the linked ones and leaves editable ones alone).
    """
    snapshot = payload["snapshot"]
    mode = snapshot["materials"]
    if mode == "none":
        return {}
    from ..assets import importers
    from . import blend_library_materials

    paths, in_place = {}, set()
    root = snapshot["asset_root_abs"]
    for key in used:
        entry = payload["materials"].get(key)
        if entry is None:
            continue
        # Link reuses the shared node; Append reuses this scene's editable copy of the same
        # material (as the Asset Browser does), so edits survive a re-import and the bindings
        # written into replaced geometry files stay valid for every node reading them.
        existing = importers.find_imported_material(_material_record(entry), mode)
        found_mode = mode
        if existing is None and mode == "link" and reused is not None:
            # Update: a linked material the artist made editable (Make Editable) is that
            # material now. It is kept and listed, never replaced by a new linked copy.
            existing = importers.find_imported_material(_material_record(entry), "append")
            found_mode = "append"
        if existing is not None:
            paths[key] = existing.path()
            if reused is not None:
                reused.append((key, existing, found_mode))
            continue
        if entry.get("unsupported"):
            report(f"Material {entry['name']} is not translated: {entry['unsupported']}", True)
            continue
        record = json.loads(json.dumps(entry["record"]))  # placement rewrites file names
        diagnostics = []
        try:
            own = session is not None and layout.same_path(entry["source"], snapshot["source"])
            blend_dir = session.blend_dir if own else layout.blend_folder(root, entry["source"])
            if own and blend_library_materials.record_images(record):
                session.written = True
            tex = blend_library_materials.place_packed_textures(
                entry["staging"], record, blend_dir, store=session.store if own else None, source=entry["source"]
            )
            in_place.update(
                image["filename"]
                for image in blend_library_materials.record_images(record)
                if image.get("packed") is False and image.get("filename")
            )
            prepared = {
                "record": record,
                "root": str(tex),
                "source": entry["source"],
                "name": entry["local_name"],
                "asset_root": root,
                "asset_root_text": snapshot["asset_root"],
                "blend_folder": Path(blend_dir).as_posix(),
            }
            node = blend_library_materials.build_library_material(
                prepared, parent=hou.node("/mat"), diagnostics=diagnostics
            )
        except Exception as error:
            report(f"Material {entry['name']} could not be built: {error}", True)
            continue
        for diagnostic in diagnostics:
            if diagnostic.get("code") == "material.missing_texture":
                report(diagnostic.get("message") or "Texture not found: " + str(diagnostic.get("path")), False)
        if mode == "link":
            blend_library_materials.mark_linked(
                node,
                {
                    "source": entry["source"],
                    "name": entry["local_name"],
                    "library_root": snapshot.get("library_root", ""),
                    "signature": importers.source_signature(entry["source"]),
                    "key": "",
                },
            )
        paths[key] = node.path()
    if in_place:
        report(TEXTURES_IN_PLACE.format(count=len(in_place)), False)
    return paths


def _geometry_parts(frame, structure):
    """(label, Geo, children) per geometry the item writes (hierarchy: one per geo object)."""
    if structure == "hierarchy":
        return [(entry["name"], entry["geo"], entry["children"]) for entry in frame["objects"] if entry["geo"]]
    return [("", part["geo"], frame["children"]) for part in frame.get("parts") or [{"geo": frame["geo"]}]]


def _used_materials(frame, structure):
    used = set()
    for geo in (frame.get("shapes") or {}).values() if structure == "hierarchy" else ():
        used.update(geo.material_groups.values())
    for _, geo, children in _geometry_parts(frame, structure):
        used.update(geo.material_groups.values())
        for child in children.values():
            used.update(child.material_groups.values())
    return used


class _Sources:
    """Writes or stashes one item's geometry and creates its source SOPs.

    Files storage: the names come from the item's plan (_Session.plan). A re-import
    overwrites the files in place (skipped when the recorded content digest is identical);
    _Session.commit deletes the files of the previous import of the same structure that
    this one no longer writes.
    """

    def __init__(self, hou, payload, item, material_paths, report):
        self.hou, self.payload, self.item = hou, payload, item
        self.snapshot = payload["snapshot"]
        self.material_paths, self.report = material_paths, report
        self.files = {}  # relative path ("/") -> content digest (cycles_import.json)
        self.folder = None
        self.replaced, self.patterns, self.shape_paths = [], {}, {}
        if self.snapshot["storage"] == "files":
            self.session = payload["session"]
            self.plan = item["plan"]
            self.folder = self.plan["folder"]
            self.previous = self.plan["previous"]

    def encode(self, geo, children):
        """Content digest of what write_geometry writes (children carry their material paths)."""
        digests = {key: geo_digest(_with_material_paths(child, self.material_paths)) for key, child in children.items()}
        return geo_digest(geo, digests)

    def _target(self, name):
        return self.folder.joinpath(*name.split("/"))

    def _save(self, geometry, name):
        self.session.written = True
        _write_atomic(self.hou, geometry, self._target(name))

    def write(self, label, frames_geo, suffix=""):
        """frames_geo: [(Geo, children)] (one per frame). Returns the File SOP path text."""
        hou = self.hou
        sequence = len(self.snapshot["frame_values"]) > 1
        digests = [self.encode(geo, children) for geo, children in frames_geo]
        digest = digests[0] if not sequence else hashlib.sha256("\n".join(digests).encode()).hexdigest()
        names = self.plan["names"][("object", label) if label else ("flat", suffix)]
        # A sequence is reused only as a whole, so each frame records the sequence digest.
        reuse = all(self._target(name).is_file() and self.previous.get(name) == digest for name in names)
        for name, (geo, children) in zip(names, frames_geo, strict=True):
            if not reuse:
                if self._target(name).exists():
                    self.replaced.append(name)
                self._save(write_geometry(hou, geo, children, self.material_paths), name)
            self.files[name] = digest
        if reuse:
            self.report(f"Reused identical geometry {self._target(names[0]).as_posix()}", False)
        pattern = _sequence_pattern(names[0]) if sequence else names[0]
        self.patterns[pattern] = self._path_text(pattern)
        return self.patterns[pattern]

    def _path_text(self, pattern):
        return _variable_path(self.snapshot["asset_root"], self.snapshot["asset_root_abs"], self._target(pattern))

    def shape_path(self, key, geo):
        """Path text of the shared shape file (written once per shape and import)."""
        if key not in self.shape_paths:
            bound = _with_material_paths(geo, self.material_paths)  # a Material SOP cannot reach inside
            digest = self.encode(bound, {})
            [name] = self.plan["names"][("shape", key)]
            if not (self._target(name).is_file() and self.previous.get(name) == digest):
                if self._target(name).exists():
                    self.replaced.append(name)
                self._save(write_geometry(self.hou, bound, {}, self.material_paths), name)
            self.files[name] = digest
            self.shape_paths[key] = self._path_text(name)
        return self.shape_paths[key]

    def shape_sop(self, container, key, geo, label):
        """File SOP loading the shared shape file as a packed disk primitive (one file per shape)."""
        node = container.createNode("file", "file1")
        _set(node, "file", self.shape_path(key, geo))
        _set(node, "loadtype", "delayed")
        _mark(node, SOURCE_USER_DATA)
        return node

    def converted(self, label, frames_geo, suffix=""):
        """("files", File SOP path text) or ("stash", hou.Geometry of the first frame)."""
        if self.snapshot["storage"] == "files":
            return "files", self.write(label, frames_geo, suffix)
        geo, children = frames_geo[0]
        return "stash", write_geometry(self.hou, geo, children, self.material_paths)

    def finish(self, removed):
        """After the JSON is saved and stale files deleted: retarget and reload File SOPs."""
        if removed and self.snapshot["structure"] != "hierarchy" and len(self.patterns) == 1:
            # The previous import wrote other names (a frame range <-> one frame): File SOPs that
            # read it follow the replacement instead of pointing at deleted files.
            [(pattern, text)] = self.patterns.items()
            retarget = self.payload.setdefault("retarget", {})
            for old in {_sequence_pattern(name) for name in self.previous} - {pattern}:
                for variant in (self._path_text(old), self._target(old).as_posix()):
                    retarget[variant] = text
        if self.replaced or removed:
            self.report(
                f"Replaced the previous import in {self.folder.as_posix()}"
                + (f" ({len(removed)} old file(s) deleted)" if removed else ""),
                False,
            )
            self.payload.setdefault("replaced_folders", set()).add(str(self.folder))

    def source_sop(self, container, label, frames_geo, suffix=""):
        kind, value = self.converted(label, frames_geo, suffix)
        if kind == "files":
            node = container.createNode("file", "file1")
            _set(node, "file", value)
        else:
            node = container.createNode("stash", "stash1")
            node.parm("stash").set(value)
        _mark(node, SOURCE_USER_DATA)
        return node


def _material_entries(geo, material_paths, mode):
    """[(mat_* group, /mat path)] a Material SOP assigns: groups of unpacked primitives only."""
    if mode == "none":
        return []
    groups = [
        (group, material_paths[key])
        for group, key in sorted(geo.material_groups.items())
        if key in material_paths and len(geo.prim_groups.get(group, ()))
    ]
    # Groups that only exist inside packed children are assigned there (shop_materialpath).
    unpacked = len(geo.poly_counts) + len(geo.line_counts)
    return [(g, p) for g, p in groups if np.any(np.asarray(geo.prim_groups[g]) < unpacked)]


def _sop_chain(container, source, geo, material_paths, mode):
    """source -> Material SOP (unpacked mat_* groups) -> OUT null with display/render flags."""
    last = source
    groups = _material_entries(geo, material_paths, mode)
    if groups:
        material = container.createNode("material", "material1")
        _mark(material, MATERIAL_USER_DATA)
        material.setFirstInput(last)
        _set(material, "num_materials", len(groups))
        for index, (group, path) in enumerate(groups, start=1):
            _set(material, f"group{index}", group)
            _set(material, f"shop_materialpath{index}", path)
        last = material
    out = container.createNode("null", "OUT")
    out.setFirstInput(last)
    out.setDisplayFlag(True)
    out.setRenderFlag(True)
    try:
        container.layoutChildren()
    except Exception:
        pass
    return out


def _category(hou, name):
    getter = getattr(hou, name, None)
    try:
        return getter() if callable(getter) else None
    except Exception:
        return None


def _child_category(network):
    try:
        return network.childTypeCategory()
    except Exception:
        return None


def is_sop_network(hou, network):
    """True for a SOP network (a geo object's contents): a drop there creates the node in it."""
    sop = _category(hou, "sopNodeTypeCategory")
    # HOM returns a new wrapper per call: categories compare with ==, never `is`.
    return network is not None and sop is not None and _child_category(network) == sop


def object_network(hou, network):
    """The object network holding ``network`` (itself when it is one), else /obj."""
    obj = _category(hou, "objNodeTypeCategory")
    current = network
    while current is not None and obj is not None:
        if _child_category(current) == obj:
            return current
        try:
            current = current.parent()
        except Exception:
            break
    return hou.node("/obj")


def _parent_network(hou, snapshot, *, sop=False):
    """The network the import creates its nodes in: the snapshot's parent when it is an object
    network (or, with ``sop``, a SOP network); otherwise the object network around it, or /obj."""
    network = hou.node(snapshot["parent"]) if snapshot.get("parent") else None
    if network is None:
        return hou.node("/obj")
    obj = _category(hou, "objNodeTypeCategory")
    category = _child_category(network)
    if obj is None or category is None or category == obj:
        return network  # unknown categories (tests, odd networks): as given
    if sop and is_sop_network(hou, network):
        return network
    return object_network(hou, network)


def _build_reference(hou, payload, item, network, position):
    """Link: a geo object holding a Blender File node in Link mode, or, dropped into a SOP
    network, the Blender File node itself there. Returns the top-level nodes created."""
    from . import blend_catalogue

    snapshot = payload["snapshot"]
    in_sop = is_sop_network(hou, network)
    if in_sop:
        container = network
    else:
        container = network.createNode("geo", free_name(network, item["name"]), run_init_scripts=False)
        _place(container, position)
    if payload.get("asset_types", (None, None))[0] is not None:
        # Link mode of the Cycles Blender File node: the inner reference node reads the .blend.
        node = container.createNode(
            file_node.FILE_TYPE, free_name(container, item["name"]) if in_sop else "blender_file"
        )
        if in_sop:
            _place(node, position)
        file_node.configure_file_node(
            node,
            source=snapshot["source"],
            kind=item["kind"],
            name=item["name"],
            mode="link",
            include_disabled=snapshot["include_disabled"],
            apply_subdivision=snapshot.get("apply_subdivision", False),
            nested_instances=snapshot.get("nested_instances", True),
            viewport_shape=snapshot.get("viewport_shape"),
        )
        _flags(node)
        file_node.record_update(
            node, signature=snapshot.get("source_signature"), storage=None, materials=snapshot["materials"]
        )
        reference = file_node.inner(node, file_node.REFERENCE_NODE)
        if reference is not None:
            blend_catalogue.refresh_node(reference)
        return [node] if in_sop else [container]
    from ..assets.importers import REFERENCE_SOP, target_token

    reference = container.createNode(
        REFERENCE_SOP, free_name(container, item["name"]) if in_sop else "blender_reference"
    )
    if in_sop:
        _place(reference, position)
    if not reference.evalParm("multiple_references"):
        reference.parm("multiple_references").set(1)
        reference.parm("references").set(1)
        reference.parm("references_initialized").set(1)
    reference.parm("file").set(snapshot["source"])
    reference.parm("ref_enabled1").set(1)
    reference.parm("ref_target_kind1").set(item["kind"])
    reference.parm("ref_target1").set(target_token(item["name"]))
    if snapshot["include_disabled"] and item["kind"] == "collection":
        parm = reference.parm("ref_include_disabled_collections1")
        if parm is not None:
            parm.set(1)
    if snapshot.get("viewport_shape"):
        reference.parm("viewport_shape").set(snapshot["viewport_shape"])
    blend_catalogue.initialize_rows(reference)
    blend_catalogue.bind_targets(reference)
    reference.setDisplayFlag(True)
    reference.setRenderFlag(True)
    _user_data(reference if in_sop else container, payload, item)
    blend_catalogue.refresh_node(reference)
    return [reference] if in_sop else [container]


def _asset_options(payload, item):
    """The asset record's options in cycles_import.json: import options, the Blender
    scene facts and the conversion report (lossy, skipped, renamed, notes)."""
    snapshot = payload["snapshot"]
    info = item.get("info", {})
    options = {
        key: snapshot[key]
        for key in (
            "storage",
            "structure",
            "materials",
            "frames",
            "include_disabled",
            "apply_subdivision",
            "nested_instances",
            "convert_vectors",
        )
    }
    options.update(
        selection=dict(kind=item["kind"], name=item["name"]),
        blender=dict(
            version=info.get("blender_version"),
            scene=info.get("scene"),
            frame=info.get("frame"),
            units=info.get("units"),
            objects=info.get("objects", []),
        ),
        report=item.get("report", {}),
    )
    return json.loads(json.dumps(options, default=str))


def _sequence_pattern(name):
    """stem.0001.bgeo.sc -> stem.$F4.bgeo.sc; other names unchanged."""
    match = re.fullmatch(r"(.+)\.\d{4,}\.bgeo\.sc", name)
    return match.group(1) + ".$F4.bgeo.sc" if match else name


def _reload_file_sops(hou, folders, retarget=None):
    """Press Reload on File SOPs that read replaced files (they keep the old geometry otherwise).

    retarget {old file parm text: new text}: File SOPs of a previous import whose files were
    replaced under other names (frame range changes) are pointed at the new files.
    """
    roots = [Path(folder) for folder in folders]
    retarget = {os.path.normcase(key): value for key, value in (retarget or {}).items()}
    try:
        instances = hou.sopNodeTypeCategory().nodeTypes()["file"].instances()
    except Exception:
        return 0
    count = 0
    for node in instances:
        try:
            text = node.parm("file").unexpandedString()
            if os.path.normcase(text.replace("\\", "/")) in retarget:
                node.parm("file").set(retarget[os.path.normcase(text.replace("\\", "/"))])
            path = Path(node.parm("file").eval()).resolve()
        except Exception:
            continue
        if any(path.is_relative_to(root) for root in roots):
            try:
                node.parm("reload").pressButton()
                count += 1
            except Exception:
                pass
    return count


def _part_suffixes(parts):
    """ "" for the first part; "_subdiv", "_subdiv_2", ... for further parts (other subdivision settings)."""
    suffixes = [""]
    for index in range(1, len(parts)):
        suffixes.append("_subdiv" if index == 1 else f"_subdiv_{index}")
    return suffixes


def _part_frames(frames, part, material_paths):
    """[(Geo, children)] per frame for one part (one render-time subdivision setting).

    Primitives at the top of a packed file get shop_materialpath too (the node has
    no Material SOP; blend-import.md: Asset nodes); packed children get theirs in write_geometry.
    """
    wanted = _setting_key(part["subdivision"])
    result = []
    for frame in frames:
        match = next(
            (
                p
                for p in frame.get("parts") or [{"subdivision": None, "geo": frame["geo"]}]
                if _setting_key(p["subdivision"]) == wanted
            ),
            None,
        )
        geo = match["geo"] if match else Geo()
        result.append((_with_material_paths(geo, material_paths) if material_paths else geo, frame["children"]))
    return result


def _flags(node):
    for setter in ("setDisplayFlag", "setRenderFlag"):
        try:
            getattr(node, setter)(True)
        except Exception:
            pass


def _file_node_options(snapshot, item):
    return dict(
        source=snapshot["source"],
        kind=item["kind"],
        name=item["name"],
        mode="converted",
        stash=snapshot["storage"] == "embedded",
        frames=snapshot["frames"],
        include_disabled=snapshot["include_disabled"],
        apply_subdivision=snapshot.get("apply_subdivision", False),
        nested_instances=snapshot.get("nested_instances", True),
    )


def _build_flat(hou, payload, item, frames, network, position, material_paths, report):
    """packed: one geo object per selection item and render-time subdivision setting.

    With the Cycles Blender File asset installed each object holds one Blender File node in
    Converted mode (Part = the subdivision part); otherwise a File / Stash SOP network.
    """
    snapshot = payload["snapshot"]
    asset_type = payload.get("asset_types", (None, None))[0]
    sources = _Sources(hou, payload, item, material_paths, report)
    first = frames[0].get("parts") or [{"subdivision": None, "geo": frames[0]["geo"]}]
    created, file_nodes = [], []
    # Dropped into a SOP network (asset installed): the first part's node goes there; further
    # subdivision parts need objects of their own (Cycles subdivides per object).
    in_sop = asset_type is not None and is_sop_network(hou, network)
    objects = object_network(hou, network) if in_sop else network
    for part, suffix in zip(first, _part_suffixes(first), strict=True):
        frames_geo = _part_frames(frames, part, material_paths)
        here = in_sop and not created
        if here:
            container = network
        else:
            container = objects.createNode("geo", free_name(objects, item["name"] + suffix), run_init_scripts=False)
            if created:
                _place_beside(container, created[-1] if not in_sop or len(created) > 1 else network)
            else:
                _place(container, position)
        if asset_type is not None:
            token = file_node.part_token(suffix, len(first))
            node = container.createNode(
                file_node.FILE_TYPE, free_name(container, item["name"] + suffix) if here else "blender_file"
            )
            if here:
                _place(node, position)
            file_node.configure_file_node(node, part=token, **_file_node_options(snapshot, item))
            kind, value = sources.converted("", frames_geo, suffix)
            file_node.set_converted(
                node, path_text=value if kind == "files" else None, geometry=value if kind == "stash" else None
            )
            _flags(node)
            file_nodes.append((node, token, kind, part["subdivision"]))
        else:
            source = sources.source_sop(container, "", frames_geo, suffix=suffix)
            _sop_chain(container, source, part["geo"], material_paths, snapshot["materials"])
        if part["subdivision"] and here:
            # The artist's own object: its Cycles settings are left alone (warning; blend-import.md: Asset nodes).
            report(
                f"{item['name']}: subdivided at render time in Blender; {network.path()} does not "
                "subdivide it. Give it its own object, or import with Apply Subdivision.",
                True,
            )
        elif part["subdivision"]:
            _apply_subdivision(container, part["subdivision"], report)
        created.append(node if here else container)
    extra = {}
    if sources.folder is not None:
        extra = payload["session"].finish_item(payload, item, sources)
    if asset_type is not None:
        for node, token, kind, setting in file_nodes:
            file_node.record_update(
                node,
                signature=snapshot.get("source_signature"),
                storage=kind,
                materials=snapshot["materials"],
                part=token,
                subdivision=setting,
            )
    else:
        for container in created:
            _user_data(container, payload, item, extra)
    if len(created) > 1:
        report(
            f"{item['name']}: split into {len(created)} objects, one per render-time subdivision setting "
            "(Cycles subdivides per object)",
            False,
        )
    return created


def _place_beside(node, previous):
    try:
        node.setPosition(previous.position() + type(previous.position())(0, -1))
    except Exception:
        try:
            node.moveToGoodPosition()
        except Exception:
            pass


def _apply_subdivision(node, setting, report):
    """Render-time subdivision on the object's Cycles tab (Blender's unapplied Subsurf)."""
    problem = None
    try:
        from ..scene import object_properties

        object_properties.install(node)
    except Exception as error:
        problem = error
    values = dict(
        cycles_subdivision_override=1,
        cycles_subdivision_type=setting["type"],
        cycles_subdivision_adaptive=0,
        cycles_subdivision_ipr=1,
        cycles_subdivision_ipr_level=setting["ipr_level"],
        cycles_subdivision_render_level=setting["render_level"],
        cycles_subdivision_boundary=setting["boundary"],
        cycles_subdivision_fvar=setting["fvar"],
        cycles_subdivision_use_creases=int(setting["use_creases"]),
    )
    for name, value in values.items():
        parm = node.parm(name)
        if parm is None:
            reason = f": {problem}" if problem else ""
            report(f"{node.path()}: could not set render-time subdivision ({name} missing{reason})", True)
            return
        parm.set(value)


def _mark(node, key, value="1"):
    try:
        node.setUserData(key, value)
    except Exception:
        pass


def _flat_matrix(matrix):
    """16 floats of a hou.Matrix4 (or the fakes' tuple of rows)."""
    if matrix is None:
        return None
    try:
        values = matrix.asTuple()
    except AttributeError:
        values = [value for row in matrix for value in row]
    return [float(value) for value in values]


def _local_flat(entry):
    return [value for row in row_matrix(entry["local"]) for value in row]


def _object_frames(frames, name):
    result = []
    for frame in frames:
        match = next(o for o in frame["objects"] if o["name"] == name)
        result.append((match["geo"], match["children"]))
    return result


def _hierarchy_object(hou, network, entry, frames, sources, material_paths, mode):
    """One geo (or null) object for a Blender object, with its source chain and local transform."""
    name = free_name(network, entry["name"], "object")
    node = network.createNode("geo" if entry["node_type"] == "geo" else "null", name, run_init_scripts=False)
    if entry.get("shape"):
        source = sources.shape_sop(node, entry["shape"], frames[0]["shapes"][entry["shape"]], entry["name"])
        _sop_chain(node, source, Geo(), material_paths, "none")
    elif entry["node_type"] == "geo":
        source = sources.source_sop(node, entry["name"], _object_frames(frames, entry["name"]))
        _sop_chain(node, source, entry["geo"], material_paths, mode)
    node.setParmTransform(hou.Matrix4(row_matrix(entry["local"])))
    _mark(node, file_node.OBJECT_USER_DATA, entry["name"])
    return node


def _build_hierarchy(hou, payload, item, frames, network, position, material_paths, report, transforms=None):
    """One geo/null object per Blender object, parented like Blender, under a collection null.

    ``network``: /obj (stock networks) or the Cycles Blender Hierarchy object. ``transforms``
    (dict) receives each object's written local transform (16 floats, row vectors).
    """
    snapshot = payload["snapshot"]
    sources = _Sources(hou, payload, item, material_paths, report)
    created, nodes = [], {}
    root = None
    if item["kind"] == "collection":
        root = network.createNode("null", free_name(network, item["name"]))
        _mark(root, file_node.COLLECTION_USER_DATA, item["name"])
        _place(root, position)
        created.append(root)
    first = frames[0]["objects"]
    subdivided = []
    for entry in first:
        node = _hierarchy_object(hou, network, entry, frames, sources, material_paths, snapshot["materials"])
        nodes[entry["name"]] = node
        if transforms is not None:
            transforms[entry["name"]] = _local_flat(entry)
        if entry.get("subdivision"):
            subdivided.append((node, entry["subdivision"]))
    for entry in first:
        node = nodes[entry["name"]]
        parent = nodes.get(entry["parent"]) if entry["parent"] else root
        if parent is not None:
            node.setFirstInput(parent)
        else:
            # Object selections have no collection null: their root objects are the result.
            created.append(node)
        _place(node, position if node is (created[0] if created else None) and root is None else None)
    for node, setting in subdivided:
        _apply_subdivision(node, setting, report)
    extra = {}
    if sources.folder is not None:
        extra = payload["session"].finish_item(payload, item, sources)
    if payload.get("asset_types", (None, None))[1] is None:
        for node in [root] if root is not None else created:
            _user_data(node, payload, item, extra)
    return created


def _hierarchy_name(snapshot):
    selection = snapshot["selection"]
    if len(selection) == 1:
        return selection[0][1]
    return Path(snapshot["source"]).stem


def asset_types(hou=None):
    """(Blender File SOP type, Blender Hierarchy object type); None for each one not installed."""
    import hou as host

    return file_node.node_types(hou or host)


def build_import(payload, *, undo=True, report=None, on_shared=None):
    """Host thread (HOM): create the nodes for a prepared payload; one undo group. Returns top-level nodes.

    Creates (blend-import.md: Importer dialog): storage "reference" -> a geo object with a Cycles Blender File node in
    Link mode per item; "files" / "embedded" with structure "packed" -> a geo object per item
    and subdivision part with a Blender File node in Converted mode (Stash in .hip on for
    "embedded"); structure "hierarchy" -> one Cycles Blender Hierarchy object holding the
    hierarchy. When those assets are not installed, the stock networks of earlier builds are
    created and the report says so.

    ``on_shared`` ("replace" | "cancel") overrides the snapshot's choice for a re-import that
    would change files another scene uses (see shared_scenes; blend-import.md: Import record).
    """
    import hou

    messages = []

    def say(text, error=False):
        messages.append((text, error))

    report = report or payload.get("report")
    snapshot = payload["snapshot"]
    if on_shared is not None:
        if on_shared not in ON_SHARED:
            raise BlendImportError("on_shared must be one of " + ", ".join(ON_SHARED))
        snapshot["on_shared"] = on_shared
    if snapshot.get("structure") == "merged":
        raise BlendImportError(MERGED_REMOVED)
    types = asset_types(hou)
    payload["asset_types"] = types
    hierarchy = snapshot["structure"] == "hierarchy" and snapshot["storage"] != "reference"
    if types[1 if hierarchy else 0] is None:
        name = file_node.HIERARCHY_TYPE if hierarchy else file_node.FILE_TYPE
        say(f"The {name} asset is not installed: created File / Stash / reference nodes instead.", False)
    group = hou.undos.group(UNDO_LABEL) if undo else contextlib.nullcontext()
    session = None
    try:
        for text in resolve_folders(snapshot):
            say(text)
        if snapshot["storage"] == "reference":
            with group:
                network = _parent_network(hou, snapshot, sop=True)
                position = snapshot.get("position")
                created = []
                for item in payload["items"]:
                    created += _build_reference(hou, payload, item, network, position if not created else None)
        else:
            frames_per_item = [[load_frame(frame) for frame in item["frames"]] for item in payload["items"]]
            if snapshot["storage"] == "files":
                # Names, path lengths and the shared-scene rule are settled before anything is written.
                session = _Session(snapshot, say)
                payload["session"] = session
                plans = [
                    session.plan(item, frames) for item, frames in zip(payload["items"], frames_per_item, strict=True)
                ]
                session.check(plans)
            with group:
                # A SOP network takes the Blender File nodes of a packed import; a hierarchy or the
                # stock networks go to the object network around it.
                network = _parent_network(hou, snapshot, sop=not hierarchy and types[0] is not None)
                position = snapshot.get("position")
                created = []
                used = set()
                for frames in frames_per_item:
                    used |= _used_materials(frames[0], snapshot["structure"])
                material_paths = _build_materials(hou, payload, sorted(used), say, session)
                container, transforms = None, {}
                if hierarchy and types[1] is not None:
                    container = network.createNode(
                        file_node.HIERARCHY_TYPE, free_name(network, _hierarchy_name(snapshot))
                    )
                    _place(container, position)
                    file_node.configure_hierarchy(
                        container,
                        source=snapshot["source"],
                        selection=[tuple(item) for item in snapshot["selection"]],
                        stash=snapshot["storage"] == "embedded",
                        frames=snapshot["frames"],
                        include_disabled=snapshot["include_disabled"],
                        apply_subdivision=snapshot.get("apply_subdivision", False),
                        nested_instances=snapshot.get("nested_instances", True),
                    )
                for item, frames in zip(payload["items"], frames_per_item, strict=True):
                    if container is not None:
                        _build_hierarchy(hou, payload, item, frames, container, None, material_paths, say, transforms)
                    else:
                        build = _build_hierarchy if hierarchy else _build_flat
                        created += build(
                            hou, payload, item, frames, network, position if not created else None, material_paths, say
                        )
                    lossy = (item.get("report") or {}).get("lossy") or []
                    skipped = (item.get("report") or {}).get("skipped") or []
                    if lossy or skipped:
                        where = "cycles_import.json" if session is not None else "the node user data"
                        counts = f"{len(lossy)} lossy conversion(s), {len(skipped)} skipped item(s)"
                        say(f"{item['name']}: {counts}, see {where}", False)
                if container is not None:
                    _layout(container)
                    file_node.record_update(
                        container,
                        signature=snapshot.get("source_signature"),
                        storage="files" if snapshot["storage"] == "files" else "stash",
                        materials=snapshot["materials"],
                        transforms=transforms,
                    )
                    created = [container]
                if session is not None:
                    session.commit(payload)
                if payload.get("replaced_folders"):
                    _reload_file_sops(hou, sorted(payload["replaced_folders"]), payload.get("retarget"))
        if created:
            try:
                created[0].setCurrent(True, clear_all_selected=True)
            except Exception:
                pass
    except layout.ForeignStoreError as error:
        raise BlendImportError(str(error)) from None
    except BaseException:
        if session is not None:
            session.save_partial()
        raise
    finally:
        if session is not None:
            session.close()
        payload.pop("session", None)
        if payload.get("staging"):
            shutil.rmtree(payload["staging"], ignore_errors=True)
            payload["staging"] = None
    if report is not None:
        for text in payload.get("messages", ()):
            report(text, False)
        for text, error in messages:
            report(text, error)
    payload["build_messages"] = messages
    return created


def _layout(network):
    try:
        network.layoutChildren()
    except Exception:
        pass


# ------------------------------------------------------------------ update (blend-import.md: Asset nodes)

UPDATE_UNDO_LABEL = "Update Blender assets"


def _norm(path):
    return os.path.normcase(os.path.abspath(path)) if path else ""


def _file_group_key(values, name):
    return (
        _norm(values["source"]),
        values["kind"],
        name,
        bool(values["stash"]),
        tuple(values["frames"]) if values["frames"] else None,
        bool(values["include_disabled"]),
        bool(values["apply_subdivision"]),
        bool(values["nested_instances"]),
        values["materials"],
    )


def update_snapshot(nodes, *, on_shared="replace", report=None, **options):
    """Host thread (HOM): one import snapshot per group of nodes that are one import (blend-import.md: Asset nodes).

    Blender File nodes of the same asset, options and storage are one import (the subdivision
    parts of one asset, and with files storage every node reading those files); each Blender
    Hierarchy object is one import. Link-mode nodes are skipped (reported). ``options`` go to
    import_snapshot (executable, timeout, asset_root, ...).
    """
    import hou

    if on_shared not in ON_SHARED:
        raise BlendImportError("on_shared must be one of " + ", ".join(ON_SHARED))
    groups, order, seen = {}, [], set()
    for node in nodes:
        try:
            node = file_node._node(node)
            if node.path() in seen:
                continue
            seen.add(node.path())
            values = file_node.node_options(node)
            if values["node_kind"] == "hierarchy":
                if not values["selection"]:
                    raise BlendImportError(f"{node.path()}: Selection is empty")
                key = ("hierarchy", node.path())
                groups[key] = dict(node_kind="hierarchy", values=values, selection=values["selection"], nodes=[node])
                order.append(key)
                continue
            if values["mode"] != "converted":
                if report is not None:
                    report(f"{node.path()}: Link mode reads the .blend directly; nothing to update.", False)
                continue
            name = file_node.target_name(node)
        except file_node.NodeError as error:
            raise BlendImportError(str(error)) from None
        key = _file_group_key(values, name)
        if key not in groups:
            groups[key] = dict(node_kind="file", values=values, selection=[(values["kind"], name)], nodes=[])
            order.append(key)
        groups[key]["nodes"].append(node)
    others = [n for n in file_node.all_nodes(hou) if n.path() not in seen and file_node.is_file_node(n)]
    for key in order:
        group = groups[key]
        if group["node_kind"] != "file" or group["values"]["stash"]:
            continue
        for other in others:
            if other.path() in seen:
                continue
            try:
                values = file_node.node_options(other)
                if values["mode"] != "converted":
                    continue
                if _file_group_key(values, file_node.target_name(other)) == key:
                    group["nodes"].append(other)
                    seen.add(other.path())
            except Exception:
                continue
    imports = []
    for key in order:
        group = groups[key]
        values = group["values"]
        if not values["source"] or not os.path.isfile(values["source"]):
            raise BlendImportError(f"{group['nodes'][0].path()}: Source missing: {values['source'] or '(empty)'}")
        snapshot = import_snapshot(
            values["source"],
            group["selection"],
            storage="embedded" if values["stash"] else "files",
            structure="hierarchy" if group["node_kind"] == "hierarchy" else "packed",
            materials=values["materials"],
            frames=values["frames"],
            include_disabled=values["include_disabled"],
            apply_subdivision=values["apply_subdivision"],
            nested_instances=values["nested_instances"],
            on_shared=on_shared,
            report=report,
            **options,
        )
        imports.append(
            dict(
                node_kind=group["node_kind"],
                nodes=[node.path() for node in group["nodes"]],
                fingerprints={node.path(): file_node.fingerprint_of(node) for node in group["nodes"]},
                snapshot=snapshot,
            )
        )
    return dict(kind="update", imports=imports, on_shared=on_shared)


def prepare_update(snapshot, *, cancelled=None, evaluate=None):
    """Any thread, no HOM: prepare_import for every import of an update snapshot."""
    imports = []
    try:
        for entry in snapshot["imports"]:
            _check(cancelled)
            payload = prepare_import(entry["snapshot"], cancelled=cancelled, evaluate=evaluate)
            imports.append(dict(entry, payload=payload))
    except BaseException:
        for entry in imports:
            if entry["payload"].get("staging"):
                shutil.rmtree(entry["payload"]["staging"], ignore_errors=True)
        raise
    return dict(snapshot, imports=imports)


def _reload_linked(reused, say, hook, report):
    """Materials found in the scene on Update: linked -> Reload from Library; editable -> listed."""
    linked, done = [], set()
    for _, node, mode in reused:
        try:
            path = node.path()
        except Exception:
            continue
        if path in done:
            continue
        done.add(path)
        if mode == "link":
            linked.append(node)
        else:
            say(f"Material {path} not updated: editable", False)
    if not linked:
        return
    if hook is None:
        try:
            from ..assets import importers

            hook = importers.reload_linked_async
        except Exception as error:
            say(f"Linked materials were not reloaded ({error}); use Cycles > Reload Selected from Library.", True)
            return
    try:
        hook(linked, report=report)
        say(f"Reloading {len(linked)} linked material(s) from the library", False)
    except Exception as error:
        say(f"Linked materials were not reloaded ({error}); use Cycles > Reload Selected from Library.", True)


def _new_part_node(hou, template, item, suffix, token, snapshot):
    """A sibling geo object + Blender File node for a subdivision part the asset did not have."""
    container = template.parent()
    network = container.parent()
    sibling = network.createNode("geo", free_name(network, item["name"] + suffix), run_init_scripts=False)
    _place_beside(sibling, container)
    node = sibling.createNode(file_node.FILE_TYPE, template.name())
    file_node.configure_file_node(node, part=token, **_file_node_options(snapshot, item))
    for name in ("load_as", "unpack_iterations"):
        value = file_node._eval(template, name)
        if value is not None and node.parm(name) is not None:
            node.parm(name).set(value)
    _flags(node)
    return node


def _session_for(entry, frames_per_item, say):
    snapshot = entry["payload"]["snapshot"]
    if snapshot["storage"] != "files":
        return None
    session = _Session(snapshot, say)
    entry["payload"]["session"] = session
    try:
        plans = [
            session.plan(item, frames) for item, frames in zip(entry["payload"]["items"], frames_per_item, strict=True)
        ]
        session.check(plans)
    except BaseException:
        session.close()
        entry["payload"].pop("session", None)
        raise
    return session


def _sync_file_nodes(hou, entry, nodes, say, hook, report):
    """Update the Blender File nodes of one asset: files or stash, parts, materials, records."""
    payload = entry["payload"]
    snapshot = payload["snapshot"]
    [item] = payload["items"]
    frames = [load_frame(frame) for frame in item["frames"]]
    session = _session_for(entry, [frames], say)
    try:
        reused = []
        material_paths = _build_materials(
            hou, payload, sorted(_used_materials(frames[0], "packed")), say, session, reused
        )
        sources = _Sources(hou, payload, item, material_paths, say)
        first = frames[0].get("parts") or [{"subdivision": None, "geo": frames[0]["geo"]}]
        suffixes = _part_suffixes(first)
        by_suffix = {}
        for node in nodes:
            by_suffix.setdefault(file_node.part_suffix(file_node._eval(node, "part", "")), []).append(node)
        touched, created = [], []
        for part, suffix in zip(first, suffixes, strict=True):
            kind, value = sources.converted("", _part_frames(frames, part, material_paths), suffix)
            token = file_node.part_token(suffix, len(first))
            targets = by_suffix.get(suffix) or []
            if not targets:
                node = _new_part_node(hou, nodes[0], item, suffix, token, snapshot)
                created.append(node)
                targets = [node]
                say(f"{item['name']}: new subdivision part: created {node.parent().path()}", False)
            for node in targets:
                file_node.set_converted(
                    node,
                    path_text=value if kind == "files" else None,
                    geometry=value if kind == "stash" else None,
                    part=token,
                )
                if part["subdivision"] and not _merged_in_object(node):
                    # Not on an object where other geometry joins the part: the warning names the fix.
                    _apply_subdivision(node.parent(), part["subdivision"], say)
                touched.append((node, token, kind, part["subdivision"]))
        for suffix, kept in by_suffix.items():
            if suffix not in suffixes:
                for node in kept:
                    say(f"{node.path()}: its part no longer exists in the .blend; the node was kept", False)
        if session is not None:
            session.finish_item(payload, item, sources)
            session.commit(payload)
        if payload.get("replaced_folders"):
            _reload_file_sops(hou, sorted(payload["replaced_folders"]), payload.get("retarget"))
        for node, token, kind, setting in touched:
            file_node.record_update(
                node,
                signature=snapshot.get("source_signature"),
                storage=kind,
                materials=snapshot["materials"],
                options=entry["fingerprints"].get(node.path()),
                part=token,
                subdivision=setting,
            )
            warning = file_node.subdivision_problem(node)
            if warning:
                say(f"{node.path()}: {warning}", True)
        _reload_linked(reused, say, hook, report)
        return dict(nodes=[node for node, *_ in touched], created=created)
    except BaseException:
        if session is not None:
            session.save_partial()
        raise
    finally:
        if session is not None:
            session.close()
        payload.pop("session", None)


def _merged_in_object(node):
    """True when the node's output joins other geometry before its object's render node."""
    container = node.parent()
    for getter in ("renderNode", "displayNode"):
        try:
            render = getattr(container, getter)()
        except Exception:
            render = None
        if render is not None:
            return file_node.upstream_of(render, node)[1]
    return False


def _marked_child(node, key):
    try:
        children = node.children()
    except Exception:
        return None
    return next((child for child in children if child.userData(key)), None)


def _consumers(node):
    try:
        return list(node.outputs())
    except Exception:
        return []


def _inputs(node):
    inputs = node.inputs
    return list(inputs() if callable(inputs) else inputs)


def _rewire(old, new):
    """Every node reading `old` reads `new` instead."""
    for consumer in _consumers(old):
        if consumer is new:
            continue
        for index, source in enumerate(_inputs(consumer)):
            if source is not None and source.path() == old.path():
                consumer.setInput(index, new)


def _update_material_sop(container, source, geo, material_paths, mode):
    """Material SOP entries: new groups added, vanished mat_* groups removed, the rest untouched."""
    wanted = _material_entries(geo, material_paths, mode)
    material = _marked_child(container, MATERIAL_USER_DATA)
    if material is None:
        if not wanted:
            return
        material = container.createNode("material", "material1")
        _mark(material, MATERIAL_USER_DATA)
        _rewire(source, material)
        material.setFirstInput(source)
        current = []
    else:
        count = int(material.evalParm("num_materials") or 0)
        current = [
            (material.evalParm(f"group{i}"), material.evalParm(f"shop_materialpath{i}")) for i in range(1, count + 1)
        ]
    groups = {group for group, _ in wanted}
    kept = [(g, p) for g, p in current if not (str(g or "").startswith("mat_") and g not in groups)]
    have = {g for g, _ in kept}
    result = kept + [(g, p) for g, p in wanted if g not in have]
    if result == current:
        return
    _set(material, "num_materials", len(result))
    for index, (group, path) in enumerate(result, start=1):
        _set(material, f"group{index}", group)
        _set(material, f"shop_materialpath{index}", path)


def _refresh_hierarchy_object(hou, node, entry, frames, sources, material_paths, snapshot, say):
    """Rewrite an existing hierarchy object's geometry; artist-added nodes are never touched."""
    if entry["node_type"] != "geo" and not entry.get("shape"):
        return
    if file_node._type_name(node) != "geo":
        say(f"{node.path()}: the Blender object has geometry now; delete this null and Update to recreate it", False)
        return
    source = _marked_child(node, SOURCE_USER_DATA)
    wanted = "stash" if snapshot["storage"] == "embedded" else "file"
    if entry.get("shape"):
        kind, value = "files", sources.shape_path(entry["shape"], frames[0]["shapes"][entry["shape"]])
    else:
        kind, value = sources.converted(entry["name"], _object_frames(frames, entry["name"]))
    if source is None or file_node._type_name(source) != wanted:
        replacement = node.createNode(wanted, wanted + "1")
        _mark(replacement, SOURCE_USER_DATA)
        if source is not None:
            _rewire(source, replacement)
            try:
                replacement.setPosition(source.position())
            except Exception:
                pass
            source.destroy()
        else:
            _sop_chain(node, replacement, Geo(), material_paths, "none")
        source = replacement
    if kind == "files":
        _set(source, "file", value)
        if entry.get("shape"):
            _set(source, "loadtype", "delayed")
        try:
            source.parm("reload").pressButton()
        except Exception:
            pass
    else:
        source.parm("stash").set(value)
    if not entry.get("shape"):
        _update_material_sop(node, source, entry["geo"] or Geo(), material_paths, snapshot["materials"])


def _sync_hierarchy(hou, entry, nodes, say, hook, report):
    """Update a Blender Hierarchy object in place ("Update on this node"; blend-import.md: Asset nodes)."""
    [container] = nodes[:1]
    payload = entry["payload"]
    snapshot = payload["snapshot"]
    frames_per_item = [[load_frame(frame) for frame in item["frames"]] for item in payload["items"]]
    session = _session_for(entry, frames_per_item, say)
    try:
        reused, used = [], set()
        for frames in frames_per_item:
            used |= _used_materials(frames[0], "hierarchy")
        material_paths = _build_materials(hou, payload, sorted(used), say, session, reused)
        stored = file_node.record(container).get("transforms") or {}
        transforms = dict(stored)
        objects, collections = {}, {}
        for child in container.allSubChildren():
            name = child.userData(file_node.OBJECT_USER_DATA)
            if name and name not in objects:
                objects[name] = child
            name = child.userData(file_node.COLLECTION_USER_DATA)
            if name and name not in collections:
                collections[name] = child
        present, created = set(), []
        for item, frames in zip(payload["items"], frames_per_item, strict=True):
            sources = _Sources(hou, payload, item, material_paths, say)
            root = None
            if item["kind"] == "collection":
                root = collections.get(item["name"])
                if root is None:
                    root = container.createNode("null", free_name(container, item["name"]))
                    _mark(root, file_node.COLLECTION_USER_DATA, item["name"])
                    created.append(root)
            nodes_by, new = {}, []
            for entry_ in frames[0]["objects"]:
                name = entry_["name"]
                present.add(name)
                node = objects.get(name)
                if node is None:
                    node = _hierarchy_object(
                        hou, container, entry_, frames, sources, material_paths, snapshot["materials"]
                    )
                    objects[name] = node
                    transforms[name] = _local_flat(entry_)
                    new.append(entry_)
                    created.append(node)
                    say(f"{node.path()}: created for the new Blender object {name}", False)
                else:
                    _refresh_hierarchy_object(hou, node, entry_, frames, sources, material_paths, snapshot, say)
                    wanted = _local_flat(entry_)
                    written = stored.get(name)
                    try:
                        current = _flat_matrix(node.parmTransform())
                    except Exception:
                        current = None
                    if written is None or current is None or np.allclose(current, written, atol=1e-6):
                        if current is None or not np.allclose(current, wanted, atol=1e-6):
                            node.setParmTransform(hou.Matrix4(row_matrix(entry_["local"])))
                        transforms[name] = wanted
                    else:
                        say(f"{node.path()}: moved by hand; its transform was kept", False)
                if entry_.get("subdivision"):
                    _apply_subdivision(node, entry_["subdivision"], say)
                nodes_by[name] = node
            for entry_ in new:
                parent = nodes_by.get(entry_["parent"]) if entry_["parent"] else root
                if parent is not None:
                    nodes_by[entry_["name"]].setFirstInput(parent)
            if session is not None:
                session.finish_item(payload, item, sources)
        for name, node in objects.items():
            if name not in present:
                say(f"{node.path()}: the Blender object {name} no longer exists; the object was kept", False)
        if session is not None:
            session.commit(payload)
        if payload.get("replaced_folders"):
            _reload_file_sops(hou, sorted(payload["replaced_folders"]), payload.get("retarget"))
        if created:
            _layout(container)
        file_node.record_update(
            container,
            signature=snapshot.get("source_signature"),
            storage="files" if snapshot["storage"] == "files" else "stash",
            materials=snapshot["materials"],
            options=entry["fingerprints"].get(container.path()),
            transforms={name: transforms[name] for name in present if name in transforms},
        )
        _reload_linked(reused, say, hook, report)
        return dict(nodes=[container], created=created)
    except BaseException:
        if session is not None:
            session.save_partial()
        raise
    finally:
        if session is not None:
            session.close()
        payload.pop("session", None)


def sync_import(payload, nodes=None, *, report=None, undo=True, reload_materials=None):
    """Host thread (HOM), one undo group: apply a prepared update to the existing nodes.

    ``nodes``: None (the nodes recorded in the update snapshot) or, for a single-import update,
    the nodes to update. ``reload_materials(nodes, report=...)`` reloads linked materials
    (default: assets.importers.reload_linked_async, looked up lazily). Returns one dict per
    import: {"nodes": updated nodes, "created": new objects/nodes}.
    """
    import hou

    messages = []

    def say(text, error=False):
        messages.append((text, error))

    if payload.get("on_shared") and payload["on_shared"] not in ON_SHARED:
        raise BlendImportError("on_shared must be one of " + ", ".join(ON_SHARED))
    imports = payload["imports"]
    group = hou.undos.group(UPDATE_UNDO_LABEL) if undo else contextlib.nullcontext()
    results = []
    try:
        with group:
            for entry in imports:
                if nodes is not None and len(imports) == 1:
                    targets = [file_node._node(node) for node in nodes]
                else:
                    targets = [hou.node(path) for path in entry["nodes"]]
                targets = [node for node in targets if node is not None]
                if not targets:
                    say("The nodes of an update were deleted; nothing was changed for them.", True)
                    continue
                for text in resolve_folders(entry["payload"]["snapshot"]):
                    say(text)
                sync = _sync_hierarchy if entry["node_kind"] == "hierarchy" else _sync_file_nodes
                try:
                    results.append(sync(hou, entry, targets, say, reload_materials, report))
                except layout.ForeignStoreError as error:
                    raise BlendImportError(str(error)) from None
                for item in entry["payload"]["items"]:
                    lossy = (item.get("report") or {}).get("lossy") or []
                    skipped = (item.get("report") or {}).get("skipped") or []
                    if lossy or skipped:
                        counts = f"{len(lossy)} lossy conversion(s), {len(skipped)} skipped item(s)"
                        say(f"{item['name']}: {counts}, see cycles_import.json", False)
    finally:
        for entry in imports:
            staging = entry["payload"].get("staging")
            if staging:
                shutil.rmtree(staging, ignore_errors=True)
                entry["payload"]["staging"] = None
    if report is not None:
        for entry in imports:
            for text in entry["payload"].get("messages", ()):
                report(text, False)
        for text, error in messages:
            report(text, error)
    payload["build_messages"] = messages
    return results


apply_update = sync_import


def update_import(
    nodes, *, cancelled=None, report=None, on_shared="replace", reload_materials=None, evaluate=None, **options
):
    """Synchronous Update (blocks while Blender works): update_snapshot, prepare_update, sync_import.

    The Update button and Update All Blender Files run the same steps with the export on a
    background thread (blend_file_node.start_update).
    """
    snapshot = update_snapshot(nodes, on_shared=on_shared, report=report, **options)
    payload = prepare_update(snapshot, cancelled=cancelled, evaluate=evaluate)
    return sync_import(payload, None, report=report, reload_materials=reload_materials)


def import_blend(
    source,
    selection,
    *,
    storage="files",
    structure="packed",
    materials="link",
    frames=None,
    include_disabled=False,
    asset_root=DEFAULT_ASSET_ROOT,
    parent=None,
    position=None,
    executable=None,
    timeout=300,
    report=None,
    convert_vectors=True,
    apply_subdivision=False,
    nested_instances=True,
    on_shared="replace",
):
    """Synchronous wrapper (blocks while Blender works): snapshot, prepare, build."""
    snapshot = import_snapshot(
        source,
        selection,
        storage=storage,
        structure=structure,
        materials=materials,
        frames=frames,
        include_disabled=include_disabled,
        asset_root=asset_root,
        parent=parent,
        position=position,
        executable=executable,
        timeout=timeout,
        report=report,
        convert_vectors=convert_vectors,
        apply_subdivision=apply_subdivision,
        nested_instances=nested_instances,
        on_shared=on_shared,
    )
    payload = prepare_import(snapshot)
    return build_import(payload, report=report)


def shared_scenes(source, selection=None, *, asset_root=DEFAULT_ASSET_ROOT, structure=None, library_root=None):
    """Host thread (HOM), before importing: the other scenes a re-import would change (blend-import.md: Import record).

    ``source`` is a .blend path (with ``selection``, [(kind, name)]) or the dict from
    import_snapshot. Returns [{"kind", "name", "asset", "scenes"}] for each selected asset
    that was imported before (with ``structure``: in that structure) and whose record names
    other existing .hip files; [] when the blend folder or its JSON does not exist or the
    JSON is not intact (an unreadable one is set aside as cycles_import.json.bad, as an
    import would). The UI shows Replace / Cancel and passes ``on_shared``.
    """
    from . import blend_library_materials

    if isinstance(source, dict):
        snapshot = source
        source, selection = snapshot["source"], snapshot["selection"]
        asset_root = snapshot.get("asset_root") or asset_root
        structure = structure or snapshot.get("structure")
        library_root = library_root or snapshot.get("library_root") or None
    text = (asset_root or DEFAULT_ASSET_ROOT).strip().replace("\\", "/")
    text, _ = blend_library_materials.asset_root_text(
        DEFAULT_ASSET_ROOT if text.rstrip("/") == OBSOLETE_ASSET_ROOT else text
    )
    try:
        root = blend_library_materials.resolve_asset_root(text, source, library_root)
    except blend_library_materials.AssetRootError as error:
        raise BlendImportError(str(error)) from None
    blend_dir = layout.blend_folder(root, str(Path(source).resolve()))
    if not layout.json_path(blend_dir).is_file():
        return []
    hip = _scene_path() or ""
    result = []
    try:
        with layout.open_store(blend_dir) as store:
            if store.state != "ok":
                return []
            for kind, name in _selection(selection):
                original = asset_original(kind, name)
                if structure is not None:
                    previous = _structure_files(store, original, structure)[0]
                else:
                    previous = store.asset_files(original)
                scenes = store.other_scenes(original, hip) if previous else []
                if scenes:
                    asset = store.data["names"]["assets"].get(original, name)
                    result.append(dict(kind=kind, name=name, asset=asset, scenes=scenes))
    except layout.ImportLockError as error:
        raise BlendImportError(str(error)) from None
    return result
