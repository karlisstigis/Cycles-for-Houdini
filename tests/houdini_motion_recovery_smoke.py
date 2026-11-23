# SPDX-License-Identifier: Apache-2.0
"""Changing connectivity falls back locally; valid point and OBJ motion survive."""

from support.paths import artifacts_root
import argparse
import json
import os
from pathlib import Path
import hou
import numpy as np
import cycles_houdini as ch

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
device = parser.parse_args().device
hou.setFps(24)
hou.setFrame(1)
obj = hou.node("/obj").createNode("geo", "motion_recovery", run_init_scripts=False)
source = obj.createNode("python")
source.addSpareParmTuple(hou.FloatParmTemplate("time", "Time", 1))
source.parm("time").setExpression("$T", hou.exprLanguage.Hscript)
source.addSpareParmTuple(hou.ToggleParmTemplate("broken", "Changing Topology", default_value=True))
source.addSpareParmTuple(hou.ToggleParmTemplate("fail_subframe", "Fail Subframes", default_value=False))
source.parm("python").set("""import hou
n=hou.pwd();g=n.geometry();t=n.evalParm('time')
if n.evalParm('fail_subframe') and abs(t*24-round(t*24))>1e-6:
    raise hou.NodeError('This asset only cooks integer frames')
g.addAttrib(hou.attribType.Point,'pscale',.1)
# Stable first point belongs to the loose-particle component.
p=g.createPoint();p.setPosition((1+t*24,0,0))
poly=g.createPolygon()
vertices=[(-1.4,-.4),(-.6,-.4),(-.6,.4),(-1.4,.4)]
if n.evalParm('broken') and t>0:vertices.insert(2,(-.5,0))
for x,y in vertices:
    p=g.createPoint();p.setPosition((x,y,.7+t*2));poly.addVertex(p)
""")
camera = hou.node("/obj").createNode("cam", "motion_recovery_camera")
camera.setParms(dict(tz=5, projection=1, orthowidth=4))
request = ch.geometry_request(obj, source, material="surface", motion="deformation")
requests = [request]


def scene(offset=0):
    return dict(
        geometry=requests,
        materials=[dict(key="surface", kind="emission", color=(0.2, 0.7, 0.1), strength=1.0)],
        camera_path=camera.path(),
        background=(0, 0, 0),
        motion=dict(enabled=True, fps=24, shutter_frames=1.0, samples=3, offset=offset),
    )


checks = []
with ch.Session(
    device=device, width=160, height=80, samples=128, interactive=False, passes=["combined", "position"]
) as session:
    frame = session.wait(session.apply(scene()), timeout=120)
    status = session.status()
    issues = status["source_diagnostics"]
    assert len(issues) == 1 and issues[0]["code"] == "motion.unavailable", issues
    assert status["geometry_resources"] == 2 and not status["error"], status
    rgba = np.frombuffer(frame["planes"]["combined"]["pixels"], np.float32).reshape(80, 160, 4)
    positions = np.frombuffer(frame["planes"]["position"]["pixels"], np.float32).reshape(80, 160, 3)
    # Surface still lies at the requested frame, and a moving point covers more
    # than its stationary diameter of 8 pixels (4 world units / 160 pixels).
    assert abs(float(positions[40, 40, 2]) - 0.7) < 1e-4, positions[40, 40]
    assert (rgba[40, 100:150, 1] > 0.005).sum() > 20
    checks.append("Only changing mesh topology loses blur; loose points retain valid deformation motion")
    previous = status["geometry_conversions"]
    session.wait(session.apply(dict(camera_path=camera.path())))
    assert session.status()["source_diagnostics"] == issues and session.status()["geometry_conversions"] == previous
    checks.append("Camera-only edit preserves cached recovery diagnostics")
    source.parm("broken").set(False)
    session.wait(session.apply(scene()))
    assert not session.status()["source_diagnostics"], session.status()
    checks.append("Repairing correspondence restores blur and clears the warning")
    healthy_obj = hou.node("/obj").createNode("geo", "healthy_motion", run_init_scripts=False)
    healthy = healthy_obj.createNode("python")
    healthy.addSpareParmTuple(hou.FloatParmTemplate("time", "Time", 1))
    healthy.parm("time").setExpression("$T", hou.exprLanguage.Hscript)
    healthy.parm("python").set(
        'import hou\ng=hou.pwd().geometry()\ng.addAttrib(hou.attribType.Point,"pscale",.1)\np=g.createPoint();p.setPosition((1+hou.pwd().evalParm("time")*24,.6,0))'
    )
    requests.append(ch.geometry_request(healthy_obj, healthy, material="surface", motion="deformation"))
    source.parm("fail_subframe").set(True)
    for offset in (0.0, 0.25):
        # Offset .25 fails even at the shutter centre: recovery must use the
        # requested frame, not an incomplete or unavailable centre sample.
        frame = session.wait(session.apply(scene(offset)), timeout=120)
        status = session.status()
        issues = status["source_diagnostics"]
        assert status["geometry_resources"] == 3 and not status["error"], status
        assert len(issues) == 2 and all("cook failed" in i["message"] for i in issues), issues
        position = np.frombuffer(frame["planes"]["position"]["pixels"], np.float32).reshape(80, 160, 3)
        rgba = np.frombuffer(frame["planes"]["combined"]["pixels"], np.float32).reshape(80, 160, 4)
        assert abs(float(position[40, 40, 2]) - 0.7) < 1e-4, position[40, 40]
        assert 6 <= (rgba[40, 100:150, 1] > 0.005).sum() <= 10
        assert (rgba[64, 100:150, 1] > 0.005).sum() > 20, "Independent object lost valid deformation blur"
        assert not source.errors(), source.errors()
        previous = status["geometry_conversions"]
        session.wait(session.apply(dict(camera_path=camera.path())))
        assert session.status()["source_diagnostics"] == issues
        assert session.status()["geometry_conversions"] == previous
    checks.append(
        "Failed shutter and centre cooks preserve nominal mesh/points, retain independent blur, restore the SOP and cache warnings"
    )
    source.parm("fail_subframe").set(False)
    frame = session.wait(session.apply(scene()))
    assert not session.status()["source_diagnostics"], session.status()
    rgba = np.frombuffer(frame["planes"]["combined"]["pixels"], np.float32).reshape(80, 160, 4)
    assert (rgba[40, 100:150, 1] > 0.005).sum() > 20
    checks.append("Repairing subframe cooking restores deformation blur and clears warnings")
report = dict(result="passed", device=device, checks=checks)
path = artifacts_root() / "stage3/materials/recovery/houdini-motion-recovery-smoke.json"
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(report, indent=2))
print(json.dumps(report))
