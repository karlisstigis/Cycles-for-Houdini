# SPDX-License-Identifier: Apache-2.0
"""Independent images for packed numeric fields, local repair and shared prototypes."""

import argparse, json, traceback
import hou, numpy as np
import OpenImageIO as oiio
import cycles_houdini as ch
from cycles_houdini import final_render
from packed_instance_attribute_fixture import Fixture

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
device = parser.parse_args().device
f = Fixture(device)
report = dict(result="running", device=device, checks=[])
try:
    with ch.Session(
        device=device,
        width=f.width,
        height=f.height,
        samples=4,
        threads=2,
        collect_diagnostics=True,
        use_texture_cache=False,
    ) as session:

        def render(label, warning=False, expected=None):
            frame = session.wait(session.apply(f.update(), reuse_if_unchanged=True))
            status = session.status()
            assert (
                not status["error"] and status["state"] == "complete" and bool(status["source_diagnostics"]) == warning
            ), status
            colors = f.colors(frame, expected, warning)
            assert status["object_resources"] == 4 * (3 if f.shape.evalParm("input") == 3 else 1), status
            report["checks"].append(dict(label=label, colors=colors, status=status))
            return frame, status

        frame, status = render("Float rand and integer counter on packed primitives")
        conversions = status["geometry_conversions"]
        cooks = f.grid.cookCount()
        assert status["geometry_resources"] == status["mesh_variant_resources"] == 1, status
        for offset in (0.1, 0.05, 0):
            f.set_fields(offset=offset)
            frame, status = render("Instance value edit " + str(offset))
            assert status["geometry_conversions"] == conversions and f.grid.cookCount() == cooks, status
        f.set_fields(owner="point")
        frame, status = render("Fields on packed points")
        assert status["geometry_conversions"] == conversions, status
        f.set_fields()
        f.output.parm("input").set(1)
        render("Nearest nested packed field wins over ancestor")
        f.set_fields("missing")
        render("Missing inner field inherits ancestor", expected=[(0.9, 0.1 * (k + 1), 0.3) for k in range(4)])
        f.output.parm("input").set(0)
        for mode in ("bad_one", "good", "bad_type", "good", "missing", "good"):
            f.set_fields(mode)
            frame, status = render("Local field recovery " + mode, mode != "good")
            if mode == "bad_one":
                assert status["geometry_resources"] == 2, status
            if mode == "good":
                assert status["geometry_resources"] == 1, status
        f.prototype_fields.parm("snippet").set("f@rand=.1;")
        f.set_fields("bad_one")
        render(
            "Prototype data takes precedence over invalid outer data",
            expected=[(0.1, 0.1 * (k + 1), 0.3) for k in range(4)],
        )
        f.prototype_fields.parm("snippet").set("")
        f.set_fields()
        writer = f.material.createNode("cycles::aov_output::1", "unregistered_field")
        writer.parm("name").set("unused_packed_field")
        writer.setNamedInput("color", f.rand, "color")
        f.emission.setInput(f.emission.inputNames().index("color"), None)
        f.emission.parmTuple("color").set((0.2, 0.4, 0.6))
        f.set_fields("bad_type")
        render("Unused AOV field errors remain quiet", expected=[(0.2, 0.4, 0.6)])
        writer.destroy()
        f.emission.setNamedInput("color", f.combine, "vector")
        f.set_fields()
        for kind in (1, 2, 3):
            f.shape.parm("input").set(kind)
            render("Packed points/hair/mixed " + str(kind))
            f.set_fields("bad_one")
            render("Mixed-domain local failure " + str(kind), True)
            f.set_fields()
            render("Mixed-domain repair " + str(kind))
        f.shape.parm("input").set(0)
        f.emission.setNamedInput("color", f.rand, "color")
        f.set_fields("vector")
        render("Three-component instance colors")
        f.set_fields("float2")
        render("Two-component instance colors")
        # Check the Vector socket itself, rather than relying on Color's tuple
        # conversion. Each instance must retain its components and prototype.
        f.emission.setNamedInput("color", f.rand, "vector")
        for kind in range(4):
            f.shape.parm("input").set(kind)
            for owner in ("primitive", "point"):
                for mode in ("vector", "float2"):
                    f.set_fields(mode, owner=owner)
                    frame, status = render("Vector output " + str(kind) + " " + owner + " " + mode)
                    before = status["geometry_conversions"]
                    cooks = f.grid.cookCount()
                    f.set_fields(mode, owner=owner, offset=0.05)
                    frame, status = render("Vector value edit " + str(kind) + " " + owner + " " + mode)
                    assert status["geometry_conversions"] == before and f.grid.cookCount() == cooks, status
            f.set_fields("bad_vector")
            render("Non-finite vector component stays local " + str(kind), True)
            f.set_fields("vector")
            render("Vector component repair " + str(kind))
        f.shape.parm("input").set(0)
        f.emission.setNamedInput("color", f.rand, "color")
        f.set_fields("vector")
        tangent = f.material.createNode("cycles::tangent::1", "prototype_basis")
        # The default this test was written for (new nodes now take Blender's).
        tangent.setParms(dict(axis="x"))
        tangent.setParms(dict(direction_type="uv_map", attribute="rand"))
        f.emission.setNamedInput("color", tangent, "tangent")
        render("Uniform instance UV cannot masquerade as a mesh tangent basis", True, [(1, 0, 1)])
        f.prototype_fields.parm("snippet").set("v@rand=set(@P.x+.5,@P.y+.5,0);")
        render("Prototype UV repair restores a real tangent basis", expected=[])
        f.prototype_fields.parm("snippet").set("")
        f.emission.setNamedInput("color", f.combine, "vector")
        f.set_fields()
        output = f.material.node("surface_output")
        output.setNamedInput("displacement", f.displacement, "displacement")
        f.material.parm("cycles_displacement_method").set("both")
        frame, status = render("Integer instance field drives independent true displacement")
        assert status["mesh_variant_resources"] == 4, status
        f.check_displacement(frame)
        f.set_fields("bad_one")
        frame, status = render("Broken color retains valid independent displacement", True)
        assert status["mesh_variant_resources"] == 4, status
        f.check_displacement(frame)
        f.set_fields()
        render("Repair restores shading with displacement")
        f.set_fields("bad_height")
        frame, status = render("Broken height retains unmodified geometry locally", True)
        f.check_displacement(frame, disabled_one=True)
        f.set_fields()
        frame, status = render("Height repair restores independent displacement")
        f.check_displacement(frame)
        output.setInput(output.inputNames().index("displacement"), None)
        f.material.parm("cycles_displacement_method").set("bump")
        # No field edits may be misclassified as a pose-only worker update.
        f.set_fields(offset=0.13)
        render("Post-repair value edits reach the retained worker")
        f.camera.parm("tx").set(0.05)
        render("Camera continues after repairs")
        f.obj.parm("tx").set(0.05)
        render("Object continues after repairs")
        f.rop.setParms(dict(allowmotionblur=1, geometry_motion="deformation", motion_samples=3))
        f.prototype_fields.parm("snippet").set("@P.x+=.1*@Time;")
        for kind in range(4):
            f.shape.parm("input").set(kind)
            f.set_fields()
            frame, status = render("Deforming prototype with instance fields " + str(kind))
            for mode in ("good", "bad_one", "good", "bad_type", "good"):
                before = status["geometry_conversions"]
                f.set_fields(mode, offset=0.07 if mode == "good" else 0)
                frame, status = render("Deformation field repair " + str(kind) + " " + mode, mode != "good")
                if mode == "good" and report["checks"][-2]["label"].startswith("Deforming"):
                    assert status["geometry_conversions"] == before, status
            # The nominal prototype is identical at time zero, but the two
            # shutter endpoints move. A reuse decision must inspect all keys.
            assert hou.time() == 0
            nominal = f.prototype_fields.geometry().pointFloatAttribValuesAsString("P")
            before = status["geometry_conversions"]
            f.prototype_fields.parm("snippet").set("@P.x+=.2*@Time;")
            assert f.prototype_fields.geometry().pointFloatAttribValuesAsString("P") == nominal
            frame, status = render("Shutter-only prototype edit " + str(kind))
            assert status["geometry_conversions"] > before, status
            f.prototype_fields.parm("snippet").set("@P.x+=.1*@Time;")
        f.shape.parm("input").set(0)
        f.rop.setParms(dict(allowmotionblur=0, geometry_motion="transform"))
        f.prototype_fields.parm("snippet").set("")
        f.set_fields()
        session.close()
        assert session.status()["device_allocation_bytes"] == session.status()["owned_geometry_bytes"] == 0
        report["closed"] = session.status()
    f.camera.parm("tx").set(0)
    f.obj.parm("tx").set(0)
    f.set_fields("animation")
    f.rop.parm("picture").set(str(f.dest / "packed-fields.$F4.exr"))
    f.rop.setParms(dict(persistent_data=True, use_denoise=False))
    f.rop.render(frame_range=(1, 5, 1))
    summary = f.rop.cachedUserData(final_render._summary_key)
    assert summary["completed_frames"] == 5 and summary["created_sessions"] == 1 and not summary["session_alive"], (
        summary
    )
    for number in range(1, 6):
        reader = oiio.ImageInput.open(str(f.dest / f"packed-fields.{number:04d}.exr"))
        assert reader
        spec = reader.spec()
        issues = json.loads(spec.get_string_attribute("cycles_houdini:source_diagnostics") or "[]")
        pixels = np.asarray(reader.read_image(format=oiio.FLOAT))
        reader.close()
        assert bool(issues) == (number == 3), (number, issues)
        colors = np.unique(np.round(pixels[:, :, :3].reshape(-1, 3), 4), axis=0)
        for k in range(4):
            expected = (1, 0, 1) if number == 3 and k == 1 else (0.1 * number + 0.1 * k, 0.1 * (k + 1), 0.3)
            assert np.max(np.abs(colors - np.asarray(expected)), axis=1).min() < 3e-4, (number, k, colors)
    report.update(result="passed", batch_frames=5, batch_summary=summary, scope=__doc__)
except BaseException:
    report.update(
        result="failed",
        error=traceback.format_exc(),
        sop_errors={n.path(): list(n.errors()) for n in f.obj.children() if n.errors()},
    )
    raise
finally:
    (f.dest / "report.json").write_text(json.dumps(report, indent=2))
print(json.dumps(dict(result=report["result"], checks=len(report["checks"]), batch=report.get("batch_frames"))))
