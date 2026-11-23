# SPDX-License-Identifier: Apache-2.0
"""Unpacked mixed SOPs, with independent camera/color/shape expectations."""

from support.paths import artifacts_root
import json
import math
import os
from pathlib import Path

import hou
import numpy as np
import cycles_houdini as ch

destination = artifacts_root() / "stage3/mixed-geometry"
destination.mkdir(parents=True, exist_ok=True)
obj = hou.node("/obj").createNode("geo", "mixed_geometry_fixture", run_init_scripts=False)
source = obj.createNode("python", "unpacked_mesh_curve_points")
script = """import hou
g=hou.pwd().geometry()
g.addAttrib(hou.attribType.Point,'id',0)
g.addAttrib(hou.attribType.Point,'pscale',.12)
g.addAttrib(hou.attribType.Point,'Cd',(0.,1.,0.))
g.addAttrib(hou.attribType.Vertex,'width',.12)
g.addAttrib(hou.attribType.Vertex,'uv',(0.,0.,0.))
points=[]
for i,p in enumerate(((-1.,-.6,0.),(-.2,-.6,0.),(-.2,.6,0.),(-1.,.6,0.),(.22,.35,0.),(.22,-.55,0.),(.7,.18,0.),(.7,-.38,0.))):
    point=g.createPoint();point.setPosition(p);point.setAttribValue('id',100+i)
    point.setAttribValue('Cd',(1.,0.,0.) if i<4 else (0.,0.,1.) if i<6 else (0.,1.,0.));points.append(point)
face=g.createPolygon()
for i in (0,1,2,3):
    v=face.addVertex(points[i]);v.setAttribValue('uv',((i in (1,2))*1.,(i>=2)*1.,0.))
curve=g.createPolygon();curve.setIsClosed(False)
for i in (2,4,5):
    curve.addVertex(points[i])
"""
source.parm("python").set(script)
source.setRenderFlag(True)
assert source.geometry().points()[6].attribValue("Cd") == (0.0, 1.0, 0.0)
camera = hou.node("/obj").createNode("cam", "mixed_camera")
camera.parm("tz").set(3)
camera.parm("projection").set(1)
camera.parm("orthowidth").set(3)


def sample(frame, name, x, y):
    plane = frame["planes"][name]
    count = plane["channels"]
    pixels = np.frombuffer(plane["pixels"], np.float32).reshape(frame["height"], frame["width"], count)
    px = round((x / 3 + 0.5) * frame["width"])
    py = round((y / 2 + 0.5) * frame["height"])
    return pixels[py, px]


