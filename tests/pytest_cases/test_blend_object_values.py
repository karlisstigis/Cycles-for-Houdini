# SPDX-License-Identifier: Apache-2.0
"""Link mode carries Blender's per-object values (task S2, blender-sync-audit items 3, 5, 6):
Object Info Random, ray visibility, holdout, shadow catcher, indirect-only, object colour, pass
index and Object/Instancer attributes, as Blender v5.3.0's Cycles translator syncs them.

The bpy case builds a .blend with Blender 5.3 (the PyPI bpy module), exports it with the same
render requests the worker sends, and compares with Blender's own numbers: the depsgraph's
per-instance random_id, and a Cycles render of Object Info > Random for real objects (whose
number Blender computes inside Cycles and does not expose).
"""

import importlib
import importlib.util
import json
import subprocess
import sys
import types
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
asset = bpy.data.collections.new("Asset")
scene.collection.children.link(asset)


def mesh_object(name, collection, location=(0, 0, 0)):
    data = bpy.data.meshes.new(name)
    data.from_pydata([(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)], [], [(0, 1, 2, 3)])
    obj = bpy.data.objects.new(name, data)
    obj.location = location
    collection.objects.link(obj)
    return obj


material = bpy.data.materials.new("PerObject")
material.use_nodes = True
nodes = material.node_tree.nodes
for kind, name in (("INSTANCER", "inst_color"), ("OBJECT", "tint"), ("INSTANCER", "tint"), ("OBJECT", "color"),
                   ("INSTANCER", "missing"), ("OBJECT", "pass_index"), ("INSTANCER", "flag")):
    node = nodes.new("ShaderNodeAttribute")
    node.attribute_type, node.attribute_name = kind, name

cube = mesh_object("Würfel ä", asset)
cube.color = (0.2, 0.4, 0.6, 0.8)
cube.pass_index = 7
cube["tint"] = (0.1, 0.2, 0.3)
cube["flag"] = True
cube.data.materials.append(material)
mesh_object("NoCamera", asset, (2, 0, 0)).visible_camera = False
ghost = mesh_object("Ghost", asset, (4, 0, 0))
for name in ("camera", "diffuse", "glossy", "transmission", "volume_scatter", "shadow", "raycast"):
    setattr(ghost, "visible_" + name, False)
mesh_object("Catcher", asset, (6, 0, 0)).is_shadow_catcher = True
mesh_object("HoldMe", asset, (8, 0, 0)).is_holdout = True
kids = bpy.data.collections.new("HoldoutKids")
asset.children.link(kids)
mesh_object("HeldByCollection", kids, (10, 0, 0))
indirect = bpy.data.collections.new("IndirectKids")
asset.children.link(indirect)
mesh_object("IndirectObj", indirect, (12, 0, 0))
layer = bpy.context.view_layer.layer_collection.children["Asset"]
layer.children["HoldoutKids"].holdout = True
layer.children["IndirectKids"].indirect_only = True


def scatter(name, location, distribute, object_instance=None):
    obj = mesh_object(name, asset, location)
    obj["tint"] = (0.9, 0.8, 0.7, 0.6)
    tree = bpy.data.node_groups.new(name + "Tree", "GeometryNodeTree")
    tree.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    n, links = tree.nodes, tree.links
    grid = n.new("GeometryNodeMeshGrid")
    grid.inputs["Vertices X"].default_value = 4
    grid.inputs["Vertices Y"].default_value = 3
    if distribute:
        points = n.new("GeometryNodeDistributePointsOnFaces")
        points.inputs["Density"].default_value = 3.0
    else:
        points = n.new("GeometryNodeMeshToPoints")
    place = n.new("GeometryNodeInstanceOnPoints")
    links.new(grid.outputs["Mesh"], points.inputs["Mesh"])
    links.new(points.outputs["Points"], place.inputs["Points"])
    if object_instance is None:
        prim = n.new("GeometryNodeMeshCube")
        prim.inputs["Size"].default_value = (0.1, 0.1, 0.1)
        assign = n.new("GeometryNodeSetMaterial")
        assign.inputs["Material"].default_value = material
        links.new(prim.outputs["Mesh"], assign.inputs["Geometry"])
        links.new(assign.outputs["Geometry"], place.inputs["Instance"])
    else:
        info = n.new("GeometryNodeObjectInfo")
        info.inputs["Object"].default_value = object_instance
        info.inputs["As Instance"].default_value = True
        links.new(info.outputs["Geometry"], place.inputs["Instance"])
    store = n.new("GeometryNodeStoreNamedAttribute")
    store.data_type, store.domain = "FLOAT_COLOR", "INSTANCE"
    store.inputs["Name"].default_value = "inst_color"
    position = n.new("GeometryNodeInputPosition")
    links.new(place.outputs["Instances"], store.inputs["Geometry"])
    links.new(position.outputs["Position"], store.inputs["Value"])
    links.new(store.outputs["Geometry"], n.new("NodeGroupOutput").inputs["Geometry"])
    obj.modifiers.new("GN", "NODES").node_group = tree
    return obj


