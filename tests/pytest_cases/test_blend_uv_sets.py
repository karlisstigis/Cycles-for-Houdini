# SPDX-License-Identifier: Apache-2.0
"""UV maps and UV attributes on Blender import (task U): blend_export.uv_sets decides which UV
sets a mesh carries and which keep their Blender names; blend_convert.mesh_geo writes them.

Pure cases cover the decision and the converter on synthetic records; the bpy case builds real
meshes (UV maps, a geometry-nodes style Face Corner vector attribute read by an Attribute node,
attributes only named "uv", a Point attribute, a node-group reference) and checks the exporter
records for converter and render requests and the Houdini attributes the converter writes.
"""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------- the pure decision


def _sets(layers=(), default=None, attributes=(), referenced=()):
    from cycles_blender.blend_export import uv_sets

    return uv_sets(list(layers), default, list(attributes), set(referenced))


@pytest.mark.pure
@pytest.mark.cpu
def test_single_uv_map_not_named_by_a_material_is_only_uv():
    plan = _sets(["UVMap"], "UVMap")
    assert plan == dict(default="UVMap", source="uv_layer", attributes=[], keep=set())


@pytest.mark.pure
@pytest.mark.cpu
def test_uv_map_named_by_a_material_keeps_its_name():
    assert _sets(["UVMap"], "UVMap", referenced={"UVMap"})["keep"] == {"UVMap"}


@pytest.mark.pure
@pytest.mark.cpu
def test_two_uv_maps_render_active_is_default_other_keeps_name():
    plan = _sets(["Detail", "Main"], "Main")
    assert plan["default"] == "Main" and plan["keep"] == {"Detail"}
    # An unknown default falls back to the first map.
    assert _sets(["Detail", "Main"], None)["default"] == "Detail"


@pytest.mark.pure
@pytest.mark.cpu
def test_attribute_detection_rules():
    attributes = [
        ("UVMap", "CORNER", "FLOAT_VECTOR"),  # read by a material
        (".vs.UVMap", "CORNER", "BOOLEAN"),  # dot-prefixed and boolean
        (".uvhidden", "CORNER", "FLOAT_VECTOR"),  # dot-prefixed
        ("uv_flag", "CORNER", "BOOLEAN"),  # wrong type
        ("uv_seam", "EDGE", "BOOLEAN"),  # wrong domain and type
        ("face_uv", "FACE", "FLOAT2"),  # wrong domain
        ("wind", "CORNER", "FLOAT_VECTOR"),  # read by a material, but no "uv": never a UV set
        ("offset", "POINT", "FLOAT_VECTOR"),  # neither read nor named uv
        ("PointUV", "POINT", "FLOAT2"),  # name contains uv
        ("zz_uv", "CORNER", "FLOAT_VECTOR"),  # name contains uv, read by a material
    ]
    plan = _sets(attributes=attributes, referenced={"UVMap", "wind", "zz_uv"})
    assert plan["attributes"] == ["UVMap", "PointUV", "zz_uv"]
    # A candidate read by a material wins (first by name among those read) and keeps its name.
    assert plan["default"] == "UVMap" and plan["source"] == "attribute"
    assert plan["keep"] == {"UVMap", "PointUV", "zz_uv"}
    plan = _sets(attributes=attributes, referenced={"wind", "zz_uv"})
    assert plan["default"] == "zz_uv" and plan["keep"] == {"UVMap", "PointUV", "zz_uv"}
    # An attribute read by a material without "uv" in its name never becomes uv.
    assert _sets(attributes=[("wind", "CORNER", "FLOAT_VECTOR")], referenced={"wind"})["default"] is None


@pytest.mark.pure
@pytest.mark.cpu
def test_default_attribute_order_without_material_reads():
    vector = "FLOAT_VECTOR"
    # "map1" has no "uv" in its name, so it is never a candidate (only a real UV map can be map1).
    names = ["zz_uv", "map1", "UV", "uv", "UVMap"]
    for expected in ("UVMap", "uv", "UV", "zz_uv"):
        plan = _sets(attributes=[(name, "CORNER", vector) for name in names])
        assert plan["default"] == expected and expected not in plan["keep"] and "map1" not in plan["attributes"]
        names.remove(expected)
    plan = _sets(attributes=[("b_uv", "CORNER", vector), ("a_uv", "POINT", "FLOAT2")])
    assert plan["default"] == "a_uv" and plan["keep"] == {"b_uv"}
    # Real UV maps win over attributes; attributes then keep their names.
    plan = _sets(["Main"], "Main", attributes=[("UVMap", "CORNER", vector)])
    assert plan["default"] == "Main" and plan["keep"] == {"UVMap"}
    assert _sets(attributes=[("offset", "CORNER", vector)])["default"] is None


