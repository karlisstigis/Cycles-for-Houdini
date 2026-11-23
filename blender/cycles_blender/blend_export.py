# SPDX-License-Identifier: GPL-3.0-or-later
# Blender component of Cycles for Houdini (see blender/README.md). This file is one of the scripts
# that run inside Blender and use its Python API, so it is published under the GNU General Public
# License, version 3 or later (blender/LICENSE). The Houdini integration (Apache-2.0) never imports
# it; the two sides exchange only files, command-line arguments and pipe messages.
"""Standalone Blender background exporter for owned Houdini prototype bundles.

Run with the explicit Blender script (package imports are host-independent):
  blender --background asset.blend --python export_main.py -- --request request.json --output bundle

Geometry stays in Blender local coordinates. Instance matrices map those local
coordinates into Houdini world space, including evaluated collection offsets.
The uncompressed NumPy archive contains numeric arrays only; consumers must use
np.load(..., allow_pickle=False). No executable data is stored in the bundle.
"""

import argparse
from importlib import import_module
import itertools
import json
import math
from pathlib import Path
import sys
import traceback

import numpy as np

from cycles_houdini.blender.blend_ids import hash_string, hash_uint2, object_random_id


SCHEMA_VERSION = 1
# Object types whose evaluated mesh Blender also yields as a MESH dupli of the object itself.
_MESH_COMPONENT_TYPES = frozenset(("CURVE", "SURFACE", "FONT"))
_CONVERT = ((1.0, 0.0, 0.0, 0.0), (0.0, 0.0, 1.0, 0.0), (0.0, -1.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0))


def _helper(name):
    return import_module("." + name, __package__)


def _material_exporter():
    return _helper("blend_material_export")


def _array(collection, property_name, count, width, dtype):
    values = np.empty(count * width, dtype=dtype)
    if values.size:
        collection.foreach_get(property_name, values)
    if np.issubdtype(values.dtype, np.floating) and not np.isfinite(values).all():
        raise ValueError("Non-finite evaluated mesh data: " + property_name)
    return values.reshape(count, width) if width != 1 else values


def _original(obj):
    return getattr(obj, "original", obj)


def _identity(datablock):
    library = getattr(datablock, "library", None)
    return (str(library.filepath) if library else "", datablock.name_full)


def _pointer(datablock):
    return datablock.as_pointer()


def _view_layers(bpy, scene):
    """Candidate view layers, the file's active scene and view layer first."""
    context = bpy.context
    active_scene = getattr(context, "scene", None)
    active_layer = getattr(context, "view_layer", None)
    scenes = [scene] + [candidate for candidate in (active_scene, *bpy.data.scenes) if candidate is not None]
    seen = set()
    for candidate in scenes:
        if _pointer(candidate) in seen:
            continue
        seen.add(_pointer(candidate))
        layers = list(candidate.view_layers)
        if active_layer is not None and any(_pointer(layer) == _pointer(active_layer) for layer in layers):
            layers.sort(key=lambda layer: _pointer(layer) != _pointer(active_layer))
        for layer in layers:
            yield candidate, layer


def _layer_collection(layer, collection):
    """First included depth-first LayerCollection of collection, or None."""
    pending = [layer]
    while pending:
        item = pending.pop()
        if _pointer(item.collection) == _pointer(collection):
            if not item.exclude:
                return item
            continue
        pending.extend(reversed(list(item.children)))
    return None


def _exclusion_layer(bpy, collection, scene):
    """Return (scene, view_layer, LayerCollection) that decides child exclusion.

    A collection instance ignores view-layer flags, so the source file's own
    layer tree is read instead. Blender propagates exclusion to all children of
    an excluded LayerCollection, so only an included occurrence carries the
    author's per-child choices; an excluded-only or unlinked collection keeps
    all of its children.
    """
    for owner, view_layer in _view_layers(bpy, scene):
        found = _layer_collection(view_layer.layer_collection, collection)
        if found is not None:
            return owner, view_layer, found
    return None, None, None


def _filtered_collection(bpy, collection, scene):
    """Return a temporary copy of collection without view-layer-excluded children.

    The copy links the original objects and unaffected child collections, so
    source data is not modified; the caller removes the new collections. It
    keeps instance_offset and each copied collection's render/viewport hiding,
    which Blender still honours for instanced collections. An object that is
    reachable through an included child remains included.
    """
    owner, view_layer, root = _exclusion_layer(bpy, collection, scene)
    if root is None:
        return collection, []
    excluded = {}

    def has_exclusion(layer):
        key = _pointer(layer)
        if key not in excluded:
            excluded[key] = any(child.exclude or has_exclusion(child) for child in layer.children)
        return excluded[key]

    if not has_exclusion(root):
        return collection, []
    skipped = []

    def mirror(layer, source):
        if not has_exclusion(layer):
            return source
        copy = bpy.data.collections.new("__CH_EXPORT_FILTER__" + source.name)
        for obj in source.objects:
            copy.objects.link(obj)
        children = {_pointer(child.collection): child for child in layer.children}
        for child in source.children:
            child_layer = children.get(_pointer(child))
            if child_layer is not None and child_layer.exclude:
                skipped.append(
                    dict(
                        name=child.name_full,
                        parent=source.name_full,
                        scene=owner.name_full,
                        view_layer=view_layer.name,
                    )
                )
                continue
            copy.children.link(child if child_layer is None else mirror(child_layer, child))
        for flag in ("hide_render", "hide_viewport"):
            if hasattr(source, flag):
                setattr(copy, flag, getattr(source, flag))
        return copy

    result = mirror(root, collection)
    result.instance_offset = tuple(collection.instance_offset)
    return result, skipped


def _select(bpy, request, scene, skipped=None):
    name = request["selector"]
    kind = request["selector_type"].upper()
    if kind == "OBJECT":
        selected = bpy.data.objects.get(name)
        if selected is None:
            raise ValueError("Blender object does not exist: " + name)
        if scene.objects.get(selected.name) is None:
            scene.collection.objects.link(selected)
        return {_identity(selected)}
    if kind == "COLLECTION":
        selected = bpy.data.collections.get(name)
        if selected is None:
            raise ValueError("Blender collection does not exist: " + name)
        # Evaluate the chosen collection as an identity instance. Directly
        # linking its members would lose the collection's instance_offset.
        # Children the source file excludes from its view layer (for example
        # rig widget shapes) stay out unless the request asks to include them.
        if not request.get("include_excluded_collections", False):
            selected, excluded = _filtered_collection(bpy, selected, scene)
            if skipped is not None:
                skipped.extend(excluded)
        instance = bpy.data.objects.new("__CH_EXPORT_SELECTOR__", None)
        instance.instance_type = "COLLECTION"
        instance.instance_collection = selected
        scene.collection.objects.link(instance)
        return {_identity(instance)}
    raise ValueError("selector_type must be OBJECT or COLLECTION")


# UV sets (docs/dev/blend-import.md, "UV maps and UV attributes"). Generic attributes of
# these domains and types count as UV sets only when "uv" is in their name (a material
# reading an attribute is not enough: "wind" or "rest" must stay plain attributes). With no
# real UV map, a candidate a material reads, else the first of these names, is the default
# ("map1" has no "uv", so it can only be a real UV map; kept for the agreed order).
_UV_SET_DOMAINS = ("CORNER", "POINT")
_UV_SET_TYPES = ("FLOAT2", "FLOAT_VECTOR")
_UV_DEFAULT_NAMES = ("UVMap", "uv", "UV", "map1")
# Shader nodes that name a mesh field: (bl_idname, property). Attribute nodes count only
# with attribute_type GEOMETRY.
_UV_REFERENCE_NODES = (
    ("ShaderNodeUVMap", "uv_map"),
    ("ShaderNodeAttribute", "attribute_name"),
    ("ShaderNodeNormalMap", "uv_map"),
    ("ShaderNodeTangent", "uv_map"),
    ("ShaderNodeVertexColor", "layer_name"),
)


def uv_sets(layer_names, default_layer, attributes, referenced, promote=True):
    """Which UV sets a mesh exports and the names they keep (pure; no Blender types).

    layer_names: the mesh's real UV map names; default_layer: the render-active one (or None).
    attributes: (name, domain, data_type) of its other generic attributes.
    referenced: names the object's materials read explicitly (material_field_names); they
    choose the default among the candidates and decide which defaults keep their names.
    Returns dict(default, source, attributes, keep): the set that becomes Houdini/Cycles uv,
    "uv_layer" or "attribute" (None without UVs), the generic attribute names detected as UV
    sets (input order), and the names that are also kept under their own name. The default
    keeps its own name only when a material names it; every other UV set always does.
    The native Link path receives this decision in the record (src/houdini/blend_mesh.cpp).
    promote=False (Link renders, exactly like Blender): only real UV maps are UV sets;
    "uv"-named attributes stay plain named attributes.
    """
    referenced = set(referenced)
    detected = uv_named_attributes(attributes) if promote else []
    default, source = None, None
    if layer_names:
        default = default_layer if default_layer in layer_names else layer_names[0]
        source = "uv_layer"
    elif detected:
        read = sorted(name for name in detected if name in referenced)
        preferred = [name for name in _UV_DEFAULT_NAMES if name in detected]
        default = (read or preferred or sorted(detected))[0]
        source = "attribute"
    keep = {name for name in (*layer_names, *detected) if name != default or name in referenced}
    return dict(default=default, source=source, attributes=detected, keep=keep)


def uv_named_attributes(attributes):
    """Names of (name, domain, data_type) generic attributes that look like UV sets."""
    return [
        name
        for name, domain, data_type in attributes
        if not name.startswith(".")
        and domain in _UV_SET_DOMAINS
        and data_type in _UV_SET_TYPES
        and "uv" in name.lower()
    ]


def material_object_attributes(materials):
    """(type, name) of the Attribute nodes of type OBJECT or INSTANCER the materials read, inside
    node groups too (sorted). Blender's Cycles syncs exactly these per object
    (sync_object_attributes); VIEW_LAYER attributes are not per object."""
    names, seen = set(), set()
    pending = [getattr(material, "node_tree", None) for material in materials if material is not None]
    while pending:
        tree = pending.pop()
        if tree is None or tree.as_pointer() in seen:
            continue
        seen.add(tree.as_pointer())
        for node in tree.nodes:
            if node.bl_idname == "ShaderNodeGroup":
                pending.append(getattr(node, "node_tree", None))
            elif node.bl_idname == "ShaderNodeAttribute" and getattr(node, "attribute_type", "") in _OBJECT_ATTRIBUTES:
                if node.attribute_name:
                    names.add((node.attribute_type, node.attribute_name))
    return sorted(names)


_MESH_INTRINSIC_OUTPUTS = frozenset(("Pointiness", "Random Per Island"))


def reads_mesh_intrinsics(materials):
    """True when a material (or a node group inside it) links the Geometry node's Pointiness
    or Random Per Island output. Blender computes both on the mesh it renders, so native
    subdivision of the control cage cannot reproduce them (blend_subdivision)."""
    seen = set()

    def walk(tree):
        if tree is None or tree.as_pointer() in seen:
            return False
        seen.add(tree.as_pointer())
        for node in tree.nodes:
            if node.bl_idname == "ShaderNodeNewGeometry":
                if any(socket.is_linked and socket.name in _MESH_INTRINSIC_OUTPUTS for socket in node.outputs):
                    return True
            elif node.bl_idname == "ShaderNodeGroup" and walk(node.node_tree):
                return True
        return False

    return any(material is not None and walk(getattr(material, "node_tree", None)) for material in materials)


def material_field_names(materials):
    """Mesh field names the materials' node trees read by name, inside node groups too."""
    names, seen = set(), set()
    pending = [getattr(material, "node_tree", None) for material in materials if material is not None]
    while pending:
        tree = pending.pop()
        if tree is None or tree.as_pointer() in seen:
            continue
        seen.add(tree.as_pointer())
        for node in tree.nodes:
            if node.bl_idname == "ShaderNodeGroup":
                pending.append(getattr(node, "node_tree", None))
                continue
            for idname, prop in _UV_REFERENCE_NODES:
                if node.bl_idname != idname:
                    continue
                if idname == "ShaderNodeAttribute" and getattr(node, "attribute_type", "GEOMETRY") != "GEOMETRY":
                    continue
                value = getattr(node, prop, "")
                if value:
                    names.add(value)
    return names


def uv_summary(record):
    """Report line for a mesh record's UV sets, e.g. "UVs: uv (from UVMap), UVMap"."""
    default = record.get("default_uv")
    if default is None:
        return "UVs: none"
    names = ["uv (from " + default + ")"]
    for field in record.get("uv_layers", ()):
        if field.get("keep_name", True):
            names.append(field["name"])
    for field in record.get("attributes", ()):
        if field.get("uv_set") and field.get("keep_name", True):
            names.append(field["name"])
    return "UVs: " + ", ".join(names)


