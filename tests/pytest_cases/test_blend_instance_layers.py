# SPDX-License-Identifier: Apache-2.0
"""Object and Instancer attributes through deeper geometry-nodes instancing, compared with
Blender 5.3's own Cycles render (docs/dev/provenance/blender-object-values.md, section 5.4; the
experiments behind each rule are recorded in its section 5.5).

One .blend holds several groups of planes; every plane emits the value of one Attribute node
(type Instancer or Object), and the colour Blender renders at each exported instance must
equal the value blend_export exports for it. Rules pinned here:
- only the four innermost instancing layers are searched (six nested layers);
- a level's own non-instance components shift the instance numbering, except the component
  of the root object's own kind (mesh, curves, legacy curve and point cloud roots, and a level
  inside the instances);
- an object instanced by geometry nodes continues with its own instance layers, innermost first;
- members of an instanced collection continue the same way;
- custom property conversion: a 2-vector fills blue 0 and alpha 1, an unusable custom property
  hides a built-in property of the same name, a mesh property is found when the object has none.
"""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[2]

SCRIPT = r"""
import json, sys, tempfile
# argv[1]: <checkout>/houdini/python3.13libs; the Blender component is <checkout>/blender.
sys.path[:0] = [sys.argv[1], sys.argv[1] + "/../../blender"]
import bpy
from cycles_blender import blend_export

out = sys.argv[2]
bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene
NAMES = ["d1", "d2", "d3", "d4", "d5", "d6", "ca", "ia", "sa", "ib"]
OBJECT_NAMES = ["color", "pair", "meshprop", "flags"]
material = bpy.data.materials.new("Emit")
material.use_nodes = True
tree = material.node_tree
tree.nodes.clear()
emission = tree.nodes.new("ShaderNodeEmission")
tree.links.new(emission.outputs[0], tree.nodes.new("ShaderNodeOutputMaterial").inputs[0])
attribute_nodes = {}
for kind, names in (("INSTANCER", NAMES), ("OBJECT", OBJECT_NAMES)):
    for name in names:
        node = tree.nodes.new("ShaderNodeAttribute")
        node.attribute_type, node.attribute_name = kind, name
        attribute_nodes[(kind, name)] = node
asset = bpy.data.collections.new("Asset")
scene.collection.children.link(asset)
spare = bpy.data.collections.new("Spare")
scene.collection.children.link(spare)


def gn_object(name, kind="MESH", collection=asset, location=(0, 0, 0)):
    if kind == "MESH":
        data = bpy.data.meshes.new(name)
        data.from_pydata([(0, 0, 0)], [], [])
    elif kind == "CURVES":
        data = bpy.data.hair_curves.new(name)
    elif kind == "CURVE":
        data = bpy.data.curves.new(name, "CURVE")
        data.splines.new("POLY").points.add(1)
    elif kind == "POINTCLOUD":
        data = bpy.data.pointclouds.new(name)
    obj = bpy.data.objects.new(name, data)
    obj.location = location
    collection.objects.link(obj)
    group = bpy.data.node_groups.new(name, "GeometryNodeTree")
    group.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    obj.modifiers.new("GN", "NODES").node_group = group
    return obj, group.nodes, group.links


def plane(n, links):
    grid = n.new("GeometryNodeMeshGrid")
    grid.inputs["Size X"].default_value = grid.inputs["Size Y"].default_value = 0.8
    grid.inputs["Vertices X"].default_value = grid.inputs["Vertices Y"].default_value = 2
    assign = n.new("GeometryNodeSetMaterial")
    assign.inputs["Material"].default_value = material
    links.new(grid.outputs["Mesh"], assign.inputs["Geometry"])
    return assign.outputs["Geometry"]


def instances(n, links, count, offset, instance, name=None, add=(0.05, 0.05, 0.5)):
    line = n.new("GeometryNodeMeshLine")
    line.inputs["Count"].default_value = count
    line.inputs["Offset"].default_value = offset
    points = n.new("GeometryNodeMeshToPoints")
    links.new(line.outputs["Mesh"], points.inputs["Mesh"])
    place = n.new("GeometryNodeInstanceOnPoints")
    links.new(points.outputs["Points"], place.inputs["Points"])
    links.new(instance, place.inputs["Instance"])
    if name is None:
        return place.outputs["Instances"]
    store = n.new("GeometryNodeStoreNamedAttribute")
    store.data_type, store.domain = "FLOAT_COLOR", "INSTANCE"
    store.inputs["Name"].default_value = name
    value = n.new("ShaderNodeVectorMath")
    value.operation = "MULTIPLY_ADD"
    links.new(n.new("GeometryNodeInputPosition").outputs["Position"], value.inputs[0])
    value.inputs[1].default_value = (0.1, 0.1, 0.1)
    value.inputs[2].default_value = add
    links.new(place.outputs["Instances"], store.inputs["Geometry"])
    links.new(value.outputs["Vector"], store.inputs["Value"])
    return store.outputs["Geometry"]


def extra(n, links, kind):
    socket = {"mesh": lambda: n.new("GeometryNodeMeshCube").outputs["Mesh"],
              "curves": lambda: n.new("GeometryNodeCurvePrimitiveLine").outputs["Curve"],
              "pointcloud": lambda: n.new("GeometryNodePoints").outputs["Points"]}[kind]()
    move = n.new("GeometryNodeTransform")
    move.inputs["Translation"].default_value = (100.0, 0.0, 0.0)  # out of view
    links.new(socket, move.inputs["Geometry"])
    return move.outputs["Geometry"]


def join(n, links, sockets):
    node = n.new("GeometryNodeJoinGeometry")
    for socket in sockets:
        links.new(socket, node.inputs["Geometry"])
    return node.outputs["Geometry"]


def finish(n, links, socket):
    links.new(socket, n.new("NodeGroupOutput").inputs["Geometry"])


# Six nested layers on an 8 x 8 grid at the origin; layer k (1 = innermost) stores "d<k>".
obj, n, links = gn_object("Nest")
socket = plane(n, links)
for k in range(1, 7):
    step = float(2 ** ((k - 1) // 2))
    socket = instances(n, links, 2, (step, 0, 0) if k % 2 else (0, step, 0), socket, "d%d" % k, (0.05, 0.05, 0.1 * k))
finish(n, links, socket)

# Own components: rows at y = 10 ... 13.
for row, (name, kind, extras, inner) in enumerate((
    ("MeshPoints", "MESH", ["mesh", "pointcloud"], None),
    ("CurvesMesh", "CURVES", ["mesh", "curves"], None),
    ("LegacyCurve", "CURVE", ["mesh", "curves"], None),
    ("InnerMeshPoints", "MESH", [], ["mesh", "pointcloud"]),
)):
    obj, n, links = gn_object(name, kind, location=(0, 10 + row, 0))
    if inner is None:
        socket = instances(n, links, 4, (1, 0, 0), plane(n, links), "ca", (0.05, 0.05 * (row + 1), 0.6))
    else:
        socket = instances(n, links, 2, (1, 0, 0), plane(n, links), "ca", (0.05, 0.05 * (row + 1), 0.6))
        socket = join(n, links, [socket] + [extra(n, links, k) for k in inner])
        socket = instances(n, links, 2, (2, 0, 0), socket)
    finish(n, links, join(n, links, [socket] + [extra(n, links, k) for k in extras]))

# An object with its own instances, instanced by another object (x 0 ... 1, y 15 ... 19).
inner, n, links = gn_object("Inner", collection=spare)
socket = instances(n, links, 2, (1, 0, 0), plane(n, links), "ia", (0.05, 0.05, 0.6))
store = n.new("GeometryNodeStoreNamedAttribute")
store.data_type, store.domain = "FLOAT_COLOR", "INSTANCE"
store.inputs["Name"].default_value = "sa"
store.inputs["Value"].default_value = (0.2, 0.3, 0.4, 1.0)
links.new(socket, store.inputs["Geometry"])
finish(n, links, store.outputs["Geometry"])
outer, n, links = gn_object("Outer", location=(0, 15, 0))
info = n.new("GeometryNodeObjectInfo")
info.inputs["Object"].default_value = inner
info.inputs["As Instance"].default_value = True
socket = instances(n, links, 3, (0, 2, 0), info.outputs["Geometry"], "sa", (0.5, 0.05, 0.05))
finish(n, links, socket)

# A collection of three members (a plain mesh, two scatters) instanced twice (x 10 ... 14, y 15 ... 17).
members = bpy.data.collections.new("Members")
scene.collection.children.link(members)
plain = bpy.data.objects.new("Plain", bpy.data.meshes.new("Plain"))
plain.location = (100, 0, 0)
members.objects.link(plain)
for name, x, blue in (("B", 0, 0.4), ("C", 3, 0.8)):
    obj, n, links = gn_object(name, collection=members, location=(x, 0, 0))
    finish(n, links, instances(n, links, 2, (1, 0, 0), plane(n, links), "ib", (0.05, 0.05, blue)))
holder, n, links = gn_object("Holder", location=(10, 15, 0))
info = n.new("GeometryNodeCollectionInfo")
info.inputs["Collection"].default_value = members
finish(n, links, instances(n, links, 2, (0, 2, 0), info.outputs["Instances"]))

# Real objects for property conversion (x 10 ... 13, y 0).
for k, name in enumerate(("Hidden", "Pair", "MeshOnly", "Flags")):
    data = bpy.data.meshes.new(name)
    data.from_pydata([(-0.4, -0.4, 0), (0.4, -0.4, 0), (0.4, 0.4, 0), (-0.4, 0.4, 0)], [], [(0, 1, 2, 3)])
    data.materials.append(material)
    obj = bpy.data.objects.new(name, data)
    obj.location = (10 + k, 0, 0)
    obj.color = (0.9, 0.8, 0.7, 1.0)
    asset.objects.link(obj)
bpy.data.objects["Hidden"]["color"] = "not a colour"  # hides the built-in Object > Color
bpy.data.objects["Pair"]["pair"] = (0.3, 0.6)
bpy.data.objects["MeshOnly"].data["meshprop"] = 0.25
bpy.data.objects["Flags"]["flags"] = [True, False]  # a boolean array is not convertible
for layer in ("Spare", "Members"):
    bpy.context.view_layer.layer_collection.children[layer].exclude = True

camera = bpy.data.objects.new("Camera", bpy.data.cameras.new("Camera"))
camera.location = (7.5, 10.0, 50.0)
camera.data.type, camera.data.ortho_scale, camera.data.clip_end = "ORTHO", 22.0, 200.0
scene.collection.objects.link(camera)
scene.camera = camera
scene.render.engine = "CYCLES"
scene.cycles.device, scene.cycles.samples, scene.cycles.use_denoising = "CPU", 1, False
scene.cycles.filter_width = 0.01
scene.render.resolution_x = scene.render.resolution_y = 176
scene.render.image_settings.file_format = "OPEN_EXR"
scene.render.image_settings.color_depth = "32"
scene.view_settings.view_transform = "Standard"
path = out + "/instance_layers.blend"
bpy.ops.wm.save_as_mainfile(filepath=path)

renders = {}
for (kind, name), node in attribute_nodes.items():
    tree.links.new(node.outputs["Color"], emission.inputs["Color"])
    scene.render.filepath = out + "/%s_%s.exr" % (kind, name)
    bpy.ops.render.render(write_still=True, scene=scene.name)
    image = bpy.data.images.load(scene.render.filepath)
    renders[kind + ":" + name] = list(image.pixels[:])

# blend_export loads the file itself, so it runs after the renders.
# The nest is selected as an object: a collection selection adds the selector's own level, and
# Blender stops making instances past its nesting limit.
requests = [dict(source=path, selector_type="COLLECTION", selector="Asset", frame=1.0),
            dict(source=path, selector_type="OBJECT", selector="Nest", frame=1.0)]
with tempfile.TemporaryDirectory() as textures:
    results = blend_export.evaluate_many(requests, textures)


def pixel(pixels, x, y):
    column, row = int((x - (7.5 - 11.0)) / 22.0 * 176), int((y - (10.0 - 11.0)) / 22.0 * 176)
    offset = (row * 176 + column) * 4
    return pixels[offset : offset + 3]


rows = []
for selection, (manifest, arrays) in zip(("Asset", "Nest"), results):
    for instance in manifest["instances"]:
        if (instance["name"] == "Nest") != (selection == "Nest"):
            continue
        x, y = instance["blender_transform"][3], instance["blender_transform"][7]
        for attribute in instance["blender_object"].get("attributes", []):
            key = attribute["type"] + ":" + attribute["name"]
            rows.append(dict(name=instance["name"], x=x, y=y, key=key, exported=attribute["value"],
                             rendered=pixel(renders[key], x, y)))
print("RESULT:" + json.dumps(rows))
"""


