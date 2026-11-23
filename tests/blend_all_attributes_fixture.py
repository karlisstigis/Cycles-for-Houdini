# SPDX-License-Identifier: Apache-2.0
"""A .blend that carries every attribute kind Blender 5.3 offers (Convert & Append audit).

Run by Blender or the bpy module:

    blender -b --factory-startup --python tests/blend_all_attributes_fixture.py -- OUT.blend OUT.json

Builds the collection "AllAttributes":
  - AttrMesh: two quads sharing an edge (6 points, 7 edges, 2 faces, 8 corners). One attribute
    per domain (POINT, EDGE, FACE, CORNER) and data type, named "<domain>_<type>" in lower case;
    two UV maps (UVMap, "UV Map.001"), colour attributes with odd names ("paint" is the render
    colour, "Col Ör.1"), vertex groups (one partial, one named "pscale", one empty), attributes
    that collide with Houdini names ("Cd", "name") or map to one ("id"), sharp_edge, and two
    material slots.
  - AttrCurves: two poly curves (3 and 4 points) without a radius, one attribute per domain
    (POINT, CURVE) and data type.
  - AttrPoints: five points without a radius, one POINT attribute per data type.
  - AttrInstances: a geometry-nodes object instancing a cube on three points with an
    INSTANCE-domain float attribute "inst_float".
  - RadiusCurves, RadiusPoints (collection "DefaultRadius"): straight poly curves and points
    without radius attributes and with an emission material, for the default-radius renders.

OUT.json holds Blender's own values (read back after writing) of every attribute at every
element, so a test can compare what arrives in Houdini against them.
"""

import json
import sys

DOMAINS = {"mesh": ("POINT", "EDGE", "FACE", "CORNER"), "curves": ("POINT", "CURVE"), "points": ("POINT",)}
TYPES = (
    "FLOAT",
    "INT",
    "INT8",
    "BOOLEAN",
    "FLOAT2",
    "FLOAT_VECTOR",
    "FLOAT_COLOR",
    "BYTE_COLOR",
    "QUATERNION",
    "FLOAT4X4",
    "STRING",
    "INT32_2D",
    "INT16_2D",
    "FLOAT4",
)
# data type -> (foreach property, width)
PROPERTIES = {
    "FLOAT": ("value", 1),
    "INT": ("value", 1),
    "INT8": ("value", 1),
    "BOOLEAN": ("value", 1),
    "FLOAT2": ("vector", 2),
    "FLOAT_VECTOR": ("vector", 3),
    "FLOAT_COLOR": ("color", 4),
    "BYTE_COLOR": ("color", 4),
    "QUATERNION": ("value", 4),
    "FLOAT4X4": ("value", 16),
    "INT32_2D": ("value", 2),
    "INT16_2D": ("value", 2),
    "FLOAT4": ("vector", 4),
}


def attribute_values(data_type, count, seed):
    """Distinct deterministic values per element (raw Blender layout, flat)."""
    import math

    values = []
    for index in range(count):
        k = index + 1 + seed
        if data_type == "FLOAT":
            values.append(k * 0.5 + 0.25)
        elif data_type == "INT":
            values.append(k * 3 - 7)
        elif data_type == "INT8":
            values.append(k - 4)
        elif data_type == "BOOLEAN":
            values.append(index % 2 == 0)
        elif data_type == "FLOAT2":
            values += [k * 0.1, -k * 0.2]
        elif data_type == "FLOAT_VECTOR":
            values += [k * 1.0, k + 0.5, -k * 0.25]
        elif data_type in ("FLOAT_COLOR", "BYTE_COLOR"):
            values += [min(k * 0.07, 1.0), 0.25, 0.5, 0.75 if index % 2 else 1.0]
        elif data_type == "QUATERNION":
            angle = 0.2 * k
            values += [math.cos(angle / 2), math.sin(angle / 2), 0.0, 0.0]  # w, x, y, z: about Blender X
        elif data_type == "FLOAT4X4":
            matrix = [1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, k * 1.0, k * 2.0, k * 3.0, 1.0]
            matrix[1] = 0.1 * k  # a shear, column-major (Blender's foreach order)
            values += matrix
        elif data_type in ("INT32_2D", "INT16_2D"):
            values += [k * 2, -k]
        elif data_type == "FLOAT4":
            values += [k * 1.0, k * 2.0, k * 3.0, k * 4.0]
        elif data_type == "STRING":
            values.append(["alpha", "beta", "gamma", "Ünicode ö"][index % 4] + str(k))
    return values


