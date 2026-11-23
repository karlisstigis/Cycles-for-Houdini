# SPDX-License-Identifier: Apache-2.0
"""Inline native-ramp Float/RGB/Vector Curves: independent hou.Ramp references,
Range/Extension/Factor, local recovery, Float Ramp replacement and final frames."""

from ramp_table_reference import native_lookup
from support.paths import artifacts_root
import argparse, json, traceback
import hou, numpy as np, OpenImageIO as oiio
import cycles_houdini as ch
from cycles_houdini import native_scene, final_render
from shader_curve_fixture import Fixture, ROOT
import native_curve_reference as reference
from native_curve_reference import CASES, CHANNELS

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
args = parser.parse_args()
dest = artifacts_root() / "stage3/materials/shader-curves"
dest.mkdir(parents=True, exist_ok=True)
report = dict(result="running", device=args.device, checks=[])
f = Fixture(args.device)
session = None
try:
    # Authoring contract: Value on top and Factor at the bottom (ports and
    # parameters), inline native float ramps, no editor button.
    for kind, node in f.nodes.items():
        assert node.inputNames() == ("value", "fac"), (kind, node.inputNames())
        names = [t.name() for t in node.parmTemplateGroup().entries()]
        assert names[0] == "value" and names[-1] == "fac", (kind, names)
        assert node.parm("edit_curves") is None
        for channel in CHANNELS[kind]:
            template = node.parm("curve_" + channel).parmTemplate()
            assert template.type() == hou.parmTemplateType.Ramp and template.parmType() == hou.rampParmType.Float, (
                kind,
                channel,
            )
        ramp = node.parm("curve_" + CHANNELS[kind][0]).evalAsRamp()
        lo = -1.0 if kind == "vector_curves" else 0.0
        assert ramp.keys() == (0.0, 1.0) and ramp.values() == (lo, 1.0), (kind, ramp.keys(), ramp.values())
        assert (
            node.parmTuple("curve_domain").eval() == (lo, 1.0)
            and node.parm("curve_extension").evalAsString() == "extrapolated"
        )
    assert hou.nodeType(hou.vopNodeTypeCategory(), "cycles::float_ramp::1") is None
    # HDR and negative values survive the native float ramp.
    node = f.nodes["float_curve"]
    node.parm("curve_value").set(hou.Ramp((hou.rampBasis.Linear,) * 3, (0.0, 0.5, 1.0), (-3.5, 12.25, 0.5)))
    assert node.parm("curve_value").evalAsRamp().values() == (-3.5, 12.25, 0.5)
    report["checks"].append(dict(label="Authoring layout, defaults and HDR/negative ramp values"))

    f.bands()
    values = np.float32(np.arange(16) / 15) * np.float32(1.5) - np.float32(0.25)
    session = ch.Session(
        device=args.device,
        width=f.width,
        height=f.height,
        samples=1,
        execution="worker",
        collect_diagnostics=True,
        passes=["combined", "position", "normal"],
    )

    def render(label, expected=None, warning=False, tolerance=4e-5):
        frame = session.wait(session.apply(native_scene.capture(f.rop)))
        status = session.status()
        assert not status["error"] and status["state"] != "failed", status
        assert bool(status["source_diagnostics"]) == warning, (label, status["source_diagnostics"])
        if expected is not None:
            sampled = f.image(frame)[f.height // 2, np.arange(16) * 16 + 8, :3]
            try:
                np.testing.assert_allclose(
                    sampled, np.broadcast_to(expected, sampled.shape), atol=tolerance, rtol=tolerance, err_msg=label
                )
            except AssertionError:
                np.savez(dest / "failure.npz", actual=sampled, expected=expected)
                report["failure_label"] = label
                raise
            error = float(np.abs(sampled - np.broadcast_to(expected, sampled.shape)).max())
        else:
            error = None
        report["checks"].append(dict(label=label, max_error=error, status=status))
        return frame, status

    def expected(case, extrapolate, factor):
        rows = []
        for v in values:
            value = float(v) if case["kind"] == "float_curve" else (float(v),) * 3
            rows.append(
                np.broadcast_to(
                    np.asarray(
                        reference.evaluate(case["kind"], case["channels"], case["domain"], extrapolate, value, factor),
                        float,
                    ),
                    (3,),
                )
            )
        return np.asarray(rows)

    for name, case in CASES.items():
        node = f.nodes[case["kind"]]
        f.connect(node)
        for extrapolate in (True, False):
            reference.apply(node, case["channels"], case["domain"], extrapolate)
            for factor in (1.0, 0.35):
                node.parm("fac").set(factor)
                # Smooth-basis end slopes are numerical estimates on both sides
                # (renderer: local cubic fit; reference: Richardson differences).
                render(
                    f"{name} extrapolate={extrapolate} factor={factor}",
                    expected(case, extrapolate, factor),
                    tolerance=1e-3 if extrapolate else 4e-5,
                )
        node.parm("fac").set(1.0)
    node = f.nodes["float_curve"]
    case = CASES["float_mixed"]
    f.connect(node)
    reference.apply(node, case["channels"], case["domain"], True)
    for factor in (-0.5, 1.5):
        node.parm("fac").set(factor)
        render("Unclamped factor " + str(factor), expected(case, True, factor), tolerance=1e-3)
    node.parm("fac").set(1.0)

    # Float Ramp replacement: Range 0..1, Horizontal, Factor 1 maps Value
    # exactly like the retired node's Factor (hou.Ramp.lookup, held ends).
    ramp = hou.Ramp(
        (hou.rampBasis.Constant, hou.rampBasis.CatmullRom, hou.rampBasis.Linear, hou.rampBasis.Linear),
        (0.1, 0.3, 0.7, 0.9),
        (2.0, 0.4, 1.6, 0.8),
    )
    node.parmTuple("curve_domain").set((0.0, 1.0))
    node.parm("curve_extension").set(1)
    node.parm("curve_value").set(ramp)
    render(
        "Float Ramp replacement matches hou.Ramp.lookup with held ends",
        np.asarray([[native_lookup(ramp, min(max(float(v), 0.0), 1.0), interpolate=True)] * 3 for v in values]),
    )

    # Local recovery: broken stops and Range warn and recover per node.
    f.constant(0.425)
    good = ramp
    for cycle in range(2):
        node.parm("curve_value").set(hou.Ramp((hou.rampBasis.Linear,) * 2, (0.5, 0.5), (0.0, 1.0)))
        render("Coincident stops " + str(cycle), (1, 0, 1), True)
        node.parm("curve_value").set(good)
        render("Repair " + str(cycle), (native_lookup(good, 0.425, interpolate=True),) * 3)
    node.parm("curve_value1value").set(float("nan"))
    render("Nonfinite stop value", (1, 0, 1), True)
    node.parm("curve_value").set(good)
    render("Finite repair", (native_lookup(good, 0.425, interpolate=True),) * 3)
    node.parmTuple("curve_domain").set((1, 0))
    render("Invalid range", (1, 0, 1), True)
    node.parmTuple("curve_domain").set((0, 1))
    render("Range repair", (native_lookup(good, 0.425, interpolate=True),) * 3)
    node.parm("fac").set(0.0)
    node.parmTuple("curve_domain").set((1, 0))
    render("Zero factor skips the curve", (0.425,) * 3)
    node.parmTuple("curve_domain").set((0, 1))
    node.parm("fac").set(1.0)
    node.bypass(True)
    render("Bypass skips curve controls", (0.425,) * 3)
    node.bypass(False)

    # A world graph lowers the same way.
    session.close(wait=True)
    closed = session.status()
    assert closed["owned_geometry_bytes"] == closed["device_allocation_bytes"] == 0, closed
    world = ch.create_world_material(name="curve_world")
    world_node = world.createNode("cycles::rgb_curves::1")
    case = CASES["rgb_mixed"]
    reference.apply(world_node, case["channels"], case["domain"], True)
    world_node.parmTuple("value").set((0.425,) * 3)
    world.node("background_shader").parm("strength").set(1)
    world.node("background_shader").setNamedInput("color", world_node, "value")
    with ch.Session(device=args.device, width=64, height=64, samples=1, execution="worker") as world_session:
        frame = world_session.wait(
            world_session.apply(dict(world=dict(graph=ch.material_graph(world)), background=(0.07, 0.09, 0.11)))
        )
        plane = frame["planes"]["combined"]
        pixels = np.frombuffer(plane["pixels"], np.float32).reshape(64, 64, plane["channels"])
        np.testing.assert_allclose(
            pixels[32, 32, :3],
            reference.evaluate("rgb_curves", case["channels"], case["domain"], True, (0.425,) * 3),
            atol=4e-5,
            rtol=4e-5,
        )
        report["checks"].append(dict(label="World RGB Curves"))

    # Final frames with animated stops: one session, a recoverable broken frame.
    f.connect(node)
    f.constant(0.425)
    node.parm("curve_value").set(hou.Ramp((hou.rampBasis.Linear,) * 3, (0.0, 0.5, 1.0), (0.0, 0.5, 1.0)))
    node.parmTuple("curve_domain").set((0.0, 1.0))
    node.parm("curve_extension").set(0)
    node.parm("curve_value2value").setExpression(".25+.05*$F", hou.exprLanguage.Hscript)
    node.parm("curve_value3pos").setExpression("if($F==3,.5,1)", hou.exprLanguage.Hscript)
    f.rop.setParms(dict(picture=str(dest / "curve.$F4.exr"), persistent_data=True, use_denoise=False, samples=1))
    expected_frames = [
        (1, 0, 1) if frame == 3 else ((0.425 / 0.5) * (0.25 + 0.05 * frame),) * 3 for frame in range(1, 6)
    ]
    f.rop.render(frame_range=(1, 5, 1))
    summary = f.rop.cachedUserData(final_render._summary_key)
    assert summary["completed_frames"] == 5 and summary["created_sessions"] == 1 and not summary["session_alive"], (
        summary
    )
    for frame in range(1, 6):
        reader = oiio.ImageInput.open(str(dest / f"curve.{frame:04}.exr"))
        assert reader
        warnings = json.loads(reader.spec().get_string_attribute("cycles_houdini:source_diagnostics") or "[]")
        pixels = reader.read_image(format=oiio.FLOAT)
        reader.close()
        assert bool(warnings) == (frame == 3), warnings
        np.testing.assert_allclose(
            pixels[f.height // 2, f.width // 2, :3], expected_frames[frame - 1], atol=5e-5, rtol=4e-5
        )
    report.update(result="passed", batch_frames=5, batch_summary=summary, closed=closed)
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    if session:
        session.close()
    (dest / "report.json").write_text(json.dumps(report, indent=2))
print(json.dumps(dict(result=report["result"], checks=len(report["checks"]), batch_frames=report.get("batch_frames"))))
