# SPDX-License-Identifier: Apache-2.0
"""Link mode carries hair as Blender v5.3.0's Cycles translator syncs it (task H1,
blender-sync-audit item 4): Curves objects (also geometry-nodes hair and curve instances) and
legacy particle hair (with and without children), intern/cycles/blender/curves.cpp.

The bpy case builds a .blend with Blender 5.3 (the PyPI bpy module), exports it with the
render requests the worker sends, and compares with Blender's own numbers: the evaluated
Curves data, and for particle hair BKE_particle_co_hair / BKE_particle_uv_on_emitter through
the Python API (the same functions Blender's translator calls). Values Blender computes inside
Cycles (Random, Intercept, Length, Generated, radius) are checked against transcriptions of
its code here; the render comparison is tests/houdini_blend_sync_parity_smoke.py.
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
import bmesh
import numpy as np
from mathutils import Matrix
from cycles_blender import blend_export

out = sys.argv[2]
bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene
scene.cycles_curves.shape = "THICK"
asset = bpy.data.collections.new("Asset")
scene.collection.children.link(asset)
rng = np.random.default_rng(7)


def material(name):
    result = bpy.data.materials.new(name)
    return result


# A simple Curves object: Catmull-Rom, no radius attribute, one single-point curve.
simple = bpy.data.hair_curves.new("SimpleData")
simple.add_curves([4, 3, 1, 5])
simple.attributes["position"].data.foreach_set("vector", rng.random(13 * 3).astype(np.float32))
simple.materials.append(material("First"))
simple.materials.append(material("Second"))
obj = bpy.data.objects.new("SimpleHair", simple)
obj.location = (1, 2, 3)
asset.objects.link(obj)

# Poly curves with a radius, a per-curve colour, a per-point float and a FLOAT4 (not carried).
poly = bpy.data.hair_curves.new("PolyData")
poly.add_curves([2, 3, 4])
poly.attributes["position"].data.foreach_set("vector", rng.random(9 * 3).astype(np.float32))
poly.set_types(type="POLY")
poly.attributes.new("radius", "FLOAT", "POINT").data.foreach_set("value", rng.random(9).astype(np.float32) * 0.1)
poly.attributes.new("tint", "FLOAT_COLOR", "CURVE").data.foreach_set("color", rng.random(12).astype(np.float32))
poly.attributes.new("weight", "FLOAT", "POINT").data.foreach_set("value", rng.random(9).astype(np.float32))
poly.attributes.new("quad", "FLOAT4", "POINT")
asset.objects.link(bpy.data.objects.new("PolyHair", poly))

# The surface for geometry-nodes hair and particle hair (UVs from the grid primitive).
grid_mesh = bpy.data.meshes.new("SurfaceMesh")
editable = bmesh.new()
editable.loops.layers.uv.new("UVMap")
bmesh.ops.create_grid(editable, x_segments=3, y_segments=3, size=1.0, calc_uvs=True)
editable.to_mesh(grid_mesh)
editable.free()
surface = bpy.data.objects.new("Surface", grid_mesh)
asset.objects.link(surface)
surface.data.materials.append(material("Skin"))
surface.data.materials.append(material("Fur"))

# Geometry-nodes hair on the surface: one curve per distributed point, the surface UV stored
# on the curves as surface_uv_coordinate (what Blender's hair assets do).
fur = bpy.data.objects.new("GNHair", bpy.data.hair_curves.new("GNHairData"))
asset.objects.link(fur)
tree = bpy.data.node_groups.new("HairTree", "GeometryNodeTree")
tree.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
n, links = tree.nodes, tree.links
info = n.new("GeometryNodeObjectInfo")
info.inputs["Object"].default_value = surface
points = n.new("GeometryNodeDistributePointsOnFaces")
points.inputs["Density"].default_value = 20.0
line = n.new("GeometryNodeCurvePrimitiveLine")
line.inputs["End"].default_value = (0, 0, 0.3)
resample = n.new("GeometryNodeResampleCurve")
resample.inputs["Count"].default_value = 5
place = n.new("GeometryNodeInstanceOnPoints")
realize = n.new("GeometryNodeRealizeInstances")
uv = n.new("GeometryNodeInputNamedAttribute")
uv.data_type = "FLOAT_VECTOR"
uv.inputs["Name"].default_value = "UVMap"
store = n.new("GeometryNodeStoreNamedAttribute")
store.data_type, store.domain = "FLOAT2", "CURVE"
store.inputs["Name"].default_value = "surface_uv_coordinate"
radius = n.new("GeometryNodeSetCurveRadius")
radius.inputs["Radius"].default_value = 0.01
# Realize also carries the surface's own UVMap onto the curves; hair assets drop it.
drop = n.new("GeometryNodeRemoveAttribute")
drop.inputs["Name"].default_value = "UVMap"
links.new(info.outputs["Geometry"], points.inputs["Mesh"])
links.new(line.outputs["Curve"], resample.inputs["Curve"])
links.new(points.outputs["Points"], place.inputs["Points"])
links.new(resample.outputs["Curve"], place.inputs["Instance"])
links.new(place.outputs["Instances"], realize.inputs["Geometry"])
links.new(realize.outputs["Geometry"], store.inputs["Geometry"])
links.new(uv.outputs["Attribute"], store.inputs["Value"])
links.new(store.outputs["Geometry"], drop.inputs["Geometry"])
links.new(drop.outputs["Geometry"], radius.inputs["Curve"])
links.new(radius.outputs["Curve"], n.new("NodeGroupOutput").inputs["Geometry"])
fur.modifiers.new("GN", "NODES").node_group = tree

# Curve instances from geometry nodes on a mesh object (not realized).
sprouts = bpy.data.objects.new("Sprouts", bpy.data.meshes.new("SproutsMesh"))
sprouts.location = (5, 0, 0)
asset.objects.link(sprouts)
tree = bpy.data.node_groups.new("SproutTree", "GeometryNodeTree")
tree.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
n, links = tree.nodes, tree.links
grid = n.new("GeometryNodeMeshGrid")
grid.inputs["Vertices X"].default_value = 3
grid.inputs["Vertices Y"].default_value = 2
to_points = n.new("GeometryNodeMeshToPoints")
line = n.new("GeometryNodeCurvePrimitiveLine")
place = n.new("GeometryNodeInstanceOnPoints")
links.new(grid.outputs["Mesh"], to_points.inputs["Mesh"])
links.new(to_points.outputs["Points"], place.inputs["Points"])
links.new(line.outputs["Curve"], place.inputs["Instance"])
links.new(place.outputs["Instances"], n.new("NodeGroupOutput").inputs["Geometry"])
sprouts.modifiers.new("GN", "NODES").node_group = tree


def emitter(name, location, children, hidden):
    data = surface.data.copy()
    obj = bpy.data.objects.new(name, data)
    obj.location = location
    obj.rotation_euler = (0.3, 0.1, 0.2)
    asset.objects.link(obj)
    system = obj.modifiers.new("Hair", "PARTICLE_SYSTEM").particle_system
    s = system.settings
    s.type, s.count, s.hair_length = "HAIR", 25, 0.4
    s.render_step = s.display_step = 2
    s.root_radius, s.tip_radius, s.radius_scale, s.shape = 1.0, 0.2, 0.05, 0.3
    s.use_close_tip = not children
    s.material = 2
    s.use_advanced_hair = True
    if children:
        s.child_type = "INTERPOLATED"
        s.child_percent = s.rendered_child_count = 3
    obj.show_instancer_for_render = not hidden
    return obj


emitter("Furry", (0, 4, 0), False, False)
emitter("Furrier", (0, 8, 0), True, True)
mixed = bpy.data.collections.new("Mixed")
scene.collection.children.link(mixed)
mixed.objects.link(bpy.data.objects.new("MixedHair", simple))
body = bpy.data.objects.new("Body", bpy.data.meshes.new("Body"))
body.data.from_pydata([(0, 0, 0), (1, 0, 0), (1, 1, 0)], [], [(0, 1, 2)])
mixed.objects.link(body)
path = out + "/hair.blend"
bpy.ops.wm.save_as_mainfile(filepath=path)

# Blender's own numbers. Display settings equal the render ones above, so the viewport
# depsgraph evaluates the same strands as the export's render depsgraph.
depsgraph = bpy.context.evaluated_depsgraph_get()
reference = dict(curves={}, particles={}, sprouts=0)
for name in ("SimpleHair", "PolyHair", "GNHair"):
    data = bpy.data.objects[name].evaluated_get(depsgraph).data
    count, points = len(data.curves), len(data.points)
    offsets = np.empty(count + 1, np.int32)
    data.curve_offset_data.foreach_get("value", offsets)
    attributes = {}
    kinds = {"FLOAT_COLOR": ("color", 4), "FLOAT_VECTOR": ("vector", 3), "FLOAT2": ("vector", 2), "FLOAT": ("value", 1)}
    for attribute in data.attributes:
        if attribute.name.startswith(".") or attribute.data_type not in kinds:
            continue
        prop, width = kinds[attribute.data_type]
        values = np.empty(len(attribute.data) * width, np.float32)
        attribute.data.foreach_get(prop, values)
        attributes[attribute.name] = dict(domain=attribute.domain, values=values.tolist())
    reference["curves"][name] = dict(offsets=offsets.tolist(), attributes=attributes, slots=len(data.materials),
                                     order=[(a.name, a.domain, a.data_type) for a in data.attributes])
for name in ("Furry", "Furrier"):
    obj = bpy.data.objects[name].evaluated_get(depsgraph)
    system = obj.particle_systems[0]
    s = system.settings
    parents, children = len(system.particles), len(system.child_particles)
    first = parents if children else 0
    steps = (1 << s.render_step) + 1
    inverse = np.linalg.inv(np.array(Matrix(obj.matrix_world)))
    world = np.array([[list(system.co_hair(obj, particle_no=i, step=k)) for k in range(steps)]
                      for i in range(first, parents + children)])
    local = world @ inverse[:3, :3].T + inverse[:3, 3]
    modifier = obj.modifiers["Hair"]
    uvs = [list(system.uv_on_emitter(modifier, particle=system.particles[i] if i < parents else system.particles[0],
                                     particle_no=i, uv_no=0)) for i in range(first, parents + children)]
    reference["particles"][name] = dict(
        parents=parents, children=children, steps=steps, local=local.tolist(), uv=uvs,
        texspace=[list(obj.data.texspace_location), list(obj.data.texspace_size)],
        settings=[s.root_radius, s.tip_radius, s.radius_scale, s.shape, s.use_close_tip, s.material],
    )
reference["sprouts"] = sum(1 for i in depsgraph.object_instances
                           if i.is_instance and i.parent and i.parent.original.name == "Sprouts"
                           and i.object.type == "CURVE")

requests = [dict(source=path, selector_type="COLLECTION", selector="Asset", frame=1.0),
            dict(source=path, selector_type="COLLECTION", selector="Mixed", frame=1.0),
            dict(source=path, selector_type="OBJECT", selector="GNHair", frame=1.0, viewport_hulls=True)]
result = dict(reference=reference)
with tempfile.TemporaryDirectory() as textures:
    for name, value in zip(("Asset", "Mixed", "GNHair"), blend_export.evaluate_many(requests, textures)):
        if isinstance(value, Exception):
            result[name] = dict(error=str(value))
            continue
        manifest, arrays = value
        hair = {}
        for record in manifest.get("hair", []):
            hair[record["key"]] = dict(
                record,
                values={k: np.asarray(arrays[v]).tolist() for k, v in record["arrays"].items()},
                attribute_values={f["name"]: np.asarray(arrays[f["array"]]).tolist() for f in record["attributes"]},
            )
        result[name] = dict(
            hair=hair,
            meshes=[m["key"] for m in manifest["meshes"]],
            instances=[dict(name=i["name"], key=i["mesh_key"], geometry_type=i.get("geometry_type", "mesh"),
                            materials=i["material_keys"], blender=i.get("blender_object"),
                            transform=i["transform"]) for i in manifest["instances"]],
            diagnostics=manifest["diagnostics"],
            hulls=manifest.get("viewport_hulls"),
        )
print("RESULT:" + json.dumps(result))
"""


