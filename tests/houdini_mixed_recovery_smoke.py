# SPDX-License-Identifier: Apache-2.0
"""Foreign shaders on mesh, hair, points and sparse density in one native SOP."""

from support.paths import artifacts_root
from native_test_runtime import runtime_executable
import argparse
import json
import os
from pathlib import Path
import subprocess
import hou
import numpy as np
import cycles_houdini as ch
from cycles_houdini import native_scene

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
device = parser.parse_args().device
root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
dest = artifacts_root() / "stage3/materials/recovery"
dest.mkdir(parents=True, exist_ok=True)
fixture = dest / ("mixed-" + str(os.getpid()) + ".vdb")
subprocess.run([str(runtime_executable("ch_harness.exe")), "--write-pyro-fixture", str(fixture)], check=True)
obj = hou.node("/obj").createNode("geo", "mixed_recovery", run_init_scripts=False)
file = obj.createNode("file")
file.parm("file").set(str(fixture))
volume = obj.createNode("stash")
volume.parm("stash").set(file.geometry())
file.destroy()
fixture.unlink()
foreign = hou.node("/mat").createNode("principledshader", "foreign_mixed_shader")
obj.parm("shop_materialpath").set(foreign.path())
pig = obj.createNode("testgeometry_pighead")
move = obj.createNode("xform")
move.setInput(0, pig)
move.parmTuple("t").set((-2.3, 0, 0))
move.parm("scale").set(0.6)
strands = hou.Geometry()
strands.addAttrib(hou.attribType.Point, "pscale", 0.2)
strands.addAttrib(hou.attribType.Point, "width", 0.15)
point = strands.createPoint()
point.setPosition((3, 0, 0))
curve = strands.createPolygon(is_closed=False)
for y in (-0.7, 0.7):
    point = strands.createPoint()
    point.setPosition((2, y, 0))
    curve.addVertex(point)
stash = obj.createNode("stash")
stash.parm("stash").set(strands)
merge = obj.createNode("merge")
for index, sop in enumerate((volume, move, stash)):
    merge.setInput(index, sop)
camera = hou.node("/obj").createNode("cam", "mixed_recovery_camera")
camera.setParms(dict(tz=6, projection=1, orthowidth=8))
request = ch.geometry_request(obj, merge, material_mode="native", material="default")


def rgb(frame):
    return np.frombuffer(frame["planes"]["combined"]["pixels"], np.float32).reshape(100, 160, 4)[..., :3]


checks = []
with ch.Session(device=device, width=160, height=100, samples=128, interactive=False) as session:
    frame = session.wait(
        session.apply(
            dict(
                geometry=[request],
                material_resolver=native_scene.resolve_materials,
                materials=[dict(key="default", kind="diffuse", color=(0.3, 0.3, 0.3))],
                camera_path=camera.path(),
                background=(0, 0, 0),
                rendering=dict(volume_bounce=4),
            )
        )
    )
    status = session.status()
    assert status["geometry_resources"] == 4 and status["object_resources"] == 4, status
    assert status["volume_ray_marching"]
    issues = status["source_diagnostics"]
    assert any(d["resource"].endswith("/volume") for d in issues), issues
    image = rgb(frame)
    assert np.allclose(image[48:52, 78:82], 0, atol=1e-6), image[50, 80]
    for x in (34, 120, 140):
        region = image[42:58, x - 4 : x + 4]
        assert ((region[..., 0] > 0.8) & (region[..., 1] < 0.1) & (region[..., 2] > 0.8)).sum() > 10, (
            x,
            region.max(axis=(0, 1)),
        )
    checks.append(
        "Mixed SOP retains mesh, curve, points and VDB; unlit surface markers are visible and volume emits no light"
    )
    frame = session.wait(
        session.apply(
            dict(
                background=(1, 1, 1),
                lights=[
                    dict(
                        key="volume_light",
                        kind="point",
                        strength=(1000, 1000, 1000),
                        transform=(1, 0, 0, 0, 0, 1, 0, 2, 0, 0, 1, 4),
                    )
                ],
            )
        )
    )
    value = rgb(frame)[46:54, 76:84].mean(axis=(0, 1))
    assert value[0] > value[1] + 0.1 and value[2] > value[1] + 0.1, value
    assert session.status()["source_diagnostics"] == issues
    session.write_exr(dest / "mixed-recovery.exr")
    checks.append("Foreign volume shader uses the existing density with magenta scattering and persistent diagnostic")
report = dict(result="passed", device=device, checks=checks)
(dest / "houdini-mixed-recovery-smoke.json").write_text(json.dumps(report, indent=2))
print(json.dumps(report))