# ---------------------------------------------------------------- converter on synthetic records


def _record(uv_layers=(), attributes=(), default_uv=None, source=None):
    arrays = {}

    def add(field, value):
        arrays["m/" + field] = np.asarray(value)
        return "m/" + field

    record = dict(
        key="m",
        name="Tri",
        vertex_count=3,
        polygon_count=1,
        corner_count=3,
        arrays=dict(
            positions=add("positions", np.array([(0, 0, 0), (1, 0, 0), (0, 1, 0)], np.float32)),
            edges=add("edges", np.array([(0, 1), (1, 2), (2, 0)], np.int32)),
            polygon_counts=add("polygon_counts", np.array([3], np.int32)),
            vertex_indices=add("vertex_indices", np.array([0, 1, 2], np.int32)),
            material_indices=add("material_indices", np.array([0], np.int32)),
            smooth=add("smooth", np.array([1], np.uint8)),
        ),
        uv_layers=[dict(layer, array=add(layer["name"], layer.pop("values"))) for layer in map(dict, uv_layers)],
        attributes=[dict(field, array=add(field["name"], field.pop("values"))) for field in map(dict, attributes)],
        default_uv=default_uv,
        default_uv_source=source,
        color_attributes=[],
    )
    return record, arrays


def _convert(record, arrays):
    from cycles_houdini.blender import blend_convert as bc

    report = bc.Report()
    geo = bc.mesh_geo(record, arrays, [], {}, report)
    return geo.attribs, report


CORNER_UV = np.array([(0.1, 0.2), (0.3, 0.4), (0.5, 0.6)], np.float32)
# Houdini reverses the winding keeping the first corner: corners 0, 2, 1.
HOUDINI_ORDER = [0, 2, 1]


@pytest.mark.pure
@pytest.mark.cpu
def test_converter_single_map_unnamed_and_named():
    layer = dict(name="UVMap", values=CORNER_UV, active_render=True, keep_name=False)
    attribs, report = _convert(*_record([layer], default_uv="UVMap", source="uv_layer"))
    assert sorted(n for n, a in attribs.items() if a.owner == "vertex" and n != "N") == ["uv"]
    assert np.allclose(attribs["uv"].values, np.c_[CORNER_UV[HOUDINI_ORDER], np.zeros(3)])
    assert "Tri: UVs: uv (from UVMap)" in report.notes
    attribs, report = _convert(*_record([dict(layer, keep_name=True)], default_uv="UVMap", source="uv_layer"))
    assert {"uv", "UVMap"} <= set(attribs) and np.allclose(attribs["UVMap"].values, attribs["uv"].values)
    assert "Tri: UVs: uv (from UVMap), UVMap" in report.notes


@pytest.mark.pure
@pytest.mark.cpu
def test_converter_uv_attribute_is_not_axis_turned_and_point_domain_is_promoted():
    vector = np.c_[CORNER_UV, np.full(3, 0.25, np.float32)]
    attributes = [
        dict(name="UVMap", domain="CORNER", data_type="FLOAT_VECTOR", values=vector, uv_set=True, keep_name=True),
        dict(name="velocity", domain="POINT", data_type="FLOAT_VECTOR", values=np.ones((3, 3), np.float32)),
        dict(
            name="ptuv",
            domain="POINT",
            data_type="FLOAT2",
            values=np.array([(1, 2), (3, 4), (5, 6)], np.float32),
            uv_set=True,
            keep_name=True,
        ),
    ]
    attribs, report = _convert(*_record(attributes=attributes, default_uv="UVMap", source="attribute"))
    expected = vector[HOUDINI_ORDER]
    for name in ("uv", "UVMap"):
        attrib = attribs[name]
        assert attrib.owner == "vertex" and attrib.kind == "float" and attrib.width == 3
        assert not attrib.vector and not attrib.normal and np.allclose(attrib.values, expected)
    # Same form as uv; points promoted to vertices.
    assert attribs["ptuv"].owner == "vertex" and not attribs["ptuv"].vector
    assert np.allclose(attribs["ptuv"].values, [(1, 2, 0), (5, 6, 0), (3, 4, 0)])
    # Generic attributes that are not UVs are unchanged (velocity -> v, axis turned).
    assert attribs["v"].vector and np.allclose(attribs["v"].values, [(1, 1, -1)] * 3)
    assert "Tri: UVs: uv (from UVMap), UVMap, ptuv" in report.notes