def _mesh_arrays(mesh, key, arrays, extended=False, referenced=(), promote_uv=True, texture_space=None):
    """Evaluated mesh arrays. extended (converter requests only) adds the attribute
    types a render never reads and lists every attribute that was not exported.
    referenced: field names the object's materials read (material_field_names), for uv_sets.
    promote_uv: uv_sets(promote=...). texture_space: the mesh whose Blender texture space
    gives Generated coordinates (default: mesh)."""
    record = dict(
        key=key,
        name=mesh.name_full,
        vertex_count=len(mesh.vertices),
        polygon_count=len(mesh.polygons),
        corner_count=len(mesh.loops),
        normals_domain=getattr(mesh, "normals_domain", "CORNER"),
        arrays={},
        uv_layers=[],
        default_uv=None,
        color_attributes=[],
        default_color=None,
        attributes=[],
    )

    def add(name, value):
        archive_key = key + "/" + name
        arrays[archive_key] = np.ascontiguousarray(value)
        return archive_key

    record["arrays"] = dict(
        positions=add("positions", _array(mesh.vertices, "co", len(mesh.vertices), 3, np.float32)),
        edges=add("edges", _array(mesh.edges, "vertices", len(mesh.edges), 2, np.int32)),
        polygon_counts=add("polygon_counts", _array(mesh.polygons, "loop_total", len(mesh.polygons), 1, np.int32)),
        vertex_indices=add("vertex_indices", _array(mesh.loops, "vertex_index", len(mesh.loops), 1, np.int32)),
        material_indices=add(
            "material_indices", _array(mesh.polygons, "material_index", len(mesh.polygons), 1, np.int32)
        ),
        smooth=add("smooth", _array(mesh.polygons, "use_smooth", len(mesh.polygons), 1, np.uint8)),
    )
    if hasattr(mesh, "corner_normals"):
        normals = _array(mesh.corner_normals, "vector", len(mesh.loops), 3, np.float32)
    else:
        mesh.calc_normals_split()
        normals = _array(mesh.loops, "normal", len(mesh.loops), 3, np.float32)
    record["arrays"]["normals"] = add("normals", normals)
    if not extended:
        # Link only: Blender's vertex normals, with "edges" above, give Pointiness and Random
        # per Island by Blender's own rule (intern/cycles/blender/mesh.cpp attr_create_pointiness,
        # attr_create_random_per_island; src/core/mesh_pointiness.h, mesh_islands.h).
        if hasattr(mesh, "vertex_normals"):
            vertex_normals = _array(mesh.vertex_normals, "vector", len(mesh.vertices), 3, np.float32)
        else:
            vertex_normals = _array(mesh.vertices, "normal", len(mesh.vertices), 3, np.float32)
        record["arrays"]["vertex_normals"] = add("vertex_normals", vertex_normals)
    # Preserve Blender's tessellation for non-planar quads/ngons. Consumers
    # can reconstruct triangle corner data through triangle_loops exactly.
    mesh.calc_loop_triangles()
    record["triangle_count"] = len(mesh.loop_triangles)
    record["arrays"].update(
        triangle_vertices=add(
            "triangle_vertices", _array(mesh.loop_triangles, "vertices", len(mesh.loop_triangles), 3, np.int32)
        ),
        triangle_loops=add(
            "triangle_loops", _array(mesh.loop_triangles, "loops", len(mesh.loop_triangles), 3, np.int32)
        ),
        triangle_polygons=add(
            "triangle_polygons", _array(mesh.loop_triangles, "polygon_index", len(mesh.loop_triangles), 1, np.int32)
        ),
    )
    for index, layer in enumerate(mesh.uv_layers):
        values = (
            _array(layer.uv, "vector", len(mesh.loops), 2, np.float32)
            if hasattr(layer, "uv")
            else _array(layer.data, "uv", len(mesh.loops), 2, np.float32)
        )
        record["uv_layers"].append(
            dict(
                name=layer.name,
                array=add("uv_" + str(index), values),
                active_render=bool(layer.active_render),
                active=index == mesh.uv_layers.active_index,
            )
        )
    if record["uv_layers"]:
        record["default_uv"] = next(
            (layer["name"] for layer in record["uv_layers"] if layer["active_render"]),
            next((layer["name"] for layer in record["uv_layers"] if layer["active"]), record["uv_layers"][0]["name"]),
        )
    colors = set()
    for index, attribute in enumerate(mesh.color_attributes):
        if attribute.domain not in ("POINT", "CORNER"):
            continue
        colors.add(attribute.name)
        values = _array(attribute.data, "color", len(attribute.data), 4, np.float32)
        record["color_attributes"].append(
            dict(
                name=attribute.name,
                domain=attribute.domain,
                data_type=attribute.data_type,
                active_render=index == mesh.color_attributes.render_color_index,
                array=add("color_" + str(index), values),
            )
        )
    record["default_color"] = next(
        (field["name"] for field in record["color_attributes"] if field["active_render"]), None
    )
    # Material Attribute nodes can consume named mesh fields as well as UV/Cd.
    properties = {
        "FLOAT": ("value", 1, np.float32),
        "FLOAT_VECTOR": ("vector", 3, np.float32),
        "FLOAT2": ("vector", 2, np.float32),
        "INT": ("value", 1, np.int32),
        "INT8": ("value", 1, np.int32),
        "BOOLEAN": ("value", 1, np.uint8),
    }
    if extended:
        properties.update(_CONVERTER_PROPERTIES, **_MESH_CONVERTER_PROPERTIES)
        record["skipped_attributes"] = []
    for index, attribute in enumerate(mesh.attributes):
        if attribute.name in _TRANSLATED_MESH_FIELDS:
            continue  # written from the exporter's own data (N from corner_normals)
        if (
            extended
            and attribute.data_type == "STRING"
            and attribute.domain in ("POINT", "CORNER", "FACE")
            and not attribute.name.startswith(".")
        ):
            # Strings cannot travel as numeric arrays: an index per element into a table of
            # distinct values.
            record["attributes"].append(_string_attribute(attribute, key + "/attribute_" + str(index), arrays))
            continue
        if (
            attribute.name.startswith(".")
            or attribute.name in colors
            or attribute.name in mesh.uv_layers
            or attribute.name == "position"
            or attribute.data_type not in properties
            or attribute.domain not in ("POINT", "CORNER", "FACE", "EDGE")
        ):
            if (
                extended
                and not attribute.name.startswith(".")
                and attribute.name not in colors
                and attribute.name not in mesh.uv_layers
                and attribute.name != "position"
            ):
                record["skipped_attributes"].append(
                    dict(
                        name=attribute.name,
                        domain=attribute.domain,
                        data_type=attribute.data_type,
                        reason=skip_reason(attribute.domain, attribute.data_type, properties),
                    )
                )
            continue
        prop, width, dtype = properties[attribute.data_type]
        record["attributes"].append(
            dict(
                name=attribute.name,
                domain=attribute.domain,
                data_type=attribute.data_type,
                array=add("attribute_" + str(index), _array(attribute.data, prop, len(attribute.data), width, dtype)),
            )
        )
    plan = uv_sets(
        [layer["name"] for layer in record["uv_layers"]],
        record["default_uv"],
        [(field["name"], field["domain"], field["data_type"]) for field in record["attributes"]],
        referenced,
        promote=promote_uv,
    )
    for layer in record["uv_layers"]:
        layer["keep_name"] = layer["name"] in plan["keep"]
    # The render colour becomes Cd; it keeps its own name too only when a material names it.
    for field in record["color_attributes"]:
        field["keep_name"] = not field["active_render"] or field["name"] in set(referenced)
    for field in record["attributes"]:
        if field["name"] in plan["attributes"]:
            field.update(uv_set=True, keep_name=field["name"] in plan["keep"])
    record["default_uv"], record["default_uv_source"] = plan["default"], plan["source"]
    # Blender's texture space (BKE_mesh_texspace_get: the box before modifiers, or a custom
    # one; a zero-size side is already 1) drives Generated coordinates in Link renders
    # (src/core/generated_coordinates.h). Reading it through RNA evaluates the automatic box.
    space = mesh if texture_space is None else texture_space
    location = [float(value) for value in space.texspace_location]
    size = [float(value) for value in space.texspace_size]
    if all(math.isfinite(value) for value in location + size):
        record["texture_space"] = dict(location=location, size=size)
    return record


# ------------------------------------------------------------------ converter-only data
#
# The Houdini converter (blend_convert) asks for these with request flags; render
# requests never set them, so their manifests and arrays are unchanged.

# Extra attribute types a converter request exports (property, width, dtype).
_CONVERTER_PROPERTIES = {
    "INT32_2D": ("value", 2, np.int32),
    "FLOAT_COLOR": ("color", 4, np.float32),
    "BYTE_COLOR": ("color", 4, np.float32),
}
# Mesh-only converter types (curves and point clouds keep _GENERIC_PROPERTIES); STRING is
# read separately. Byte colours read through "color" arrive as linear floats, like Cycles reads them.
_MESH_CONVERTER_PROPERTIES = {
    "QUATERNION": ("value", 4, np.float32),  # Blender order w, x, y, z
    "FLOAT4X4": ("value", 16, np.float32),  # column-major (column-vector matrices)
    "FLOAT4": ("vector", 4, np.float32),
    "INT16_2D": ("value", 2, np.int32),
}
# Mesh fields the exporter writes from its own data instead (custom normals -> corner normals).
_TRANSLATED_MESH_FIELDS = frozenset(("custom_normal",))
_GENERIC_PROPERTIES = {
    "FLOAT": ("value", 1, np.float32),
    "FLOAT_VECTOR": ("vector", 3, np.float32),
    "FLOAT2": ("vector", 2, np.float32),
    "INT": ("value", 1, np.int32),
    "INT8": ("value", 1, np.int32),
    "BOOLEAN": ("value", 1, np.uint8),
    **_CONVERTER_PROPERTIES,
}


# Converter requests (Convert & Append) carry every type on Curves and point clouds too: the
# mesh converter types, and strings (read separately, as for meshes).
_GENERIC_CONVERTER_PROPERTIES = {**_GENERIC_PROPERTIES, **_MESH_CONVERTER_PROPERTIES}


def skip_reason(domain, data_type, properties, domains=None):
    """Why an attribute is not exported, in words the import report shows the artist."""
    if domain == "EDGE":
        return "Houdini has no edge attributes"
    if domains is not None and domain not in domains:
        return "no Houdini equivalent for the " + domain.lower() + " domain here"
    if data_type not in properties and data_type != "STRING":
        return "attribute type " + data_type + " is not supported"
    return "not supported"


def _string_attribute(attribute, archive_key, arrays):
    """A STRING attribute as an index per element into its distinct values (per-element read:
    StringAttributeValue has no foreach_get). Returns the record's field (without array)."""
    table, indices = {}, np.empty(len(attribute.data), dtype=np.int32)
    for element, item in enumerate(attribute.data):
        indices[element] = table.setdefault(bytes(item.value).decode("utf-8", "replace"), len(table))
    arrays[archive_key] = indices
    return dict(
        name=attribute.name, domain=attribute.domain, data_type="STRING", strings=list(table), array=archive_key
    )


def _generic_attributes(attributes, key, arrays, domains, exclude=(), extended=False):
    """(exported, skipped) generic attributes of a Curves or PointCloud datablock. extended
    (converter requests only) adds the types a render never reads, strings included."""
    properties = _GENERIC_CONVERTER_PROPERTIES if extended else _GENERIC_PROPERTIES
    exported, skipped = [], []
    for index, attribute in enumerate(attributes):
        name = attribute.name
        if name.startswith(".") or name in exclude:
            continue
        archive_key = key + "/attribute_" + str(index)
        if extended and attribute.data_type == "STRING" and attribute.domain in domains:
            exported.append(_string_attribute(attribute, archive_key, arrays))
            continue
        if attribute.data_type not in properties or attribute.domain not in domains:
            skipped.append(
                dict(
                    name=name,
                    domain=attribute.domain,
                    data_type=attribute.data_type,
                    reason=skip_reason(attribute.domain, attribute.data_type, properties, domains),
                )
            )
            continue
        prop, width, dtype = properties[attribute.data_type]
        arrays[archive_key] = np.ascontiguousarray(_array(attribute.data, prop, len(attribute.data), width, dtype))
        exported.append(dict(name=name, domain=attribute.domain, data_type=attribute.data_type, array=archive_key))
    return exported, skipped


def _vertex_group_arrays(obj, mesh, record, arrays):
    """Vertex group weights as POINT float arrays (0 where a vertex is not in the group)."""
    groups = list(getattr(obj, "vertex_groups", ()) or ())
    record["vertex_groups"] = []
    if not groups:
        return
    taken = {attribute["name"] for attribute in record["attributes"]}
    weights = np.zeros((len(groups), len(mesh.vertices)), dtype=np.float32)
    used = np.zeros(len(groups), dtype=bool)
    for vertex in mesh.vertices:
        for element in vertex.groups:
            if 0 <= element.group < len(groups):
                weights[element.group, vertex.index] = element.weight
                used[element.group] = True
    for index, group in enumerate(groups):
        if group.name in taken:
            continue  # an attribute of the same name already carries it
        archive_key = record["key"] + "/vertex_group_" + str(index)
        arrays[archive_key] = np.ascontiguousarray(weights[index])
        record["vertex_groups"].append(dict(name=group.name, array=archive_key, empty=not bool(used[index])))


_CURVE_INTERNAL = frozenset(
    (
        "position",
        "radius",
        "handle_left",
        "handle_right",
        "handle_type_left",
        "handle_type_right",
        "nurbs_weight",
        "nurbs_order",
        "knots_mode",
        "resolution",
        "curve_type",
        "normal_mode",
    )
)


# Blender's radius when a Curves or point cloud datablock has no "radius" attribute (Blender
# v5.3.0 intern/cycles/blender/curves.cpp export_hair_curves: std::fill(..., 0.005f);
# pointcloud.cpp export_pointcloud: std::fill(..., 0.01f)). The Link records use the same values.
# Attribute types that Blender's Cycles accepts as a float radius, by its implicit attribute
# conversions (Blender manual, Attributes reference, Data Conversion: "When a vector becomes a
# float the average of the components is taken"; colours become "their grayscale equivalent"):
# (RNA property, components, dtype). Vectors average their components; colours take the
# luminance with the OCIO config's default luma coefficients (byte colours are decoded first, as
# RNA "color" reads them). QUATERNION, FLOAT4X4 and STRING have no float conversion: the radius is
# not found and the default applies, as in Blender.
_RADIUS_TYPES = {
    "FLOAT": ("value", 1, np.float32),
    "INT": ("value", 1, np.float32),
    "INT8": ("value", 1, np.float32),
    "BOOLEAN": ("value", 1, np.uint8),
    "FLOAT2": ("vector", 2, np.float32),
    "FLOAT_VECTOR": ("vector", 3, np.float32),
    "FLOAT4": ("vector", 4, np.float32),
    "INT16_2D": ("value", 2, np.int32),
    "INT32_2D": ("value", 2, np.int32),
    "FLOAT_COLOR": ("color", 4, np.float32),
    "BYTE_COLOR": ("color", 4, np.float32),
}


