# SPDX-License-Identifier: Apache-2.0
"""Scene selection, native import priority, source repair and ordered output."""

import pass_fixtures as pass_fixture

import argparse, json, traceback
from pathlib import Path
import hou, numpy as np, OpenImageIO as oiio
import cycles_houdini as ch
from cycles_houdini import final_render, stylesheets
from light_shader_fixture import Fixture

p = argparse.ArgumentParser()
p.add_argument("--device", default="CPU")
f = Fixture(p.parse_args().device)
pass_fixture.set_parameters(f.rop, dict(use_material_assignments=1, samples=1, ipr_samples=1, pass_position=1))
pass_fixture.parm(f.light, "light_intensity").set(0)
pass_fixture.parm(f.sop, "cols").set(3)
f.sop.parmTuple("size").set((4, 4))
mat = ch.create_material(name="style_source_material", shader="emission")
shader = mat.node("surface_shader")
shader.parmTuple("color").set((0.2, 0.4, 0.6))
pass_fixture.parm(shader, "strength").set(1)
ch.promote_parameter(mat, shader, "strength", name="power")
pass_fixture.parm(f.obj, "shop_materialpath").set(mat.path())
for name in ("shop_materialstylesheet", "material_stylesheet", "categories"):
    if not pass_fixture.parm(f.obj, name):
        f.obj.addSpareParmTuple(hou.StringParmTemplate(name, name, 1))
source = f.obj.createNode("python", "source_styles")
source.setInput(0, f.sop)
for name in ("detail_sheet", "primitive_sheet"):
    source.addSpareParmTuple(hou.StringParmTemplate(name, name, 1))
pass_fixture.parm(source, "python").set("""node=hou.pwd();g=node.geometry()
for owner,parm in ((hou.attribType.Global,'detail_sheet'),(hou.attribType.Prim,'primitive_sheet')):
 a=g.addAttrib(owner,'material_stylesheet','');value=node.evalParm(parm)
 if owner==hou.attribType.Global:g.setGlobalAttribValue(a,value)
 elif g.prims():g.prims()[0].setAttribValue(a,value)
""")
source.setDisplayFlag(True)
source.setRenderFlag(True)
dest = f.dest.parent / "stylesheet-sources"
dest.mkdir(exist_ok=True)
entry = f.session()
report = dict(result="running", device=f.device, checks=[])


def sheet(power, target=None):
    return json.dumps({"styles": [dict(target=target or {}, overrides={"materialParameters": {"power": power}})]})


def imported(*paths):
    return json.dumps({"importFiles": [str(path).replace("\\", "/") for path in paths]})


def register(name, power, target=None):
    hou.styles.addStyle(name, "Cycles stylesheet source verification", sheet(power, target))


def render(label, power=1, right=None, warning=False):
    frame = f.update()
    status = entry.session.status()
    pixels = f.pixels(frame)
    for x, value in ((16, power), (48, power if right is None else right)):
        np.testing.assert_allclose(pixels[32, x, :3], np.array((0.2, 0.4, 0.6)) * value, rtol=3e-5, atol=3e-5)
    assert bool(status["source_diagnostics"]) == warning, status["source_diagnostics"]
    assert not status["error"] and status["state"] == "complete", status
    if report["checks"]:
        assert status["renderer_pid"] == report["checks"][0]["status"]["renderer_pid"]
    report["checks"].append(dict(label=label, status=status, resources=entry.session.native_stylesheet_resources()))
    return frame