@pytest.mark.pure
@pytest.mark.cpu
def test_converter_uv_set_names():
    from cycles_houdini.blender import blend_convert as bc

    used = {"uv"}
    assert [bc.uv_set_name(n, used) for n in ("uv2", "UVMap.001", "uv", "UV", "P", "UV")] == [
        "uv2",
        "UVMap_001",
        "blend_uv",
        "UV",
        "blend_P",
        "UV_2",
    ]


# ---------------------------------------------------------------- real Blender meshes (bpy)

SCRIPT = r"""
import json, sys, tempfile
# argv[1]: <checkout>/houdini/python3.13libs; the Blender component is <checkout>/blender.
sys.path[:0] = [sys.argv[1], sys.argv[1] + "/../../blender"]
import bpy
from cycles_blender import blend_export
from cycles_houdini.blender import blend_convert

out = sys.argv[2]
bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene
CORNER_UV = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8]


def quad(name, materials=()):
    me = bpy.data.meshes.new(name)
    me.from_pydata([(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)], [], [(0, 1, 2, 3)])
    for material in materials:
        me.materials.append(material)
    scene.collection.objects.link(bpy.data.objects.new(name, me))
    return me


def uv_map(me, name, values=CORNER_UV):
    layer = me.uv_layers.new(name=name)
    layer.data.foreach_set("uv", values)
    return layer


def attribute(me, name, kind, domain, values):
    field = me.attributes.new(name, kind, domain)
    field.data.foreach_set("value" if kind == "BOOLEAN" else "vector", values)


def material(name, build):
    result = bpy.data.materials.new(name)
    result.use_nodes = True
    build(result.node_tree)
    return result


def texcoord(tree):
    tree.nodes.new("ShaderNodeTexCoord")


def uvnode(tree):
    tree.nodes.new("ShaderNodeUVMap").uv_map = "UVMap"


def attribute_node(name):
    def build(tree):
        node = tree.nodes.new("ShaderNodeAttribute")
        node.attribute_type, node.attribute_name = "GEOMETRY", name
        mapping = tree.nodes.new("ShaderNodeMapping")
        tree.links.new(node.outputs["Vector"], mapping.inputs["Vector"])
    return build


def grouped(tree):
    group = bpy.data.node_groups.new("ReadInner", "ShaderNodeTree")
    attribute_node("zz_uv")(group)
    tree.nodes.new("ShaderNodeGroup").node_tree = group


vectors = [0.1, 0.2, 0.0, 0.3, 0.4, 0.0, 0.5, 0.6, 0.0, 0.7, 0.8, 0.0]
uv_map(quad("Single", [material("TexCoord", texcoord)]), "UVMap")
uv_map(quad("Named", [material("UVNode", uvnode)]), "UVMap")
two = quad("Two")
uv_map(two, "UVMap")
uv_map(two, "Detail", [0.9] * 8)
two.uv_layers["UVMap"].active_render = True
sapling = quad("Sapling", [material("Sapling", attribute_node("UVMap"))])
attribute(sapling, "UVMap", "FLOAT_VECTOR", "CORNER", vectors)
attribute(sapling, ".vs.UVMap", "BOOLEAN", "CORNER", [False] * 4)
named_only = quad("NamedOnly")
attribute(named_only, "my_uv", "FLOAT_VECTOR", "CORNER", vectors)
attribute(named_only, ".uvhidden", "FLOAT_VECTOR", "CORNER", vectors)
attribute(named_only, "uv_flag", "BOOLEAN", "CORNER", [True] * 4)
attribute(quad("PointDomain"), "uvpoint", "FLOAT2", "POINT", [0, 0, 1, 0, 1, 1, 0, 1])
grouped_me = quad("Grouped", [material("Grouped", grouped)])
attribute(grouped_me, "zz_uv", "FLOAT_VECTOR", "CORNER", vectors)
attribute(grouped_me, "aa_uv", "FLOAT_VECTOR", "CORNER", [0.9] * 12)
attribute(quad("Wind", [material("Wind", attribute_node("wind"))]), "wind", "FLOAT_VECTOR", "CORNER", vectors)
# Every generic attribute (artist rule U5): domains, types, a reserved-name clash, internals.
every = quad("AllAttrs", [material("Paint", lambda tree: setattr(tree.nodes.new("ShaderNodeVertexColor"), "layer_name", "Paint"))])
def generic(name, kind, domain, prop, values):
    every.attributes.new(name, kind, domain).data.foreach_set(prop, values)
generic("temp", "FLOAT", "POINT", "value", [1, 2, 3, 4])
generic("ci8", "INT8", "CORNER", "value", [1, 2, 3, 4])
generic("fb", "BOOLEAN", "FACE", "value", [True])
generic("fcol", "FLOAT_COLOR", "FACE", "color", [0.1, 0.2, 0.3, 0.4])
generic("spin", "QUATERNION", "POINT", "value", [1, 0, 0, 0, 0.5, 0.1, 0.2, 0.3] * 2)
generic("mat4", "FLOAT4X4", "FACE", "value", list(range(16)))
generic("i2", "INT32_2D", "CORNER", "value", [1, 2] * 4)
generic("f4", "FLOAT4", "POINT", "vector", [1, 2, 3, 4] * 4)
generic("ew", "FLOAT", "EDGE", "value", [1, 2, 3, 4])
generic(".hidden", "FLOAT", "POINT", "value", [1, 2, 3, 4])
generic("P", "FLOAT", "POINT", "value", [5, 6, 7, 8])
generic("rot", "FLOAT", "POINT", "value", [1, 1, 1, 1])  # a Houdini name with a fixed meaning
generic("my attr", "FLOAT", "FACE", "value", [9])
label = every.attributes.new("label", "STRING", "POINT")
for element, text in enumerate((b"a", b"b", b"a", b"")):
    label.data[element].value = text
paint = every.color_attributes.new("Paint", "BYTE_COLOR", "CORNER")
every.color_attributes.new("Col2", "FLOAT_COLOR", "POINT")
every.color_attributes.render_color_index = 0
path = out + "/uv_sets.blend"
bpy.ops.wm.save_as_mainfile(filepath=path)

cases = ("Single", "Named", "Two", "Sapling", "NamedOnly", "PointDomain", "Grouped", "Wind", "AllAttrs")
flags = dict(vertex_groups=True, object_tree=True, material_export="names", native_subdivision=False)
requests = [dict(source=path, selector_type="OBJECT", selector=name, frame=1.0, **flags) for name in cases]
requests.append(dict(source=path, selector_type="OBJECT", selector="Sapling", frame=1.0))
requests.append(dict(source=path, selector_type="OBJECT", selector="Wind", frame=1.0))
# The removed switch's request field: ignored, Link renders stay Blender-exact.
requests.append(dict(source=path, selector_type="OBJECT", selector="Sapling", frame=1.0, uv_attributes="promote"))
render_cases = ("SaplingRender", "WindRender", "SaplingPromote")
result = {}
with tempfile.TemporaryDirectory() as textures:
    for name, value in zip(cases + render_cases, blend_export.evaluate_many(requests, textures)):
        if isinstance(value, Exception):
            result[name] = {"error": repr(value)}
            continue
        manifest, arrays = value
        [record] = manifest["meshes"]
        report = blend_convert.Report()
        geo = blend_convert.mesh_geo(record, arrays, [], {}, report)
        result[name] = dict(
            default_uv=record["default_uv"],
            source=record["default_uv_source"],
            layers={layer["name"]: layer["keep_name"] for layer in record["uv_layers"]},
            uv_attributes={f["name"]: f["keep_name"] for f in record["attributes"] if f.get("uv_set")},
            exported=sorted(f["name"] for f in record["attributes"]),
            vertex={n: a.values.tolist() for n, a in geo.attribs.items() if a.owner == "vertex" and n != "N"},
            attribs={
                n: dict(owner=a.owner, kind=a.kind, width=a.width, values=a.values if a.kind == "string" else a.values.tolist())
                for n, a in geo.attribs.items()
            },
            skipped=report.skipped,
            renamed=report.renamed,
            vector={n: bool(a.vector) for n, a in geo.attribs.items()},
            notes=report.notes,
            diagnostics=[d for d in manifest.get("diagnostics", []) if d.get("code") == "blend.uv_attribute"],
        )
print("RESULT:" + json.dumps(result, default=str))
"""


