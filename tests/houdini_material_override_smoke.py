# SPDX-License-Identifier: Apache-2.0
"""Native Material SOP overrides: independent emission, local recovery and five-frame output."""

import argparse, json, traceback
from pathlib import Path
import hou, numpy as np, OpenImageIO as oiio
import cycles_houdini as ch
from cycles_houdini import native_scene, registry, final_render
from light_shader_fixture import Fixture

p = argparse.ArgumentParser()
p.add_argument("--device", default="CPU")
f = Fixture(p.parse_args().device)
f.rop.setParms(dict(use_material_assignments=1, samples=8, ipr_samples=8))
builder = ch.create_material(name="native_override_emission", shader="emission")
shader = builder.node("surface_shader")
shader.parmTuple("color").set((0.1, 0.2, 0.4))
shader.parm("strength").set(2)
ch.promote_parameter(builder, shader, "color", name="tint")
ch.promote_parameter(builder, shader, "strength", name="power")
material = f.obj.createNode("material", "native_material_sop")
material.setInput(0, f.sop)
material.setParms(
    dict(
        shop_materialpath1=builder.path(),
        num_local1=2,
        local1_name1="tint",
        local1_type1="color",
        local1_name2="power",
        local1_type2="float",
        local1_fval2=3,
    )
)
material.parmTuple("local1_cval1").set((0.2, 0.4, 0.8))
material.setDisplayFlag(True)
material.setRenderFlag(True)
entry = f.session()
dest = f.dest.parent / "material-overrides"
dest.mkdir(exist_ok=True)
report = dict(result="running", device=f.device, checks=[])


def authored():
    return tuple(
        (parm.path(), tuple(key.asCode() for key in parm.keyframes()), repr(parm.eval()))
        for node in (builder, *builder.allSubChildren())
        for parm in node.parms()
    )


original = authored()


def render(label, expected, code=None):
    frame = f.update()
    status = entry.session.status()
    rgb = f.pixels(frame)[31:33, 31:33, :3].mean((0, 1))
    np.testing.assert_allclose(rgb, expected, atol=1e-5, rtol=1e-5)
    assert status["state"] == "complete" and status["render_device_ids"] == [f.device], status
    assert {d["code"] for d in status["source_diagnostics"]} == ({code} if code else set()), status
    assert authored() == original, "Override capture modified artist parameters"
    report["checks"].append(dict(label=label, rgb=rgb.tolist(), status=status))
    return frame


try:
    render("Native color and float overrides reach the renderer", (0.6, 1.2, 2.4))
    pid = entry.session.status()["renderer_pid"]
    assert entry.session.native_material_references()[0]["overrides"]
    material.parm("local1_fval2").set(5)
    render("Editing the native local override updates its shader", (1, 2, 4))
    material.parm("local1_name2").set("unrecognized")
    render("Unknown override keeps valid color and base strength", (0.4, 0.8, 1.6), "material.override_ignored")
    material.parm("local1_name2").set("power")
    material.parm("local1_fval2").set(3)
    render("Repair clears the override warning", (0.6, 1.2, 2.4))
    material.setParms(dict(local1_type2="string", local1_sval2="not a number"))
    render("Wrong value type omits only the affected field", (0.4, 0.8, 1.6), "material.override_ignored")
    material.parm("local1_type2").set("float")
    render("Type repair restores the override", (0.6, 1.2, 2.4))
    literal = f.obj.createNode("attribwrangle", "authored_override_literal")
    literal.setInput(0, material)
    literal.parm("class").set(1)
    literal.addSpareParmTuple(hou.StringParmTemplate("payload", "Override Literal", 1))
    literal.parm("snippet").set('s@material_override=chs("payload");')
    literal.setDisplayFlag(True)
    literal.setRenderFlag(True)
    literal.parm("payload").set("{'power':4,'tintr':0.25,'tintg':0.5,'tintb':0.75,}")
    render("Trailing-comma native dictionary syntax is accepted", (1, 2, 3))
    literal.parm("payload").set("{'power':4,'tint':(0.25,0.5,0.75)}")
    render("Whole color tuples lower to owned components", (1, 2, 3))
    literal.parm("payload").set("{'power':1e309,'tintr':0.25}")
    render("Nonfinite field keeps independent overrides", (0.5, 0.4, 0.8), "material.override_ignored")
    sentinel = dest / "must-not-exist.txt"
    assert not sentinel.exists()
    literal.parm("payload").set(
        "{'power':__import__('pathlib').Path(" + repr(str(sentinel)) + ").write_text('executed')}"
    )
    render("Override literals never execute embedded code", (0.2, 0.4, 0.8), "material.override_ignored")
    assert not sentinel.exists()
    literal.parm("payload").set("{'power':4}")
    render("Literal repair clears diagnostics", (0.4, 0.8, 1.6))
    literal.parm("payload").set("{'power':7}")
    render("Scalar override has an independent reference", (0.7, 1.4, 2.8))
    shader.parm("strength").setExpression('ch("../power")*2', hou.exprLanguage.Hscript)
    original = authored()
    render("Contributing Hscript arithmetic evaluates the promoted override", (1.4, 2.8, 5.6))
    shader.parm("strength").setExpression('hou.parm("../power").eval()*2', hou.exprLanguage.Python)
    original = authored()
    render(
        "Contributing Python expressions retain the authored material locally",
        (0.4, 0.8, 1.6),
        "material.override_ignored",
    )
    shader.parm("strength").set(builder.parm("power"), follow_parm_reference=False)
    original = authored()
    render("Repairing the promoted reference restores its override", (0.7, 1.4, 2.8))
    assert entry.session.status()["renderer_pid"] == pid
    literal.destroy()
    material.setDisplayFlag(True)
    material.setRenderFlag(True)
    material.parm("local1_fval2").setExpression("$F", hou.exprLanguage.Hscript)
    material.parm("local1_name2").setExpression(
        "'unrecognized' if int(hou.frame())==3 else 'power'", hou.exprLanguage.Python
    )
    f.rop.parm("picture").set(str(dest / "frame.$F4.exr"))
    f.rop.render(frame_range=(1, 5, 1))
    summary = f.rop.cachedUserData(final_render._summary_key)
    assert summary["completed_frames"] == 5 and summary["created_sessions"] == 1 and not summary["session_alive"], (
        summary
    )
    frames = []
    for index in range(1, 6):
        image = oiio.ImageBuf(str(dest / f"frame.{index:04d}.exr"))
        pixels = image.get_pixels()
        rgb = [list(image.spec().channelnames).index(c) for c in ("R", "G", "B")]
        expected = np.array((0.2, 0.4, 0.8)) * (2 if index == 3 else index)
        np.testing.assert_allclose(pixels[31:33, 31:33][:, :, rgb].mean((0, 1)), expected, atol=1e-5, rtol=1e-5)
        issues = json.loads(image.spec().get_string_attribute("cycles_houdini:source_diagnostics") or "[]")
        assert bool(issues) == (index == 3), issues
        frames.append(dict(frame=index, diagnostics=issues))
    report.update(result="passed", batch=frames, final=summary)
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    registry.close_all()
    (dest / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(dict(result=report["result"], checks=len(report["checks"]))))
