# SPDX-License-Identifier: Apache-2.0
"""Animated OBJ styling uses shutter values without borrowing geometry motion."""

import argparse, json, time, traceback
import hou, numpy as np, OpenImageIO as oiio
import cycles_houdini as ch
from cycles_houdini import native_scene, registry, final_render
from sampled_override_fixture import Fixture
from independent_binding_fixture import motion_pixels

p = argparse.ArgumentParser()
p.add_argument("--device", default="CPU")
device = p.parse_args().device
f = Fixture(device)
f.assigned.parm("num_local1").set(0)
f.output.setInput(0, f.sop)
f.output.parm("snippet").set("")
f.sop.parm("snippet").set("v@N=set(1,0,0);v@v=set(0,0,0);")
f.obj.addSpareParmTuple(hou.StringParmTemplate("shop_materialstylesheet", "Stylesheet", 1))
control = f.obj.parm("shop_materialstylesheet")
dest = f.dest.parent / "object-binding-shutter"
dest.mkdir(exist_ok=True)
entry = registry.for_rop(f.rop, native_scene.configuration(f.rop))
report = dict(result="running", device=device, checks=[])


def style(broken=False):
    control.setExpression(
        'import json\nreturn json.dumps({"styles":[{"overrides":{"surfaceParameters":{"power":2+hou.time()*3},"displaceParameters":{"height":'
        + ('"bad" if hou.time()>0 else ' if broken else "")
        + ".5+hou.time()*72}}}]})",
        hou.exprLanguage.Python,
    )


def render(label, samples, centres, warning=False, retain=False, objects=1, geometry=1, power=2, expected_samples=None):
    f.rop.parm("motion_samples").set(samples)
    before = entry.session.status()
    cooks = f.sop.cookCount()
    update = native_scene.capture(f.rop)
    frame = entry.session.wait(entry.apply(update, hou.time()))
    status = entry.session.status()
    deadline = time.monotonic() + 3
    while status["state"] != "complete" and time.monotonic() < deadline:
        time.sleep(0.005)
        status = entry.session.status()
    pixels = f.pixels(frame)["combined"][:, :, :3]
    difference = float(np.abs(pixels - motion_pixels(centres) * power).mean())
    report["checks"].append(
        dict(
            label=label,
            samples=samples,
            mode=f.rop.evalParm("geometry_motion"),
            mean_difference=difference,
            status=status,
            sop_cooks=f.sop.cookCount(),
            previous_cooks=cooks,
            host_errors={node.path(): node.errors() for node in (f.obj, f.sop, f.output)},
        )
    )
    assert difference / power < 0.0008, (label, difference, status)
    assert status["state"] == "complete" and not status["error"] and bool(status["source_diagnostics"]) == warning, (
        status
    )
    assert (
        not status["motion_warnings"]
        and status["geometry_resources"] == geometry
        and status["object_resources"] == objects
    ), status
    assert len(update["geometry"][0]["object_material_samples"]) == (
        samples if expected_samples is None else expected_samples
    ), update["geometry"][0]
    if len(report["checks"]) > 1:
        assert status["renderer_pid"] == report["checks"][0]["status"]["renderer_pid"]
    if f.rop.evalParm("geometry_motion") != "deformation":
        assert status["material_motion_cooks"] == 0 and f.sop.cookCount() == cooks, status
    if retain:
        assert (
            status["geometry_conversions"] == before["geometry_conversions"]
            and status["displacement_rebuilds"] == before["displacement_rebuilds"]
        ), status


def malformed():
    style()
    control.setExpression(
        control.expression().replace("return json.dumps", 'if hou.time()>0:return "{broken"\nreturn json.dumps'),
        hou.exprLanguage.Python,
    )


def alternate_material():
    material = ch.create_material(name="object_shutter_alternate", shader="emission")
    shader = material.node("surface_shader")
    shader.parmTuple("color").set((0.9, 0.1, 0.2))
    shader.parm("strength").set(1)
    height = material.createNode("cycles::displacement::1", "height")
    height.setParms(dict(height=0.5, midlevel=0, scale=1, space="object"))
    material.node("surface_output").setNamedInput("displacement", height, "displacement")
    material.parm("cycles_displacement_method").set("true")
    ch.promote_parameter(material, height, "height", name="height")
    ch.promote_parameter(material, shader, "strength", name="power")
    return material


