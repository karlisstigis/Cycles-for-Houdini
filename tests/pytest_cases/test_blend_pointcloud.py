# SPDX-License-Identifier: Apache-2.0
"""Link mode carries point clouds as Blender v5.3.0's Cycles translator syncs them (task H2,
blender-sync-audit item 4): POINTCLOUD objects and every geometry-nodes point cloud (Mesh to
Points, Distribute Points on Faces, instanced Points), intern/cycles/blender/pointcloud.cpp
(export_pointcloud, copy_attributes) and geometry.cpp (determine_geom_type, find_used_shaders).

The bpy case builds a .blend with Blender 5.3 (the PyPI bpy module), exports it with the render
requests the worker sends and compares with Blender's own numbers (the evaluated PointCloud
data); Random is checked against a transcription of Cycles' hash. Gaussian splats and volumes
stay unsupported with a warning. A conversion request (Convert & Append) of legacy particle
hair warns instead of silently dropping it. The render comparison is
tests/houdini_blend_sync_parity_smoke.py.
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
import numpy as np
from cycles_blender import blend_export
from cycles_houdini.blender import blend_convert

out = sys.argv[2]
bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene
asset = bpy.data.collections.new("Asset")
scene.collection.children.link(asset)
rng = np.random.default_rng(11)


def attribute_material(name, attribute):
    material = bpy.data.materials.new(name)
    material.use_nodes = True
    nodes = material.node_tree.nodes
    node = nodes.new("ShaderNodeAttribute")
    node.attribute_name = attribute
    emission = nodes.new("ShaderNodeEmission")
    material.node_tree.links.new(node.outputs["Color"], emission.inputs["Color"])
    material.node_tree.links.new(emission.outputs[0], nodes["Material Output"].inputs["Surface"])
    return material


# A point cloud object with a radius, attributes of every carried type and one that is not.
cloud = bpy.data.pointclouds.new("CloudData")
cloud.resize(40)
cloud.attributes["position"].data.foreach_set("vector", rng.random(120).astype(np.float32))
cloud.attributes.new("radius", "FLOAT", "POINT").data.foreach_set("value", (rng.random(40) * 0.08 + 0.02).astype(np.float32))
cloud.attributes.new("heat", "FLOAT", "POINT").data.foreach_set("value", rng.random(40).astype(np.float32))
cloud.attributes.new("tint", "FLOAT_COLOR", "POINT").data.foreach_set("color", rng.random(160).astype(np.float32))
cloud.attributes.new("dir", "FLOAT_VECTOR", "POINT").data.foreach_set("vector", rng.random(120).astype(np.float32))
cloud.attributes.new("uvish", "FLOAT2", "POINT").data.foreach_set("vector", rng.random(80).astype(np.float32))
cloud.attributes.new("count", "INT", "POINT").data.foreach_set("value", rng.integers(-5, 50, 40).astype(np.int32))
cloud.attributes.new("flag", "BOOLEAN", "POINT").data.foreach_set("value", (rng.random(40) > 0.5))
cloud.attributes.new("quat", "QUATERNION", "POINT")
cloud.materials.append(attribute_material("First", "position"))
cloud.materials.append(attribute_material("Second", "heat"))
obj = bpy.data.objects.new("Cloud", cloud)
obj.location = (1, 2, 3)
asset.objects.link(obj)

# No radius attribute (0.01) and no material.
plain = bpy.data.pointclouds.new("PlainData")
plain.resize(5)
plain.attributes["position"].data.foreach_set("vector", rng.random(15).astype(np.float32))
asset.objects.link(bpy.data.objects.new("Plain", plain))


def node_object(name, build, location=(0, 0, 0)):
    result = bpy.data.objects.new(name, bpy.data.meshes.new(name + "Mesh"))
    result.location = location
    asset.objects.link(result)
    tree = bpy.data.node_groups.new(name + "Tree", "GeometryNodeTree")
    tree.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    tree.links.new(build(tree.nodes, tree.links), tree.nodes.new("NodeGroupOutput").inputs["Geometry"])
    result.modifiers.new("GN", "NODES").node_group = tree
    return result


def scatter(n, links):
    grid = n.new("GeometryNodeMeshGrid")
    points = n.new("GeometryNodeDistributePointsOnFaces")
    points.inputs["Density"].default_value = 30.0
    store = n.new("GeometryNodeStoreNamedAttribute")
    store.data_type, store.domain = "FLOAT", "POINT"
    store.inputs["Name"].default_value = "weight"
    random = n.new("FunctionNodeRandomValue")
    links.new(grid.outputs["Mesh"], points.inputs["Mesh"])
    links.new(points.outputs["Points"], store.inputs["Geometry"])
    links.new(random.outputs["Value"], store.inputs["Value"])
    return store.outputs["Geometry"]


def sprinkles(n, links):
    line = n.new("GeometryNodeMeshLine")
    line.inputs["Count"].default_value = 3
    to_points = n.new("GeometryNodeMeshToPoints")
    blob = n.new("GeometryNodePoints")
    blob.inputs["Count"].default_value = 4
    blob.inputs["Radius"].default_value = 0.2
    position = n.new("FunctionNodeRandomValue")
    position.data_type = "FLOAT_VECTOR"
    place = n.new("GeometryNodeInstanceOnPoints")
    links.new(line.outputs["Mesh"], to_points.inputs["Mesh"])
    links.new(position.outputs["Value"], blob.inputs["Position"])
    links.new(to_points.outputs["Points"], place.inputs["Points"])
    links.new(blob.outputs["Geometry"], place.inputs["Instance"])
    return place.outputs["Instances"]


node_object("Scatter", scatter, (0, 3, 0))
node_object("Sprinkles", sprinkles, (0, 6, 0))

splat = bpy.data.pointclouds.new("SplatData")
splat.resize(3)
splat.type = "GAUSSIAN_SPLAT"
asset.objects.link(bpy.data.objects.new("Splat", splat))
asset.objects.link(bpy.data.objects.new("Fog", bpy.data.volumes.new("FogData")))

mixed = bpy.data.collections.new("Mixed")
scene.collection.children.link(mixed)
body = bpy.data.objects.new("Body", bpy.data.meshes.new("Body"))
body.data.from_pydata([(0, 0, 0), (1, 0, 0), (1, 1, 0)], [], [(0, 1, 2)])
mixed.objects.link(body)
hair = bpy.data.hair_curves.new("HairData")
hair.add_curves([3])
hair.attributes["position"].data.foreach_set("vector", [0, 0, 0, 0, 0, 1, 0, 0, 2])
mixed.objects.link(bpy.data.objects.new("Hair", hair))
mixed.objects.link(bpy.data.objects.new("Speck", plain))

furs = bpy.data.collections.new("Furs")
scene.collection.children.link(furs)
for name, hidden in (("Furry", False), ("Furrier", True)):
    emitter = bpy.data.objects.new(name, bpy.data.meshes.new(name))
    emitter.data.from_pydata([(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)], [], [(0, 1, 2, 3)])
    furs.objects.link(emitter)
    system = emitter.modifiers.new("Fur", "PARTICLE_SYSTEM").particle_system
    system.settings.type = "HAIR"
    system.settings.count = 10
    emitter.show_instancer_for_render = not hidden
furs.objects.link(obj)

path = out + "/points.blend"
bpy.ops.wm.save_as_mainfile(filepath=path)

# Blender's own numbers: the evaluated point clouds, real objects and geometry-nodes duplis.
depsgraph = bpy.context.evaluated_depsgraph_get()
kinds = {"FLOAT_COLOR": ("color", 4), "FLOAT_VECTOR": ("vector", 3), "FLOAT2": ("vector", 2), "FLOAT": ("value", 1),
         "INT": ("value", 1), "BOOLEAN": ("value", 1)}


def read(data):
    result = {}
    for attribute in data.attributes:
        if attribute.name.startswith(".") or attribute.data_type not in kinds:
            continue
        prop, width = kinds[attribute.data_type]
        values = np.empty(len(attribute.data) * width, np.float64)
        attribute.data.foreach_get(prop, values)
        result[attribute.name] = dict(type=attribute.data_type, values=values.tolist())
    return dict(count=len(data.points), attributes=result)


reference = {}
for instance in depsgraph.object_instances:
    if instance.object.type != "POINTCLOUD":
        continue
    owner = instance.parent.original.name if instance.is_instance else instance.object.original.name
    reference.setdefault(owner, []).append(read(instance.object.data))

flags = dict(vertex_groups=True, curves=True, pointclouds=True, object_tree=True, native_subdivision=False)
requests = [dict(source=path, selector_type="COLLECTION", selector="Asset", frame=1.0),
            dict(source=path, selector_type="COLLECTION", selector="Mixed", frame=1.0),
            dict(source=path, selector_type="OBJECT", selector="Scatter", frame=1.0, viewport_hulls=True),
            dict(source=path, selector_type="COLLECTION", selector="Furs", frame=1.0, **flags)]
result = dict(reference=reference)
with tempfile.TemporaryDirectory() as textures:
    for name, value in zip(("Asset", "Mixed", "Scatter", "Convert"), blend_export.evaluate_many(requests, textures)):
        if isinstance(value, Exception):
            result[name] = dict(error=str(value))
            continue
        manifest, arrays = value
        points = {}
        for record in manifest.get("points", []):
            points[record["key"]] = dict(
                record,
                values={k: np.asarray(arrays[v]).tolist() for k, v in record["arrays"].items()},
                attribute_values={f["name"]: np.asarray(arrays[f["array"]]).tolist() for f in record["attributes"]},
            )
        result[name] = dict(
            points=points,
            instances=[dict(name=i["name"], key=i["mesh_key"], geometry_type=i.get("geometry_type", "mesh"),
                            materials=i["material_keys"], blender=i.get("blender_object"),
                            transform=i["transform"]) for i in manifest["instances"]],
            materials={m["key"]: m.get("name") for m in manifest["materials"]},
            diagnostics=manifest["diagnostics"],
            hulls=manifest.get("viewport_hulls"),
            hull_reason=manifest.get("viewport_hull_reason"),
        )
        if name == "Convert":
            report = blend_convert.Report()
            frame = blend_convert.assemble(manifest, arrays, "hierarchy", {}, report, {})
            result[name]["report"] = report.as_dict()
            result[name]["converted"] = {
                entry["name"]: dict(points=entry["geo"].num_points if entry.get("geo") is not None else None,
                                    attributes=sorted(entry["geo"].attribs) if entry.get("geo") is not None else [])
                for entry in frame["objects"]
            }
            result[name]["pointclouds"] = [dict(key=r["key"], point_count=r["point_count"]) for r in manifest["pointclouds"]]
print("RESULT:" + json.dumps(result))
"""