def fill(attribute, data_type, seed):
    count = len(attribute.data)
    values = attribute_values(data_type, count, seed)
    if data_type == "STRING":
        for item, value in zip(attribute.data, values, strict=True):
            item.value = value.encode("utf-8")
        return
    prop, _ = PROPERTIES[data_type]
    attribute.data.foreach_set(prop, values)


def read_back(attribute):
    """Blender's own values: [[...] per element] (strings as str)."""
    data_type = attribute.data_type
    if data_type == "STRING":
        return [
            bytes(item.value).decode("utf-8") if isinstance(item.value, bytes) else item.value
            for item in attribute.data
        ]
    prop, width = PROPERTIES[data_type]
    count = len(attribute.data)
    flat = [0.0] * (count * width)
    attribute.data.foreach_get(prop, flat)
    rows = [flat[index * width : (index + 1) * width] for index in range(count)]
    if data_type in ("INT", "INT8", "INT32_2D", "INT16_2D", "BOOLEAN"):
        rows = [[int(round(value)) for value in row] for row in rows]
    return rows


def describe(attributes, names):
    return {
        name: dict(
            domain=attributes[name].domain, data_type=attributes[name].data_type, values=read_back(attributes[name])
        )
        for name in names
    }


def emission(bpy, name, color):
    material = bpy.data.materials.new(name)
    material.use_nodes = True
    nodes, links = material.node_tree.nodes, material.node_tree.links
    nodes.clear()
    node = nodes.new("ShaderNodeEmission")
    node.inputs["Color"].default_value = color
    output = nodes.new("ShaderNodeOutputMaterial")
    links.new(node.outputs[0], output.inputs["Surface"])
    return material


