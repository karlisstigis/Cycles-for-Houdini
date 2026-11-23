# SPDX-License-Identifier: Apache-2.0
"""Native Pig Head / Copy to Points renders with shutter sampling and no IDs."""

from support.paths import artifacts_root
from array import array
import json
import os
from pathlib import Path
import hou
import cycles_houdini as ch

root = artifacts_root() / "stage2/packed"
root.mkdir(parents=True, exist_ok=True)
hou.setFps(24)
hou.setFrame(1)
obj = hou.node("/obj").createNode("geo", "pigs", run_init_scripts=False)
types = hou.sopNodeTypeCategory().nodeTypes()
pig_type = next(name for name in types if name.split("::")[0] == "testgeometry_pighead")
pig = obj.createNode(pig_type)
targets = obj.createNode("grid", "targets")
targets.setParms(dict(rows=2, cols=3))
targets.parmTuple("size").set((7, 3))
move = obj.createNode("xform", "moving_targets")
move.setInput(0, targets)
move.parm("rx").set(90)
copies = obj.createNode("copytopoints", "copytopoints1")
copies.setInput(0, pig)
copies.setInput(1, move)
copies.parm("pack").set(1)
copies.setDisplayFlag(True)
copies.setRenderFlag(True)
geo = copies.geometry()
assert len(geo.prims()) == 6
assert not any(
    (geo.findPointAttrib("id"), geo.findPrimAttrib("id"), geo.findPointAttrib("name"), geo.findPrimAttrib("name"))
)
camera = hou.node("/obj").createNode("cam", "pig_camera")
camera.parm("tz").set(12)
camera.parm("projection").set(1)
camera.parm("orthowidth").set(11)
camera.parm("shutter").set(0.5)
report = dict(
    result="running",
    fixture="Native Pig Head, Grid, Transform, Copy to Points",
    authored_id_attributes=False,
    instances=6,
    devices=[],
)
for device in ("CPU", ch.default_device()):
    with ch.Session(device=device, width=440, height=248, samples=32, interactive=False) as session:

        def scene(enabled):
            return dict(
                geometry=[ch.geometry_request(obj, copies, material="pig", material_mode="uniform", at_time=0)],
                materials=[dict(key="pig", kind="emission", color=(0.6, 0.2, 0.1))],
                camera_path=camera.path(),
                background=(0, 0, 0),
                motion=dict(enabled=enabled, fps=24.0, shutter_frames=0.5, offset=0.0, samples=3),
            )

        plain = session.wait(session.apply(scene(False), at_time=0))
        static = session.wait(session.apply(scene(True), at_time=0))
        assert plain["planes"]["combined"]["pixels"] == static["planes"]["combined"]["pixels"], (
            "Static pigs changed with shutter sampling"
        )
        pixels = array("f")
        pixels.frombytes(static["planes"]["combined"]["pixels"])
        assert sum(value > 0.1 for value in pixels[0::4]) > 4000, "Pigs are missing from the image"
        keys = session.native_object_keys()
        assert len(keys) == 6 and all("/packed:index:" in key for key in keys)
        assert session.status()["geometry_resources"] == 1
        tag = "cpu" if device == "CPU" else "optix"
        session.write_exr(root / ("implicit-pigs-" + tag + ".exr"))
        move.parm("tx").setExpression("$T * 48", hou.exprLanguage.Hscript)
        blurred = session.wait(session.apply(scene(True), at_time=0))
        assert blurred["planes"]["combined"]["pixels"] != static["planes"]["combined"]["pixels"]
        assert session.native_object_keys() == keys
        assert session.status()["geometry_resources"] == 1
        report["devices"].append(
            dict(
                device=device,
                result="passed",
                status=session.status(),
                static_motion_matches=True,
                animated_motion_changes=True,
            )
        )
        move.parm("tx").deleteAllKeyframes()
        move.parm("tx").set(0)
report["result"] = "passed"
(root / "houdini-implicit-packed-smoke.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(dict(result="passed", devices=len(report["devices"]), instances=6)))
