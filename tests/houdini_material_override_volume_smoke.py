# SPDX-License-Identifier: Apache-2.0
"""Material SOP volume overrides retain valid density, nearby surfaces and repair diagnostics."""

import argparse, json, math, traceback
import hou, numpy as np
import cycles_houdini as ch
from cycles_houdini import native_scene, registry
from volume_field_recovery_fixture import fixture

p = argparse.ArgumentParser()
p.add_argument("--device", default="CPU")
device = p.parse_args().device
rop, source, destination = fixture(device)
destination = destination.parent / "material-override-volumes"
destination.mkdir(exist_ok=True)
material = hou.node("/mat/volume_absorption")
gain = material.node("density_scale").parm("value2")
ch.promote_parameter(material, gain.node(), gain.name(), name="density_gain")
sop = source.parent().createNode("material", "volume_local_overrides")
sop.setInput(0, source)
sop.setParms(
    dict(
        shop_materialpath1=material.path(),
        num_local1=1,
        local1_name1="density_gain",
        local1_type1="float",
        local1_fval1=1,
    )
)
source.parent().node("OUT").setInput(0, sop)
entry = registry.for_rop(rop, native_scene.configuration(rop))
report = dict(result="running", device=device, checks=[])


def render(label, gain_value, warning=False):
    frame = entry.session.wait(entry.apply(native_scene.capture(rop), hou.time()))
    status = entry.session.status()
    pixels = np.frombuffer(frame["planes"]["combined"]["pixels"], np.float32).reshape(48, 80, 4)
    actual = pixels[21:27, 37:43, :3].mean((0, 1))
    np.testing.assert_allclose(actual, math.exp(-1.125 * gain_value), atol=0.04, rtol=0)
    np.testing.assert_allclose(pixels[24, 70, :3], (0.1, 0.7, 0.2), atol=2e-5, rtol=0)
    assert status["state"] == "complete" and status["render_device_ids"] == [device], status
    assert {d["code"] for d in status["source_diagnostics"]} == ({"material.override_ignored"} if warning else set()), (
        status
    )
    assert material.evalParm("density_gain") == 1 and gain.eval() == 1, "Capture changed the authored material"
    report["checks"].append(dict(label=label, transmittance=actual.tolist(), status=status))


try:
    render("Native volume override matches independent optical depth", 1)
    sop.parm("local1_fval1").set(2)
    render("Changing volume override changes extinction", 2)
    sop.parm("local1_name1").set("missing")
    render("Unknown volume parameter retains the authored density", 1, True)
    sop.parm("local1_name1").set("density_gain")
    render("Repair restores the intended density and clears warnings", 2)
    sop.setParms(dict(local1_type1="string", local1_sval1="not a number"))
    render("Wrong override type preserves usable volume and surface", 1, True)
    sop.setParms(dict(local1_type1="float", local1_fval1=0.5))
    render("Type repair updates extinction", 0.5)
    before = entry.session.status()["geometry_conversions"]
    entry.session.wait(entry.apply(dict(camera_path=rop.evalParm("camera")), hou.time()))
    assert entry.session.status()["geometry_conversions"] == before
    report.update(result="passed")
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    registry.close_all()
    (destination / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(dict(result=report["result"], checks=len(report["checks"]))))
