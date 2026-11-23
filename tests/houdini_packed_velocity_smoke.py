# SPDX-License-Identifier: Apache-2.0
"""Packed point v gives transform blur without shutter cooks or artist IDs."""

import argparse, json, traceback
import hou
import numpy as np
import OpenImageIO as oiio
import cycles_houdini as ch
from cycles_houdini import final_render
from packed_velocity_fixture import Fixture

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
device = parser.parse_args().device
if device.startswith("["):
    device = json.loads(device)
expected_devices = list(device) if isinstance(device, list) else [device]
f = Fixture(device)
missing = f.obj.createNode("attribdelete", "remove_packed_velocity")
missing.setInput(0, f.copies)
missing.parm("ptdel").set("v")
wrong = f.obj.createNode("attribwrangle", "wrong_packed_velocity_type")
wrong.setInput(0, missing)
wrong.parm("snippet").set("f@v=1;")
nested = f.outer
parent = f.obj.createNode("xform", "rotate_packed_hierarchy")
parent.setInput(0, nested)
parent.parm("rz").set(90)
parent_v = f.obj.createNode("attribwrangle", "parent_velocity")
parent_v.setInput(0, parent)
parent_v.parm("snippet").set("v@v={0,12,0};")
report = dict(result="running", device=device, checks=[])


def scene(source=None, **kwargs):
    result = f.update(sop=source or f.copies, **kwargs)
    for request in result["geometry"]:
        request["motion"] = "velocity"
    return result


