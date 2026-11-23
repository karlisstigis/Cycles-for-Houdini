# SPDX-License-Identifier: Apache-2.0
"""Native surface/displace categories fork only genuinely different shader values."""

import pass_fixtures as pass_fixture

import argparse, json, traceback
import hou, numpy as np
import OpenImageIO as oiio
import cycles_houdini as ch
from cycles_houdini import registry, native_scene, final_render
from light_shader_fixture import Fixture, write_image

p = argparse.ArgumentParser()
p.add_argument("--device", default="CPU")
f = Fixture(p.parse_args().device)
pass_fixture.set_parameters(f.rop, dict(use_material_assignments=1, samples=1, ipr_samples=1, pass_position=1))
pass_fixture.parm(f.light, "light_intensity").set(0)
pass_fixture.set_parameters(f.sop, dict(cols=3, rows=2))
f.sop.parmTuple("size").set((4, 4))
mat = ch.create_material(name="stylesheet_domains", shader="emission")
shader = mat.node("surface_shader")
shader.parmTuple("color").set((0.2, 0.4, 0.6))
value = mat.createNode("cycles::value::1", "shared_control")
pass_fixture.parm(value, "value").set(1)
ch.promote_parameter(mat, value, "value", name="gain")
shader.setNamedInput("strength", value, "value")
height = mat.createNode("cycles::math::1", "height_scale")
pass_fixture.set_parameters(height, dict(math_type="multiply", value2=0.1))
height.setNamedInput("value1", value, "value")
disp = mat.createNode("cycles::displacement::1", "height")
pass_fixture.set_parameters(disp, dict(scale=1, midlevel=0, space="object"))
disp.setNamedInput("height", height, "value")
mat.node("surface_output").setNamedInput("displacement", disp, "displacement")
pass_fixture.parm(mat, "cycles_displacement_method").set("true")
pass_fixture.parm(f.obj, "shop_materialpath").set(mat.path())
f.obj.addSpareParmTuple(hou.StringParmTemplate("shop_materialstylesheet", "Stylesheet", 1))
control = pass_fixture.parm(f.obj, "shop_materialstylesheet")
entry = f.session()
dest = f.dest.parent / "stylesheet-domains"
dest.mkdir(exist_ok=True)
report = dict(result="running", device=f.device, checks=[])


def sheet(overrides, target=None):
    return json.dumps({"styles": [{"target": {} if target is None else target, "overrides": overrides}]})


def set_fields(material=None, surface=None, displacement=None, target=None):
    values = {
        name: value
        for name, value in (
            ("materialParameters", material),
            ("surfaceParameters", surface),
            ("displaceParameters", displacement),
        )
        if value is not None
    }
    control.set(sheet(values, target) if values else "")


def render(label, power=1, z=-0.1, right=None, warning=False, retain_displacement=False):
    print("BEGIN " + label, flush=True)
    report["pending"] = label
    (dest / "report.json").write_text(json.dumps(report, indent=2))
    before = entry.session.status()
    frame = f.update()
    status = entry.session.status()
    pixels = f.pixels(frame)
    report["current"] = dict(
        label=label,
        status=status,
        references=entry.session.native_material_references(),
        pixels=[pixels[32, x, :3].tolist() for x in (16, 48)],
        z=[float(f.pixels(frame, "position")[32, x, 2]) for x in (16, 48)],
    )
    (dest / "report.json").write_text(json.dumps(report, indent=2))
    for x, gain, distance in ((16, power, z), (48, *(right if right is not None else (power, z)))):
        expected = np.array((0.2, 0.4, 0.6)) * gain if np.isscalar(gain) else gain
        np.testing.assert_allclose(pixels[32, x, :3], expected, atol=3e-5, rtol=3e-5)
        np.testing.assert_allclose(f.pixels(frame, "position")[32, x, 2], distance, atol=3e-5, rtol=3e-5)
    assert bool(status["source_diagnostics"]) == warning, status["source_diagnostics"]
    assert status["state"] == "complete" and not status["error"], status
    if report["checks"]:
        assert status["renderer_pid"] == report["checks"][0]["status"]["renderer_pid"]
    if retain_displacement:
        assert status["displacement_rebuilds"] == before["displacement_rebuilds"], (label, before, status)
    report["checks"].append(dict(label=label, status=status))
    return frame