@pytest.fixture(scope="module")
def exported(tmp_path_factory):
    if importlib.util.find_spec("bpy") is None:
        pytest.skip("bpy (Blender as a Python module) is not installed")
    folder = tmp_path_factory.mktemp("blend_hair")
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


def _instances(case, name):
    return [i for i in case["instances"] if i["name"] == name]


def _hair(case, name, geometry_type):
    (instance,) = [i for i in _instances(case, name) if i["geometry_type"] == geometry_type]
    return case["hair"][instance["key"]], instance


def blender_random(index):
    """hash_uint2_to_float(index, 0) (intern/cycles/util/hash.h), scalar transcription."""
    from cycles_houdini.blender.blend_ids import hash_uint2

    return float(np.float32(hash_uint2(index, 0)) / np.float32(0xFFFFFFFF))


def blender_intercept(positions, offsets):
    """export_hair_curves / ExportCurveSegments: normalized length along each curve, and the
    curve length (float32, as Cycles accumulates it)."""
    positions = np.asarray(positions, np.float32)
    intercept, lengths = np.zeros(len(positions), np.float32), []
    for start, end in zip(offsets[:-1], offsets[1:]):
        length = np.float32(0)
        for point in range(start + 1, end):
            length += np.float32(np.linalg.norm(positions[point] - positions[point - 1]))
            intercept[point] = length
        if length > 0:
            intercept[start + 1 : end] /= length
        lengths.append(float(length))
    return intercept, lengths


