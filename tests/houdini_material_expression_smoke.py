# SPDX-License-Identifier: Apache-2.0
"""Native numeric/time expressions, sampled dependencies and scoped recovery."""

from support.paths import artifacts_root
import argparse
import json
import time
import traceback
from pathlib import Path
import hou
import numpy as np
import OpenImageIO as oiio
import cycles_houdini as ch
from cycles_houdini import registry, native_scene, final_render
from material_expression_fixture import Fixture, ROOT, COLOR

p = argparse.ArgumentParser()
p.add_argument("--device", default="CPU")
p.add_argument("--report", type=Path)
args = p.parse_args()
dest = artifacts_root() / "stage3/materials/material-expressions"
dest.mkdir(parents=True, exist_ok=True)
path = args.report or dest / "report.json"
path.parent.mkdir(parents=True, exist_ok=True)
report = dict(result="running", scope=__doc__, device=args.device, checks=[], phase="initializing")
f = entry = None


def save():
    temporary = path.with_name(path.name + ".writing")
    temporary.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def render(label, power, z=0.23, *, warning=False, retain=True):
    report["phase"] = label
    save()
    before = entry.session.status()
    authored = f.snapshot()
    frame = f.update()
    status = entry.session.status()
    deadline = time.monotonic() + 3
    while status["state"] != "complete" and time.monotonic() < deadline:
        assert status["state"] != "failed", status
        time.sleep(0.005)
        status = entry.session.status()
    report["current"] = dict(
        label=label,
        status=status,
        expected_rgb=f.rgb(power).tolist(),
        expected_z=z,
        measured_rgb=f.pixels(frame)[31:33, 31:33, :3].mean((0, 1)).tolist(),
        measured_z=f.pixels(frame, "position")[31:33, 31:33, 2].tolist(),
    )
    save()
    assert status["state"] == "complete" and not status["error"] and status["render_device_ids"] == f.target_ids, status
    issues = status["source_diagnostics"]
    assert {d["code"] for d in issues} == ({"material.override_ignored"} if warning else set()), issues
    if warning:
        assert all(d["source"] == f.material.path() and d["message"] and d["action"] for d in issues), issues
    f.assert_pixels(frame, f.rgb(power), z)
    assert f.snapshot() == authored, "Capture changed artist parameters"
    if report["checks"]:
        assert status["renderer_pid"] == report["checks"][0]["status"]["renderer_pid"], status
    if retain and report["checks"]:
        assert status["geometry_conversions"] == before["geometry_conversions"], (before, status)
        assert status["displacement_rebuilds"] == before["displacement_rebuilds"], (before, status)
    report["checks"].append(dict(label=label, status=status, expected_rgb=f.rgb(power).tolist(), expected_z=z))
    save()
    return frame, status


