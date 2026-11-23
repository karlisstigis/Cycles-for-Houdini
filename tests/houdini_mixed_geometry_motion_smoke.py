# SPDX-License-Identifier: Apache-2.0
"""Independent shutter coverage and a shared cook across native SOP domains."""

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
requested_device = parser.parse_args().device
root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
destination = artifacts_root() / "stage3/mixed-geometry"
destination.mkdir(parents=True, exist_ok=True)
hou.setFps(24)
hou.setFrame(1)
obj = hou.node("/obj").createNode("geo", "mixed_motion", run_init_scripts=False)
source = obj.createNode("python", "shared_mesh_curve_particles")
source.parm("python").set("""import hou
g=hou.pwd().geometry()
g.addAttrib(hou.attribType.Point,'id',0);g.addAttrib(hou.attribType.Prim,'id',0)
g.addAttrib(hou.attribType.Point,'pscale',.2);g.addAttrib(hou.attribType.Point,'width',.2)
g.addAttrib(hou.attribType.Point,'v',(48.,0.,0.))
coordinates=[(-.2,.6,0),(.2,.6,0),(.2,1.4,0),(-.2,1.4,0),(0,-.3,0),(0,.3,0),(0,-1,0)]
points=[]
for i,coordinate in enumerate(coordinates):
    p=g.createPoint();p.setPosition(coordinate);p.setAttribValue('id',100+i);points.append(p)
face=g.createPolygon();face.setAttribValue('id',10)
for i in range(4):face.addVertex(points[i])
curve=g.createPolygon(is_closed=False);curve.setAttribValue('id',20)
for i in (4,5):curve.addVertex(points[i])
""")
motion = obj.createNode("xform", "one_shutter_cook")
motion.setInput(0, source)
motion.parm("tx").setExpression("$T * 48", hou.exprLanguage.Hscript)
without_ids = obj.createNode("attribdelete", "index_correspondence")
without_ids.setInput(0, motion)
without_ids.parm("ptdel").set("id")
deleted = obj.createNode("blast", "deleted_curve")
deleted.setInput(0, motion)
deleted.parm("group").set("1")
deleted.parm("grouptype").set(4)
deleted.parm("negate").set(0)
changing = obj.createNode("switch", "changing_domains")
changing.setInput(0, motion)
changing.setInput(1, deleted)
changing.parm("input").setExpression("$T > 0", hou.exprLanguage.Hscript)
camera = hou.node("/obj").createNode("cam", "mixed_motion_camera")
camera.parm("tz").set(3)
camera.parm("projection").set(1)
camera.parm("orthowidth").set(4)
timing = dict(enabled=True, fps=24, shutter_frames=0.5, offset=0, samples=3)


def scene(node=motion, mode="deformation"):
    return dict(
        geometry=[ch.geometry_request(obj, node, material="motion", motion=mode, at_time=0)],
        camera_path=camera.path(),
        motion=timing,
        background=(0, 0, 0),
        materials=[dict(key="motion", kind="emission", color=(0.8, 0.1, 0.02))],
    )


def coverages(frame):
    image = np.frombuffer(frame["planes"]["combined"]["pixels"], np.float32).reshape(120, 160, 4)
    values = []
    for y, expected in ((1.0, 0.4), (0.0, 0.2), (-1.0, 0.4)):
        row = round((y / 3 + 0.5) * 120)
        measured = float(image[row - 1 : row + 1, 79:81, 0].mean()) / 0.8
        assert abs(measured - expected) < 0.035, (y, measured, expected)
        values.append(dict(world_y=y, expected=expected, measured=measured))
    return values


reports = []
for device in (requested_device,):
    checks = []
    with ch.Session(device=device, width=160, height=120, samples=2048, interactive=False) as session:
        before = hou.time()
        frame = session.wait(session.apply(scene(), at_time=0))
        initial = session.status()
        assert hou.time() == before and initial["points"] == initial["curves"] == 1
        assert initial["geometry_resources"] == initial["object_resources"] == 3
        assert initial["motion_cooks"] <= len(range(timing["samples"])) + 1, initial
        checks.append(
            dict(
                name="Three independent shutter coverages with one SOP cook per sample",
                coverage=coverages(frame),
                status=initial,
            )
        )
        camera.parm("tz").set(4)
        session.wait(session.apply(scene(), at_time=0))
        warm = session.status()
        assert warm["motion_cooks"] == 0 and warm["geometry_conversions"] == initial["geometry_conversions"], warm
        checks.append(dict(name="Warm camera edit recooks and uploads no deformation components", status=warm))
        camera.parm("tz").set(3)
        # Index correspondence must refer to the original mixed detail indices,
        # not the compacted mesh-only copy created for public triangulation.
        frame = session.wait(session.apply(scene(without_ids), at_time=0))
        coverages(frame)
        checks.append(dict(name="Index fallback survives mesh subset copying with valid stable topology"))
        velocity_frame = session.wait(session.apply(scene(source, "velocity"), at_time=0))
        coverages(velocity_frame)
        checks.append(dict(name="Velocity positions agree with the specified linear deformation trajectory"))
        session.wait(session.apply(scene(), at_time=0))
        frame = session.wait(session.apply(scene(changing), at_time=0))
        status = session.status()
        assert status["geometry_resources"] == status["object_resources"] == 3, status
        issues = status["source_diagnostics"]
        assert (
            len(issues) == 1
            and issues[0]["code"] == "motion.unavailable"
            and "Geometry type changes" in issues[0]["message"]
        ), issues
        image = np.frombuffer(frame["planes"]["combined"]["pixels"], np.float32).reshape(120, 160, 4)
        for y, expected in ((1.0, 0.4), (0.0, 1.0), (-1.0, 0.4)):
            row = round((y / 3 + 0.5) * 120)
            measured = float(image[row - 1 : row + 1, 79:81, 0].mean()) / 0.8
            assert abs(measured - expected) < 0.035, (y, measured, expected)
        frame = session.wait(session.apply(scene(), at_time=0))
        coverages(frame)
        assert not session.status()["source_diagnostics"], session.status()
        checks.append(
            dict(name="Disappearing curve retains nominal geometry; mesh/points keep blur and repairs clear warnings")
        )
        session.wait(session.apply(dict(geometry=[])))
        assert (
            session.status()["partition_cache_bytes"]
            == session.status()["geometry_resources"]
            == session.status()["object_resources"]
            == 0
        )
    assert session.status()["owned_geometry_bytes"] == session.status()["device_allocation_bytes"] == 0
    reports.append(dict(device=device, checks=checks))
(destination / "houdini-mixed-geometry-motion-smoke.json").write_text(
    json.dumps(dict(result="passed", devices=reports), indent=2), encoding="utf-8"
)
print(json.dumps(dict(result="passed", checks_per_device=[len(r["checks"]) for r in reports])))
