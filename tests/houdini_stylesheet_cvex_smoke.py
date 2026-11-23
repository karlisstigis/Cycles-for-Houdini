# SPDX-License-Identifier: Apache-2.0
"""CVEX rule failures stay local and repair without dropping geometry."""

import pass_fixtures as pass_fixture

import argparse, json, traceback, subprocess, os
from pathlib import Path
import hou, numpy as np
import cycles_houdini as ch
from cycles_houdini import native_scene, final_render
import OpenImageIO as oiio
from light_shader_fixture import Fixture

p = argparse.ArgumentParser()
p.add_argument("--device", default="CPU")
f = Fixture(p.parse_args().device)
pass_fixture.set_parameters(f.rop, dict(use_material_assignments=1, samples=1, ipr_samples=1, pass_position=1))
pass_fixture.parm(f.light, "light_intensity").set(0)
pass_fixture.set_parameters(f.sop, dict(cols=3, rows=2))
f.sop.parmTuple("size").set((4, 4))
mat = ch.create_material(name="stylesheet_cvex", shader="emission")
shader = mat.node("surface_shader")
shader.parmTuple("color").set((0.2, 0.4, 0.6))
pass_fixture.parm(shader, "strength").set(1)
for parm, name in (("strength", "power"), ("color", "tint")):
    ch.promote_parameter(mat, shader, parm, name=name)
pass_fixture.parm(f.obj, "shop_materialpath").set(mat.path())
f.obj.addSpareParmTuple(hou.StringParmTemplate("shop_materialstylesheet", "Stylesheet", 1))
source = f.obj.createNode("python", "cvex_data")
source.setInput(0, f.sop)
source.addSpareParmTuple(hou.IntParmTemplate("data_kind", "Data Kind", 1, default_value=(1,)))
pass_fixture.parm(source, "python").set("""node=hou.pwd();g=node.geometry();kind=node.evalParm('data_kind')
if kind:
 default=0 if kind==2 else (0.,0.,0.) if kind==3 else '' if kind==5 else 0.
 elements=g.prims() or g.points();a=g.addAttrib(hou.attribType.Prim if g.prims() else hou.attribType.Point,'gain',default)
 for i,p in enumerate(elements):
  value=int(i+2) if kind==2 else (i+2.,i+3.,i+4.) if kind==3 else str(i+2) if kind==5 else float('nan') if kind==4 and i==0 else float(i+2)
  p.setAttribValue(a,value)
""")
source.setDisplayFlag(True)
source.setRenderFlag(True)
control = pass_fixture.parm(f.obj, "shop_materialstylesheet")
entry = f.session()
dest = f.dest.parent / "stylesheet-cvex"
dest.mkdir(exist_ok=True)
report = dict(result="running", device=f.device, checks=[])


def script(code, bindings=None, returned="result"):
    value = {"code": code}
    if bindings is not None:
        value["bindings"] = bindings
    if returned is not None:
        value["return"] = returned
    return {"script": value}


def sheet(fields, target=None):
    return json.dumps({"styles": [{"target": target or {}, "overrides": {"materialParameters": fields}}]})


code = "cvex lookup(float gain=1; export float result=0) { result=gain*2; }"
bound = script(code, {"gain": "gain"})


def render(label, left=1, right=None, warning=False, at=None):
    print("BEGIN " + label, flush=True)
    (dest / "report.json").write_text(json.dumps(report, indent=2))
    frame = (
        f.update() if at is None else entry.session.wait(entry.apply(native_scene.capture(f.rop, at), at), timeout=60)
    )
    status = entry.session.status()
    pixels = f.pixels(frame)
    report["current"] = dict(
        label=label,
        status=status,
        references=entry.session.native_material_references(),
        pixels=[pixels[32, x, :3].tolist() for x in (16, 48)],
    )
    (dest / "report.json").write_text(json.dumps(report, indent=2))
    for x, value in ((16, left), (48, left if right is None else right)):
        color = np.array((0.2, 0.4, 0.6)) * value if np.isscalar(value) else value
        np.testing.assert_allclose(pixels[32, x, :3], color, atol=3e-5, rtol=3e-5)
    assert bool(status["source_diagnostics"]) == warning, status["source_diagnostics"]
    assert status["state"] == "complete" and not status["error"], status
    if report["checks"]:
        assert status["renderer_pid"] == report["checks"][0]["status"]["renderer_pid"]
    report["checks"].append(dict(label=label, status=status, references=entry.session.native_material_references()))
    return frame


