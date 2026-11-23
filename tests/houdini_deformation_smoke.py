# SPDX-License-Identifier: Apache-2.0
"""Subframe geometry, identity correspondence and sampled normals, independently checked."""

from support.paths import artifacts_root
from cycles_houdini.device_selection import set_devices as set_rop_devices
from array import array
import json
import math
import os
from pathlib import Path

import hou
import cycles_houdini as ch

destination = artifacts_root() / "stage2/motion/deformation"
destination.mkdir(parents=True, exist_ok=True)
hou.setFps(24)
hou.setFrame(1)
obj = hou.node("/obj").createNode("geo", "deformation_fixture", run_init_scripts=False)
camera = hou.node("/obj").createNode("cam", "deformation_camera")
camera.parm("tz").set(5)
camera.parm("projection").set(1)
camera.parm("orthowidth").set(4)


def source(name, code):
    result = obj.createNode("python", name)
    result.parm("python").set(code)
    return result


def mover(name, input_node):
    result = obj.createNode("xform", name)
    result.setInput(0, input_node)
    result.parm("tx").setExpression("$T * 48", hou.exprLanguage.Hscript)
    return result


points = source(
    "known_point",
    """import hou
g = hou.pwd().geometry(); g.addAttrib(hou.attribType.Point, 'id', 101); g.addAttrib(hou.attribType.Point, 'pscale', 0.2)
p = g.createPoint(); p.setPosition((0, 0, 0))
""",
)
point_motion = mover("point_deformation", points)
fallback_points = source(
    "points_without_pscale", points.evalParm("python").replace("; g.addAttrib(hou.attribType.Point, 'pscale', 0.2)", "")
)
fallback_motion = mover("fallback_point_deformation", fallback_points)
radius_motion = obj.createNode("attribwrangle", "radius_deformation")
radius_motion.setInput(0, point_motion)
radius_motion.parm("snippet").set("@pscale = 0.2 + @Time * 9.6;")
hair = source(
    "known_hair",
    """import hou
g = hou.pwd().geometry(); g.addAttrib(hou.attribType.Prim, 'id', 202); g.addAttrib(hou.attribType.Point, 'width', 0.2)
curve = g.createPolygon(is_closed=False)
for y in (-0.6, 0.6):
    p = g.createPoint(); p.setPosition((0, y, 0)); curve.addVertex(p)
""",
)
hair_motion = mover("hair_deformation", hair)
width_motion = obj.createNode("attribwrangle", "width_deformation")
width_motion.setInput(0, hair_motion)
width_motion.parm("snippet").set("@width = 0.2 + @Time * 9.6;")
mesh = source(
    "known_mesh",
    """import hou
g = hou.pwd().geometry(); g.addAttrib(hou.attribType.Point, 'id', 0)
poly = g.createPolygon()
# Keep identity attached to the same position, with native clockwise +Z winding.
# Authored +Z shutter normals must agree with the geometric front face.
for identity, xy in reversed(list(enumerate([(-0.2, -0.6), (0.2, -0.6), (0.2, 0.6), (-0.2, 0.6)]))):
    p = g.createPoint(); p.setPosition((xy[0], xy[1], 0)); p.setAttribValue('id', identity+100); poly.addVertex(p)
""",
)
mesh_motion = mover("mesh_deformation", mesh)
mesh_shear = obj.createNode("attribwrangle", "changing_mesh_shape")
mesh_shear.setInput(0, mesh)
mesh_shear.parm("snippet").set("@P.x += @Time * 48 * (1 + @P.y);")
normal_motion = obj.createNode("attribwrangle", "split_normal_deformation")
normal_motion.setInput(0, mesh_motion)
normal_motion.parm("snippet").set("v@N = set(abs(@Time) * 96, 0, 1 - abs(@Time) * 96);")
sort_mesh = obj.createNode("sort", "reordered_mesh")
sort_mesh.setInput(0, normal_motion)
sort_mesh.parm("ptsort").set(5)
sort_mesh.parm("primsort").set(4)
mesh_switch = obj.createNode("switch", "shutter_mesh_reorder")
mesh_switch.setInput(0, normal_motion)
mesh_switch.setInput(1, sort_mesh)
mesh_switch.parm("input").setExpression("$T > 0", hou.exprLanguage.Hscript)
two_points_code = """import hou
g = hou.pwd().geometry(); g.addAttrib(hou.attribType.Point, 'id', 0); g.addAttrib(hou.attribType.Point, 'pscale', 0.2)
for identity, x in [(10, -0.8), (20, 0.8)]:
    p = g.createPoint(); p.setPosition((x, 0, 0)); p.setAttribValue('id', identity)
"""
two_points = source("two_points", two_points_code)
two_motion = mover("two_point_deformation", two_points)
sort_points = obj.createNode("sort", "reordered_points")
sort_points.setInput(0, two_motion)
sort_points.parm("ptsort").set(5)
point_switch = obj.createNode("switch", "shutter_point_reorder")
point_switch.setInput(0, two_motion)
point_switch.setInput(1, sort_points)
point_switch.parm("input").setExpression("$T > 0", hou.exprLanguage.Hscript)
missing_ids = source(
    "ambiguous_points",
    two_points_code.replace("g.addAttrib(hou.attribType.Point, 'id', 0); ", "").replace(
        "; p.setAttribValue('id', identity)", ""
    ),
)
ambiguous_motion = mover("ambiguous_deformation", missing_ids)
ambiguous_sort = obj.createNode("sort", "ambiguous_sort")
ambiguous_sort.setInput(0, ambiguous_motion)
ambiguous_sort.parm("ptsort").set(5)
ambiguous_switch = obj.createNode("switch", "ambiguous_switch")
ambiguous_switch.setInput(0, ambiguous_motion)
ambiguous_switch.setInput(1, ambiguous_sort)
ambiguous_switch.parm("input").setExpression("$T > 0", hou.exprLanguage.Hscript)
changed_ids = obj.createNode("attribwrangle", "changing_point_ids")
changed_ids.setInput(0, point_motion)
changed_ids.parm("snippet").set("if (@Time > 0) i@id += 10000;")
# Use a time-dependent switch to an independently empty detail for the lifetime rejection.
empty = source("empty", "import hou\ng = hou.pwd().geometry()")
birth_switch = obj.createNode("switch", "birth_switch")
birth_switch.setInput(0, point_motion)
birth_switch.setInput(1, empty)
birth_switch.parm("input").setExpression("$T > 0", hou.exprLanguage.Hscript)
mixed_switch = obj.createNode("switch", "mixed_switch")
mixed_switch.setInput(0, mesh_motion)
mixed_switch.setInput(1, point_motion)
mixed_switch.parm("input").setExpression("$T > 0", hou.exprLanguage.Hscript)
pack = obj.createNode("pack", "unsupported_deforming_pack")
pack.setInput(0, mesh_motion)
two_hair = source(
    "two_hair",
    """import hou
g = hou.pwd().geometry(); g.addAttrib(hou.attribType.Prim, 'id', 0)
g.addAttrib(hou.attribType.Point, 'id', 0); g.addAttrib(hou.attribType.Point, 'width', 0.2)
for identity, x in [(10, -0.8), (20, 0.8)]:
    curve = g.createPolygon(is_closed=False); curve.setAttribValue('id', identity)
    for index, y in enumerate((-0.6, 0.6)):
        p = g.createPoint(); p.setPosition((x, y, 0)); p.setAttribValue('id', identity*10+index); curve.addVertex(p)
""",
)
two_hair_motion = mover("two_hair_motion", two_hair)
hair_sort = obj.createNode("sort", "reordered_hair")
hair_sort.setInput(0, two_hair_motion)
hair_sort.parm("ptsort").set(5)
hair_sort.parm("primsort").set(4)
hair_switch = obj.createNode("switch", "shutter_hair_reorder")
hair_switch.setInput(0, two_hair_motion)
hair_switch.setInput(1, hair_sort)
hair_switch.parm("input").setExpression("$T > 0", hou.exprLanguage.Hscript)


