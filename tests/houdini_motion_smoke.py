# SPDX-License-Identifier: Apache-2.0
"""Native shutter units and velocity/transform blur against analytic coverage."""

from support.paths import artifacts_root
from cycles_houdini.device_selection import set_devices as set_rop_devices
from array import array
import argparse
import json
import math
import os
from pathlib import Path

import hou
import cycles_houdini as ch
from cycles_houdini import _native, native_scene

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
requested_device = parser.parse_args().device

destination = artifacts_root() / "stage2/motion"
destination.mkdir(parents=True, exist_ok=True)
hou.setFps(24)
hou.setFrame(1)
obj = hou.node("/obj").createNode("geo", "native_motion", run_init_scripts=False)
source = obj.createNode("python", "velocity_points")
point_code = """import hou
g = hou.pwd().geometry()
g.addAttrib(hou.attribType.Point, 'pscale', 0.2)
g.addAttrib(hou.attribType.Point, 'id', 101)
g.addAttrib(hou.attribType.Point, 'v', (48.0, 0.0, 0.0))
p = g.createPoint(); p.setPosition((0, 0, 0))
"""
source.parm("python").set(point_code)
hair = obj.createNode("python", "velocity_hair")
hair_code = """import hou
g = hou.pwd().geometry()
g.addAttrib(hou.attribType.Prim, 'id', 202)
g.addAttrib(hou.attribType.Point, 'width', 0.2)
g.addAttrib(hou.attribType.Point, 'v', (48.0, 0.0, 0.0))
curve = g.createPolygon(is_closed=False)
for y in (-0.6, 0.6):
    p = g.createPoint(); p.setPosition((0, y, 0)); curve.addVertex(p)
"""
hair.parm("python").set(hair_code)
mesh = obj.createNode("python", "velocity_mesh")
mesh.parm("python").set("""import hou
g = hou.pwd().geometry(); g.addAttrib(hou.attribType.Point, 'v', (48.0, 0.0, 0.0))
face = g.createPolygon()
for x, y in [(-0.2, -0.6), (0.2, -0.6), (0.2, 0.6), (-0.2, 0.6)]:
    p = g.createPoint(); p.setPosition((x, y, 0)); face.addVertex(p)
""")
pack = obj.createNode("pack", "rigid_body")
pack.setInput(0, mesh)
identified = obj.createNode("python", "rigid_identity")
identified.setInput(0, pack)
identified.parm("python").set("""import hou
g = hou.pwd().geometry(); g.addAttrib(hou.attribType.Prim, 'name', 'body-A')
for primitive in g.prims(): primitive.setAttribValue('name', 'body-A')
""")
rigid = obj.createNode("xform", "rigid_motion")
rigid.setInput(0, identified)
rigid.parm("tx").setExpression("$T * 48", hou.exprLanguage.Hscript)
camera = hou.node("/obj").createNode("cam", "native_motion_camera")
camera.parm("tz").set(5)
camera.parm("projection").set(1)
camera.parm("orthowidth").set(4)
assert camera.parm("shutter") is not None
camera.parm("shutter").set(0.5)
rop = hou.node("/out").createNode("cycles_ipr", "cycles_native_motion")
for name, value in dict(
    source_mode="sop",
    soppath=source.path(),
    camera=camera.path(),
    resx=160,
    resy=90,
    samples=2048,
    allowmotionblur=1,
    geometry_motion="velocity",
    material_kind="emission",
).items():
    rop.parm(name).set(value)
rop.parmTuple("basecolor").set((0.8, 0.1, 0.02))
rop.parmTuple("background").set((0, 0, 0))
source.setRenderFlag(True)


def motion(**changes):
    return dict(dict(enabled=True, fps=24.0, shutter_frames=0.5, offset=0.0, samples=3), **changes)


def scene(sop=source, mode="velocity", settings=None, now=0.0):
    return dict(
        geometry=[ch.geometry_request(obj, sop, material="motion", motion=mode, at_time=now)],
        camera_path=camera.path(),
        background=(0, 0, 0),
        motion=motion() if settings is None else settings,
        materials=[dict(key="motion", kind="emission", color=(0.8, 0.1, 0.02))],
    )