@pytest.fixture(scope="module")
def exported(tmp_path_factory):
    if importlib.util.find_spec("bpy") is None:
        pytest.skip("bpy (Blender as a Python module) is not installed")
    folder = tmp_path_factory.mktemp("blend_uv_sets")
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
    result = json.loads(lines[-1][len("RESULT:") :])
    assert not [name for name, value in result.items() if "error" in value], result
    return result


# Houdini corner order of a Blender quad (winding reversed, first corner kept).
QUAD_UV = [[0.1, 0.2, 0.0], [0.7, 0.8, 0.0], [0.5, 0.6, 0.0], [0.3, 0.4, 0.0]]


@pytest.mark.parametrize(
    ("case", "names"),
    [
        ("Single", ["uv"]),  # one real UV map, not named by the shader
        ("Named", ["UVMap", "uv"]),  # a UV Map node names it
        ("Two", ["Detail", "uv"]),  # the other map keeps its name, no uv2
        ("Sapling", ["UVMap", "uv"]),  # no UV maps; Attribute node reads the vector attribute
        ("NamedOnly", ["uv"]),  # no UV maps; "uv" in the name, not read by a shader
        ("PointDomain", ["uv"]),  # Point FLOAT2 attribute promoted to vertices
        ("Grouped", ["aa_uv", "uv", "zz_uv"]),  # zz_uv is read inside a node group: it is uv
    ],
)
@pytest.mark.blender
@pytest.mark.cpu
def test_bpy_uv_sets(exported, case, names):
    result = exported[case]
    vertex = result["vertex"]
    assert sorted(n for n in vertex if not n.startswith("uv_flag")) == names
    expected = [[0, 0, 0], [0, 1, 0], [1, 1, 0], [1, 0, 0]] if case == "PointDomain" else QUAD_UV
    assert np.allclose(vertex["uv"], expected)
    for name in names:
        assert not result["vector"][name]


