# SPDX-License-Identifier: Apache-2.0
"""Implicit CVEX time, explicit time, local coordinates and watched channels."""

import argparse, json, traceback, subprocess
from pathlib import Path
import hou, numpy as np
import cycles_houdini as ch
from cycles_houdini import native_scene, final_render
from light_shader_fixture import Fixture

p = argparse.ArgumentParser()
p.add_argument("--device", default="CPU")
f = Fixture(p.parse_args().device)
f.rop.setParms(dict(use_material_assignments=1, samples=1, ipr_samples=1))
f.light.parm("light_intensity").set(0)
mat = ch.create_material(name="cvex_time", shader="emission")
mat.node("surface_shader").parmTuple("color").set((0.2, 0.4, 0.6))
mat.node("surface_shader").parm("strength").set(1)
ch.promote_parameter(mat, mat.node("surface_shader"), "strength", name="power")
f.obj.parm("shop_materialpath").set(mat.path())
f.obj.addSpareParmTuple(hou.StringParmTemplate("shop_materialstylesheet", "Stylesheet", 1))
style = f.obj.parm("shop_materialstylesheet")
f.obj.addSpareParmTuple(hou.FloatParmTemplate("gain", "Gain", 1))
f.obj.parm("gain").setExpression("$F")
controls = hou.node("/obj").createNode("null", "cvex_time_channels")
controls.addSpareParmTuple(hou.FloatParmTemplate("gain", "Gain", 1))
gain = controls.parm("gain")
gain.set(2)
source = f.obj.createNode("attribwrangle", "cvex_time_fields")
source.setInput(0, f.sop)
source.setParms({"class": 1, "snippet": "f@gain=3;"})
source.setDisplayFlag(True)
source.setRenderFlag(True)
entry = f.session()
dest = f.dest.parent / "stylesheet-cvex-time"
dest.mkdir(exist_ok=True)
report = dict(result="running", device=f.device, checks=[])


def rule(code, target=None, bindings=None):
    script = {"code": code, "return": "result"}
    if bindings is not None:
        script["bindings"] = bindings
    style.set(
        json.dumps(
            {"styles": [{"target": target or {}, "overrides": {"materialParameters": {"power": {"script": script}}}}]}
        )
    )


def render(label, power, frame=1, warning=False):
    print("BEGIN " + label, flush=True)
    update = native_scene.capture(f.rop, hou.frameToTime(frame))
    image = entry.session.wait(entry.apply(update, hou.frameToTime(frame)))
    status = entry.session.status()
    pixel = f.pixels(image)[32, 32, :3]
    report["checks"].append(
        dict(
            label=label,
            frame=frame,
            pixel=pixel.tolist(),
            status=status,
            resources=entry.session.native_stylesheet_resources(),
        )
    )
    np.testing.assert_allclose(pixel, np.array((0.2, 0.4, 0.6)) * power, atol=3e-5, rtol=3e-5)
    assert status["state"] == "complete" and not status["error"] and bool(status["source_diagnostics"]) == warning, (
        status
    )
    assert hou.frame() == 1 and status["geometry_resources"] == status["object_resources"] == 1, (hou.frame(), status)
    if len(report["checks"]) > 1:
        assert status["renderer_pid"] == report["checks"][0]["status"]["renderer_pid"]
    return status