@pytest.fixture(scope="module")
def exported(tmp_path_factory):
    if importlib.util.find_spec("bpy") is None:
        pytest.skip("bpy (Blender as a Python module) is not installed")
    folder = tmp_path_factory.mktemp("blend_pointcloud")
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


def _points(case, name):
    return [
        (case["points"][i["key"]], i) for i in case["instances"] if i["name"] == name and i["geometry_type"] == "points"
    ]


def blender_random(index):
    """hash_uint2_to_float(index, 0) (intern/cycles/util/hash.h), scalar transcription."""
    from cycles_houdini.blender.blend_ids import hash_uint2

    return float(np.float32(hash_uint2(index, 0)) / np.float32(0xFFFFFFFF))


@pytest.mark.blender
@pytest.mark.cpu
def test_point_cloud_positions_radii_random_and_attributes_equal_blender(exported):
    case = exported["Asset"]
    assert "error" not in case, case
    ((record, instance),) = _points(case, "Cloud")
    (blender,) = exported["reference"]["Cloud"]
    attributes = blender["attributes"]
    assert record["point_count"] == blender["count"] == 40
    np.testing.assert_array_equal(
        np.asarray(record["values"]["positions"], np.float32).ravel(),
        np.asarray(attributes["position"]["values"], np.float32),
    )
    np.testing.assert_array_equal(
        np.asarray(record["values"]["radius"], np.float32), np.asarray(attributes["radius"]["values"], np.float32)
    )
    assert record["max_radius"] == pytest.approx(max(attributes["radius"]["values"]))
    assert record["values"]["randoms"] == pytest.approx([blender_random(i) for i in range(40)], abs=0)
    fields = {field["name"]: field for field in record["attributes"]}
    # copy_attributes: every attribute by name except Cycles' own "radius"; "position" because
    # the first material reads it. QUATERNION has no Cycles attribute type here.
    assert sorted(fields) == ["count", "dir", "flag", "heat", "position", "tint", "uvish"]
    for name, field in fields.items():
        assert field["domain"] == "POINT" and field["data_type"] == attributes[name]["type"]
        np.testing.assert_allclose(
            np.asarray(record["attribute_values"][name], np.float64).ravel(), attributes[name]["values"], rtol=1e-7
        )
    assert [field["name"] for field in record["skipped_attributes"]] == ["quat"]
    assert any(
        d["code"] == "blend.point_attribute" and "quat (QUATERNION)" in d["message"] for d in case["diagnostics"]
    )
    # find_used_shaders + export_pointcloud: every point uses shader 0, the first slot.
    assert [case["materials"][key] for key in instance["materials"]] == ["First"]
    assert instance["blender"]["visibility"] == 255 and instance["transform"][3] == 1.0