@pytest.mark.blender
@pytest.mark.cpu
def test_bpy_records(exported):
    assert exported["Single"]["layers"] == {"UVMap": False} and exported["Named"]["layers"] == {"UVMap": True}
    assert exported["Two"]["layers"] == {"UVMap": False, "Detail": True}
    sapling = exported["Sapling"]
    assert (sapling["default_uv"], sapling["source"], sapling["uv_attributes"]) == (
        "UVMap",
        "attribute",
        {"UVMap": True},
    )
    assert ".vs.UVMap" not in sapling["exported"]
    assert sapling["notes"] == ["Sapling: UVs: uv (from UVMap), UVMap"]
    assert [d["message"] for d in sapling["diagnostics"]] == ["UVs: uv (from UVMap), UVMap"]
    # Link renders follow Blender exactly (audit item 16, no switch): a vector attribute is never
    # a UV map, so there is no default UV and "UVMap" stays a plain named attribute; the record
    # says so in an info note. The removed switch's request field changes nothing.
    for case in ("SaplingRender", "SaplingPromote"):
        render = exported[case]
        assert (render["default_uv"], render["source"], render["uv_attributes"]) == (None, None, {})
        assert "UVMap" in render["exported"]
        [note] = render["diagnostics"]
        assert note["severity"] == "info" and note["message"].startswith("No UV map; UVMap stays a named attribute")
    named_only = exported["NamedOnly"]
    assert named_only["uv_attributes"] == {"my_uv": False}
    # Dot-prefixed attributes are not exported; a boolean "uv" attribute stays a plain attribute.
    assert ".uvhidden" not in named_only["exported"] and "uv_flag" in named_only["exported"]
    assert "uv_flag" in named_only["vertex"] and "uv_flag" not in named_only["uv_attributes"]
    assert exported["PointDomain"]["uv_attributes"] == {"uvpoint": False}
    assert exported["Grouped"]["default_uv"] == "zz_uv"
    assert exported["Grouped"]["uv_attributes"] == {"zz_uv": True, "aa_uv": True}
    assert exported["Single"]["diagnostics"] == []