try:
    with ch.Session(
        device=device,
        width=f.width,
        height=f.height,
        samples=1024,
        threads=2,
        interactive=True,
        collect_diagnostics=True,
    ) as session:

        def render(name, source=None, expected=0.4, warning=False, probes=None, resources=1, objects=4, **kwargs):
            frame = session.wait(session.apply(scene(source, **kwargs), at_time=0))
            status = session.status()
            assert not status["error"] and status["state"] != "failed", status
            assert sorted(status["render_device_ids"]) == sorted(expected_devices), status["render_device_ids"]
            assert bool(status["source_diagnostics"]) == warning, status["source_diagnostics"]
            assert all(issue["code"] == "motion.packed_velocity" for issue in status["source_diagnostics"]), status
            assert all(issue["attribute"] == "v" for issue in status["source_diagnostics"]), status
            assert (
                status["geometry_resources"] == resources
                and status["object_resources"] == objects
                and status["motion_cooks"] == 0
            ), status
            values = []
            for x, y, wanted in probes or [(x, y, expected) for x in (-0.9, 0.9) for y in (-0.4, 0.4)]:
                measured = f.coverage(frame, x, y)
                assert abs(measured - wanted) < 0.04, (name, x, y, measured, wanted)
                values.append(dict(x=x, y=y, coverage=measured, expected=wanted))
            report["checks"].append(dict(name=name, status=status, probes=values))
            return frame, status

        for steps in (3, 5):
            render("Packed velocity " + str(steps) + " samples", steps=steps)
        for offset in (-1, 1):
            render(
                "Offset velocity shutter " + str(offset),
                offset=offset,
                probes=[(x + offset * 0.5, y, 0.4) for x in (-0.9, 0.9) for y in (-0.4, 0.4)],
            )
        render("Restore centered shutter before retention check")
        conversions = session.status()["geometry_conversions"]
        f.velocity.parm("test_velocity").set(0.5)
        render("Velocity edit retains prototype", expected=0.8)
        assert session.status()["geometry_conversions"] == conversions
        for repeat in range(6):
            render("Warm velocity retains geometry " + str(repeat), expected=0.8)
        assert session.status()["geometry_conversions"] == conversions
        f.velocity.parm("test_velocity").set(1)
        f.velocity.parm("test_invalid").set(1)
        probes = [(x, y, 1.0 if (x, y) == (-0.9, -0.4) else 0.4) for x in (-0.9, 0.9) for y in (-0.4, 0.4)]
        render("One invalid velocity leaves other instances blurred", warning=True, probes=probes)
        f.camera.parm("tz").set(6)
        render("Cached velocity warning remains visible", warning=True, probes=probes)
        f.velocity.parm("test_invalid").set(0)
        render("Velocity repair clears warning")
        render("Missing packed velocity retains nominal instances", missing, expected=1.0)
        render("Wrong packed velocity type retains nominal instances", wrong, expected=1.0, warning=True)
        assert "4 affected packed instances" in session.status()["source_diagnostics"][0]["message"]
        render("Unused invalid velocity is quiet with blur off", wrong, expected=1.0, enabled=False)
        render("Restoring packed velocity restores blur")
        f.velocity.parm("test_identity").set(1)
        render("Duplicate id and name are irrelevant to velocity")
        f.targets.parm("rows").setExpression("if($T<0,3,2)", hou.exprLanguage.Hscript)
        render("Changing shutter population does not need correspondence")
        f.targets.parm("rows").deleteAllKeyframes()
        f.targets.parm("rows").set(2)
        f.velocity.parm("test_identity").set(0)
        render("Nested instances retain inner velocity", nested)
        render(
            "Rotated parent composes local velocity",
            parent,
            probes=[(x, y, 0.4) for x in (-0.4, 0.4) for y in (-0.9, 0.9)],
        )
        render(
            "Parent and child velocities compose",
            parent_v,
            probes=[(x, y, 0.32) for x in (-0.4, 0.4) for y in (-0.9, 0.9)],
        )
        f.velocity.parm("test_invalid").set(1)
        parent_v.parm("snippet").set("v@v={0,48,0};")
        render(
            "Invalid child retains independent parent velocity",
            parent_v,
            warning=True,
            probes=[(x, y, 0.4 if (x, y) == (0.4, -0.9) else 0.2) for x in (-0.4, 0.4) for y in (-0.9, 0.9)],
        )
        f.velocity.parm("test_invalid").set(0)
        parent_v.parm("snippet").set("v@v={0,12,0};")
        render(
            "Child velocity repair restores composed motion",
            parent_v,
            probes=[(x, y, 0.32) for x in (-0.4, 0.4) for y in (-0.9, 0.9)],
        )
        f.obj.parm("tx").setExpression("$T*12", hou.exprLanguage.Hscript)
        render("Independent OBJ motion combines with packed velocity", expected=0.32)
        f.camera.parm("tx").setExpression("$T*60", hou.exprLanguage.Hscript)
        render("Matching camera motion cancels combined motion", expected=1.0)
        f.obj.parm("tx").deleteAllKeyframes()
        f.obj.parm("tx").set(0)
        f.camera.parm("tx").deleteAllKeyframes()
        f.camera.parm("tx").set(0)
        f.motion.parm("snippet").set("v@N={0,0,1};")
        render("Authored prototype normals allow packed transform blur")
        f.motion.parm("snippet").set("v@v={12,0,0};")
        render("Prototype velocity and instance velocity combine", expected=0.32)
        f.motion.parm("snippet").set("")
        for kind, expected in (("points", 0.4), ("curves", 0.2), ("mesh", 0.4)):
            f.select(kind)
            render("Packed " + kind + " velocity", expected=expected, point_radius=0.2, curve_width=0.2)
        mixed = f.obj.createNode("attribwrangle", "mixed_container_velocity")
        mixed.setInput(0, f.mixed_pack)
        mixed.parm("snippet").set("v@v={48,0,0};")
        render(
            "Mixed components share one container velocity",
            mixed,
            resources=3,
            objects=3,
            probes=[(0, 0, 0.4), (-1.2, 0, 0.4), (1.2, 0, 0.2)],
            point_radius=0.2,
            curve_width=0.2,
        )
        render("Master blur off retains every instance", expected=1.0, enabled=False)
        # A finite local v may overflow after a parent scale. Recover before
        # strict engine validation; a nominally visible instance must survive.
        huge = f.obj.createNode("attribwrangle", "large_finite_velocity")
        huge.setInput(0, f.pack)
        huge.parm("snippet").set("v@v={3e38,0,0};")
        container = f.obj.createNode("pack", "large_velocity_container")
        container.setInput(0, huge)
        container.parm("pivot").set("origin")
        clean = f.obj.createNode("attribdelete", "no_outer_large_velocity")
        clean.setInput(0, container)
        clean.parm("ptdel").set("v")
        scaled = f.obj.createNode("xform", "parent_scale")
        scaled.setInput(0, clean)
        scaled.parm("scale").set(128)
        f.camera.parm("orthowidth").set(512)
        render(
            "Unrepresentable nested shutter position keeps nominal instance",
            scaled,
            expected=1.0,
            warning=True,
            objects=1,
            probes=[(0, 0, 1.0)],
        )
        f.obj.parm("scale").set(128)
        render(
            "Unrepresentable OBJ-scaled velocity keeps nominal instance",
            huge,
            expected=1.0,
            warning=True,
            objects=1,
            probes=[(0, 0, 1.0)],
        )
        f.obj.parm("scale").set(1)
        f.camera.parm("orthowidth").set(4)
        assert ch.capabilities()["native_packed_velocity"]
    rop = f.rop(f.copies)
    rop.parm("geometry_motion").set("velocity")
    # The default this test was written for (before the ROP took Blender's defaults).
    rop.parm("persistent_data").set(1)
    f.velocity.parm("test_invalid").setExpression("if($F==2 || $F==4,1,0)", hou.exprLanguage.Hscript)
    rop.parm("picture").set(str(f.dest / "packed-velocity.$F4.exr"))
    rop.render(frame_range=(1, 5, 1))
    batch = []
    for number in range(1, 6):
        image = oiio.ImageInput.open(str(f.dest / f"packed-velocity.{number:04d}.exr"))
        assert image
        spec = image.spec()
        pixels = np.asarray(image.read_image(), np.float32)
        image.close()
        issues = json.loads(spec.get_string_attribute("cycles_houdini:source_diagnostics") or "[]")
        assert bool(issues) == (number in (2, 4)), (number, issues)
        # EXR rows are top-down; first copied point is (-.9,-.4).
        column = round((-0.9 / 4 + 0.5) * f.width)
        row = round((0.4 / 2.25 + 0.5) * f.height)
        red = list(spec.channelnames).index("R")
        value = float(pixels[row - 1 : row + 1, column - 1 : column + 1, red].mean() / f.color[0])
        expected = 1.0 if issues else 0.4
        assert abs(value - expected) < 0.04, (number, value, expected)
        batch.append(dict(frame=number, coverage=value, expected=expected, diagnostics=issues))
    summary = rop.cachedUserData(final_render._summary_key)
    assert summary["completed_frames"] == 5 and summary["created_sessions"] == 1 and not summary["session_alive"], (
        summary
    )
    hou.hipFile.save(str(f.dest / "packed-velocity.hiplc"))
    report.update(result="passed", batch=batch, batch_summary=summary)
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    (f.dest / "report.json").write_text(json.dumps(report, indent=2))
print(json.dumps(dict(result="passed", checks=len(report["checks"]))))