# ---------------------------------------------------------------- Curves objects


@pytest.mark.blender
@pytest.mark.cpu
def test_curves_keys_radius_shape_and_random_equal_blender(exported):
    case = exported["Asset"]
    assert "error" not in case, case
    for name, shape in (("SimpleHair", "THICK"), ("PolyHair", "LINEAR")):
        record, instance = _hair(case, name, "curves")
        blender = exported["reference"]["curves"][name]
        assert record["values"]["offsets"] == blender["offsets"]
        np.testing.assert_array_equal(
            np.asarray(record["values"]["positions"], np.float32).ravel(),
            np.asarray(blender["attributes"]["position"]["values"], np.float32),
        )
        count = len(blender["offsets"]) - 1
        assert record["values"]["randoms"] == pytest.approx([blender_random(i) for i in range(count)], abs=0)
        assert record["shape"] == shape  # the first curve's type; THICK is the scene's hair shape
        assert len(instance["materials"]) == 1  # Blender renders every curve with the first slot
        assert instance["blender"]["visibility"] == 255
    simple, _ = _hair(case, "SimpleHair", "curves")
    assert simple["values"]["radius"] == [pytest.approx(0.005)] * 13  # no radius attribute
    poly, _ = _hair(case, "PolyHair", "curves")
    np.testing.assert_array_equal(
        np.asarray(poly["values"]["radius"], np.float32),
        np.asarray(exported["reference"]["curves"]["PolyHair"]["attributes"]["radius"]["values"], np.float32),
    )