@pytest.mark.blender
@pytest.mark.cpu
def test_bpy_attribute_read_by_shader_without_uv_in_name_is_not_a_uv(exported):
    # A Face Corner vector "wind" read by an Attribute node: never a UV set, never uv. It stays
    # the plain attribute it was before (converter: vertex "wind", turned with the axes).
    for case in ("Wind", "WindRender"):
        wind = exported[case]
        assert wind["default_uv"] is None and wind["source"] is None and wind["uv_attributes"] == {}
        assert "wind" in wind["exported"] and wind["diagnostics"] == [] and wind["notes"] == []
        assert "uv" not in wind["vertex"] and wind["vector"]["wind"]
    assert np.allclose(
        exported["Wind"]["vertex"]["wind"], [[0.1, 0.0, -0.2], [0.7, 0.0, -0.8], [0.5, 0.0, -0.6], [0.3, 0.0, -0.4]]
    )


@pytest.mark.blender
@pytest.mark.cpu
def test_bpy_every_generic_attribute_arrives(exported):
    every = exported["AllAttrs"]
    attribs = every["attribs"]

    def check(name, owner, kind, width):
        assert (attribs[name]["owner"], attribs[name]["kind"], attribs[name]["width"]) == (owner, kind, width), name
        return attribs[name]["values"]

    assert check("temp", "point", "float", 1) == [[1], [2], [3], [4]]
    assert check("ci8", "vertex", "int", 1) == [[1], [4], [3], [2]]  # winding reversed
    assert check("fb", "prim", "int", 1) == [[1]]  # boolean as 0/1
    assert np.allclose(check("fcol", "prim", "float", 4), [[0.1, 0.2, 0.3, 0.4]])
    # Quaternions: Blender w, x, y, z -> Houdini x, y, z, w with the axis part turned (x, z, -y).
    assert np.allclose(check("spin", "point", "float", 4)[:2], [[0, 0, 0, 1], [0.1, 0.3, -0.2, 0.5]])
    assert check("mat4", "prim", "float", 16) and check("i2", "vertex", "int", 2) == [[1, 2]] * 4
    assert check("f4", "point", "float", 4) == [[1, 2, 3, 4]] * 4
    assert check("label", "point", "string", 1) == ["a", "b", "a", ""]
    # Clash with a name the converter writes itself: blend_P; invalid names are sanitised.
    assert check("blend_P", "point", "float", 1) == [[5], [6], [7], [8]]
    assert every["renamed"]["P"] == "blend_P" and every["renamed"]["my attr"] == "my_attr"
    assert every["renamed"]["rot"] == "blend_rot" and "rot" not in attribs
    assert check("my_attr", "prim", "float", 1) == [[9]]
    # Edge attributes and dot-prefixed internals are not imported; edges are reported.
    assert "ew" not in attribs and not [n for n in attribs if "hidden" in n]
    assert any(text.startswith("ew (EDGE FLOAT)") for text in every["skipped"])
    # Colour attributes: render colour -> Cd, also Paint because the material names it; Col2 by name.
    assert {"Cd", "Paint", "Col2"} <= set(attribs) and attribs["Col2"]["width"] == 4
    assert np.allclose(np.array(attribs["Cd"]["values"]), np.array(attribs["Paint"]["values"])[:, :3])


@pytest.mark.pure
@pytest.mark.cpu
def test_converter_matrix_axis_change():
    from cycles_houdini.blender import blend_convert as bc

    # A Blender translation by (1, 2, 3) is a Houdini translation by (1, 3, -2) (row vectors: last row).
    blender = np.eye(4)
    blender[:3, 3] = (1, 2, 3)
    memory = blender.T.reshape(1, 16)  # Blender stores column-major
    geo = bc.Geo()
    builder = bc._Builder(geo, bc.Report())
    builder.generic(dict(name="m", domain="FACE", data_type="FLOAT4X4"), memory, "prim")
    houdini = geo.attribs["m"].values.reshape(4, 4)
    assert np.allclose(houdini[3, :3], (1, 3, -2)) and np.allclose(houdini[:3, :3], np.eye(3))