@pytest.mark.blender
@pytest.mark.cpu
def test_point_cloud_without_radius_uses_blenders_default(exported):
    ((record, instance),) = _points(exported["Asset"], "Plain")
    assert record["values"]["radius"] == [pytest.approx(0.01)] * 5
    assert record["attributes"] == [] and len(instance["materials"]) == 1  # the default surface
    assert record["values"]["randoms"] == pytest.approx([blender_random(i) for i in range(5)], abs=0)


@pytest.mark.blender
@pytest.mark.cpu
def test_geometry_nodes_point_clouds_equal_blender(exported):
    case = exported["Asset"]
    # Distribute Points on Faces: a POINTCLOUD dupli of the mesh object, its "id" and a stored
    # attribute by name; the object's own (empty) mesh is a separate member.
    ((record, _),) = _points(case, "Scatter")
    (blender,) = exported["reference"]["Scatter"]
    assert record["point_count"] == blender["count"] > 10
    np.testing.assert_array_equal(
        np.asarray(record["values"]["positions"], np.float32).ravel(),
        np.asarray(blender["attributes"]["position"]["values"], np.float32),
    )
    np.testing.assert_array_equal(
        np.asarray(record["values"]["radius"], np.float32),
        np.asarray(blender["attributes"]["radius"]["values"], np.float32),
    )
    for name in ("id", "weight"):
        np.testing.assert_array_equal(
            np.asarray(record["attribute_values"][name], np.float64).ravel(), blender["attributes"][name]["values"]
        )
    # Instanced Points (not realized): one point cloud member per instance, as Blender syncs them.
    sprinkles = _points(case, "Sprinkles")
    assert len(sprinkles) == len(exported["reference"]["Sprinkles"]) == 3
    assert len({tuple(instance["transform"]) for _, instance in sprinkles}) == 3
    for record, _ in sprinkles:
        assert record["point_count"] == 4 and record["values"]["radius"] == [pytest.approx(0.2)] * 4