@pytest.mark.blender
@pytest.mark.cpu
def test_curve_attributes_by_name_equal_blender(exported):
    record, _ = _hair(exported["Asset"], "PolyHair", "curves")
    blender = exported["reference"]["curves"]["PolyHair"]["attributes"]
    fields = {field["name"]: field for field in record["attributes"]}
    assert fields["tint"]["domain"] == "CURVE" and fields["weight"]["domain"] == "POINT"
    for name in ("tint", "weight"):
        np.testing.assert_array_equal(
            np.asarray(record["attribute_values"][name], np.float32).ravel(), np.asarray(blender[name]["values"])
        )
    assert [field["name"] for field in record["skipped_attributes"]] == ["quad"]  # FLOAT4
    assert any(
        d["code"] == "blend.hair_attribute" and "quad (FLOAT4)" in d["message"]
        for d in exported["Asset"]["diagnostics"]
    )


@pytest.mark.blender
@pytest.mark.cpu
def test_geometry_nodes_hair_takes_surface_uv_as_the_standard_uv(exported):
    record, _ = _hair(exported["Asset"], "GNHair", "curves")
    blender = exported["reference"]["curves"]["GNHair"]
    assert record["values"]["offsets"] == blender["offsets"] and len(blender["offsets"]) > 10
    assert record["uv_attribute"] == "surface_uv_coordinate"
    # attr_create_generic's rule: the first FLOAT2 Curve attribute is the standard UV.
    assert [n for n, domain, kind in blender["order"] if (domain, kind) == ("CURVE", "FLOAT2")][0] == record[
        "uv_attribute"
    ]
    np.testing.assert_array_equal(
        np.asarray(record["attribute_values"]["surface_uv_coordinate"], np.float32).ravel(),
        np.asarray(blender["attributes"]["surface_uv_coordinate"]["values"], np.float32),
    )
    uv = np.asarray(record["attribute_values"]["surface_uv_coordinate"])
    assert (uv >= 0).all() and (uv <= 1).all() and np.ptp(uv, axis=0).min() > 0.3
    assert record["values"]["radius"] == [pytest.approx(0.01)] * record["point_count"]


