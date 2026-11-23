# SPDX-License-Identifier: Apache-2.0
"""Missing shader fields must not discard independently valid mesh motion."""

from support.paths import artifacts_root
from subdivision_test_setup import configure_render, set_subdivision
import argparse, json, os, time, traceback
from pathlib import Path
import hou, numpy as np, OpenImageIO as oiio
import cycles_houdini as ch
from cycles_houdini import native_scene, registry, final_render
from shutter_coordinate_recovery_fixture import Fixture, CASES
from independent_binding_fixture import motion_pixels

p = argparse.ArgumentParser()
p.add_argument("--device", default="CPU")
device = p.parse_args().device
root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
dest = artifacts_root() / "stage3/materials/recovery/shutter-coordinates"
dest.mkdir(parents=True, exist_ok=True)
f = Fixture(device)
entry = registry.for_rop(f.rop, native_scene.configuration(f.rop))
report = dict(result="running", device=device, checks=[])


def render(label, broken=0, samples=3, offset=0, *, objects=1, retain=False):
    before = entry.session.status()
    configure_render(f.rop, dict(motion_samples=samples, shutteroffset=offset))
    if f.parm.eval() != broken:
        f.edit(broken)
    frame = entry.session.wait(entry.apply(native_scene.capture(f.rop), hou.time()))
    status = entry.session.status()
    deadline = time.monotonic() + 3
    while status["state"] != "complete" and time.monotonic() < deadline:
        assert status["state"] != "failed", status
        time.sleep(0.005)
        status = entry.session.status()
    plane = frame["planes"]["combined"]
    pixels = np.frombuffer(plane["pixels"], np.float32).reshape(90, 160, plane["channels"])[:, :, :3]
    expected = f.expected_pixels(broken, samples, offset)
    difference = float(np.abs(pixels - expected).mean())
    codes = {issue["code"] for issue in status["source_diagnostics"]}
    report["checks"].append(
        dict(
            label=label,
            case=f.case,
            broken=broken,
            samples=samples,
            offset=offset,
            mean_difference=difference,
            status=status,
            previous=before,
        )
    )
    assert (
        status["state"] == "complete" and status["geometry_resources"] == 1 and status["object_resources"] == objects
    ), status
    assert not status["motion_warnings"] and not any(code.startswith("motion.displacement_") for code in codes), status
    assert bool(codes) == bool(broken), (label, codes)
    assert np.isfinite(pixels).all() and difference < 0.0008, (label, difference, codes)
    if retain:
        assert all(
            status[key] == before[key] for key in ("geometry_conversions", "extractions", "displacement_rebuilds")
        ), status
    return status, pixels