def scene(sop=point_motion, settings=None, now=0):
    return dict(
        geometry=[ch.geometry_request(obj, sop, material="motion", motion="deformation", at_time=now)],
        camera_path=camera.path(),
        background=(0, 0, 0),
        motion=dict(enabled=True, fps=24, shutter_frames=0.5, offset=0, samples=3) if settings is None else settings,
        materials=[dict(key="motion", kind="emission", color=(0.8, 0.1, 0.02))],
    )


def plane(frame, name):
    values = array("f")
    values.frombytes(frame["planes"][name]["pixels"])
    return values


def centre_mean(frame, name, channel=0, x=0, y=0):
    values = plane(frame, name)
    channels = frame["planes"][name]["channels"]
    column = round((x / 4 + 0.5) * frame["width"])
    row = round((y / 2.25 + 0.5) * frame["height"])
    return (
        sum(
            values[(yy * frame["width"] + xx) * channels + channel]
            for yy in (row - 1, row)
            for xx in (column - 1, column)
        )
        / 4
    )


reports = []
for device in ("CPU", ch.default_device()):
    camera.parm("tz").set(5)
    point_motion.parm("tx").setExpression("$T * 48", hou.exprLanguage.Hscript)
    checks = []
    with ch.Session(device=device, width=160, height=90, samples=2048, interactive=False) as session:

        def render(name, update, expected, x=0, y=0, now=0):
            before = hou.time()
            frame = session.wait(session.apply(update, at_time=now))
            assert hou.time() == before
            measured = centre_mean(frame, "combined", x=x, y=y) / 0.8
            assert abs(measured - expected) < 0.035, (name, measured, expected)
            checks.append(
                dict(name=name, analytic_coverage=expected, measured_coverage=measured, status=session.status())
            )
            return frame

        render("Subframe point deformation", scene(), 0.4)
        initial = session.status()
        camera.parm("tz").set(6)
        render("Warm camera edit retains sampled geometry", scene(), 0.4)
        assert session.status()["geometry_conversions"] == initial["geometry_conversions"]
        assert session.status()["motion_cooks"] == 0, session.status()
        camera.parm("tz").set(5)
        render(
            "Five-step deformation preserves a linear trajectory",
            scene(settings=dict(scene()["motion"], samples=5)),
            0.4,
        )
        for offset in (-1, 1):
            render(
                f"Deformation shutter offset {offset:+}",
                scene(settings=dict(scene()["motion"], offset=offset)),
                0.4,
                x=offset * 0.5,
            )
        point_motion.parm("tx").setExpression("($T - 0.125) * 48", hou.exprLanguage.Hscript)
        render("Explicit subframe render time preserves the timeline", scene(now=0.125), 0.4, now=0.125)
        point_motion.parm("tx").setExpression("$T * 48", hou.exprLanguage.Hscript)
        render("Point radius is sampled across the shutter", scene(radius_motion), 5 / 12)
        render("Linear hair deformation with stable topology IDs", scene(hair_motion), 0.2)
        render("Hair widths are sampled across the shutter", scene(width_motion), 5 / 24)
        render("Polygon mesh deformation", scene(mesh_motion), 0.4)
        render("Changing mesh shape at its centre", scene(mesh_shear), 0.4)
        render("Changing mesh shape at the faster upper section", scene(mesh_shear), 0.4 / 1.3, y=0.3)
        render("Changing mesh shape at the slower lower section", scene(mesh_shear), 0.4 / 0.7, y=-0.3)
        frame = render("Authored normals are sampled with mesh positions", scene(normal_motion), 0.4)
        # At centre pixels the width-.4 plane is visible for x in [-.2,.2]
        # along its distance-1 trajectory. Endpoint normals are (1,0,0),
        # centre normal (0,0,1), interpolated then normalized by the engine.
        steps = 4000
        expected_x = (
            sum(
                (2 * abs(-0.2 + (i + 0.5) * 0.4 / steps))
                / math.hypot(2 * abs(-0.2 + (i + 0.5) * 0.4 / steps), 1 - 2 * abs(-0.2 + (i + 0.5) * 0.4 / steps))
                for i in range(steps)
            )
            * 0.4
            / steps
        )
        measured_x = centre_mean(frame, "normal", channel=0)
        assert abs(measured_x - expected_x) < 0.009, (measured_x, expected_x)
        checks.append(
            dict(name="Independent sampled-normal integral", analytic_normal_x=expected_x, measured_normal_x=measured_x)
        )
        session.write_exr(destination / ("cpu-normals.exr" if device == "CPU" else "optix-normals.exr"))
        reordered = render("Mesh point/face reorder keeps identity and corner normals", scene(mesh_switch), 0.4)
        assert abs(centre_mean(reordered, "normal", channel=0) - expected_x) < 0.009
        render("Point reorder matches persistent IDs", scene(point_switch), 0.4, x=-0.8)
        render("Reordered second point retains its trajectory", scene(point_switch), 0.4, x=0.8)
        render("Curve/point reorder preserves strand identities", scene(hair_switch), 0.2, x=-0.8)
        render("Reordered second strand retains its trajectory", scene(hair_switch), 0.2, x=0.8)

        def recover(name, update, text):
            frame = session.wait(session.apply(update, at_time=0))
            status = session.status()
            assert status["state"] == "complete" and not status["error"], status
            assert any(text.lower() in d["message"].lower() for d in status["source_diagnostics"]), status
            assert frame["planes"]["combined"]["pixels"]
            checks.append(dict(name=name, recovered_with_nominal_geometry=True))

        # Index fallback follows current ordering, including a deliberate Sort.
        # No user ID is required; explicit IDs above preserve physical identity
        # through that same reorder. These are intentionally distinct policies.
        implicit_reorder = session.wait(session.apply(scene(ambiguous_switch), at_time=0))
        assert implicit_reorder["planes"]["combined"]["pixels"]
        checks.append(dict(name="Reordered points without IDs use index correspondence"))
        for base_node, expected in ((points, 0.4), (hair, 0.2), (mesh, 0.4)):
            code = base_node.evalParm("python")
            # Force a newly built detail at every shutter time, retaining order.
            code += """\nfor owner in (hou.attribType.Point, hou.attribType.Prim):
    attribute = g.findPointAttrib('id') if owner == hou.attribType.Point else g.findPrimAttrib('id')
    if attribute is not None: attribute.destroy()
for point in g.points(): point.setPosition(point.position() + hou.Vector3(hou.time()*48, 0, 0))
"""
            rebuilt = source("implicit_recooked_" + base_node.name(), code)
            render("Recooked " + base_node.name() + " uses automatic index correspondence", scene(rebuilt), expected)
        recover("Persistent identities may not change", scene(changed_ids), "identities change")
        recover("Particle lifetime changes need another blur method", scene(birth_switch), "born/deleted")
        recover("Changing primitive kinds cannot be interpolated", scene(mixed_switch), "Geometry type")
        render("Packed prototype deformation uses native shutter contents", scene(pack), 0.4)
        render("Valid deformation recovers after rejected inputs", scene(), 0.4)
        point_motion.parm("tx").setExpression("$T * 24", hou.exprLanguage.Hscript)
        render("A centre-preserving motion edit still invalidates the cache", scene(), 0.8)
        point_motion.parm("tx").setExpression("$T * 48", hou.exprLanguage.Hscript)
        render("Motion disabled samples the nominal frame", scene(settings=dict(scene()["motion"], enabled=False)), 1)
        render("Deformation re-enabled", scene(), 0.4)
        fallback_scene = scene(fallback_motion)
        fallback_scene["geometry"][0]["point_radius"] = 0.2
        render("Deformation fallback radius is part of its payload", fallback_scene, 0.4)
        fallback_scene["geometry"][0]["point_radius"] = 0.1
        render("Fallback radius edits invalidate deformation samples", fallback_scene, 0.2)
        second = dict(fallback_scene["geometry"][0], point_radius=0.2, object_key="second-deformation-object")
        session.wait(session.apply(dict(fallback_scene, geometry=fallback_scene["geometry"] + [second]), at_time=0))
        assert session.status()["geometry_resources"] == session.status()["object_resources"] == 2
        checks.append(dict(name="Different deformation defaults use explicit variants", resources=2))
        session.wait(session.apply(scene(), at_time=0))
        retained = session.status()["device_allocation_bytes"]
        converted = session.status()["geometry_conversions"]
        for _ in range(6):
            session.wait(session.apply(scene(), at_time=0))
            assert session.status()["device_allocation_bytes"] == retained
            assert session.status()["geometry_conversions"] == converted
        session.close()
        assert session.status()["device_allocation_bytes"] == 0
    reports.append(dict(device=device, checks=checks, repeated_updates=6))

rop = hou.node("/out").createNode("cycles_ipr", "cycles_deformation")
set_rop_devices(rop, ch.default_device())
for name, value in dict(
    soppath=mesh_shear.path(),
    camera=camera.path(),
    resx=160,
    resy=90,
    samples=2048,
    allowmotionblur=1,
    geometry_motion="deformation",
    material_kind="emission",
).items():
    rop.parm(name).set(value)
rop.parmTuple("basecolor").set((0.8, 0.1, 0.02))
rop.parmTuple("background").set((0, 0, 0))
mesh_shear.setRenderFlag(True)
hou.hipFile.save(str(destination / "native-deformation-motion.hiplc"))
report = dict(
    result="passed",
    fixtures=reports,
    scope="Unpacked and packed mesh/points/linear hair with fixed topology or explicit identity; incompatible shutter geometry retains requested-frame contents",
)
(destination / "houdini-deformation-smoke.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(
    json.dumps(
        dict(
            result="passed",
            checks_per_device=[len(item["checks"]) for item in reports],
            evidence=str(destination / "houdini-deformation-smoke.json"),
        )
    )
)
