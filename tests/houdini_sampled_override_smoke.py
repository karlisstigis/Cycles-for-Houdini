# SPDX-License-Identifier: Apache-2.0
"""Native shutter-varying literals against independently positioned motion meshes."""

from support.paths import artifacts_root
from native_test_runtime import runtime_executable
from subdivision_test_setup import configure_render, set_subdivision
import pass_fixtures as pass_fixture

import argparse, json, os, subprocess, time, traceback
from pathlib import Path
import hou, numpy as np, OpenImageIO as oiio
import cycles_houdini as ch
from cycles_houdini import native_scene, registry, final_render
from sampled_override_fixture import Fixture

p = argparse.ArgumentParser()
p.add_argument("--device", default="CPU")
device = p.parse_args().device
f = Fixture(device)
root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
dest = artifacts_root() / "examples/sampled-overrides"
dest.mkdir(parents=True, exist_ok=True)
references = dest / ("reference-cpu" if device == "CPU" else "reference-optix")
run = subprocess.run(
    [
        str(runtime_executable("ch_harness.exe")),
        "--resources",
        os.environ["CYCLES_HOUDINI_RESOURCES"],
        "--device",
        device,
        "--displacement-motion-reference",
        "--output",
        str(references),
    ],
    capture_output=True,
    text=True,
    timeout=180,
)
assert run.returncode == 0, (run.stdout, run.stderr)
entry = registry.for_rop(f.rop, native_scene.configuration(f.rop))
report = dict(result="running", device=device, checks=[])


def render(label, samples=3, codes=(), factor=1, objects=1, geometry=1):
    pass_fixture.parm(f.rop, "motion_samples").set(samples)
    frame = entry.session.wait(entry.apply(native_scene.capture(f.rop), hou.time()))
    status = entry.session.status()
    planes = f.pixels(frame)
    # The final image can arrive before the worker's next progress/status poll.
    # Keep the completion assertion, but wait for that separate acknowledgement.
    deadline = time.monotonic() + 3
    while status["state"] != "complete" and time.monotonic() < deadline:
        assert status["state"] != "failed", status
        time.sleep(0.005)
        status = entry.session.status()
    reader = oiio.ImageInput.open(str(references / f"position-{samples}.exr"))
    assert reader
    spec = reader.spec()
    pixels = np.asarray(reader.read_image(), np.float32)
    reader.close()
    expected = np.flipud(pixels[:, :, [list(spec.channelnames).index(c) for c in ("R", "G", "B")]]) * factor
    error = np.abs(planes["combined"][:, :, :3] - expected)
    report["checks"].append(dict(label=label, samples=samples, mean_difference=float(error.mean()), status=status))
    assert error.mean() < 0.0008, (
        label,
        samples,
        error.mean(),
        status["motion_warnings"],
        status["source_diagnostics"],
    )
    if codes is None:
        assert status["source_diagnostics"], status
    else:
        assert {issue["code"] for issue in status["source_diagnostics"]} == set(codes), status
    assert not status["motion_warnings"], status
    assert (
        status["geometry_resources"] == geometry
        and status["object_resources"] == objects
        and status["state"] == "complete"
    ), status
    assert all(np.isfinite(values).all() for values in planes.values())
    return frame, status, planes


