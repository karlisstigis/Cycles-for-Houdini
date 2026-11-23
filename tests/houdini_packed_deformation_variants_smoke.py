# SPDX-License-Identifier: Apache-2.0
"""One nominal prototype can need distinct shutter profiles per packed instance."""

import argparse, json, traceback
import hou
import numpy as np
import cycles_houdini as ch
from cycles_houdini import native_scene
from packed_deformation_fixture import Fixture

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
device = parser.parse_args().device
f = Fixture(device)
report = dict(result="running", checks=[])
static = f.obj.createNode("grid", "static_shutter_shape")
static.setParms(dict(type=0, orient=0, rows=2, cols=2))
static.parmTuple("size").set((0.4, 1.2))
static_pack = f.obj.createNode("pack", "different_shutter_prototype")
static_pack.setInput(0, static)
static_pack.parm("pivot").set("origin")
choice = f.obj.createNode("switch", "same_nominal_different_shutter")
choice.setInput(0, f.pack)
choice.setInput(1, static_pack)
choice.parm("input").setExpression("abs($FF-rint($FF))>0.00001", hou.exprLanguage.Hscript)
left = f.obj.createNode("xform", "animated_instance")
left.setInput(0, f.pack)
left.parm("tx").set(-0.7)
right = f.obj.createNode("xform", "static_instance")
right.setInput(0, choice)
right.parm("tx").set(0.7)
joined = f.obj.createNode("merge", "two_nominally_shared_instances")
joined.setInput(0, left)
joined.setInput(1, right)
nested = f.obj.createNode("pack", "nested_profile_variants")
nested.setInput(0, joined)
nested.parm("pivot").set("origin")


def pixels(frame):
    return f.pixels(frame)


def probe(frame, x, color, coverage):
    image = pixels(frame)
    column = round((x / 4 + 0.5) * f.width)
    rgb = image[44:46, column - 1 : column + 1, :3].mean(axis=(0, 1))
    assert np.allclose(rgb, np.asarray(color) * coverage, atol=0.035), (x, rgb, color, coverage)


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

        def render(name, source=nested, steps=3, warning=False, native=False, colors=None):
            update = f.update(steps, sop=source, material_mode="native" if native else "uniform")
            if native:
                update["material_resolver"] = native_scene.resolve_materials
            frame = session.wait(session.apply(update, at_time=0))
            status = session.status()
            assert status["state"] == "complete" and not status["error"] and status["render_device_ids"] == [device], (
                status
            )
            assert status["geometry_resources"] == status["object_resources"] == 2, status
            assert status["curves"] == 0 and np.isfinite(pixels(frame)).all(), status
            assert bool(status["source_diagnostics"]) == warning, status["source_diagnostics"]
            first, second = colors or (f.color, f.color)
            probe(frame, -0.7, first, 0.4)
            probe(frame, 0.7, second, 1)
            report["checks"].append(dict(name=name, status=status))
            return status

        frame = session.wait(session.apply(f.update(enabled=False, sop=joined), at_time=0))
        status = session.status()
        assert (
            status["geometry_resources"] == 1 and status["object_resources"] == 2 and not status["source_diagnostics"]
        ), status
        probe(frame, -0.7, f.color, 1)
        probe(frame, 0.7, f.color, 1)
        report["checks"].append(dict(name="Requested-frame geometry shares one native prototype", status=status))
        for source in (joined, nested):
            for steps in (3, 5):
                status = render("Distinct complete profiles " + source.name() + " " + str(steps), source, steps)
                assert status["motion_cooks"] == steps - 1, status
                warm = render("Retained independent profiles " + source.name() + " " + str(steps), source, steps)
                assert warm["motion_cooks"] == 0 and warm["geometry_conversions"] == status["geometry_conversions"], (
                    warm
                )
        static.parm("rows").set(3)
        broken = render("One profile has incompatible shutter topology", warning=True)
        warm = render("Cached local topology recovery keeps the other profile moving", warning=True)
        assert warm["source_diagnostics"] == broken["source_diagnostics"] and warm["motion_cooks"] == 0
        static.parm("rows").set(2)
        render("Repairing the one incompatible profile restores its samples")
        hair = f.obj.createNode("line", "shutter_kind_change")
        hair.parmTuple("origin").set((0, -0.6, 0))
        hair.parm("dist").set(1.2)
        static_pack.setInput(0, hair)
        render("Shutter-only hair does not become a nominal object", warning=True)
        static_pack.setInput(0, static)
        render("Repairing shutter geometry kind keeps both nominal objects")
        empty = f.obj.createNode("add", "empty_shutter_contents")
        static_pack.setInput(0, empty)
        render("Empty shutter contents keep requested-frame geometry", warning=True)
        static_pack.setInput(0, static)
        render("Restoring shutter contents clears the local warning")
        # Different bindings also share the same nominal source, but their
        # geometry palettes and independently sampled contents remain explicit.
        a = ch.create_material(name="moving_profile_material", shader="emission")
        # The default this test was written for (new nodes now take Blender's).
        a.node("surface_shader").setParms(dict(color=(0.8, 0.8, 0.8), strength=10.0))
        b = ch.create_material(name="static_profile_material", shader="emission")
        # The default this test was written for (new nodes now take Blender's).
        b.node("surface_shader").setParms(dict(color=(0.8, 0.8, 0.8), strength=10.0))
        colors = ((0.8, 0.2, 0.05), (0.1, 0.6, 0.3))
        for material, color in zip((a, b), colors):
            material.node("surface_shader").parmTuple("color").set(color)
            material.node("surface_shader").parm("strength").set(1)
        f.obj.parm("shop_materialpath").set(a.path())
        binding = f.obj.createNode("material", "static_instance_material")
        binding.setInput(0, right)
        binding.parm("shop_materialpath1").set(b.path())
        joined.setInput(1, binding)
        status = render("Independent complete profiles and native binding palettes", native=True, colors=colors)
        warm = render("Warm native palettes retain both profile variants", native=True, colors=colors)
        assert warm["geometry_conversions"] == status["geometry_conversions"] and warm["motion_cooks"] == 0, warm
        reference = None
        for source in (f.motion, f.pack):
            update = f.update(enabled=False, sop=source)
            update["motion"]["vectors"] = True
            update["motion_blur"] = False
            update["passes"] = ["combined", "motion"]
            frame = session.wait(session.apply(update, at_time=0))
            status = session.status()
            assert not status["error"] and not status["source_diagnostics"], status
            assert status["geometry_resources"] == status["object_resources"] == 1, status
            vector = f.pixels(frame, "motion")[44:46, 79:81].mean(axis=(0, 1))
            assert np.allclose(vector, (-80, 0, -80, 0), atol=2), (source.path(), vector)
            if reference is not None:
                assert np.allclose(vector, reference, atol=1e-4), (vector, reference)
            reference = vector
            report["checks"].append(
                dict(name="Independent frame motion vectors " + source.name(), vectors=vector.tolist(), status=status)
            )
    report["result"] = "passed"
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    (f.dest / "variants-report.json").write_text(json.dumps(report, indent=2))
print(json.dumps(dict(result=report["result"], checks=len(report["checks"]))))
