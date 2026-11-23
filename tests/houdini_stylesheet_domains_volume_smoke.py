# SPDX-License-Identifier: Apache-2.0
"""Scoped shading controls preserve real volume density and independent surfaces."""

import argparse, json, math, traceback
import hou, numpy as np
import cycles_houdini as ch
from cycles_houdini import native_scene, registry
from volume_field_recovery_fixture import fixture

p = argparse.ArgumentParser()
p.add_argument("--device", default="CPU")
device = p.parse_args().device
rop, source, dest = fixture(device)
dest = dest.parent / "stylesheet-domains-volumes"
dest.mkdir(exist_ok=True)
material = hou.node("/mat/volume_absorption")
ch.promote_parameter(material, material.node("density_scale"), "value2", name="density_gain")
sop = source.parent().createNode("attribwrangle", "volume_styles")
sop.setInput(0, source)
sop.parm("class").set(1)
sop.addSpareParmTuple(hou.StringParmTemplate("rule", "Stylesheet", 1))
sop.parm("snippet").set('s@material_stylesheet=chs("rule");')
source.parent().node("OUT").setInput(0, sop)
entry = registry.for_rop(rop, native_scene.configuration(rop))
report = dict(result="running", device=device, checks=[])


def render(label, gain, fields, warning=False):
    sop.parm("rule").set(json.dumps({"styles": [{"overrides": fields}]}) if fields else "")
    frame = entry.session.wait(entry.apply(native_scene.capture(rop), hou.time()))
    status = entry.session.status()
    pixels = np.frombuffer(frame["planes"]["combined"]["pixels"], np.float32).reshape(48, 80, 4)
    actual = pixels[21:27, 37:43, :3].mean((0, 1))
    np.testing.assert_allclose(actual, math.exp(-1.125 * gain), atol=0.04, rtol=0)
    np.testing.assert_allclose(pixels[24, 70, :3], (0.1, 0.7, 0.2), atol=2e-5, rtol=0)
    assert status["state"] == "complete" and not status["error"] and bool(status["source_diagnostics"]) == warning, (
        status
    )
    if report["checks"]:
        assert status["renderer_pid"] == report["checks"][0]["status"]["renderer_pid"]
    assert material.evalParm("density_gain") == 1, "Capture edited an authored parameter"
    report["checks"].append(dict(label=label, transmittance=actual.tolist(), status=status))


try:
    render("Authored density", 1, {})
    render("Surface category controls volume shading", 2, {"surfaceParameters": {"density_gain": 2}})
    render("Displacement category does not alter density", 1, {"displaceParameters": {"density_gain": 3}})
    render(
        "Surface category overrides generic density",
        0.5,
        {
            "materialParameters": {"density_gain": 2},
            "surfaceParameters": {"density_gain": 0.5},
            "displaceParameters": {"density_gain": 3},
        },
    )
    render(
        "Bad surface control retains generic density",
        2,
        {"materialParameters": {"density_gain": 2}, "surfaceParameters": {"density_gain": "bad"}},
        True,
    )
    render(
        "Surface control repairs during IPR",
        0.5,
        {"materialParameters": {"density_gain": 2}, "surfaceParameters": {"density_gain": 0.5}},
    )
    render(
        "Invalid displacement override is diagnosed without altering volume",
        1,
        {"displaceParameters": {"density_gain": "bad"}},
        True,
    )
    render(
        "Unknown scoped control reports omission without changing density",
        1,
        {"displaceParameters": {"unknown": 3}},
        True,
    )
    render("Remove rules restores volume", 1, {})
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
