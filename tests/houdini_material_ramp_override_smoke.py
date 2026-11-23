# SPDX-License-Identifier: Apache-2.0
"""Whole native ramp/curve overrides, independent displacement and live repairs."""

from support.paths import artifacts_root
from support.reports import atomic_json
import argparse
import ast
import json
import os
import time
import traceback
from pathlib import Path
import hou
import numpy as np
import OpenImageIO as oiio
import cycles_houdini as ch
from cycles_houdini import final_render, native_scene, registry
from material_ramp_override_fixture import Fixture, ROOT, RGB_CASES, CURVE_CASES

p = argparse.ArgumentParser()
p.add_argument("--device", default="CPU")
p.add_argument("--report", type=Path)
args = p.parse_args()
dest = artifacts_root() / "stage3/materials/material-ramp-overrides"
dest.mkdir(parents=True, exist_ok=True)
report_path = args.report or dest / "report.json"
report_path.parent.mkdir(parents=True, exist_ok=True)
report = dict(
    result="running",
    device=args.device,
    checks=[],
    scope=__doc__,
    phase="initializing",
    started_at=time.time(),
    schema="Native Material SOP scalar/count/stop dictionaries; no private ramp literal or artist-node mutation",
)
f = None
entry = None


def save():
    atomic_json(report_path, report)


def phase(label):
    report["phase"] = label
    save()


def complete(session):
    status = session.status()
    deadline = time.monotonic() + 3
    while status["state"] != "complete" and time.monotonic() < deadline:
        assert status["state"] != "failed", status
        time.sleep(0.005)
        status = session.status()
    assert status["state"] == "complete" and not status["error"], status
    return status


def render(label, expected=None, *, warning=False, z=0.15, retain=False, codes=None):
    phase(label)
    authored = f.authored()
    before = entry.session.status()
    frame = f.update()
    status = complete(entry.session)
    assert status["render_device_ids"] == f.target_ids, status
    assert bool(status["source_diagnostics"]) == warning, status["source_diagnostics"]
    assert {d["code"] for d in status["source_diagnostics"]} == (
        set(codes) if codes is not None else {"material.override_ignored"} if warning else set()
    ), status["source_diagnostics"]
    if codes and {"material.ramp_unavailable", "material.curve_unavailable", "material.override_ignored"} & set(codes):
        assert all(d["source"] == f.material.path() for d in status["source_diagnostics"]), status["source_diagnostics"]
    if expected is not None:
        f.assert_pixels(frame, expected, z)
    else:
        pixels = f.pixels(frame)
        assert np.isfinite(pixels).all() and pixels[31:33, 31:33, :3].max() > 0.01
        np.testing.assert_allclose(f.pixels(frame, "position")[31:33, 31:33, 2], z, atol=2e-5, rtol=0)
    assert f.authored() == authored, "Override acquisition modified artist parameters"
    if retain:
        assert status["geometry_conversions"] == before["geometry_conversions"], (before, status)
        assert status["displacement_rebuilds"] == before["displacement_rebuilds"], (before, status)
        assert not {"tri_verts", "tri_vindex"} & entry.session.diagnostics()["buffer_copy_requests"].keys()
    report["checks"].append(
        dict(
            label=label,
            status=status,
            expected=np.asarray(np.broadcast_to(expected, (3,)), dtype=float).tolist()
            if expected is not None
            else None,
        )
    )
    report["last_completed_check"] = label
    save()
    return frame, status