def red(frame):
    values = array("f")
    values.frombytes(frame["planes"]["combined"]["pixels"])
    assert all(math.isfinite(value) for value in values)
    return values[0::4]


def coverage(frame, centre=0.0):
    values = red(frame)
    x = round((centre / 4 + 0.5) * frame["width"])
    y = frame["height"] // 2
    return sum(values[row * frame["width"] + column] for row in (y - 1, y) for column in (x - 1, x)) / (4 * 0.8)


def centroid(frame):
    values = red(frame)
    total = sum(values)
    assert total > 0
    return sum(value * ((i % frame["width"] + 0.5) / frame["width"] * 4 - 2) for i, value in enumerate(values)) / total


reports = []
for device in (requested_device,):
    camera.parm("tx").deleteAllKeyframes()
    camera.parm("tx").set(0)
    camera.parm("orthowidth").deleteAllKeyframes()
    camera.parm("orthowidth").set(4)
    obj.parm("tx").deleteAllKeyframes()
    obj.parm("tx").set(0)
    source.parm("python").set(point_code)
    checks = []
    with ch.Session(device=device, width=160, height=90, samples=2048, interactive=False) as session:

        def render(name, update, expected, centre=0.0, now=0.0):
            before = hou.time()
            frame = session.wait(session.apply(update, at_time=now))
            assert hou.time() == before, "Subframe capture moved the Houdini timeline"
            measured = coverage(frame, centre)
            assert abs(measured - expected) < 0.035, (name, measured, expected)
            measured_centre = centroid(frame)
            assert abs(measured_centre - centre) < 0.012, (name, measured_centre, centre)
            checks.append(
                dict(
                    name=name,
                    analytic_coverage=expected,
                    measured_coverage=measured,
                    analytic_centroid=centre,
                    measured_centroid=measured_centre,
                    status=session.status(),
                )
            )
            return frame

        frame = render("Point velocity, 24 FPS, half-frame shutter", scene(), 0.4)
        initial = session.status()
        assert abs(initial["shutter_open_seconds"] + 1 / 96) < 1e-12
        assert abs(initial["shutter_close_seconds"] - 1 / 96) < 1e-12
        session.write_exr(destination / ("native-velocity-cpu.exr" if device == "CPU" else "native-velocity-optix.exr"))
        camera.parm("tz").set(6)
        render("Camera edit retains velocity samples", scene(), 0.4)
        assert session.status()["geometry_conversions"] == initial["geometry_conversions"]
        assert session.status()["extractions"] == initial["extractions"]
        camera.parm("tz").set(5)
        render("Absolute render time retains an unchanged velocity snapshot", scene(now=0.125), 0.4, now=0.125)
        assert session.status()["geometry_conversions"] == initial["geometry_conversions"]
        render("Double FPS halves the velocity distance", scene(settings=motion(fps=48)), 0.8)
        render("Quarter-frame shutter halves the velocity distance", scene(settings=motion(shutter_frames=0.25)), 0.8)
        render("Five samples retain the same linear trajectory", scene(settings=motion(samples=5)), 0.4)
        for offset in (-1.0, 1.0):
            render(
                f"Velocity shutter offset {offset:+}", scene(settings=motion(offset=offset)), 0.4, centre=offset * 0.5
            )
        render("Zero shutter is static", scene(settings=motion(shutter_frames=0)), 1.0)
        assert session.status()["motion_samples"] == 0
        render("Motion disabled is static", scene(settings=motion(enabled=False)), 1.0)
        render("Velocity re-enabled", scene(), 0.4)

        obj.parm("tx").setExpression("$T * 48", hou.exprLanguage.Hscript)
        render("Native OBJ transform", scene(mode="transform"), 0.4)
        converted = session.status()["geometry_conversions"]
        camera.parm("tz").set(6)
        render("Transform/camera edit keeps geometry", scene(mode="transform"), 0.4)
        assert session.status()["geometry_conversions"] == converted
        camera.parm("tz").set(5)
        for offset in (-1.0, 1.0):
            render(
                f"OBJ shutter offset {offset:+}",
                scene(mode="transform", settings=motion(offset=offset)),
                0.4,
                centre=offset * 0.5,
            )
        obj.parm("tx").setExpression("($T - 0.125) * 48", hou.exprLanguage.Hscript)
        render("Explicit render time differs from the timeline", scene(mode="transform", now=0.125), 0.4, now=0.125)
        assert hou.time() == 0
        obj.parm("tx").setExpression("$T * 48", hou.exprLanguage.Hscript)
        camera.parm("tx").setExpression("$T * 48", hou.exprLanguage.Hscript)
        render("Matched object/camera motion cancels relatively", scene(mode="transform"), 1.0)
        obj.parm("tx").deleteAllKeyframes()
        obj.parm("tx").set(0)
        render("Native camera transform", scene(mode="transform"), 0.4)
        render("Camera trailing shutter", scene(mode="transform", settings=motion(offset=1)), 0.4, centre=-0.5)
        camera.parm("tx").deleteAllKeyframes()
        camera.parm("tx").set(0)

        render("Linear hair velocity", scene(hair), 0.2)
        render("Polygon mesh velocity", scene(mesh), 0.4)
        render("Packed rigid transform", scene(rigid, "transform"), 0.4)
        assert session.status()["geometry_resources"] == session.status()["object_resources"] == 1
        assert session.status()["motion_cooks"] == 2
        packed_keys = session.native_object_keys()
        converted = session.status()["geometry_conversions"]
        camera.parm("tz").set(6)
        render("Packed camera edit keeps the prototype", scene(rigid, "transform"), 0.4)
        assert session.status()["geometry_conversions"] == converted
        camera.parm("tz").set(5)
        render(
            "Packed trailing shutter uses its actual centre pose",
            scene(rigid, "transform", motion(offset=1)),
            0.4,
            centre=0.5,
        )
        assert session.native_object_keys() == packed_keys
        assert session.status()["motion_cooks"] == 3

        def reject(name, update, text):
            before = session.status()
            generation = before.get("committed_generation", before["generation"])
            keys = session.native_object_keys()
            frame_generation = session.frame_info()["generation"]
            try:
                session.wait(session.apply(update, at_time=0))
            except RuntimeError as error:
                assert text.lower() in str(error).lower(), (name, str(error), text)
            else:
                raise AssertionError("Invalid motion accepted: " + name)
            rejected = session.status()
            assert rejected.get("committed_generation", rejected["generation"]) == generation
            if "committed_generation" in rejected:
                # Captured desired state can advance before engine validation;
                # the acknowledged renderer scene/image must remain intact.
                assert rejected["object_resources"] == before["object_resources"]
                assert session.frame_info()["generation"] == frame_generation
            else:
                assert session.native_object_keys() == keys
            checks.append(dict(name=name, accepted_renderer_unchanged=True))

        render("Static packed geometry needs no authored identity", scene(pack, "transform"), 1.0)
        rigid.setInput(0, pack)
        render("Automatic packed identity supports rigid blur", scene(rigid, "transform"), 0.4)
        implicit_keys = session.native_object_keys()
        assert any("/packed:index:" in key for key in implicit_keys)
        for offset in (-1.0, 1.0):
            render(
                f"Automatic packed identity shutter offset {offset:+}",
                scene(rigid, "transform", motion(offset=offset)),
                0.4,
                centre=offset * 0.5,
            )
            assert session.native_object_keys() == implicit_keys
        rigid.setInput(0, identified)
        camera.parm("orthowidth").setExpression("4 + $T", hou.exprLanguage.Hscript)
        render("Animated orthographic width samples shutter projection", scene(), 0.4)
        assert ch.capabilities()["native_animated_camera_intrinsics"]
        capture = _native.create(device, 160, 90, 2048, False, False, True, False, None, True)
        try:
            sampled = _native.apply(capture, scene(), 0.0)
            assert _native.snapshot_info(sampled)["camera_intrinsic_samples"] == motion()["samples"]
        finally:
            _native.close(capture)
        assert not session.status()["source_diagnostics"], session.status()
        camera.parm("orthowidth").deleteAllKeyframes()
        camera.parm("orthowidth").set(4)
        reject("Even sample counts are rejected", scene(settings=motion(samples=4)), "odd")
        reject("Negative shutter is rejected", scene(settings=motion(shutter_frames=-0.1)), "shutter")
        source.parm("python").set(point_code.replace("g.addAttrib(hou.attribType.Point, 'v', (48.0, 0.0, 0.0))", ""))
        render("Missing point velocity renders static geometry", scene(), 1.0)
        source.parm("python").set(point_code)
        render("Adding velocity restores point blur", scene(), 0.4)
        for input_node, expected in ((mesh, 1.0), (hair, 1.0)):
            original = input_node.evalParm("python")
            input_node.parm("python").set(original + "\ng.findPointAttrib('v').destroy()\n")
            render("Missing " + input_node.name() + " velocity renders static geometry", scene(input_node), expected)
            input_node.parm("python").set(original)
            render(
                "Restoring " + input_node.name() + " velocity restores blur",
                scene(input_node),
                0.4 if input_node == mesh else 0.2,
            )
        source.parm("python").set(point_code.replace("(48.0, 0.0, 0.0)", '(float("nan"), 0.0, 0.0)'))
        render("Nonfinite velocity renders static geometry", scene(), 1.0)
        assert any("finite" in warning and source.path() in warning for warning in session.status()["motion_warnings"])
        source.parm("python").set(point_code)
        render("Repairing velocity clears its warning", scene(), 0.4)
        assert not session.status()["motion_warnings"]
        for input_node, expected_blur in ((source, 0.4), (mesh, 0.4), (hair, 0.2)):
            original = input_node.evalParm("python")
            for bad in (
                "g.findPointAttrib('v').destroy(); g.addAttrib(hou.attribType.Point, 'v', 1.0)",
                "[p.setAttribValue('v', (float('inf'), 0., 0.)) for p in g.points()]",
            ):
                input_node.parm("python").set(original + "\n" + bad)
                render("Invalid " + input_node.name() + " velocity keeps geometry visible", scene(input_node), 1.0)
                assert session.status()["motion_warnings"]
                session.wait(session.apply(scene(input_node), at_time=0))
                assert session.status()["motion_warnings"], "Cache reuse lost warning"
            input_node.parm("python").set(original)
            render("Repaired " + input_node.name() + " restores blur", scene(input_node), expected_blur)
            assert not session.status()["motion_warnings"]
        # A single OBJ may contain independent mesh and curve partitions.
        # Invalid hair velocity must not freeze the valid mesh beside it.
        mixed = obj.createNode("merge", "mixed_velocity_fallback")
        shifted_mesh = obj.createNode("xform")
        shifted_mesh.setInput(0, mesh)
        shifted_mesh.parm("tx").set(-1)
        bad_hair = obj.createNode("python")
        bad_hair.parm("python").set(
            hair_code
            + "\nfor p in g.points():\n p.setPosition(p.position() + hou.Vector3(1,0,0)); p.setAttribValue('v', (float('nan'),0.,0.))\n"
        )
        mixed.setInput(0, shifted_mesh)
        mixed.setInput(1, bad_hair)
        frame = session.wait(session.apply(scene(mixed), at_time=0))
        assert abs(coverage(frame, -1) - 0.4) < 0.035 and abs(coverage(frame, 1) - 1) < 0.035
        assert len(session.status()["motion_warnings"]) == 1
        checks.append(
            dict(name="Invalid curve velocity preserves valid mesh blur within one OBJ", status=session.status())
        )
        mixed.destroy()
        shifted_mesh.destroy()
        bad_hair.destroy()
        obj.parm("sx").setExpression("$T * 48", hou.exprLanguage.Hscript)
        reject("Changing shutter transform handedness is rejected", scene(mode="transform"), "handedness")
        obj.parm("sx").deleteAllKeyframes()
        obj.parm("sx").set(1)
        # Scale parameters clamp zero to 1e-14; a pretransform authors an
        # exactly singular matrix rather than a small invertible scale.
        obj.setPreTransform(hou.hmath.buildScale((0, 1, 1)))
        assert obj.worldTransform().determinant() == 0
        reject("Exactly singular transforms are rejected", scene(mode="transform"), "singular")
        obj.setPreTransform(hou.Matrix4(1))
        render("Valid motion recovers after rejected updates", scene(), 0.4)
        first = scene()["geometry"][0]
        second = dict(first, object_key="second-motion-object", motion="transform")
        session.wait(session.apply(dict(scene(), geometry=[first, second]), at_time=0))
        assert session.status()["geometry_resources"] == session.status()["object_resources"] == 2
        checks.append(dict(name="Different geometry motion needs explicit variants", resources=2))
        second["motion"] = "velocity"
        session.wait(session.apply(dict(scene(), geometry=[first, second]), at_time=0))
        assert session.status()["geometry_resources"] == 1 and session.status()["object_resources"] == 2
        checks.append(dict(name="Compatible geometry motion shares one payload", resources=1))
        session.wait(session.apply(scene(), at_time=0))
        retained = session.status()["device_allocation_bytes"]
        for _ in range(6):
            session.wait(session.apply(scene(), at_time=0))
            assert session.status()["device_allocation_bytes"] == retained
        session.close()
        assert session.status()["device_allocation_bytes"] == 0
    reports.append(dict(device=device, checks=checks, repeated_updates=6))

