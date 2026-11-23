# SPDX-License-Identifier: Apache-2.0
"""Invalid authored normals recover locally, including cached and shutter samples."""

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
obj = hou.node("/obj").createNode("geo", "normal_recovery", run_init_scripts=False)
stash = obj.createNode("stash")
camera = hou.node("/obj").createNode("cam", "normal_recovery_camera")
camera.setParms(dict(tz=5, projection=1, orthowidth=4))
material = dict(key="surface", kind="emission", color=(0.2, 0.7, 0.1), strength=1.0)
checks = []


def geometry(normal=None, owner=hou.attribType.Point, overridden=False):
    geo = hou.Geometry()
    poly = geo.createPolygon()
    for xyz in ((-1, -0.5, 0), (-1, 0.5, 0), (1, 0.5, 0), (1, -0.5, 0)):
        point = geo.createPoint()
        point.setPosition(xyz)
        poly.addVertex(point)
    if normal is not None:
        attribute = geo.addAttrib(owner, "N", normal)
        for item in geo.points() if owner == hou.attribType.Point else poly.vertices():
            item.setAttribValue(attribute, normal)
    if overridden:
        geo.addAttrib(hou.attribType.Vertex, "N", (0.6, 0.0, 0.8))
        geo.addAttrib(hou.attribType.Point, "Cd", "unused malformed color")
        geo.addAttrib(hou.attribType.Vertex, "Cd", (0.2, 0.7, 0.1))
    return geo


def scene(source=stash, motion="transform"):
    return dict(
        geometry=[ch.geometry_request(obj, source, material="surface", motion=motion)],
        materials=[material],
        camera_path=camera.path(),
        background=(0, 0, 0),
        motion=dict(enabled=motion == "deformation", fps=24, shutter_frames=1.0, samples=3),
    )


def plane(frame, name):
    value = frame["planes"][name]
    return np.frombuffer(value["pixels"], np.float32).reshape(frame["height"], frame["width"], value["channels"])


with ch.Session(
    device=device, width=128, height=64, samples=256, interactive=False, passes=["combined", "normal", "position"]
) as session:
    stash.parm("stash").set(geometry())
    baseline = session.wait(session.apply(scene()))
    assert np.allclose(plane(baseline, "normal")[32, 64], (0, 0, 1), atol=1e-5)
    for owner in (hou.attribType.Point, hou.attribType.Vertex):
        for value in (1.0, "bad", (1, 0, 0), (float("nan"), 0.0, 1.0), (0.0, float("inf"), 1.0), (0.0, 0.0, 0.0)):
            stash.parm("stash").set(geometry(value, owner))
            frame = session.wait(session.apply(scene()))
            issues = session.status()["source_diagnostics"]
            assert len(issues) == 1 and issues[0]["code"] == "geometry.invalid_normal", issues
            assert issues[0]["source"] == stash.path() and issues[0]["resource"], issues
            assert not session.status()["error"]
            for name in ("combined", "normal", "position"):
                assert np.allclose(plane(frame, name), plane(baseline, name), atol=1e-5), (owner, value, name)
    checks.append(
        "Point/vertex N with invalid type, tuple, NaN, infinity or zero length renders identically to generated normals"
    )

    conversions = session.status()["geometry_conversions"]
    session.wait(session.apply(dict(camera_path=camera.path())))
    assert session.status()["source_diagnostics"] == issues
    assert session.status()["geometry_conversions"] == conversions
    # An unrelated shading edit reuses the cached empty normal stream. Its
    # warning must survive this lower-level mesh cache, not only scene hits.
    color = obj.createNode("attribwrangle")
    color.setInput(0, stash)
    color.parm("snippet").set("v@Cd=set(.2,.7,.1);")
    session.wait(session.apply(scene(color)))
    color.parm("snippet").set("v@Cd=set(.4,.3,.2);")
    session.wait(session.apply(scene(color)))
    assert len(session.status()["source_diagnostics"]) == 1
    assert session.status()["mesh_topology_reuses"] > 0, session.status()
    checks.append("Camera and mesh attribute cache reuse retain the warning without reintroducing invalid normals")

    stash.parm("stash").set(geometry("unused malformed normal", overridden=True))
    material["color"] = (1, 1, 1)
    material["color_attribute"] = "Cd"
    frame = session.wait(session.apply(scene()))
    assert not session.status()["source_diagnostics"], session.status()
    assert np.allclose(plane(frame, "normal")[32, 64], (0.6, 0, 0.8), atol=2e-4)
    assert np.allclose(plane(frame, "combined")[32, 64, :3], (0.2, 0.7, 0.1), atol=1e-5)
    checks.append("Valid vertex N/Cd take precedence over unused malformed point attributes; repair clears warnings")
    material.pop("color_attribute")
    material["color"] = (0.2, 0.7, 0.1)

    # Independent no-N motion reference. The broken sample is an endpoint,
    # while the centre has usable authored normals.
    stash.parm("stash").set(geometry())
    moving = obj.createNode("xform")
    moving.setInput(0, stash)
    moving.parm("tx").setExpression("$T*48", hou.exprLanguage.Hscript)
    reference = session.wait(session.apply(scene(moving, "deformation")))
    corrupt = obj.createNode("attribwrangle")
    corrupt.setInput(0, moving)
    corrupt.parm("snippet").set("v@N = @Time < 0 ? set(0,0,0) : set(0,0,1);")
    frame = session.wait(session.apply(scene(corrupt, "deformation")))
    issues = session.status()["source_diagnostics"]
    assert len(issues) == 1 and issues[0]["code"] == "geometry.invalid_normal", issues
    assert "shutter sample" in issues[0]["message"], issues
    for name in ("combined", "normal", "position"):
        assert np.allclose(plane(frame, name), plane(reference, name), atol=1e-5), name
    # A wide animated mesh has partial coverage near its centre-frame edge.
    assert 0.05 < plane(frame, "combined")[32, 28, 1] / 0.7 < 0.95
    checks.append(
        "An invalid shutter normal keeps deformation motion and matches independent geometry without authored normals"
    )
    corrupt.parm("snippet").set("v@N=set(0,0,1);")
    session.wait(session.apply(scene(corrupt, "deformation")))
    assert not session.status()["source_diagnostics"]
    checks.append("Repairing shutter normals restores authored normals and clears the warning")

report = dict(result="passed", device=device, checks=checks)
path = artifacts_root() / "stage3/materials/recovery/houdini-normal-recovery-smoke.json"
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(report, indent=2))
print(json.dumps(report))
