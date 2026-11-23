# SPDX-License-Identifier: Apache-2.0
"""Native nonlinear ramps: per-ray CPU/GPU references, recovery and final frames."""

from ramp_table_reference import native_lookup
from support.paths import artifacts_root
from cycles_houdini.device_selection import set_devices as set_rop_devices
import argparse, json, os, traceback
from pathlib import Path
import hou
import numpy as np
import OpenImageIO as oiio
import cycles_houdini as ch
from cycles_houdini import native_scene, final_render

device = argparse.ArgumentParser()
device.add_argument("--device", default="CPU")
device = device.parse_args().device
dest = artifacts_root() / "stage3/materials/ramp-interpolation"
dest.mkdir(parents=True, exist_ok=True)
report = dict(result="running", device=device, checks=[])
bases = (
    hou.rampBasis.Constant,
    hou.rampBasis.Linear,
    hou.rampBasis.CatmullRom,
    hou.rampBasis.MonotoneCubic,
    hou.rampBasis.Bezier,
    hou.rampBasis.BSpline,
    hou.rampBasis.Hermite,
)
spaces = (hou.colorType.RGB, hou.colorType.LAB)
keys = (0.05, 0.12, 0.3, 0.45, 0.67, 0.85, 0.95)
values = (
    (0.2, 0.4, 0.8),
    (0.8, 0.2, 0.1),
    (0.1, 0.9, 0.4),
    (0.6, 0.3, 0.2),
    (0.9, 0.2, 0.6),
    (0.2, 0.7, 0.3),
    (0.7, 0.1, 0.4),
)
builder = ch.create_material(name="interpolation", shader="emission")
emission = builder.node("surface_shader")
emission.parm("strength").set(1)
color = builder.createNode("cycles::color_ramp::1", "native_color")
scalar = builder.createNode("cycles::float_curve::1", "native_scalar")
# Float Curve replaces Float Ramp: Range 0..1, Horizontal, Factor 1 map Value like Float Ramp's Factor.
scalar.parmTuple("curve_domain").set((0.0, 1.0))
scalar.parm("curve_extension").set(1)
scalar.parm("fac").set(1.0)
color.parm("ramp_interpolation").set("NATIVE")
emission.setNamedInput("color", color, "color")
obj = hou.node("/obj").createNode("geo", "receiver", run_init_scripts=False)
grid = obj.createNode("grid")
grid.setParms(dict(type=0, orient=0, rows=2, cols=2))
grid.parmTuple("size").set((8, 8))
grid.setRenderFlag(True)
camera = hou.node("/obj").createNode("cam", "reference_camera")
camera.setParms(dict(tz=5, projection=1, orthowidth=4))
rop = hou.node("/out").createNode("cycles_ipr", "ramp_interpolation")
set_rop_devices(rop, device)
rop.setParms(dict(soppath=grid.path(), camera=camera.path(), materialpath=builder.path(), resx=256, resy=32, samples=1))
rop.parmTuple("background").set((0, 0, 0))
rop.parmTuple("lightstrength").set((0, 0, 0))
# Sixteen wide constant-factor bands exercise the SVM node with varying input,
# while the middle of each band avoids filter/AA integration ambiguity.
geometry = builder.createNode("cycles::geometry::1")
separate = builder.createNode("cycles::separate_xyz::1")
separate.setNamedInput("vector", geometry, "position")
previous = separate
socket = "x"
for operation, constant in (("divide", 4.0), ("add", 0.5), ("multiply", 16.0), ("floor", 0.0), ("divide", 15.0)):
    node = builder.createNode("cycles::math::1")
    node.parm("math_type").set(operation)
    node.parm("value2").set(constant)
    node.setNamedInput("value1", previous, socket)
    previous = node
    socket = "value"
color.setNamedInput("factor", previous, "value")
scalar.setNamedInput("value", previous, "value")
session = ch.Session(device=device, width=256, height=32, samples=1, collect_diagnostics=True)


def render(label, ramp=None, warning=False):
    frame = session.wait(session.apply(native_scene.capture(rop)))
    status = session.status()
    plane = frame["planes"]["combined"]
    image = np.frombuffer(plane["pixels"], np.float32).reshape(32, 256, plane["channels"])
    assert np.isfinite(image).all() and status["state"] != "Failed", status
    if ramp is not None:
        expected = np.array(
            [
                native_lookup(ramp, float(np.float32(i / 15)), interpolate=True if ramp.isColor() is False else None)
                for i in range(16)
            ]
        )
        if expected.ndim == 1:
            expected = np.repeat(expected[:, None], 3, axis=1)
        measured = image[16, np.arange(16) * 16 + 8, :3]
        np.testing.assert_allclose(measured, expected, atol=3.0e-5, rtol=3.0e-5)
        error = float(np.abs(measured - expected).max())
    else:
        error = None
        if warning:
            np.testing.assert_allclose(image[16, 128, :3], (1, 0, 1), atol=0.001, rtol=0)
    assert bool(status["source_diagnostics"]) == warning, status["source_diagnostics"]
    if report["checks"]:
        assert status["geometry_conversions"] == report["checks"][0]["status"]["geometry_conversions"], status
    report["checks"].append(dict(label=label, max_error=error, status=status))
    return frame