def build(bpy):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    asset = bpy.data.collections.new("AllAttributes")
    scene.collection.children.link(asset)
    expected = {}

    # ---------------------------------------------------------------- mesh
    mesh = bpy.data.meshes.new("AttrMeshData")
    mesh.from_pydata(
        [(0, 0, 0), (1, 0, 0), (2, 0, 0), (0, 1, 0), (1, 1, 0), (2, 1, 0)], [], [(0, 1, 4, 3), (1, 2, 5, 4)]
    )
    mesh.update()
    names = []
    for seed, domain in enumerate(DOMAINS["mesh"]):
        for data_type in TYPES:
            name = (domain + "_" + data_type).lower()
            fill(mesh.attributes.new(name, data_type, domain), data_type, seed * 10)
            names.append(name)
    for name, data_type, domain in (("Cd", "FLOAT_VECTOR", "POINT"), ("name", "INT", "FACE"), ("id", "INT", "POINT")):
        fill(mesh.attributes.new(name, data_type, domain), data_type, 50)
        names.append(name)
    sharp = mesh.attributes.new("sharp_edge", "BOOLEAN", "EDGE")
    sharp.data.foreach_set("value", [True, False, False, False, False, False, False])
    uv = mesh.uv_layers.new(name="UVMap")
    uv_values = [value for index in range(len(mesh.loops)) for value in (index * 0.125, 1.0 - index * 0.125)]
    uv.uv.foreach_set("vector", uv_values)
    other = mesh.uv_layers.new(name="UV Map.001")
    other.uv.foreach_set("vector", [value * 0.5 for value in uv_values])
    mesh.uv_layers.active_index = 0
    for layer in mesh.uv_layers:
        layer.active_render = layer.name == "UVMap"
    paint = mesh.color_attributes.new("paint", "BYTE_COLOR", "CORNER")
    fill(paint, "BYTE_COLOR", 70)
    odd = mesh.color_attributes.new("Col Ör.1", "FLOAT_COLOR", "POINT")
    fill(odd, "FLOAT_COLOR", 80)
    mesh.color_attributes.render_color_index = list(mesh.color_attributes).index(mesh.color_attributes["paint"])
    mesh.color_attributes.active_color_index = mesh.color_attributes.render_color_index
    names += ["paint", "Col Ör.1"]
    for material in (emission(bpy, "Red Paint", (1, 0, 0, 1)), emission(bpy, "Blue", (0, 0, 1, 1))):
        mesh.materials.append(material)
    mesh.polygons[1].material_index = 1
    obj = bpy.data.objects.new("AttrMesh", mesh)
    asset.objects.link(obj)
    weights = obj.vertex_groups.new(name="weights")
    weights.add([0, 2], 0.75, "REPLACE")
    clash = obj.vertex_groups.new(name="pscale")
    clash.add([1], 0.5, "REPLACE")
    obj.vertex_groups.new(name="empty group")
    expected["AttrMesh"] = dict(
        points=len(mesh.vertices),
        edges=[list(edge.vertices) for edge in mesh.edges],
        faces=[list(polygon.vertices) for polygon in mesh.polygons],
        loops=[[polygon.loop_start, polygon.loop_total] for polygon in mesh.polygons],
        attributes=describe(mesh.attributes, names),
        uv=dict(UVMap=uv_values, other=[value * 0.5 for value in uv_values]),
        vertex_groups={"weights": [0.75, 0, 0.75, 0, 0, 0], "pscale": [0, 0.5, 0, 0, 0, 0], "empty group": [0.0] * 6},
        materials=["Red Paint", "Blue"],
        render_color="paint",
    )

    # ---------------------------------------------------------------- curves (no radius)
    curves = bpy.data.hair_curves.new("AttrCurvesData")
    curves.add_curves([3, 4])
    positions = [0, 0, 0, 0, 0, 1, 0, 0, 2, 1, 0, 0, 1, 0, 1, 1, 0, 2, 1, 0, 3]
    curves.attributes["position"].data.foreach_set("vector", positions)
    curves.set_types(type="POLY")
    names = []
    for seed, domain in enumerate(DOMAINS["curves"]):
        for data_type in TYPES:
            name = (domain + "_" + data_type).lower()
            fill(curves.attributes.new(name, data_type, domain), data_type, seed * 10)
            names.append(name)
    asset.objects.link(bpy.data.objects.new("AttrCurves", curves))
    expected["AttrCurves"] = dict(
        points=len(curves.points),
        curves=len(curves.curves),
        attributes=describe(curves.attributes, names),
        builtin=describe(
            curves.attributes,
            sorted(a.name for a in curves.attributes if not a.name.startswith(".") and a.name not in names),
        ),
    )

    # ---------------------------------------------------------------- point cloud (no radius)
    cloud = bpy.data.pointclouds.new("AttrPointsData")
    cloud.resize(5)
    cloud.attributes["position"].data.foreach_set("vector", [v for i in range(5) for v in (i * 0.5, 0.0, 1.0)])
    names = []
    for data_type in TYPES:
        name = ("POINT_" + data_type).lower()
        fill(cloud.attributes.new(name, data_type, "POINT"), data_type, 0)
        names.append(name)
    asset.objects.link(bpy.data.objects.new("AttrPoints", cloud))
    expected["AttrPoints"] = dict(points=5, attributes=describe(cloud.attributes, names))

    # ---------------------------------------------------------------- geometry-nodes instances
    base = bpy.data.meshes.new("AttrInstancesBase")
    holder = bpy.data.objects.new("AttrInstances", base)
    holder.location = (0, 5, 0)
    asset.objects.link(holder)
    tree = bpy.data.node_groups.new("AttrInstancesTree", "GeometryNodeTree")
    tree.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    nodes, links = tree.nodes, tree.links
    line = nodes.new("GeometryNodeMeshLine")
    line.inputs["Count"].default_value = 3
    points = nodes.new("GeometryNodeMeshToPoints")
    cube = nodes.new("GeometryNodeMeshCube")
    cube.inputs["Size"].default_value = (0.2, 0.2, 0.2)
    place = nodes.new("GeometryNodeInstanceOnPoints")
    store = nodes.new("GeometryNodeStoreNamedAttribute")
    store.data_type = "FLOAT"
    store.domain = "INSTANCE"
    store.inputs["Name"].default_value = "inst_float"
    index = nodes.new("GeometryNodeInputIndex")
    output = nodes.new("NodeGroupOutput")
    links.new(line.outputs[0], points.inputs["Mesh"])
    links.new(points.outputs[0], place.inputs["Points"])
    links.new(cube.outputs[0], place.inputs["Instance"])
    links.new(place.outputs[0], store.inputs["Geometry"])
    links.new(index.outputs[0], store.inputs["Value"])
    links.new(store.outputs[0], output.inputs[0])
    holder.modifiers.new("GN", "NODES").node_group = tree
    expected["AttrInstances"] = dict(instance_attributes=["inst_float"])

    # ---------------------------------------------------------------- default radius renders
    radius = bpy.data.collections.new("DefaultRadius")
    scene.collection.children.link(radius)
    strands = bpy.data.hair_curves.new("RadiusCurvesData")
    strands.add_curves([2] * 5)
    strand_positions = []
    for index in range(5):
        x = -0.08 + index * 0.04
        strand_positions += [x, -0.09, 0.0, x, 0.0, 0.0]
    strands.attributes["position"].data.foreach_set("vector", strand_positions)
    strands.set_types(type="POLY")
    strands.materials.append(emission(bpy, "StrandGlow", (1, 0.6, 0.1, 1)))
    radius.objects.link(bpy.data.objects.new("RadiusCurves", strands))
    dots = bpy.data.pointclouds.new("RadiusPointsData")
    dots.resize(5)
    dots.attributes["position"].data.foreach_set("vector", [v for i in range(5) for v in (-0.08 + i * 0.04, 0.05, 0.0)])
    dots.materials.append(emission(bpy, "DotGlow", (0.1, 0.6, 1, 1)))
    radius.objects.link(bpy.data.objects.new("RadiusPoints", dots))
    return expected


