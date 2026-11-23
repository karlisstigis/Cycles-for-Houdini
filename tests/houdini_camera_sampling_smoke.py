# SPDX-License-Identifier: Apache-2.0
"""Rolling/weighted exposure, local source repair and real five-frame output."""

from support.paths import artifacts_root
from native_test_runtime import runtime_executable
import pass_fixtures as pass_fixture

import argparse, json, os, subprocess, time, traceback
from pathlib import Path
import hou, numpy as np, OpenImageIO as oiio
from cycles_houdini import native_scene, registry, camera_properties, final_render
from camera_sampling_fixture import Fixture, reference

p = argparse.ArgumentParser()
p.add_argument("--device", default="CPU")
device = p.parse_args().device
root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
dest = artifacts_root() / "stage2/cameras/sampling"
dest.mkdir(parents=True, exist_ok=True)
report = dict(result="running", device=device, checks=[])
f = Fixture(device)
entry = registry.for_rop(f.rop, native_scene.configuration(f.rop))
entry.session.close()
entry.session = registry.session_from_configuration(native_scene.configuration(f.rop), collect_diagnostics=True)


def render(
    label, shape="uniform", rolling=False, duration=0.1, offset=0, steps=3, codes=(), camera_only=False, camera_x=0
):
    pass_fixture.set_parameters(f.rop, dict(shutteroffset=offset, motion_samples=steps))
    before = entry.session.status()
    frame = entry.session.wait(entry.apply(native_scene.capture(f.rop, camera_only=camera_only), hou.time()))
    status = entry.session.status()
    deadline = time.monotonic() + 3
    while status["state"] != "complete" and time.monotonic() < deadline:
        time.sleep(0.005)
        status = entry.session.status()
    plane = frame["planes"]["combined"]
    pixels = np.frombuffer(plane["pixels"], np.float32).reshape(96, 160, plane["channels"])[:, :, :3]
    expected = reference(shape, rolling, duration, offset, camera_x=camera_x)
    error = float(np.abs(pixels - expected).mean())
    record = dict(
        label=label,
        mean_difference=error,
        shape=shape,
        rolling=rolling,
        duration=duration,
        offset=offset,
        steps=steps,
        status=status,
        previous=before,
        diagnostics=entry.session.diagnostics(),
    )
    report["checks"].append(record)
    assert (
        status["state"] == "complete"
        and status["geometry_resources"] == status["object_resources"] == 1
        and not status["motion_warnings"]
    ), status
    assert {d["code"] for d in status["source_diagnostics"]} == set(codes), status
    if not np.isfinite(pixels).all() or error >= 0.0012:
        np.savez(dest / "failed-image.npz", expected=expected, actual=pixels)
        raise AssertionError((label, error))
    # Every row must conserve this emitter's radiance/coverage, independently
    # of exposure shape, scan duration and scan position.
    assert np.max(np.abs(pixels[:, :, 2].sum(axis=1) - 3.2)) < 0.08, (
        label,
        pixels[:, :, 2].sum(axis=1).min(),
        pixels[:, :, 2].sum(axis=1).max(),
    )
    if camera_only:
        assert all(
            status[k] == before[k] for k in ("extractions", "geometry_conversions", "subdivision_tessellations")
        ), status
        if status["render_revision"] != before["render_revision"]:
            assert not {"tri_verts", "tri_vindex"} & record["diagnostics"]["buffer_copy_requests"].keys()
    return status


