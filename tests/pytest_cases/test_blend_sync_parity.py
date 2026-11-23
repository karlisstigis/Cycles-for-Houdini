# SPDX-License-Identifier: Apache-2.0
"""Link mode matches Blender's own Cycles translator (task S1, blender-sync-audit items 1, 2, 4,
7, 16): geometry-nodes instances, the texture space behind Generated coordinates, material
indices above the slot count, the UV rule for "uv"-named attributes and objects the Link path
cannot render yet.

The bpy cases build a .blend with Blender 5.3 (the PyPI bpy module) and evaluate it with the
same render requests the worker sends (a RENDER depsgraph through the exporter's engine); the
reference numbers come from Blender's own depsgraph in the same process.
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


def plane(name, size=2.0):
    bpy.ops.mesh.primitive_plane_add(size=size)
    obj = bpy.context.active_object
    obj.name = obj.data.name = name
    return obj


plane("Plane")
arr = plane("PlaneArr")
arr.location.x = 10
arr.modifiers.new("Array", "ARRAY").count = 3
custom = plane("PlaneCustom")
custom.location.x = 20
custom.data.use_auto_texspace = False
custom.data.texspace_location = (0.25, -0.5, 0.1)
custom.data.texspace_size = (2.0, 0.5, 0.3)
bpy.ops.mesh.primitive_cube_add(size=1.0)
rock = bpy.context.active_object
rock.name = "Rock"
rock.location = (0, 0, -20)


def scatter(name, mode):
    bpy.ops.mesh.primitive_plane_add(size=1.0)
    obj = bpy.context.active_object
    obj.name = name
    tree = bpy.data.node_groups.new(name + "Tree", "GeometryNodeTree")
    tree.interface.new_socket("Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
    tree.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    nodes, links = tree.nodes, tree.links
    output = nodes.new("NodeGroupOutput")
    grid = nodes.new("GeometryNodeMeshGrid")
    grid.inputs["Vertices X"].default_value = 3
    grid.inputs["Vertices Y"].default_value = 2
    points = nodes.new("GeometryNodeMeshToPoints")
    place = nodes.new("GeometryNodeInstanceOnPoints")
    links.new(grid.outputs["Mesh"], points.inputs["Mesh"])
    links.new(points.outputs["Points"], place.inputs["Points"])
    if mode == "primitive":
        cube = nodes.new("GeometryNodeMeshCube")
        cube.inputs["Size"].default_value = (0.2, 0.3, 0.4)
        # Keep the primitive's UV map and add a generic attribute, as asset authors do.
        store = nodes.new("GeometryNodeStoreNamedAttribute")
        store.data_type, store.domain = "FLOAT2", "CORNER"
        store.inputs["Name"].default_value = "UVMap"
        links.new(cube.outputs["Mesh"], store.inputs["Geometry"])
        links.new(cube.outputs["UV Map"], store.inputs["Value"])
        tint = nodes.new("GeometryNodeStoreNamedAttribute")
        tint.data_type, tint.domain = "FLOAT", "POINT"
        tint.inputs["Name"].default_value = "tint"
        tint.inputs["Value"].default_value = 0.75
        links.new(store.outputs["Geometry"], tint.inputs["Geometry"])
        links.new(tint.outputs["Geometry"], place.inputs["Instance"])
    else:
        info = nodes.new("GeometryNodeObjectInfo")
        info.inputs["Object"].default_value = rock
        info.inputs["As Instance"].default_value = mode == "object_instance"
        links.new(info.outputs["Geometry"], place.inputs["Instance"])
    links.new(place.outputs["Instances"], output.inputs["Geometry"])
    obj.modifiers.new("GN", "NODES").node_group = tree
    return obj


scatter("ScatterPrim", "primitive").location.x = 30
scatter("ScatterInfo", "object_info").location.x = 40
scatter("ScatterObject", "object_instance").location.x = 50  # control: real object instances

# Material index above the slot count (5 with 2 slots) and a negative one.
slots = plane("Slots")
slots.location.x = 60
bpy.ops.object.mode_set(mode="EDIT")
bpy.ops.mesh.subdivide()
bpy.ops.object.mode_set(mode="OBJECT")
for index in range(2):
    slots.data.materials.append(bpy.data.materials.new("Slot" + str(index)))
indices = slots.data.attributes["material_index"] if "material_index" in slots.data.attributes else None
if indices is None:
    indices = slots.data.attributes.new("material_index", "INT", "FACE")
for face, value in enumerate((0, 1, 5, -3)):
    indices.data[face].value = value

# A collection with a mesh, hair curves, a point cloud and a mesh with particle hair.
mixed = bpy.data.collections.new("Mixed")
scene.collection.children.link(mixed)
body = bpy.data.objects.new("Body", bpy.data.meshes.new("Body"))
body.data.from_pydata([(0, 0, 0), (1, 0, 0), (1, 1, 0)], [], [(0, 1, 2)])
mixed.objects.link(body)
curves = bpy.data.hair_curves.new("HairData")
curves.add_curves([3])
mixed.objects.link(bpy.data.objects.new("Hair", curves))
# A geometry-nodes point cloud (Mesh to Points) arrives as a POINTCLOUD dupli of a mesh object.
cloud = bpy.data.objects.new("Cloud", bpy.data.meshes.new("CloudMesh"))
mixed.objects.link(cloud)
tree = bpy.data.node_groups.new("CloudTree", "GeometryNodeTree")
tree.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
grid = tree.nodes.new("GeometryNodeMeshGrid")
to_points = tree.nodes.new("GeometryNodeMeshToPoints")
tree.links.new(grid.outputs["Mesh"], to_points.inputs["Mesh"])
tree.links.new(to_points.outputs["Points"], tree.nodes.new("NodeGroupOutput").inputs["Geometry"])
cloud.modifiers.new("GN", "NODES").node_group = tree
furry = bpy.data.objects.new("Furry", bpy.data.meshes.new("Furry"))
furry.data.from_pydata([(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)], [], [(0, 1, 2, 3)])
mixed.objects.link(furry)
system = furry.modifiers.new("Fur", "PARTICLE_SYSTEM").particle_system
system.settings.type = "HAIR"
system.settings.count = 10
only = bpy.data.collections.new("OnlyCurves")
scene.collection.children.link(only)
only.objects.link(bpy.data.objects.new("LoneHair", bpy.data.hair_curves.new("LoneHairData")))
path = out + "/sync_parity.blend"
bpy.ops.wm.save_as_mainfile(filepath=path)

# Blender's own numbers: what its depsgraph hands to Cycles (object_instances, object data).
depsgraph = bpy.context.evaluated_depsgraph_get()
reference = {}
for instance in depsgraph.object_instances:
    parent = instance.parent.original.name if instance.is_instance and instance.parent else None
    owner = parent or instance.object.original.name
    if instance.object.type != "MESH" or not instance.show_self:
        continue
    data = instance.object.data
    count = len(data.vertices)
    if not count:
        continue
    matrix = instance.matrix_world
    world = [list(matrix @ vertex.co) for vertex in data.vertices]
    entry = reference.setdefault(owner, dict(counts=[], world=[], generated=[]))
    entry["counts"].append(count)
    entry["world"].extend(world)
    location, size = list(data.texspace_location), list(data.texspace_size)
    entry.setdefault("texture_spaces", []).append([location, size])
    entry.setdefault("uv_layers", []).append([layer.name for layer in data.uv_layers])

objects = ("Plane", "PlaneArr", "PlaneCustom", "ScatterPrim", "ScatterInfo", "ScatterObject", "Slots")
requests = [dict(source=path, selector_type="OBJECT", selector=name, frame=1.0) for name in objects]
requests.append(dict(source=path, selector_type="COLLECTION", selector="Mixed", frame=1.0))
requests.append(dict(source=path, selector_type="COLLECTION", selector="OnlyCurves", frame=1.0))
names = objects + ("Mixed", "OnlyCurves")
result = dict(reference=reference)
with tempfile.TemporaryDirectory() as textures:
    for name, value in zip(names, blend_export.evaluate_many(requests, textures)):
        if isinstance(value, Exception):
            result[name] = dict(error=str(value))
            continue
        manifest, arrays = value
        meshes = {mesh["key"]: mesh for mesh in manifest["meshes"]}
        result[name] = dict(
            mode=manifest["depsgraph_mode"],
            instances=[
                dict(
                    name=instance["name"],
                    mesh=instance["mesh_key"],
                    transform=instance["blender_transform"],
                    materials=len(instance["material_keys"]),
                    geometry_type=instance.get("geometry_type", "mesh"),
                )
                for instance in manifest["instances"]
            ],
            meshes={
                key: dict(
                    vertex_count=mesh["vertex_count"],
                    positions=arrays[mesh["arrays"]["positions"]].tolist(),
                    material_indices=arrays[mesh["arrays"]["material_indices"]].tolist(),
                    texture_space=mesh.get("texture_space"),
                    uv_layers=[layer["name"] for layer in mesh["uv_layers"]],
                    default_uv=mesh["default_uv"],
                    attributes={field["name"]: arrays[field["array"]].tolist() for field in mesh["attributes"]},
                )
                for key, mesh in meshes.items()
            },
            diagnostics=manifest["diagnostics"],
        )
print("RESULT:" + json.dumps(result))
"""