try:
    for case in ("parameter", "deformation", "base_animation", "image"):
        f.setup(case)
        for samples in (3, 5):
            render("Native shutter override: " + case, samples)
    f.setup("parameter")
    camera = hou.node(pass_fixture.evaluate(f.rop, "camera"))
    for samples in (3, 5):
        for offset in (-1, 1):
            pass_fixture.parm(f.rop, "shutteroffset").set(offset)
            pass_fixture.parm(camera, "tx").set(offset * 0.75)
            render("Requested-frame shading with an offset displacement shutter", samples)
    for scheme in ("linear", "catmull_clark"):
        f.setup("parameter")
        configure_render(f.rop, dict(subdivision=dict(type=scheme, space="object", dicing_rate=0.1, max_level=3)))
        # Fixed-axis displacement isolates dicing from custom-normal policies.
        pass_fixture.parm(f.vector, "space").set("object")
        for component in f.vector.parmTuple("vector"):
            component.deleteAllKeyframes()
            component.set(0, follow_parm_reference=False)
        f.vector.parmTuple("vector")[0].set(pass_fixture.parm(f.material, "height"), follow_parm_reference=False)
        f.material.node("surface_output").setNamedInput("displacement", f.vector, "displacement")
        render(scheme + " render subdivision samples native literals")
    # An off-frame invalid binding must not erase independently valid SOP motion.
    f.setup("parameter")
    pass_fixture.parm(f.sop, "snippet").set("v@N=set(1,0,0);@P.x+=@Time*72;f@existing_mask=.5;")
    pass_fixture.parm(f.assigned, "test_speed").set(0)
    missing = "/mat/repairable_shutter_override"
    pass_fixture.parm(f.output, "snippet").set('if(@Time>0)s@shop_materialpath="' + missing + '";')
    render(
        "Missing shutter material retains requested-frame displacement and geometry blur",
        codes=("motion.displacement_binding",),
    )
    repaired = ch.create_material(name=missing.rsplit("/", 1)[1], shader="emission")
    node = repaired.createNode("cycles::displacement::1", "displacement")
    pass_fixture.set_parameters(node, dict(height=0.5, midlevel=0, scale=1))
    ch.promote_parameter(repaired, node, "height", name="height")
    repaired.node("surface_output").setNamedInput("displacement", node, "displacement")
    pass_fixture.parm(repaired, "cycles_displacement_method").set("true")
    irrelevant = repaired.createNode("cycles::attribute::1", "offframe_surface_only")
    pass_fixture.parm(irrelevant, "attribute").set("missing_surface_only")
    repaired.node("surface_shader").setNamedInput("color", irrelevant, "color")
    render("Creating the missing shutter material clears the warning")
    before = entry.session.status()
    pass_fixture.parm(irrelevant, "attribute").set("another_missing_surface_field")
    _, status, _ = render("Unused off-frame surface fields do not affect displacement or geometry")
    assert (
        status["geometry_conversions"] == before["geometry_conversions"]
        and status["displacement_rebuilds"] == before["displacement_rebuilds"]
    ), status
    field = repaired.createNode("cycles::attribute::1", "offframe_displacement_field")
    pass_fixture.parm(field, "attribute").set("existing_mask")
    node.setNamedInput("height", field, "fac")
    _, field_status, _ = render("An off-frame shader edit captures newly demanded existing fields")
    pass_fixture.parm(field, "attribute").set("missing_displacement_mask")
    _, missing_status, _ = render(
        "A missing off-frame field retains nominal displacement and valid geometry motion", codes=None
    )
    assert any(
        issue.get("attribute") == "missing_displacement_mask" for issue in missing_status["source_diagnostics"]
    ), missing_status
    pass_fixture.parm(field, "attribute").set("existing_mask")
    render("Repairing an off-frame field clears the warning")
    node.setInput(node.inputIndex("height"), None)
    _, field_removed, _ = render("Removing off-frame field demand retires captured attribute storage")
    assert field_removed["owned_geometry_bytes"] < field_status["owned_geometry_bytes"], (field_status, field_removed)
    repaired.destroy()
    render("Deleting an off-frame material retains usable geometry", codes=("motion.displacement_binding",))
    pass_fixture.parm(f.output, "snippet").set("")
    render("Removing the broken shutter assignment clears recovery")
    f.setup("parameter")
    pass_fixture.parm(f.sop, "snippet").set("v@N=set(1,0,0);@P.x+=@Time*72;")
    pass_fixture.parm(f.assigned, "num_local1").set(0)
    pass_fixture.parm(f.assigned, "shop_materialpath1").set("op:" + f.material.path())
    render("Canonical op paths retain native shading and geometry motion")
    # One visible and one distant copy make the single-mesh reference reusable.
    f.setup("parameter")
    targets = f.obj.createNode("line", "two_temporal_instances")
    pass_fixture.set_parameters(targets, dict(points=2, dist=8))
    targets.parmTuple("dir").set((1, 0, 0))
    copies = f.obj.createNode("copytopoints", "shared_override_prototypes")
    copies.setInput(0, f.output)
    copies.setInput(1, targets)
    pass_fixture.parm(copies, "pack").set(1)
    copies.setDisplayFlag(True)
    copies.setRenderFlag(True)
    for samples in (3, 5):
        _, status, _ = render("Identical packed override timelines retain one prototype", samples, objects=2)
        assert status["packed_instances"] == 2, status
    # Same requested-frame values do not imply compatible displacement histories.
    copies.setInput(0, f.sop)
    outer = f.obj.createNode("attribwrangle", "per_instance_override_history")
    outer.setInput(0, copies)
    pass_fixture.parm(outer, "class").set(1)
    split = (
        's@shop_materialpath="'
        + f.material.path()
        + '";s@material_override=sprintf("{\'height\':%.8f}",.5+@Time*(@primnum==0?72:0));'
    )
    pass_fixture.parm(outer, "snippet").set(split)
    outer.setDisplayFlag(True)
    outer.setRenderFlag(True)
    for samples in (3, 5):
        render("Different packed displacement histories create explicit variants", samples, objects=2, geometry=2)
    camera = hou.node(pass_fixture.evaluate(f.rop, "camera"))
    pass_fixture.parm(camera, "tx").set(8)
    frame = entry.session.wait(entry.apply(native_scene.capture(f.rop), hou.time()))
    status = entry.session.status()
    planes = f.pixels(frame)
    np.testing.assert_allclose(
        planes["combined"][30:60, 55:105, :3], np.broadcast_to((0.2, 0.4, 0.8), (30, 50, 3)), atol=1e-5, rtol=0
    )
    assert not status["source_diagnostics"] and not status["motion_warnings"] and status["geometry_resources"] == 2, (
        status
    )
    report["checks"].append(
        dict(label="The second packed history renders its independently specified static rectangle", status=status)
    )
    pass_fixture.parm(camera, "tx").set(0)
    pass_fixture.parm(outer, "snippet").set(split.replace("(@primnum==0?72:0)", "72"))
    render("Repairing packed histories merges compatible geometry again", objects=2)
    f.output.setDisplayFlag(True)
    f.output.setRenderFlag(True)
    outer.destroy()
    copies.destroy()
    targets.destroy()
    # Nominal fallback ownership is valid even when a shutter visits a native binding.
    pass_fixture.parm(f.obj, "shop_materialpath").set("")
    pass_fixture.parm(f.assigned, "shop_materialpath1").set("")
    configure_render(f.rop, dict(material_kind="emission", emission_strength=1))
    f.rop.parmTuple("basecolor").set((0.2, 0.4, 0.8))
    pass_fixture.parm(f.sop, "snippet").set("v@N=set(1,0,0);@P.x+=.5+@Time*72;")
    pass_fixture.parm(f.output, "snippet").set('if(@Time>0)s@shop_materialpath="' + f.material.path() + '";')
    render("Unassigned nominal geometry retains its owned fallback while bindings vary")
    # Point and hair shading remains at the requested frame while their valid
    # geometry motion is independently checked against explicit position sums.
    shape_material = ch.create_material(name="sampled_shape_surface", shader="emission")
    shape_material.node("surface_shader").parmTuple("color").set((0.2, 0.4, 0.8))
    pass_fixture.parm(shape_material.node("surface_shader"), "strength").set(1)
    ch.promote_parameter(shape_material, shape_material.node("surface_shader"), "strength", name="power")
    for kind in ("points", "curves"):
        if kind == "points":
            source = f.obj.createNode("add", "moving_point")
            pass_fixture.set_parameters(source, dict(points=1, usept0=1, pt0x=0.5))
        else:
            source = f.obj.createNode("line", "moving_hair")
            pass_fixture.set_parameters(source, dict(points=2, dist=1))
            source.parmTuple("origin").set((0.5, -0.5, 0))
        moved = f.obj.createNode("attribwrangle", "shape_motion_" + kind)
        moved.setInput(0, source)
        pass_fixture.parm(moved, "snippet").set("@P.x+=@Time*72;f@pscale=.2;f@width=.4;")
        assigned = f.obj.createNode("material", "shape_material_" + kind)
        assigned.setInput(0, moved)
        pass_fixture.set_parameters(
            assigned,
            dict(
                style="point" if kind == "points" else "primitive",
                shop_materialpath1=shape_material.path(),
                num_local1=1,
                local1_name1="power",
                local1_type1="float",
                local1_fval1=1,
            ),
        )
        assigned.setDisplayFlag(True)
        assigned.setRenderFlag(True)
        configure_render(f.rop, dict(shutteroffset=0, motion_samples=3))
        pass_fixture.parm(camera, "tx").set(0)
        baseline = entry.session.wait(entry.apply(native_scene.capture(f.rop), hou.time()))
        expected = f.pixels(baseline)["combined"]
        x = np.where(expected[:, :, 2] > 0.01)[1]
        assert len(x) and 60 < x.max() - x.min() < 82, (kind, x)
        pass_fixture.parm(assigned, "local1_fval1").setExpression("1+$T*24", hou.exprLanguage.Hscript)
        for offset in (0, 1):
            pass_fixture.parm(f.rop, "shutteroffset").set(offset)
            pass_fixture.parm(camera, "tx").set(offset * 0.75)
            frame = entry.session.wait(entry.apply(native_scene.capture(f.rop), hou.time()))
            status = entry.session.status()
            error = np.abs(f.pixels(frame)["combined"] - expected)
            if offset:
                # Recentring a translated curve can change grazing-ray rounding.
                # Allow at most two samples at an edge, with a strict mean budget.
                assert error.mean() < 1e-6 and error.max() < 2 * 0.8 / 1024 + 1e-5, (
                    kind,
                    offset,
                    error.mean(),
                    error.max(),
                )
            else:
                np.testing.assert_allclose(f.pixels(frame)["combined"], expected, atol=2e-5, rtol=0)
            assert not status["source_diagnostics"] and not status["motion_warnings"], status
            report["checks"].append(
                dict(
                    label=kind + " changing bindings retain motion and requested-frame shading",
                    offset=offset,
                    mean_difference=float(error.mean()),
                    maximum_difference=float(error.max()),
                    status=status,
                )
            )
        lease = ch._native.preserve_deformation(assigned.path(), 0, 24, 0.5, 3, shape_material.path())
        owned = ch._native.convert_preserved_deformation(lease)
        del lease
        row = owned["components"][0]
        count = 1 if kind == "points" else 2
        np.testing.assert_allclose(
            [sample["sums"][0] for sample in row["samples"]], np.array((0.5, -0.25, 1.25)) * count, atol=1e-6, rtol=0
        )
        assert row["kind"] == kind and len(row["samples"]) == 3, owned
        report["checks"].append(
            dict(label=kind + " owned motion samples match independent translation sums", motion=owned)
        )
    f.setup("parameter")
    f.output.setDisplayFlag(True)
    f.output.setRenderFlag(True)
    warm = []
    for index in range(16):
        rate = 72 + index
        pass_fixture.parm(f.assigned, "test_speed").set(rate)
        frame = entry.session.wait(entry.apply(native_scene.capture(f.rop), hou.time()))
        status = entry.session.status()
        planes = f.pixels(frame)
        x = np.where(planes["combined"][:, :, 2] > 0.01)[1]
        assert len(x) and abs((x.max() - x.min()) - (60 + rate * 5 / 6)) < 6, (rate, x)
        assert (
            not status["source_diagnostics"] and not status["motion_warnings"] and status["geometry_resources"] == 1
        ), status
        warm.append(
            {
                name: status[name]
                for name in (
                    "owned_geometry_bytes",
                    "device_allocation_bytes",
                    "engine_shader_resources",
                    "retired_shader_resources",
                )
            }
        )
    for field in (
        "owned_geometry_bytes",
        "device_allocation_bytes",
        "engine_shader_resources",
        "retired_shader_resources",
    ):
        assert len({row[field] for row in warm[2:]}) == 1, (field, warm)
    report["warm_unique_timelines"] = warm
    # Every final image remains valid, including an off-frame binding failure on
    # frame three and recovery on frame four, in one persistent final session.
    f.setup("parameter")
    pass_fixture.parm(f.sop, "snippet").set("v@N=set(1,0,0);@P.x+=@Time*72;")
    pass_fixture.parm(f.assigned, "test_speed").set(0)
    f.output.setDisplayFlag(True)
    f.output.setRenderFlag(True)
    pass_fixture.parm(f.output, "snippet").set(
        'if(@Frame>3 && @Frame<3.5)s@shop_materialpath="/mat/batch_missing_shutter";'
    )
    pass_fixture.parm(camera, "tx").setExpression("rint($T*24)*3", hou.exprLanguage.Hscript)
    configure_render(
        f.rop, dict(motion_samples=3, picture=str(dest / "frame.$F4.exr"), crypto_material=1, crypto_levels=2)
    )
    # The default this test was written for (before the ROP took Blender's defaults).
    f.rop.parm("persistent_data").set(1)
    f.rop.render(frame_range=(1, 5, 1))
    summary = f.rop.cachedUserData(final_render._summary_key)
    assert summary["completed_frames"] == 5 and summary["created_sessions"] == 1 and not summary["session_alive"], (
        summary
    )
    reference = oiio.ImageBuf(str(references / "position-3.exr"))
    ref = reference.get_pixels()
    ref = ref[:, :, [list(reference.spec().channelnames).index(c) for c in ("R", "G", "B")]]
    batch = []
    for index in range(1, 6):
        image = oiio.ImageBuf(str(dest / f"frame.{index:04d}.exr"))
        channels = list(image.spec().channelnames)
        pixels = image.get_pixels()
        error = np.abs(pixels[:, :, [channels.index(c) for c in ("R", "G", "B")]] - ref)
        assert error.mean() < 0.0008, (index, error.mean())
        issues = json.loads(image.spec().get_string_attribute("cycles_houdini:source_diagnostics") or "[]")
        assert {issue["code"] for issue in issues} == ({"motion.displacement_binding"} if index == 3 else set()), issues
        batch.append(dict(frame=index, mean_difference=float(error.mean()), diagnostics=issues))
    report.update(batch=batch, final=summary)
    hou.setFrame(1)
    if "4090" in device or not (dest / "sampled-overrides.hiplc").exists():
        hou.hipFile.save(str(dest / "sampled-overrides.hiplc"))
    report["result"] = "passed"
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    registry.close_all()
    (dest / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(dict(result=report["result"], checks=len(report["checks"]))))