# Save an editable native ROP example and verify host-authored configuration.
set_rop_devices(rop, requested_device)
capture = native_scene.capture(rop, at_time=0)
assert capture["motion"] == dict(motion(), vectors=False)
assert capture["geometry"][0]["motion"] == "velocity"
assert native_scene.motion_times(capture["motion"], 0) == [-1 / 96, 0.0, 1 / 96]
# Lights do not blur over the shutter (as in Blender): a moving light reports nothing
# and is captured at the requested frame.
light = hou.node("/obj").createNode("hlight", "moving_light")
light.parm("tx").setExpression("$T * 48 + 1", hou.exprLanguage.Hscript)
rop.parm("lightpath").set(light.path())
capture = native_scene.capture(rop, at_time=0)
assert not capture["source_diagnostics"], capture["source_diagnostics"]
assert capture["lights"][0]["transform"][3] == 1, capture["lights"][0]["transform"]
with ch.Session(device="CPU", width=160, height=90, samples=64, interactive=False) as recovery:
    recovery.wait(recovery.apply(capture, at_time=0))
    assert recovery.status()["state"] == "complete" and not recovery.status()["error"]
    assert not recovery.status()["source_diagnostics"], recovery.status()["source_diagnostics"]
    light.parm("tx").deleteAllKeyframes()
    light.parm("tx").set(1)
    static = native_scene.capture(rop, at_time=0)
    assert static["lights"][0]["transform"] == capture["lights"][0]["transform"]
    recovery.wait(recovery.apply(static, at_time=0))
    assert not recovery.status()["source_diagnostics"]