try:
    parm = pass_fixture.parm(f.rop, "apply_stylesheets")
    assert parm and not parm.parmTemplate().isHidden() and parm.eval() == ""
    register("look_a", 2)
    register("look_b", 3)
    assert "look_a" in parm.menuItems() and "look_b" in parm.menuItems()
    render("Scene styles opt in")
    parm.set("look_a")
    render("Selected scene stylesheet", 2)
    pass_fixture.parm(f.obj, "shop_materialstylesheet").set(sheet(7))
    render("Scene rules precede object rules", 2)
    parm.set("look_b look_a")
    render("Native manager order determines priority", 3)
    hou.styles.reorderStyles(("look_b", "look_a"))
    render("Reordered scene priority", 2)
    parm.set("look_* ^look_a")
    render("Native include/exclude selection", 3)
    register("look_b", 4)
    render("Edit registered sheet without a SOP cook", 4)
    hou.styles.removeStyle("look_b")
    render("Deleted selected style restores object binding", 7)
    register("look_b", 5)
    render("Recreated selected style repairs binding", 5)
    hou.styles.renameStyle("look_b", "look_renamed")
    render("Pattern follows renamed style", 5)
    parm.set("")
    pass_fixture.parm(f.obj, "shop_materialstylesheet").set("")
    render("Disable scene selection")
    file = dest / "object.json"
    nested = dest / "nested.json"
    missing = dest / "missing.json"
    missing.unlink(missing_ok=True)
    file.write_text(sheet(2))
    pass_fixture.parm(f.obj, "shop_materialstylesheet").set(imported(file.resolve()))
    render("Object file import", 2)
    cooks = source.cookCount()
    file.write_text(sheet(3))
    render("File edit invalidates binding cache", 3)
    assert source.cookCount() == cooks
    file.write_text(imported(nested.resolve()))
    nested.write_text(sheet(4))
    render("Nested import", 4)
    nested.write_text("{broken")
    render("Malformed nested import keeps geometry", warning=True)
    nested.write_text(sheet(5))
    render("Nested import repaired", 5)
    nested.unlink()
    render("Missing nested import is reported", warning=True)
    nested.write_text(sheet(2))
    render("Missing nested file created", 2)
    file.write_text(
        json.dumps({"importFiles": [missing.resolve().as_posix()], "styles": json.loads(sheet(3))["styles"]})
    )
    render("Missing import retains valid local rules", 3, warning=True)
    missing.write_text(sheet(6))
    render("Missing import repair restores native imported priority", 6)
    file.write_text(imported("registered_import"))
    register("registered_import", 4)
    render("Registered import", 4)
    register("registered_import", 5)
    render("Registered import update", 5)
    hou.styles.removeStyle("registered_import")
    render("Registered import deleted", warning=True)
    register("registered_import", 2)
    render("Registered import recreated", 2)
    file.write_text(json.dumps({"importFiles": [file.resolve().as_posix()], "styles": json.loads(sheet(3))["styles"]}))
    render("Cycle keeps valid local rules", 3, warning=True)
    file.write_text(sheet(4))
    render("Cycle repair", 4)
    pass_fixture.parm(f.obj, "shop_materialstylesheet").set("")
    pass_fixture.parm(source, "detail_sheet").set(imported(file.resolve()))
    render("Detail attribute imports file", 4)
    file.write_text(sheet(5))
    render("Detail import edit bypasses geometry data-ID cache", 5)
    assert source.cookCount() == cooks + 1
    file.write_text(sheet(5, dict(type="self")))
    pass_fixture.parm(source, "detail_sheet").set("")
    pass_fixture.parm(source, "primitive_sheet").set(imported(file.resolve()))
    render("Primitive import stays local", 5, 1)
    file.write_text("{broken")
    render("Broken primitive import keeps both faces", warning=True)
    file.write_text(sheet(2, dict(type="self")))
    render("Primitive import repair", 2, 1)
    file.write_text(" " * 1048577)
    render("Excessive imported source omitted locally", warning=True)
    file.write_text(sheet(3, dict(type="self")))
    render("Source budget repair", 3, 1)
    pass_fixture.parm(source, "primitive_sheet").set("")
    pass_fixture.parm(f.obj, "shop_materialstylesheet").set(imported(file.resolve()))
    file.write_text(sheet(4))
    pass_fixture.parm(f.obj, "material_stylesheet").set(sheet(2))
    render("Alias retains native priority over object sheet", 2)
    pass_fixture.parm(f.obj, "material_stylesheet").set("")
    render("Alias removal restores import", 4)
    register("look_a", 6, dict(objectName="/obj/not_this_object"))
    parm.set("look_a")
    render("Scene object condition excludes receiver", 4)
    register("look_a", 6, dict(objectName=f.obj.path()))
    render("Scene object condition matches receiver", 6)
    # Requested-frame object evaluation must not depend on Houdini's UI frame.
    parm.set("")
    pass_fixture.parm(f.obj, "shop_materialstylesheet").setExpression(
        'import json\nreturn json.dumps({"styles":[{"overrides":{"materialParameters":{"power":hou.frame()+1}}}]})',
        hou.exprLanguage.Python,
    )
    now = hou.time()
    owned = ch.geometry_request(f.obj, source, material_mode="native", at_time=now + 1 / hou.fps())
    assert "3" in owned["stylesheet"], owned
    pass_fixture.parm(f.obj, "shop_materialstylesheet").deleteAllKeyframes()
    pass_fixture.parm(f.obj, "shop_materialstylesheet").set("")
    # The native object subject preserves HOM path, category and bundle targeting.
    pass_fixture.parm(f.obj, "categories").set("hero,warm")
    for target in ({"objectName": f.obj.path()}, {"objectCategory": "hero"}, {"objectBundle": "stylesheet_heroes"}):
        if "objectBundle" in target:
            hou.addNodeBundle("stylesheet_heroes").addNode(f.obj)
        text = sheet(6, target)
        expected = hou.StyleSheet(text).cloneWithObject(f.obj).asJSON()
        actual = ch._native.object_stylesheet(f.obj.path(), [text], "", "hero,warm")["json"]

        def rules(value):
            return json.loads(value or "{}").get("styles", [])

        assert rules(actual) == rules(expected), (target, actual, expected)
        assert rules(actual), (target, actual)
        negative = sheet(6, {key: "missing" for key in target})
        assert not rules(ch._native.object_stylesheet(f.obj.path(), [negative], "", "hero,warm")["json"]), target
    # Imported parameters must update real geometry as well as surface color.
    displacement = mat.createNode("cycles::displacement::1", "imported_height")
    pass_fixture.set_parameters(displacement, dict(height=0.15, scale=1, midlevel=0, space="object"))
    mat.node("surface_output").setNamedInput("displacement", displacement, "displacement")
    pass_fixture.parm(mat, "cycles_displacement_method").set("true")
    ch.promote_parameter(mat, displacement, "height", name="height")
    pass_fixture.parm(f.obj, "shop_materialstylesheet").set(imported(file.resolve()))

    def height(value, power=2):
        return json.dumps({"styles": [{"overrides": {"materialParameters": {"power": power, "height": value}}}]})

    file.write_text(height(0.25))
    image = render("Imported true displacement", 2)
    np.testing.assert_allclose(f.pixels(image, "position")[32, 16, 2], -0.25, atol=3e-5)
    file.write_text(height(0.4, 3))
    image = render("Imported displacement edit", 3)
    np.testing.assert_allclose(f.pixels(image, "position")[32, 16, 2], -0.4, atol=3e-5)
    file.write_text(height(1e100))
    image = render("Invalid imported height keeps authored displacement", 2, warning=True)
    np.testing.assert_allclose(f.pixels(image, "position")[32, 16, 2], -0.15, atol=3e-5)
    file.write_text("{broken")
    image = render("Broken imported file keeps authored geometry", warning=True)
    np.testing.assert_allclose(f.pixels(image, "position")[32, 16, 2], -0.15, atol=3e-5)
    file.write_text(height(0.3))
    image = render("Imported displacement repaired", 2)
    np.testing.assert_allclose(f.pixels(image, "position")[32, 16, 2], -0.3, atol=3e-5)
    mat.node("surface_output").setInput(mat.node("surface_output").inputIndex("displacement"), None)
    pass_fixture.parm(f.obj, "shop_materialstylesheet").set("")
    # Packed descendants retain source dependencies and prototype sharing.
    f.sop.parmTuple("size").set((2, 4))
    targets = f.obj.createNode("stash", "targets")
    g = hou.Geometry()
    for x in (-1, 1):
        g.createPoint().setPosition((x, 0, 0))
    pass_fixture.parm(targets, "stash").set(g)
    copies = f.obj.createNode("copytopoints", "copies")
    copies.setInput(0, f.sop)
    copies.setInput(1, targets)
    pass_fixture.parm(copies, "pack").set(1)
    source.setInput(0, copies)
    file.write_text(sheet(2))
    pass_fixture.parm(source, "detail_sheet").set(imported(file.resolve()))
    render("Packed imported rules", 2)
    file.write_text(sheet(3))
    render("Packed imported rules edit without recook", 3)
    file.write_text("{broken")
    render("Packed imported rules broken", warning=True)
    file.write_text(sheet(4))
    render("Packed imported rules repaired", 4)
    pass_fixture.parm(source, "detail_sheet").set("")
    render("Remove imported layer")
    assert not entry.session.native_stylesheet_resources(), entry.session.native_stylesheet_resources()
    session = entry.session
    session.close(wait=True)
    closed = session.status()
    assert closed["owned_geometry_bytes"] == closed["device_allocation_bytes"] == 0
    # One process renders five source states without any IPR observer.
    source.setInput(0, f.sop)
    f.sop.parmTuple("size").set((4, 4))
    for frame in range(1, 6):
        (dest / f"frame{frame}.json").write_text("{broken" if frame == 3 else sheet(frame))
    file_pattern = (dest / "frame$F.json").resolve().as_posix()
    pass_fixture.parm(f.obj, "shop_materialstylesheet").set(imported(file_pattern))
    pass_fixture.set_parameters(f.rop, dict(picture=str(dest / "source.$F4.exr"), persistent_data=1))
    f.rop.render(frame_range=(1, 5, 1))
    summary = f.rop.cachedUserData(final_render._summary_key)
    assert summary["completed_frames"] == 5 and summary["created_sessions"] == 1 and not summary["session_alive"], (
        summary
    )
    for number in range(1, 6):
        reader = oiio.ImageInput.open(str(dest / f"source.{number:04}.exr"))
        spec = reader.spec()
        pixels = reader.read_image(format=oiio.FLOAT)
        reader.close()
        issues = json.loads(spec.get_string_attribute("cycles_houdini:source_diagnostics") or "[]")
        assert bool(issues) == (number == 3), issues
        channels = [
            list(spec.channelnames).index(("Combined." if "Combined.R" in spec.channelnames else "") + c) for c in "RGB"
        ]
        np.testing.assert_allclose(
            pixels[32, 32, channels], np.array((0.2, 0.4, 0.6)) * (1 if number == 3 else number), atol=3e-5, rtol=3e-5
        )
    __import__("cycles_houdini.registry", fromlist=["close_all"]).close_all()
    pass_fixture.parm(f.rop, "apply_stylesheets").set("look_* ^look_a")
    saved_path = f.rop.path()
    hip = dest / "stylesheet-sources.hip"
    hou.hipFile.save(str(hip))
    hou.hipFile.load(str(hip), suppress_save_prompt=True, ignore_load_warnings=True)
    saved = hou.node(saved_path)
    assert saved and pass_fixture.evaluate(saved, "apply_stylesheets") == "look_* ^look_a"
    assert stylesheets.selected(saved, hou.time()) == ("look_renamed",), stylesheets.selected(saved, hou.time())
    layout = saved.parmTemplateGroup()
    indices = layout.findIndices("apply_stylesheets")
    assert indices and not layout.find("apply_stylesheets").isHidden()
    assert [layout.entryAtIndices(indices[:depth]).label() for depth in range(1, len(indices))] == [
        "Scene",
        "Objects",
    ], indices
    report.update(
        result="passed",
        closed=closed,
        batch_frames=5,
        batch=summary,
        saved_scene=str(hip),
        object_targeting="HOM parity for path, category and bundle subjects",
    )
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    __import__("cycles_houdini.registry", fromlist=["close_all"]).close_all()
    (dest / "report.json").write_text(json.dumps(report, indent=2))
print(json.dumps(dict(result=report["result"], checks=len(report["checks"]))))