def _luma_coefficients():
    """Luminance weights as Blender's colour management uses them: the OCIO config's default luma
    coefficients; Rec.709's when PyOpenColorIO is absent."""
    try:
        import PyOpenColorIO as ocio

        return np.asarray(ocio.GetCurrentConfig().getDefaultLumaCoefs(), np.float32)
    except Exception:  # noqa: BLE001 - no OCIO module or configuration: Blender's built-in default
        return np.asarray((0.2126, 0.7152, 0.0722), np.float32)


def _radius_values(radius, count):
    """count float32 radii from a radius attribute of a _RADIUS_TYPES type, converted as described
    above (float32: components summed left to right, then divided by their number)."""
    name, width, dtype = _RADIUS_TYPES[radius.data_type]
    values = _array(radius.data, name, count, width, dtype)
    if width == 1:
        return values.astype(np.float32)
    values = values.astype(np.float32)
    if radius.data_type in ("FLOAT_COLOR", "BYTE_COLOR"):
        luma = _luma_coefficients()
        return (values[:, 0] * luma[0] + values[:, 1] * luma[1] + values[:, 2] * luma[2]).astype(np.float32)
    total = values[:, 0]
    for column in range(1, width):
        total = total + values[:, column]
    return (total / np.float32(width)).astype(np.float32)


def _converter_radius(attributes, points, offsets, default):
    """(per-point radius, source) as Blender's Cycles reads it: a "radius" attribute on the
    POINT domain, or on the CURVE domain repeated for each of its points, converted to float
    (_RADIUS_TYPES); without one, the default. source: "attribute" or "default". A radius of
    another kind (QUATERNION, FLOAT4X4, STRING) stays a plain attribute. Used by Link records
    (_hair_curves_record, _point_cloud_record) and Convert & Append alike."""
    radius = attributes.get("radius")
    if radius is not None and radius.data_type in _RADIUS_TYPES:
        if radius.domain == "POINT":
            return _radius_values(radius, points), "attribute"
        if radius.domain == "CURVE" and offsets is not None:
            per_curve = _radius_values(radius, len(offsets) - 1)
            return np.repeat(per_curve, np.diff(offsets)).astype(np.float32), "attribute"
    return np.full(points, default, dtype=np.float32), "default"


def _curves_arrays(curves, key, arrays):
    """Hair/GN curves: control points, per-curve offsets, radius and POINT/CURVE attributes.
    Converter only (Convert & Append): every attribute is exported, Blender's own curve data
    (curve_type, cyclic, handles, NURBS weights, resolution, ...) included; "position" and
    "radius" become P and width. Without a radius, Blender's 0.005 (_converter_radius)."""
    points, count = len(curves.points), len(curves.curves)
    attributes = curves.attributes
    record = dict(key=key, name=curves.name_full, point_count=points, curve_count=count, arrays={})

    def add(name, value):
        archive_key = key + "/" + name
        arrays[archive_key] = np.ascontiguousarray(value)
        return archive_key

    record["arrays"]["positions"] = add(
        "positions", _array(attributes["position"].data, "vector", points, 3, np.float32)
    )
    offsets = np.empty(count + 1, dtype=np.int32)
    if count:
        curves.curve_offset_data.foreach_get("value", offsets)
    else:
        offsets[:] = 0
    record["arrays"]["offsets"] = add("offsets", offsets)
    radii, record["radius_source"] = _converter_radius(attributes, points, offsets, _HAIR_DEFAULT_RADIUS)
    record["arrays"]["radius"] = add("radius", radii)
    types = attributes.get("curve_type")
    if types is not None and types.domain == "CURVE":
        values = _array(types.data, "value", count, 1, np.int32)
        # 0 Catmull-Rom, 1 poly, 2 Bezier, 3 NURBS (Blender CurveType).
        record["curve_types"] = sorted({int(value) for value in np.unique(values)})
    else:
        record["curve_types"] = [0]
    cyclic = attributes.get("cyclic")
    record["cyclic_count"] = (
        int(np.count_nonzero(_array(cyclic.data, "value", count, 1, np.uint8))) if cyclic is not None else 0
    )
    exclude = {"position"} | ({"radius"} if record["radius_source"] == "attribute" else set())
    record["attributes"], record["skipped_attributes"] = _generic_attributes(
        attributes, key, arrays, ("POINT", "CURVE"), exclude, extended=True
    )
    return record


def _pointcloud_arrays(cloud, key, arrays):
    """Point clouds for the converter: positions, radius (Blender's 0.01 without one) and every
    POINT attribute."""
    points = len(cloud.points)
    attributes = cloud.attributes
    record = dict(key=key, name=cloud.name_full, point_count=points, arrays={})
    positions = _array(attributes["position"].data, "vector", points, 3, np.float32)
    record["arrays"]["positions"] = key + "/positions"
    arrays[key + "/positions"] = np.ascontiguousarray(positions)
    radii, record["radius_source"] = _converter_radius(attributes, points, None, _POINT_DEFAULT_RADIUS)
    record["arrays"]["radius"] = key + "/radius"
    arrays[key + "/radius"] = np.ascontiguousarray(radii)
    exclude = {"position"} | ({"radius"} if record["radius_source"] == "attribute" else set())
    record["attributes"], record["skipped_attributes"] = _generic_attributes(
        attributes, key, arrays, ("POINT",), exclude, extended=True
    )
    return record


# ------------------------------------------------------------------ Link hair
#
# Link renders carry hair the way Blender's Cycles translator syncs it. Ported from Blender
# v5.3.0 (Apache-2.0, SPDX-FileCopyrightText: 2011-2022 Blender Foundation):
# intern/cycles/blender/curves.cpp (export_hair_curves, attr_create_generic and sync_hair for
# Curves objects; ObtainCacheParticleData, ObtainCacheParticleUV, ObtainCacheParticleVcol,
# ExportCurveSegments, shaperadius and sync_particle_hair for legacy particle hair),
# intern/cycles/blender/util.h (mesh_texture_space), intern/cycles/util/hash.h
# (hash_uint2_to_float) and intern/cycles/util/color.h (color_srgb_to_linear).
# The records keep Blender's own domains; blender_assets._native_curves expands them per key.

_HAIR_DEFAULT_RADIUS = 0.005  # export_hair_curves: a Curves object without "radius"
# The default of the ROP's Curve Subdivisions (SceneParams::hair_subdivisions), equal to Blender's
# default scene.cycles_curves.subdivisions (Cycles' own default is 3). The export cannot see the ROP,
# so it notes a .blend that differs from this default.
_HOUDINI_HAIR_SUBDIVISIONS = 2
_CURVE_TYPE_POLY = 1


def hash_uint2_array(kx, ky=0):
    """hash_uint2 over a numpy array of kx (uint32 arithmetic, as Cycles' util/hash.h)."""
    with np.errstate(over="ignore"):
        kx = np.asarray(kx).astype(np.uint32)
        a = np.full(kx.shape, (0xDEADBEEF + (2 << 2) + 13) & _UINT, dtype=np.uint32)
        b = a.copy()
        c = a.copy()
        a = a + kx
        b = b + np.uint32(ky & _UINT)

        def rot(value, shift):
            return (value << np.uint32(shift)) | (value >> np.uint32(32 - shift))

        c ^= b
        c -= rot(b, 14)
        a ^= c
        a -= rot(c, 11)
        b ^= a
        b -= rot(a, 25)
        c ^= b
        c -= rot(b, 16)
        a ^= c
        a -= rot(c, 4)
        b ^= a
        b -= rot(a, 14)
        c ^= b
        c -= rot(b, 24)
    return c


def curve_randoms(count):
    """Hair Info > Random per curve: hash_uint2_to_float(curve index, 0), in float32 like Cycles
    ((float)hash / (float)0xFFFFFFFFu)."""
    return hash_uint2_array(np.arange(count, dtype=np.uint32)).astype(np.float32) / np.float32(0xFFFFFFFF)


def srgb_to_linear(values):
    """Cycles color_srgb_to_linear per component (float32)."""
    values = np.asarray(values, dtype=np.float32)
    low = np.where(values < 0.0, np.float32(0.0), values * np.float32(1.0 / 12.92))
    high = np.power((values + np.float32(0.055)) * np.float32(1.0 / 1.055), np.float32(2.4))
    return np.where(values < 0.04045, low, high).astype(np.float32)


def hair_shape(scene):
    """scene.cycles_curves.shape as Blender's sync maps it to Cycles' hair shape."""
    settings = getattr(scene, "cycles_curves", None)
    return "RIBBON" if getattr(settings, "shape", "THICK") == "RIBBONS" else "THICK"


def hair_settings(scene):
    settings = getattr(scene, "cycles_curves", None)
    return dict(shape=hair_shape(scene), subdivisions=int(getattr(settings, "subdivisions", 2)))


def _hair_curves_record(curves, key, arrays, shape):
    """A Curves datablock (object type CURVES, also geometry-nodes hair and curve instances) as
    export_hair_curves and sync_hair read it: the control points as Cycles keys, "radius" (per
    point or per curve, float, integer or boolean, as lookup<float> adapts it) or 0.005, the
    first curve's type choosing linear segments, a random value per curve, the first
    FLOAT2 Curve attribute as the standard UV, every other attribute by name. Cyclic flags and
    per-curve material indices are ignored, as Blender's translator ignores them (all curves
    use the object's first material). Generated has no attribute (Cycles falls back to the
    object-space position)."""
    points, count = len(curves.points), len(curves.curves)
    attributes = curves.attributes
    record = dict(key=key, name=curves.name_full, kind="curves", point_count=points, curve_count=count, arrays={})

    def add(name, value):
        archive_key = key + "/" + name
        arrays[archive_key] = np.ascontiguousarray(value)
        record["arrays"][name] = archive_key

    if "position" not in attributes:
        return None
    add("positions", _array(attributes["position"].data, "vector", points, 3, np.float32))
    offsets = np.zeros(count + 1, dtype=np.int32)
    if count:
        curves.curve_offset_data.foreach_get("value", offsets)
    add("offsets", offsets)
    # lookup<float>("radius", Point): a CURVE-domain or integer radius too (_converter_radius).
    radii, _ = _converter_radius(attributes, points, offsets, _HAIR_DEFAULT_RADIUS)
    add("radius", radii)
    add("randoms", curve_randoms(count))
    types = attributes.get("curve_type")
    first_type = 0  # CURVE_TYPE_CATMULL_ROM when the attribute does not exist
    if types is not None and types.domain == "CURVE" and count:
        first_type = int(_array(types.data, "value", count, 1, np.int32)[0])
    record["shape"] = "LINEAR" if first_type == _CURVE_TYPE_POLY else shape
    record["max_radius"] = float(np.abs(radii).max()) if points else 0.0
    record["attributes"], record["skipped_attributes"] = _generic_attributes(
        attributes, key, arrays, ("POINT", "CURVE"), _CURVE_INTERNAL | {"cyclic"}
    )
    # attr_create_generic: "Weak, use first float2 attribute as standard UV."
    record["uv_attribute"] = next(
        (
            field["name"]
            for field in record["attributes"]
            if field["data_type"] == "FLOAT2" and field["domain"] == "CURVE"
        ),
        None,
    )
    return record


def particle_hair_systems(obj):
    """(modifier, particle system) pairs Blender's Cycles renders as hair strands
    (object_has_particle_hair: render-enabled Particle System modifiers, Path, Hair)."""
    result = []
    for modifier in getattr(obj, "modifiers", ()):
        if modifier.type != "PARTICLE_SYSTEM" or not modifier.show_render:
            continue
        system = modifier.particle_system
        settings = system.settings
        if settings.render_type == "PATH" and settings.type == "HAIR":
            result.append((modifier, system))
    return result


def _shape_radius(shape, root, tip, time):
    """shaperadius (curves.cpp)."""
    radius = 1.0 - time
    if shape != 0.0:
        radius = np.power(radius, 1.0 + shape) if shape < 0.0 else np.power(radius, 1.0 / (1.0 - shape))
    return radius * (root - tip) + tip