rop.parm("lightpath").set("")
import OpenImageIO as OIIO

set_rop_devices(rop, "CPU")
rop.parm("picture").set(str(destination / "native-soho-motion.exr"))
rop.render(frame_range=(1, 1))
image = OIIO.ImageInput.open(str(destination / "native-soho-motion.exr"))
assert image is not None
spec = image.spec()
pixels = image.read_image(format=OIIO.FLOAT)
channel = spec.channelnames.index("R")
batch_coverage = float(pixels[44:46, 79:81, channel].mean() / 0.8)
assert abs(batch_coverage - 0.4) < 0.035, batch_coverage
image.close()
hou.setFps(48)
assert native_scene.capture(rop, at_time=0)["motion"]["fps"] == 48
hou.setFps(24)
set_rop_devices(rop, requested_device)
hou.hipFile.save(str(destination / "native-velocity-motion.hiplc"))
report = dict(
    result="passed",
    fixtures=reports,
    soho_batch_analytic_coverage=0.4,
    soho_batch_measured_coverage=batch_coverage,
    scope="Native OBJ/camera/packed transforms and point-velocity mesh/point/hair; deformation and volume velocity have separate fixtures. Simulation lifetime attributes remain ordinary data.",
)
(destination / "houdini-motion-smoke.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(
    json.dumps(
        dict(
            result=report["result"],
            checks_per_device=[len(item["checks"]) for item in reports],
            batch_coverage=batch_coverage,
            evidence=str(destination / "houdini-motion-smoke.json"),
        )
    )
)
