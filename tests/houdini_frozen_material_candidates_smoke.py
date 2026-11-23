# SPDX-License-Identifier: Apache-2.0
"""Deferred material candidate errors preserve effective assignment semantics."""

from support.paths import artifacts_root
import gc
import json
import os
from pathlib import Path
import hou
import cycles_houdini as ch

hou.setFps(24)
hou.setFrame(1)
obj = hou.node("/obj").createNode("geo", "candidate_fixture", run_init_scripts=False)
grid = obj.createNode("grid")
grid.setParms(dict(rows=2, cols=2))
source = obj.createNode("python", "bindings")
source.setInput(0, grid)
source.addSpareParmTuple(hou.FloatParmTemplate("sample_time", "Sample Time", 1))
source.parm("sample_time").setExpression("$T", hou.exprLanguage.Hscript)
builder = ch.create_material(name="frozen_candidate", shader="emission")
# The default this test was written for (new nodes now take Blender's).
builder.node("surface_shader").setParms(dict(color=(0.8, 0.8, 0.8), strength=10.0))
key = "native-material:" + builder.userData("cycles_graph_id") + ":" + str(builder.sessionId())
checks = []


def set_source(extra):
    source.parm("python").set(
        "import hou\ng=hou.pwd().geometry()\n"
        + "g.addAttrib(hou.attribType.Point,'id',0)\n"
        + 'for p in g.points():p.setAttribValue("id",p.number())\n'
        + extra
    )


def prepare(fallback="/mat/unused_missing"):
    return ch._native.preserve_deformation(source.path(), hou.time(), hou.fps(), 1.0, 3, fallback)


def check(fallback="/mat/unused_missing"):
    lease = prepare(fallback)
    value = ch._native.convert_preserved_deformation(lease)
    assert value["components"][0]["materials"] == [key], value
    del lease
    gc.collect()
    assert ch._native.geometry_lease_count() == 0
    return value


def rejection(fragment, fallback="/mat/unused_missing"):
    lease = prepare(fallback)
    try:
        ch._native.convert_preserved_deformation(lease)
    except RuntimeError as error:
        assert fragment in str(error), (fragment, str(error))
    else:
        raise AssertionError("Frozen invalid material binding was accepted")
    del lease
    gc.collect()
    assert ch._native.geometry_lease_count() == 0


set_source(
    "g.addAttrib(hou.attribType.Prim,'shop_materialpath','')\n"
    + "g.setPrimStringAttribValues('shop_materialpath',["
    + repr("op:" + builder.path())
    + "]*len(g.prims()))\n"
)
check()
checks.append("Interned primitive op: binding and unused missing OBJ fallback")
set_source(
    "g.addAttrib(hou.attribType.Global,'shop_materialpath','')\n"
    + "g.setGlobalAttribValue('shop_materialpath',"
    + repr(builder.path())
    + ")\n"
    + "g.addAttrib(hou.attribType.Prim,'shop_materialpath','')\n"
)
check()
checks.append("Empty primitive strings retain detail fallback")
set_source(
    "g.addAttrib(hou.attribType.Point,'shop_materialpath','')\n"
    + "g.setPointStringAttribValues('shop_materialpath',['/mat/unused_point_binding']*len(g.points()))\n"
)
check(builder.path())
checks.append("Mesh point material paths do not replace effective object bindings")
set_source(
    "g.addAttrib(hou.attribType.Prim,'shop_materialpath','')\n"
    + "g.setPrimStringAttribValues('shop_materialpath',["
    + repr(builder.path())
    + "]*len(g.prims()))\n"
    + "if hou.pwd().evalParm('sample_time')>0:g.prim(0).setAttribValue('shop_materialpath','/mat/missing_shutter_material')\n"
)
lease = prepare()
value = ch._native.convert_preserved_deformation(lease)
assert len(value["components"][0]["samples"]) == 3 and value["components"][0]["materials"][0].startswith(
    "native-material:timeline:"
), value
del lease
gc.collect()
assert ch._native.geometry_lease_count() == 0
checks.append("Frozen binding timelines retain valid geometry correspondence")
set_source("if hou.pwd().evalParm('sample_time')>0:g.clear()\n")
rejection("Geometry type changes")
checks.append("Strict frozen API still rejects genuinely missing geometry correspondence")
set_source("g.addAttrib(hou.attribType.Prim,'shop_materialpath','')\n")
lease = prepare()
value = ch._native.convert_preserved_deformation(lease)
assert value["components"][0]["materials"] == ["native-material:unresolved:/mat/unused_missing"], value
del lease
gc.collect()
assert ch._native.geometry_lease_count() == 0
checks.append("Used missing assignment retains its owned diagnostic identity")
set_source(
    "g.addAttrib(hou.attribType.Prim,'shop_materialpath','')\n"
    + "g.setPrimStringAttribValues('shop_materialpath',["
    + repr(builder.path())
    + "]*len(g.prims()))\n"
)
lease = prepare()
reference = ch._native.convert_preserved_deformation(lease)
builder.destroy()
obj.destroy()
assert ch._native.convert_preserved_deformation(lease)["fingerprint"] == reference["fingerprint"]
del lease
gc.collect()
assert ch._native.geometry_lease_count() == 0
checks.append("Frozen interned bindings survive source/material deletion")
path = artifacts_root() / "stage1/geometry-ownership/frozen-material-candidates.json"
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(dict(result="passed", checks=checks), indent=2), encoding="utf-8")
print(path)