def _particle_hair_record(obj, key, arrays, shape, palette_size, referenced):
    """Legacy particle hair of one emitter as ObtainCacheParticleData, ExportCurveSegments and
    sync_particle_hair export it: per system (modifier order) the children when there are any,
    else the parents, (1 << render_step) + 1 keys (plus the extra spiral kink steps) from the
    render path cache in object space, radius from root/tip/shape/scale with the closed tip,
    material slot settings.material - 1, then the emitter's UV maps and byte colour attributes
    at each strand root (BKE_particle_uv_on_emitter, BKE_particle_mcol_on_emitter), Generated
    from the root key in the emitter mesh's texture space, and a random value per curve.
    None if no strand is rendered."""
    from mathutils import Matrix

    matrix = np.array(Matrix(obj.matrix_world), dtype=np.float64)
    inverse = np.linalg.inv(matrix)
    positions, radii, counts, slots, roots = [], [], [], [], []
    per_system = []  # (modifier, system, first particle number, curve count)
    for modifier, system in particle_hair_systems(obj):
        settings = system.settings
        parents, children = len(system.particles), len(system.child_particles)
        include_parents = settings.child_type == "NONE" or children == 0
        total = children + (parents if include_parents else 0)
        if total == 0:
            continue
        steps = (1 << int(settings.render_step)) + 1
        if settings.kink == "SPIRAL":
            steps += int(settings.kink_extra_steps)
        first = 0 if include_parents else parents
        co = system.co_hair
        flat = np.fromiter(
            itertools.chain.from_iterable(
                co(obj, particle_no=i, step=k) for i in range(first, parents + children) for k in range(steps)
            ),
            dtype=np.float64,
            count=total * steps * 3,
        ).reshape(total, steps, 3)
        # BKE_particle_co_hair leaves its output untouched past a strand's last cached key;
        # Cycles then repeats the previous key (co_world starts as prev_co_world), RNA gives zero.
        for k in range(1, steps):
            missing = ~flat[:, k].any(axis=1)
            flat[missing, k] = flat[missing, k - 1]
        local = (flat @ inverse[:3, :3].T + inverse[:3, 3]).astype(np.float32)
        segment = np.linalg.norm(np.diff(local, axis=1), axis=2).astype(np.float32)
        time = np.concatenate([np.zeros((total, 1), np.float32), np.cumsum(segment, axis=1, dtype=np.float32)], axis=1)
        length = time[:, -1:]
        with np.errstate(divide="ignore", invalid="ignore"):
            fraction = np.where(length > 0.0, time / np.where(length > 0.0, length, 1.0), 0.0).astype(np.float32)
        scale = float(settings.radius_scale) * 0.5
        radius = _shape_radius(
            float(settings.shape), scale * float(settings.root_radius), scale * float(settings.tip_radius), fraction
        ).astype(np.float32)
        if settings.use_close_tip:
            radius[:, -1] = 0.0
        positions.append(local.reshape(-1, 3))
        radii.append(radius.reshape(-1))
        counts.append(np.full(total, steps, dtype=np.int32))
        slots.append(np.full(total, min(max(int(settings.material) - 1, 0), max(palette_size - 1, 0)), np.int32))
        roots.append(local[:, 0])
        per_system.append((modifier, system, first, parents + children))
    if not per_system:
        return None
    count = sum(len(c) for c in counts)
    record = dict(key=key, name=obj.name_full, kind="particles", curve_count=count, arrays={}, shape=shape)

    def add(name, value):
        archive_key = key + "/" + name
        arrays[archive_key] = np.ascontiguousarray(value)
        record["arrays"][name] = archive_key
        return archive_key

    positions = np.concatenate(positions)
    radii = np.concatenate(radii)
    counts = np.concatenate(counts)
    record["point_count"] = len(positions)
    add("positions", positions)
    add("radius", radii)
    add("offsets", np.concatenate([[0], np.cumsum(counts)]).astype(np.int32))
    add("randoms", curve_randoms(count))
    add("material_slots", np.concatenate(slots))
    record["max_radius"] = float(np.abs(radii).max()) if len(radii) else 0.0
    # sync_particle_hair: Generated = root * size - loc with mesh_texture_space's size and loc.
    data = getattr(obj, "data", None)
    location = np.array(getattr(data, "texspace_location", (0.0, 0.0, 0.0)), dtype=np.float32)
    size = np.array(getattr(data, "texspace_size", (1.0, 1.0, 1.0)), dtype=np.float32)
    size = np.where(size != 0.0, np.float32(0.5) / np.where(size != 0.0, size, 1.0), 0.0).astype(np.float32)
    location = location * size - np.float32(0.5)
    add("generated", (np.concatenate(roots) * size - location).astype(np.float32))
    # The emitter's UV maps and byte colours at the strand roots; children use the first
    # particle as Blender's translator passes it (only a parent's own data is read).
    record["attributes"], record["skipped_attributes"] = [], []
    layers = list(getattr(data, "uv_layers", ()) or ())
    default = next((layer.name for layer in layers if layer.active_render), None)
    colors = [
        attribute.name
        for attribute in getattr(data, "attributes", ())
        if attribute.data_type == "BYTE_COLOR" and attribute.domain == "CORNER"
    ]
    requests = [
        ("uv", index, layer.name)
        for index, layer in enumerate(layers)
        if layer.name == default or layer.name in referenced
    ]
    requests += [("color", index, name) for index, name in enumerate(colors) if name in referenced]
    record["uv_attribute"] = None
    for kind, index, name in requests:
        values = []
        for modifier, system, first, end in per_system:
            particles = system.particles
            function = system.uv_on_emitter if kind == "uv" else system.mcol_on_emitter
            number = "uv_no" if kind == "uv" else "vcol_no"
            # ObtainCacheParticleUV/Vcol: the parent's own particle while parents are exported,
            # then (children) the first particle, whose data is not read for a child.
            fallback = particles[0] if len(particles) else None
            source = itertools.chain(particles if first == 0 else (), itertools.repeat(fallback))
            try:
                values.extend(
                    function(modifier, particle=particle, particle_no=i, **{number: index})
                    for i, particle in zip(range(first, end), source)
                )
            except (RuntimeError, IndexError) as error:
                record["skipped_attributes"].append(dict(name=name, domain="CURVE", data_type=kind, reason=str(error)))
                values = None
                break
        if values is None:
            continue
        width = 2 if kind == "uv" else 3
        array = np.array(values, dtype=np.float32).reshape(-1, width)
        if kind == "color":
            array = np.concatenate([srgb_to_linear(array), np.ones((len(array), 1), np.float32)], axis=1)
        archive_key = add("attribute_" + kind + "_" + str(index), array)
        record["attributes"].append(
            dict(name=name, domain="CURVE", data_type="FLOAT2" if kind == "uv" else "FLOAT_COLOR", array=archive_key)
        )
        if kind == "uv" and name == default:
            record["uv_attribute"] = name
    return record


# ------------------------------------------------------------------ Link point clouds
#
# Link renders carry point clouds the way Blender's Cycles translator syncs them. Ported from
# Blender v5.3.0 (Apache-2.0, SPDX-FileCopyrightText: 2011-2022 Blender Foundation):
# intern/cycles/blender/pointcloud.cpp (export_pointcloud, copy_attributes) and
# intern/cycles/blender/geometry.cpp (determine_geom_type, find_used_shaders).

_POINT_DEFAULT_RADIUS = 0.01  # export_pointcloud: a point cloud without "radius"


def _point_cloud_record(cloud, key, arrays, referenced):
    """A PointCloud datablock (object type POINTCLOUD, also every geometry-nodes point cloud:
    Mesh to Points, Distribute Points on Faces, Points, realized point instances) as
    export_pointcloud reads it: positions, the POINT "radius" (float, integer or boolean) or
    0.01, a random value per point (ATTR_STD_POINT_RANDOM, hash_uint2_to_float(index, 0)), every other attribute by name
    (copy_attributes). "radius" is Cycles' standard radius attribute (an Attribute node named
    "radius" reads it); "position" is copied under its name only when a material reads it.
    Material indices are ignored (every point uses shader 0, the first slot) and there is no
    Generated attribute (Cycles falls back to the object-space position)."""
    count = len(cloud.points)
    attributes = cloud.attributes
    record = dict(key=key, name=cloud.name_full, kind="points", point_count=count, arrays={})

    def add(name, value):
        archive_key = key + "/" + name
        arrays[archive_key] = np.ascontiguousarray(value)
        record["arrays"][name] = archive_key

    if "position" not in attributes:
        return None
    positions = _array(attributes["position"].data, "vector", count, 3, np.float32)
    add("positions", positions)
    # lookup<float>("radius", Point): an integer or boolean radius too (_converter_radius).
    radii, _ = _converter_radius(attributes, count, None, _POINT_DEFAULT_RADIUS)
    add("radius", radii)
    add("randoms", curve_randoms(count))
    record["max_radius"] = float(np.abs(radii).max()) if count else 0.0
    exclude = {"radius"} if "position" in referenced else {"radius", "position"}
    record["attributes"], record["skipped_attributes"] = _generic_attributes(
        attributes, key, arrays, ("POINT",), exclude
    )
    return record


def _collection_paths(bpy, name):
    """{object name_full: [collection names from the selected collection down]} (first path wins)."""
    root = bpy.data.collections.get(name)
    paths = {}
    if root is None:
        return paths
    pending = [(root, [root.name])]
    while pending:
        collection, path = pending.pop(0)
        for obj in collection.objects:
            paths.setdefault(obj.name_full, path)
        pending.extend((child, path + [child.name]) for child in collection.children)
    return paths


_NO_DUPLI = 0x7FFFFFFF


def _geometry_instance(obj, original, depsgraph):
    """True for a temporary dupli carrying geometry other than its original object's own."""
    try:
        evaluated = original.evaluated_get(depsgraph)
    except Exception:
        return False
    data, own = getattr(obj, "data", None), getattr(evaluated, "data", None)
    return data is not None and own is not None and data.as_pointer() != own.as_pointer()


def _dupli_path(instance):
    return [int(value) for value in instance.persistent_id if int(value) != _NO_DUPLI]


# Per-object values Blender's Cycles syncs for every object and instance (Link renders, audit
# items 3, 5, 6; docs/dev/blend-import.md "Per-object values"). Ported from Blender v5.3.0
# intern/cycles (Apache-2.0, SPDX-FileCopyrightText: 2011-2022 Blender Foundation):
# intern/cycles/util/hash.h (hash_uint2, hash_string), intern/cycles/blender/object.cpp
# (sync_object, sync_object_attributes) and util.h (object_ray_visibility).
# LICENCE NOTE: dupli_random_id, _rgba, property_rgba and the attribute lookup of _ObjectValues
# (attributes and its helpers) followed Blender v5.3.0 source/blender/blenkernel/intern/
# object_dupli.cc, which is GPL-2.0-or-later. They were removed and rewritten (21 November
# 2026) from the behaviour description docs/dev/provenance/blender-object-values.md, Blender's
# Python API documentation and user manual, without reading Blender's source; see that document's
# Provenance section (its author had read the old code and Blender's source).
_UINT = 0xFFFFFFFF
# Cycles PathRayVisibility bits; the Houdini object visibility mask uses the same bits.
_RAY_VISIBILITY = (
    ("visible_camera", 1),
    ("visible_transmission", 2),
    ("visible_diffuse", 4),
    ("visible_glossy", 8),
    ("visible_volume_scatter", 16),
    ("visible_shadow", 32 | 64),
    ("visible_raycast", 128),
)
_OBJECT_ATTRIBUTES = ("OBJECT", "INSTANCER")
# Blender's own data on geometry-nodes instances (the transform and reference are the packed
# primitives themselves).
_INSTANCE_INTERNAL = frozenset(("instance_transform", "position"))


def dupli_random_id(name, persistent_id, root_name=None):
    """Object Info > Random of an instance (Blender's DepsgraphObjectInstance.random_id). name:
    the instanced object's name; persistent_id: its used entries, innermost first; root_name:
    the scene object the instances come from when it is not the instanced object itself (else
    None). Returns an unsigned 32-bit int. See docs/dev/provenance/blender-object-values.md."""
    # Fold every used level into the name hash with the two-word hash above; an object without
    # levels folds a single 0. A different root is mixed in afterwards by XOR.
    seed = hash_string(name)
    for entry in list(persistent_id) or [0]:
        seed = hash_uint2(seed, int(entry) & _UINT)
    if root_name is not None:
        seed ^= object_random_id(root_name)
    return seed


def ray_visibility(obj):
    """object_ray_visibility: the Cycles visibility bits of an object's Visibility > Ray switches."""
    return sum(mask for name, mask in _RAY_VISIBILITY if getattr(obj, name, True))


def _rgba(value):
    """A property value as [r, g, b, a] floats, or None when it is not convertible. See
    docs/dev/provenance/blender-object-values.md."""
    if isinstance(value, bool):
        value = 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        grey = float(value)
        return [grey, grey, grey, 1.0]
    if isinstance(value, (str, bytes)):
        return None
    try:
        # At most five items are read: enough to reject anything longer than four.
        items = list(itertools.islice(iter(value), 5))
    except TypeError:
        return None
    if not 1 <= len(items) <= 4:
        return None
    if any(isinstance(item, bool) or not isinstance(item, (int, float)) for item in items):
        return None
    colour = [0.0, 0.0, 0.0, 1.0]
    colour[: len(items)] = [float(item) for item in items]
    return colour


def property_rgba(block, name):
    """The value of a custom property or RNA path `name` on data-block `block`, as [r, g, b, a],
    or None when `block` is None, nothing is found or it is not convertible. See
    docs/dev/provenance/blender-object-values.md."""
    if block is None:
        return None
    try:
        custom = name in block.keys()
    except (AttributeError, TypeError):
        custom = False  # a struct without custom properties
    if custom:
        # A custom property wins over a built-in one of the same name, even when unusable.
        return _rgba(block[name])
    try:
        value = block.path_resolve(name)
    except (ValueError, AttributeError, TypeError):
        return None
    return _rgba(value)


# Object type -> the GeometrySet component Blender renders as that object itself (not counted
# among the separately rendered components of the root's own geometry).
_OWN_COMPONENT = {
    "MESH": "mesh",
    "VOLUME": "volume",
    "CURVE": "curves",
    "FONT": "curves",
    "CURVES": "curves",
    "POINTCLOUD": "pointcloud",
    "GREASEPENCIL": "grease_pencil",
}
# Blender's manual (Attribute node): "Currently only up to 4 layers of Geometry Node instancing are
# searched"; a Blender 5.3 render with six nested layers found the four innermost only (experiment
# E1, docs/dev/provenance/blender-object-values.md 5.5; test_instancer_attribute_searches_four_innermost_layers).
_INSTANCE_LAYER_LIMIT = 4
_GEOMETRY_COMPONENTS = ("mesh", "volume", "curves", "pointcloud", "grease_pencil")
# Instance attribute data type -> (property read by foreach_get, values per point, numpy type).
_INSTANCE_COLUMNS = {
    "FLOAT": ("value", 1, np.float32),
    "INT": ("value", 1, np.int32),
    "INT8": ("value", 1, np.int32),
    "BOOLEAN": ("value", 1, np.bool_),
    "FLOAT2": ("vector", 2, np.float32),
    "INT32_2D": ("value", 2, np.int32),
    "FLOAT_VECTOR": ("vector", 3, np.float32),
    "FLOAT_COLOR": ("color", 4, np.float32),
    "BYTE_COLOR": ("color", 4, np.float32),
}


