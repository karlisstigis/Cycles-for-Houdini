# SPDX-License-Identifier: Apache-2.0
"""Check the native light, stock light, and renderer capture on a new rig."""

import argparse
import json

import hou

from cycles_houdini import light_properties, matrix_from_houdini, native_scene, object_scene
from support.paths import artifacts_root


parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
args = parser.parse_args()
scene = hou.node("/obj").createNode("subnet", "pose_scene")
scene.setParms(dict(tx=2, ty=1, rz=6))
parent = scene.createNode("null", "rig")
other_parent = scene.createNode("null", "second_rig")
target = scene.createNode("null", "look_target")
light = scene.createNode(light_properties.LIGHT_TYPE, "named_key")
stock = scene.createNode("hlight::2.0", "stock_light")
camera = scene.createNode("cam", "camera")
rop = hou.node("/out").createNode("cycles_ipr", "pose_capture")
rop.setParms(dict(source_mode="objects", objectroot=scene.path(), lights=light.name(), camera=camera.path()))
assert light.type().definition().extraInfoValue("subtype") == "light"
assert set(scene.recursiveGlob("*", filter=hou.nodeTypeFilter.ObjLight)) == {light, stock}
assert object_scene.selected(rop, hou.time(), lights=True) == [light]
assert object_scene.selected(rop, hou.time()) == []
for node in (light, stock):
    node.setInput(0, parent)
    node.setParms(dict(tx=1.5, ty=0.85, tz=3.4, rx=10, ry=-7))
    node.setPreTransform(hou.hmath.buildTranslate((0.25, -0.5, 0.2)))
    assert node.evalParmTuple("s") == (1, 1, 1) and node.evalParm("scale") == 1

checks = []


def compare(label):
    now = hou.time()
    expected = matrix_from_houdini(stock.worldTransformAtTime(now))
    named = matrix_from_houdini(light.worldTransformAtTime(now))
    light.cook(force=True)
    stock.cook(force=True)
    assert named == matrix_from_houdini(light.worldTransformAtTime(now))
    data = native_scene.capture(rop, now)
    assert len(data["lights"]) == 1, data["lights"]
    definition = data["lights"][0]
    assert definition["source_path"] == definition["linking_id"] == light.path()
    assert not data["geometry"], data["geometry"]
    error = max(abs(a - b) for a, b in zip(expected, definition["transform"]))
    assert error < 1e-8, (label, expected, definition)
    assert named == definition["transform"], (label, named, definition)
    record = dict(case=label, frame=hou.frame(), max_matrix_error=error, pose=named, stock_pose=expected)
    if light.evalParm("lookatpath"):
        matrix = stock.worldTransformAtTime(now)
        center = matrix.extractTranslates()
        direction = (hou.Vector3((0, 0, -1)) * matrix - center).normalized()
        target_direction = (target.worldTransformAtTime(now).extractTranslates() - center).normalized()
        record["stock_aim_dot"] = direction.dot(target_direction)
    checks.append(record)
    return definition


light.setParms(dict(cycles_light_type="spot", cycles_light_radius=0.35, cycles_light_spot_angle=40))
stock.parm("coneenable").set(1)
parent.setParms(dict(tx=-2, ty=1.5, tz=1.25, rx=17, ry=-33, rz=23))
assert compare("nested rotated parent")["radius"] == 0.35
parent.setParms(dict(sx=2, sy=1.5, sz=0.75))
compare("nonuniform parent")
parent.parm("sx").set(-2)
compare("mirrored parent")
other_parent.setParms(dict(tx=3, ty=-2, ry=27))
for label, input_node in (("reparent", other_parent), ("disconnect parent", None), ("restore parent", parent)):
    for node in (light, stock):
        node.setInput(0, input_node)
    compare(label)
for node in (light, stock):
    node.parm("lookatpath").set("../look_target")
target.parm("tx").setExpression("0.25 + 0.125 * $F", hou.exprLanguage.Hscript)
target.setParms(dict(ty=0.5, tz=-1))
parent.parm("ry").setExpression("$F * 7 - 33", hou.exprLanguage.Hscript)
for frame in (1, 5, 10):
    hou.setFrame(frame)
    compare("Look At under an animated mirrored nonuniform parent")
assert len({tuple(check["pose"]) for check in checks[-3:]}) == 3

for shape, size in (("rectangle", (2, 3)), ("ellipse", (2, 3)), ("square", (2, 2)), ("disk", (2, 2))):
    light.setParms(
        dict(cycles_light_type="grid", cycles_light_area_shape=shape, singlesided=1, areasizex=2, areasizey=3)
    )
    definition = compare("native " + shape + " area size and aim")
    assert tuple(definition["size"]) == size, definition
    assert definition["ellipse"] == (shape in ("ellipse", "disk"))
    assert definition["normalize"]

report = dict(
    result="passed",
    cases=checks,
    device=args.device,
    houdini=hou.applicationVersionString(),
    library=light.type().definition().libraryFilePath(),
)
destination = artifacts_root() / "stage3/light-properties/parenting"
destination.mkdir(parents=True, exist_ok=True)
(destination / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(dict(result="passed", cases=len(checks), max_matrix_error=max(c["max_matrix_error"] for c in checks))))
