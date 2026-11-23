# SPDX-License-Identifier: Apache-2.0
"""Packed contents must deform, with native sampling and retained instances."""

import argparse, json, traceback
import hou
import numpy as np
import OpenImageIO as oiio
import cycles_houdini as ch
from cycles_houdini import native_scene, final_render
from packed_deformation_fixture import Fixture

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
        samples=1024,
        threads=2,
        interactive=False,
        collect_diagnostics=True,
    ) as session:

        def render(name, update=None, now=0, **expected):
            before = hou.time()
            frame = session.wait(session.apply(update or f.update(), at_time=now))
            status = session.status()
            assert hou.time() == before
            values = f.check(frame, status, now=now, **expected)
            report["checks"].append(dict(name=name, coverage=values, status=status))
            return frame, status

        for kind in ("mesh", "points", "curves"):
            f.select(kind)
            for steps in (3, 5):
                options = dict(point_radius=0.2, curve_width=0.2)
                frame, status = render("Packed " + kind + " at " + str(steps) + " samples", f.update(steps, **options))
                assert status["geometry_resources"] == status["object_resources"] == 1
                conversions = status["geometry_conversions"]
                frame, status = render("Unchanged " + kind + " retains geometry", f.update(steps, **options))
                assert status["geometry_conversions"] == conversions and status["motion_cooks"] == 0, status
            conversions = status["geometry_conversions"]
            session.wait(session.apply(dict(camera_path=f.camera.path()), at_time=0))
            assert session.status()["geometry_conversions"] == conversions and session.status()["motion_cooks"] == 0
            f.motion.parm("test_broken").set(1)
            render("Broken " + kind + " sample keeps nominal geometry", f.update(**options), warning=True)
            f.motion.parm("test_broken").set(0)
            render("Repaired " + kind + " sample restores deformation", f.update(**options))
        f.select("mesh")
        for offset in (-1, 1):
            render("Offset shutter " + str(offset), f.update(offset=offset), offset=offset)
        f.camera.parm("tx").set(6)
        render("Explicit render time retains the visible timeline", f.update(now=0.125), now=0.125)
        f.camera.parm("tx").set(0)
        for broken in (1, 0, 1, 0):
            f.motion.parm("test_broken").set(broken)
            frame, status = render("Broken shutter topology and repair " + str(broken), warning=bool(broken))
            conversions = status["geometry_conversions"]
            issues = status["source_diagnostics"]
            frame, status = render("Cached recovery remains visible " + str(broken), warning=bool(broken))
            assert (
                status["geometry_conversions"] == conversions
                and status["source_diagnostics"] == issues
                and status["motion_cooks"] == 0
            )
        for source in (f.copies, f.nested):
            frame = session.wait(session.apply(f.update(sop=source), at_time=0))
            status = session.status()
            assert (
                not status["source_diagnostics"]
                and status["geometry_resources"] == 1
                and status["object_resources"] == 4
            ), status
            assert status["motion_cooks"] == 2, status
            for x in (-0.9, 0.9):
                for y in (-0.4, 0.4):
                    assert abs(f.coverage(frame, x, y) - 0.4) < 0.04
            report["checks"].append(dict(name="Shared animated " + source.name(), status=status))
        for source, resources, objects in ((f.mixed_pack, 3, 3), (f.same_level, 2, 5)):
            frame, status = render(
                "Mixed components " + source.name(), f.update(sop=source, point_radius=0.2, curve_width=0.2)
            )
            assert (
                status["geometry_resources"] == resources
                and status["object_resources"] == objects
                and status["motion_cooks"] == 2
            ), status
        for broken in (1, 0):
            f.motion.parm("test_broken").set(broken)
            frame, status = render(
                "One mixed component fails and repairs",
                f.update(sop=f.mixed_pack, point_radius=0.2, curve_width=0.2),
                warning=bool(broken),
            )
            assert (
                status["geometry_resources"] == status["object_resources"] == 3
                and len(status["source_diagnostics"]) == broken
            ), status
            assert f.coverage(frame, -1.2, 0) > 0.95 and f.coverage(frame, 1.2, 0) > 0.95
        # Two materials require engine binding variants, but must share the
        # source SOP's shutter cooks. Identical bindings then share one resource.
        first = ch.create_material(name="packed_first", shader="emission")
        # The default this test was written for (new nodes now take Blender's).
        first.node("surface_shader").setParms(dict(color=(0.8, 0.8, 0.8), strength=10.0))
        second = ch.create_material(name="packed_second", shader="emission")
        # The default this test was written for (new nodes now take Blender's).
        second.node("surface_shader").setParms(dict(color=(0.8, 0.8, 0.8), strength=10.0))
        for material in (first, second):
            material.node("surface_shader").parmTuple("color").set(tuple(float(v) for v in f.color))
            material.node("surface_shader").parm("strength").set(1)
        other = hou.node("/obj").createNode("geo", "packed_other", run_init_scripts=False)
        f.obj.parm("shop_materialpath").set(first.path())
        other.parm("shop_materialpath").set(second.path())
        update = f.update(material_mode="native")
        update["geometry"].append(
            ch.geometry_request(
                other, f.pack, material="surface", material_mode="native", motion="deformation", at_time=0
            )
        )
        update["material_resolver"] = native_scene.resolve_materials
        frame, status = render("Different bindings share source shutter acquisition", update)
        assert status["geometry_resources"] == status["object_resources"] == 2 and status["motion_cooks"] == 2, status
        other.parm("shop_materialpath").set(first.path())
        update["geometry"][1] = ch.geometry_request(
            other, f.pack, material="surface", material_mode="native", motion="deformation", at_time=0
        )
        frame, status = render("Compatible materials retain one animated prototype", update)
        assert status["geometry_resources"] == 1 and status["object_resources"] == 2 and status["motion_cooks"] == 0, (
            status
        )
        conversions = status["geometry_conversions"]
        owned = status["owned_geometry_bytes"]
        for _ in range(6):
            frame, status = render("Warm prototype and shader retention", update)
            assert (
                status["geometry_conversions"] == conversions
                and status["owned_geometry_bytes"] == owned
                and status["motion_cooks"] == 0
            ), status
        assert ch.capabilities()["native_packed_deformation"]
    rop = f.rop()
    f.motion.parm("test_broken").setExpression("if($F==2 || $F==4,1,0)", hou.exprLanguage.Hscript)
    # Follow nominal animation frames while leaving the camera stationary
    # throughout each individual shutter interval.
    f.camera.parm("tx").setExpression("2*(rint($FF)-1)", hou.exprLanguage.Hscript)
    # The default this test was written for (before the ROP took Blender's defaults).
    rop.parm("persistent_data").set(1)
    rop.parm("picture").set(str(f.dest / "packed.$F4.exr"))
    rop.render(frame_range=(1, 5, 1))
    batch = []
    for number in range(1, 6):
        image = oiio.ImageInput.open(str(f.dest / f"packed.{number:04d}.exr"))
        assert image
        spec = image.spec()
        pixels = np.asarray(image.read_image(), np.float32)
        image.close()
        issues = json.loads(spec.get_string_attribute("cycles_houdini:source_diagnostics") or "[]")
        assert bool(issues) == (number in (2, 4)), (number, issues)
        red = list(spec.channelnames).index("R")
        measured = float(pixels[44:46, 79:81, red].mean() / f.color[0])
        expected = 1.0 if issues else 0.4
        assert abs(measured - expected) < 0.04, (number, measured, expected)
        batch.append(dict(frame=number, coverage=measured, expected=expected, diagnostics=issues))
    summary = rop.cachedUserData(final_render._summary_key)
    assert summary["completed_frames"] == 5 and summary["created_sessions"] == 1 and not summary["session_alive"], (
        summary
    )
    hou.hipFile.save(str(f.dest / "packed-deformation.hiplc"))
    report.update(result="passed", batch=batch, batch_summary=summary)
except BaseException:
    report.update(
        result="failed",
        error=traceback.format_exc(),
        sop_errors={n.path(): list(n.errors()) for n in f.obj.children() if n.errors()},
    )
    raise
finally:
    (f.dest / "report.json").write_text(json.dumps(report, indent=2))
print(json.dumps(dict(result="passed", checks=len(report["checks"]))))