def _point_column(cloud, name, data_type, prop, width, dtype):
    """One POINT attribute of a point cloud as a numpy array of `width` columns, or None."""
    attribute = cloud.attributes.get(name)
    if attribute is None or attribute.domain != "POINT" or attribute.data_type != data_type:
        return None
    count = len(cloud.points)
    buffer = np.zeros(count * width, dtype=dtype)
    try:
        attribute.data.foreach_get(prop, buffer)
    except (TypeError, RuntimeError):
        # Fallback for buffer types foreach_get does not accept for this attribute.
        flat = []
        for item in attribute.data:
            entry = getattr(item, prop)
            flat.extend(entry if width > 1 else [entry])
        buffer = np.asarray(flat, dtype=dtype)
    return buffer.reshape(count, width) if width > 1 else buffer


class _InstanceLevel:
    """One level of geometry-nodes instances: the instances point cloud of a GeometrySet, how its
    instances are numbered in persistent_id, and its converted attribute columns (cached)."""

    def __init__(self, geometry, own_component):
        self.geometry = geometry
        self.cloud = geometry.instances_pointcloud()
        self.size = len(self.cloud.points)
        self.components = sum(
            1
            for component in _GEOMETRY_COMPONENTS
            if component != own_component and getattr(geometry, component, None) is not None
        )
        ids = _point_column(self.cloud, "id", "INT", "value", 1, np.int32)
        if ids is None:
            self.sorted_ids = self.first_index = None
        else:
            # np.unique returns the first occurrence of each id: duplicates match only the first.
            self.sorted_ids, self.first_index = np.unique(ids, return_index=True)
        self.references = None
        self.children = {}
        self.colours = {}

    @classmethod
    def make(cls, geometry, own_component=None):
        if geometry is None:
            return None
        cloud = geometry.instances_pointcloud()
        if cloud is None or len(cloud.points) == 0:
            return None
        return cls(geometry, own_component)

    def index_of(self, identifier):
        """The point index of the instance numbered `identifier`, or None."""
        if self.sorted_ids is None:
            return identifier if 0 <= identifier < self.size else None
        slot = int(np.searchsorted(self.sorted_ids, identifier))
        if slot < len(self.sorted_ids) and int(self.sorted_ids[slot]) == identifier:
            return int(self.first_index[slot])
        return None

    def reference(self, index):
        """What instance `index` instances: a GeometrySet, an object, a collection or None."""
        if self.references is None:
            self.references = list(self.geometry.instance_references())
            self.reference_index = _point_column(self.cloud, ".reference_index", "INT", "value", 1, np.int32)
        if self.reference_index is None:
            return None
        slot = int(self.reference_index[index])
        return self.references[slot] if 0 <= slot < len(self.references) else None

    def child(self, index, object_level):
        """The level of instances inside instance `index`: a GeometrySet reference's own
        instances, or for an object reference the object's evaluated geometry (object_level
        builds and caches that; a collection or None reference has no further level)."""
        if index not in self.children:
            reference = self.reference(index)
            if hasattr(reference, "instances_pointcloud"):  # a GeometrySet
                level = _InstanceLevel.make(reference)
            elif getattr(reference, "type", None) is not None and hasattr(reference, "evaluated_get"):
                level = object_level(reference)  # an object
            elif hasattr(reference, "all_objects"):
                level = _CollectionLevel(reference)
            else:
                level = None
            self.children[index] = level
        return self.children[index]

    def colours_of(self, name):
        """Attribute `name` of every instance as an (N, 4) float array, or None when this level
        has no convertible POINT attribute of that name."""
        if name not in self.colours:
            attribute = self.cloud.attributes.get(name)
            table = None
            if attribute is not None and attribute.domain == "POINT":
                spec = _INSTANCE_COLUMNS.get(attribute.data_type)
                if spec is not None:
                    column = _point_column(self.cloud, name, attribute.data_type, *spec)
                    if column is not None:
                        column = column.astype(np.float64).reshape(self.size, -1)
                        table = np.zeros((self.size, 4), dtype=np.float64)
                        table[:, 3] = 1.0
                        if column.shape[1] == 1:
                            table[:, :3] = column  # a scalar fills red, green and blue
                        else:
                            table[:, : column.shape[1]] = column
            self.colours[name] = table
        return self.colours[name]


class _CollectionLevel:
    """An instanced collection inside geometry-nodes instances. Observed numbering (experiment E4):
    member k of the collection takes entry 2k for its own geometry and 2k + 1 for the instances
    its geometry nodes make. Only collections without child collections are followed (the order
    across child collections was not measured)."""

    def __init__(self, collection):
        self.members = None if len(collection.children) else list(collection.objects)

    def member_level(self, entry, object_level):
        if entry is None or self.members is None or entry < 0 or entry % 2 == 0:
            return None
        k = entry // 2
        return object_level(self.members[k]) if k < len(self.members) else None


def _layer_flags(view_layer):
    """{object pointer: [holdout, indirect_only]} from the view layer's collections that link the
    object directly. Blender gives an object a collection's Holdout and Indirect Only flags only
    when that collection itself contains the object (not through a parent collection), and skips
    excluded collections."""
    flags, pending = {}, [view_layer.layer_collection] if view_layer is not None else []
    while pending:
        layer = pending.pop()
        pending.extend(layer.children)
        if layer.exclude or not (layer.holdout or layer.indirect_only):
            continue
        for obj in layer.collection.objects:
            entry = flags.setdefault(obj.as_pointer(), [False, False])
            entry[0] |= bool(layer.holdout)
            entry[1] |= bool(layer.indirect_only)
    return flags


class _ObjectValues:
    """Blender's per-object values for each exported instance of a Link render (sync_object).

    A collection is evaluated as an identity collection instance (_select), so its members
    arrive as duplis of the temporary selector. Blender renders the source file's collection
    with those members as real objects: a depth-1 dupli is treated as the real object, and a
    deeper one as an instance generated by that member (the member is the root object, the
    selector's level is dropped from persistent_id). Object selections are already real.
    """

    def __init__(self, depsgraph, request):
        self.depsgraph = depsgraph
        self.collection = request["selector_type"].upper() == "COLLECTION"
        self.members = {}
        self.flags = _layer_flags(getattr(depsgraph, "view_layer", None))
        self.evaluated, self.levels, self.columns = {}, {}, {}

    def track(self, instance, original):
        """Remember the selector's members (depth-1 duplis come before what they generate)."""
        if self.collection and instance.is_instance:
            path = _dupli_path(instance)
            if len(path) == 1:
                self.members[path[0]] = original

    def _root(self, instance, original):
        """(root original, root evaluated, persistent_id below the root, is an instance in Blender)."""
        if not instance.is_instance:
            return original, instance.object, [], False
        path = _dupli_path(instance)
        if self.collection:
            if len(path) == 1:
                return original, instance.object, [], False
            member = self.members.get(path[-1])
            if member is not None:
                pointer = member.as_pointer()
                if pointer not in self.evaluated:
                    self.evaluated[pointer] = member.evaluated_get(self.depsgraph)
                return member, self.evaluated[pointer], path[:-1], True
        parent = instance.parent
        return _original(parent), parent, path, True

    def values(self, instance, original):
        """dict(random_id, visibility, holdout, shadow_catcher, pass_index, color), or None for an
        object Blender does not render (no ray visibility left)."""
        obj = instance.object
        root, root_evaluated, path, dupli = self._root(instance, original)
        visibility = ray_visibility(obj) & ray_visibility(root_evaluated)
        holdout_layer, indirect_layer = self.flags.get(root.as_pointer(), (False, False))
        holdout = bool(holdout_layer or root_evaluated.is_holdout)
        if not holdout and indirect_layer:
            visibility &= ~1
        if not visibility:
            return None
        if not dupli:
            random_id = object_random_id(original.name)
        elif self.collection:
            random_id = dupli_random_id(
                original.name, path, None if root.as_pointer() == original.as_pointer() else root.name
            )
        else:
            random_id = int(instance.random_id) & _UINT
        color = [float(value) for value in obj.color]
        if not all(math.isfinite(value) for value in color):
            raise ValueError("Non-finite Blender object colour: " + original.name_full)
        values = dict(
            random_id=random_id,
            visibility=visibility,
            holdout=holdout,
            shadow_catcher=bool(obj.is_shadow_catcher or root_evaluated.is_shadow_catcher),
            pass_index=int(obj.pass_index),
            color=color,
        )
        values["_root"] = (root_evaluated, path, dupli)
        return values

    def attributes(self, instance, values, requests):
        """Object and Instancer attributes of one exported instance: [dict(type, name, value)]
        in the order of `requests` ((type, name) pairs, type "OBJECT" or "INSTANCER"); value is
        [r, g, b, a] floats, 0, 0, 0, 0 when nothing is found. values is the dict values()
        returned (its "_root" entry). See docs/dev/provenance/blender-object-values.md."""
        root_evaluated, path, dupli = values["_root"]
        obj = instance.object
        layers = None
        found = []
        for kind, name in requests:
            value = None
            if kind == "INSTANCER" and dupli:
                system = instance.particle_system
                if system is not None:
                    value = property_rgba(system.settings, name)
                if value is None:
                    if layers is None:
                        layers = self._instance_layers(root_evaluated, path)
                    value = self._layer_value(layers, name)
                if value is None:
                    value = property_rgba(root_evaluated, name)
            if value is None:
                value = property_rgba(obj, name)
            if value is None:
                value = property_rgba(getattr(obj, "data", None), name)
            if value is None:
                value = [0.0, 0.0, 0.0, 0.0]
            if not all(math.isfinite(component) for component in value):
                raise ValueError("Non-finite Blender " + kind.lower() + " attribute " + name)
            found.append(dict(type=kind, name=name, value=value))
        return found

    def _instance_layers(self, root_evaluated, path):
        """[(level, point index)] of the geometry-nodes instances `path` passes through, outermost
        first. Levels are cached per root object for the whole export (self.levels)."""
        level, layers = self._object_level(root_evaluated), []
        entries = iter(reversed(path))  # outermost level first
        while level is not None:
            if isinstance(level, _CollectionLevel):
                level = level.member_level(next(entries, None), self._object_level)
                continue
            if level.components:
                # The level's own components take numbers 0 .. c-1; its instances follow the count.
                if next(entries, None) != level.components:
                    break
            identifier = next(entries, None)
            if identifier is None:
                break
            index = level.index_of(identifier)
            if index is None:
                break
            layers.append((level, index))
            # An instanced object continues with its own geometry-nodes instances, numbered like
            # a root's, with no path entry of its own (experiment E3, see the provenance document).
            level = level.child(index, self._object_level)
        return layers

    def _object_level(self, obj):
        """The outermost instance level of an object's evaluated geometry (its own kind of
        component not counted), cached per evaluated object for the whole export."""
        if not getattr(obj, "is_evaluated", True):
            obj = obj.evaluated_get(self.depsgraph)
        key = obj.as_pointer()
        if key not in self.levels:
            evaluate = getattr(obj, "evaluated_geometry", None)
            geometry = evaluate() if evaluate is not None else None
            self.levels[key] = _InstanceLevel.make(geometry, _OWN_COMPONENT.get(getattr(obj, "type", None)))
        return self.levels[key]

    @staticmethod
    def _layer_value(layers, name):
        """The first convertible instance attribute `name`, innermost layer first, or None. Only
        the innermost _INSTANCE_LAYER_LIMIT layers are searched."""
        for level, index in reversed(layers[-_INSTANCE_LAYER_LIMIT:]):
            table = level.colours_of(name)
            if table is not None:
                return [float(component) for component in table[index]]
        return None


def _material_names(materials):
    """Converter requests with material_export="names": identify materials without translating them."""
    records = []
    for key, material in materials.items():
        library = getattr(material, "library", None) if material is not None else None
        records.append(
            dict(
                key=key,
                name=material.name_full if material is not None else "Blender default surface",
                local_name=material.name if material is not None else None,
                library=str(library.filepath) if library else "",
                default=material is None,
                nodes=[],
                outputs={},
            )
        )
    return dict(materials=records, dependencies=[], diagnostics=[], color_management={})


def _dependencies(bpy, source, material_result):
    result = {str(Path(source).resolve())}
    for library in bpy.data.libraries:
        result.add(str(Path(bpy.path.abspath(library.filepath, library=library.parent)).resolve()))
    # Include exporter-resolved texture dependencies, including sequence/UDIM
    # tile paths. Packed textures have no external dependency to invalidate.
    for dependency in material_result.get("dependencies", []):
        if isinstance(dependency, str):
            result.add(str(Path(dependency).resolve()))
        elif dependency.get("source_path") and not dependency.get("packed", False):
            result.add(str(Path(dependency["source_path"]).resolve()))
    return sorted(result)