try:
    for case in CASES:
        f.configure(case)
        for samples in (3, 5):
            render(case + " valid geometry and displacement motion", samples=samples)
            for broken in (1, 0, 2, 0):
                render(case + " shutter field break/repair " + str(broken), broken, samples)
    for case in ("uv", "color", "named"):
        for nominal in (False, True):
            f.configure(case, nominal=nominal)
            for offset in (-0.75, 0, 0.75):
                for broken in (1, 0, 2, 0):
                    render(
                        case + (" requested-frame" if nominal else " shutter") + " source recovery",
                        broken,
                        offset=offset,
                    )
    f.configure("named", scale_animation=True)
    for samples in (3, 5):
        for broken in (0, 1, 0, 2, 0):
            render("Independent shader animation survives a held field", broken, samples)
    for scheme in ("linear", "catmull_clark"):
        for case in ("uv", "color", "named"):
            f.configure(case)
            configure_render(f.rop, dict(subdivision=dict(type=scheme, space="object", dicing_rate=0.2, max_level=3)))
            for broken in (0, 1, 0):
                render(scheme + " " + case + " field recovery retains render subdivision and motion", broken)
    targets = f.obj.createNode("line", "two_targets")
    targets.setParms(dict(points=2, dist=5))
    targets.parmTuple("dir").set((1, 0, 0))
    copies = f.obj.createNode("copytopoints", "packed_field_recovery")
    copies.setInput(0, f.switch)
    copies.setInput(1, targets)
    copies.parm("pack").set(1)
    copies.setDisplayFlag(True)
    copies.setRenderFlag(True)
    for case in ("uv", "named"):
        f.configure(case)
        for offset in (-0.75, 0.75):
            for broken in (0, 1, 0):
                render(
                    "Packed " + case + " holds compatible fields and retains motion", broken, offset=offset, objects=2
                )
    f.switch.setDisplayFlag(True)
    f.switch.setRenderFlag(True)
    f.configure("uv")
    render("Before cached navigation with a recovered field", 1)
    camera = hou.node(f.rop.evalParm("camera"))
    camera.parm("tx").set(0.05)
    render("Recovered field camera edit retains geometry", 1, retain=True)
    camera.parm("tx").set(0)
    render("Recovered field camera restoration retains geometry", 1, retain=True)
    f.obj.parm("tx").set(-0.45)
    render("Recovered field object edit retains geometry", 1, retain=True)
    f.obj.parm("tx").set(-0.5)
    render("Recovered field object restoration retains geometry", 1, retain=True)
    warm = []
    for index in range(12):
        status, _ = render("Repeated field failure and repair " + str(index), index % 2)
        warm.append(
            {key: status[key] for key in ("owned_geometry_bytes", "device_allocation_bytes", "engine_shader_resources")}
        )
    for parity in (0, 1):
        for key in warm[0]:
            assert len({row[key] for row in warm[6 + parity :: 2]}) == 1, (key, warm)
    report["warm_resources"] = warm
    # Compare the same recovery in a direct final session, then render a genuine
    # ordered five-frame ROP sequence with a failure only on frame three.
    f.configure("uv")
    with ch.Session(
        device=device, width=160, height=90, samples=1024, execution="sync", interactive=False, passes=["combined"]
    ) as final:
        for broken in (0, 1, 0):
            f.edit(broken)
            frame = final.wait(final.apply(native_scene.capture(f.rop), at_time=hou.time()))
            values = np.frombuffer(frame["planes"]["combined"]["pixels"], np.float32).reshape(90, 160, 4)
            assert float(np.abs(values[:, :, :3] - f.expected_pixels(broken)).mean()) < 0.0008
            assert not final.status()["error"] and bool(final.status()["source_diagnostics"]) == bool(broken)
    registry.close_all()
    f.configure("uv")
    copies.setDisplayFlag(True)
    copies.setRenderFlag(True)
    f.switch.parm("input").setExpression("if($FF>2.7 && $FF<3,1,0)", hou.exprLanguage.Hscript)
    camera.parm("tx").setExpression("rint($T*24)*3", hou.exprLanguage.Hscript)
    configure_render(f.rop, dict(motion_samples=3, persistent_data=1, picture=str(dest / "frame.$F4.exr")))
    f.rop.render(frame_range=(1, 5, 1))
    summary = f.rop.cachedUserData(final_render._summary_key)
    assert summary["completed_frames"] == 5 and summary["created_sessions"] == 1 and not summary["session_alive"], (
        summary
    )
    batch = []
    for index in range(1, 6):
        image = oiio.ImageBuf(str(dest / f"frame.{index:04d}.exr"))
        pixels = image.get_pixels()
        channels = list(image.spec().channelnames)
        expected = motion_pixels((-0.5, 0, 0.5)) if index == 3 else motion_pixels((-0.75, 0, 0.75))
        error = float(np.abs(pixels[:, :, [channels.index(c) for c in ("R", "G", "B")]] - expected).mean())
        assert error < 0.0008, (index, error)
        issues = json.loads(image.spec().get_string_attribute("cycles_houdini:source_diagnostics") or "[]")
        assert bool(issues) == (index == 3) and not any(
            issue["code"].startswith("motion.displacement_") for issue in issues
        ), (index, issues)
        batch.append(dict(frame=index, mean_difference=error, diagnostics=issues))
    report.update(batch=batch, final=summary, direct_final_states=3)
    report["result"] = "passed"
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    registry.close_all()
    (dest / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(dict(result=report["result"], checks=len(report["checks"]))))