try:
    # Establish the nominal SOP before asserting static geometry is not recooked.
    f.output.geometry()
    for mode in ("transform", "velocity", "deformation"):
        f.rop.parm("geometry_motion").set(mode)
        for samples in (3, 5):
            style()
            centres = np.linspace(-0.75, 0.75, samples)
            render("Object stylesheet samples true displacement", samples, centres)
            render("Unchanged object shutter retains geometry", samples, centres, retain=True)
            style(True)
            render("Bad off-frame object field retains authored height", samples, np.minimum(centres, 0), True)
            render("Cached object shutter preserves recovery warning", samples, np.minimum(centres, 0), True, True)
            style()
            render("Object field repairs during IPR", samples, centres)
        malformed()
        render("Malformed off-frame object JSON keeps usable geometry", 3, (-0.75, 0, 0), True)
        render("Cached malformed object JSON retains its warning", 3, (-0.75, 0, 0), True, True)
        style()
        render("Malformed object JSON repairs during IPR", 3, (-0.75, 0, 0.75))
    f.rop.parm("geometry_motion").set("transform")
    camera = hou.node(f.rop.evalParm("camera"))
    for samples in (3, 5):
        for offset in (-1, 1):
            f.rop.parm("shutteroffset").set(offset)
            camera.parm("tx").set(offset * 0.75)
            render("Object styling with offset shutter " + str(offset), samples, np.linspace(-0.75, 0.75, samples))
    f.rop.parm("shutteroffset").set(0)
    camera.parm("tx").set(0)
    style()
    control.setExpression(
        control.expression().replace("return json.dumps", 'if hou.time()<=0:return ""\nreturn json.dumps'),
        hou.exprLanguage.Python,
    )
    render("A rule absent at the requested frame can contribute later in the shutter", 3, (0, 0, 0.75), power=1)
    control.deleteAllKeyframes()
    control.set("")
    if f.obj.parm("material_override") is None:
        f.obj.addSpareParmTuple(hou.StringParmTemplate("material_override", "Material Override", 1))
    override = f.obj.parm("material_override")
    override.setExpression('repr({"power":2,"height":.5+hou.time()*72})', hou.exprLanguage.Python)
    render("Object local overrides sample their own shutter values", 3, (-0.75, 0, 0.75))
    override.setExpression(
        'repr({"power":2,"height":"bad" if hou.time()>0 else .5+hou.time()*72})', hou.exprLanguage.Python
    )
    render("Invalid object override preserves valid shutter samples", 3, (-0.75, 0, 0), True)
    override.setExpression('repr({"power":2,"height":.5+hou.time()*72})', hou.exprLanguage.Python)
    render("Object local override repair", 3, (-0.75, 0, 0.75))
    override.deleteAllKeyframes()
    override.set("")
    style()
    other = alternate_material()
    path = other.path()
    f.obj.parm("shop_materialpath").setExpression(
        repr(f.material.path()) + " if hou.time()<=0 else " + repr(path), hou.exprLanguage.Python
    )
    render("Animated object material assignment retains nominal surface shading", 3, (-0.75, 0, 0.75))
    other.destroy()
    render("Missing off-frame object material retains requested-frame displacement", 3, (0, 0, 0), True)
    other = alternate_material()
    render("Recreated off-frame object material clears recovery", 3, (-0.75, 0, 0.75))
    f.obj.parm("shop_materialpath").deleteAllKeyframes()
    f.obj.parm("shop_materialpath").set(f.material.path())
    future = dest / "future-style.json"

    def write_future(height, padding=False):
        future.write_text(
            json.dumps(
                {
                    "styles": [
                        {
                            "overrides": {
                                "surfaceParameters": {"power": 2},
                                "displaceParameters": {"height": height},
                                **({"materialParameters": {"unused_large_value": "x" * 900000}} if padding else {}),
                            }
                        }
                    ]
                }
            )
        )

    write_future(1.25)
    style()
    control.setExpression(
        control.expression().replace(
            "return json.dumps",
            'if hou.time()>0:return json.dumps({"importFiles":[' + repr(future.as_posix()) + "]})\nreturn json.dumps",
            1,
        ),
        hou.exprLanguage.Python,
    )
    render("An import used only off-frame contributes displacement", 3, (-0.75, 0, 0.75))
    write_future(1)
    render("Editing an off-frame import invalidates its sampled binding", 3, (-0.75, 0, 0.5))
    future.unlink()
    render("Deleting an off-frame import keeps authored height", 3, (-0.75, 0, 0), True)
    write_future(1.25)
    render("Recreating an off-frame import repairs sampled height", 3, (-0.75, 0, 0.75))
    future.write_text("{broken")
    render("Malformed off-frame import retains geometry", 3, (-0.75, 0, 0), True)
    write_future(0.75)
    render("Repairing an off-frame import clears warnings", 3, (-0.75, 0, 0.25))
    write_future(1.25, True)
    render("Object source capacity retains requested-frame geometry", 129, (0, 0, 0), True, expected_samples=0)
    assert any("8 MiB" in issue["message"] for issue in report["checks"][-1]["status"]["source_diagnostics"])
    assert "file:" + future.as_posix() in entry.session.native_stylesheet_resources()
    write_future(1.25)
    times = np.linspace(-0.75, 0.75, 129)
    render("Reducing an off-frame import repairs the source capacity", 129, np.where(times > 0, 0.75, times))
    style()
    render("Removing the off-frame import restores inline styling", 3, (-0.75, 0, 0.75))
    targets = f.obj.createNode("line", "object_shutter_targets")
    targets.setParms(dict(points=2, dist=8))
    targets.parmTuple("dir").set((1, 0, 0))
    copies = f.obj.createNode("copytopoints", "object_shutter_packed")
    copies.setInput(0, f.output)
    copies.setInput(1, targets)
    copies.parm("pack").set(1)
    copies.setDisplayFlag(True)
    copies.setRenderFlag(True)
    for mode in ("transform", "velocity", "deformation"):
        f.rop.parm("geometry_motion").set(mode)
        style()
        render("Packed object stylesheet " + mode, 3, (-0.75, 0, 0.75), objects=2)
        render("Packed object stylesheet cache " + mode, 3, (-0.75, 0, 0.75), objects=2, retain=True)
        style(True)
        render("Packed object stylesheet recovery " + mode, 3, (-0.75, 0, 0), True, objects=2)
        style()
        render("Packed object stylesheet repair " + mode, 3, (-0.75, 0, 0.75), objects=2)
    copies.setDisplayFlag(False)
    copies.setRenderFlag(False)
    f.output.setDisplayFlag(True)
    f.output.setRenderFlag(True)
    for node in (f.sop, f.output, copies):
        geometry_data = node.geometry()
        assert not any(
            (
                geometry_data.findPointAttrib("id"),
                geometry_data.findPointAttrib("name"),
                geometry_data.findPrimAttrib("id"),
                geometry_data.findPrimAttrib("name"),
            )
        )
    entry.session.close(wait=True)
    closed = entry.session.status()
    assert closed["owned_geometry_bytes"] == closed["device_allocation_bytes"] == 0
    f.rop.parm("geometry_motion").set("transform")
    control.setExpression(
        'import json\nframe=round(hou.frame())\nt=(hou.frame()-frame)/hou.fps()\nreturn json.dumps({"styles":[{"overrides":{"surfaceParameters":{"power":2},"displaceParameters":{"height":"bad" if frame==3 and t>0 else .5+t*72}}}]})',
        hou.exprLanguage.Python,
    )
    f.rop.setParms(dict(picture=str(dest / "object.$F4.exr"), persistent_data=1, motion_samples=3))
    f.rop.render(frame_range=(1, 5, 1))
    summary = f.rop.cachedUserData(final_render._summary_key)
    assert summary["completed_frames"] == 5 and summary["created_sessions"] == 1 and not summary["session_alive"], (
        summary
    )
    for number in range(1, 6):
        reader = oiio.ImageInput.open(str(dest / f"object.{number:04}.exr"))
        spec = reader.spec()
        pixels = reader.read_image(format=oiio.FLOAT)
        reader.close()
        channels = list(spec.channelnames)
        rgb = [channels.index(("Combined." if "Combined.R" in channels else "") + c) for c in "RGB"]
        expected = motion_pixels((-0.75, 0, 0) if number == 3 else (-0.75, 0, 0.75)) * 2
        assert np.abs(pixels[:, :, rgb] - expected).mean() / 2 < 0.0008
        issues = json.loads(spec.get_string_attribute("cycles_houdini:source_diagnostics") or "[]")
        assert bool(issues) == (number == 3), issues
    report.update(result="passed", closed=closed, batch=summary, batch_frames=5)
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    registry.close_all()
    (dest / "report.json").write_text(json.dumps(report, indent=2))
print(json.dumps(dict(result=report["result"], checks=len(report["checks"]))))