@pytest.fixture(scope="module")
def rows(tmp_path_factory):
    if importlib.util.find_spec("bpy") is None:
        pytest.skip("bpy (Blender as a Python module) is not installed")
    folder = tmp_path_factory.mktemp("blend_instance_layers")
    script = folder / "make_render_export.py"
    script.write_text(SCRIPT, encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, "-I", str(script), str(ROOT / "houdini/python3.13libs"), str(folder)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=600,
        cwd=folder,
    )
    lines = [line for line in completed.stdout.splitlines() if line.startswith("RESULT:")]
    assert lines, completed.stdout[-4000:] + completed.stderr[-4000:]
    return json.loads(lines[-1][len("RESULT:") :])


def _select(rows, key, x_range, y_range):
    return [
        r
        for r in rows
        if r["key"] == key
        and x_range[0] - 0.5 <= r["x"] <= x_range[1] + 0.5
        and y_range[0] - 0.5 <= r["y"] <= y_range[1] + 0.5
    ]


def _agrees(row, nonzero=True):
    exported, rendered = np.asarray(row["exported"][:3]), np.asarray(row["rendered"])
    assert np.allclose(exported, rendered, atol=1e-5), row
    assert (np.abs(rendered).max() > 1e-6) == nonzero, row


@pytest.mark.blender
@pytest.mark.cpu
def test_instancer_attribute_searches_four_innermost_layers(rows):
    for k in range(1, 7):
        selected = _select(rows, "INSTANCER:d%d" % k, (0, 7), (0, 7))
        assert len(selected) == 64, k
        for row in selected:
            _agrees(row, nonzero=k <= 4)  # layers 5 and 6 are beyond the four innermost


