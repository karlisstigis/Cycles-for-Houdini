# SPDX-License-Identifier: Apache-2.0
"""SOP-authored scoped shading and sampled height match analytic shutter coverage."""

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
configure_render(f.rop, dict(geometry_motion="transform", subdivision=dict(type="none")))
f.output.addSpareParmTuple(hou.StringParmTemplate("scoped_rule", "Stylesheet", 1))
f.output.parm("snippet").set('s@material_stylesheet=chs("scoped_rule");')
control = f.output.parm("scoped_rule")
dest = f.dest.parent / "stylesheet-domains-motion"
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


def render(label, samples, centres, warning=False, retain=False):
    f.rop.parm("motion_samples").set(samples)
    before = entry.session.status()
    frame = entry.session.wait(entry.apply(native_scene.capture(f.rop), hou.time()))
    status = entry.session.status()
    deadline = time.monotonic() + 3
    while status["state"] != "complete" and time.monotonic() < deadline:
        time.sleep(0.005)
        status = entry.session.status()
    pixels = f.pixels(frame)["combined"][:, :, :3]
    expected = motion_pixels(centres) * 2
    difference = float(np.abs(pixels - expected).mean())
    report["checks"].append(dict(label=label, samples=samples, mean_difference=difference, status=status))
    # Match the existing unit-power analytic fixture's Monte Carlo budget.
    assert difference / 2 < 0.0008, (label, difference, status)
    assert status["state"] == "complete" and not status["error"] and bool(status["source_diagnostics"]) == warning, (
        status
    )
    assert not status["motion_warnings"] and status["geometry_resources"] == status["object_resources"] == 1, status
    if len(report["checks"]) > 1:
        assert status["renderer_pid"] == report["checks"][0]["status"]["renderer_pid"]
    if retain:
        assert (
            status["geometry_conversions"] == before["geometry_conversions"]
            and status["displacement_rebuilds"] == before["displacement_rebuilds"]
        ), status


try:
    for mode in ("transform", "velocity", "deformation"):
        f.rop.parm("geometry_motion").set(mode)
        for samples in (3, 5):
            style()
            centres = np.linspace(-0.75, 0.75, samples)
            render(mode + ": scoped height animation and nominal surface value", samples, centres)
            render(mode + ": unchanged scoped shutter retains geometry", samples, centres, retain=True)
            style(True)
            render(
                mode + ": invalid off-frame height keeps authored height on that sample",
                samples,
                np.minimum(centres, 0),
                True,
            )
            render(
                mode + ": cached off-frame recovery retains the warning",
                samples,
                np.minimum(centres, 0),
                True,
                retain=True,
            )
            style()
            render(mode + ": scoped off-frame control repair", samples, centres)
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
