# SPDX-License-Identifier: Apache-2.0
"""Changing heavy mixed curve prototypes remain shared across 100 packed instances."""

import argparse
import json
import time
import traceback
import hou
import numpy as np
from cycles_houdini import native_scene, registry
from parametric_curve_recovery_fixture import Fixture, pixels

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
device = parser.parse_args().device
f = Fixture(device)
report = dict(result="running", device=device, events=[])
# 512 physical copies make a genuinely heavier shared curve prototype: 1,024
# source curves, 41,984 sampled keys and 512 boxes, before outer instancing.
inner = f.obj.createNode("grid", "prototype_targets")
inner.setParms(dict(type=0, rows=16, cols=32))
layout = f.obj.createNode("attribwrangle", "prototype_layout")
layout.setInput(0, inner)
layout.parm("snippet").set("@P=set((@ptnum%32)*.1,(@ptnum/32)*.1,0);")
heavy = f.obj.createNode("copytopoints", "heavy_mixed_prototype")
heavy.setInput(0, f.output)
heavy.setInput(1, layout)
heavy.parm("pack").set(0)
# Native SOPs can change their intermediate curve representation. A final
# deformation wrangle preserves the rational source weights in its output;
# exercise shape/weight edits on that final prototype, not an intermediate.
warped = f.obj.createNode("attribwrangle", "prototype_bend")
warped.setInput(0, heavy)
warped.addSpareParmTuple(hou.FloatParmTemplate("bend", "Bend", 1))
warped.parm("snippet").set('@P.y+=chf("bend")*sin(@P.x*2.0);')
pack = f.obj.createNode("pack", "shared_prototype")
pack.setInput(0, warped)
targets = f.obj.createNode("grid", "instance_targets")
targets.setParms(dict(type=0, rows=10, cols=10))
poses = f.obj.createNode("attribwrangle", "instance_layout")
poses.setInput(0, targets)
poses.addSpareParmTuple(hou.FloatParmTemplate("move_one", "Move One", 1))
poses.parm("snippet").set('@P=set((@ptnum%10)*6,(@ptnum/10)*4,0); if(@ptnum==0)@P.x+=chf("move_one");')
copies = f.obj.createNode("copytopoints", "packed_instances")
copies.setInput(0, pack)
copies.setInput(1, poses)
copies.parm("pack").set(1)
copies.setDisplayFlag(True)
copies.setRenderFlag(True)
f.camera.setParms(dict(tx=28.6, ty=18.7, tz=50, orthowidth=64))
f.rop.setParms(dict(resx=240, resy=160, samples=4, ipr_samples=4))
entry = registry.for_rop(f.rop, native_scene.configuration(f.rop))
last_image = None


def render(label, curves, broken=False):
    global last_image
    began = time.monotonic()
    frame = entry.session.wait(entry.apply(native_scene.capture(f.rop), hou.time()))
    status = entry.session.status()
    image = pixels(frame)
    assert status["state"] != "failed" and status["curves"] == curves and status["points"] == 0, status
    # One explicit source container inside each of 100 outer packed copies.
    assert (
        status["packed_instances"] == 200 and status["geometry_resources"] == 2 and status["object_resources"] == 200
    ), status
    assert np.isfinite(image).all() and image.max() > 0.3
    last_image = image
    assert {d["code"] for d in status["source_diagnostics"]} == ({"geometry.curve_unavailable"} if broken else set()), (
        status
    )
    assert len(status["source_diagnostics"]) == (1 if broken else 0), status
    report["events"].append(
        dict(
            label=label, status=status, diagnostics=entry.session.diagnostics(), total_seconds=time.monotonic() - began
        )
    )
    return status


try:
    before = render("Heavy shared prototype", 102400)
    cooks = heavy.cookCount()
    for x in (28.65, 28.55, 28.6):
        f.camera.parm("tx").set(x)
        after = render("Camera " + str(x), 102400)
        assert after["geometry_conversions"] == before["geometry_conversions"] and heavy.cookCount() == cooks, after
    for x in (0.5, -0.5, 0):
        poses.parm("move_one").set(x)
        after = render("One instance " + str(x), 102400)
        assert after["geometry_conversions"] == before["geometry_conversions"] and heavy.cookCount() == cooks, after
    baseline = last_image
    for bend in (0.35, 0, -0.35, 0):
        warped.parm("bend").set(bend)
        render("Prototype bend " + str(bend), 102400)
        if bend:
            assert np.max(np.abs(last_image - baseline)) > 0.02
        else:
            np.testing.assert_allclose(last_image, baseline, atol=1e-5, rtol=0)
    for mode in (8, 0, 7, 0, 1, 0, 9, 0):
        f.control.parm("mode").set(mode)
        current = render(
            "Prototype mode " + str(mode), 0 if mode == 9 else 51200 if mode in (1, 7) else 102400, mode in (1, 7, 9)
        )
        if mode == 1:
            for x, label in ((28.65, "Broken cached camera preview"), (28.6, "Broken cached camera return")):
                f.camera.parm("tx").set(x)
                after = render(label, 51200, True)
                assert after["geometry_conversions"] == current["geometry_conversions"], after
                assert after["source_diagnostics"] == current["source_diagnostics"], after
    session = entry.session
    registry.close_all()
    deadline = time.monotonic() + 20
    while session.status()["state"] != "closed" and time.monotonic() < deadline:
        time.sleep(0.01)
    report["closed"] = session.status()
    assert report["closed"]["owned_geometry_bytes"] == report["closed"]["device_allocation_bytes"] == 0, report[
        "closed"
    ]
    report.update(
        result="passed",
        source_curves=1024,
        sampled_keys=41984,
        instances=100,
        nested_containers=200,
        rational_curves_per_prototype=512,
        scope="Shared ownership, transforms and repair; timings are observations, not production latency certification",
    )
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    registry.close_all()
    (f.dest / "packed-report.json").write_text(json.dumps(report, indent=2))
print(json.dumps(dict(result=report["result"], events=len(report["events"]))))