@pytest.mark.blender
@pytest.mark.cpu
def test_splats_and_volumes_are_still_warned(exported):
    warnings = [d for d in exported["Asset"]["diagnostics"] if d["code"] == "blend.unsupported_geometry"]
    messages = sorted(d["message"] for d in warnings)
    assert all(d["severity"] == "warning" for d in warnings) and len(messages) == 2, messages
    assert messages[0].endswith("(GAUSSIAN_SPLAT) yet; skipped Splat")
    assert messages[1].endswith("volumes (VOLUME) yet; skipped Fog")
    assert not [i for i in exported["Asset"]["instances"] if i["name"] in ("Splat", "Fog")]


@pytest.mark.blender
@pytest.mark.cpu
def test_mesh_hair_and_points_selection_has_no_warnings(exported):
    case = exported["Mixed"]
    assert "error" not in case, case
    assert sorted(i["geometry_type"] for i in case["instances"]) == ["curves", "mesh", "points"]
    assert not [d for d in case["diagnostics"] if d["severity"] == "warning"]


@pytest.mark.blender
@pytest.mark.cpu
def test_geometry_nodes_points_get_a_viewport_hull(exported):
    # The object's own mesh is empty; the hull is the points' grown by the largest radius.
    case = exported["Scatter"]
    assert "error" not in case, case
    hulls = case["hulls"]
    assert hulls is not None, case["hull_reason"]
    assert len(hulls["members"]) == 1
    ((record, instance),) = _points(case, "Scatter")
    points = np.asarray(hulls["members"][0]["points"]).reshape(-1, 3)
    matrix = np.asarray(instance["transform"]).reshape(4, 4)
    local = (points - matrix[:3, 3]) @ np.linalg.inv(matrix[:3, :3]).T
    positions = np.asarray(record["values"]["positions"])
    radius = record["max_radius"]
    assert np.abs(local.min(axis=0) - (positions.min(axis=0) - radius)).max() < 1e-5
    assert np.abs(local.max(axis=0) - (positions.max(axis=0) + radius)).max() < 1e-5


