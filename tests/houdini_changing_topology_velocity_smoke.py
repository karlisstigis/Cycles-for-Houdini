# SPDX-License-Identifier: Apache-2.0
"""Changing topology uses nominal P/v, never cross-frame point correspondence."""

from support.paths import artifacts_root
from array import array
import json
import os
from pathlib import Path
import hou
import cycles_houdini as ch

root = artifacts_root() / "stage2/motion"
root.mkdir(parents=True, exist_ok=True)
hou.setFps(24)
obj = hou.node("/obj").createNode("geo", "changing_topology", run_init_scripts=False)
sop = obj.createNode("python", "procedural_population")
camera = hou.node("/obj").createNode("cam", "velocity_camera")
camera.setParms(dict(tz=5, projection=1, orthowidth=4))
code = """import hou, math
g = hou.pwd().geometry()
t = hou.time(); frame = int(round(t*24))
g.addAttrib(hou.attribType.Point, 'v', (48.0, 0.0, 0.0))
g.addAttrib(hou.attribType.Point, 'pscale', .2)
g.addAttrib(hou.attribType.Point, 'width', .2)
def point(x, y):
    p = g.createPoint(); p.setPosition((x+48*t, y, 0)); return p
kind = KIND
if kind == 'mesh':
    coords = [(-.2,-.6),(-.2,.6),(.2,.6),(.2,-.6)]
    if frame % 2: coords += [(0,0)]
    order = list(range(len(coords)))
    if frame % 2: order.reverse()
    points = {i: point(*coords[i]) for i in order}
    faces = [(0,1,4),(1,2,4),(2,3,4),(3,0,4)] if frame % 2 else [(0,1,2),(0,2,3)]
    for indices in faces:
        face = g.createPolygon()
        for i in indices: face.addVertex(points[i])
elif kind == 'points':
    coords = [(0,0)] + [(10+i,3) for i in range(frame+1)]
    if frame % 2: coords.reverse()
    for xy in coords: point(*xy)
else:
    # Retessellate the visible straight strand and change the offscreen count.
    strands = [(0, [-.6,0,.6] if frame % 2 else [-.6,.6])]
    strands += [(10+i, [-.6,.6]) for i in range(frame+1)]
    if frame % 2: strands.reverse()
    for x, ys in strands:
        curve = g.createPolygon(is_closed=False)
        for y in ys: curve.addVertex(point(x,y))
"""
reports = []
for device in ("CPU", ch.default_device()):
    rows = []
    with ch.Session(device=device, width=160, height=90, samples=2048, interactive=False) as session:
        for kind, expected in (("mesh", 0.4), ("points", 0.4), ("curves", 0.2)):
            sop.parm("python").set(code.replace("KIND", repr(kind)))
            counts = set()
            for frame_number in range(1, 6):
                hou.setFrame(frame_number)
                now = hou.time()
                geo = sop.geometry()
                counts.add((len(geo.points()), len(geo.prims())))
                assert not any(
                    (
                        geo.findPointAttrib("id"),
                        geo.findPointAttrib("name"),
                        geo.findPrimAttrib("id"),
                        geo.findPrimAttrib("name"),
                    )
                )
                # Camera is static over this shutter, centred on nominal geometry.
                camera.parm("tx").set(48 * now)
                update = dict(
                    geometry=[ch.geometry_request(obj, sop, material="emission", motion="velocity", at_time=now)],
                    camera_path=camera.path(),
                    background=(0, 0, 0),
                    materials=[dict(key="emission", kind="emission", color=(0.8, 0.1, 0.02))],
                    motion=dict(enabled=True, fps=24.0, shutter_frames=0.5, offset=0.0, samples=3),
                )
                image = session.wait(session.apply(update, at_time=now))
                pixels = array("f")
                pixels.frombytes(image["planes"]["combined"]["pixels"])
                measured = sum(pixels[(y * 160 + x) * 4] for y in (44, 45) for x in (79, 80)) / (4 * 0.8)
                assert abs(measured - expected) < 0.035, (device, kind, frame_number, measured, expected)
                status = session.status()
                assert status["motion_cooks"] == 0, "Velocity unexpectedly cooked shutter geometry"
                rows.append(
                    dict(
                        kind=kind,
                        frame=frame_number,
                        points=len(geo.points()),
                        primitives=len(geo.prims()),
                        expected_coverage=expected,
                        measured_coverage=measured,
                        motion_cooks=0,
                    )
                )
            assert len(counts) > 1, (kind, counts)
    reports.append(dict(device=device, frames=rows))
report = dict(
    result="passed",
    authored_ids=False,
    scope="Five frames each of changing mesh, point and curve topology using nominal velocity",
    devices=reports,
)
(root / "houdini-changing-topology-velocity-smoke.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(dict(result="passed", frames_per_device=[len(row["frames"]) for row in reports])))
