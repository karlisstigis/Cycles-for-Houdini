# SPDX-License-Identifier: Apache-2.0
"""Blender HSV/HSL image references, local repair, displacement and five final frames."""

from support.paths import artifacts_root

import argparse, json, os, traceback
from pathlib import Path
import hou, numpy as np, OpenImageIO as oiio
import cycles_houdini as ch
from cycles_houdini import final_render
from hue_ramp_fixture import Fixture

p = argparse.ArgumentParser()
p.add_argument("--device", default="CPU")
device = p.parse_args().device
f = Fixture(device)
dest = artifacts_root() / "stage3/materials/hue-ramps"
dest.mkdir(parents=True, exist_ok=True)
report = dict(result="running", device=device, scope=__doc__, checks=[])
bases = (
    hou.rampBasis.Constant,
    hou.rampBasis.Linear,
    hou.rampBasis.CatmullRom,
    hou.rampBasis.MonotoneCubic,
    hou.rampBasis.Bezier,
    hou.rampBasis.BSpline,
    hou.rampBasis.Hermite,
)
session = None
initial = None
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

    def render(label, expected=None, warning=False, geometry_change=False):
        global initial
        frame = session.wait(session.apply(f.update()), timeout=120)
        status = session.status()
        error = f.inspect(frame, expected)
        assert bool(status["source_diagnostics"]) == warning, status
        if initial is None:
            initial = status
        else:
            assert status["renderer_pid"] == initial["renderer_pid"], status
            if not geometry_change:
                assert status["geometry_conversions"] == report["checks"][-1]["status"]["geometry_conversions"], status
        report["checks"].append(dict(label=label, error=error, status=status))
        return frame, status

    f.emission.setNamedInput("color", f.attribute, "fac")
    frame, _ = render("Scalar attribute keeps authored float32 values", np.asarray(f.factors)[:, None])
    np.testing.assert_array_equal(
        f.planes(frame)["combined"][f.height // 2, np.arange(len(f.factors)) * 4 + 2, 0],
        np.asarray(f.factors, np.float32),
    )
    f.emission.setNamedInput("color", f.ramp, "color")
    for index, case in enumerate(f.reference["cases"]):
        f.choose(case["id"], bases[index % len(bases)])
        render("Blender " + case["id"])
        f.ramp.updateParmStates()
        assert not f.ramp.parm("ramp_hue_interpolation").isHidden()
        assert f.ramp.parm("factor").isDisabled()
        assert f.ramp.parm("ramp1interp").isDisabled(), f.ramp.parm("ramp1interp").name()
    f.choose("hsl_far_red_blue")
    f.displace()
    frame, before = render("Independent displacement", geometry_change=True)
    position = f.planes(frame)["position"].copy()
    normal = f.planes(frame)["normal"].copy()
    assert abs(abs(position[f.height // 2, f.width // 2, 2]) - 0.25) < 1e-5
    for fault in ("negative", "singular", "nonfinite"):
        f.choose("hsl_far_red_blue")
        if fault == "negative":
            f.ramp.parm("ramp1cr").set(-0.1)
        elif fault == "singular":
            f.ramp.parmTuple("ramp1c").set((1.5, 0.5, 0.7))
        else:
            f.ramp.parm("ramp1cr").set(float("nan"))
        frame, status = render("Broken " + fault, (1, 0, 1), True)
        np.testing.assert_allclose(f.planes(frame)["position"], position, atol=2e-6, rtol=0)
        np.testing.assert_allclose(f.planes(frame)["normal"], normal, atol=2e-6, rtol=0)
        assert status["displacement_rebuilds"] == before["displacement_rebuilds"], status
        f.choose("hsl_far_red_blue")
        render("Repair " + fault)
    f.displace(True)
    reference, _ = render("Shared valid height", geometry_change=True)
    shared_position = f.planes(reference)["position"].copy()
    f.ramp.parmTuple("ramp1c").set((1.5, 0.5, 0.7))
    frame, _ = render("Shared invalid input omits displacement", (1, 0, 1), True, geometry_change=True)
    assert np.max(np.abs(f.planes(frame)["position"][:, :, 2])) < 1e-5
    f.choose("hsl_far_red_blue")
    frame, _ = render("Shared height repair", geometry_change=True)
    np.testing.assert_allclose(f.planes(frame)["position"], shared_position, atol=2e-6, rtol=0)
    # Hue controls are inactive in RGB/LAB. A broken dormant menu must not be
    # evaluated or included in their material graph.
    f.displace(False)
    f.ramp.parm("ramp_color_space").set("RGB")
    f.ramp.parm("ramp_hue_interpolation").set("not_a_hue")
    frame, _ = render(
        "Inactive hue control is ignored in RGB",
        np.array(f.factors)[:, None] * np.array((0, 0, 1)) + (1 - np.array(f.factors)[:, None]) * np.array((1, 0, 0)),
        geometry_change=True,
    )
    f.ramp.updateParmStates()
    assert f.ramp.parm("ramp_hue_interpolation").isHidden() and not f.ramp.parm("ramp1interp").isDisabled()
    f.ramp.setParms(dict(ramp_color_space="HSL", ramp_hue_interpolation="not_a_hue"))
    render("Invalid active hue control recovers", (1, 0, 1), True)
    f.choose("hsl_near_red_blue")
    render("Hue menu repair")
    session.close(wait=True)
    closed = session.status()
    assert closed["device_allocation_bytes"] == closed["owned_geometry_bytes"] == 0
    # Animated controls break only frame three; final rendering writes all five.
    f.ramp.setInput(0, None)
    f.ramp.parm("factor").set(0.425)
    for parm, good, bad in [("ramp1cr", 1, 1.5), ("ramp1cg", 0, 0.5), ("ramp1cb", 0, 0.7)]:
        f.ramp.parm(parm).setExpression(f"if($F==3,{bad},{good})", hou.exprLanguage.Hscript)
    f.rop.setParms(dict(picture=str(dest / "hue.$F4.exr"), samples=1, persistent_data=1, use_denoise=0))
    f.rop.render(frame_range=(1, 5, 1))
    summary = f.rop.cachedUserData(final_render._summary_key)
    assert summary["completed_frames"] == 5 and summary["created_sessions"] == 1 and not summary["session_alive"], (
        summary
    )
    expected = np.asarray(f.cases["hsl_near_red_blue"]["colors"][f.factors.index(float(np.float32(0.425)))])
    for number in range(1, 6):
        image = oiio.ImageInput.open(str(dest / f"hue.{number:04}.exr"))
        assert image
        spec = image.spec()
        issues = json.loads(spec.get_string_attribute("cycles_houdini:source_diagnostics") or "[]")
        pixels = image.read_image(format=oiio.FLOAT)
        names = list(spec.channelnames)
        image.close()
        assert bool(issues) == (number == 3), issues
        channels = [names.index(("Combined." if "Combined.R" in names else "") + c) for c in "RGB"]
        np.testing.assert_allclose(
            pixels[f.height // 2, f.width // 2, channels], (1, 0, 1) if number == 3 else expected, atol=3e-5, rtol=3e-5
        )
    from cycles_houdini.materials import create_world_material

    world = create_world_material(name="hue_world")
    background = world.node("background_shader")
    background.parm("strength").set(1)
    ramp = world.createNode("cycles::color_ramp::1", "world_hue")

    def repair_world():
        ramp.parm("ramp").set(hou.Ramp((hou.rampBasis.Linear,) * 2, (0.0, 1.0), ((1.0, 0.0, 0.0), (0.0, 0.0, 1.0))))
        ramp.setParms(dict(ramp_color_space="HSL", ramp_hue_interpolation="NEAR", factor=0.425))

    repair_world()
    background.setNamedInput("color", ramp, "color")
    fallback = (0.03125, 0.0625, 0.125)
    f.rop.setParms(dict(vobject="", world_source="material", world_material=world.path()))
    f.rop.parmTuple("background").set(fallback)
    world_checks = []
    with ch.Session(
        device=device, width=f.width, height=f.height, samples=1, execution="worker", threads=2, use_texture_cache=False
    ) as world_session:

        def render_world(label, target, warning=False):
            frame = world_session.wait(world_session.apply(f.update()), timeout=120)
            status = world_session.status()
            image = f.planes(frame)["combined"]
            np.testing.assert_allclose(image[f.height // 2, f.width // 2, :3], target, atol=3e-5, rtol=3e-5)
            assert bool(status["source_diagnostics"]) == warning and status["geometry_conversions"] == 0, status
            if world_checks:
                assert status["renderer_pid"] == world_checks[0]["status"]["renderer_pid"]
            world_checks.append(dict(label=label, status=status))

        render_world("World uses hue interpolation", expected)
        for fault in ("worker conversion", "host controls"):
            if fault == "worker conversion":
                ramp.parmTuple("ramp1c").set((1.5, 0.5, 0.7))
            else:
                ramp.parm("ramp1cr").set(float("nan"))
            render_world("Broken " + fault + " keeps configured background", fallback, True)
            repair_world()
            render_world("World repair " + fault, expected)
        world_session.close(wait=True)
        world_closed = world_session.status()
        assert world_closed["device_allocation_bytes"] == world_closed["owned_geometry_bytes"] == 0
    report.update(
        result="passed",
        closed=closed,
        batch_frames=5,
        batch=summary,
        world_checks=world_checks,
        world_closed=world_closed,
    )
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    if session:
        session.close(wait=True)
    (dest / "report.json").write_text(json.dumps(report, indent=2))
print(json.dumps(dict(result=report["result"], checks=len(report["checks"]), batch=report.get("batch_frames"))))