@pytest.fixture(scope="module")
def exported(tmp_path_factory):
    if importlib.util.find_spec("bpy") is None:
        pytest.skip("bpy (Blender as a Python module) is not installed")
    folder = tmp_path_factory.mktemp("blend_sync_parity")
    script = folder / "make_and_export.py"
    script.write_text(SCRIPT, encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, "-I", str(script), str(ROOT / "houdini/python3.13libs"), str(folder)],
        capture_output=True,
        text=True,
        timeout=600,
        cwd=folder,
    )
    lines = [line for line in completed.stdout.splitlines() if line.startswith("RESULT:")]
    assert lines, completed.stdout[-4000:] + completed.stderr[-4000:]
    return json.loads(lines[-1][len("RESULT:") :])


def generated(positions, texture_space):
    """Blender v5.3.0 mesh_texture_space + create_mesh (src/core/generated_coordinates.h), float32."""
    f = np.float32
    location = np.asarray(texture_space["location"], f)
    size = np.asarray(texture_space["size"], f)
    scale = np.where(size != 0, f(0.5) / np.where(size != 0, size, f(1)), size).astype(f)
    offset = (location * scale - f(0.5)).astype(f)
    return (np.asarray(positions, f) * scale - offset).astype(f)


def world_points(case):
    points = []
    for instance in case["instances"]:
        matrix = np.asarray(instance["transform"], float).reshape(4, 4)
        local = np.asarray(case["meshes"][instance["mesh"]]["positions"], float).reshape(-1, 3)
        points.extend((local @ matrix[:3, :3].T + matrix[:3, 3]).tolist())
    return np.asarray(sorted(map(tuple, np.round(points, 5))))


