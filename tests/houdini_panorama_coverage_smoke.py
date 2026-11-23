# SPDX-License-Identifier: Apache-2.0
"""Native panoramic images, camera motion, source repair, picking and real output."""

from support.paths import artifacts_root
from native_test_runtime import runtime_executable
from subdivision_test_setup import configure_render, set_subdivision
import pass_fixtures as pass_fixture

import argparse, json, os, subprocess, time, traceback
from pathlib import Path
import hou, numpy as np, OpenImageIO as oiio
import cycles_houdini as ch
from cycles_houdini import native_scene, registry, camera_tools, camera_properties, final_render
from panorama_fixture import (
    Fixture,
    MODELS,
    reference,
    ray_pixels,
    motion_reference,
    motion_vectors,
    vector_reference,
    dof_reference,
)

p = argparse.ArgumentParser()
p.add_argument("--device", default="CPU")
device = p.parse_args().device
root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
dest = artifacts_root() / "stage2/cameras/panorama"
dest.mkdir(parents=True, exist_ok=True)
report = dict(result="running", device=device, checks=[])
f = Fixture(device)
entry = None


def render(label, *, expected=None, codes=(), camera_only=False, crop=None, data=False):
    global entry
    previous_entry = entry
    entry = registry.for_rop(f.rop, native_scene.configuration(f.rop))
    if getattr(entry, "panorama_diagnostic_session", None) is not entry.session:
        entry.session.close()
        entry.session = registry.session_from_configuration(entry.config, collect_diagnostics=True)
        entry.panorama_diagnostic_session = entry.session
    before = entry.session.status()
    update = native_scene.capture(f.rop, camera_only=camera_only)
    if crop is not None:
        update["crop"] = crop
    frame = entry.session.wait(entry.apply(update, hou.time()))
    status = entry.session.status()
    deadline = time.monotonic() + 3
    while status["state"] != "complete" and time.monotonic() < deadline:
        time.sleep(0.005)
        status = entry.session.status()
    plane = frame["planes"]["combined"]
    pixels = np.frombuffer(plane["pixels"], np.float32).reshape(frame["height"], frame["width"], plane["channels"])[
        :, :, :3
    ]
    expected = reference(f.spec, frame["full_width"], frame["full_height"]) if expected is None else expected
    expected = expected[frame["y"] : frame["y"] + frame["height"], frame["x"] : frame["x"] + frame["width"]]
    error = float(np.abs(pixels - expected).mean())
    diagnostics = entry.session.diagnostics()
    report["checks"].append(
        dict(label=label, mean_difference=error, spec=f.spec, status=status, previous=before, diagnostics=diagnostics)
    )
    assert (
        status["state"] == "complete"
        and status["geometry_resources"] == status["object_resources"] == 6
        and not status["motion_warnings"]
    ), status
    assert {d["code"] for d in status["source_diagnostics"]} == set(codes), status
    if not np.isfinite(pixels).all() or error >= 0.0012:
        np.savez(dest / "failed-image.npz", expected=expected, actual=pixels)
        raise AssertionError((label, error))
    if camera_only:
        assert all(
            status[k] == before[k] for k in ("geometry_conversions", "extractions", "subdivision_tessellations")
        ), status
        if status["render_revision"] != before["render_revision"]:
            assert not {"tri_verts", "tri_vindex"} & diagnostics["buffer_copy_requests"].keys(), diagnostics
    if data:
        y, x = np.indices((frame["height"], frame["width"]))
        u = (x + frame["x"] + 0.5) / frame["full_width"]
        v = (y + frame["y"] + 0.5) / frame["full_height"]
        rgb, point, depth, valid = ray_pixels(f.spec, u, v, frame["full_width"], frame["full_height"])
        stable = valid & (np.max(np.abs(pixels - rgb), axis=-1) < 1e-5)
        assert stable.sum() > pixels.shape[0] * pixels.shape[1] * 0.25, label
        actual_point = np.frombuffer(frame["planes"]["position"]["pixels"], np.float32).reshape(
            frame["height"], frame["width"], 3
        )
        actual_depth = np.frombuffer(frame["planes"]["depth"]["pixels"], np.float32).reshape(
            frame["height"], frame["width"]
        )
        pe = float(np.quantile(np.linalg.norm(actual_point[stable] - point[stable], axis=-1), 0.95))
        de = float(np.quantile(np.abs(actual_depth[stable] - depth[stable]), 0.95))
        assert pe < 0.025 and de < 0.025, (label, pe, de)
        report["checks"][-1]["data_errors"] = dict(position_p95=pe, depth_p95=de)
    return frame