_NON_RENDERING_TYPES = frozenset(("CAMERA", "LIGHT", "EMPTY", "ARMATURE", "LATTICE", "SPEAKER", "LIGHT_PROBE"))
# Renderable Blender geometry a Link render does not carry yet: skipped with one warning per type.
# Hair (Curves objects and particle hair) is carried (_hair_curves_record, _particle_hair_record),
# and point clouds (_point_cloud_record) except Gaussian splats (Blender renders those as
# splats, RENDER_AS_GSPLATS, which Link mode does not carry yet).
_UNSUPPORTED_LINK_TYPES = {
    "GAUSSIAN_SPLAT": "Gaussian splat point clouds",
    "VOLUME": "volumes",
    "GREASEPENCIL": "Grease Pencil",
    "GPENCIL": "Grease Pencil",
}


def _unsupported_diagnostics(skipped):
    """One warning per skipped object type, naming its objects."""
    result = []
    for kind, names in skipped.items():
        label = _UNSUPPORTED_LINK_TYPES.get(kind, "Blender " + kind + " objects")
        result.append(
            dict(
                code="blend.unsupported_geometry",
                severity="warning",
                source=", ".join(names),
                message="Not rendered: Link mode does not support "
                + label
                + " ("
                + kind
                + ") yet; skipped "
                + ", ".join(names),
            )
        )
    return result


_CONVERTER_FLAGS = ("vertex_groups", "curves", "pointclouds", "object_tree")


def _instance_count(geometry):
    cloud = geometry.instances_pointcloud()
    return len(cloud.points) if cloud is not None else 0


def _converter_request(request):
    return any(request.get(flag, False) for flag in _CONVERTER_FLAGS)


def _flat(matrix):
    return [float(matrix[row][column]) for row in range(4) for column in range(4)]


def _palette_keys(obj, data, material_module, materials):
    palette = list(getattr(data, "materials", ()) or ()) or [None]
    # Object-level slot links override the evaluated data palette.
    for index, slot in enumerate(obj.material_slots):
        if index < len(palette) and slot.link == "OBJECT":
            palette[index] = slot.material
    keys = []
    for material in palette:
        key = material_module.material_key(material)
        keys.append(key)
        materials[key] = material
    return keys


class _ConverterState:
    """Converter-only bookkeeping: selection members, owners, curves and point clouds.

    A collection is evaluated as one identity collection instance, so its members
    arrive as depth-1 duplis (persistent_id has one used entry) and anything they
    generate (geometry nodes instances, nested collection instances) is deeper;
    the first persistent_id entry names the generating member.
    """

    def __init__(self, bpy, request, conversion):
        self.request = request
        self.conversion = conversion
        self.collection = request["selector_type"].upper() == "COLLECTION"
        self.paths = _collection_paths(bpy, request["selector"]) if self.collection else {}
        self.members, self.objects, self.names = {}, [], set()
        self.curves, self.pointclouds, self.cache = [], [], {}
        self.trees, self.tree_checked = {}, set()
        self.instance_attributes = {}  # owner name -> instance-domain attribute names

    def instance_domain(self, original, depsgraph):
        """Names of the INSTANCE-domain attributes of a geometry-nodes object's instances (nested
        instances too). The converter does not carry them (packed primitives get no per-instance
        values yet); the import report names them."""
        name = original.name_full
        if name in self.instance_attributes:
            return
        self.instance_attributes[name] = []
        if not any(modifier.type == "NODES" for modifier in getattr(original, "modifiers", ())):
            return
        evaluated = original.evaluated_get(depsgraph)
        geometry = evaluated.evaluated_geometry() if hasattr(evaluated, "evaluated_geometry") else None
        found, pending, seen = [], [geometry], set()
        while pending:
            node = pending.pop()
            if node is None or id(node) in seen:
                continue
            seen.add(id(node))
            cloud = node.instances_pointcloud()
            if cloud is None or not len(cloud.points):
                continue
            for attribute in cloud.attributes:
                if not attribute.name.startswith(".") and attribute.name not in _INSTANCE_INTERNAL:
                    if attribute.name not in found:
                        found.append(attribute.name)
            pending.extend(
                reference for reference in node.instance_references() if type(reference).__name__ == "GeometrySet"
            )
        self.instance_attributes[name] = found

    def instance_tree(self, original, depsgraph, arrays):
        """Record a geometry nodes object's instance hierarchy once (request flag nested_instances).

        The depsgraph flattens nested instances; the evaluated geometry keeps the tree. Each
        node lists, per instance, its reference index and local transform (row-major, Blender
        axes); a reference that is itself instancing geometry is a child node, anything else
        (components, objects, collections) is -1 and arrives as ordinary duplis.
        """
        name = original.name_full
        if name in self.tree_checked or not self.request.get("nested_instances", False):
            return
        self.tree_checked.add(name)
        if not any(modifier.type == "NODES" for modifier in getattr(original, "modifiers", ())):
            return
        evaluated = original.evaluated_get(depsgraph)
        geometry = evaluated.evaluated_geometry() if hasattr(evaluated, "evaluated_geometry") else None
        if geometry is None or not _instance_count(geometry):
            return
        prefix = "tree_" + str(len(self.trees)).zfill(4)
        nodes = []

        def add(node):
            index = len(nodes)
            nodes.append(None)
            cloud = node.instances_pointcloud()
            count = len(cloud.points)
            references = []
            for reference in node.instance_references():
                nested = type(reference).__name__ == "GeometrySet" and _instance_count(reference)
                references.append(add(reference) if nested else -1)
            ref_index = np.empty(count, dtype=np.int32)
            cloud.attributes[".reference_index"].data.foreach_get("value", ref_index)
            columns = np.empty(count * 16, dtype=np.float32)
            cloud.attributes["instance_transform"].data.foreach_get("value", columns)
            transforms = columns.reshape(count, 4, 4).transpose(0, 2, 1)  # foreach_get is column-major
            if not np.isfinite(transforms).all():
                raise ValueError("Non-finite geometry nodes instance transform: " + name)
            key = prefix + "/n" + str(index).zfill(5)
            arrays[key + "/ref_index"] = ref_index
            arrays[key + "/transforms"] = np.ascontiguousarray(transforms)
            nodes[index] = dict(
                name=node.name or "instance",
                count=count,
                ref_index=key + "/ref_index",
                transforms=key + "/transforms",
                references=references,
            )
            return index

        add(geometry)
        self.trees[name] = dict(nodes=nodes)

    def _object(self, original, instance):
        if original.name_full in self.names:
            return
        self.names.add(original.name_full)
        blender_parent = getattr(original, "parent", None)
        self.objects.append(
            dict(
                name=original.name_full,
                type=original.type,
                parent=blender_parent.name_full if blender_parent is not None else None,
                collection_path=self.paths.get(original.name_full, []),
                transform=_flat(self.conversion @ instance.matrix_world),
                instance_type=str(getattr(original, "instance_type", "NONE")),
            )
        )

    def owner(self, instance, original):
        """{owner, depth, dupli_path} or None for the collection selector itself."""
        path = _dupli_path(instance) if instance.is_instance else []
        if not instance.is_instance:
            if self.collection:
                return None
            self._object(original, instance)
            owner = original.name_full
        elif self.collection:
            if len(path) == 1:
                self.members[path[0]] = original.name_full
                self._object(original, instance)
                owner = original.name_full
            else:
                # persistent_id lists the innermost index first; the last used entry
                # is the index of the selected collection's member.
                owner = self.members.get(path[-1]) if path else None
        else:
            owner = _original(instance.parent).name_full
        return dict(owner=owner, depth=len(path), dupli_path=path)

    def geometry(self, obj, original, arrays, material_module, materials):
        """(key, material_keys, geometry_type) for curves/point clouds, or None if not requested."""
        kind = {"CURVES": "curves", "POINTCLOUD": "pointcloud"}.get(obj.type)
        if kind is None or not self.request.get("curves" if kind == "curves" else "pointclouds", False):
            return None
        data = obj.data
        pointer = (obj.as_pointer(), _identity(original), data.as_pointer())
        if pointer not in self.cache:
            if kind == "curves":
                record = _curves_arrays(data, "curves_" + str(len(self.curves)).zfill(6), arrays)
                self.curves.append(record)
            else:
                record = _pointcloud_arrays(data, "points_" + str(len(self.pointclouds)).zfill(6), arrays)
                self.pointclouds.append(record)
            record["object_name"] = original.name_full  # names the object in the import report
            self.cache[pointer] = (record["key"], _palette_keys(obj, data, material_module, materials))
        key, material_keys = self.cache[pointer]
        return key, material_keys, kind