try:
    render("Underlying material", 1)
    for target in ({}, {"type": "self"}):
        rule('cvex clock(export float result=0){result=chf("' + f.obj.parm("gain").path() + '");}', target)
        for frame in (1, 3, 5, 1):
            render(("Object" if target else "Primitive") + " implicit absolute time", frame, frame)
        rule('cvex clock(export float result=0){result=chf("gain");}', target)
        for frame in (5, 2, 1):
            render(("Object" if target else "Primitive") + " relative channel uses owning OBJ", frame, frame)
    rule(
        'cvex clock(export float result=0){result=chf("'
        + f.obj.parm("gain").path()
        + '",'
        + str(hou.frameToTime(4))
        + ");}"
    )
    for frame in (1, 5):
        render("Explicit time remains explicit", 4, frame)
    rule('cvex clock(export float result=0){result=chf("' + gain.path() + '");}')
    render("Unconnected control node is a runtime dependency", 2)
    assert "op:" + controls.path() in entry.session.native_stylesheet_resources()
    gain.set(4)
    render("Edit external channel with cached geometry", 4)
    gain.setExpression("$F+1")
    render("Animated external channel at requested frame", 5, 4)
    gain.deleteAllKeyframes()
    gain.set(2)
    rule(
        'cvex clock(float gain=1;export float result=0){result=gain*chf("' + controls.parm("gain").path() + '");}',
        bindings={"gain": "gain"},
    )
    render("Channel time and primitive data compose", 6)
    f.sop.setParms(dict(cols=130, rows=130))
    render("Bounded batches cross four thousand subjects", 6)
    f.sop.setParms(dict(cols=2, rows=2))
    render("Retire batched script results", 6)
    rule('cvex clock(export float result=0){result=exp(chf("' + gain.path() + '"));}')
    gain.set(1000)
    render("Non-finite channel result keeps material", 1, warning=True)
    gain.set(0)
    render("Channel repair clears local warning", 1)
    gain.set(1)
    render("Repaired result updates again", np.e)
    rule(
        "cvex selected(float unused=0;export float result=3;export float other=0){other=unused;}",
        bindings={"unused": "missing"},
    )
    render("Binding used only by another export is irrelevant", 3)
    for declaration, expected in (("int", 4), ("vector2", [2, 3]), ("vector4", [2, 3, 4, 5])):
        expression = "4" if declaration == "int" else "set(" + ",".join(map(str, expected)) + ")"
        value = {
            "styles": [
                {
                    "target": {"type": "self"},
                    "overrides": {
                        "materialParameters": {
                            "power": {
                                "script": {
                                    "code": "cvex typed(export "
                                    + declaration
                                    + " result=0){result="
                                    + expression
                                    + ";}",
                                    "return": "result",
                                }
                            }
                        }
                    },
                }
            ]
        }
        owned = ch._native.object_stylesheet(f.obj.path(), [], json.dumps(value), "", hou.time())
        actual = json.loads(owned["json"])["styles"][0]["overrides"]["materialParameters"]["power"]
        if isinstance(actual, dict):
            actual = actual["value"]
        assert actual == expected, (declaration, actual, expected, owned)
    program = dest / "timed.vex"
    vfl = dest / "timed.vfl"
    vfl.write_text('cvex timed(float factor=1;export float result=0){result=factor*chf("gain");}')
    compiled = subprocess.run(
        [str(Path(hou.getenv("HFS")) / "bin/vcc.exe"), "-o", str(program), str(vfl)], capture_output=True, text=True
    )
    assert compiled.returncode == 0, (compiled.stdout, compiled.stderr)
    command = {"script": {"command": '"' + program.as_posix() + '" factor 2', "return": "result"}}
    style.set(json.dumps({"styles": [{"overrides": {"materialParameters": {"power": command}}}]}))
    render("Compiled command retains arguments and local channel time", 6, 3)
    # Final output must use each requested frame even while the UI stays at 1.
    rule('cvex clock(export float result=0){result=chf("' + f.obj.parm("gain").path() + '");}')
    entry.session.close(wait=True)
    closed = entry.session.status()
    assert closed["owned_geometry_bytes"] == closed["device_allocation_bytes"] == 0
    f.rop.setParms(dict(picture=(dest / "time.$F4.exr").as_posix(), persistent_data=1))
    f.rop.render(frame_range=(1, 5, 1))
    result = f.rop.cachedUserData(final_render._summary_key)
    assert result["completed_frames"] == 5 and result["created_sessions"] == 1 and not result["session_alive"], result
    import OpenImageIO as oiio

    for frame in range(1, 6):
        filename = dest / f"time.{frame:04d}.exr"
        image = oiio.ImageInput.open(str(filename))
        assert image, filename
        spec = image.spec()
        pixels = image.read_image(format=oiio.FLOAT)
        image.close()
        channels = [
            list(spec.channelnames).index(("Combined." if "Combined.R" in spec.channelnames else "") + c) for c in "RGB"
        ]
        np.testing.assert_allclose(pixels[32, 32, channels], np.array((0.2, 0.4, 0.6)) * frame, atol=3e-5)
    assert hou.frame() == 1
    report.update(result="passed", frames=5, closed=closed, typed_exports=3)
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    ch.registry.close_all()
    (dest / "report.json").write_text(json.dumps(report, indent=2))
print(json.dumps(dict(result=report["result"], checks=len(report["checks"]), frames=report.get("frames", 0))))