@pytest.mark.blender
@pytest.mark.cpu
def test_own_components_shift_the_instance_numbering(rows):
    selected = _select(rows, "INSTANCER:ca", (0, 3), (10, 13))
    assert len(selected) == 16
    for row in selected:
        _agrees(row)


@pytest.mark.blender
@pytest.mark.cpu
def test_instanced_objects_and_collections_continue_with_their_own_instances(rows):
    inner = _select(rows, "INSTANCER:ia", (0, 1), (15, 19))
    shared = _select(rows, "INSTANCER:sa", (0, 1), (15, 19))
    members = _select(rows, "INSTANCER:ib", (10, 14), (15, 17))
    assert len(inner) == len(shared) == 6 and len(members) == 8
    for row in inner + shared + members:
        _agrees(row)
    for row in shared:  # the instanced object's own layer is the innermost one
        assert np.allclose(row["rendered"], [0.2, 0.3, 0.4], atol=1e-5), row


@pytest.mark.blender
@pytest.mark.cpu
def test_property_conversion_rules_match_blender(rows):
    expect = {
        ("Hidden", "OBJECT:color"): [0.0, 0.0, 0.0],
        ("Pair", "OBJECT:pair"): [0.3, 0.6, 0.0],
        ("MeshOnly", "OBJECT:meshprop"): [0.25, 0.25, 0.25],
        ("Flags", "OBJECT:flags"): [0.0, 0.0, 0.0],
        ("Pair", "OBJECT:color"): [0.9, 0.8, 0.7],
    }
    found = {(r["name"], r["key"]): r for r in rows if (r["name"], r["key"]) in expect}
    assert set(found) == set(expect)
    for key, value in expect.items():
        row = found[key]
        assert np.allclose(row["rendered"], value, atol=1e-5), row
        assert np.allclose(row["exported"][:3], value, atol=1e-5), row
    assert found[("Pair", "OBJECT:pair")]["exported"][3] == 1.0