save()
try:
    hou.setFps(24)
    hou.setFrame(1)
    f = Fixture(args.device)
    entry = f.session()
    render("Native Material SOP input reaches fit before the first authored read", 2.4)
    for identifier in (
        "abs_negative",
        "minimum",
        "maximum",
        "clamp_inside",
        "fit_inside",
        "fit_reversed_source",
        "fit_equal_at",
        "efit_above",
        "efit_equal_below",
        "fit01_inside",
        "floor_negative",
        "ceil_negative",
        "int_negative",
        "nested",
    ):
        row = f.reference(identifier)
        hou.setFrame(row["frame"])
        f.expression("(" + row["expression"] + ")+10")
        render("Native HScript reference: " + identifier, row["result"] + 10)
    hou.setFrame(1)
    f.expression('fit(ch("../gain"),0,1,1,5)')
    f.local("gain", "fval").set(0.8)
    f.gain = 0.8
    render("Native numeric input edits update supported functions", 4.2)
    f.expression('sqrt(ch("../gain"))')
    render("A valid authored function outside the allowlist omits only its override", np.sqrt(0.2), warning=True)
    f.camera.parm("tx").set(0.1)
    render("Unsupported-expression warning persists through a cached camera update", np.sqrt(0.2), warning=True)
    f.expression('fit(ch("../gain"),0,1,1,5)')
    render("Repairing the contributing expression restores the override", 4.2)
    f.material.parm("gain").setExpression(".2+$T*10", hou.exprLanguage.Hscript)
    render("Constant native override replaces an animated source at the requested frame", 4.2)
    hou.setFrame(2)
    render("Constant override keeps the animated source fixed on another requested frame", 4.2)
    hou.setFrame(1)
    f.material.parm("gain").deleteAllKeyframes()
    f.material.parm("gain").set(0.2)

    f.sampled()
    render("chf reads an explicit external frame and owned gain", 1.6)
    requested_before = f.source.parm("sampled").evalAtTime(hou.time())
    assert requested_before == 0.2
    f.edit_source(1.2)
    assert f.source.parm("sampled").evalAtTime(hou.time()) == requested_before
    render("An off-frame-only external edit updates the requested image", 2.0)
    f.delete_source()
    render("Missing sampled channel keeps usable shading and independent height", 0.2, warning=True)
    f.camera.parm("tx").set(0.2)
    render("Missing sampled-source warning survives camera reuse", 0.2, warning=True)
    f.make_source()
    render("Recreating the sampled source repairs its warning without another control edit", 2.0)
    f.edit_source(0.8)
    render("Repairing the explicit sample value updates without a render restart", 1.6)
    f.sampled("cht")
    render("cht samples seconds with the pinned Houdini frame-one offset", 1.6)
    f.sampled("chf", True)
    render("A requested-frame relative chf sample uses the active frame", 1.6)
    hou.setFrame(1.25)
    f.expression('10+$F+$FF+$T+ch("../gain")')
    render(
        "Time variables use fractional requested time rather than an integer global frame",
        10 + 1 + 1.25 + 0.25 / 24 + 0.8,
    )
    hou.setFrame(-1.5)
    render("Negative half-frame rounding matches stock Houdini", 10 - 1 - 1.5 - 2.5 / 24 + 0.8)

    # Stylesheet categories use the same evaluator; unsupported surface source
    # expressions must not omit the independently scoped displacement value.
    hou.setFrame(1)
    f.scoped()
    render("Generic surface and displacement categories remain independent", 3.4, 0.28, retain=False)
    f.scoped(0.7)
    render("A surface-category function edit retains scoped height", 3.8, 0.28)
    f.expression('sqrt(ch("../gain"))')
    render("Unsupported surface function retains the independently overridden height", np.sqrt(0.2), 0.28, warning=True)
    f.expression('fit(ch("../gain"),0,1,1,5)')
    render("Repairing the stylesheet expression clears the scoped warning", 3.8, 0.28)
    f.scoped(0.7, 0.8)
    render("Displacement-category function edits change only true height", 3.8, 0.26, retain=False)
    f.style(material={"gain": 0.3}, surface={"gain": "bad"}, displacement={"gain": 0.8})
    render("Invalid scoped literal retains valid generic input and displacement", 2.2, 0.26, warning=True)
    f.scoped(0.7, 0.8)
    render("Repairing the scoped literal restores functions without rebuilding height", 3.8, 0.26)
    f.expression('fit(ch("../gain"),0,1,1,5)')
    f.native(0.8, 0.23)
    render("Returning to native Local Overrides retains supported geometry", 4.2, 0.23, retain=False)
    f.integer_control_fault(True)
    render("Invalid owned integer result omits only its input before strict metadata validation", 1.8, warning=True)
    f.camera.parm("tx").set(0.3)
    render("Integer-result warning persists during camera reuse", 1.8, warning=True)
    f.integer_control_fault(False)
    render("Integer expression repair restores its input and clears the warning", 4.2)
    entry.session.close(wait=True)
    closed = entry.session.status()
    assert closed["owned_geometry_bytes"] == closed["device_allocation_bytes"] == 0, closed
    report["closed"] = closed
    registry.close_all()

    # Render actual shutter histories. These source expressions are unchanged at
    # the requested frame, so only correct off-frame capture changes the image.
    from sampled_override_fixture import Fixture as SampledFixture
    from independent_binding_fixture import motion_pixels

    sampled = SampledFixture(f.target_ids)
    sampled.setup("parameter")
    sampled.rop.setParms(dict(samples=1024, ipr_samples=1024, render_threads=2))
    # The default this test was written for (before the ROP took Blender's defaults).
    sampled.rop.setParms(dict(pixel_filter="box", filter_width=1.0))
    control = sampled.assigned.parm("local1_fval1")
    control.deleteAllKeyframes()
    control.set(0.5)
    temporal = registry.for_rop(sampled.rop, native_scene.configuration(sampled.rop))
    for mode in ("time", "frame", "explicit_frame", "explicit_time"):
        if sampled.material.parm("sampled_rate") is None:
            sampled.material.addSpareParmTuple(
                hou.FloatParmTemplate("sampled_rate", "Sampled Rate", 1, default_value=(1.0,))
            )
        for rate in (72, 36, 72):
            report["phase"] = "Shutter " + mode + " rate " + str(rate)
            save()
            source = sampled.material.parm("sampled_rate")
            source.setExpression("1+$T*" + str(rate * 2), hou.exprLanguage.Hscript)
            expression = {
                "time": 'ch("../height")*(1+$T*' + str(rate * 2) + ")",
                "frame": 'ch("../height")*(1+($FF-1)*' + str(rate * 2) + "/24)",
                "explicit_frame": 'ch("../height")*chf("../sampled_rate",$FF)',
                "explicit_time": 'ch("../height")*cht("../sampled_rate",$T)',
            }[mode]
            sampled.scalar.parm("height").setExpression(expression, hou.exprLanguage.Hscript)
            image = temporal.session.wait(temporal.apply(native_scene.capture(sampled.rop), hou.time()))
            status = temporal.session.status()
            actual = sampled.pixels(image)["combined"][:, :, :3]
            difference = float(np.abs(actual - motion_pixels((-rate / 96, 0.0, rate / 96))).mean())
            report["current"] = dict(label=report["phase"], status=status, mean_difference=difference)
            save()
            assert difference < 0.0008, (mode, rate, difference, status)
            assert not status["source_diagnostics"] and not status["motion_warnings"] and not status["error"], status
            report["checks"].append(
                dict(
                    label="Requested-value-preserving shutter edit: " + mode,
                    rate=rate,
                    mean_difference=difference,
                    status=status,
                )
            )
            save()
    temporal.session.close(wait=True)
    closed = temporal.session.status()
    assert closed["owned_geometry_bytes"] == closed["device_allocation_bytes"] == 0, closed
    report["temporal_closed"] = closed
    registry.close_all()

    # Three ordered persistent final frames exercise the same expression route.
    report["phase"] = "Writing three ordered persistent final frames"
    save()
    f.native(0.35, 0.23)
    f.expression('fit(ch("../gain"),0,1,1,5)+$FF')
    f.rop.setParms(dict(picture=str(dest / "expressions.$F4.exr"), persistent_data=1))
    f.rop.render(frame_range=(1, 3, 1))
    summary = f.rop.cachedUserData(final_render._summary_key)
    assert summary["completed_frames"] == 3 and summary["created_sessions"] == 1 and not summary["session_alive"], (
        summary
    )
    batch = []
    for number in range(1, 4):
        report["phase"] = "Verifying final frame " + str(number)
        save()
        output = dest / f"expressions.{number:04d}.exr"
        reader = oiio.ImageInput.open(str(output))
        assert reader
        spec = reader.spec()
        pixels = reader.read_image(format=oiio.FLOAT)
        reader.close()
        channels = list(spec.channelnames)
        rgb = [channels.index(("Combined." if "Combined.R" in channels else "") + c) for c in "RGB"]
        np.testing.assert_allclose(
            pixels[31:33, 31:33][:, :, rgb], np.broadcast_to(f.rgb(2.4 + number), (2, 2, 3)), atol=7e-5, rtol=3e-5
        )
        assert not json.loads(spec.get_string_attribute("cycles_houdini:source_diagnostics") or "[]")
        batch.append(dict(frame=number, path=str(output), expected_rgb=f.rgb(2.4 + number).tolist()))
    report.update(result="passed", phase="complete", batch=batch, final=summary)
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    try:
        registry.close_all()
    finally:
        save()
print(json.dumps(dict(result=report["result"], checks=len(report["checks"]), report=str(path))))
