# SPDX-License-Identifier: Apache-2.0
"""Actual camera shutter optics, local repairs, camera-only retention and five final frames."""

from support.paths import artifacts_root
import argparse
import json
import os
from pathlib import Path
import time
import traceback

import hou
import numpy as np
import OpenImageIO as oiio
from cycles_houdini import camera_properties, final_render, native_scene, registry
from camera_dof_motion_fixture import Fixture


p = argparse.ArgumentParser()
p.add_argument("--device", default="CPU")
p.add_argument("--report", type=Path, default=artifacts_root() / "stage2/cameras/dof-motion/report.json")
args = p.parse_args()
args.report.parent.mkdir(parents=True, exist_ok=True)
f = Fixture(args.device)
entry = registry.for_rop(f.rop, native_scene.configuration(f.rop))
entry.session.close()
entry.session = registry.session_from_configuration(native_scene.configuration(f.rop), collect_diagnostics=True)
report = dict(result="running", device=args.device, checks=[], batch=[])


def save():
    args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")


def render(label, camera_only=False):
    previous = entry.session.status()
    cooks = f.sop.cookCount()
    update = f.update(camera_only)
    optics = update["camera_dof"]
    frame = entry.session.wait(entry.apply(update, hou.time()))
    status = entry.session.status()
    deadline = time.monotonic() + 5
    while status["state"] != "complete" and time.monotonic() < deadline:
        assert status["state"] != "failed", status
        time.sleep(0.005)
        status = entry.session.status()
    event = dict(
        label=label,
        case=f.case,
        offset=f.offset,
        steps=f.steps,
        optics=optics,
        status=status,
        captured=f.capture_info(entry.session),
        cooks_before=cooks,
        cooks_after=f.sop.cookCount(),
    )
    report["attempted"] = event
    save()
    event["image"] = f.inspect(frame)
    assert status["state"] == "complete" and not status["motion_warnings"], event
    assert {issue["code"] for issue in status["source_diagnostics"]} == f.expected_codes(), event
    assert len(optics["motion"]) == event["captured"]["camera_dof_samples"] == f.expected_samples(), event
    if f.rop.evalParm("allowmotionblur"):
        assert (
            event["captured"]["camera_motion_samples"] == f.steps
            and event["captured"]["mesh_motion_samples"] == f.steps - 1
        ), event
    if f.case == "motion_off":
        assert "motion" in frame["planes"], frame.keys()
    if camera_only:
        assert event["cooks_after"] == cooks, event
        assert all(
            status[key] == previous[key] for key in ("geometry_conversions", "extractions", "subdivision_tessellations")
        ), event
        if status["render_revision"] != previous["render_revision"]:
            assert not {"tri_verts", "tri_vindex"} & entry.session.diagnostics()["buffer_copy_requests"].keys(), event
    if report["checks"]:
        assert status["renderer_pid"] == report["checks"][0]["status"]["renderer_pid"], event
    report["checks"].append(event)
    report.pop("attempted", None)
    save()
    return event


try:
    render("Focus pull with independent geometry/camera motion")
    f.camera.parm("test_dof_amplitude").set(0.5)
    signature = native_scene.camera_signature(f.update(True), hou.time())
    render("Off-frame-only focus edit with identical nominal focus", True)
    f.camera.parm("test_dof_amplitude").set(1)
    assert signature != native_scene.camera_signature(f.update(True), hou.time()), (
        "Shutter-only focus edit did not enter camera signature"
    )
    render("Identical focus repair restores accepted state", True)
    for case in (
        "offframe_bad",
        "focus",
        "nominal_bad",
        "focus",
        "disabled",
        "ratio_bad",
        "focus",
        "aperture",
        "ratio",
        "rotation",
        "blade_hold",
        "focus",
    ):
        f.set_case(case)
        render("Camera source case " + case, True)
    for offset in (-1, 1):
        f.set_case("focus", offset=offset)
        event = render("Offset requested-frame optics differ from shutter midpoint")
        assert event["optics"]["focus_distance"] == 2.5 and event["optics"]["motion"][1]["focus_distance"] != 2.5, event
    for case in ("ortho", "rolling"):
        f.set_case(case)
        render("Projection/exposure case " + case)
    for steps in (5, 129):
        f.set_case("focus", steps=steps)
        # A shutter sample-count edit legitimately recaptures deformation.
        render("Owned chronological sample count " + str(steps))
    f.set_case("motion_off")
    render("Motion vector pass uses requested-frame DOF without shutter optics")
    registry.close_all()
    closed = entry.session.status()
    assert closed["owned_geometry_bytes"] == closed["device_allocation_bytes"] == 0, closed
    report["closed"] = closed

    # Ordered final frames include an invalid requested lens and two repairs.
    # The exact same source recovery must be embedded into each written image.
    f.set_case("focus")
    f.camera.parm("cycles_focus_distance").setExpression(
        "float('nan') if int(hou.frame()+.5)==3 else 2.5+96*(hou.time()-(int(hou.frame()+.5)-1)/24)",
        hou.exprLanguage.Python,
    )
    f.rop.setParms(dict(persistent_data=1, picture=str(args.report.parent / "dof.$F4.exr")))
    f.rop.render(frame_range=(1, 5, 1))
    summary = f.rop.cachedUserData(final_render._summary_key)
    assert summary["completed_frames"] == 5 and summary["created_sessions"] == 1 and not summary["session_alive"], (
        summary
    )
    for number in range(1, 6):
        hou.setFrame(number)
        f.case = "nominal_bad" if number == 3 else "focus"
        path = args.report.parent / f"dof.{number:04d}.exr"
        image = oiio.ImageBuf(str(path))
        names = list(image.spec().channelnames)
        pixels = np.flipud(image.get_pixels()[:, :, [names.index(c) for c in ("R", "G", "B")]])
        delta = float(np.abs(pixels - f.reference()).mean())
        issues = json.loads(image.spec().get_string_attribute("cycles_houdini:source_diagnostics") or "[]")
        event = dict(frame=number, path=str(path), mean_difference=delta, diagnostics=issues)
        report["batch"].append(event)
        save()
        assert delta < 0.0025 and {issue["code"] for issue in issues} == f.expected_codes(), event
    report.update(result="passed", final=summary)
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    registry.close_all()
    save()
print(json.dumps(dict(result=report["result"], checks=len(report["checks"]), frames=len(report["batch"]))))
