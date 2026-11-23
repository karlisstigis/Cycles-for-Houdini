# SPDX-License-Identifier: Apache-2.0
"""Break/repair native curves and fields while one persistent session stays alive."""

import argparse
import json
import time
import traceback
import hou
import numpy as np
import OpenImageIO as oiio
from cycles_houdini import native_scene, registry, final_render, object_properties
from parametric_curve_recovery_fixture import Fixture, pixels

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
device = parser.parse_args().device
f = Fixture(device)
report = dict(result="running", device=device, events=[])
entry = registry.for_rop(f.rop, native_scene.configuration(f.rop))


def render(label, curves, code=None):
    frame = entry.session.wait(entry.apply(native_scene.capture(f.rop), hou.time()))
    status = entry.session.status()
    image = pixels(frame)
    assert status["state"] != "failed" and status["curves"] == curves and status["points"] == 0, status
    assert np.isfinite(image).all()
    np.testing.assert_allclose(image[24, 71], (0.1, 0.7, 0.2), atol=1e-4, rtol=0)
    codes = {d["code"] for d in status["source_diagnostics"]}
    if code:
        accepted = {code}
        if code == "geometry.invalid_color":
            accepted.add("material.missing_color")
        if code == "geometry.invalid_uv":
            accepted.add("material.missing_uv")
        field = {"geometry.invalid_color": "Cd", "geometry.invalid_uv": "uv"}.get(code, code)
        assert accepted & codes or any(
            d["code"] == "material.missing_attribute" and d["attribute"] == field for d in status["source_diagnostics"]
        ), status
    else:
        assert not codes, status
    if report["events"]:
        assert status["renderer_pid"] == report["events"][0]["status"]["renderer_pid"]
    report["events"].append(dict(label=label, status=status))
    return image, status


try:
    baseline, initial = render("Initial mixed SOP", 2)
    for mode, code, root, count in (
        (1, "geometry.curve_unavailable", "Cd", 1),
        (2, "geometry.invalid_width", "Cd", 2),
        (3, "geometry.invalid_color", "Cd", 2),
        (4, "signal", "signal", 2),
        (5, "geometry.invalid_uv", "uv", 2),
        (7, "geometry.curve_unavailable", "Cd", 1),
        (9, "geometry.curve_unavailable", "Cd", 0),
        (10, "geometry.curve_unavailable", "Cd", 1),
    ):
        f.reader.parm("attribute").set(root)
        f.control.parm("mode").set(mode)
        render("Broken " + str(mode), count, code)
        f.control.parm("mode").set(0)
        f.reader.parm("attribute").set("Cd")
        repaired, status = render("Repair " + str(mode), 2)
        assert np.abs(repaired - baseline).mean() < 0.0005
    # Pw changes alter geometry without changing P. It must invalidate the
    # rational curve resource, while the retained mesh keeps rendering.
    f.control.parm("mode").set(8)
    changed, status = render("Weights only", 2)
    assert np.abs(changed - baseline).mean() > 0.0002
    f.control.parm("mode").set(0)
    render("Restore weights", 2)
    f.control.parm("mode").set(6)
    unblurred, status = render("Unused broken velocity", 2)
    assert not status["motion_warnings"] and np.abs(unblurred - baseline).mean() < 0.0005
    f.rop.parm("allowmotionblur").set(1)
    _, status = render("Broken active velocity", 2, "motion.unavailable")
    assert status["motion_warnings"], status
    f.control.parm("mode").set(0)
    _, status = render("Velocity repaired", 2)
    assert not status["motion_warnings"], status
    f.rop.parm("allowmotionblur").set(0)
    # Global sampling and an opt-in object override both reach conversion.
    for value in (4, 32):
        f.rop.parm("curve_segments").set(value)
        render("ROP sampling " + str(value), 2)
    object_properties.install(f.obj)
    f.obj.setParms(dict(cycles_curve_segments_override=1, cycles_curve_segments=8))
    payload = native_scene.capture(f.rop)
    assert payload["geometry"][0]["curve_segments"] == 8, payload["geometry"][0]
    render("Object sampling override", 2)
    f.obj.parm("cycles_curve_segments_override").set(0)
    f.rop.parm("curve_segments").set(16)
    _, before = render("Inherited sampling restored", 2)
    for x in (0.02, -0.02, 0):
        f.camera.parm("tx").set(x)
        _, status = render("Camera " + str(x), 2)
        assert status["geometry_conversions"] == before["geometry_conversions"], status
    session = entry.session
    registry.close_all()
    deadline = time.monotonic() + 15
    while session.status()["state"] != "closed" and time.monotonic() < deadline:
        time.sleep(0.01)
    report["closed"] = session.status()
    assert report["closed"]["owned_geometry_bytes"] == report["closed"]["device_allocation_bytes"] == 0, report[
        "closed"
    ]
    f.control.parm("mode").setExpression("(0,7,0,1,0)[max(0,min(4,int(hou.frame()+.5)-1))]", hou.exprLanguage.Python)
    f.rop.setParms(dict(persistent_data=1, picture=str(f.dest / "curve-recovery.$F4.exr")))
    f.rop.render(frame_range=(1, 5, 1))
    summary = f.rop.cachedUserData(final_render._summary_key)
    assert summary["completed_frames"] == 5 and summary["created_sessions"] == 1 and not summary["session_alive"], (
        summary
    )
    report["sequence"] = []
    for frame in range(1, 6):
        image = oiio.ImageBuf(str(f.dest / f"curve-recovery.{frame:04d}.exr"))
        assert not image.has_error, image.geterror()
        data = image.get_pixels()
        assert np.isfinite(data).all()
        np.testing.assert_allclose(data[24, 71, :3], (0.1, 0.7, 0.2), atol=1e-4, rtol=0)
        issues = json.loads(image.spec().get_string_attribute("cycles_houdini:source_diagnostics") or "[]")
        assert bool(issues) == (frame in (2, 4)), issues
        report["sequence"].append(dict(frame=frame, diagnostics=issues))
    report.update(result="passed", final=summary)
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    registry.close_all()
    (f.dest / "recovery-report.json").write_text(json.dumps(report, indent=2))
print(json.dumps(dict(result=report["result"], events=len(report["events"]))))