reports = []
for device in ("CPU", ch.default_device()):
    checks = []
    with ch.Session(device=device, width=240, height=160, samples=256) as session:
        request = ch.geometry_request(obj, source, material="mixed")
        scene = dict(
            geometry=[request],
            camera_path=camera.path(),
            background=(0, 0, 0),
            transparent_background=True,
            materials=[dict(key="mixed", kind="emission", color=(1, 1, 1), color_attribute="Cd", strength=1)],
        )
        frame = session.wait(session.apply(scene))
        status = session.status()
        assert status["geometry_resources"] == status["object_resources"] == 3, status
        assert status["points"] == 2 and status["curves"] == 1, status
        assert status["mesh_variant_resources"] == 1, status
        assert np.allclose(sample(frame, "combined", -0.6, 0), (1, 0, 0, 1), atol=0.0001)
        assert np.allclose(sample(frame, "combined", 0.22, -0.15), (0, 0, 1, 1), atol=0.0001)
        assert np.allclose(sample(frame, "combined", 0.7, 0.18), (0, 1, 0, 1), atol=0.0001), sample(
            frame, "combined", 0.7, 0.18
        )
        position = sample(frame, "position", 0.7, 0.18)
        expected = math.sqrt(0.12**2 - (float(position[0]) - 0.7) ** 2 - (float(position[1]) - 0.18) ** 2)
        assert abs(float(position[2]) - expected) < 0.002, (position, expected)
        assert abs(float(sample(frame, "depth", 0.7, 0.18)[0]) - (3 - float(position[2]))) < 0.002
        assert np.allclose(sample(frame, "position", -0.6, 0)[2], 0, atol=0.0001)
        assert np.allclose(sample(frame, "normal", -0.6, 0), (0, 0, 1), atol=0.0001)
        mesh_position = sample(frame, "position", -0.6, 0)
        expected_uv = ((float(mesh_position[0]) + 1) / 0.8, (float(mesh_position[1]) + 0.6) / 1.2)
        assert np.allclose(sample(frame, "uv", -0.6, 0)[:2], expected_uv, atol=0.0001)
        assert np.allclose(sample(frame, "combined", 1.3, 0.8), (0, 0, 0, 0), atol=0.0001)
        checks.append("three independent primitive domains; shared curve/mesh point is not a particle")
        conversions = status["geometry_conversions"]
        objects = session.native_object_keys()
        camera.parm("tx").set(0.1)
        session.wait(session.apply(dict(geometry=[request], camera_path=camera.path())))
        assert session.status()["geometry_conversions"] == conversions
        assert session.status()["partition_scans"] == status["partition_scans"]
        assert session.status()["component_checks"] == status["component_checks"]
        assert session.native_object_keys() == objects
        checks.append("camera edit retains geometry and component identities")
        source.parm("python").set(script.replace("(.7,.18,0.)", "(.8,.18,0.)"))
        session.wait(session.apply(dict(geometry=[request])))
        assert session.status()["geometry_conversions"] == conversions + 1, session.status()
        assert session.status()["component_reuses"] >= status["component_reuses"] + 2, session.status()
        assert session.status()["points"] == 2 and session.status()["curves"] == 1
        source.parm("python").set(script)
        session.wait(session.apply(dict(geometry=[request])))
        assert session.native_object_keys() == objects
        assert session.status()["geometry_conversions"] == conversions + 2, session.status()
        checks.append("loose-point position edits update only particles and retain mesh/hair without conversion")
        camera.parm("tx").set(0)
        before = session.status()["geometry_conversions"]
        source.parm("python").set(script + "\npoints[6].setAttribValue('Cd',(.3,.7,.2))\n")
        changed = session.wait(session.apply(dict(geometry=[request], camera_path=camera.path())))
        assert session.status()["geometry_conversions"] == before + 1, session.status()
        assert np.allclose(sample(changed, "combined", 0.7, 0.18), (0.3, 0.7, 0.2, 1), atol=0.0001)
        source.parm("python").set(script)
        session.wait(session.apply(dict(geometry=[request])))
        checks.append("point-only color edits retain mesh/hair and render the independently specified color")
        before = session.status()["geometry_conversions"]
        source.parm("python").set(
            script.replace("curve.addVertex(points[i])", "curve.addVertex(points[i]).setAttribValue('width',.2)")
        )
        session.wait(session.apply(dict(geometry=[request])))
        assert session.status()["geometry_conversions"] == before + 1, session.status()
        source.parm("python").set(script)
        session.wait(session.apply(dict(geometry=[request])))
        checks.append("hair vertex width edits update only curves")
        # A merged SOP carries several types' attributes on every element.
        # Edits outside a converter's supported inputs must retain its resource.
        decorated = (
            script
            + """
g.addAttrib(hou.attribType.Prim,'id',0)
g.addAttrib(hou.attribType.Point,'age',0.)
g.addAttrib(hou.attribType.Point,'life',10.)
g.addAttrib(hou.attribType.Vertex,'N',(0.,0.,-1.))
for p in g.prims():p.setAttribValue('id',500+p.number())
"""
        )
        source.parm("python").set(decorated)
        session.wait(session.apply(dict(geometry=[request])))
        before = session.status()["geometry_conversions"]
        for label, code in (
            ("Mesh primitive identity is not a hair/particle identity", "g.prims()[0].setAttribValue('id',700)"),
            (
                "Mesh point identities do not affect transform-only mesh/hair rendering",
                "for p in points[:4]:p.setAttribValue('id',900+p.number())",
            ),
            (
                "Mesh/shared-hair point pscale does not affect hair widths or unrelated particles",
                "for p in points[:4]:p.setAttribValue('pscale',.35)",
            ),
            (
                "Particle lifetime attributes outside the particle selection do not rebuild mesh or hair",
                "for p in points[:4]:p.setAttribValue('age',3.)",
            ),
            (
                "Hair vertex normals do not rebuild hair or unrelated mesh normals",
                "for v in g.prims()[1].vertices():v.setAttribValue('N',(1.,0.,0.))",
            ),
        ):
            source.parm("python").set(decorated + "\n" + code + "\n")
            unchanged = session.wait(session.apply(dict(geometry=[request])))
            assert session.status()["geometry_conversions"] == before, (label, session.status())
            assert np.allclose(sample(unchanged, "combined", -0.6, 0), (1, 0, 0, 1), atol=0.0001), label
            assert np.allclose(sample(unchanged, "combined", 0.22, -0.15), (0, 0, 1, 1), atol=0.0001), label
            assert np.allclose(sample(unchanged, "combined", 0.7, 0.18), (0, 1, 0, 1), atol=0.0001), label
            checks.append(label)
        source.parm("python").set(decorated + "\npoints[6].setAttribValue('pscale',.18)\n")
        resized = session.wait(session.apply(dict(geometry=[request])))
        assert session.status()["geometry_conversions"] == before + 1, session.status()
        assert abs(float(sample(resized, "position", 0.7, 0.18)[2])) > 0.17
        checks.append("Particle pscale still updates its own resource and independently specified sphere depth")
        source.parm("python").set(script)
        session.wait(session.apply(dict(geometry=[request])))
        before = session.status()["geometry_conversions"]
        source.parm("python").set(script.replace("(-.2,.6,0.)", "(-.25,.6,0.)"))
        session.wait(session.apply(dict(geometry=[request])))
        assert session.status()["geometry_conversions"] == before + 2, session.status()
        source.parm("python").set(script)
        session.wait(session.apply(dict(geometry=[request])))
        checks.append("editing a shared mesh/hair point updates both consumers and retains particles")
        subdivided = dict(
            request, subdivision=dict(type="linear", space="object", dicing_rate=0.1, max_level=2, triangle_limit=20000)
        )
        subdivided_frame = session.wait(session.apply(dict(geometry=[subdivided], camera_path=camera.path())))
        assert (
            session.status()["subdivision_control_faces"] == 1
            and session.status()["points"] == 2
            and session.status()["curves"] == 1
        )
        assert np.allclose(sample(subdivided_frame, "combined", -0.6, 0), (1, 0, 0, 1), atol=0.0001)
        assert np.allclose(sample(subdivided_frame, "combined", 0.22, -0.15), (0, 0, 1, 1), atol=0.0001)
        assert np.allclose(sample(subdivided_frame, "combined", 0.7, 0.18), (0, 1, 0, 1), atol=0.0001)
        session.wait(session.apply(dict(geometry=[request])))
        checks.append(
            "subdivision affects only the filtered mesh while curve and point domains keep their shapes and colors"
        )
        before = session.status()
        generation = before.get("committed_generation", before["generation"])
        frame_generation = session.frame_info()["generation"]
        try:
            session.wait(session.apply(dict(deleted_geometry=[request["geometry_key"]])))
        except RuntimeError:
            pass
        else:
            raise AssertionError("Logical SOP geometry was deleted while component objects still reference it")
        rejected = session.status()
        assert rejected.get("committed_generation", rejected["generation"]) == generation, rejected
        assert (
            rejected["geometry_resources"] == before["geometry_resources"]
            and rejected["object_resources"] == before["object_resources"]
        ), rejected
        assert session.frame_info()["generation"] == frame_generation
        session.wait(
            session.apply(dict(deleted_objects=[request["object_key"]], deleted_geometry=[request["geometry_key"]]))
        )
        assert (
            session.status()["geometry_resources"]
            == session.status()["object_resources"]
            == session.status()["partition_cache_bytes"]
            == 0
        )
        session.wait(session.apply(dict(geometry=[request])))
        assert session.native_object_keys() == objects
        checks.append(
            "logical source deletion rejects dangling ownership, retires all components and supports recreation"
        )
        session.wait(session.apply(dict(geometry=[])))
        assert session.status()["geometry_resources"] == session.status()["object_resources"] == 0
        checks.append("component retirement")
    assert session.status()["owned_geometry_bytes"] == session.status()["device_allocation_bytes"] == 0
    camera.parm("tx").set(0)
    reports.append(dict(device=device, checks=checks, status=status))
(destination / "houdini-mixed-geometry-smoke.json").write_text(
    json.dumps(dict(result="passed", devices=reports), indent=2), encoding="utf-8"
)
print(json.dumps(dict(result="passed", checks_per_device=[len(r["checks"]) for r in reports])))