try:
    assert all(
        ch.capabilities()[name]
        for name in ("native_panoramic_cameras", "native_panoramic_motion_vectors", "native_panoramic_depth_of_field")
    )
    run = subprocess.run(
        [
            str(runtime_executable("ch_harness.exe")),
            "--resources",
            os.environ["CYCLES_HOUDINI_RESOURCES"],
            "--device",
            device,
            "--panorama-contracts",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert run.returncode == 0, (run.stdout, run.stderr)
    report["contracts"] = json.loads(run.stdout.strip().splitlines()[-1])
    pass_fixture.parm(f.camera, "cycles_focus_distance").set(7)
    group = f.camera.parmTemplateGroup()
    group.remove("cycles_camera_lens")
    f.camera.setParmTemplateGroup(group)
    assert camera_properties.install(f.camera) and pass_fixture.evaluate(f.camera, "cycles_focus_distance") == 7
    assert not camera_properties.install(f.camera)
    for model in MODELS:
        f.configure(model)
        render(model, data=True)
    cases = [
        ("equirectangular", dict(longitude_min=0, longitude_max=360)),
        ("equirectangular", dict(longitude_min=220, longitude_max=400, latitude_min=-40, latitude_max=55)),
        ("central_cylindrical", dict(u_min=0, u_max=360)),
        ("central_cylindrical", dict(u_min=-60, u_max=120, v_min=-2, v_max=1, radius=2)),
        ("fisheye_equidistant", dict(fov=90)),
        ("fisheye_equidistant", dict(fov=270)),
        ("fisheye_equisolid", dict(fov=270, lens=7)),
        ("fisheye_lens_polynomial", dict(coefficients=(0, -0.095238095, 0, 0, 0))),
        ("fisheye_lens_polynomial", dict(coefficients=(0, 0, -0.005, 0, 0))),
        ("fisheye_lens_polynomial", dict(coefficients=(0, 0, 0, -0.0004, 0))),
        ("fisheye_lens_polynomial", dict(coefficients=(0, 0, 0, 0, -0.00003))),
    ]
    for index, (model, parameters) in enumerate(cases):
        f.configure(model, **parameters)
        render("Calibration " + str(index), camera_only=True, data=True)
    for width, height in ((64, 128), (96, 96), (128, 64)):
        configure_render(f.rop, dict(resx=width, resy=height))
        registry.close_all()
        entry = None
        for model in ("fisheye_equisolid", "fisheye_lens_polynomial"):
            for fit, aspect in (("auto", 1), ("horizontal", 2), ("vertical", 0.75)):
                f.configure(model, fit=fit, pixel_aspect=aspect, coefficients=(0, -0.095238095, 0, 0, 0))
                render("Sensor " + str((width, height, fit, aspect)), data=True)
    configure_render(f.rop, dict(resx=128, resy=64))
    registry.close_all()
    entry = None
    for model in MODELS:
        f.configure(model, coefficients=(0, -0.095238095, 0, 0, 0))
        render("Crop " + model, crop=(16, 8, 96, 48), data=True)
        render("Full image after crop " + model, crop=(0, 0, 128, 64), camera_only=True)
    f.configure("equirectangular")
    frame = render("Focus picking in all directions")
    focus = []
    for x, y in ((0, 32), (32, 32), (64, 32), (96, 32), (64, 8)):
        _, _, expected, _ = ray_pixels(f.spec, np.array((x + 0.5) / 128), np.array((y + 0.5) / 64), 128, 64)
        actual = camera_tools.focus_distance(frame, x, y, f.camera)
        assert abs(actual - float(expected)) < 0.025, (x, y, actual, expected)
        focus.append(dict(pixel=(x, y), distance=actual, expected=float(expected)))
    camera_tools.set_focus(f.camera, focus[0]["distance"])
    assert (
        pass_fixture.evaluate(f.camera, "cycles_dof") == 1
        and abs(pass_fixture.evaluate(f.camera, "cycles_focus_distance") - focus[0]["distance"]) < 1e-5
    )
    report["focus"] = focus
    pass_fixture.parm(f.camera, "cycles_dof").set(0)
    parent = hou.node("/obj").createNode("null", "panorama_parent")
    pass_fixture.set_parameters(parent, dict(ry=30, tx=0.2, ty=-0.1))
    f.configure("equirectangular", angle=10)
    f.camera.setInput(0, parent)
    f.spec.update(angle=40, origin=(0.2, -0.1, 0))
    render("Parented panoramic camera", camera_only=True)
    f.camera.setInput(0, None)
    parent.destroy()
    f.configure("equirectangular")
    configure_render(f.rop, dict(allowmotionblur=1, motion_samples=3))
    # Changing the global motion mode requires an authoritative capture; the
    # artist-facing navigation gates enforce this same precondition.
    render("Motion mode enabled before camera-only lens edits")
    pass_fixture.parm(f.camera, "cycles_longitude_max").setExpression(
        "float('nan') if abs(hou.frame()-1)>.01 else 180", hou.exprLanguage.Python
    )
    render("Invalid shutter range retains requested panorama", codes=("camera.shutter_panorama",), camera_only=True)
    f.configure("equirectangular")
    render("Range repair clears warnings", camera_only=True)
    pass_fixture.parm(f.camera, "cycles_longitude_max").setExpression("180+$T*24", hou.exprLanguage.Hscript)
    # Independent uniform exposure of authored longitude, rather than a
    # reference using captured samples or the renderer's interpolation.
    animated = (
        sum(
            reference(dict(f.spec, longitude_max=180 + (0.5 / 24) * ((i + 0.5) / 128 - 0.5) * 24), samples=4)
            for i in range(128)
        )
        / 128
    )
    render("Animated longitude evaluates at actual ray time", expected=animated, camera_only=True)
    f.configure("equirectangular")
    render("Animated lens repair", camera_only=True)
    pass_fixture.parm(f.camera, "cycles_fisheye_lens").setExpression("float('nan')", hou.exprLanguage.Python)
    pass_fixture.parm(f.camera, "focal").setExpression("float('nan')", hou.exprLanguage.Python)
    render("Unused lens and native focal fields are ignored", camera_only=True)
    pass_fixture.parm(f.camera, "focal").deleteAllKeyframes()
    pass_fixture.parm(f.camera, "focal").set(50)
    for model in MODELS:
        f.configure(model, coefficients=(0, -0.095238095, 0, 0, 0))
        pass_fixture.parm(f.camera, "ry").setExpression("$T*240", hou.exprLanguage.Hscript)
        render("Camera motion " + model, expected=motion_reference(f.spec), camera_only=True)
    f.configure("equirectangular")
    configure_render(f.rop, dict(allowmotionblur=0, pass_motion=1))
    pass_fixture.parm(f.camera, "ry").setExpression("$T*240", hou.exprLanguage.Hscript)
    report["vectors"] = []
    for index, (model, parameters) in enumerate(
        [(model, {}) for model in MODELS]
        + [
            ("equirectangular", dict(longitude_min=0, longitude_max=360)),
            ("central_cylindrical", dict(u_min=0, u_max=360)),
        ]
    ):
        f.configure(model, coefficients=(0, 0, -0.005, 0, 0), **parameters)
        pass_fixture.parm(f.camera, "ry").setExpression("$T*240", hou.exprLanguage.Hscript)
        frame = render("Motion vectors " + str(index), camera_only=index != 0)
        plane = frame["planes"]["motion"]
        vectors = np.frombuffer(plane["pixels"], np.float32).reshape(64, 128, 4)
        centre, valid = motion_vectors(f.spec, 128, 64)
        target = vector_reference(f.spec, 128, 64)
        yy, xx = np.indices((64, 128))
        mask = valid & (yy >= 12) & (yy < 52)
        if model not in ("equirectangular", "central_cylindrical"):
            mask &= ((xx - 63.5) / 64) ** 2 + ((yy - 31.5) / 32) ** 2 < 0.5**2
        delta = np.abs(vectors[mask] - target[mask])
        result = dict(
            model=model, parameters=parameters, mean_difference=float(delta.mean()), max_difference=float(delta.max())
        )
        report["vectors"].append(result)
        if result["mean_difference"] >= 0.02 or result["max_difference"] >= 0.08:
            np.savez(dest / "failed-vectors.npz", actual=vectors, expected=target, centre=centre, mask=mask)
            raise AssertionError(result)
    f.configure("equirectangular")
    pass_fixture.parm(f.camera, "ry").setExpression("$T*240", hou.exprLanguage.Hscript)
    configure_render(f.rop, dict(allowmotionblur=1, pass_motion=0))
    render("Blur re-enabled preserves panorama orientation", expected=motion_reference(f.spec))
    pass_fixture.parm(f.rop, "allowmotionblur").set(0)
    f.configure("fisheye_equidistant")
    render("Motion disabled preserves panorama orientation")
    # The native parameter's strict positive limit clamps zero into range.
    # A non-finite source actually exercises essential-configuration rejection.
    before = entry.session.status()
    pass_fixture.parm(f.camera, "cycles_fisheye_fov").setExpression("float('nan')", hou.exprLanguage.Python)
    try:
        entry.apply(native_scene.capture(f.rop, camera_only=True), hou.time())
    except (RuntimeError, ValueError):
        pass
    else:
        raise AssertionError("Invalid requested panorama was silently accepted")
    after = entry.session.status()
    assert after["state"] != "failed" and after["committed_generation"] == before["committed_generation"], after
    f.configure("fisheye_equidistant")
    render("Requested lens repair resumes the retained worker", camera_only=True)
    configure_render(f.rop, dict(subdivision=dict(type="linear", space="pixel", dicing_rate=4, max_level=4)))
    pass_fixture.set_parameters(f.camera, dict(cycles_dof=1, cycles_fstop=0.05, cycles_focus_distance=3))
    for index, model in enumerate(("fisheye_equidistant", "equirectangular", "mirrorball")):
        f.configure(model)
        render(
            "Radial depth of field with adaptive subdivision " + model,
            expected=dof_reference(f.spec),
            camera_only=False,
        )
    pass_fixture.parm(f.camera, "cycles_focus_distance").set(1)
    render("Live panoramic focus edit", expected=dof_reference(f.spec, focus=1), camera_only=True)
    pass_fixture.set_parameters(f.camera, dict(cycles_dof=0, cycles_focus_distance=7))
    set_subdivision(f.rop, type="none")
    session = entry.session
    registry.close_all()
    deadline = time.monotonic() + 15
    while session.status()["state"] != "closed" and time.monotonic() < deadline:
        time.sleep(0.01)
    report["resource_retirement"] = session.status()
    assert (
        report["resource_retirement"]["state"] == "closed"
        and report["resource_retirement"]["device_allocation_bytes"]
        == report["resource_retirement"]["owned_geometry_bytes"]
        == 0
    )
    registry.close_all()
    entry = None
    models = (
        "equirectangular",
        "equirectangular",
        "fisheye_lens_polynomial",
        "fisheye_equisolid",
        "central_cylindrical",
    )
    f.configure("equirectangular", coefficients=(0, 0, -0.005, 0, 0))
    configure_render(f.rop, dict(allowmotionblur=1, persistent_data=1, picture=str(dest / "frame.$F4.exr")))
    pass_fixture.parm(f.camera, "cycles_panorama_type").setExpression(
        repr(models) + "[max(0,min(4,int(hou.frame()+.5)-1))]", hou.exprLanguage.Python
    )
    pass_fixture.parm(f.camera, "cycles_longitude_max").setExpression(
        "float('nan') if int(hou.frame()+.5)==2 and abs(hou.frame()-round(hou.frame()))>.01 else 180",
        hou.exprLanguage.Python,
    )
    pass_fixture.parm(f.camera, "ry").setExpression("floor($FF+.5)*10", hou.exprLanguage.Hscript)
    f.rop.render(frame_range=(1, 5, 1))
    summary = f.rop.cachedUserData(final_render._summary_key)
    assert summary["completed_frames"] == 5 and summary["created_sessions"] == 1 and not summary["session_alive"], (
        summary
    )
    report["batch"] = []
    for index, model in enumerate(models, 1):
        image = oiio.ImageBuf(str(dest / f"frame.{index:04d}.exr"))
        channels = list(image.spec().channelnames)
        pixels = np.flipud(image.get_pixels()[:, :, [channels.index(c) for c in ("R", "G", "B")]])
        expected = reference(dict(f.spec, model=model, angle=index * 10))
        error = float(np.abs(pixels - expected).mean())
        assert error < 0.0012, (index, error)
        issues = json.loads(image.spec().get_string_attribute("cycles_houdini:source_diagnostics") or "[]")
        assert {d["code"] for d in issues} == ({"camera.shutter_panorama"} if index == 2 else set()), (index, issues)
        report["batch"].append(dict(frame=index, mean_difference=error, diagnostics=issues))
    report.update(result="passed", final=summary)
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    registry.close_all()
    (dest / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(dict(result=report["result"], checks=len(report["checks"]))))
