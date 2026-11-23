# SPDX-License-Identifier: Apache-2.0
"""Independent Color/Alpha images, exact preview, per-output recovery and native final frames.

Alpha stops live on the inline Alpha ramp (alpha_ramp#value) at the reference
positions; previously they were the fourth component of each Color stop."""

from ramp_table_reference import native_lookup
from support.paths import artifacts_root

import argparse, json, struct, traceback
import hou, numpy as np, OpenImageIO as oiio
import cycles_houdini as ch
from cycles_houdini import ramps, final_render
from hue_ramp_fixture import Fixture

p = argparse.ArgumentParser()
p.add_argument("--device", default="CPU")
device = p.parse_args().device
f = Fixture(device, "blender-rgba-ramps.json")
f.choose("hsl_far_nonuniform")
dest = artifacts_root() / "stage3/materials/color-ramp-rgba"
dest.mkdir(parents=True, exist_ok=True)
report = dict(result="running", device=device, scope=__doc__, checks=[])
session = None
factor = np.asarray(f.factors, np.float32)
bases = (
    hou.rampBasis.Constant,
    hou.rampBasis.Linear,
    hou.rampBasis.CatmullRom,
    hou.rampBasis.MonotoneCubic,
    hou.rampBasis.Bezier,
    hou.rampBasis.BSpline,
    hou.rampBasis.Hermite,
)


def preview(options=None):
    return ch._native.ramp_preview(*ramps.preview_sources(f.ramp), tuple(f.factors), options or dict(raw=True))


def connect(output):
    f.emission.setNamedInput("color", f.ramp, output)