@pytest.mark.blender
@pytest.mark.cpu
def test_geometry_nodes_curve_instances_render_nothing_as_in_blender(exported):
    # make_duplis_geometry_set_impl hands instanced curves (and curves of a non-Curves object)
    # to render engines as legacy Curve duplis; Blender's Cycles meshes them (object_to_mesh),
    # which gives nothing without a bevel. Only a Curves object's own curves are hair.
    assert exported["reference"]["sprouts"] == 6
    sprouts = _instances(exported["Asset"], "Sprouts")
    assert [i["geometry_type"] for i in sprouts] == ["mesh"]  # the object's own (empty) mesh only


# ---------------------------------------------------------------- legacy particle hair


@pytest.mark.blender
@pytest.mark.cpu
@pytest.mark.parametrize("name", ["Furry", "Furrier"])
def test_particle_hair_strands_equal_blenders_path_cache(exported, name):
    record, instance = _hair(exported["Asset"], name, "particle_hair")
    blender = exported["reference"]["particles"][name]
    count = blender["children"] or blender["parents"]
    assert blender["parents"] == 25 and blender["children"] == (75 if name == "Furrier" else 0)
    assert record["curve_count"] == count and record["point_count"] == count * blender["steps"]
    offsets = record["values"]["offsets"]
    assert offsets == list(range(0, count * blender["steps"] + 1, blender["steps"]))
    positions = np.asarray(record["values"]["positions"], np.float64)
    np.testing.assert_allclose(positions, np.asarray(blender["local"]).reshape(-1, 3), atol=2e-6)
    # Radius: shaperadius over the normalized length, root/tip times radius scale / 2, closed tip.
    root, tip, scale, shape, close, slot = blender["settings"]
    intercept, lengths = blender_intercept(np.asarray(blender["local"]).reshape(-1, 3), offsets)
    expected = (1.0 - intercept) ** (1.0 / (1.0 - shape)) * (root - tip) * scale * 0.5 + tip * scale * 0.5
    if close:
        expected[np.asarray(offsets[1:]) - 1] = 0.0
    np.testing.assert_allclose(record["values"]["radius"], expected, rtol=1e-5, atol=1e-7)
    assert all(length > 0.3 for length in lengths)
    assert record["values"]["material_slots"] == [slot - 1] * count
    assert len(instance["materials"]) == 2
    assert record["values"]["randoms"] == pytest.approx([blender_random(i) for i in range(count)], abs=0)
    # Root UV from the emitter (BKE_particle_uv_on_emitter), the default UV map.
    assert record["uv_attribute"] == "UVMap"
    np.testing.assert_allclose(record["attribute_values"]["UVMap"], blender["uv"], atol=1e-7)
    # Generated: root key in the emitter's texture space (mesh_texture_space).
    location, size = (np.asarray(v, np.float32) for v in blender["texspace"])
    size = np.where(size != 0, np.float32(0.5) / size, 0).astype(np.float32)
    location = location * size - np.float32(0.5)
    roots = np.asarray(blender["local"], np.float32)[:, 0]
    np.testing.assert_allclose(record["values"]["generated"], roots * size - location, atol=2e-6)