try:
    render("Authored shared control")
    set_fields(surface={"gain": 2})
    render("Surface value leaves height unchanged", 2, retain_displacement=True)
    set_fields(surface={"gain": 3})
    render("Surface edit retains displacement", 3, retain_displacement=True)
    set_fields(displacement={"gain": 3})
    render("Displacement value leaves surface unchanged", 1, -0.3)
    set_fields(surface={"gain": 2}, displacement={"gain": 3})
    render("One control has independent values", 2, -0.3, retain_displacement=True)
    set_fields(material={"gain": 4})
    render("Material value affects both roots", 4, -0.4)
    set_fields(material={"gain": 4}, surface={"gain": 2})
    render("Surface overrides material value only for shading", 2, -0.4, retain_displacement=True)
    set_fields(material={"gain": 4}, displacement={"gain": 3})
    render("Displacement overrides material value only for height", 4, -0.3)
    set_fields(material={"gain": 4}, surface={"gain": 2}, displacement={"gain": 3})
    render("Three categories retain their own values", 2, -0.3, retain_displacement=True)
    set_fields(material={"gain": 4}, surface={"gain": "bad"}, displacement={"gain": 3})
    render("Invalid surface value retains generic value and height", 4, -0.3, warning=True, retain_displacement=True)
    set_fields(material={"gain": 4}, surface={"gain": 2}, displacement={"gain": "bad"})
    render("Invalid height value retains generic value and surface", 2, -0.4, warning=True)
    set_fields(surface={"gain": 2}, displacement={"gain": 3})
    render("Both scoped fields repaired", 2, -0.3)
    set_fields(surface={"gain": 2}, displacement={"gain": 3}, target={"group": "0"})
    render("Scoped rules are local to the targeted face", 2, -0.3, right=(1, -0.1))
    control.set("")
    render("Remove scoped rules")
    set_fields(surface={"gain": 2}, target={"group": "0"})
    render("Surface palette expansion retains equal height", 2, right=(1, -0.1), retain_displacement=True)
    set_fields(surface={"gain": 4}, target={"group": "1"})
    render("Surface palette swap retains equal height", 1, right=(4, -0.1), retain_displacement=True)
    set_fields(material={"gain": 3}, target={"group": "0"})
    render("Ordinary material palette expansion retains valid ownership", 3, -0.3, right=(1, -0.1))
    control.set("")
    render("Ordinary material palette contraction repairs height")
    # Neutral role values must retain one shared source node, not fork the graph.
    set_fields(surface={"gain": 1}, displacement={"gain": 1})
    render("Equal role values remain shared")
    refs = entry.session.native_material_references()
    graphs = native_scene.resolve_materials(refs, hou.time())
    nodes = graphs[0]["graph"]["nodes"]
    assert len([node for node in nodes if node["definition"] == "cycles/value/1"]) == 1, nodes
    set_fields(surface={"gain": 2}, displacement={"gain": 3})
    render("Different role values fork shared control", 2, -0.3)
    refs = entry.session.native_material_references()
    graphs = native_scene.resolve_materials(refs, hou.time())
    nodes = graphs[0]["graph"]["nodes"]
    assert len([node for node in nodes if node["definition"] == "cycles/value/1"]) == 2, nodes

    def cvex(number):
        return {
            "script": {"code": "cvex scoped(export float result=0){result=" + str(number) + ";}", "return": "result"}
        }

    bad = {"script": {"code": "cvex broken export float result=0){}", "return": "result"}}
    set_fields(surface={"gain": cvex(3)}, displacement={"gain": cvex(4)})
    render("Native CVEX categories remain independent", 3, -0.4)
    set_fields(surface={"gain": bad}, displacement={"gain": cvex(4)})
    render("Broken surface script retains independent height", 1, -0.4, warning=True, retain_displacement=True)
    set_fields(surface={"gain": cvex(3)}, displacement={"gain": bad})
    render("Broken displacement script retains independent surface", 3, -0.1, warning=True)
    set_fields(surface={"gain": cvex(3)}, displacement={"gain": cvex(4)})
    render("Scoped scripts repaired", 3, -0.4)
    set_fields(surface={"unknown": 1, "gain": 2}, displacement={"gain": 3})
    render("Unknown scoped field preserves valid fields", 2, -0.3, warning=True)
    # Literal tuple/component controls can drive two roots differently too.
    color = mat.createNode("cycles::color::1", "shared_vector")
    color.parmTuple("value").set((0.1, 0.2, 0.3))
    offset = ch.promote_parameter(mat, color, "value", name="offset")
    vector = mat.createNode("cycles::vector_displacement::1", "vector_height")
    pass_fixture.set_parameters(vector, dict(space="object", scale=1, midlevel=0))
    vector.setNamedInput("vector", color, "color")
    shader.setInput(shader.inputNames().index("strength"), None)
    pass_fixture.parm(shader, "strength").set(1)
    shader.setNamedInput("color", color, "color")
    mat.node("surface_output").setNamedInput("displacement", vector, "displacement")
    control.set("")
    render("Authored shared vector", (0.1, 0.2, 0.3), 0.3)
    set_fields(surface={"offset": [0.6, 0.2, 0.1]})
    render("Surface tuple leaves vector displacement unchanged", (0.6, 0.2, 0.1), 0.3, retain_displacement=True)
    set_fields(displacement={"offset": [0, 0, 0.5]})
    render("Displacement tuple leaves color unchanged", (0.1, 0.2, 0.3), 0.5)
    set_fields(
        material={"offset": [0.3, 0.4, 0.5]}, surface={"offset": [0.6, 0.2, 0.1]}, displacement={offset[2].name(): 0.7}
    )
    render("Role component overrides generic tuple locally", (0.6, 0.2, 0.1), 0.7)
    set_fields(surface={"offset": [0.6, 0.2, 0.1]}, displacement={"offset": [0, 0]})
    render("Bad height tuple retains authored displacement", (0.6, 0.2, 0.1), 0.3, warning=True)
    # A filename controls one shared texture in both branches. No diagnostic
    # color may leak into the independently valid displacement branch.
    image = mat.createNode("cycles::image_texture::1", "shared_image")
    pass_fixture.set_parameters(image, dict(colorspace="data", interpolation="closest"))
    coordinates = mat.createNode("cycles::texture_coordinate::1", "coordinates")
    image.setNamedInput("vector", coordinates, "generated")
    a = dest / "authored.exr"
    b = dest / "alternate.exr"
    missing = dest / "repaired.exr"
    if missing.exists():
        missing.unlink()
    write_image(a, (0.1, 0.2, 0.3))
    write_image(b, (0.6, 0.4, 0.2))
    pass_fixture.parm(image, "filename").set(str(a))
    ch.promote_parameter(mat, image, "filename", name="image_file")
    shader.setNamedInput("color", image, "color")
    vector.setNamedInput("vector", image, "color")
    control.set("")
    render("Authored shared image", (0.1, 0.2, 0.3), 0.3)
    set_fields(surface={"image_file": b.as_posix()})
    render("Surface filename leaves image displacement unchanged", (0.6, 0.4, 0.2), 0.3, retain_displacement=True)
    set_fields(displacement={"image_file": b.as_posix()})
    render("Displacement filename leaves image color unchanged", (0.1, 0.2, 0.3), 0.2)
    set_fields(surface={"image_file": missing.as_posix()})
    render("Missing surface image preserves valid height", (1, 0, 1), 0.3, warning=True)
    write_image(missing, (0.2, 0.5, 0.7))
    render("Surface image repaired during IPR", (0.2, 0.5, 0.7), 0.3, retain_displacement=True)
    missing.unlink()
    set_fields(displacement={"image_file": missing.as_posix()})
    render("Missing height image disables only displacement", (0.1, 0.2, 0.3), 0, warning=True)
    write_image(missing, (0.2, 0.5, 0.7))
    render("Height image repaired during IPR", (0.1, 0.2, 0.3), 0.7)
    # Restore the scalar graph before packed binding and capacity checks.
    shader.setInput(shader.inputNames().index("color"), None)
    shader.parmTuple("color").set((0.2, 0.4, 0.6))
    shader.setNamedInput("strength", value, "value")
    mat.node("surface_output").setNamedInput("displacement", disp, "displacement")
    control.set("")
    render("Restore scalar graph")
    targets = f.obj.createNode("stash", "two_targets")
    g = hou.Geometry()
    for x in (-1, 1):
        point = g.createPoint()
        point.setPosition((x, 0, 0))
    pass_fixture.parm(targets, "stash").set(g)
    f.sop.parmTuple("size").set((2, 4))
    copies = f.obj.createNode("copytopoints", "scoped_packed_copies")
    copies.setInput(0, f.sop)
    copies.setInput(1, targets)
    pass_fixture.parm(copies, "pack").set(1)
    copies.setDisplayFlag(True)
    copies.setRenderFlag(True)
    set_fields(surface={"gain": 2}, displacement={"gain": 3}, target={"group": "0"})
    render("Packed owner keeps role bindings through the prototype", 2, -0.3, right=(1, -0.1))
    set_fields(surface={"gain": 3}, displacement={"gain": 2})
    render("Both packed copies accept repaired scoped values", 3, -0.2)
    copies.setDisplayFlag(False)
    copies.setRenderFlag(False)
    f.sop.setDisplayFlag(True)
    f.sop.setRenderFlag(True)
    f.sop.parmTuple("size").set((4, 4))
    control.set("")
    render("Return from packed geometry")
    # Forking a long shared chain can exceed the existing graph budget. Omit
    # the scoped fields locally, retaining a valid generic material value.
    chain = value
    for index in range(130):
        node = mat.createNode("cycles::math::1", "shared_chain_" + str(index))
        pass_fixture.set_parameters(node, dict(math_type="add", value2=0))
        node.setNamedInput("value1", chain, "value")
        chain = node
    shader.setNamedInput("strength", chain, "value")
    height.setNamedInput("value1", chain, "value")
    set_fields(material={"gain": 4}, surface={"gain": 2}, displacement={"gain": 3})
    render("Scoped graph capacity preserves generic material values", 4, -0.4, warning=True)
    set_fields(material={"gain": 4}, surface={"gain": 4}, displacement={"gain": 4})
    render("Equal values repair scoped graph capacity", 4, -0.4, retain_displacement=True)
    shader.setNamedInput("strength", value, "value")
    height.setNamedInput("value1", value, "value")
    for child in mat.children():
        if child.name().startswith("shared_chain_"):
            child.destroy()
    control.set("")
    render("Remove capacity fixture")
    writer = mat.createNode("cycles::aov_output::1", "scoped_value_writer")
    pass_fixture.parm(writer, "name").set("ScopedGain")
    writer.setNamedInput("value", value, "value")
    pass_fixture.parm(f.rop, "aov_count").set(1)
    pass_fixture.set_parameters(f.rop, dict(aov_name1="ScopedGain", aov_type1="value"))
    for label, generic, surface, displacement, power, z in (
        ("AOV uses authored shading value", None, None, None, 1, -0.1),
        ("AOV follows surface category only", None, 2, 3, 2, -0.3),
        ("AOV ignores displacement-only value", None, None, 4, 1, -0.4),
        ("AOV retains generic value after surface type failure", 3, "bad", 2, 3, -0.2),
        ("AOV repairs surface value without changing height", 3, 4, 2, 4, -0.2),
    ):
        set_fields(
            material=None if generic is None else {"gain": generic},
            surface=None if surface is None else {"gain": surface},
            displacement=None if displacement is None else {"gain": displacement},
        )
        frame = render(label, power, z, warning=surface == "bad", retain_displacement=label.startswith("AOV repairs"))
        np.testing.assert_allclose(f.pixels(frame, "ScopedGain")[32, 32], power, atol=3e-5)
    assert pass_fixture.parm(mat, "gain").eval() == 1 and pass_fixture.parm(
        value, "value"
    ).getReferencedParm() == pass_fixture.parm(mat, "gain")
    session = entry.session
    session.close(wait=True)
    closed = session.status()
    assert closed["owned_geometry_bytes"] == closed["device_allocation_bytes"] == 0
    rules = [
        sheet({"surfaceParameters": {"gain": 2}, "displaceParameters": {"gain": 1}}),
        sheet({"surfaceParameters": {"gain": 2}, "displaceParameters": {"gain": 3}}),
        sheet({"surfaceParameters": {"gain": "bad"}, "displaceParameters": {"gain": 3}}),
        sheet({"surfaceParameters": {"gain": 4}, "displaceParameters": {"gain": 2}}),
        sheet({"materialParameters": {"gain": 5}}),
    ]
    control.setExpression(repr(rules) + "[max(0,min(4,int(hou.frame())-1))]", hou.exprLanguage.Python)
    pass_fixture.set_parameters(f.rop, dict(picture=str(dest / "domains.$F4.exr"), persistent_data=1))
    f.rop.render(frame_range=(1, 5, 1))
    summary = f.rop.cachedUserData(final_render._summary_key)
    assert summary["completed_frames"] == 5 and summary["created_sessions"] == 1 and not summary["session_alive"], (
        summary
    )
    for number, (power, z) in enumerate(((2, -0.1), (2, -0.3), (1, -0.3), (4, -0.2), (5, -0.5)), 1):
        reader = oiio.ImageInput.open(str(dest / f"domains.{number:04}.exr"))
        spec = reader.spec()
        pixels = reader.read_image(format=oiio.FLOAT)
        reader.close()
        names = list(spec.channelnames)
        rgb = [names.index(("Combined." if "Combined.R" in names else "") + component) for component in "RGB"]
        np.testing.assert_allclose(pixels[32, 32, rgb], np.array((0.2, 0.4, 0.6)) * power, atol=3e-5, rtol=3e-5)
        position = next(name for name in names if "position" in name.lower() and name.rsplit(".", 1)[-1] in ("Z", "B"))
        np.testing.assert_allclose(pixels[32, 32, names.index(position)], z, atol=3e-5, rtol=3e-5)
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