# ---------------------------------------------------------------- item 1: geometry-nodes instances


@pytest.mark.parametrize("name", ["ScatterPrim", "ScatterInfo", "ScatterObject"])
@pytest.mark.blender
@pytest.mark.cpu
def test_geometry_nodes_instances_match_blender_object_instances(exported, name):
    case, reference = exported[name], exported["reference"][name]
    assert case["mode"] == "RENDER"
    counts = sorted(case["meshes"][instance["mesh"]]["vertex_count"] for instance in case["instances"])
    counts = [count for count in counts if count]
    assert counts == sorted(reference["counts"]) and sum(counts) == 48  # 6 cubes x 8 vertices
    expected = np.asarray(sorted(map(tuple, np.round(reference["world"], 5))))
    assert np.allclose(world_points(case), expected, atol=1e-4)


@pytest.mark.blender
@pytest.mark.cpu
def test_geometry_instance_keeps_its_uv_map_and_attributes(exported):
    assert exported["reference"]["ScatterPrim"]["uv_layers"][0] == ["UVMap"]
    meshes = [mesh for mesh in exported["ScatterPrim"]["meshes"].values() if mesh["vertex_count"]]
    assert meshes and all(mesh["uv_layers"] == ["UVMap"] and mesh["default_uv"] == "UVMap" for mesh in meshes)
    assert all(np.allclose(mesh["attributes"]["tint"], 0.75) for mesh in meshes)


