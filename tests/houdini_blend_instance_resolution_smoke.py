# SPDX-License-Identifier: Apache-2.0
"""Public native capture: mixed OBJ/Blender references and per-point absolute frames.

The loader is replaced with a small authored native mesh fixture. Exporter fidelity has
its own Blender comparison tests; this fixture isolates public request resolution,
native direct transforms, omission/recovery, and dependency observation.
"""

import argparse
import json
import math
from pathlib import Path

import hou
from cycles_houdini import _native, native_scene, instancing, object_scene, blender_assets
from blend_direct_fixture import mesh, prototype as direct_prototype

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--out", required=True, type=Path)
parser.add_argument("--device", default="CPU")
args = parser.parse_args()
args.out.mkdir(parents=True, exist_ok=True)
hou.setFrame(12.5)
now = hou.time()
root = hou.node("/obj").createNode("subnet", "blend_resolution_test")
ordinary = root.createNode("geo", "ordinary.blend", run_init_scripts=False)
box = ordinary.createNode("box")
box.setRenderFlag(True)
box.setDisplayFlag(True)
ordinary.parm("tx").set(7)
source_obj = root.createNode("geo", "mixed_instances", run_init_scripts=False)
author_source = source_obj.createNode("python", "instance_points")
cast = source_obj.createNode("attribcast", "index_storage")
cast.setInput(0, author_source)
cast.setParms(dict(attribs1="index", class1="point"))
source = source_obj.createNode("python", "index_values")
source.setInput(0, cast)
source.setRenderFlag(True)
source.setDisplayFlag(True)
owned = root.createNode("subnet", "owned_imports")
owned.setUserData("cycles_external_blend", "1")
rop = hou.node("/out").createNode("cycles_ipr", "blend_resolution_test")
rop.setParms(
    dict(source_mode="objects", objectroot=root.path(), vobject="*", forceobject="*", use_material_assignments=1)
)
asset = args.out / "fixture.blend"
asset.write_bytes(b"fixture-v1")
reference = asset.resolve().as_posix() + "#object=Cube"
assert instancing.is_blend_reference(reference)
assert not instancing.is_blend_reference(ordinary.path())
assert not instancing.is_blend_reference(str(asset) + "/#object=Cube")

calls, prepared, unavailable, checks = [], {}, set(), []
subdivision_modes = []


def prepare(value, frame, *, native_subdivision=True):
    calls.append((value, frame))
    subdivision_modes.append(native_subdivision)
    if value in unavailable:
        raise ValueError("fixture source is temporarily unavailable")
    key = (value, frame)
    if key not in prepared:
        resource = "resolution/mesh/" + repr(frame)
        prepared[key] = direct_prototype(resource, mesh(resource), (frame * 0.1, 1.0, 0.0), [str(asset.resolve())])
        if value.endswith("#collection=Pair"):
            member = dict(prepared[key]["instances"][0])
            member.update(
                key=resource + "/second", transform=[1, 0, 0, -frame * 0.1, 0, 1, 0, -1.0, 0, 0, 1, 0, 0, 0, 0, 1]
            )
            prepared[key]["instances"].append(member)
    return prepared[key]


def author(values, indices=None, integer=False, float64=False):
    configuration = dict(values=values, indices=indices, integer=integer)
    author_source.parm("python").set(
        """import hou
c = """
        + repr(configuration)
        + """
g = hou.pwd().geometry()
g.addAttrib(hou.attribType.Point, 'instance', '')
if c['indices'] is not None:g.addAttrib(hou.attribType.Point,'index',0 if c['integer'] else 0.)
for i, value in enumerate(c['values']):
    point=g.createPoint();point.setPosition((i*2,0,0));point.setAttribValue('instance',value)
    if c['indices'] is not None:point.setAttribValue('index',c['indices'][i])
"""
    )
    cast.parm("precision1").set("fpreal64" if float64 else "unchanged")
    code = "import hou\ng=hou.pwd().geometry()\n"
    if float64:
        code += (
            "values="
            + repr(indices)
            + '\nfor point, value in zip(g.points(),values):point.setAttribValue("index",value)\n'
        )
    source.parm("python").set(code)
    source.cook(force=True)
    assert not source.errors(), source.errors()
    if float64:
        assert source.geometry().findPointAttrib("index").numericDataType() == hou.numericData.Float64


def capture():
    diagnostics = []
    request = native_scene.geometry_requests_for_rop(rop, source, now, diagnostics=diagnostics)[-1]
    table = _native.point_instance_table(request, now)
    return request, table, diagnostics


original = blender_assets.prepare_prototype
original_many = blender_assets.prepare_prototypes
original_warmup = blender_assets.prewarm_prototypes
blender_assets.prepare_prototype = prepare