@pytest.mark.blender
@pytest.mark.cpu
def test_convert_warns_about_particle_hair_and_keeps_point_clouds(exported):
    case = exported["Convert"]
    assert "error" not in case, case
    (warning,) = [d for d in case["diagnostics"] if d["code"] == "blend.particle_hair"]
    assert warning["severity"] == "warning" and warning["source"] == "Furry, Furrier"
    assert "not converted" in warning["message"] and warning["message"].endswith("Emitters: Furry, Furrier")
    assert any(text.startswith("Furry, Furrier: Particle hair is not converted") for text in case["report"]["skipped"])
    # Convert & Append writes a point cloud as points with pscale and its attributes by name.
    assert case["pointclouds"] == [dict(key="points_000000", point_count=40)]
    converted = case["converted"]
    assert converted["Cloud"]["points"] == 40
    assert {"pscale", "heat", "tint", "dir", "uvish", "count", "flag"} <= set(converted["Cloud"]["attributes"])
    assert converted["Furry"]["points"] == 4 and converted["Furrier"]["points"] is None  # emitter only / nothing


# ---------------------------------------------------------------- Houdini preparation


@pytest.fixture
def assets(monkeypatch):
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
    monkeypatch.setattr(module, "_meshes", {})
    captured = []
    native = types.SimpleNamespace(
        blend_points=lambda record, arrays, palette: captured.append((record, arrays, palette)) or object()
    )
    monkeypatch.setitem(sys.modules, "cycles_houdini._native", native)
    monkeypatch.setattr(sys.modules["cycles_houdini"], "_native", native, raising=False)
    return module, captured


def _cloud_record():
    arrays = {
        "p/positions": np.arange(4 * 3, dtype=np.float32).reshape(4, 3),
        "p/radius": np.float32([0.1, 0.2, 0.3, 0.4]),
        "p/randoms": np.float32([0.1, 0.2, 0.3, 0.4]),
        "p/tint": np.float32([[1, 0, 0, 0.5], [0, 1, 0, 1], [0, 0, 1, 0.25], [1, 1, 1, 1]]),
        "p/count": np.int32([1, 2, 3, 4]),
        "p/pair": np.float32([[0, 1], [2, 3], [4, 5], [6, 7]]),
    }
    record = dict(
        key="p",
        name="p",
        kind="points",
        point_count=4,
        max_radius=0.4,
        arrays=dict(positions="p/positions", radius="p/radius", randoms="p/randoms"),
        attributes=[
            dict(name="tint", domain="POINT", data_type="FLOAT_COLOR", array="p/tint"),
            dict(name="count", domain="POINT", data_type="INT", array="p/count"),
            dict(name="pair", domain="POINT", data_type="FLOAT2", array="p/pair"),
        ],
        skipped_attributes=[],
    )
    return record, arrays, {key: "id:" + key for key in arrays}


@pytest.mark.pure
@pytest.mark.cpu
def test_native_points_keep_blenders_points_and_split_colours(assets):
    module, captured = assets
    record, arrays, identities = _cloud_record()
    key, _ = module._native_points(record, arrays, identities)
    assert key.startswith("blend/points/")
    ((native, local, palette),) = captured
    assert palette == ("",) and native["generated_attribute"] is False
    assert set(native["arrays"]) == {"positions", "radii", "randoms"}
    np.testing.assert_array_equal(local["positions"], arrays["p/positions"])
    np.testing.assert_array_equal(local["radii"], arrays["p/radius"])
    np.testing.assert_array_equal(local["randoms"], arrays["p/randoms"])
    fields = {field["name"]: local[field["array"]] for field in native["attributes"]}
    np.testing.assert_array_equal(fields["enc:tint"], arrays["p/tint"][:, :3])
    np.testing.assert_array_equal(fields["__cycles_blender_alpha_enc:tint"].ravel(), [0.5, 1, 0.25, 1])
    np.testing.assert_array_equal(fields["enc:count"].ravel(), [1, 2, 3, 4])
    np.testing.assert_array_equal(fields["enc:pair"], arrays["p/pair"])
    assert module._native_points(record, arrays, identities)[0] == key and len(captured) == 1


@pytest.mark.pure
@pytest.mark.cpu
def test_bounds_include_points_grown_by_their_largest_radius():
    from cycles_houdini.blender import blend_bounds

    arrays = {"p/positions": np.array([[0, 0, 0], [1, 0, 0], [0, 2, 0], [0, 0, 3]], np.float32)}
    manifest = dict(
        source="a.blend",
        selector_type="OBJECT",
        selector="Points",
        frame=1.0,
        dependency_versions={},
        materials=[],
        meshes=[],
        points=[dict(key="p", arrays=dict(positions="p/positions"), max_radius=0.5)],
        instances=[dict(mesh_key="p", transform=[1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1])],
    )
    record = blend_bounds.evaluated_record(manifest, arrays)
    assert record["known"] and record["bounds"] == pytest.approx([-0.5, -0.5, -0.5, 1.5, 2.5, 3.5])
