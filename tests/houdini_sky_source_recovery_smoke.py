# SPDX-License-Identifier: Apache-2.0
"""Sky source faults stay local; repaired expressions and ordered output recover."""

import argparse, json, traceback
import hou, numpy as np, OpenImageIO as oiio
import cycles_houdini as ch
from cycles_houdini import native_scene, registry
from sky_source_recovery_fixture import Fixture, CASES

p = argparse.ArgumentParser()
p.add_argument("--device", default="CPU")
args = p.parse_args()
f = Fixture(args.device)
report = dict(result="running", device=args.device, checks=[])
session = ch.Session(
    device=args.device, width=128, height=64, samples=64, threads=2, execution="worker", collect_diagnostics=True
)


def render(label, broken, baseline=None, control_z=0):
    frame = session.wait(session.apply(native_scene.capture(f.rop), hou.time()), timeout=90)
    status = session.status()
    values = f.check(frame, status, broken, baseline, control_z)
    report["checks"].append(dict(case=f.case, label=label, status=status))
    return values


try:
    for case in CASES:
        f.configure(case)
        baseline = render("Healthy sky source", False)
        pid = session.status()["renderer_pid"]
        conversions = session.status()["geometry_conversions"]
        f.set_broken(True)
        render("Invalid expression remains renderable", True)
        if case.startswith("world_"):
            assert session.status()["geometry_conversions"] == conversions
        render("Warning survives unchanged source capture", True)
        f.objects[0].parm("tz").set(0.1)
        f.camera.parm("tx").set(0.05)
        render("Independent object and camera update through the source fault", True, control_z=0.1)
        assert session.status()["renderer_pid"] == pid
        f.objects[0].parm("tz").set(0)
        f.camera.parm("tx").set(0)
        f.set_broken(False)
        render("Repair restores the original image and clears diagnostics", False, baseline)
    session.close(wait=True)
    closed = session.status()
    assert closed["state"] == "closed" and closed["owned_geometry_bytes"] == closed["device_allocation_bytes"] == 0, (
        closed
    )
    report["closed"] = closed
    # Exercise the real final ROP path; every requested frame must be written.
    f.configure("surface_color")
    f.rop.setParms(dict(persistent_data=1, picture=str(f.dest / "sequence.$F4.exr")))

    def edit(rop, event_type, at_time):
        if event_type == hou.ropRenderEventType.PreFrame:
            f.set_broken(int(round(hou.timeToFrame(at_time))) % 2 == 1)

    f.rop.addRenderEventCallback(edit, run_before_script=True)
    try:
        f.rop.render(frame_range=(1, 5, 1))
    finally:
        f.rop.removeRenderEventCallback(edit)
    report["batch"] = []
    for frame in range(1, 6):
        path = f.dest / f"sequence.{frame:04}.exr"
        image = oiio.ImageInput.open(str(path))
        assert image, path
        spec = image.spec()
        data = image.read_image(format=oiio.FLOAT)
        issues = json.loads(spec.get_string_attribute("cycles_houdini:source_diagnostics") or "[]")
        rgb = [float(data[32, 96, spec.channelnames.index(c)]) for c in ("R", "G", "B")]
        assert bool(issues) == (frame % 2 == 1), issues
        if issues:
            np.testing.assert_allclose(rgb, (1, 0, 1), atol=2e-5, rtol=0)
        assert np.isfinite(data).all()
        image.close()
        report["batch"].append(dict(frame=frame, path=str(path), diagnostics=issues, rgb=rgb))
    report["result"] = "passed"
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    session.close(wait=True)
    registry.close_all()
    (f.dest / "report.json").write_text(json.dumps(report, indent=2))
print(json.dumps(dict(result=report["result"], checks=len(report["checks"]), frames=len(report.get("batch", [])))))