@pytest.mark.blender
@pytest.mark.cpu
def test_hidden_emitter_renders_only_its_hair(exported):
    case = exported["Asset"]
    assert sorted(i["geometry_type"] for i in _instances(case, "Furry")) == ["mesh", "particle_hair"]
    assert [i["geometry_type"] for i in _instances(case, "Furrier")] == ["particle_hair"]
    values = [i["blender"] for i in _instances(case, "Furry")]
    assert values[0] == values[1]  # the hair object has the emitter's per-object values


@pytest.mark.blender
@pytest.mark.cpu
def test_mixed_selection_has_no_unsupported_warning(exported):
    for name in ("Asset", "Mixed"):
        case = exported[name]
        assert "error" not in case, case
        assert not [d for d in case["diagnostics"] if d["code"] == "blend.unsupported_geometry"]
    kinds = sorted(i["geometry_type"] for i in exported["Mixed"]["instances"])
    assert kinds == ["curves", "mesh"]


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
        blend_curves=lambda record, arrays, palette: captured.append((record, arrays, palette)) or object()
    )
    monkeypatch.setitem(sys.modules, "cycles_houdini._native", native)
    monkeypatch.setattr(sys.modules["cycles_houdini"], "_native", native, raising=False)
    return module, captured


def _curves_record(kind="curves"):
    arrays = {
        "h/positions": np.arange(9 * 3, dtype=np.float32).reshape(9, 3),
        "h/offsets": np.array([0, 4, 5, 9], np.int32),  # the middle curve has one key
        "h/radius": np.full(9, 0.01, np.float32),
        "h/randoms": np.array([0.1, 0.2, 0.3], np.float32),
        "h/tint": np.array([[1, 0, 0, 0.5], [0, 1, 0, 1], [0, 0, 1, 0.25]], np.float32),
        "h/uv": np.array([[0.1, 0.2], [0.3, 0.4], [0.5, 0.6]], np.float32),
        "h/weight": np.arange(9, dtype=np.float32),
    }
    record = dict(
        key="h",
        name="h",
        kind=kind,
        shape="THICK",
        uv_attribute="uv",
        arrays=dict(positions="h/positions", offsets="h/offsets", radius="h/radius", randoms="h/randoms"),
        attributes=[
            dict(name="tint", domain="CURVE", data_type="FLOAT_COLOR", array="h/tint"),
            dict(name="uv", domain="CURVE", data_type="FLOAT2", array="h/uv"),
            dict(name="weight", domain="POINT", data_type="FLOAT", array="h/weight"),
        ],
    )
    if kind == "particles":
        arrays["h/generated"] = np.ones((3, 3), np.float32)
        arrays["h/material_slots"] = np.array([0, 3, 1], np.int32)
        record["arrays"].update(generated="h/generated", material_slots="h/material_slots")
    return record, arrays, {key: "id:" + key for key in arrays}


