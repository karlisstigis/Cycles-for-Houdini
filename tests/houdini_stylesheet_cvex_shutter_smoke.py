# SPDX-License-Identifier: Apache-2.0
"""Static SOPs retain geometry while CVEX channels animate shutter displacement."""

from subdivision_test_setup import configure_render, set_subdivision
import argparse, json, time, traceback
import hou, numpy as np
from cycles_houdini import native_scene, registry
from sampled_override_fixture import Fixture
from independent_binding_fixture import motion_pixels

p = argparse.ArgumentParser()
p.add_argument("--device", default="CPU")
device = p.parse_args().device
f = Fixture(device)
f.assigned.parm("num_local1").set(0)
set_subdivision(f.rop, type="none")
f.sop.parm("snippet").set("v@N=set(1,0,0);v@v=set(0,0,0);")
f.obj.addSpareParmTuple(hou.FloatParmTemplate("cvex_height", "Height", 1))
f.obj.parm("cvex_height").setExpression(".5+$T*72")
f.obj.addSpareParmTuple(hou.StringParmTemplate("shop_materialstylesheet", "Stylesheet", 1))
control = f.obj.parm("shop_materialstylesheet")
f.output.addSpareParmTuple(hou.StringParmTemplate("scoped_rule", "Stylesheet", 1))
f.output.parm("snippet").set('s@material_stylesheet=chs("scoped_rule");')
packed = f.obj.createNode("pack", "cvex_time_packed")
packed.setInput(0, f.output)
f.output.setDisplayFlag(True)
f.output.setRenderFlag(True)
dest = f.dest.parent / "stylesheet-cvex-shutter"
dest.mkdir(exist_ok=True)
entry = registry.for_rop(f.rop, native_scene.configuration(f.rop))
report = dict(result="running", device=device, checks=[])


def style(where, broken=False):
    expression = 'chf("' + f.obj.parm("cvex_height").path() + '")'
    if broken:
        expression = "(" + expression + ">.5?exp(1000.):" + expression + ")"
    code = "cvex clock(export float result=0){result=" + expression + ";}"
    entry = {
        "overrides": {
            "surfaceParameters": {"power": 2},
            "displaceParameters": {"height": {"script": {"code": code, "return": "result"}}},
        }
    }
    if where == "object":
        entry["target"] = {"type": "self"}
    value = json.dumps({"styles": [entry]})
    control.set(value if where == "object" else "")
    f.output.parm("scoped_rule").set(value if where != "object" else "")


def render(label, samples, centres, warning=False, retain=False):
    print("BEGIN " + label, flush=True)
    f.rop.parm("motion_samples").set(samples)
    before = entry.session.status()
    cooks = f.sop.cookCount()
    image = entry.session.wait(entry.apply(native_scene.capture(f.rop), hou.time()))
    status = entry.session.status()
    deadline = time.monotonic() + 3
    while status["state"] != "complete" and time.monotonic() < deadline:
        time.sleep(0.005)
        status = entry.session.status()
    delta = float(np.abs(f.pixels(image)["combined"][:, :, :3] - motion_pixels(centres) * 2).mean())
    report["checks"].append(
        dict(
            label=label,
            samples=samples,
            difference=delta,
            status=status,
            cooks=f.sop.cookCount(),
            references=entry.session.native_material_references(),
            row=f.pixels(image)["combined"][32, :, :3].tolist(),
        )
    )
    assert delta / 2 < 0.0008, (label, delta, status)
    assert not status["error"] and status["state"] == "complete" and bool(status["source_diagnostics"]) == warning, (
        status
    )
    assert not status["motion_warnings"] and status["geometry_resources"] == status["object_resources"] == 1, status
    if f.rop.evalParm("geometry_motion") != "deformation":
        assert status["material_motion_cooks"] == 0 and f.sop.cookCount() == cooks, status
    if retain:
        assert (
            status["geometry_conversions"] == before["geometry_conversions"]
            and status["displacement_rebuilds"] == before["displacement_rebuilds"]
        ), status


try:
    for where in ("object", "sop", "packed"):
        (packed if where == "packed" else f.output).setDisplayFlag(True)
        (packed if where == "packed" else f.output).setRenderFlag(True)
        for mode in ("transform", "velocity", "deformation"):
            f.rop.parm("geometry_motion").set(mode)
            for samples in (3, 5):
                style(where)
                f.output.geometry()
                render(where + " " + mode + ": implicit shutter time", samples, np.linspace(-0.75, 0.75, samples))
                render(
                    where + " " + mode + ": unchanged shutter retains geometry",
                    samples,
                    np.linspace(-0.75, 0.75, samples),
                    retain=True,
                )
                style(where, True)
                f.output.geometry()
                render(
                    where + " " + mode + ": bad off-frame result keeps nominal height",
                    samples,
                    np.minimum(np.linspace(-0.75, 0.75, samples), 0),
                    True,
                )
                style(where)
                f.output.geometry()
                render(
                    where + " " + mode + ": result repairs while IPR runs", samples, np.linspace(-0.75, 0.75, samples)
                )
    entry.session.close(wait=True)
    closed = entry.session.status()
    assert closed["owned_geometry_bytes"] == closed["device_allocation_bytes"] == 0
    report.update(result="passed", closed=closed)
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    registry.close_all()
    (dest / "report.json").write_text(json.dumps(report, indent=2))
print(json.dumps(dict(result=report["result"], checks=len(report["checks"]))))