def expected_alpha():
    return np.repeat(np.asarray(f.case["alphas"])[:, None], 3, axis=1)


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

    assert f.ramp.outputNames() == ("color", "alpha"), f.ramp.outputNames()
    # Color/Alpha are checked separately against independent Blender data and
    # together through two live consumers. Nonlinear native bases stay dormant
    # in HSV/HSL, including for scalar alpha.
    for index, case in enumerate(f.reference["cases"]):
        f.choose(case["id"], hou.rampBasis.Linear if case["mode"] == "RGB" else bases[index % 7])
        connect("color")
        frame, status = render("Color " + case["id"])
        data = preview()
        linear = np.frombuffer(data["linear"], np.float32).reshape(-1, 4)
        assert not data["color_issue"] and not data["alpha_issue"], data
        np.testing.assert_allclose(linear[:, :3], case["colors"], atol=3e-5, rtol=3e-5)
        np.testing.assert_allclose(linear[:, 3], case["alphas"], atol=3e-5, rtol=3e-5)
        connect("alpha")
        render("Alpha " + case["id"], expected_alpha())
        for i in (0, len(f.factors) // 2, len(f.factors) - 1):
            np.testing.assert_array_equal(ch._native.ramp_preview_sample(data["prepared"], f.factors[i]), linear[i])
    # All seven native bases, with alpha independent of the RGB/LAB transform.
    for space in ("RGB", "LAB"):
        for basis in bases:
            f.choose("rgb_near_nonuniform", basis)
            f.ramp.parm("ramp_color_space").set(space)
            values = [c["alpha"] for c in f.case["controls"]]
            keys = [c["position"] for c in f.case["controls"]]
            native = hou.Ramp((basis,) * len(keys), keys, values)
            target = np.array([native_lookup(native, float(x)) for x in factor])
            connect("alpha")
            render("Native Alpha " + space + " " + str(basis), target[:, None])
            data = preview()
            np.testing.assert_allclose(
                np.frombuffer(data["linear"], np.float32).reshape(-1, 4)[:, 3], target, atol=3e-5, rtol=3e-5
            )
    f.choose("hsl_far_nonuniform")
    connect("color")
    f.emission.setNamedInput("strength", f.ramp, "alpha")
    frame, status = render(
        "Both outputs are reachable", np.asarray(f.case["colors"]) * np.asarray(f.case["alphas"])[:, None]
    )
    graph = ch.material_graph(f.mat)
    assert len([n for n in graph["nodes"] if n["definition"] == "cycles/owned_ramp/1"]) == 2, graph
    f.emission.setInput(f.emission.inputIndex("strength"), None)
    f.emission.parm("strength").set(1)
    # The same OCIO/display path is used by the preview and viewer, without
    # baking alpha into the RGB sample or changing scene generation.
    frame, _ = render("Display preview baseline")
    before = session.status()["generation"]
    policy = ch.color_policy()
    for raw in (True, False):
        options = dict(
            raw=raw,
            display=policy["default_display"],
            view=policy["default_views"].get(policy["default_display"], ""),
            channels="rgb",
            exposure=0.0,
            gamma=1.0,
        )
        data = preview(options)
        display = session.display_image(**options)
        pixels = np.frombuffer(display["rgba"], np.uint8).reshape(f.height, f.width, 4)
        measured = pixels[f.height // 2, np.arange(len(f.factors)) * 4 + 2]
        np.testing.assert_allclose(np.frombuffer(data["rgba"], np.uint8).reshape(-1, 4), measured, atol=1, rtol=0)
        assert session.status()["generation"] == before
    # Independent source faults must not invalidate a sibling output or height.
    f.displace()
    frame, initial = render("Independent height baseline")
    position = f.planes(frame)["position"].copy()
    for cycle in range(2):
        f.ramp.parm("alpha_ramp1value").set(float("nan"))
        frame, status = render("Unused bad Alpha " + str(cycle))
        assert preview()["alpha_issue"] and not preview()["color_issue"]
        np.testing.assert_array_equal(f.planes(frame)["position"], position)
        assert status["displacement_rebuilds"] == initial["displacement_rebuilds"]
        connect("alpha")
        render("Used bad Alpha " + str(cycle), (1, 0, 1), True)
        f.choose("hsl_far_nonuniform")
        render("Alpha repair " + str(cycle), expected_alpha())
        f.ramp.parm("ramp1cr").set(float("nan"))
        frame, _ = render("Unused bad Color " + str(cycle), expected_alpha())
        assert preview()["color_issue"] and not preview()["alpha_issue"]
        np.testing.assert_array_equal(f.planes(frame)["position"], position)
        connect("color")
        render("Used bad Color " + str(cycle), (1, 0, 1), True)
        f.choose("hsl_far_nonuniform")
        render("Color repair " + str(cycle))
    connect("alpha")
    f.ramp.parm("ramp_hue_interpolation").set("not_a_hue")
    render("Alpha ignores unusable hue control", expected_alpha())
    assert preview()["color_issue"] and not preview()["alpha_issue"]
    f.choose("hsl_far_nonuniform")
    connect("color")
    render("Hue repair")
    f.displacement.setNamedInput("height", f.ramp, "alpha")
    frame, _ = render("Alpha drives displacement")
    height = f.planes(frame)["position"].copy()
    assert np.max(np.abs(height[:, :, 2])) > 0.1
    f.ramp.parm("ramp1cr").set(float("nan"))
    frame, _ = render("Bad Color retains valid Alpha displacement", (1, 0, 1), True)
    np.testing.assert_array_equal(f.planes(frame)["position"], height)
    f.choose("hsl_far_nonuniform")
    render("Color repair retains Alpha height")
    f.ramp.parm("alpha_ramp1value").set(float("nan"))
    frame, _ = render("Bad shared Alpha omits height while Color renders", warning=True)
    assert np.max(np.abs(f.planes(frame)["position"][:, :, 2])) < 1e-5
    f.choose("hsl_far_nonuniform")
    frame, _ = render("Alpha height repair")
    np.testing.assert_array_equal(f.planes(frame)["position"], height)
    # Malformed private preview calls remain programming errors, not warnings.
    source = ramps.preview_sources(f.ramp)
    invalid = [
        ([], source[1], [0.5], {}),
        (source[0], source[1], [], {}),
        (source[0], source[1], [0.5] * 4097, {}),
        (source[0], source[1], [float("nan")], {}),
        (source[0], source[1], [0.5], dict(display="not_a_display")),
        (source[1], source[0], [0.5], {}),
    ]
    for args in invalid:
        try:
            ch._native.ramp_preview(*args)
        except RuntimeError:
            pass
        else:
            raise AssertionError("Malformed preview call accepted")
    for owner, x in ((None, 0.5), (preview()["prepared"], float("inf"))):
        try:
            ch._native.ramp_preview_sample(owner, x)
        except RuntimeError:
            pass
        else:
            raise AssertionError("Invalid preview owner/sample accepted")
    # Actual transparent closure coverage, rather than only Alpha-as-emission.
    output = f.mat.node("surface_output")
    output.setInput(output.inputIndex("displacement"), None)
    transparent = f.mat.createNode("cycles::transparent_bsdf::1")
    mixed = f.mat.createNode("cycles::mix_closure::1")
    mixed.setNamedInput("closure1", transparent, "BSDF")
    mixed.setNamedInput("closure2", f.emission, "emission")
    mixed.setNamedInput("fac", f.ramp, "alpha")
    output.setNamedInput("surface", mixed, "closure")
    f.rop.parm("transparent_background").set(1)
    for alpha in (0.0, 1.0):
        for i in range(1, int(f.ramp.parm("alpha_ramp").evalAsInt()) + 1):
            f.ramp.parm(f"alpha_ramp{i}value").set(alpha)
        frame = session.wait(session.apply(f.update()), timeout=120)
        image = f.planes(frame)["combined"]
        samples = image[f.height // 2, np.arange(len(f.factors)) * 4 + 2]
        np.testing.assert_allclose(samples[:, 3], alpha, atol=1e-6, rtol=0)
        np.testing.assert_allclose(samples[:, :3], np.asarray(f.case["colors"]) * alpha, atol=3e-5, rtol=3e-5)
        path = dest / f"coverage-{int(alpha)}.exr"
        session.write_exr(path)
        reader = oiio.ImageInput.open(str(path))
        spec = reader.spec()
        pixels = reader.read_image(format=oiio.FLOAT)
        reader.close()
        a = list(spec.channelnames).index("A" if "A" in spec.channelnames else "Combined.A")
        np.testing.assert_allclose(
            pixels[f.height // 2, np.arange(len(f.factors)) * 4 + 2, a], alpha, atol=1e-6, rtol=0
        )
    session.close(wait=True)
    closed = session.status()
    assert closed["device_allocation_bytes"] == closed["owned_geometry_bytes"] == 0
    # Final/batch uses the same independent Alpha recovery and continues frames.
    output.setNamedInput("surface", f.emission, "emission")
    f.rop.parm("transparent_background").set(0)
    connect("alpha")
    f.ramp.setInput(0, None)
    f.ramp.parm("factor").set(0.425)
    f.choose("hsl_far_nonuniform")
    for i in range(1, f.ramp.parm("alpha_ramp").evalAsInt() + 1):
        f.ramp.parm(f"alpha_ramp{i}value").setExpression("if($F==3,1e100,.2+.1*$F)", hou.exprLanguage.Hscript)
    f.rop.setParms(dict(picture=str(dest / "alpha.$F4.exr"), samples=1, persistent_data=1, use_denoise=0))
    f.rop.render(frame_range=(1, 5, 1))
    summary = f.rop.cachedUserData(final_render._summary_key)
    assert summary["completed_frames"] == 5 and summary["created_sessions"] == 1 and not summary["session_alive"], (
        summary
    )
    for number in range(1, 6):
        reader = oiio.ImageInput.open(str(dest / f"alpha.{number:04}.exr"))
        spec = reader.spec()
        issues = json.loads(spec.get_string_attribute("cycles_houdini:source_diagnostics") or "[]")
        pixels = reader.read_image(format=oiio.FLOAT)
        reader.close()
        assert bool(issues) == (number == 3), issues
        channels = [
            list(spec.channelnames).index(("Combined." if "Combined.R" in spec.channelnames else "") + c) for c in "RGB"
        ]
        np.testing.assert_allclose(
            pixels[f.height // 2, f.width // 2, channels],
            (1, 0, 1) if number == 3 else (0.2 + 0.1 * number,) * 3,
            atol=3e-5,
            rtol=3e-5,
        )
    report.update(
        result="passed",
        closed=closed,
        batch_frames=5,
        batch=summary,
        preview_contracts=len(invalid) + 2,
        opacity_outputs=2,
    )
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    if session:
        session.close(wait=True)
    (dest / "report.json").write_text(json.dumps(report, indent=2))
print(json.dumps(dict(result=report["result"], checks=len(report["checks"]), batch_frames=report.get("batch_frames"))))