scatter("Scatter", (0, 5, 0), False)
scatter("Spread", (0, 10, 0), True)
spare = bpy.data.collections.new("Spare")
scene.collection.children.link(spare)
pebble = mesh_object("Pebble", spare, (0, 0, -5))
pebble.data.materials.append(material)
pebble.color = (1, 0, 0, 1)
scatter("Pebbles", (0, 15, 0), False, pebble)
bolt = mesh_object("Bolt", spare, (0, 0, -9))
inner = bpy.data.collections.new("Inner")
inner.objects.link(bolt)
nest = bpy.data.objects.new("Nest", None)
nest.instance_type, nest.instance_collection = "COLLECTION", inner
nest.location = (0, 20, 0)
asset.objects.link(nest)
bpy.context.view_layer.layer_collection.children["Spare"].exclude = True
path = out + "/object_values.blend"
bpy.ops.wm.save_as_mainfile(filepath=path)

depsgraph = bpy.context.evaluated_depsgraph_get()
native = []
for instance in depsgraph.object_instances:
    if not instance.is_instance:
        continue
    native.append(dict(name=instance.object.original.name, parent=instance.parent.original.name,
                       persistent_id=[int(v) for v in instance.persistent_id if v != 0x7FFFFFFF],
                       random_id=int(instance.random_id), location=list(instance.matrix_world.translation)))

# Blender's own Cycles: Object Info > Random of two real objects (one non-ASCII name) and of a
# scatter's instances, as emission strength on planes filling an orthographic camera.
check = bpy.data.scenes.new("RandomCheck")
mat = bpy.data.materials.new("RandomEmission")
mat.use_nodes = True
tree = mat.node_tree
tree.nodes.clear()
info = tree.nodes.new("ShaderNodeObjectInfo")
emission = tree.nodes.new("ShaderNodeEmission")
tree.links.new(info.outputs["Random"], emission.inputs["Strength"])
tree.links.new(emission.outputs[0], tree.nodes.new("ShaderNodeOutputMaterial").inputs[0])
rendered = ["Ünïcødé ✓", "Plain"]
for k, name in enumerate(rendered):
    data = bpy.data.meshes.new(name + "Mesh")
    data.from_pydata([(-0.5, -0.5, 0), (0.5, -0.5, 0), (0.5, 0.5, 0), (-0.5, 0.5, 0)], [], [(0, 1, 2, 3)])
    data.materials.append(mat)
    obj = bpy.data.objects.new(name, data)
    obj.location = (k - 0.5, 0, 0)
    check.collection.objects.link(obj)
    rendered[k] = obj.name
camera = bpy.data.objects.new("RandomCamera", bpy.data.cameras.new("RandomCamera"))
camera.location = (0, 0, 5)
camera.data.type, camera.data.ortho_scale = "ORTHO", 2.0
check.collection.objects.link(camera)
check.camera = camera
check.render.engine = "CYCLES"
check.cycles.device, check.cycles.samples, check.cycles.use_denoising = "CPU", 1, False
check.render.resolution_x, check.render.resolution_y = 8, 4
check.render.image_settings.file_format = "OPEN_EXR"
check.render.image_settings.color_depth = "32"
check.view_settings.view_transform = "Standard"
check.render.filepath = out + "/random.exr"
bpy.ops.render.render(write_still=True, scene=check.name)
image = bpy.data.images.load(out + "/random.exr")
pixels = list(image.pixels[:])
rendered_values = {rendered[0]: pixels[(2 * 8 + 1) * 4], rendered[1]: pixels[(2 * 8 + 6) * 4]}

requests = [dict(source=path, selector_type="COLLECTION", selector="Asset", frame=1.0),
            dict(source=path, selector_type="OBJECT", selector="Scatter", frame=1.0)]
result = dict(native=native, rendered=rendered_values)
with tempfile.TemporaryDirectory() as textures:
    for name, value in zip(("Asset", "Scatter"), blend_export.evaluate_many(requests, textures)):
        if isinstance(value, Exception):
            result[name] = dict(error=str(value))
            continue
        manifest, arrays = value
        result[name] = dict(
            instances=[dict(name=i["name"], persistent_id=[v for v in i["persistent_id"] if v != 0x7FFFFFFF],
                            location=i["blender_transform"][3::4][:3], blender=i["blender_object"],
                            is_instance=i["is_instance"]) for i in manifest["instances"]],
            diagnostics=manifest["diagnostics"])