# ------------------------------------------------------------------ expectations (pure Python)
#
# What a Convert & Append import must produce from OUT.json (docs/user/blender-import.md,
# "Attribute mapping"). Shared by tests/pytest_cases/test_blend_all_attributes.py (the
# converter's geometry) and tests/houdini_blend_converted_attributes_smoke.py (Houdini).

INT_TYPES = ("INT", "INT8", "BOOLEAN", "INT32_2D", "INT16_2D")
QUALIFIERS = {"QUATERNION": "Quaternion", "FLOAT4X4": "Matrix"}
OWNERS = {"POINT": "point", "FACE": "prim", "CURVE": "prim", "CORNER": "vertex"}
# Blender Z-up -> Houdini Y-up (x, y, z) -> (x, z, -y), as blend_convert._CONVERT.
_C = ((1.0, 0.0, 0.0, 0.0), (0.0, 0.0, 1.0, 0.0), (0.0, -1.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0))
_C_INV = ((1.0, 0.0, 0.0, 0.0), (0.0, 0.0, -1.0, 0.0), (0.0, 1.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0))


def _turn(x, y, z):
    return [x, z, -y]


def _multiply(a, b):
    return [[sum(a[r][k] * b[k][c] for k in range(4)) for c in range(4)] for r in range(4)]


def convert_value(data_type, row):
    """One Blender element (read_back row) as Houdini holds it."""
    if data_type == "FLOAT_VECTOR":
        return _turn(*row)
    if data_type == "QUATERNION":  # w, x, y, z -> x, y, z, w with the axis part turned
        return _turn(*row[1:4]) + [row[0]]
    if data_type == "FLOAT4X4":  # column-major M -> Houdini row-major of (C M C^-1)^T
        matrix = [[row[c * 4 + r] for c in range(4)] for r in range(4)]
        turned = _multiply(_multiply(_C, matrix), _C_INV)
        return [turned[c][r] for r in range(4) for c in range(4)]
    return list(row) if isinstance(row, list) else [row]


def houdini_corner_order(loops):
    """Blender loop index per Houdini vertex: each polygon reversed, keeping its first corner."""
    order = []
    for start, total in loops:
        corners = list(range(start, start + total))
        order += [corners[0]] + corners[:0:-1]
    return order


def _row(obj, name, owner, kind, size, values, qualifier=None):
    return dict(object=obj, name=name, owner=owner, kind=kind, size=size, values=values, qualifier=qualifier)