try:
    render("Underlying material")
    control.set(sheet({"power": script("cvex constant(export float result=0){ result=3; }")}))
    render("Inline constant", 3)
    control.set(sheet({"power": bound}))
    render("Primitive float binding", 4, 6)
    control.set(sheet({"power": script(code, {"gain": "missing"})}))
    render("Missing explicit binding keeps authored value", warning=True)
    control.set(sheet({"power": bound}))
    render("Binding repair", 4, 6)
    pass_fixture.parm(source, "data_kind").set(4)
    render("Non-finite input stays local", 1, 6, True)
    pass_fixture.parm(source, "data_kind").set(1)
    render("Input repaired", 4, 6)
    pass_fixture.parm(source, "data_kind").set(3)
    render("Incompatible binding type retains material", warning=True)
    pass_fixture.parm(source, "data_kind").set(1)
    render("Binding type repaired", 4, 6)
    control.set(sheet({"power": script("cvex broken export float result=0) {result=3;}")}))
    render("Compilation failure keeps geometry", warning=True)
    control.set(sheet({"power": script("cvex constant(export float result=0){ result=3; }", returned="missing")}))
    render("Missing export keeps material", warning=True)
    control.set(sheet({"power": {"script": {"binding": "missing"}}}))
    render("Missing binding shortcut keeps material", warning=True)
    control.set(sheet({"power": {"script": {"binding": "gain"}}}))
    render("Binding shortcut repaired", 2, 3)
    control.set(sheet({"power": script(code, {"gain": {"value": 4}})}))
    render("Malformed constant binding keeps authored value", warning=True)
    control.set(sheet({"power": script(code, {"gain": {"sourceType": "constant", "value": 4.0}})}))
    render("Constant script binding", 8)
    control.set(
        sheet(
            {"power": script("cvex lookup(float ignored=0; export float result=0){result=3;}", {"ignored": "missing"})}
        )
    )
    render("Unused binding stays quiet", 3)
    control.set(
        sheet({"power": script("cvex defaults(float optional_value=5; export float result=0){result=optional_value;}")})
    )
    render("Unauthored optional input retains script default", 5)
    pass_fixture.parm(source, "data_kind").set(2)
    control.set(
        sheet({"power": script("cvex lookup(int gain=1; export float result=0){result=gain*2;}", {"gain": "gain"})})
    )
    render("Integer primitive binding", 4, 6)
    pass_fixture.parm(source, "data_kind").set(5)
    control.set(
        sheet(
            {
                "power": script(
                    'cvex lookup(string gain=""; export float result=0){result=atoi(gain)*2;}', {"gain": "gain"}
                )
            }
        )
    )
    render("String primitive binding", 4, 6)
    pass_fixture.parm(source, "data_kind").set(3)
    control.set(
        sheet({"tint": script("cvex lookup(vector gain=0; export vector result=0){result=gain*.1;}", {"gain": "gain"})})
    )
    render("Vector primitive binding", (0.2, 0.3, 0.4), (0.3, 0.4, 0.5))
    pass_fixture.parm(source, "data_kind").set(1)
    control.set(
        sheet(
            {
                "power": script(
                    "cvex lookup(float gain=1; export float result=0){result=exp(gain*1000);}", {"gain": "gain"}
                )
            }
        )
    )
    render("Overflow result retains authored value", warning=True)
    control.set(sheet({"power": script("cvex lookup(export float one=2; export float two=3){}", returned=None)}))
    render("Ambiguous exported result retains material", warning=True)
    control.set(sheet({"power": script("cvex lookup(export float one=2; export float two=3){}", returned="two")}))
    render("Explicit return selects exported result", 3)
    control.set(
        sheet(
            {"power": script(code, {"gain": {"sourceType": "preBinding", "name": "pre_gain"}})},
            dict(preBindings={"pre_gain": "gain"}),
        )
    )
    render("Native explicit target prebinding", 4, 6)
    control.set(
        json.dumps(
            dict(
                scriptDefinitions={"shared_power": bound["script"]},
                styles=[
                    dict(
                        target={},
                        overrides=dict(materialParameters={"power": {"script": {"importScript": "shared_power"}}}),
                    )
                ],
            )
        )
    )
    render("Shared script retains native bindings", 4, 6)
    # A broken high-priority field must expose a valid lower native rule.
    rules = [
        {"overrides": {"materialParameters": {"power": script(code, {"gain": "missing"})}}},
        {"overrides": {"materialParameters": {"power": 3}}},
    ]
    control.set(json.dumps({"styles": rules}))
    render("Missing high-priority binding retains lower rule", 3, warning=True)
    rules[0]["overrides"]["materialParameters"]["power"] = script("cvex broken export float result=0) {result=3;}")
    control.set(json.dumps({"styles": rules}))
    render("Compilation failure retains lower rule", 3, warning=True)
    control.set(json.dumps({"styles": list(reversed(rules))}))
    render("Unused broken lower rule is quiet", 3)
    control.set(sheet({"power": bound, "tint": [0.1, 0.2, 0.3]}))
    render("Independent valid fields", (0.4, 0.8, 1.2), (0.6, 1.2, 1.8))
    control.set(sheet({"power": script("cvex broken export float result=0){}"), "tint": [0.1, 0.2, 0.3]}))
    render("Broken field keeps independent color", (0.1, 0.2, 0.3), warning=True)
    control.set(sheet({"power": bound}, dict(group="missing_group")))
    pass_fixture.parm(source, "data_kind").set(0)
    render("Unmatched binding rule stays quiet")
    control.set(sheet({"power": script("cvex broken export float result=0){}")}, dict(group="missing_group")))
    render("Unmatched bad code stays quiet")
    pass_fixture.parm(source, "data_kind").set(1)
    control.set("")
    render("Remove stylesheet clears warnings")
    # Failed script inputs/results must never become displacement coordinates.
    displacement = mat.createNode("cycles::displacement::1", "cvex_height")
    pass_fixture.set_parameters(displacement, dict(height=0.15, scale=1, midlevel=0, space="object"))
    mat.node("surface_output").setNamedInput("displacement", displacement, "displacement")
    pass_fixture.parm(mat, "cycles_displacement_method").set("true")
    ch.promote_parameter(mat, displacement, "height", name="height")
    height = script("cvex height(float gain=1; export float result=0){result=gain*.1;}", {"gain": "gain"})
    control.set(sheet({"height": height, "power": 2}))
    frame = render("Valid scripted displacement", 2)
    np.testing.assert_allclose([f.pixels(frame, "position")[32, x, 2] for x in (16, 48)], (-0.2, -0.3), atol=2e-5)
    pass_fixture.parm(source, "data_kind").set(4)
    frame = render("Invalid height input retains authored displacement", 2, warning=True)
    np.testing.assert_allclose([f.pixels(frame, "position")[32, x, 2] for x in (16, 48)], (-0.15, -0.3), atol=2e-5)
    pass_fixture.parm(source, "data_kind").set(1)
    frame = render("Height input repaired", 2)
    np.testing.assert_allclose([f.pixels(frame, "position")[32, x, 2] for x in (16, 48)], (-0.2, -0.3), atol=2e-5)
    control.set(sheet({"height": height, "tint": script("cvex broken export float result=0){}")}))
    frame = render("Broken color script preserves independent displacement", warning=True)
    np.testing.assert_allclose([f.pixels(frame, "position")[32, x, 2] for x in (16, 48)], (-0.2, -0.3), atol=2e-5)
    control.set(sheet({"height": script("cvex bad(export float result=0){result=exp(1000.);}"), "power": 2}))
    frame = render("Non-finite height result retains authored displacement", 2, warning=True)
    np.testing.assert_allclose([f.pixels(frame, "position")[32, x, 2] for x in (16, 48)], (-0.15, -0.15), atol=2e-5)
    control.set("")
    mat.node("surface_output").setInput(mat.node("surface_output").inputIndex("displacement"), None)
    render("Displacement stylesheet removed")
    # Preserve partial native targeting and prebound values through packed owners.
    f.sop.parmTuple("size").set((2, 4))
    targets = f.obj.createNode("stash", "cvex_targets")
    g = hou.Geometry()
    a = g.addAttrib(hou.attribType.Point, "outer_gain", 0.0)
    for i, x in enumerate((-1, 1)):
        pt = g.createPoint()
        pt.setPosition((x, 0, 0))
        pt.setAttribValue(a, float(i + 4))
    pass_fixture.parm(targets, "stash").set(g)
    copies = f.obj.createNode("copytopoints", "cvex_shared_copies")
    copies.setInput(0, source)
    copies.setInput(1, targets)
    pass_fixture.parm(copies, "pack").set(1)
    packed_fields = f.obj.createNode("attribwrangle", "packed_bindings")
    packed_fields.setInput(0, copies)
    pass_fixture.set_parameters(packed_fields, {"class": 1, "snippet": "f@outer_gain=4+@primnum;"})
    source.setDisplayFlag(False)
    source.setRenderFlag(False)
    packed_fields.setDisplayFlag(True)
    packed_fields.setRenderFlag(True)
    control.set(
        sheet(
            {"power": script(code, {"gain": {"sourceType": "preBinding", "name": "outer"}})},
            dict(preBindings={"outer": "outer_gain"}, subTarget={}),
        )
    )
    render("Packed ancestor prebinding reaches shared prototype", 8, 10)
    control.set(sheet({"power": script(code, {"gain": "missing"})}, dict(group="0", subTarget={})))
    render("Packed failed binding keeps both copies", warning=True)
    control.set(
        sheet(
            {"power": script(code, {"gain": {"sourceType": "preBinding", "name": "outer"}})},
            dict(preBindings={"outer": "outer_gain"}, subTarget={}),
        )
    )
    render("Packed ancestor binding repair", 8, 10)
    packed_fields.setRenderFlag(False)
    packed_fields.setDisplayFlag(False)
    source.setDisplayFlag(True)
    source.setRenderFlag(True)
    f.sop.parmTuple("size").set((4, 4))
    control.set("")
    render("Return to unpacked geometry")
    # Ordinary VEX files remain dependencies even while missing or broken.
    program = dest / "external.vex"
    vfl = dest / "external.vfl"

    def compile_program(power):
        vfl.write_text("cvex external(export float result=0){result=" + str(power) + ";}")
        compiled = subprocess.run(
            [str(Path(hou.getenv("HFS")) / "bin/vcc.exe"), "-o", str(program), str(vfl)], capture_output=True, text=True
        )
        assert compiled.returncode == 0, (compiled.stdout, compiled.stderr)

    command = {"script": {"command": '"' + program.as_posix() + '"', "return": "result"}}
    compile_program(3)
    control.set(sheet({"power": command}))
    render("External CVEX program", 3)
    assert "file:" + program.as_posix() in entry.session.native_stylesheet_resources(), (
        entry.session.native_stylesheet_resources()
    )
    program.unlink()
    render("Deleted CVEX program keeps material", warning=True)
    compile_program(4)
    render("External program recreated", 4)
    program.write_bytes(b"invalid compiled program")
    render("Broken external program keeps material", warning=True)
    compile_program(2)
    render("External program repaired", 2)
    control.set("")
    render("Remove program dependency")
    shop = hou.node("/shop").createNode("vopcvex", "stylesheet_cvex_program")
    export = shop.createNode("parameter", "style_result")
    pass_fixture.set_parameters(export, dict(parmname="result", exportparm=1, floatdef=3))
    node_script = {"script": {"node": shop.path(), "return": "result"}}
    control.set(sheet({"power": node_script}))
    render("Native CVEX node program", 3)
    assert "vex:op:" + shop.path() in entry.session.native_stylesheet_resources(), (
        entry.session.native_stylesheet_resources()
    )
    pass_fixture.parm(export, "floatdef").set(4)
    render("CVEX node parameter edit", 4)
    pass_fixture.parm(export, "parmname").set("other_result")
    render("Broken node result keeps material", warning=True)
    pass_fixture.parm(export, "parmname").set("result")
    render("CVEX node result repaired", 4)
    shop.destroy()
    render("Deleted CVEX node keeps material", warning=True)
    shop = hou.node("/shop").createNode("vopcvex", "stylesheet_cvex_program")
    export = shop.createNode("parameter", "style_result")
    pass_fixture.set_parameters(export, dict(parmname="result", exportparm=1, floatdef=2))
    render("Recreated CVEX node repairs material", 2)
    control.set("")
    render("Remove node program dependency")
    f.obj.addSpareParmTuple(hou.FloatParmTemplate("style_gain", "Style Gain", 1))
    gain = pass_fixture.parm(f.obj, "style_gain")
    gain.setExpression("$F")
    channel = script(
        'cvex channel(export float result=0){result=chf("' + gain.path() + '",' + str(hou.frameToTime(3)) + ");}"
    )
    control.set(sheet({"power": channel}))
    render("Explicit CVEX channel time differs from UI frame", 3, at=hou.frameToTime(3))
    assert hou.frame() == 1, hou.frame()
    channel = script('cvex channel(export float result=0){result=chf("' + gain.path() + '",0.);}')
    control.set(sheet({"power": channel}))
    render("Explicit CVEX channel at first frame", 1)
    gain.deleteAllKeyframes()
    gain.set(2)
    render("Channel changes reevaluate cached geometry", 2)
    control.set(sheet({"power": channel}, dict(type="self")))
    render("Object script reads repaired channel", 2, at=hou.frameToTime(4))
    gain.setExpression("$F")
    channel = script(
        'cvex channel(export float result=0){result=chf("' + gain.path() + '",' + str(hou.frameToTime(4)) + ");}"
    )
    control.set(sheet({"power": channel}, dict(type="self")))
    render("Object script samples explicit channel time", 4, at=hou.frameToTime(4))
    control.set("")
    gain.deleteAllKeyframes()
    gain.set(1)
    render("Remove dynamic script")
    shape = f.obj.createNode("stash", "cvex_other_components")
    source.setInput(0, shape)
    control.set(sheet({"power": bound}))
    for curves in (False, True):
        control.set(sheet({"power": bound}, {} if curves else {"type": "point"}))
        g = hou.Geometry()
        size = g.addAttrib(hou.attribType.Point, "width" if curves else "pscale", 0.2)
        for x in (-1, 1):
            points = []
            for y in (-2.0, 2.0) if curves else (0.0,):
                point = g.createPoint()
                point.setPosition((x, y, 0))
                point.setAttribValue(size, 0.2)
                points.append(point)
            if curves:
                line = g.createPolygon(is_closed=False)
                for point in points:
                    line.addVertex(point)
        pass_fixture.parm(shape, "stash").set(g)
        render("Hair primitive binding" if curves else "Particle point binding", 4, 6)
        pass_fixture.parm(source, "data_kind").set(4)
        render("Hair binding failure stays local" if curves else "Particle binding failure stays local", 1, 6, True)
        pass_fixture.parm(source, "data_kind").set(1)
        render("Hair binding repaired" if curves else "Particle binding repaired", 4, 6)
    source.setInput(0, f.sop)
    control.set("")
    render("Restore mesh after point and hair recovery")
    other = ch.create_material(name="cvex_assignment", shader="emission")
    other.node("surface_shader").parmTuple("color").set((0.8, 0.1, 0.2))
    pass_fixture.parm(other.node("surface_shader"), "strength").set(1)

    def material_script(path):
        return script(
            'cvex assign(float gain=0; export string result=""){result=gain<3 ? "'
            + mat.path()
            + '" : "'
            + path
            + '";}',
            {"gain": "gain"},
        )

    control.set(
        json.dumps({"styles": [{"target": {}, "overrides": {"material": {"name": material_script(other.path())}}}]})
    )
    render("Script chooses material per face", (0.2, 0.4, 0.6), (0.8, 0.1, 0.2))
    control.set(
        json.dumps(
            {
                "styles": [
                    {"target": {}, "overrides": {"material": {"name": material_script("/mat/cvex_assignment_repair")}}}
                ]
            }
        )
    )
    render("Missing script-selected material stays local", (0.2, 0.4, 0.6), (1, 0, 1), True)
    repaired = ch.create_material(name="cvex_assignment_repair", shader="emission")
    repaired.node("surface_shader").parmTuple("color").set((0.7, 0.3, 0.1))
    pass_fixture.parm(repaired.node("surface_shader"), "strength").set(1)
    render("Created script-selected material repairs binding", (0.2, 0.4, 0.6), (0.7, 0.3, 0.1))
    control.set(
        json.dumps(
            {
                "styles": [
                    {
                        "target": {},
                        "overrides": {"material": {"name": script('cvex broken export string result=""){}')}},
                    }
                ]
            }
        )
    )
    render("Broken assignment script keeps underlying materials", warning=True)
    control.set("")
    render("Remove scripted material assignment")
    control.set(
        json.dumps(
            {
                "styles": [
                    {
                        "overrides": {
                            "unsupportedParameters": {"ignored": script("cvex broken export float result=0){}")},
                            "materialParameters": {"power": 2},
                        }
                    }
                ]
            }
        )
    )
    render("Unsupported category does not execute broken code", 2, warning=True)
    control.set(sheet({"power": 2}))
    render("Remove unsupported scripted layer", 2)
    control.set(
        json.dumps(
            {
                "styles": [
                    {"overrides": {"material": {"name": script('cvex broken export string result=""){}')}}},
                    {"overrides": {"material": {"name": other.path()}}},
                ]
            }
        )
    )
    render("Bad assignment script exposes lower native material", (0.8, 0.1, 0.2), warning=True)
    control.set("")
    render("Remove assignment recovery rules")
    session = entry.session
    session.close(wait=True)
    closed = session.status()
    assert closed["owned_geometry_bytes"] == closed["device_allocation_bytes"] == 0
    values = [
        sheet({"power": script("cvex frame(export float result=0){result=" + str(i) + ";}")}) for i in range(1, 6)
    ]
    values[2] = sheet({"power": script("cvex broken export float result=0){}")})
    control.setExpression(repr(values) + "[max(0,min(4,int(hou.frame())-1))]", hou.exprLanguage.Python)
    pass_fixture.set_parameters(f.rop, dict(picture=str(dest / "cvex.$F4.exr"), persistent_data=1))
    f.rop.render(frame_range=(1, 5, 1))
    summary = f.rop.cachedUserData(final_render._summary_key)
    assert summary["completed_frames"] == 5 and summary["created_sessions"] == 1 and not summary["session_alive"], (
        summary
    )
    for number in range(1, 6):
        reader = oiio.ImageInput.open(str(dest / f"cvex.{number:04}.exr"))
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
    report.update(result="passed", closed=closed, batch_frames=5, batch=summary)
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    __import__("cycles_houdini.registry", fromlist=["close_all"]).close_all()
    (dest / "report.json").write_text(json.dumps(report, indent=2))
print(json.dumps(dict(result=report["result"], checks=len(report["checks"]))))