try:
    contracts = subprocess.run(
        [
            str(runtime_executable("ch_harness.exe")),
            "--resources",
            os.environ["CYCLES_HOUDINI_RESOURCES"],
            "--device",
            device,
            "--camera-sampling-contracts",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert contracts.returncode == 0, (contracts.stdout, contracts.stderr)
    report["contracts"] = json.loads(contracts.stdout.strip().splitlines()[-1])
    # Upgrading an old opt-in camera tab preserves expressions and native parms.
    original = pass_fixture.parm(f.camera, "cycles_focus_distance").expression()
    group = f.camera.parmTemplateGroup()
    group.remove("cycles_camera_sampling")
    f.camera.setParmTemplateGroup(group)
    assert (
        camera_properties.install(f.camera)
        and pass_fixture.parm(f.camera, "cycles_focus_distance").expression() == original
    )
    assert not camera_properties.install(f.camera)
    assert all(v == 1 for v in pass_fixture.parm(f.camera, "cycles_shutter_curve").eval().values())
    pass_fixture.parm(f.camera, "cycles_shutter_curve").set(hou.Ramp((hou.rampBasis.Linear,) * 2, (0, 1), (0, 1)))
    pass_fixture.parm(f.camera, "cycles_shutter_curve").revertToRampDefaults()
    assert all(v == 1 for v in pass_fixture.parm(f.camera, "cycles_shutter_curve").eval().values())
    for shape in ("uniform", "triangle", "rise", "fall"):
        for rolling, duration in ((False, 0.1), (True, 0), (True, 0.25), (True, 1)):
            f.set_curve(shape)
            f.set_rolling(rolling, duration)
            for steps in (3, 5):
                for offset in (-0.75, 0, 0.75):
                    render(shape, shape, rolling, duration, offset, steps)
    f.set_curve("negative")
    f.set_rolling(True, 0)
    render("Pure scan ignores an unused invalid exposure curve", rolling=True, duration=0)
    f.set_curve("triangle")
    f.set_rolling(True, 0.25)
    render("Before live source edits", "triangle", True, 0.25)
    f.set_curve("negative")
    render(
        "Negative curve keeps rolling and motion",
        rolling=True,
        duration=0.25,
        codes=("camera.invalid_shutter_curve",),
        camera_only=True,
    )
    f.set_curve("zero")
    render(
        "Zero exposure keeps rolling and motion",
        rolling=True,
        duration=0.25,
        codes=("camera.invalid_shutter_curve",),
        camera_only=True,
    )
    f.set_curve("triangle")
    render("Curve repair clears its warning", "triangle", True, 0.25, camera_only=True)
    pass_fixture.parm(f.camera, "cycles_rolling_duration").setExpression("float('nan')", hou.exprLanguage.Python)
    render(
        "Invalid scan duration retains valid shaped exposure",
        "triangle",
        codes=("camera.invalid_rolling_shutter",),
        camera_only=True,
    )
    f.set_rolling(True, 0.25)
    render("Scan repair retains shaped exposure", "triangle", True, 0.25, camera_only=True)
    pass_fixture.parm(f.camera, "cycles_rolling_duration").setExpression(".25+$T*24", hou.exprLanguage.Hscript)
    render(
        "Animated scan holds requested settings",
        "triangle",
        True,
        0.25,
        codes=("camera.shutter_rolling",),
        camera_only=True,
    )
    f.set_rolling(True, 0.25)
    render("Animated scan repair", "triangle", True, 0.25, camera_only=True)
    f.set_curve("uniform")
    pass_fixture.parm(f.camera, "cycles_use_shutter_curve").set(1)
    pass_fixture.parm(f.camera, "cycles_shutter_curve1value").setExpression("1+$T*24", hou.exprLanguage.Hscript)
    render(
        "Animated ramp holds requested exposure",
        rolling=True,
        duration=0.25,
        codes=("camera.shutter_curve",),
        camera_only=True,
    )
    pass_fixture.parm(f.camera, "cycles_shutter_curve1value").deleteAllKeyframes()
    pass_fixture.parm(f.camera, "cycles_shutter_curve1value").set(1)
    render("Ramp channel repair clears its warning", rolling=True, duration=0.25, camera_only=True)
    f.set_curve("triangle")
    pass_fixture.parm(f.camera, "tx").set(0.05)
    render("Camera navigation retains geometry and exposure", "triangle", True, 0.25, camera_only=True, camera_x=0.05)
    pass_fixture.parm(f.camera, "tx").set(0)
    render("Camera navigation repair", "triangle", True, 0.25, camera_only=True)
    # Known bad optional branches stay independent; vector-only/disabled blur
    # does not acquire their sources at all.
    f.set_curve("negative")
    pass_fixture.parm(f.camera, "cycles_rolling_duration").setExpression("float('nan')", hou.exprLanguage.Python)
    render(
        "Both optional branches recover independently",
        codes=("camera.invalid_shutter_curve", "camera.invalid_rolling_shutter"),
        camera_only=True,
    )
    pass_fixture.parm(f.rop, "allowmotionblur").set(0)
    pass_fixture.parm(f.rop, "pass_motion").set(1)
    update = native_scene.capture(f.rop)
    assert not update["camera_sampling"]["source_diagnostics"] and not update["camera_sampling"]["shutter_curve"]
    frame = entry.session.wait(entry.apply(update, hou.time()))
    assert not entry.session.status()["source_diagnostics"]
    plane = frame["planes"]["motion"]
    vectors = np.frombuffer(plane["pixels"], np.float32).reshape(96, 160, 4)
    assert np.max(np.abs(vectors[30:60, 79:81] - np.array((-20, 0, -20, 0)))) < 0.01, vectors[48, 80]
    pass_fixture.set_parameters(f.rop, dict(allowmotionblur=1, pass_motion=0))
    f.set_curve("triangle")
    f.set_rolling(True, 0.25)
    render("Restored exposure after vector-only mode", "triangle", True, 0.25)
    warm = []
    for index in range(10):
        pass_fixture.parm(f.camera, "cycles_rolling_duration").set(0.25 if index % 2 else 0.5)
        status = render(
            "Warm exposure edit " + str(index), "triangle", True, 0.25 if index % 2 else 0.5, camera_only=True
        )
        warm.append(
            {k: status[k] for k in ("owned_geometry_bytes", "device_allocation_bytes", "engine_shader_resources")}
        )
    for k in warm[0]:
        assert len({row[k] for row in warm[4:]}) == 1, (k, warm)
    report["warm_resources"] = warm
    registry.close_all()
    f.set_curve("uniform")
    f.set_rolling(True, 0.25)
    pass_fixture.parm(f.sop, "snippet").set("@P.x+=(@Time-float(floor(@Frame-.5))/24.0)*12;")
    pass_fixture.parm(f.camera, "cycles_rolling_duration").setExpression(
        "float('nan') if int(hou.frame()+.5)==2 else .25", hou.exprLanguage.Python
    )
    pass_fixture.parm(f.camera, "cycles_use_shutter_curve").set(1)
    pass_fixture.parm(f.camera, "cycles_shutter_curve1value").setExpression(
        "if(rint($FF)==3,-1,if(rint($FF)==4,0,1))", hou.exprLanguage.Hscript
    )
    pass_fixture.parm(f.camera, "cycles_shutter_curve2value").setExpression(
        "if(rint($FF)==5,0,1)", hou.exprLanguage.Hscript
    )
    pass_fixture.set_parameters(f.rop, dict(persistent_data=1, picture=str(dest / "frame.$F4.exr")))
    f.rop.render(frame_range=(1, 5, 1))
    summary = f.rop.cachedUserData(final_render._summary_key)
    assert summary["completed_frames"] == 5 and summary["created_sessions"] == 1 and not summary["session_alive"], (
        summary
    )
    report["batch"] = []
    for index in range(1, 6):
        image = oiio.ImageBuf(str(dest / f"frame.{index:04d}.exr"))
        channels = list(image.spec().channelnames)
        pixels = np.flipud(image.get_pixels()[:, :, [channels.index(c) for c in ("R", "G", "B")]])
        shape = "rise" if index == 4 else "fall" if index == 5 else "uniform"
        error = float(np.abs(pixels - reference(shape, rolling=index != 2, duration=0.25)).mean())
        assert error < 0.0012, (index, error)
        issues = json.loads(image.spec().get_string_attribute("cycles_houdini:source_diagnostics") or "[]")
        assert {d["code"] for d in issues} == (
            {"camera.invalid_rolling_shutter"}
            if index == 2
            else {"camera.invalid_shutter_curve"}
            if index == 3
            else set()
        ), (index, issues)
        report["batch"].append(dict(frame=index, mean_difference=error, diagnostics=issues))
    report.update(result="passed", final=summary)
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    registry.close_all()
    (dest / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(dict(result=report["result"], checks=len(report["checks"]))))
