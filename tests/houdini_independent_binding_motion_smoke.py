# SPDX-License-Identifier: Apache-2.0
"""Material SOP shutter values must not change the selected geometry-motion policy."""

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
from independent_binding_fixture import motion_pixels
from light_shader_fixture import write_image

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
device = parser.parse_args().device
root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
dest = artifacts_root() / "stage2/displacement/independent-bindings"
dest.mkdir(parents=True, exist_ok=True)
f = Fixture(device)
reference_dir = dest / "references"
run = subprocess.run(
    [
        str(runtime_executable("ch_harness.exe")),
        "--resources",
        os.environ["CYCLES_HOUDINI_RESOURCES"],
        "--device",
        device,
        "--displacement-motion-reference",
        "--output",
        str(reference_dir),
    ],
    capture_output=True,
    text=True,
    timeout=180,
)
assert run.returncode == 0, (run.stdout, run.stderr)
references = {}
for count in (3, 5):
    image = oiio.ImageBuf(str(reference_dir / f"position-{count}.exr"))
    pixels = image.get_pixels()
    references[count] = np.flipud(pixels[:, :, [list(image.spec().channelnames).index(c) for c in ("R", "G", "B")]])
entry = registry.for_rop(f.rop, native_scene.configuration(f.rop))
report = dict(result="running", device=device, checks=[])


def render(label, samples=3, retain=False, shift=0, codes=(), geometry=1, objects=1, rate=72, centres=None):
    before = entry.session.status()
    pass_fixture.parm(f.rop, "motion_samples").set(samples)
    frame = entry.session.wait(entry.apply(native_scene.capture(f.rop), hou.time()))
    status = entry.session.status()
    deadline = time.monotonic() + 3
    while status["state"] != "complete" and time.monotonic() < deadline:
        assert status["state"] != "failed", status
        time.sleep(0.005)
        status = entry.session.status()
    pixels = f.pixels(frame)
    expected = np.roll(references[samples], shift, axis=1).copy()
    if rate != 72:
        expected = np.zeros_like(expected)
        x = (np.arange(160) + 0.5 - 80) / 40
        if rate:
            half = rate / 96
            coverage = np.maximum(0, np.minimum(half, x + 0.75) - np.maximum(-half, x - 0.75)) / (2 * half)
            expected[25:65] = coverage[None, :, None] * np.asarray((0.2, 0.4, 0.8))[None, None, :]
        else:
            expected[25:65, 50:110] = (0.2, 0.4, 0.8)
        expected = np.roll(expected, shift, axis=1)
    if centres is not None:
        expected = motion_pixels(centres, shift)
    if shift < 0:
        expected[:, shift:] = 0
    elif shift > 0:
        expected[:, :shift] = 0
    difference = np.abs(pixels["combined"][:, :, :3] - expected)
    report["checks"].append(dict(label=label, mean_difference=float(difference.mean()), status=status, previous=before))
    assert status["state"] == "complete" and not status["motion_warnings"], status
    if codes is None:
        assert status["source_diagnostics"], status
    else:
        assert {issue["code"] for issue in status["source_diagnostics"]} == set(codes), status
    assert status["geometry_resources"] == geometry and status["object_resources"] == objects, status
    assert difference.mean() < 0.0008, (label, float(difference.mean()))
    if retain:
        assert (
            status["geometry_conversions"] == before["geometry_conversions"]
            and status["extractions"] == before["extractions"]
        )
        assert (
            status["displacement_rebuilds"] == before["displacement_rebuilds"] and status["material_motion_cooks"] == 0
        ), status
    return status, pixels


