# SPDX-License-Identifier: Apache-2.0
"""Native stylesheet targeting, priority, packed descendants, local repair and output."""

import pass_fixtures as pass_fixture

import argparse, json, traceback
import hou, numpy as np, OpenImageIO as oiio
import cycles_houdini as ch
from cycles_houdini import final_render
from light_shader_fixture import Fixture

p = argparse.ArgumentParser()
p.add_argument("--device", default="CPU")
f = Fixture(p.parse_args().device)
pass_fixture.set_parameters(f.rop, dict(use_material_assignments=1, samples=1, ipr_samples=1, pass_position=1))
pass_fixture.parm(f.light, "light_intensity").set(0)
pass_fixture.parm(f.sop, "cols").set(3)
f.sop.parmTuple("size").set((4, 4))
a = ch.create_material(name="stylesheet_a", shader="emission")
a.node("surface_shader").parmTuple("color").set((0.2, 0.4, 0.6))
pass_fixture.parm(a.node("surface_shader"), "strength").set(1)
b = ch.create_material(name="stylesheet_b", shader="emission")
b.node("surface_shader").parmTuple("color").set((0.8, 0.1, 0.2))
pass_fixture.parm(b.node("surface_shader"), "strength").set(1)
for mat in (a, b):
    ch.promote_parameter(mat, mat.node("surface_shader"), "color", name="tint")
    ch.promote_parameter(mat, mat.node("surface_shader"), "strength", name="power")
pass_fixture.parm(f.obj, "shop_materialpath").set(a.path())
f.obj.addSpareParmTuple(hou.StringParmTemplate("shop_materialstylesheet", "Material Style Sheet", 1))
source = f.obj.createNode("python", "stylesheet_controls")
source.setInput(0, f.sop)
for name in ("detail_sheet", "primitive_sheet"):
    source.addSpareParmTuple(hou.StringParmTemplate(name, name, 1))
pass_fixture.parm(source, "python").set("""node=hou.pwd();g=node.geometry()
for owner,name,value in ((hou.attribType.Global,'material_stylesheet',node.evalParm('detail_sheet')),(hou.attribType.Prim,'material_stylesheet',node.evalParm('primitive_sheet'))):
 a=g.addAttrib(owner,name,'')
 if owner==hou.attribType.Global:g.setGlobalAttribValue(a,value)
 elif g.prims():g.prims()[0].setAttribValue(a,value)
""")
source.setDisplayFlag(True)
source.setRenderFlag(True)
entry = f.session()
dest = f.dest.parent / "material-stylesheets"
dest.mkdir(exist_ok=True)
report = dict(result="running", device=f.device, checks=[])


def sheet(overrides, target=None):
    return json.dumps(dict(styles=[dict(target={} if target is None else target, overrides=overrides)]))


def set_object(value):
    pass_fixture.parm(f.obj, "shop_materialstylesheet").set(value)


def render(label, left=(0.2, 0.4, 0.6), right=None, warning=False):
    frame = f.update()
    status = entry.session.status()
    pixels = f.pixels(frame)
    for x, expected in ((16, left), (48, left if right is None else right)):
        np.testing.assert_allclose(pixels[32, x, :3], expected, atol=3e-5, rtol=3e-5)
    assert bool(status["source_diagnostics"]) == warning, status["source_diagnostics"]
    assert not status["error"] and status["state"] == "complete", status
    if report["checks"]:
        assert status["renderer_pid"] == report["checks"][0]["status"]["renderer_pid"]
    report["checks"].append(dict(label=label, status=status))
    return frame