@pytest.mark.pure
@pytest.mark.cpu
def test_native_curves_expand_blender_domains_per_key(assets):
    module, captured = assets
    record, arrays, identities = _curves_record()
    key, _ = module._native_curves(record, arrays, identities, 1)
    assert key.startswith("blend/curves/")
    ((native, local, palette),) = captured
    assert palette == ("",) and native["shape"] == "THICK" and native["generated_attribute"] is False
    np.testing.assert_array_equal(local["first_key"], [0, 4])
    np.testing.assert_array_equal(
        local["positions"], np.concatenate([arrays["h/positions"][:4], arrays["h/positions"][5:]])
    )
    np.testing.assert_array_equal(local["randoms"], np.float32([0.1, 0.3]))  # Blender's index-based values stay
    np.testing.assert_array_equal(local["uv"], np.float32([[0.1, 0.2], [0.5, 0.6]]))
    fields = {field["name"]: local[field["array"]] for field in native["attributes"]}
    np.testing.assert_array_equal(fields["enc:tint"], [[1, 0, 0]] * 4 + [[0, 0, 1]] * 4)
    np.testing.assert_array_equal(fields["__cycles_blender_alpha_enc:tint"].ravel(), [0.5] * 4 + [0.25] * 4)
    np.testing.assert_array_equal(fields["enc:uv"], np.float32([[0.1, 0.2]] * 4 + [[0.5, 0.6]] * 4))
    np.testing.assert_array_equal(fields["enc:weight"].ravel(), [0, 1, 2, 3, 5, 6, 7, 8])
    assert "generated" not in local and "material_slots" not in local
    # Same content: same key, no second conversion; another palette size: another key.
    assert module._native_curves(record, arrays, identities, 1)[0] == key and len(captured) == 1
    assert module._native_curves(record, arrays, identities, 2)[0] != key


@pytest.mark.pure
@pytest.mark.cpu
def test_native_particle_hair_keeps_generated_and_clamps_slots(assets):
    module, captured = assets
    record, arrays, identities = _curves_record("particles")
    module._native_curves(record, arrays, identities, 2)
    ((native, local, palette),) = captured
    assert palette == ("", "") and native["generated_attribute"] is True
    np.testing.assert_array_equal(local["material_slots"], [0, 1])
    np.testing.assert_array_equal(local["generated"], np.ones((2, 3)))


@pytest.mark.pure
@pytest.mark.cpu
def test_curve_random_matches_the_scalar_transcription():
    from cycles_blender.blend_export import curve_randoms

    values = curve_randoms(5000)
    assert values.dtype == np.float32
    assert values.tolist() == [blender_random(i) for i in range(5000)]


@pytest.mark.pure
@pytest.mark.cpu
def test_bounds_and_hulls_include_hair():
    from cycles_houdini.blender import blend_bounds

    arrays = {"h/positions": np.array([[0, 0, 0], [1, 0, 0], [0, 2, 0], [0, 0, 3]], np.float32)}
    manifest = dict(
        source="a.blend",
        selector_type="OBJECT",
        selector="Hair",
        frame=1.0,
        dependency_versions={},
        materials=[],
        meshes=[],
        hair=[dict(key="h", arrays=dict(positions="h/positions"), max_radius=0.5)],
        instances=[dict(mesh_key="h", transform=[1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1])],
    )
    record = blend_bounds.evaluated_record(manifest, arrays)
    assert record["known"] and record["bounds"] == pytest.approx([-0.5, -0.5, -0.5, 1.5, 2.5, 3.5])


@pytest.mark.blender
@pytest.mark.cpu
def test_hair_only_object_gets_a_viewport_hull_of_its_keys(exported):
    case = exported["GNHair"]
    assert "error" not in case, case
    hulls = case["hulls"]
    assert hulls is not None and len(hulls["members"]) == 1 and hulls["whole"]["triangles"]
    (record,) = case["hair"].values()
    keys = np.asarray(record["values"]["positions"], np.float64)
    points = np.asarray(hulls["members"][0]["points"]).reshape(-1, 3)
    matrix = np.asarray(case["instances"][0]["transform"]).reshape(4, 4)
    local = (points - matrix[:3, 3]) @ np.linalg.inv(matrix[:3, :3]).T
    # Hull corners are strand keys; the hull spans the strands' extent.
    assert np.abs(local.min(axis=0) - keys.min(axis=0)).max() < 1e-5
    assert np.abs(local.max(axis=0) - keys.max(axis=0)).max() < 1e-5