# ---------------------------------------------------------------- item 2: texture space


def _single(case):
    [mesh] = [mesh for mesh in case["meshes"].values() if mesh["vertex_count"]]
    return mesh


@pytest.mark.parametrize("name", ["Plane", "PlaneArr", "PlaneCustom", "ScatterPrim"])
@pytest.mark.blender
@pytest.mark.cpu
def test_texture_space_is_blenders(exported, name):
    reference = exported["reference"][name]["texture_spaces"][0]
    for mesh in exported[name]["meshes"].values():
        if mesh["vertex_count"]:
            assert np.allclose(mesh["texture_space"]["location"], reference[0])
            assert np.allclose(mesh["texture_space"]["size"], reference[1])


@pytest.mark.blender
@pytest.mark.cpu
def test_generated_values_follow_blenders_formula(exported):
    # A 2 x 2 plane: its box, the flat side at 0.5.
    plane = _single(exported["Plane"])
    values = generated(plane["positions"], plane["texture_space"])
    expected = (np.asarray(plane["positions"]) + [1, 1, 0]) / [2, 2, 1] + [0, 0, 0.5]
    assert np.allclose(values, expected, atol=1e-6) and np.allclose(values[:, 2], 0.5)
    # Array x3: the texture space stays the box before modifiers, so the copies run past 1 (to 3).
    arr = _single(exported["PlaneArr"])
    assert arr["vertex_count"] == 12
    values = generated(arr["positions"], arr["texture_space"])
    assert np.isclose(values[:, 0].min(), 0) and np.isclose(values[:, 0].max(), 3)
    assert np.allclose(values[:, 0], (np.asarray(arr["positions"])[:, 0] + 1) / 2, atol=1e-6)
    # Custom texture space: (p - location) / (2 size) + 0.5.
    custom = _single(exported["PlaneCustom"])
    values = generated(custom["positions"], custom["texture_space"])
    location, size = np.array([0.25, -0.5, 0.1]), np.array([2.0, 0.5, 0.3])
    assert np.allclose(values, (np.asarray(custom["positions"]) - location) / (2 * size) + 0.5, atol=1e-5)
    # A geometry-nodes cube instance has its own box (size 0.2, 0.3, 0.4): 0..1 on every axis.
    cube = next(mesh for mesh in exported["ScatterPrim"]["meshes"].values() if mesh["vertex_count"])
    values = generated(cube["positions"], cube["texture_space"])
    assert np.allclose(values.min(axis=0), 0, atol=1e-6) and np.allclose(values.max(axis=0), 1, atol=1e-6)


# ---------------------------------------------------------------- item 7: material index clamp


@pytest.mark.blender
@pytest.mark.cpu
def test_material_indices_above_the_slots_export(exported):
    case = exported["Slots"]
    assert "error" not in case
    [instance] = case["instances"]
    assert instance["materials"] == 2
    assert sorted(_single(case)["material_indices"]) == sorted([0, 1, 5, -3])