def _export_depsgraph(bpy, depsgraph, request, output, selected, subdivision, native, diagnostics):
    from mathutils import Matrix

    material_module = _material_exporter()
    conversion = Matrix(_CONVERT)
    arrays, meshes, instances, materials = {}, [], [], {}
    cache = {}
    diagnostics = list(diagnostics)
    convert = _ConverterState(bpy, request, conversion) if _converter_request(request) else None
    # Link renders follow Blender exactly: only real UV maps are UV sets. Conversions promote
    # "uv"-named attributes too (docs/dev/blend-import.md).
    promote_uv = convert is not None
    skipped = {}
    # Link renders carry Blender's per-object values and skip objects with no ray visibility.
    objects = _ObjectValues(depsgraph, request) if convert is None else None
    invisible = []
    # Link hair (Curves objects and legacy particle hair) as Blender's translator syncs it.
    hair_records = []
    shape = hair_shape(depsgraph.scene)
    point_records = []  # Link point clouds (_point_cloud_record)
    unconverted_hair = []  # conversions: emitters whose legacy particle hair is not converted

    def hair_instance(instance, original, parent, values, cached, geometry_type):
        """One Link instance of a hair or point cloud record, with the object's per-object values
        (Blender syncs particle hair as a second object of the emitter with the same values)."""
        key, material_keys, requests = cached
        matrix = conversion @ instance.matrix_world
        flat = _flat(matrix)
        if not all(math.isfinite(value) for value in flat):
            raise ValueError("Non-finite Blender instance transform: " + original.name_full)
        values = dict(values)
        values["attributes"] = objects.attributes(instance, values, requests) if requests else []
        del values["_root"]
        instances.append(
            dict(
                key="instance_" + str(len(instances)).zfill(6),
                name=original.name_full,
                source_name=original.name_full,
                source_library=_identity(original)[0],
                mesh_key=key,
                geometry_type=geometry_type,
                transform=flat,
                material_keys=material_keys,
                blender_transform=[
                    float(instance.matrix_world[row][column]) for row in range(4) for column in range(4)
                ],
                persistent_id=[int(value) for value in instance.persistent_id],
                is_instance=bool(instance.is_instance),
                parent_name=parent.name_full if parent else None,
                blender_object=values,
            )
        )

    def hair_palette(obj, data, first_only):
        """find_used_shaders: the object's material slots (object links override the data's);
        a Curves object uses only the first (export_hair_curves sets every curve shader to 0)."""
        palette = list(getattr(data, "materials", ()) or ()) or [None]
        for index, slot in enumerate(obj.material_slots):
            if index < len(palette) and slot.link == "OBJECT":
                palette[index] = slot.material
        if first_only:
            palette = palette[:1]
        keys = []
        for material in palette:
            material_key = material_module.material_key(material)
            keys.append(material_key)
            materials[material_key] = material
        return keys, palette

    # Curve, text and surface objects also arrive as a MESH dupli of themselves (their evaluated
    # mesh component, e.g. a bevel or a geometry nodes result). Exporting the curve-typed entry
    # too would put the same surface in twice, so only the mesh component is exported.
    meshed = {
        _identity(_original(instance.object))
        for instance in depsgraph.object_instances
        if instance.is_instance
        and instance.object.type == "MESH"
        and _original(instance.object).type in _MESH_COMPONENT_TYPES
    }
    for instance in depsgraph.object_instances:
        obj = instance.object
        original = _original(obj)
        parent = _original(instance.parent) if instance.parent else None
        if instance.is_instance:
            if parent is None or _identity(parent) not in selected:
                continue
        elif _identity(original) not in selected:
            continue
        owner = None
        if convert is not None:
            owner = convert.owner(instance, original)
            if owner is None:
                continue  # the temporary collection selector
            if owner["owner"] == original.name_full:
                convert.instance_tree(original, depsgraph, arrays)
                convert.instance_domain(original, depsgraph)
        if objects is not None:
            objects.track(instance, original)
        if (
            convert is not None
            and obj.type == "MESH"
            and particle_hair_systems(obj)
            and not (instance.is_instance and _geometry_instance(obj, original, depsgraph))
            and original.name_full not in unconverted_hair
        ):
            unconverted_hair.append(original.name_full)
        # Legacy particle hair: a second object of the emitter (sync_objects, OB_VISIBLE_PARTICLES),
        # rendered even when the emitter itself is hidden (Show Emitter off). Blender skips the
        # hair of a geometry-nodes geometry instance (not the real object's own data).
        if (
            objects is not None
            and obj.type == "MESH"
            and getattr(instance, "show_particles", True)
            and particle_hair_systems(obj)
            and not (instance.is_instance and _geometry_instance(obj, original, depsgraph))
        ):
            values = objects.values(instance, original)
            if values is None:
                if original.name_full not in invisible:
                    invisible.append(original.name_full)
            else:
                data = getattr(obj, "data", None)
                pointer = ("particle_hair", obj.as_pointer(), _identity(original), data.as_pointer() if data else 0)
                if pointer not in cache:
                    keys, palette = hair_palette(obj, data, False)
                    record = _particle_hair_record(
                        obj,
                        "hair_" + str(len(hair_records)).zfill(6),
                        arrays,
                        shape,
                        len(keys),
                        material_field_names(palette),
                    )
                    if record is not None:
                        hair_records.append(record)
                    cache[pointer] = (record["key"] if record else None, keys, material_object_attributes(palette))
                if cache[pointer][0]:
                    hair_instance(instance, original, parent, values, cache[pointer], "particle_hair")
        if not instance.show_self:
            continue
        if obj.type in _MESH_COMPONENT_TYPES and _identity(original) in meshed:
            continue  # its mesh component dupli carries this geometry
        if objects is not None and obj.type == "CURVES":
            # A Curves object, geometry-nodes hair, or a curves instance (its own object data).
            values = objects.values(instance, original)
            if values is None:
                if original.name_full not in invisible:
                    invisible.append(original.name_full)
                continue
            data = obj.data
            pointer = ("curves", obj.as_pointer(), _identity(original), data.as_pointer())
            if pointer not in cache:
                record = None
                if len(data.curves):
                    record = _hair_curves_record(data, "hair_" + str(len(hair_records)).zfill(6), arrays, shape)
                keys, palette = hair_palette(obj, data, True)
                if record is not None:
                    hair_records.append(record)
                    if record["skipped_attributes"]:
                        diagnostics.append(
                            dict(
                                code="blend.hair_attribute",
                                severity="info",
                                source=original.name_full,
                                message="Hair attributes of a type Link mode does not carry: "
                                + ", ".join(
                                    field["name"] + " (" + field["data_type"] + ")"
                                    for field in record["skipped_attributes"]
                                ),
                            )
                        )
                cache[pointer] = (record["key"] if record else None, keys, material_object_attributes(palette))
            if cache[pointer][0]:
                hair_instance(instance, original, parent, values, cache[pointer], "curves")
            continue
        if objects is not None and obj.type == "POINTCLOUD":
            # A point cloud object, or a geometry-nodes point cloud (its own object data).
            data = obj.data
            if getattr(data, "type", "POINTS") == "GAUSSIAN_SPLAT":
                names = skipped.setdefault("GAUSSIAN_SPLAT", [])
                if original.name_full not in names:
                    names.append(original.name_full)
                continue
            values = objects.values(instance, original)
            if values is None:
                if original.name_full not in invisible:
                    invisible.append(original.name_full)
                continue
            pointer = ("points", obj.as_pointer(), _identity(original), data.as_pointer())
            if pointer not in cache:
                keys, palette = hair_palette(obj, data, True)  # every point uses shader 0
                record = None
                if len(data.points):
                    record = _point_cloud_record(
                        data, "points_" + str(len(point_records)).zfill(6), arrays, material_field_names(palette)
                    )
                if record is not None:
                    point_records.append(record)
                    if record["skipped_attributes"]:
                        diagnostics.append(
                            dict(
                                code="blend.point_attribute",
                                severity="info",
                                source=original.name_full,
                                message="Point attributes of a type Link mode does not carry: "
                                + ", ".join(
                                    field["name"] + " (" + field["data_type"] + ")"
                                    for field in record["skipped_attributes"]
                                ),
                            )
                        )
                cache[pointer] = (record["key"] if record else None, keys, material_object_attributes(palette))
            if cache[pointer][0]:
                hair_instance(instance, original, parent, values, cache[pointer], "points")
            continue
        if obj.type not in ("MESH", "CURVE", "SURFACE", "FONT", "META"):
            geometry = (
                convert.geometry(obj, original, arrays, material_module, materials) if convert is not None else None
            )
            if geometry is not None:
                key, material_keys, kind = geometry
                matrix = conversion @ instance.matrix_world
                flat = _flat(matrix)
                if not all(math.isfinite(value) for value in flat):
                    raise ValueError("Non-finite Blender instance transform: " + original.name_full)
                instances.append(
                    dict(
                        key="instance_" + str(len(instances)).zfill(6),
                        name=original.name_full,
                        source_name=original.name_full,
                        source_library=_identity(original)[0],
                        mesh_key=key,
                        geometry_type=kind,
                        transform=flat,
                        material_keys=material_keys,
                        persistent_id=[int(value) for value in instance.persistent_id],
                        is_instance=bool(instance.is_instance),
                        parent_name=parent.name_full if parent else None,
                        **owner,
                    )
                )
                continue
            # Types that never render (rigs, deformers, helpers) are skipped like
            # cameras and lights; a rig's deformation is already in its evaluated
            # meshes. Renderable non-mesh types Link mode does not carry yet
            # (volumes, grease pencil) are skipped with a warning naming them.
            if obj.type not in _NON_RENDERING_TYPES:
                if convert is not None:
                    # A conversion reports what it cannot carry instead of failing everything.
                    diagnostics.append(
                        dict(
                            code="blend.unsupported_object",
                            severity="warning",
                            source=original.name_full,
                            message="Not converted: Blender object type " + obj.type,
                        )
                    )
                    continue
                names = skipped.setdefault(obj.type, [])
                if original.name_full not in names:
                    names.append(original.name_full)
                continue
            diagnostics.append(
                dict(
                    code="blend.non_mesh_object",
                    severity="info",
                    source=original.name_full,
                    message="Prototype geometry excludes object type " + obj.type,
                )
            )
            continue
        values = objects.values(instance, original) if objects is not None else None
        if objects is not None and values is None:
            if original.name_full not in invisible:
                invisible.append(original.name_full)
            continue  # sync_object: completely invisible objects are not exported
        # The evaluated object is shared by its collection instances. Keeping
        # one local mesh preserves topology and instance transforms separately.
        # Members of a collection outside the view layer arrive through a
        # reused temporary dupli object, so its address alone is not a key.
        data = getattr(obj, "data", None)
        pointer = (obj.as_pointer(), _identity(original), data.as_pointer() if data is not None else 0)
        if pointer not in cache:
            if instance.is_instance and _geometry_instance(obj, original, depsgraph):
                # preserve_all_data_layers re-evaluates the *original* object, which for a
                # geometry nodes geometry instance is the (often empty) instancer mesh. The
                # instance's own evaluated geometry already carries all its attributes and UVs
                # (Blender's Cycles reads the same data: the dupli's object data).
                mesh = obj.to_mesh()
            else:
                mesh = obj.to_mesh(preserve_all_data_layers=True, depsgraph=depsgraph)
            if mesh is None:
                if obj.type == "MESH":
                    raise ValueError("Unable to evaluate Blender mesh: " + original.name_full)
                # Blender's object_to_mesh gives no mesh either (a curve without bevel or
                # extrusion, e.g. curves instanced by geometry nodes, which reach the depsgraph as
                # legacy curves): nothing renders.
                obj.to_mesh_clear()
                cache[pointer] = None
                continue
            try:
                key = "mesh_" + str(len(meshes)).zfill(6)
                palette = list(mesh.materials) or [None]
                # Object-level slot links override the evaluated mesh palette;
                # generated modifier/GN material slots remain in mesh.materials.
                for index, slot in enumerate(obj.material_slots):
                    if index < len(palette) and slot.link == "OBJECT":
                        palette[index] = slot.material
                # Blender's Cycles reads the texture space of the evaluated mesh it renders
                # (create_mesh); for a mesh object that is the evaluated object data.
                data = getattr(obj, "data", None)
                record = _mesh_arrays(
                    mesh,
                    key,
                    arrays,
                    extended=convert is not None,
                    referenced=material_field_names(palette),
                    promote_uv=promote_uv,
                    texture_space=data if obj.type == "MESH" and hasattr(data, "texspace_location") else None,
                )
                if convert is not None:
                    record["object_name"] = original.name_full  # names the object in the import report
                if not promote_uv and not record["uv_layers"]:
                    kept = uv_named_attributes(
                        [(field["name"], field["domain"], field["data_type"]) for field in record["attributes"]]
                    )
                    if kept:
                        diagnostics.append(
                            dict(
                                code="blend.uv_attribute",
                                severity="info",
                                source=original.name_full,
                                message="No UV map; "
                                + ", ".join(kept)
                                + " stays a named attribute as in Blender (Link follows Blender exactly;"
                                " Convert & Append makes uv-named attributes UVs, docs/user/blender-import.md)",
                            )
                        )
                if record["default_uv_source"] == "attribute" or any(
                    field.get("uv_set") for field in record["attributes"]
                ):
                    diagnostics.append(
                        dict(
                            code="blend.uv_attribute",
                            severity="info",
                            source=original.name_full,
                            message=uv_summary(record),
                        )
                    )
                if request.get("vertex_groups", False):
                    _vertex_group_arrays(obj, mesh, record, arrays)
                candidate = native.get(_identity(original))
                if candidate:
                    config = candidate[1]
                    reason = subdivision.validate(mesh, record, arrays, config)
                    if not reason and convert is None and reads_mesh_intrinsics(palette):
                        reason = "Pointiness and Random per Island need Blender's subdivided mesh"
                    if reason:
                        raise subdivision.EvaluatedFallback(_identity(original), reason)
                    record["subdivision"] = config
                    if convert is not None:
                        # Converter only: the viewport level becomes the IPR level on the object tab.
                        record["subdivision_viewport_level"] = int(candidate[0].levels)
                    # Cycles samples the limit surface and its derivatives;
                    # exporting evaluated triangles/normals would be misleading.
                    for field in ("normals", "triangle_vertices", "triangle_loops", "triangle_polygons"):
                        arrays.pop(record["arrays"].pop(field), None)
                    record["triangle_count"] = 0
                    diagnostics.append(
                        dict(
                            code="blend.native_subdivision",
                            severity="info",
                            source=original.name_full,
                            message="Exported control polygons for native Catmull-Clark at render level "
                            + str(config["level"]),
                        )
                    )
                material_keys = []
                for material in palette:
                    material_key = material_module.material_key(material)
                    material_keys.append(material_key)
                    materials[material_key] = material
                meshes.append(record)
                requests = material_object_attributes(palette) if objects is not None else []
                cache[pointer] = (key, material_keys, requests)
            finally:
                obj.to_mesh_clear()
        if cache[pointer] is None:
            continue
        key, material_keys, requests = cache[pointer]
        matrix = conversion @ instance.matrix_world
        flat = [float(matrix[row][column]) for row in range(4) for column in range(4)]
        if not all(math.isfinite(value) for value in flat):
            raise ValueError("Non-finite Blender instance transform: " + original.name_full)
        instances.append(
            dict(
                key="instance_" + str(len(instances)).zfill(6),
                name=original.name_full,
                source_name=original.name_full,
                source_library=_identity(original)[0],
                mesh_key=key,
                transform=flat,
                material_keys=material_keys,
                blender_transform=[
                    float(instance.matrix_world[row][column]) for row in range(4) for column in range(4)
                ],
                persistent_id=[int(value) for value in instance.persistent_id],
                is_instance=bool(instance.is_instance),
                parent_name=parent.name_full if parent else None,
            )
        )
        if owner is not None:
            instances[-1].update(owner, geometry_type="mesh")
        if values is not None:
            values["attributes"] = objects.attributes(instance, values, requests) if requests else []
            del values["_root"]
            instances[-1]["blender_object"] = values
    diagnostics.extend(_unsupported_diagnostics(skipped))
    for name, attributes in convert.instance_attributes.items() if convert is not None else ():
        if attributes:
            diagnostics.append(
                dict(
                    code="blend.instance_attributes",
                    severity="warning",
                    source=name,
                    attributes=list(attributes),
                    message="Instance attributes are not imported (packed primitives get no per-instance values "
                    "yet; a Link render reads them with the Attribute node of type Instancer): "
                    + ", ".join(attributes),
                )
            )
    if unconverted_hair:
        diagnostics.append(
            dict(
                code="blend.particle_hair",
                severity="warning",
                source=", ".join(unconverted_hair),
                message="Particle hair is not converted: only the emitter mesh is imported (nothing for an"
                " emitter whose Show Emitter is off for renders). Convert the hair to a Curves object in"
                " Blender, or use Link mode, which renders it. Emitters: " + ", ".join(unconverted_hair),
            )
        )
    if hair_records:
        settings = hair_settings(depsgraph.scene)
        if settings["subdivisions"] != _HOUDINI_HAIR_SUBDIVISIONS:
            diagnostics.append(
                dict(
                    code="blend.hair_settings",
                    severity="info",
                    source=depsgraph.scene.name_full,
                    message="Curve Subdivisions is "
                    + str(settings["subdivisions"])
                    + " in the Blender scene. It is a whole-scene setting: Houdini renders use the ROP's"
                    " Curve Subdivisions (Rendering > Geometry > Points and Curves, default "
                    + str(_HOUDINI_HAIR_SUBDIVISIONS)
                    + "); set it to "
                    + str(settings["subdivisions"])
                    + " to match. It refines Rounded Ribbons only",
                )
            )
    if invisible:
        diagnostics.append(
            dict(
                code="blend.invisible_object",
                severity="info",
                source=invisible[0],
                message="Not rendered, as in Blender: no ray visibility left (all Visibility > Ray switches off,"
                " or only Camera on in an indirect-only collection): "
                + ", ".join(invisible[:20])
                + (" ..." if len(invisible) > 20 else ""),
            )
        )
    if not instances and not (convert is not None and convert.objects):
        unsupported = "".join(
            "; skipped " + _UNSUPPORTED_LINK_TYPES.get(kind, kind) + ": " + ", ".join(names)
            for kind, names in skipped.items()
        )
        raise ValueError(
            "Blender selection contains no render-visible evaluated mesh geometry: " + request["selector"] + unsupported
        )
    # Geometry never fails for a material: an unexportable one is recorded with
    # its reason and rendered with a fallback in Houdini (blender_assets).
    if convert is not None and request.get("material_export") == "names":
        material_result = _material_names(materials)
    else:
        material_result = material_module.export_materials(
            list(materials.values()), output, tolerant=True, textures=request.get("material_textures", "copy")
        )
    if convert is not None:
        # The converter re-exports linked-library materials from their own file.
        by_key = {record["key"]: record for record in material_result["materials"]}
        for key, material in materials.items():
            library = getattr(material, "library", None) if material is not None else None
            if key in by_key:
                by_key[key].setdefault("local_name", material.name if material is not None else None)
                by_key[key]["library"] = (
                    str(Path(bpy.path.abspath(library.filepath, library=library.parent)).resolve()) if library else ""
                )
                by_key[key].setdefault("default", material is None)
    diagnostics.extend(material_result.get("diagnostics", []))
    manifest = dict(
        schema_version=SCHEMA_VERSION,
        source=str(Path(bpy.data.filepath).resolve()),
        selector_type=request["selector_type"].upper(),
        selector=request["selector"],
        frame=float(request["frame"]),
        scene=depsgraph.scene.name_full,
        blender_version=bpy.app.version_string,
        depsgraph_mode=depsgraph.mode,
        local_axis="blender-z-up",
        world_axis="houdini-y-up",
        coordinate_matrix=[value for row in _CONVERT for value in row],
        matrix_layout="row-major; column vectors; translation offsets 3,7,11",
        meshes=meshes,
        instances=instances,
        materials=material_result["materials"],
        diagnostics=diagnostics,
        color_management=material_result.get("color_management", {}),
        units=dict(
            system=depsgraph.scene.unit_settings.system, scale_length=float(depsgraph.scene.unit_settings.scale_length)
        ),
        dependencies=_dependencies(bpy, bpy.data.filepath, material_result),
    )
    if manifest["selector_type"] == "COLLECTION" and request.get("include_excluded_collections", False):
        # Bounds records carry this so they match only the +disabled reference.
        manifest["include_excluded_collections"] = True
    if hair_records:
        # Link hair records; instances name them by mesh_key with geometry_type "curves" or
        # "particle_hair" (blender_assets._native_curves; bounds and hulls include them).
        manifest["hair"] = hair_records
    if point_records:
        # Link point clouds; instances name them by mesh_key with geometry_type "points"
        # (blender_assets._native_points; bounds and hulls include them). Conversions keep
        # their own "pointclouds" records.
        manifest["points"] = point_records
    if convert is not None:
        manifest.update(converter=True, objects=convert.objects, curves=convert.curves, pointclouds=convert.pointclouds)
        if convert.trees:
            manifest["instance_trees"] = convert.trees
    if request.get("viewport_hulls", False):
        try:
            manifest["viewport_hulls"] = _helper("blend_viewport_hulls").build_viewport_hulls(
                manifest, arrays, request.get("max_hull_vertices", 64)
            )
        except Exception as error:
            manifest["viewport_hull_reason"] = str(error)
    return manifest, arrays