try:
    for mode in ("velocity", "transform"):
        f.setup("parameter")
        pass_fixture.parm(f.rop, "geometry_motion").set(mode)
        pass_fixture.parm(f.assigned, "test_speed").set(24 if mode == "velocity" else 72)
        # The off-frame SOP positions deliberately contradict both policies.
        # Only v (Velocity) or the nominal geometry (Transform) may be consumed.
        pass_fixture.parm(f.sop, "snippet").set("v@N=set(1,0,0);v@v=set(48,0,0);@P.x+=@Time*900;")
        camera = hou.node(pass_fixture.evaluate(f.rop, "camera"))
        for samples in (3, 5):
            initial, _ = render(mode + " geometry with sampled Material SOP displacement", samples)
            assert initial["material_motion_samples"] == samples and initial["material_motion_cooks"] > 0, initial
            pass_fixture.parm(camera, "tx").set(0.05)
            render(mode + " cached camera edit", samples, retain=True, shift=-2)
            pass_fixture.parm(camera, "tx").set(0)
            render(mode + " cached camera restoration", samples, retain=True)
            pass_fixture.parm(f.obj, "tx").set(-0.45)
            render(mode + " cached object edit", samples, retain=True, shift=2)
            pass_fixture.parm(f.obj, "tx").set(-0.5)
            render(mode + " cached object restoration", samples, retain=True)
            for offset in (-1, 1):
                pass_fixture.parm(f.rop, "shutteroffset").set(offset)
                pass_fixture.parm(camera, "tx").set(offset * 0.75)
                render(mode + " binding samples with offset " + str(offset), samples)
            pass_fixture.parm(f.rop, "shutteroffset").set(0)
            pass_fixture.parm(camera, "tx").set(0)
    # Negative caching must not scan an unchanged animated non-displaced mesh,
    # and a shader-only change must activate sampling without a SOP edit.
    f.setup("parameter")
    pass_fixture.parm(f.rop, "geometry_motion").set("velocity")
    pass_fixture.parm(f.assigned, "test_speed").set(24)
    pass_fixture.parm(f.sop, "snippet").set("v@N=set(1,0,0);v@v=set(48,0,0);@P.x+=@Time*900;")
    pass_fixture.parm(f.material, "cycles_displacement_method").set("bump")
    pass_fixture.parm(camera, "tx").set(-0.5)
    render("Animated bindings on a non-displaced mesh need no shutter cook", rate=48)
    pass_fixture.parm(camera, "tx").set(-0.45)
    render("Non-displaced animated mesh uses its negative cache", rate=48, shift=-2, retain=True)
    pass_fixture.parm(f.material, "cycles_displacement_method").set("true")
    pass_fixture.parm(camera, "tx").set(0)
    activated, _ = render("Enabling displacement activates binding samples without a SOP edit")
    assert activated["material_motion_cooks"] > 0
    # Two faces have the same nominal assignment but different histories.
    f.setup("parameter")
    pass_fixture.parm(f.rop, "geometry_motion").set("transform")
    clone = f.obj.createNode("xform", "second_face")
    clone.setInput(0, f.assigned)
    pass_fixture.parm(clone, "tx").set(5)
    merged = f.obj.createNode("merge", "two_faces")
    merged.setInput(0, f.assigned)
    merged.setInput(1, clone)
    f.output.setInput(0, merged)
    pass_fixture.parm(f.output, "snippet").set(
        "s@material_override=sprintf(\"{'height':%g}\",.5+@Time*(@primnum==0?72:0));"
    )
    render("Equal nominal face bindings retain distinct displacement histories")
    pass_fixture.parm(f.output, "snippet").set(
        "s@material_override=sprintf(\"{'height':%g}\",.5+@Time*(@primnum==0?0:72));"
    )
    render("Only the second face animates after swapping histories", rate=0)
    pass_fixture.parm(f.output, "snippet").set(
        "s@material_override=sprintf(\"{'height':%g}\",.5+@Time*(@primnum==0?72:0));"
    )
    render("Restoring the first face history repairs its motion")
    f.output.setInput(0, f.assigned)
    targets = f.obj.createNode("line", "two_targets")
    pass_fixture.set_parameters(targets, dict(points=2, dist=5))
    targets.parmTuple("dir").set((1, 0, 0))
    target_motion = f.obj.createNode("attribwrangle", "target_motion")
    target_motion.setInput(0, targets)
    pass_fixture.parm(target_motion, "snippet").set("")
    copies = f.obj.createNode("copytopoints", "packed_binding_histories")
    copies.setInput(0, f.output)
    copies.setInput(1, target_motion)
    pass_fixture.parm(copies, "pack").set(1)
    copies.setDisplayFlag(True)
    copies.setRenderFlag(True)
    for mode in ("velocity", "transform"):
        f.setup("parameter")
        pass_fixture.parm(f.rop, "geometry_motion").set(mode)
        pass_fixture.parm(f.assigned, "test_speed").set(24 if mode == "velocity" else 72)
        pass_fixture.parm(f.sop, "snippet").set("v@N=set(1,0,0);v@v=set(48,0,0);@P.x*=1+@Time*36;")
        shared, _ = render(mode + " compatible packed histories share a prototype", objects=2)
        assert shared["packed_instances"] == 2
        pass_fixture.parm(camera, "tx").set(0.05)
        render(mode + " packed camera edit retains sampled bindings", objects=2, shift=-2, retain=True)
    # Packed transforms use the already acquired binding metadata, not another
    # round of SOP cooks, while the prototype geometry stays nominal.
    f.setup("parameter")
    pass_fixture.parm(f.rop, "geometry_motion").set("transform")
    pass_fixture.parm(f.assigned, "test_speed").set(24)
    pass_fixture.parm(f.sop, "snippet").set("v@N=set(1,0,0);@P.x*=1+@Time*36;")
    pass_fixture.parm(target_motion, "snippet").set("@P.x+=@Time*48;")
    transformed, _ = render("Packed transform and material samples reuse one acquisition", objects=2)
    assert transformed["motion_cooks"] == transformed["material_motion_cooks"] > 0, transformed
    pass_fixture.parm(camera, "tx").set(0.05)
    render("Packed transform/material cache retains geometry", objects=2, shift=-2, retain=True)
    pass_fixture.parm(target_motion, "snippet").set("")
    pass_fixture.parm(camera, "tx").set(0)
    f.setup("parameter")
    pass_fixture.parm(f.rop, "geometry_motion").set("transform")
    pass_fixture.parm(f.assigned, "num_local1").set(0)
    pass_fixture.parm(f.sop, "snippet").set("v@N=set(1,0,0);")
    outer = f.obj.createNode("attribwrangle", "packed_outer_overrides")
    outer.setInput(0, copies)
    pass_fixture.parm(outer, "class").set(1)
    outer.setDisplayFlag(True)
    outer.setRenderFlag(True)
    split = "s@material_override=sprintf(\"{'height':%g}\",.5+@Time*(@primnum==0?72:0));"
    pass_fixture.parm(outer, "snippet").set(split)
    render("Equal nominal packed overrides split differing histories", objects=2, geometry=2)
    pass_fixture.parm(outer, "snippet").set("s@material_override=sprintf(\"{'height':%g}\",.5+@Time*72);")
    render("Repairing packed override histories merges compatible geometry", objects=2)
    pass_fixture.parm(outer, "snippet").set(
        "s@material_override=sprintf(\"{'height':%g,'power':%g}\",.5+@Time*72,1+@Time*(@primnum==0?10:20));"
    )
    render("Unused off-frame surface overrides do not split identical displacement histories", objects=2)
    pass_fixture.parm(outer, "snippet").set(split)
    render("Diverging histories split geometry again", objects=2, geometry=2)
    f.output.setDisplayFlag(True)
    f.output.setRenderFlag(True)
    # A positive binding cache must still notice a previously surface-only
    # prototype gaining displacement. The other displaced prototype stays
    # outside the camera; the visible one's box-shutter coverage is analytic.
    f.setup("parameter")
    pass_fixture.parm(f.rop, "geometry_motion").set("transform")
    pass_fixture.parm(f.sop, "snippet").set("v@N=set(1,0,0);")
    second = ch.create_material(name="second_cached_packed_binding", shader="emission")
    second.node("surface_shader").parmTuple("color").set((0.2, 0.4, 0.8))
    pass_fixture.parm(second.node("surface_shader"), "strength").set(1)
    second_height = second.createNode("cycles::displacement::1", "height")
    pass_fixture.set_parameters(second_height, dict(height=0.5, midlevel=0, scale=1, space="object"))
    ch.promote_parameter(second, second_height, "height", name="height")
    second.node("surface_output").setNamedInput("displacement", second_height, "displacement")
    second_assigned = f.obj.createNode("material", "second_cached_assignment")
    second_assigned.setInput(0, f.sop)
    pass_fixture.set_parameters(
        second_assigned,
        dict(shop_materialpath1=second.path(), num_local1=1, local1_name1="height", local1_type1="float"),
    )
    pass_fixture.parm(second_assigned, "local1_fval1").setExpression(".5+$T*72", hou.exprLanguage.Hscript)
    distant = f.obj.createNode("xform", "distant_displaced_prototype")
    distant.setInput(0, f.output)
    pass_fixture.parm(distant, "tx").set(5)
    first_pack = f.obj.createNode("pack", "first_cached_prototype")
    first_pack.setInput(0, distant)
    second_pack = f.obj.createNode("pack", "second_cached_prototype")
    second_pack.setInput(0, second_assigned)
    mixed = f.obj.createNode("merge", "mixed_displacement_demand")
    mixed.setInput(0, first_pack)
    mixed.setInput(1, second_pack)
    mixed.setDisplayFlag(True)
    mixed.setRenderFlag(True)
    for label, method, centres in (
        ("A positive cache includes a surface-only prototype", "bump", (-0.5, -0.5, -0.5)),
        ("Enabling displacement on another cached prototype samples its bindings", "true", (-0.75, 0, 0.75)),
        ("Disabling the second prototype displacement restores nominal geometry", "bump", (-0.5, -0.5, -0.5)),
        ("Re-enabling the second prototype displacement restores its blur", "true", (-0.75, 0, 0.75)),
    ):
        pass_fixture.parm(second, "cycles_displacement_method").set(method)
        render(label, objects=2, geometry=2, centres=centres)
    f.output.setDisplayFlag(True)
    f.output.setRenderFlag(True)
    f.setup("parameter")
    pass_fixture.parm(f.rop, "geometry_motion").set("velocity")
    pass_fixture.parm(f.assigned, "test_speed").set(24)
    pass_fixture.parm(f.sop, "snippet").set("v@N=set(1,0,0);v@v=set(48,0,0);")
    missing = "/mat/independent_binding_repair"
    pass_fixture.parm(f.output, "snippet").set('if(@Time>0)s@shop_materialpath="' + missing + '";')
    render(
        "Missing shutter material retains nominal displacement and velocity",
        rate=48,
        codes=("motion.displacement_binding",),
    )
    repaired = ch.create_material(name=missing.rsplit("/", 1)[1], shader="emission")
    displacement = repaired.createNode("cycles::displacement::1", "displacement")
    pass_fixture.set_parameters(displacement, dict(height=0.5, midlevel=0, scale=1))
    ch.promote_parameter(repaired, displacement, "height", name="height")
    repaired.node("surface_output").setNamedInput("displacement", displacement, "displacement")
    pass_fixture.parm(repaired, "cycles_displacement_method").set("true")
    fixed, _ = render("Creating the missing material repairs cached binding samples")
    assert fixed["material_motion_cooks"] == 0, fixed
    repaired.destroy()
    render(
        "Deleting a shutter material recovers without dropping velocity",
        rate=48,
        codes=("motion.displacement_binding",),
    )
    f.setup("parameter")
    pass_fixture.parm(f.rop, "geometry_motion").set("transform")
    pass_fixture.parm(f.assigned, "num_local1").set(0)
    pass_fixture.parm(f.sop, "snippet").set("v@N=set(1,0,0);")
    missing = "/mat/equivalent_offframe_dependency"
    repaired = ch.create_material(name=missing.rsplit("/", 1)[1], shader="emission")
    displacement = repaired.createNode("cycles::displacement::1", f.scalar.name())
    pass_fixture.set_parameters(displacement, dict(height=0.5, midlevel=0, scale=1))
    ch.promote_parameter(repaired, displacement, "height", name="height")
    repaired.node("surface_output").setNamedInput("displacement", displacement, "displacement")
    pass_fixture.parm(repaired, "cycles_displacement_method").set("true")
    pass_fixture.parm(f.output, "snippet").set('if(@Time>0)s@shop_materialpath="' + missing + '";')
    render("An equivalent off-frame shader retains nominal displacement", rate=0)
    assert missing in entry.session.native_material_paths(), entry.session.native_material_paths()
    entry.observer.refresh_material_bindings(entry.session)
    assert repaired in {node for node, events in entry.observer.nodes}
    pass_fixture.parm(repaired, "height").setExpression(".5+$T*72", hou.exprLanguage.Hscript)
    changed, _ = render("Editing an equivalent off-frame shader restores its temporal influence", centres=(0, 0, 0.75))
    assert changed["material_motion_cooks"] == 0, changed
    pass_fixture.parm(repaired, "height").deleteAllKeyframes()
    pass_fixture.parm(repaired, "height").set(0.5)
    render("Restoring equivalent off-frame displacement removes unnecessary temporal geometry", rate=0)
    pass_fixture.parm(f.sop, "snippet").set("v@N=set(1,0,0);f@existing_mask=.5;")
    field = repaired.createNode("cycles::attribute::1", "offframe_field")
    pass_fixture.parm(field, "attribute").set("existing_mask")
    displacement.setNamedInput("height", field, "fac")
    retained, _ = render("New off-frame displacement field demand captures the nominal attribute", rate=0)
    pass_fixture.parm(field, "attribute").set("missing_mask")
    broken, _ = render("Missing off-frame fields retain usable nominal displacement", rate=0, codes=None)
    assert any(issue.get("attribute") == "missing_mask" for issue in broken["source_diagnostics"]), broken
    pass_fixture.parm(field, "attribute").set("existing_mask")
    render("Repairing an off-frame field restores its sampled binding", rate=0)
    displacement.setInput(displacement.inputIndex("height"), None)
    removed, _ = render("Removing off-frame fields retires their storage", rate=0)
    assert removed["owned_geometry_bytes"] < retained["owned_geometry_bytes"], (retained, removed)
    texture = repaired.createNode("cycles::image_texture::1", "offframe_image")
    pass_fixture.set_parameters(texture, dict(colorspace="data", interpolation="closest"))
    height_file = dest / "offframe-height.exr"
    write_image(height_file, (0.5, 0.5, 0.5))
    pass_fixture.parm(texture, "filename").set(str(height_file))
    coordinate = repaired.createNode("cycles::texture_coordinate::1", "offframe_coordinates")
    texture.setNamedInput("vector", coordinate, "object")
    displacement.setNamedInput("height", texture, "color")
    render("Owned off-frame image bindings evaluate displacement", rate=0)
    texture.setNamedInput("vector", coordinate, "generated")
    render("Sampled Generated coordinates retain valid off-frame displacement", rate=0)
    texture.setNamedInput("vector", coordinate, "object")
    render("Switching supported coordinate sources retains displacement", rate=0)
    pass_fixture.parm(texture, "filename").set(str(dest / "missing-offframe-height.exr"))
    render("Missing off-frame images freeze displacement without diagnostic geometry", rate=0, codes=None)
    pass_fixture.parm(texture, "filename").set(str(height_file))
    render("Repairing off-frame images clears recovery", rate=0)
    displacement.setInput(displacement.inputIndex("height"), None)
    repaired.destroy()
    render(
        "Deleting an off-frame dependency reports recovery without dropping geometry",
        rate=0,
        codes=("motion.displacement_binding",),
    )
    for mode in ("velocity", "transform"):
        for scheme in ("linear", "catmull_clark"):
            f.setup("parameter")
            pass_fixture.parm(f.rop, "geometry_motion").set(mode)
            pass_fixture.parm(f.assigned, "test_speed").set(24 if mode == "velocity" else 72)
            pass_fixture.parm(f.sop, "snippet").set("v@N=set(1,0,0);v@v=set(48,0,0);@P.x+=@Time*900;")
            configure_render(f.rop, dict(subdivision=dict(type=scheme, space="object", dicing_rate=0.2, max_level=3)))
            pass_fixture.parm(f.vector, "space").set("object")
            for component in f.vector.parmTuple("vector"):
                component.deleteAllKeyframes()
                component.set(0, follow_parm_reference=False)
            f.vector.parmTuple("vector")[0].set(pass_fixture.parm(f.material, "height"), follow_parm_reference=False)
            f.material.node("surface_output").setNamedInput("displacement", f.vector, "displacement")
            for samples in (3, 5):
                render(mode + " " + scheme + " vector displacement samples", samples)
            pass_fixture.parm(camera, "tx").set(0.05)
            render(mode + " " + scheme + " camera retains sampled subdivision", 5, retain=True, shift=-2)
    f.setup("parameter")
    pass_fixture.parm(f.rop, "geometry_motion").set("velocity")
    pass_fixture.parm(f.sop, "snippet").set("v@N=set(1,0,0);v@v=set(48,0,0);")
    warm = []
    for index in range(16):
        rate = 72 + index
        pass_fixture.parm(f.assigned, "test_speed").set(rate - 48)
        status, _ = render("Distinct cached binding timeline " + str(index), rate=rate)
        warm.append(
            {
                key: status[key]
                for key in (
                    "owned_geometry_bytes",
                    "device_allocation_bytes",
                    "engine_shader_resources",
                    "retired_shader_resources",
                )
            }
        )
    for key in warm[0]:
        assert len({row[key] for row in warm[2:]}) == 1, (key, warm)
    report["warm_unique_timelines"] = warm
    registry.close_all()
    f.setup("parameter")
    pass_fixture.parm(f.rop, "geometry_motion").set("velocity")
    pass_fixture.parm(f.assigned, "test_speed").set(24)
    pass_fixture.parm(f.sop, "snippet").set("v@N=set(1,0,0);v@v=set(48,0,0);@P.x+=@Time*48;")
    pass_fixture.parm(f.obj.node("grid1"), "rows").setExpression("2+$F%3", hou.exprLanguage.Hscript)
    pass_fixture.parm(f.output, "snippet").set(
        'if(@Frame>3 && @Frame<3.5)s@shop_materialpath="/mat/batch_missing_independent_binding";'
    )
    copies.setDisplayFlag(True)
    copies.setRenderFlag(True)
    pass_fixture.parm(camera, "tx").setExpression("rint($T*24)*3", hou.exprLanguage.Hscript)
    configure_render(
        f.rop,
        dict(
            motion_samples=3, persistent_data=1, picture=str(dest / "frame.$F4.exr"), crypto_material=1, crypto_levels=2
        ),
    )
    f.rop.render(frame_range=(1, 5, 1))
    summary = f.rop.cachedUserData(final_render._summary_key)
    assert summary["completed_frames"] == 5 and summary["created_sessions"] == 1 and not summary["session_alive"], (
        summary
    )
    batch = []
    for index in range(1, 6):
        image = oiio.ImageBuf(str(dest / f"frame.{index:04d}.exr"))
        pixels = image.get_pixels()
        channels = list(image.spec().channelnames)
        expected = motion_pixels((-0.5, 0, 0.5)) if index == 3 else np.flipud(references[3])
        error = np.abs(pixels[:, :, [channels.index(c) for c in ("R", "G", "B")]] - expected)
        assert error.mean() < 0.0008, (index, float(error.mean()))
        issues = json.loads(image.spec().get_string_attribute("cycles_houdini:source_diagnostics") or "[]")
        assert {issue["code"] for issue in issues} == ({"motion.displacement_binding"} if index == 3 else set()), issues
        geometry = f.output.geometryAtFrame(index)
        assert not any(
            (
                geometry.findPointAttrib("id"),
                geometry.findPointAttrib("name"),
                geometry.findPrimAttrib("id"),
                geometry.findPrimAttrib("name"),
            )
        )
        batch.append(
            dict(
                frame=index,
                points=len(geometry.points()),
                primitives=len(geometry.prims()),
                mean_difference=float(error.mean()),
                diagnostics=issues,
            )
        )
    assert len({row["points"] for row in batch}) == 3
    report.update(batch=batch, final=summary)
    report["result"] = "passed"
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    registry.close_all()
    (dest / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(dict(result=report["result"], checks=len(report["checks"]))))