try:
    render("Underlying native assignment")
    set_object(sheet({"materialParameters": {"power": 3}}, dict(type="self")))
    render("Object self target and exported numeric override", (0.6, 1.2, 1.8))
    set_object(sheet({"material": {"name": b.path()}}, dict(group="0")))
    render("Object primitive group targets one face", (0.8, 0.1, 0.2), (0.2, 0.4, 0.6))
    set_object(sheet({"materialParameters": {"tint": [0.1, 0.7, 0.2], "power": 2}}))
    render("Native array and numeric stylesheet results", (0.2, 1.4, 0.4))
    pass_fixture.parm(source, "detail_sheet").set(sheet({"materialParameters": {"power": 7}}))
    render("Parent stylesheet retains native higher priority", (0.2, 1.4, 0.4))
    set_object("")
    render("Detail stylesheet applies to geometry", (1.4, 2.8, 4.2))
    pass_fixture.parm(source, "detail_sheet").set("")
    pass_fixture.parm(source, "primitive_sheet").set(
        sheet({"material": {"name": b.path()}, "materialParameters": {"power": 2}}, dict(type="self"))
    )
    render("Primitive self stylesheet affects only its owner", (1.6, 0.2, 0.4), (0.2, 0.4, 0.6))
    pass_fixture.parm(source, "primitive_sheet").set("")
    for i in range(2):
        set_object(sheet({"materialParameters": {"power": 4, "unknown": 1}}))
        render("Unknown field retains valid override " + str(i), (0.8, 1.6, 2.4), warning=True)
        set_object(sheet({"materialParameters": {"power": 2}}))
        render("Field repair " + str(i), (0.4, 0.8, 1.2))
        set_object("{broken")
        render("Malformed stylesheet retains geometry " + str(i), warning=True)
        set_object("")
        render("Stylesheet repair " + str(i))
    set_object(sheet({"material": {"name": "/mat/missing_style_material"}}, dict(group="0")))
    render("Missing assigned material is local", (1, 0, 1), (0.2, 0.4, 0.6), True)
    missing = ch.create_material(name="missing_style_material", shader="emission")
    missing.node("surface_shader").parmTuple("color").set((0.4, 0.5, 0.6))
    pass_fixture.parm(missing.node("surface_shader"), "strength").set(1)
    render("Created material repairs native stylesheet binding", (0.4, 0.5, 0.6), (0.2, 0.4, 0.6))
    set_object("")
    render("Removed rule restores underlying assignment")
    displacement = a.createNode("cycles::displacement::1", "stylesheet_height")
    pass_fixture.set_parameters(displacement, dict(height=0.15, scale=1, midlevel=0, space="object"))
    a.node("surface_output").setNamedInput("displacement", displacement, "displacement")
    pass_fixture.parm(a, "cycles_displacement_method").set("true")
    ch.promote_parameter(a, displacement, "height", name="height")
    set_object(sheet({"materialParameters": {"height": 0.35, "power": 2}}))
    frame = render("Stylesheet changes true displacement and surface", (0.4, 0.8, 1.2))
    # The fixture's exported grid winding points toward -Z.
    np.testing.assert_allclose(f.pixels(frame, "position")[32, 16, 2], -0.35, atol=2e-5)
    set_object(sheet({"materialParameters": {"height": 0.35, "tint": "invalid_color", "power": 2}}))
    frame = render("Bad Color override retains valid true displacement", (0.4, 0.8, 1.2), warning=True)
    np.testing.assert_allclose(f.pixels(frame, "position")[32, 16, 2], -0.35, atol=2e-5)
    set_object(sheet({"materialParameters": {"height": 1e100, "power": 2}}))
    frame = render("Bad height override retains valid authored height", (0.4, 0.8, 1.2), warning=True)
    np.testing.assert_allclose(f.pixels(frame, "position")[32, 16, 2], -0.15, atol=2e-5)
    set_object("")
    frame = render("Displacement override repair")
    np.testing.assert_allclose(f.pixels(frame, "position")[32, 16, 2], -0.15, atol=2e-5)
    a.node("surface_output").setInput(a.node("surface_output").inputIndex("displacement"), None)
    # Packed copies carry the pruned native rule to each prototype's faces.
    f.sop.parmTuple("size").set((2, 4))
    targets = f.obj.createNode("stash", "two_targets")
    g = hou.Geometry()
    for x in (-1, 1):
        pt = g.createPoint()
        pt.setPosition((x, 0, 0))
    pass_fixture.parm(targets, "stash").set(g)
    copies = f.obj.createNode("copytopoints", "two_shared_copies")
    copies.setInput(0, f.sop)
    copies.setInput(1, targets)
    pass_fixture.parm(copies, "pack").set(1)
    source.setInput(0, copies)
    render("Packed copies retain underlying material")
    set_object(sheet({"material": {"name": b.path()}}, dict(group="0")))
    render("Packed owner rule reaches its whole prototype", (0.8, 0.1, 0.2), (0.2, 0.4, 0.6))
    set_object(sheet({"material": {"name": b.path()}}, dict(group="0", subTarget=dict(group="1"))))
    frame = f.update()
    pixels = f.pixels(frame)
    for x, expected in ((8, (0.2, 0.4, 0.6)), (24, (0.8, 0.1, 0.2)), (40, (0.2, 0.4, 0.6)), (56, (0.2, 0.4, 0.6))):
        np.testing.assert_allclose(pixels[32, x, :3], expected, atol=3e-5, rtol=3e-5)
    assert not entry.session.status()["source_diagnostics"]
    report["checks"].append(
        dict(label="Packed sub-target reaches exactly one internal face", status=entry.session.status())
    )
    set_object("")
    render("Packed target repair restores sharing")
    session = entry.session
    session.close(wait=True)
    closed = session.status()
    assert closed["device_allocation_bytes"] == closed["owned_geometry_bytes"] == 0
    # Five frames, including a recoverable bad field and subsequent repair.
    source.setInput(0, f.sop)
    f.sop.parmTuple("size").set((4, 4))
    values = [sheet({"materialParameters": {"power": v, **({"unknown": 1} if v == 3 else {})}}) for v in range(1, 6)]
    pass_fixture.parm(f.obj, "shop_materialstylesheet").setExpression(
        repr(values) + "[max(0,min(4,int(hou.frame())-1))]", hou.exprLanguage.Python
    )
    pass_fixture.set_parameters(f.rop, dict(picture=str(dest / "stylesheet.$F4.exr"), persistent_data=1))
    f.rop.render(frame_range=(1, 5, 1))
    summary = f.rop.cachedUserData(final_render._summary_key)
    assert summary["completed_frames"] == 5 and summary["created_sessions"] == 1 and not summary["session_alive"], (
        summary
    )
    for number in range(1, 6):
        reader = oiio.ImageInput.open(str(dest / f"stylesheet.{number:04}.exr"))
        spec = reader.spec()
        pixels = reader.read_image(format=oiio.FLOAT)
        reader.close()
        issues = json.loads(spec.get_string_attribute("cycles_houdini:source_diagnostics") or "[]")
        assert bool(issues) == (number == 3), issues
        channels = [
            list(spec.channelnames).index(("Combined." if "Combined.R" in spec.channelnames else "") + c) for c in "RGB"
        ]
        np.testing.assert_allclose(pixels[32, 32, channels], np.array((0.2, 0.4, 0.6)) * number, atol=3e-5, rtol=3e-5)
    report.update(result="passed", closed=closed, batch_frames=5, batch=summary)
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    registry = __import__("cycles_houdini.registry", fromlist=["close_all"])
    registry.close_all()
    (dest / "report.json").write_text(json.dumps(report, indent=2))
print(json.dumps(dict(result=report["result"], checks=len(report["checks"]))))