@pytest.fixture
def assets(monkeypatch, tmp_path):
    fake_hou = types.SimpleNamespace(
        text=types.SimpleNamespace(
            expandString=lambda value: value.replace("$HIP", str(tmp_path)), encodeAttrib=lambda name: name
        ),
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
    executable = tmp_path / "blender.exe"
    executable.touch()
    monkeypatch.setattr(module, "_blender", lambda: executable)
    (tmp_path / "a.blend").write_bytes(b"synthetic")
    monkeypatch.setattr(module, "_meshes", {})
    monkeypatch.setattr(module, "_topologies", {})
    return module


@pytest.mark.pure
@pytest.mark.cpu
@pytest.mark.parametrize(
    ("indices", "slots", "palette"),
    [([0, 1, 5, -3], 2, 2), ([0, 1], 2, 2), ([0, 0], 3, 1), ([-1, -2], 2, 1), ([7], 1, 1)],
)
def test_native_mesh_clamps_material_indices_like_blender(assets, monkeypatch, indices, slots, palette):
    captured = []
    native = types.SimpleNamespace(blend_mesh=lambda record, arrays, names, *rest: captured.append(names) or object())
    monkeypatch.setitem(sys.modules, "cycles_houdini._native", native)
    monkeypatch.setattr(sys.modules["cycles_houdini"], "_native", native, raising=False)
    arrays = {"m/material_indices": np.asarray(indices, np.int32)}
    mesh = dict(key="m", name="m", arrays=dict(material_indices="m/material_indices"))
    key, _ = assets._native_mesh(mesh, arrays, {"m/material_indices": "id"}, slots)
    assert captured == [("",) * palette]
    # The clamped result depends on the slot count, so it is part of the mesh key only then.
    clamped = min(indices) < 0 or max(indices) >= slots
    other, _ = assets._native_mesh(mesh, arrays, {"m/material_indices": "id"}, slots + 1)
    assert (other != key) == clamped


# ---------------------------------------------------------------- item 16: Link is Blender-exact


@pytest.mark.pure
@pytest.mark.cpu
def test_link_requests_have_no_uv_switch(assets, monkeypatch):
    # The artist chose Blender-exact Link renders without a switch (2026-11-14): the old
    # variable changes nothing, the key keeps its 9 elements and the request has no UV field.
    reference = "$HIP/a.blend#object=Tree"
    monkeypatch.delenv("CYCLES_HOUDINI_BLENDER_UV_ATTRIBUTES", raising=False)
    key, request = assets._resolve_spec((reference, 1.0, True))
    assert len(key) == 9 and key[8] is False and "uv_attributes" not in request
    assert assets._bundles._describe(key).endswith("#object=Tree frame 1")
    monkeypatch.setenv("CYCLES_HOUDINI_BLENDER_UV_ATTRIBUTES", "promote")
    assert assets._resolve_spec((reference, 1.0, True)) == (key, request)
    assert not hasattr(assets, "uv_attributes_mode")


@pytest.mark.pure
@pytest.mark.cpu
def test_uv_sets_promote_flag():
    from cycles_blender.blend_export import uv_sets

    attributes = [("UVMap", "CORNER", "FLOAT_VECTOR"), ("pt_uv", "POINT", "FLOAT2")]
    assert uv_sets([], None, attributes, {"UVMap"}, promote=False) == dict(
        default=None, source=None, attributes=[], keep=set()
    )
    assert uv_sets([], None, attributes, {"UVMap"})["default"] == "UVMap"
    # Real UV maps are UV maps either way.
    assert uv_sets(["Main"], "Main", attributes, (), promote=False)["default"] == "Main"


# ---------------------------------------------------------------- item 4: unsupported objects


@pytest.mark.blender
@pytest.mark.cpu
def test_point_clouds_and_hair_are_carried_without_warnings(exported):
    case = exported["Mixed"]
    assert "error" not in case, case
    meshes = [i for i in case["instances"] if i["geometry_type"] == "mesh"]
    rendered = [i["name"] for i in meshes if case["meshes"][i["mesh"]]["vertex_count"]]
    assert sorted(rendered) == ["Body", "Furry"]  # Cloud's own mesh is empty, as in Blender
    # Hair curves, particle hair and point clouds are Link geometry now (test_blend_hair.py and
    # test_blend_pointcloud.py have the details).
    other = sorted((i["name"], i["geometry_type"]) for i in case["instances"] if i["geometry_type"] != "mesh")
    assert other == [("Cloud", "points"), ("Furry", "particle_hair"), ("Hair", "curves")]
    assert not [d for d in case["diagnostics"] if d["code"] == "blend.unsupported_geometry"]


@pytest.mark.blender
@pytest.mark.cpu
def test_selection_with_only_empty_hair_has_no_geometry(exported):
    error = exported["OnlyCurves"]["error"]
    assert "no render-visible evaluated mesh geometry" in error and "LoneHair" not in error
