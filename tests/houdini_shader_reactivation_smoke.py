# SPDX-License-Identifier: Apache-2.0
"""An unused compiled shader must reactivate when assigned to points and hair."""

from support.paths import artifacts_root
import argparse
import json
import os
from pathlib import Path
import traceback

import hou
import numpy as np
import cycles_houdini as ch

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
device = parser.parse_args().device
dest = artifacts_root() / "stage3/materials/reactivation"
dest.mkdir(parents=True, exist_ok=True)
obj = hou.node("/obj").createNode("geo", "shader_reactivation", run_init_scripts=False)
source = obj.createNode("python", "point_and_hair_fixture")
source.parm("python").set("""import hou
g=hou.pwd().geometry()
g.addAttrib(hou.attribType.Point,'pscale',.25)
g.addAttrib(hou.attribType.Point,'width',.12)
p=g.createPoint();p.setPosition((-.75,0,0))
c=g.createPolygon(is_closed=False)
for y in (-.5,0,.5):
 p=g.createPoint();p.setPosition((.75,y,0));c.addVertex(p)
""")
camera = hou.node("/obj").createNode("cam", "reactivation_camera")
camera.setParms(dict(tz=5, projection=1, orthowidth=4))
colors = dict(red=(0.8, 0.1, 0.05), green=(0.05, 0.7, 0.1))
materials = []
for key, color in colors.items():
    builder = ch.create_material(name="reactivation_" + key, shader="emission")
    builder.node("surface_shader").parmTuple("color").set(color)
    builder.node("surface_shader").parm("strength").set(1)
    materials.append(dict(key=key, kind="graph", graph=ch.material_graph(builder)))
report = dict(result="running", device=device, checks=[])
try:
    with ch.Session(device=device, width=80, height=48, samples=64) as session:
        for key in ("red", "green", "red", "green", "red"):
            request = ch.geometry_request(obj, source, material=key, material_mode="uniform")
            frame = session.wait(
                session.apply(
                    dict(geometry=[request], materials=materials, camera_path=camera.path(), background=(0, 0, 0))
                )
            )
            plane = frame["planes"]["combined"]
            image = np.frombuffer(plane["pixels"], np.float32).reshape(48, 80, plane["channels"])
            status = session.status()
            assert status["state"] != "failed" and not status["source_diagnostics"], status
            assert (
                status["geometry_resources"] == status["object_resources"] == 2
                and status["points"] == status["curves"] == 1
            ), status
            for x in (25, 55):
                np.testing.assert_allclose(image[24, x, :3], colors[key], atol=2e-5)
            report["checks"].append(
                dict(material=key, points=image[24, 25, :3].tolist(), hair=image[24, 55, :3].tolist(), status=status)
            )
    report["closed"] = session.status()
    assert report["closed"]["device_allocation_bytes"] == report["closed"]["owned_geometry_bytes"] == 0, report[
        "closed"
    ]
    report["result"] = "passed"
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    (dest / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(dict(result=report["result"], checks=len(report["checks"]))))