print("RESULT:" + json.dumps(result))
"""


@pytest.fixture(scope="module")
def exported(tmp_path_factory):
    if importlib.util.find_spec("bpy") is None:
        pytest.skip("bpy (Blender as a Python module) is not installed")
    folder = tmp_path_factory.mktemp("blend_object_values")
    script = folder / "make_and_export.py"
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


def _by_name(case):
    result = {}
    for instance in case["instances"]:
        result.setdefault(instance["name"], []).append(instance)
    return result


# ---------------------------------------------------------------- item 3: Object Info Random


@pytest.mark.pure
@pytest.mark.cpu
def test_ported_hashes_reference_values():
    from cycles_blender.blend_export import dupli_random_id, hash_string, hash_uint2, object_random_id

    # Values Blender 5.3's Cycles renders for these names (Object Info > Random * 0xFFFFFFFF).
    assert object_random_id("Plain") == 3921286968
    assert object_random_id("W\u00fcrfel \u00e4") == 1236205882  # unsigned bytes, not signed char
    assert hash_string("") == 0 and hash_string("a") == 97 and hash_string("ab") == 97 * 37 + 98
    assert hash_uint2(0, 0) == hash_uint2(0, 0) and hash_uint2(1, 0) != hash_uint2(0, 1)
    assert dupli_random_id("A", []) == hash_uint2(hash_string("A"), 0)
    assert dupli_random_id("A", [3], "R") == hash_uint2(hash_string("A"), 3) ^ hash_uint2(hash_string("R"), 0)


@pytest.mark.blender
@pytest.mark.cpu
def test_real_object_random_matches_a_blender_cycles_render(exported):
    from cycles_houdini.blender.blend_ids import object_random_id

    assert len(exported["rendered"]) == 2
    for name, pixel in exported["rendered"].items():
        expected = float(np.float32(object_random_id(name)) * np.float32(1.0 / 0xFFFFFFFF))
        assert abs(pixel - expected) < 1e-6, (name, pixel, expected)


@pytest.mark.blender
@pytest.mark.cpu
def test_collection_members_get_the_real_object_hash(exported):
    from cycles_houdini.blender.blend_ids import object_random_id

    members = [i for i in exported["Asset"]["instances"] if len(i["persistent_id"]) == 1]
    assert len(members) >= 9
    for instance in members:
        assert instance["blender"]["random_id"] == object_random_id(instance["name"]), instance["name"]


@pytest.mark.blender
@pytest.mark.cpu
def test_instance_random_ids_equal_blenders_depsgraph(exported):
    native = {}
    for entry in exported["native"]:
        key = (entry["name"], tuple(entry["persistent_id"]))
        native.setdefault(key, set()).add(entry["random_id"] & 0xFFFFFFFF)
    # Collection selection: the selector's level is not part of Blender's own scene.
    deep = [i for i in exported["Asset"]["instances"] if len(i["persistent_id"]) > 1]
    names = {i["name"] for i in deep}
    assert {"Scatter", "Spread", "Pebble", "Bolt"} <= names  # primitive, ids, object, collection
    for instance in deep:
        key = (instance["name"], tuple(instance["persistent_id"][:-1]))
        assert instance["blender"]["random_id"] in native[key], key
    # Object selection: the depsgraph's own number.
    scatter = [i for i in exported["Scatter"]["instances"] if i["is_instance"]]
    assert len(scatter) == 12
    for instance in scatter:
        assert instance["blender"]["random_id"] in native[(instance["name"], tuple(instance["persistent_id"]))]
    assert len({i["blender"]["random_id"] for i in deep}) == len(deep)


# ---------------------------------------------------------------- item 5: visibility


@pytest.mark.blender
@pytest.mark.cpu
def test_ray_visibility_holdout_catcher_and_indirect_only(exported):
    case = _by_name(exported["Asset"])
    flags = {name: case[name][0]["blender"] for name in case}
    assert "Ghost" not in case  # every ray switch off: skipped like Blender
    assert any(
        d["code"] == "blend.invisible_object" and "Ghost" in d["message"] for d in exported["Asset"]["diagnostics"]
    )
    assert flags["NoCamera"]["visibility"] == 255 & ~1
    assert flags["IndirectObj"]["visibility"] == 255 & ~1 and not flags["IndirectObj"]["holdout"]
    assert flags["HeldByCollection"]["holdout"] and flags["HeldByCollection"]["visibility"] == 255
    assert flags["HoldMe"]["holdout"] and not flags["Catcher"]["holdout"]
    assert flags["Catcher"]["shadow_catcher"] and not flags["HoldMe"]["shadow_catcher"]
    for name in ("Würfel ä", "Scatter", "Pebble", "Bolt"):
        assert flags[name]["visibility"] == 255 and not flags[name]["holdout"], name


@pytest.mark.pure
@pytest.mark.cpu
def test_ray_visibility_bits():
    from cycles_blender.blend_export import ray_visibility

    switches = dict(
        visible_camera=1, visible_transmission=2, visible_diffuse=4, visible_glossy=8, visible_volume_scatter=16
    )
    switches.update(visible_shadow=96, visible_raycast=128)
    for name, bit in switches.items():
        obj = types.SimpleNamespace(**{key: key != name for key in switches})
        assert ray_visibility(obj) == 255 & ~bit, name


# ---------------------------------------------------------------- item 6: colour, index, attributes


@pytest.mark.blender
@pytest.mark.cpu
def test_object_colour_pass_index_and_object_attributes(exported):
    values = _by_name(exported["Asset"])["Würfel ä"][0]["blender"]
    assert np.allclose(values["color"], [0.2, 0.4, 0.6, 0.8]) and values["pass_index"] == 7
    attributes = {(a["type"], a["name"]): a["value"] for a in values["attributes"]}
    assert np.allclose(attributes[("OBJECT", "tint")], [0.1, 0.2, 0.3, 1.0])  # custom property
    assert np.allclose(attributes[("OBJECT", "color")], [0.2, 0.4, 0.6, 0.8])  # RNA path
    assert np.allclose(attributes[("OBJECT", "pass_index")], [7, 7, 7, 1])
    assert attributes[("INSTANCER", "flag")] == [1.0, 1.0, 1.0, 1.0]  # a real object: its own property
    assert attributes[("INSTANCER", "missing")] == [0.0, 0.0, 0.0, 0.0]
    assert attributes[("INSTANCER", "inst_color")] == [0.0, 0.0, 0.0, 0.0]


@pytest.mark.blender
@pytest.mark.cpu
def test_instancer_attributes_follow_each_instance(exported):
    offsets = {"Scatter": 5.0, "Spread": 10.0, "Pebble": 15.0}
    checked = 0
    for instance in exported["Asset"]["instances"]:
        if instance["name"] not in offsets or len(instance["persistent_id"]) < 2:
            continue
        attributes = {(a["type"], a["name"]): a["value"] for a in instance["blender"]["attributes"]}
        x, y, z = instance["location"]
        # The instance attribute is the point position (Store Named Attribute on the instances).
        assert np.allclose(
            attributes[("INSTANCER", "inst_color")], [x, y - offsets[instance["name"]], z, 1.0], atol=1e-5
        )
        # Instancer falls back to the instancing object's custom property; Object reads the object itself.
        assert np.allclose(attributes[("INSTANCER", "tint")], [0.9, 0.8, 0.7, 0.6])
        own = [0.9, 0.8, 0.7, 0.6] if instance["name"] != "Pebble" else [0.0, 0.0, 0.0, 0.0]
        assert np.allclose(attributes[("OBJECT", "tint")], own)
        checked += 1
    assert checked >= 12 + 12 + 1  # Scatter, Pebbles, and Spread's distributed points (with "id")


@pytest.fixture
def assets(monkeypatch, tmp_path):
    fake_hou = types.SimpleNamespace(
        text=types.SimpleNamespace(expandString=lambda value: value, encodeAttrib=lambda name: "enc:" + name),
        getenv=lambda name: None,
        hipFile=types.SimpleNamespace(eventCallbacks=lambda: [], addEventCallback=lambda callback: None),
    )
    monkeypatch.setitem(sys.modules, "hou", fake_hou)
    monkeypatch.setitem(
        sys.modules,
        "PyOpenColorIO",
        types.SimpleNamespace(GetCurrentConfig=lambda: types.SimpleNamespace(getCacheID=lambda: "working")),
    )
    module = importlib.import_module("cycles_houdini.blender.blender_assets")
    monkeypatch.setattr(module, "hou", fake_hou)
    return module


@pytest.mark.pure
@pytest.mark.cpu
def test_native_member_fields(assets):
    values = dict(
        random_id=3921286968,
        visibility=254,
        holdout=False,
        shadow_catcher=True,
        pass_index=3,
        color=[0.1, 0.2, 0.3, 0.4],
        attributes=[dict(type="INSTANCER", name="inst color", value=[1.0, 2.0, 3.0, 0.5])],
    )
    result = assets._blender_object(values)
    assert result["random_id"] == 3921286968 and result["visibility"] == 254 and result["shadow_catcher"]
    assert result["attributes"] == [
        dict(name="enc:__cycles_blender_instancer_inst color", components=3, value=[1.0, 2.0, 3.0]),
        dict(name="enc:__cycles_blender_instancer_inst color__alpha", components=1, value=[0.5]),
    ]