def _evaluate_loaded(request, output, session):
    import bpy

    source = Path(request["source"]).resolve()
    if not source.is_file() or source.suffix.lower() != ".blend":
        raise ValueError("Blender source must be an existing .blend file: " + str(source))
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    # "scene" (converter requests): the file's own current frame, read below.
    scene_frame = request.get("frame") == "scene"
    frame = 1.0 if scene_frame else float(request.get("frame", 1.0))
    if not math.isfinite(frame):
        raise ValueError("Blender source frame must be finite")
    request = dict(request, frame=frame)
    if not bpy.data.filepath or Path(bpy.data.filepath).resolve() != source:
        bpy.ops.wm.open_mainfile(filepath=str(source), load_ui=False, use_scripts=False)
    scene = bpy.data.scenes.get(request["scene"]) if request.get("scene") else bpy.context.scene
    if scene is None:
        raise ValueError("Blender scene does not exist: " + str(request.get("scene")))
    if bpy.context.window:
        bpy.context.window.scene = scene
    if scene_frame:
        # The session snapshot keeps the frame the file was saved on; earlier
        # requests of this batch may already have moved the scene.
        saved = next((entry for entry in session.scenes if _pointer(entry[0]) == _pointer(scene)), None)
        current, subframe = (saved[2], saved[3]) if saved else (scene.frame_current, scene.frame_subframe)
        frame = float(current) + float(subframe)
        request = dict(request, frame=frame)
    # Blender's render entry point requires a camera even for this geometry
    # engine. Prototype-only libraries often have none; no source is saved.
    if scene.camera is None:
        camera = bpy.data.objects.new("__CH_EXPORT_CAMERA__", bpy.data.cameras.new("__CH_EXPORT_CAMERA__"))
        scene.collection.objects.link(camera)
        scene.camera = camera
    selected = session.select(request, scene)
    scene.frame_set(math.floor(frame), subframe=frame - math.floor(frame))
    state = {}
    subdivision = _helper("blend_subdivision")
    native, diagnostics = {}, []
    for skipped in getattr(session, "selection_skipped", ()):
        diagnostics.append(
            dict(
                code="blend.excluded_collection",
                severity="info",
                source=skipped["name"],
                message="Child collection of "
                + skipped["parent"]
                + " is excluded in view layer "
                + skipped["view_layer"]
                + " of scene "
                + skipped["scene"]
                + " in the source file",
            )
        )
    # Include nested collection instances without modifying their transforms.
    objects, pending = {}, [obj for obj in bpy.data.objects if _identity(obj) in selected]
    while pending:
        obj = pending.pop()
        identity = _identity(obj)
        if identity in objects:
            continue
        objects[identity] = obj
        if obj.instance_type == "COLLECTION" and obj.instance_collection:
            pending.extend(obj.instance_collection.all_objects)
    users = bpy.data.user_map()
    for identity, obj in objects.items():
        modifier, config, reason = subdivision.settings(obj, scene)
        if modifier is None:
            continue
        # Disabling a modifier on a consumed object could change Boolean/GN/
        # driver geometry elsewhere in the selection. Keep the original graph
        # whenever another datablock consumes this object's evaluated state.
        if subdivision.has_geometry_consumers(bpy, obj, users):
            reason = "Other datablocks consume this object; preserving its evaluated dependency graph"
        if request.get("native_subdivision", True) and reason is None:
            native[identity] = (modifier, config)
        elif reason:
            diagnostics.append(
                dict(code="blend.subdivision_evaluated", severity="info", source=obj.name_full, message=reason)
            )

    def render(engine, depsgraph):
        try:
            state["result"] = _export_depsgraph(
                bpy, depsgraph, request, output, selected, subdivision, native, diagnostics
            )
            result = engine.begin_result(0, 0, 1, 1)
            result.layers[0].passes["Combined"].rect = [[0.0, 0.0, 0.0, 1.0]]
            engine.end_result(result)
        except subdivision.EvaluatedFallback as error:
            state["fallback"] = error
        except BaseException:
            state["error"] = traceback.format_exc()

    session.render = render
    changed = []
    try:
        for modifier, config in native.values():
            changed.append((modifier, modifier.show_render))
            modifier.show_render = False
        scene.render.engine = session.engine.bl_idname
        while True:
            state.clear()
            bpy.ops.render.render(scene=scene.name, write_still=False)
            fallback = state.get("fallback")
            if fallback is None:
                break
            modifier, config = native.pop(fallback.identity)
            modifier.show_render = True
            diagnostics.append(
                dict(
                    code="blend.subdivision_evaluated",
                    severity="info",
                    source=fallback.identity[1],
                    message=fallback.reason,
                )
            )
        if state.get("error"):
            raise RuntimeError(state["error"])
        if "result" not in state:
            raise RuntimeError("Blender did not invoke the background export engine")
        return state["result"]
    finally:
        for modifier, show_render in changed:
            modifier.show_render = show_render
        session.render = None


class _EvaluationSession:
    """Own temporary render state for one source's batch, including failures."""

    def __init__(self):
        import bpy

        self.bpy = bpy
        self.objects = {obj.as_pointer() for obj in bpy.data.objects}
        self.cameras = {camera.as_pointer() for camera in bpy.data.cameras}
        self.scenes = [
            (
                scene,
                scene.camera,
                scene.frame_current,
                scene.frame_subframe,
                scene.render.engine,
                {obj.as_pointer() for obj in scene.collection.objects},
            )
            for scene in bpy.data.scenes
        ]
        self.window = bpy.context.window
        self.window_scene = self.window.scene if self.window else None
        self.selection_key = None
        self.selection = None
        self.selection_links = []
        self.selection_objects = []
        self.selection_collections = []
        self.selection_skipped = []
        self.render = None
        owner = self

        class CHPrototypeExportEngine(bpy.types.RenderEngine):
            bl_idname = "CH_PROTOTYPE_BUNDLE_EXPORT"
            bl_label = "Cycles Houdini Prototype Export"
            bl_use_preview = False

            def render(self, depsgraph):
                owner.render(self, depsgraph)

        self.engine = CHPrototypeExportEngine
        bpy.utils.register_class(self.engine)

    def _release_collections(self):
        # Temporary filtered collections only link source data; removing them
        # leaves the loaded file's collections and objects as they were.
        for collection in self.selection_collections:
            self.bpy.data.collections.remove(collection)
        self.selection_collections = []

    def select(self, request, scene):
        key = (
            scene.as_pointer(),
            request["selector_type"].upper(),
            request["selector"],
            bool(request.get("include_excluded_collections", False)),
        )
        if key != self.selection_key:
            for linked_scene, obj in self.selection_links:
                linked_scene.collection.objects.unlink(obj)
            for obj in self.selection_objects:
                self.bpy.data.objects.remove(obj, do_unlink=True)
            self._release_collections()
            self.selection_links = []
            self.selection_objects = []
            self.selection_skipped = []
            self.selection_key = None
            objects = {obj.as_pointer() for obj in self.bpy.data.objects}
            collections = {collection.as_pointer() for collection in self.bpy.data.collections}
            linked = {obj.as_pointer() for obj in scene.collection.objects}
            skipped = []
            try:
                selected = _select(self.bpy, request, scene, skipped)
            finally:
                self.selection_objects = [obj for obj in self.bpy.data.objects if obj.as_pointer() not in objects]
                self.selection_collections = [
                    collection for collection in self.bpy.data.collections if collection.as_pointer() not in collections
                ]
                self.selection_links = [
                    (scene, obj)
                    for obj in scene.collection.objects
                    if obj.as_pointer() not in linked and obj.as_pointer() in objects
                ]
            self.selection, self.selection_key, self.selection_skipped = selected, key, skipped
        return self.selection

    def close(self):
        bpy = self.bpy
        for scene, camera, frame, subframe, engine, linked in self.scenes:
            scene.camera = camera
            scene.render.engine = engine
            for obj in list(scene.collection.objects):
                if obj.as_pointer() not in linked:
                    scene.collection.objects.unlink(obj)
            scene.frame_set(frame, subframe=subframe)
        for obj in list(bpy.data.objects):
            if obj.as_pointer() not in self.objects:
                bpy.data.objects.remove(obj, do_unlink=True)
        self._release_collections()
        for camera in list(bpy.data.cameras):
            if camera.as_pointer() not in self.cameras and camera.users == 0:
                bpy.data.cameras.remove(camera)
        if self.window and self.window_scene:
            self.window.scene = self.window_scene
        bpy.utils.unregister_class(self.engine)


def iter_evaluate_many(requests, texture_directory):
    """Evaluate one source's frames with one render engine and scene snapshot.

    Every frame still enters Blender's render pipeline and receives a RENDER
    depsgraph. Per-request errors preserve ordering and do not discard other
    frames. Temporary selections are reused only while their identity matches.
    """
    import bpy

    requests = list(requests)
    if not requests:
        return
    directories = (
        list(texture_directory) if isinstance(texture_directory, (list, tuple)) else [texture_directory] * len(requests)
    )
    if len(directories) != len(requests):
        raise ValueError("Blender texture directories must match the request batch")
    source = Path(requests[0]["source"]).resolve()
    if not source.is_file() or source.suffix.lower() != ".blend":
        raise ValueError("Blender source must be an existing .blend file: " + str(source))
    if any(Path(request["source"]).resolve() != source for request in requests):
        raise ValueError("Blender evaluation batch must contain one source")
    if not bpy.data.filepath or Path(bpy.data.filepath).resolve() != source:
        bpy.ops.wm.open_mainfile(filepath=str(source), load_ui=False, use_scripts=False)
    session = _EvaluationSession()
    try:
        for request, directory in zip(requests, directories):
            try:
                result = _evaluate_loaded(request, directory, session)
            except Exception as error:
                result = error
            yield result
            # Release this frame before evaluating the next one. Production
            # streams it immediately rather than accumulating a source's arrays.
            del result
    finally:
        session.close()


def evaluate_many(requests, texture_directory):
    """List-returning convenience API; workers stream iter_evaluate_many()."""
    return list(iter_evaluate_many(requests, texture_directory))


def evaluate(request, texture_directory):
    """Evaluate one request, preserving the standalone export API."""
    result = evaluate_many([request], texture_directory)[0]
    if isinstance(result, Exception):
        raise result
    return result


def export(request, output):
    """Legacy standalone bundle writer; production uses evaluate() and pipes."""
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    (output / "manifest.json").unlink(missing_ok=True)
    manifest, arrays = evaluate(request, output)
    manifest["geometry_file"] = "geometry.npz"
    with (output / "geometry.npz").open("wb") as stream:
        np.savez(stream, **arrays)
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else sys.argv[1:])
    args.output.mkdir(parents=True, exist_ok=True)
    try:
        manifest = export(json.loads(args.request.read_text(encoding="utf-8-sig")), args.output)
    except BaseException:
        (args.output / "failure.json").write_text(
            json.dumps(dict(result="failed", error=traceback.format_exc()), indent=2), encoding="utf-8"
        )
        raise
    print(
        json.dumps(
            dict(
                result="passed",
                manifest=str((args.output / "manifest.json").resolve()),
                meshes=len(manifest["meshes"]),
                instances=len(manifest["instances"]),
            )
        )
    )


if __name__ == "__main__":
    main()
