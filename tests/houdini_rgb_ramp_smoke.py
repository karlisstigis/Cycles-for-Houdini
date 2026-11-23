# SPDX-License-Identifier: Apache-2.0
"""Pinned Blender RGB/Alpha images, whole Color+Alpha ramp promotion, repair and batch frames."""

from support.paths import artifacts_root

import argparse, json, traceback
import hou, numpy as np, OpenImageIO as oiio
import cycles_houdini as ch
from cycles_houdini import ramps, final_render
from hue_ramp_fixture import Fixture

p = argparse.ArgumentParser()
p.add_argument("--device", default="CPU")
device = p.parse_args().device
f = Fixture(device, "blender-rgb-ramps.json")
dest = artifacts_root() / "stage3/materials/rgb-ramp"
dest.mkdir(parents=True, exist_ok=True)
report = dict(result="running", device=device, checks=[])
session = None
try:
    session = ch.Session(
        device=device,
        width=f.width,
        height=f.height,
        samples=1,
        execution="worker",
        threads=2,
        use_texture_cache=False,
        collect_diagnostics=True,
        passes=["combined", "position", "normal"],
    )

    def render(label, expected=None, warning=False):
        frame = session.wait(session.apply(f.update()), timeout=120)
        status = session.status()
        error = f.inspect(frame, expected)
        assert not status["error"] and bool(status["source_diagnostics"]) == warning, status
        if report["checks"]:
            assert status["renderer_pid"] == report["checks"][0]["status"]["renderer_pid"], status
        report["checks"].append(dict(label=label, error=error, status=status))
        return frame, status

    def connect(output):
        f.emission.setNamedInput("color", f.ramp, output)

    for case in f.reference["cases"]:
        f.choose(case["id"])
        connect("color")
        render("Color " + case["id"])
        data = ch._native.ramp_preview(*ramps.preview_sources(f.ramp), tuple(f.factors), dict(raw=True))
        linear = np.frombuffer(data["linear"], np.float32).reshape(-1, 4)
        assert not data["color_issue"] and not data["alpha_issue"], (
            case["id"],
            data["color_issue"],
            data["alpha_issue"],
        )
        np.testing.assert_allclose(linear[:, :3], case["colors"], atol=3e-5, rtol=3e-5)
        np.testing.assert_allclose(linear[:, 3], case["alphas"], atol=3e-5, rtol=3e-5)
        connect("alpha")
        render("Alpha " + case["id"], np.array(case["alphas"])[:, None])
    f.choose("cardinal_nonuniform")
    connect("color")
    f.displace()
    frame, baseline = render("Independent height baseline")
    height = f.planes(frame)["position"].copy()
    before = ramps.preview_sources(f.ramp)
    ch.promote_parameter(f.mat, f.ramp, "ramp", name="palette", label="Palette")
    controls = ramps.ColorRampControls(f.ramp)
    assert ramps.preview_sources(f.ramp) == before
    render("Whole Color+Alpha promotion retains image")
    # Promoted global menus and both ramps live on the material.
    menu = lambda suffix: f.mat.parm("palette" + suffix)
    for cycle in range(2):
        menu("_interpolation").set("not_an_interpolation")
        frame, status = render("Invalid global interpolation " + str(cycle), (1, 0, 1), True)
        np.testing.assert_array_equal(f.planes(frame)["position"], height)
        menu("_interpolation").set("CARDINAL")
        render("Global interpolation repair " + str(cycle))
        f.mat.parm("palette_alpha1value").set(float("nan"))
        render("Unused bad promoted Alpha " + str(cycle))
        connect("alpha")
        render("Used bad promoted Alpha " + str(cycle), (1, 0, 1), True)
        f.mat.parm("palette_alpha1value").set(f.case["controls"][0]["alpha"])
        render("Promoted Alpha repair " + str(cycle), np.array(f.case["alphas"])[:, None])
        connect("color")
    controls.parm("ramp1interp").set(99)
    render("Blender interpolation ignores dormant native basis")
    controls.parm("ramp1interp").set(1)
    f.displacement.setNamedInput("height", f.ramp, "alpha")
    frame, _ = render("Promoted Alpha height")
    alpha_height = f.planes(frame)["position"].copy()
    controls.parm("ramp1cr").set(float("nan"))
    frame, _ = render("Bad Color retains valid promoted Alpha displacement", (1, 0, 1), True)
    np.testing.assert_array_equal(f.planes(frame)["position"], alpha_height)
    controls.parm("ramp1cr").set(f.case["controls"][0]["color"][0])
    render("Promoted Color repair")
    f.mat.parm("palette_alpha1value").set(float("nan"))
    frame, _ = render("Bad shared Alpha omits displacement", warning=True)
    assert np.max(np.abs(f.planes(frame)["position"][:, :, 2])) < 1e-5
    f.mat.parm("palette_alpha1value").set(f.case["controls"][0]["alpha"])
    frame, _ = render("Shared Alpha height repair")
    np.testing.assert_array_equal(f.planes(frame)["position"], alpha_height)
    f.mat.node("surface_output").setInput(f.mat.node("surface_output").inputIndex("displacement"), None)
    session.close(wait=True)
    closed = session.status()
    assert closed["device_allocation_bytes"] == closed["owned_geometry_bytes"] == 0
    # Animation is evaluated at the requested frame on the promoted controls.
    connect("alpha")
    f.ramp.setInput(0, None)
    f.ramp.parm("factor").set(0.425)
    for i in range(1, f.mat.parm("palette_alpha").evalAsInt() + 1):
        f.mat.parm(f"palette_alpha{i}value").setExpression("if($F==3,1e100,.2+.1*$F)", hou.exprLanguage.Hscript)
    f.rop.setParms(dict(picture=str(dest / "promoted.$F4.exr"), samples=1, persistent_data=1, use_denoise=0))
    f.rop.render(frame_range=(1, 5, 1))
    summary = f.rop.cachedUserData(final_render._summary_key)
    assert summary["completed_frames"] == 5 and summary["created_sessions"] == 1 and not summary["session_alive"], (
        summary
    )
    for frame in range(1, 6):
        reader = oiio.ImageInput.open(str(dest / f"promoted.{frame:04}.exr"))
        spec = reader.spec()
        pixels = reader.read_image(format=oiio.FLOAT)
        reader.close()
        issues = json.loads(spec.get_string_attribute("cycles_houdini:source_diagnostics") or "[]")
        assert bool(issues) == (frame == 3), issues
        channels = [
            list(spec.channelnames).index(("Combined." if "Combined.R" in spec.channelnames else "") + c) for c in "RGB"
        ]
        np.testing.assert_allclose(
            pixels[f.height // 2, f.width // 2, channels],
            (1, 0, 1) if frame == 3 else (0.2 + 0.1 * frame,) * 3,
            atol=3e-5,
            rtol=3e-5,
        )
    report.update(
        result="passed",
        closed=closed,
        batch=summary,
        batch_frames=5,
        reference_cases=len(f.reference["cases"]),
        preview_samples=len(f.factors) * len(f.reference["cases"]),
    )
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    if session:
        session.close(wait=True)
    (dest / "report.json").write_text(json.dumps(report, indent=2))
print(json.dumps(dict(result=report["result"], checks=len(report["checks"]))))