def expectations(expected):
    """(rows, absent, groups, not_imported): every attribute Houdini must hold for the fixture,
    with its values in Houdini element order; names that must not exist per object; prim and edge
    groups; and per object {reason: {names}} the import log must name as not imported."""
    rows, absent = [], {}
    mesh = expected["AttrMesh"]
    corner_order = houdini_corner_order(mesh["loops"])
    renamed = {"Cd": "blend_Cd", "name": "blend_name", "Col Ör.1": "Col_r_1", "paint": None}
    for name, field in mesh["attributes"].items():
        domain, data_type, values = field["domain"], field["data_type"], field["values"]
        if domain == "EDGE":
            absent.setdefault("AttrMesh", []).append(name)
            continue
        houdini = renamed.get(name, name)
        if houdini is None:  # the render colour: Cd and Alpha, not under its own name
            absent.setdefault("AttrMesh", []).append(name)
            rows.append(_row("AttrMesh", "Cd", "vertex", "float", 3, [values[i][:3] for i in corner_order]))
            rows.append(_row("AttrMesh", "Alpha", "vertex", "float", 1, [[values[i][3]] for i in corner_order]))
            continue
        owner = OWNERS[domain]
        if owner == "vertex":
            values = [values[i] for i in corner_order]
        if domain == "CORNER" and data_type == "FLOAT2":
            # A Face Corner 2D vector is a UV map in Blender: a vertex float3 UV set.
            rows.append(_row("AttrMesh", houdini, "vertex", "float", 3, [list(v) + [0.0] for v in values]))
            continue
        rows.append(_typed_row("AttrMesh", houdini, owner, data_type, values))
    uv = mesh["uv"]
    for name, flat in (("uv", uv["UVMap"]), ("UV_Map_001", uv["other"])):
        pairs = [flat[i * 2 : i * 2 + 2] + [0.0] for i in range(len(flat) // 2)]
        rows.append(_row("AttrMesh", name, "vertex", "float", 3, [pairs[i] for i in corner_order]))
    for name, houdini in (("weights", "weights"), ("pscale", "blend_pscale"), ("empty group", "empty_group")):
        rows.append(_row("AttrMesh", houdini, "point", "float", 1, [[v] for v in mesh["vertex_groups"][name]]))
    rows.append(_row("AttrMesh", "blender_material", "prim", "string", 1, [[n] for n in mesh["materials"]]))
    absent["AttrMesh"] += ["sharp_edge", "UVMap", "pscale", "paint"]
    groups = {
        "AttrMesh": dict(prims={"mat_Red_Paint": [0], "mat_Blue": [1]}, edges={"sharp_edges": [mesh["edges"][0]]})
    }
    for obj, width_name, width in (("AttrCurves", "width", 0.01), ("AttrPoints", "pscale", 0.01)):
        data = expected[obj]
        for name, field in list(data["attributes"].items()) + list(data.get("builtin", {}).items()):
            if name == "position":
                continue
            rows.append(_typed_row(obj, name, OWNERS[field["domain"]], field["data_type"], field["values"]))
        rows.append(_row(obj, width_name, "point", "float", 1, [[width]] * data["points"]))
        absent[obj] = ["radius", "position"]
    absent["AttrInstances"] = list(expected["AttrInstances"]["instance_attributes"])
    edge_names = [name for name, field in mesh["attributes"].items() if field["domain"] == "EDGE"]
    not_imported = {
        "AttrInstances": {"instance attributes are not carried onto packed primitives yet": {"inst_float"}},
        "AttrMesh": {"Houdini has no edge attributes": set(edge_names)},
    }
    return rows, absent, groups, not_imported


def parse_line(text):
    """(object, {reason: {names}}) of an import-log line "Obj: attributes not imported: a, b
    (reason); c (other reason)", or None for any other line."""
    owner, marker, rest = text.partition(": attributes not imported: ")
    if not marker:
        return None
    reasons = {}
    for part in rest.split("); "):
        names, _, reason = part.rstrip(")").rpartition(" (")
        reasons[reason] = {name.strip() for name in names.split(",")}
    return owner, reasons


def _typed_row(obj, name, owner, data_type, values):
    if data_type == "STRING":
        return _row(obj, name, owner, "string", 1, [[value] for value in values])
    converted = [convert_value(data_type, value) for value in values]
    kind = "int" if data_type in INT_TYPES else "float"
    return _row(obj, name, owner, kind, len(converted[0]), converted, QUALIFIERS.get(data_type))


def compare(rows, lookup, tolerance=1e-4):
    """Failures (strings) of rows against lookup(object, name) -> (owner, kind, size, qualifier,
    values) or None."""
    failures = []
    for row in rows:
        found = lookup(row["object"], row["name"])
        label = row["object"] + "." + row["name"]
        if found is None:
            failures.append(label + ": missing")
            continue
        owner, kind, size, qualifier, values = found
        if (owner, kind, size) != (row["owner"], row["kind"], row["size"]):
            failures.append(f"{label}: {owner} {kind}[{size}], expected {row['owner']} {row['kind']}[{row['size']}]")
            continue
        if row["qualifier"] and qualifier != row["qualifier"]:
            failures.append(f"{label}: type info {qualifier!r}, expected {row['qualifier']!r}")
        if len(values) != len(row["values"]):
            failures.append(f"{label}: {len(values)} elements, expected {len(row['values'])}")
            continue
        for index, (got, want) in enumerate(zip(values, row["values"], strict=True)):
            got = list(got) if isinstance(got, (list, tuple)) else [got]
            if kind == "string":
                ok = got == want
            else:
                ok = len(got) == len(want) and all(abs(float(a) - float(b)) <= tolerance for a, b in zip(got, want))
            if not ok:
                failures.append(f"{label}[{index}]: {got}, expected {want}")
                break
    return failures


def main():
    import bpy

    arguments = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else sys.argv[1:]
    blend, report = arguments[:2]
    expected = build(bpy)
    expected["blender_version"] = bpy.app.version_string
    bpy.ops.wm.save_as_mainfile(filepath=blend)
    with open(report, "w", encoding="utf-8") as stream:
        json.dump(expected, stream, ensure_ascii=False)
    print("ALL_ATTRIBUTES_DONE")


if __name__ == "__main__":
    main()