save()
try:
    hou.setFps(24)
    hou.setFrame(1)
    f = Fixture(args.device)
    entry = f.session()
    render("Native count/color/vector3/Alpha-stop overrides reach the Color and Alpha ramps", (0.33, 0.33, 0.3))
    literal = ast.literal_eval(f.literal_fields())
    assert (
        literal["palette"] == 3
        and literal["palette_alpha"] == 3
        and np.allclose(literal["palette3c"], (0.2, 0.6, 0.9))
        and np.isclose(literal["palette_alpha2value"], 0.8)
    ), literal
    assert (
        f.material.parm("palette").evalAsInt() == 2
        and f.material.parm("palette3pos") is None
        and f.material.parm("palette_alpha3pos") is None
    )
    pid = entry.session.status()["renderer_pid"]
    f.local("palette_alpha3value", "fval").set(0.6)
    render("Editing an Alpha stop changes the displayed result", (0.385, 0.385, 0.35), retain=True)
    # Alpha stops are independent: an incomplete added Alpha stop drops only
    # the Alpha count override (Alpha .8 beyond stop 2); Color keeps 3 stops.
    f.local("palette_alpha3value").set("unknown")
    render(
        "Incomplete added Alpha stop retains valid underlying stops, Color and displacement",
        (0.44, 0.44, 0.4),
        warning=True,
        retain=True,
    )
    f.local("palette_alpha3value").set("palette_alpha3value")
    render("Repair restores the complete Alpha ramp and clears warnings", (0.385, 0.385, 0.35), retain=True)
    for mode in ("constant", "linear", "ease", "b_spline", "cardinal"):
        expected = f.rgb_reference(RGB_CASES[mode + "_nonuniform"])
        render("Whole RGBA " + mode + " interpolation matches the pinned Blender reference", expected, retain=True)
    expected = f.rgb_reference(RGB_CASES["linear_maximum"])
    render("A complete32-stop expanded native dictionary fits the512-field budget", expected, retain=True)
    assert len(ast.literal_eval(f.literal_fields())) > 128

    # A Color ramp referencing another native RGB ramp; Alpha stays on the
    # promoted (authored, opaque) Alpha ramp.
    group = f.material.parmTemplateGroup()
    group.append(hou.RampParmTemplate("rgb_palette", "Native RGB Ramp", hou.rampParmType.Color, default_value=2))
    f.material.setParmTemplateGroup(group)
    f.material.parm("rgb_palette").set(
        hou.Ramp((hou.rampBasis.Linear,) * 2, (0.0, 1.0), ((0.1, 0.2, 0.3), (0.8, 0.6, 0.4)))
    )
    f.rgba.parm("ramp").setExpression('ch("../rgb_palette")', hou.exprLanguage.Hscript)
    f.rgba.parm("factor").set(0.75)
    f.surface(f.rgba, alpha=True)
    f.set_fields(
        [
            ("rgb_palette", "int", 3),
            ("rgb_palette1pos", "float", 0.0),
            ("rgb_palette1c", "color", (0.1, 0.3, 0.5)),
            ("rgb_palette1interp", "int", 1),
            ("rgb_palette2pos", "float", 0.5),
            ("rgb_palette2c", "color", (0.9, 0.5, 0.1)),
            ("rgb_palette2interp", "int", 1),
            ("rgb_palette3pos", "float", 1.0),
            ("rgb_palette3c", "vector3", (0.2, 0.6, 0.9)),
            ("rgb_palette3interp", "int", 1),
        ]
    )
    render(
        "Native RGB/vector ramp count and virtual stop values render with opaque alpha", (0.55, 0.55, 0.5), retain=True
    )
    f.rgba.parm("ramp").setExpression('ch("../palette")', hou.exprLanguage.Hscript)

    f.scalar_ramp()
    render("Whole promoted Float Ramp matches independent linear interpolation", (0.6,) * 3, retain=True)
    f.local("profile3value").set("unknown")
    render("Missing added Float Ramp value retains supported stops", (0.8,) * 3, warning=True, retain=True)
    f.local("profile3value").set("profile3value")
    render("Float Ramp value repair restores its image", (0.6,) * 3, retain=True)
    expected, height = f.alpha_height_case()
    render("Authored RGB with a valid Alpha override and independent height", expected, z=height)
    f.authored_rgb_fault(True)
    render(
        "Authored bad RGB retains the overridden Alpha height and fixed height",
        (1, 0, 1),
        warning=True,
        z=height,
        retain=True,
        codes={"material.ramp_unavailable"},
    )
    expected = f.authored_rgb_fault(False)
    render(
        "RGB source repair clears its warning without rebuilding independent height", expected, z=height, retain=True
    )
    f.independent_height()
    f.set_fields([("palette1c", "color", (0.3, 0.5, 0.7)), ("palette_interpolation", "string", "LINEAR")])
    authored_alpha = f.material.parm("palette_alpha1value").eval()
    f.material.parm("palette_alpha1value").set(float("nan"))
    render(
        "Authored bad Alpha retains independent height and valid RGB override",
        (1, 0, 1),
        warning=True,
        codes={"material.ramp_unavailable"},
    )
    f.surface(f.rgba, alpha=False)
    render("Unused authored bad Alpha does not invalidate the valid RGB override", (0.225, 0.425, 0.625), retain=True)
    f.surface(f.rgba, alpha=True)
    f.material.parm("palette_alpha1value").set(authored_alpha)
    render("Alpha source repair restores valid overridden RGB shading", (0.225, 0.425, 0.625), retain=True)
    f.scalar_ramp()
    render("Restoring the scalar fixture retains the original independent height", (0.6,) * 3)
    # Per-value literal parsing preserves a valid displacement even if another
    # dictionary value contains executable syntax. Nothing executes that syntax.
    offset_component = tuple(f.material.parmTuple("offset"))[2].name()
    report["displacement_override_component"] = offset_component
    raw = ast.literal_eval(f.literal_fields())
    raw[offset_component] = 0.3
    f.use_literal(repr(raw))
    render("Independent displacement override remains active", (0.6,) * 3, z=0.3)
    before = entry.session.status()
    sentinel = dest / "must-not-exist.txt"
    assert not sentinel.exists()
    safe = {key: value for key, value in raw.items() if key != "profile3value"}
    bad = (
        repr(safe)[:-1]
        + ",'profile3value':__import__('pathlib').Path("
        # VEX chs() consumes one layer of backslash escapes. A Windows
        # Nightly path would turn \\N into an invalid Python Unicode escape,
        # making the whole dictionary malformed instead of just this field.
        + repr(sentinel.as_posix())
        + ").write_text('never')}"
    )
    f.use_literal(bad)
    cooked = f.literal.geometry().prim(0).stringAttribValue("material_override")
    tree = ast.parse(cooked, mode="eval")
    assert isinstance(tree.body, ast.Dict)
    malformed_fields = {key.value: value for key, value in zip(tree.body.keys, tree.body.values)}
    assert isinstance(malformed_fields["profile3value"], ast.Call)
    assert ast.literal_eval(malformed_fields[offset_component]) == 0.3
    report["malformed_literal_transport"] = dict(
        sentinel=str(sentinel),
        payload=cooked,
        top_level_ast=type(tree.body).__name__,
        rejected_field_ast=type(malformed_fields["profile3value"]).__name__,
        independent_offset=ast.literal_eval(malformed_fields[offset_component]),
    )
    render(
        "Malformed partial layer retains valid displacement without executing code",
        (0.8,) * 3,
        warning=True,
        z=0.3,
        retain=True,
    )
    assert not sentinel.exists()
    f.use_literal(repr(raw))
    render("Repairing the malformed field restores the ramp and clears warnings", (0.6,) * 3, z=0.3, retain=True)
    f.assigned.setDisplayFlag(True)
    f.assigned.setRenderFlag(True)

    for kind in ("float", "rgb", "vector"):
        row = CURVE_CASES[kind + "_mixed"]
        expected = f.curve_reference(row)
        render("Complete promoted native " + kind + " curve matches its hou.Ramp reference", expected)
        channel = {"float": "value", "rgb": "r", "vector": "x"}[kind]
        name = {"float": "float_curve", "rgb": "rgb_curves", "vector": "vector_curves"}[kind] + "_" + channel + "1pos"
        f.local(name).set("unknown")
        render(
            "Incomplete " + kind + " curve stop dictionary warns locally and preserves geometry",
            None,
            warning=True,
            retain=True,
        )
        f.local(name).set(name)
        render("Repairing " + kind + " curve restores exact native-reference shading", expected, retain=True)

    expected, height = f.curve_height_case()
    render("Valid overridden curve end stop value independently drives shading and height", expected, z=height)
    f.authored_curve_fault(True)
    render(
        "Authored bad curve X retains the valid End Y override and independent height",
        (1, 0, 1),
        warning=True,
        z=height,
        retain=True,
        codes={"material.ramp_unavailable"},
    )
    expected = f.authored_curve_fault(False)
    render(
        "Curve source repair clears its warning without rebuilding independent height", expected, z=height, retain=True
    )
    f.scalar_ramp()
    render("Restoring the Float Curve profile retires the temporary endpoint height", (0.6,) * 3)
    expected, height = f.lazy_expression_case()
    render("Relative Hscript overrides resolve before an active native expression read", expected, z=height)
    expected = f.lazy_expression_python(True)
    render(
        "First active relative Python read warns locally and preserves independent overridden height",
        expected,
        warning=True,
        z=height,
        retain=True,
        codes={"material.override_ignored"},
    )
    expected = f.lazy_expression_python(False)
    render(
        "Repairing the relative expression restores owned arithmetic without rebuilding height",
        expected,
        z=height,
        retain=True,
    )
    f.scalar_ramp()
    render("Restoring scalar controls restores the authored independent offset", (0.6,) * 3)

    # A contributing expression is evaluated against owned overrides at the
    # requested sample time. Unoverridden animation remains genuine Houdini data.
    group = f.material.parmTemplateGroup()
    group.append(hou.FloatParmTemplate("gain", "Gain", 1, default_value=(2.0,)))
    group.append(hou.FloatParmTemplate("animated_source", "Animated Source", 1, default_value=(0.5,)))
    f.material.setParmTemplateGroup(group)
    f.material.parm("animated_source").setExpression(".5+$T", hou.exprLanguage.Hscript)
    f.material.parm("profile1value").setExpression('ch("gain") * ch("animated_source")', hou.exprLanguage.Hscript)
    f.profile.parm("value").set(0.75)
    f.surface(f.profile, "value")
    f.set_fields([("gain", "float", 4.0)])
    render("Compound promoted references honor override values and current sample time", (1.175,) * 3)
    from cycles_houdini.material_overrides import Evaluation

    graphs = []
    phase("Capturing off-frame promoted expression dependencies")
    for at in (-1 / 96, 0.0, 1 / 96):
        evaluation = Evaluation(f.material, [f.literal_fields()])
        graph = ch.material_graph(f.material, at, evaluation=evaluation)
        assert f.profile in evaluation.animated_nodes
        graphs.append(graph)
    assert graphs[0] != graphs[1] and graphs[1] != graphs[2]
    report["checks"].append(
        dict(
            label="Off-frame-only promoted dependencies are captured at their requested times",
            times=[-1 / 96, 0.0, 1 / 96],
            graphs=graphs,
        )
    )
    save()
    f.material.parm("profile1value").setExpression(
        "hou.parm("
        + repr(f.material.path() + "/gain")
        + ").eval()*hou.parm("
        + repr(f.material.path() + "/animated_source")
        + ").eval()",
        hou.exprLanguage.Python,
    )
    render(
        "Unsupported contributing Python omits only its override and retains authored shading",
        (0.925,) * 3,
        warning=True,
        retain=True,
    )
    f.material.parm("profile1value").setExpression('ch("gain") * ch("animated_source")', hou.exprLanguage.Hscript)
    render("Repairing the contributing expression clears its warning", (1.175,) * 3, retain=True)
    assert entry.session.status()["renderer_pid"] == pid
    entry.session.close(wait=True)
    closed = entry.session.status()
    assert closed["owned_geometry_bytes"] == closed["device_allocation_bytes"] == 0, closed
    registry.close_all()
    report["closed"] = closed

    # Native shutter sampling must notice an expression edit whose value is
    # unchanged at frame1. Analytic box-shutter coverage is independent of Cycles.
    from sampled_override_fixture import Fixture as SampledFixture
    from independent_binding_fixture import motion_pixels

    phase("Constructing native sampled displacement fixture")
    sampled = SampledFixture(f.target_ids)
    sampled.setup("parameter")
    sampled.rop.setParms(dict(samples=1024, ipr_samples=1024, render_threads=2))
    # The default this test was written for (before the ROP took Blender's defaults).
    sampled.rop.setParms(dict(pixel_filter="box", filter_width=1.0))
    sampled.assigned.parm("local1_fval1").set(0.5)
    sampled.assigned.parm("local1_fval1").deleteAllKeyframes()
    sampled.assigned.parm("local1_fval1").set(0.5)
    sampled.material.addSpareParmTuple(hou.FloatParmTemplate("shutter_gain", "Shutter Gain", 1, default_value=(1.0,)))
    sampled.material.parm("shutter_gain").setExpression("1+$T*144", hou.exprLanguage.Hscript)
    sampled.scalar.parm("height").setExpression('ch("../height") * ch("../shutter_gain")', hou.exprLanguage.Hscript)
    temporal = registry.for_rop(sampled.rop, native_scene.configuration(sampled.rop))
    for rate in (72, 36, 72):
        phase("Compound displacement history rate " + str(rate))
        sampled.material.parm("shutter_gain").setExpression("1+$T*" + str(rate * 2), hou.exprLanguage.Hscript)
        image = temporal.session.wait(temporal.apply(native_scene.capture(sampled.rop), hou.time()))
        status = complete(temporal.session)
        pixels = sampled.pixels(image)["combined"][:, :, :3]
        expected = motion_pixels((-rate / 96, 0.0, rate / 96))
        difference = float(np.abs(pixels - expected).mean())
        assert not status["source_diagnostics"] and not status["motion_warnings"], status
        assert difference < 0.0008, (rate, difference, status)
        report["checks"].append(
            dict(
                label="Compound displacement history changes while the requested value stays fixed",
                rate=rate,
                mean_difference=difference,
                status=status,
            )
        )
        save()
    temporal.session.close(wait=True)
    temporal_closed = temporal.session.status()
    assert temporal_closed["owned_geometry_bytes"] == temporal_closed["device_allocation_bytes"] == 0, temporal_closed
    registry.close_all()
    report["temporal_closed"] = temporal_closed

    # Five ordered final frames retain the same recovery policy. Frame3 has a
    # missing native Alpha channel (only the Alpha count override drops);
    # frames4/5 repair it without user intervention.
    f.simple_rgba()
    f.assigned.parm("local1_name" + str(f.indices["palette_alpha3value"])).setExpression(
        "'unknown' if int(hou.frame())==3 else 'palette_alpha3value'", hou.exprLanguage.Python
    )
    phase("Writing five ordered final frames")
    f.rop.setParms(dict(picture=str(dest / "ramp.$F4.exr"), persistent_data=1))
    f.rop.render(frame_range=(1, 5, 1))
    summary = f.rop.cachedUserData(final_render._summary_key)
    assert summary["completed_frames"] == 5 and summary["created_sessions"] == 1 and not summary["session_alive"], (
        summary
    )
    frames = []
    for number in range(1, 6):
        phase("Verifying final frame " + str(number))
        path = dest / f"ramp.{number:04d}.exr"
        reader = oiio.ImageInput.open(str(path))
        assert reader
        spec = reader.spec()
        values = reader.read_image(format=oiio.FLOAT)
        reader.close()
        channels = list(spec.channelnames)
        rgb = [channels.index(("Combined." if "Combined.R" in channels else "") + c) for c in "RGB"]
        np.testing.assert_allclose(
            values[31:33, 31:33][:, :, rgb],
            np.broadcast_to((0.44, 0.44, 0.4) if number == 3 else (0.33, 0.33, 0.3), (2, 2, 3)),
            atol=7e-5,
            rtol=3e-5,
        )
        diagnostics = json.loads(spec.get_string_attribute("cycles_houdini:source_diagnostics") or "[]")
        assert bool(diagnostics) == (number == 3), diagnostics
        frames.append(dict(frame=number, path=str(path), diagnostics=diagnostics))
        report["batch"] = frames
        save()
    report.update(result="passed", phase="complete", batch=frames, final=summary)
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    try:
        registry.close_all()
    finally:
        save()
print(json.dumps(dict(result=report["result"], checks=len(report["checks"]), report=str(report_path))))