try:
    for space in spaces:
        color.parm("ramp_color_space").set(str(space).split(".")[-1])
        for a in range(7):
            for mixed in (False, True):
                bs = tuple(bases[(a + 1) % 7] if mixed and i % 2 else bases[a] for i in range(7))
                ramp = hou.Ramp(bs, keys, values)
                ramp.setColorType(space)
                color.parm("ramp").set(ramp)
                expected = color.parm("ramp").evalAsRamp()
                expected.setColorType(space)
                render("Color " + str(space) + " " + str(a) + " mixed=" + str(mixed), expected)
    emission.setNamedInput("color", scalar, "value")
    for basis in bases:
        ramp = hou.Ramp((basis,) * 7, keys, (0.2, 1.3, 0.4, 0.7, 2.1, 0.3, 0.9))
        scalar.parm("curve_value").set(ramp)
        render("Scalar " + str(basis), scalar.parm("curve_value").evalAsRamp())
    # A malformed authored source is recoverable; the accepted scene keeps its
    # mesh and clears its warning after repair without a SOP conversion.
    good = scalar.parm("curve_value").evalAsRamp()
    for cycle in range(3):
        scalar.parm("curve_value").set(hou.Ramp((hou.rampBasis.Linear,) * 2, (0.5, 0.5), (1, 2)))
        render("Broken knots " + str(cycle), warning=True)
        scalar.parm("curve_value").set(good)
        render("Repair " + str(cycle), scalar.parm("curve_value").evalAsRamp())
    # Ownership and schema violations remain strict engine errors.
    graph = ch.material_graph(builder)
    private = next(n for n in graph["nodes"] if n["definition"] == "cycles/float_curve/1")
    private["parameters"]["curve_source"] = [1]
    try:
        session.apply(dict(materials=[dict(key="bad_program", kind="graph", graph=graph)]))
    except (ValueError, RuntimeError) as error:
        assert "curve" in str(error).lower(), str(error)
    else:
        raise AssertionError("Malformed host curve controls were accepted")
    report["malformed_curve_source_rejected"] = True
    session.close()
    assert session.status()["device_allocation_bytes"] == session.status()["owned_geometry_bytes"] == 0
    # Final ROP output: five frames with changing nonlinear controls and a
    # recoverable broken frame. Persistent Data must retain one ordered session.
    rop.parm("picture").set(str(dest / "ramp.$F4.exr"))
    rop.setParms(dict(samples=1, persistent_data=True, use_denoise=False))
    scalar.setInput(scalar.inputIndex("value"), None)
    scalar.parm("value").set(0.425)
    scalar.parm("curve_value").set(hou.Ramp((hou.rampBasis.CatmullRom,) * 7, keys, (0.2, 1.3, 0.4, 0.7, 2.1, 0.3, 0.9)))
    for i, v in enumerate((0.2, 1.3, 0.4, 0.7, 2.1, 0.3, 0.9), 1):
        scalar.parm("curve_value" + str(i) + "value").setExpression(str(v) + "*$F", hou.exprLanguage.Hscript)
    scalar.parm("curve_value4pos").setExpression("if($F==3,0.3,0.45)", hou.exprLanguage.Hscript)
    expected = []
    for frame in range(1, 6):
        hou.setFrame(frame)
        bad = frame == 3
        expected.append(
            (1, 0, 1) if bad else (native_lookup(scalar.parm("curve_value").evalAsRamp(), 0.425, interpolate=True),) * 3
        )
    rop.render(frame_range=(1, 5, 1))
    summary = rop.cachedUserData(final_render._summary_key)
    assert summary["completed_frames"] == 5 and summary["created_sessions"] == 1 and not summary["session_alive"], (
        summary
    )
    for frame in range(1, 6):
        path = dest / f"ramp.{frame:04}.exr"
        reader = oiio.ImageInput.open(str(path))
        assert reader, path
        issues = json.loads(reader.spec().get_string_attribute("cycles_houdini:source_diagnostics") or "[]")
        assert bool(issues) == (frame == 3), issues
        pixels = reader.read_image(format=oiio.FLOAT)
        reader.close()
        np.testing.assert_allclose(pixels[16, 128, :3], expected[frame - 1], atol=4.0e-5, rtol=4.0e-5)
    report.update(result="passed", batch_frames=5, batch_summary=summary, closed=session.status(), scope=__doc__)
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    session.close()
    (dest / "report.json").write_text(json.dumps(report, indent=2))
print(json.dumps(dict(result=report["result"], checks=len(report["checks"]), batch=report.get("batch_frames"))))