def prepare_many(specs):
    results = []
    for reference, frame, native in specs:
        try:
            results.append(blender_assets.prepare_prototype(reference, frame, native_subdivision=native))
        except Exception as error:
            results.append(error)
    return results


blender_assets.prepare_prototypes = prepare_many
blender_assets.prewarm_prototypes = lambda specs: None
try:
    author([reference, reference, reference, ordinary.path()], [1.25, 2.75, 1.25, 99.0])
    request, table, diagnostics = capture()
    assert not diagnostics, diagnostics
    assert table["count"] == 4, table
    assert table["synthetic_points"] == 4, table  # Mixed OBJ prototypes keep their packed point table.
    prototypes = request["point_instancing"]["prototypes"]
    assert sorted(p["source_frame"] for p in prototypes if p.get("source_reference")) == [1.25, 2.75]
    assert calls == [(reference, 1.25), (reference, 2.75)], calls
    assert subdivision_modes == [True, True]
    from cycles_houdini import object_properties

    object_properties.install(source_obj)
    source_obj.parm("cycles_blender_subdivision").set(False)
    capture()
    assert subdivision_modes[-2:] == [False, False]
    source_obj.parm("cycles_blender_subdivision").set(True)
    checks.append("existing instancers default to native subdivision and can select Blender evaluation")
    rows = {row["point"]: row for row in table["instances"]}
    assert rows[0]["prototype"] == rows[2]["prototype"] != rows[1]["prototype"]
    assert math.isclose(rows[3]["matrix"][12], 13.0), rows[3]
    for point, frame in ((0, 1.25), (1, 2.75)):
        prototype = prototypes[rows[point]["prototype"]]
        assert prototype["source_frame"] == frame
        matrix = rows[point]["matrix"]
        assert math.isclose(matrix[12], point * 2 + frame * 0.1, abs_tol=1e-6) and math.isclose(matrix[13], 1.0), matrix
    selected = object_scene.selected(rop, now)
    assert all(not instancing.is_generated(node) for node in selected), [node.path() for node in selected]
    checks.append("mixed references, duplicate sharing, fractional absolute frames and nested transforms")

    author([reference, reference, ordinary.path()])
    request, table, diagnostics = capture()
    assert table["count"] == 3 and not diagnostics
    assert [p["source_frame"] for p in request["point_instancing"]["prototypes"] if p.get("source_reference")] == [12.5]
    checks.append("missing index uses current fractional Houdini frame")

    precise = [1.123456789012345, 2.876543210987654]
    author([reference, reference], precise, float64=True)
    request, table, diagnostics = capture()
    assert table["count"] == 2 and not diagnostics, (table, diagnostics)
    actual = sorted(p["source_frame"] for p in request["point_instancing"]["prototypes"])
    assert actual == precise, (actual, precise)
    checks.append("Float64 index retains exact fractional frame keys end to end")

    author([reference, ordinary.path()], [1, 2], integer=True)
    request, table, diagnostics = capture()
    assert table["count"] == 1 and table["instances"][0]["point"] == 1, table
    assert any(d["code"] == "instancing.invalid_blender_index" for d in diagnostics), diagnostics
    checks.append("invalid index omits external points while retaining OBJ references")

    # NaN survives as an ordinary geometry value, but never becomes a cache key.
    author([reference, ordinary.path()], [0.0, 2.0])
    source.parm("python").set(source.parm("python").eval() + "\ng.points()[0].setAttribValue('index',float('nan'))\n")
    request, table, diagnostics = capture()
    assert table["count"] == 1 and table["instances"][0]["point"] == 1, table
    assert any(d["code"] == "instancing.invalid_blender_index" for d in diagnostics), diagnostics
    checks.append("non-finite index omitted independently")

    collection = str(asset.resolve()).replace("\\", "/") + "#collection=Pair"
    author([collection, ordinary.path()], [1.25, 99.0])
    request, table, diagnostics = capture()
    assert table["count"] == 3 and not diagnostics, (table, diagnostics)
    members = [row for row in table["instances"] if row["point"] == 0]
    assert len(members) == 2
    assert sorted(round(row["matrix"][13], 5) for row in members) == [-1.0, 1.0]
    assert (
        len({member["geometry_key"] for member in request["point_instancing"]["prototypes"][0]["direct_instances"]})
        == 1
    )
    checks.append("collection members preserve separate transforms while sharing native mesh geometry")

    author([collection, collection], [1.25, 1.25])
    source.parm("python").set(
        source.parm("python").eval()
        + """
g.addAttrib(hou.attribType.Point, 'Cd', (0.25, 0.5, 0.75))
g.addAttrib(hou.attribType.Point, 'custom_scalar', 3.5)
g.addAttrib(hou.attribType.Point, 'custom_int', 7)
g.addAttrib(hou.attribType.Point, 'custom_nonfinite', 0.)
g.addAttrib(hou.attribType.Point, 'custom_wide', (1., 2., 3., 4.))
g.addAttrib(hou.attribType.Point, 'v', (1., 2., 3.))
g.addAttrib(hou.attribType.Point, 'orient', (0., 0., 0., 1.))
g.addAttrib(hou.attribType.Point, 'pscale', 2.)
g.points()[0].setAttribValue('custom_nonfinite', float('nan'))
g.points()[1].setAttribValue('custom_scalar', 8.5)
"""
    )
    request, table, diagnostics = capture()
    assert table["count"] == 4 and not diagnostics, (table, diagnostics)
    assert table["synthetic_points"] == 0, table
    assert len({row["identity"] for row in table["instances"]}) == 4
    for point in (0, 1):
        members = [row for row in table["instances"] if row["point"] == point]
        assert len(members) == 2 and members[0]["attributes"] == members[1]["attributes"], members
        for row in members:
            fields = row["attributes"]
            assert fields["Cd"] == dict(components=3, value=(0.25, 0.5, 0.75)), fields
            assert fields["custom_scalar"] == dict(components=1, value=(3.5 if point == 0 else 8.5, 0.0, 0.0))
            assert fields["custom_int"] == dict(components=1, value=(7.0, 0.0, 0.0))
            assert ("custom_nonfinite" in fields) == (point == 1)
            assert not {"P", "instance", "v", "pscale", "custom_wide"}.intersection(fields), fields
            assert row["velocity"] == (1.0, 2.0, 3.0)
    checks.append("direct-only collections capture point fields once without synthetic point copies")

    source.parm("python").set(
        source.parm("python").eval()
        + """
g.addAttrib(hou.attribType.Point, 'shop_materialpath', '')
g.addAttrib(hou.attribType.Point, 'material_override', '')
for point in g.points():
    point.setAttribValue('shop_materialpath', '/mat/test_point_binding')
    point.setAttribValue('material_override', '{"roughness":0.25}')
g.addAttrib(hou.attribType.Point, 'id', 7)
g.points()[1].setAttribValue('v', (float('nan'), 0., 0.))
"""
    )
    # HOM string attributes have empty defaults even when addAttrib receives a
    # nonempty string; author the values explicitly before checking ingress.
    assert source.geometry().pointStringAttribValues("shop_materialpath") == ("/mat/test_point_binding",) * 2
    assert source.geometry().pointStringAttribValues("material_override") == ('{"roughness":0.25}',) * 2
    request, table, diagnostics = capture()
    assert table["count"] == 4 and table["synthetic_points"] == 0, table
    assert len({row["identity"] for row in table["instances"]}) == 4
    assert [issue["code"] for issue in table["source_diagnostics"]].count("motion.external_velocity") == 1, table
    for row in table["instances"]:
        assert row["material"] == "/mat/test_point_binding", row
        assert row["overrides"] == ['{"roughness":0.25}'], row
        assert row["velocity"] == ((1.0, 2.0, 3.0) if row["point"] == 0 else (0.0, 0.0, 0.0))
        assert "pscale" in row["attributes"]  # Overrides conservatively retain transform attributes.
    checks.append("collection point bindings, retained transform fields and duplicate authored IDs remain intact")

    author([reference], [1.25])
    source.parm("python").set(
        source.parm("python").eval()
        + "\ng.addAttrib(hou.attribType.Point,'pscale',2.)\ng.addAttrib(hou.attribType.Point,'trans',(1.,2.,3.))\n"
    )
    request, table, diagnostics = capture()
    matrix = table["instances"][0]["matrix"]
    assert table["count"] == 1 and not diagnostics, (table, diagnostics)
    assert all(math.isclose(matrix[i], 2.0, abs_tol=1e-6) for i in (0, 5, 10)), matrix
    assert all(math.isclose(matrix[i], v, abs_tol=1e-6) for i, v in ((12, 1.25), (13, 4.0), (14, 3.0))), matrix
    checks.append("Houdini point scale and translation compose with external member transforms")

    author([reference, ordinary.path()], [3.5, 2.0])
    unavailable.add(reference)
    request, table, diagnostics = capture()
    assert table["count"] == 1 and any(d["code"] == "instancing.unavailable_blender_prototype" for d in diagnostics)
    unavailable.clear()
    request, table, diagnostics = capture()
    assert table["count"] == 2 and not diagnostics, (table, diagnostics)
    before = instancing.external_signature(rop)
    asset.write_bytes(b"fixture-v2-with-changed-dependency")
    after = instancing.external_signature(rop)
    assert before != after
    checks.append("missing external prototype repairs and file dependency changes are observed")
finally:
    blender_assets.prepare_prototype = original
    blender_assets.prepare_prototypes = original_many
    blender_assets.prewarm_prototypes = original_warmup

report = dict(result="passed", checks=checks)
(args.out / "report.json").write_text(json.dumps(report, indent=2))
print(json.dumps(report))
