# SPDX-License-Identifier: Apache-2.0
"""Camera source failures recover locally, while supported zoom retains motion."""

from support.paths import artifacts_root
from native_test_runtime import runtime_executable
from subdivision_test_setup import configure_render, set_subdivision
import pass_fixtures as pass_fixture

import argparse, hashlib, json, os, re, subprocess, time, traceback
from pathlib import Path
import hou, numpy as np, OpenImageIO as oiio
from cycles_houdini import native_scene, registry, final_render, camera_properties
from camera_lens_motion_fixture import Fixture, CASES

p = argparse.ArgumentParser()
p.add_argument("--device", default="CPU")
device = p.parse_args().device
dest = artifacts_root() / "stage2/cameras/lens-motion"
dest.mkdir(parents=True, exist_ok=True)
root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
references = dest / ("references-" + re.sub("[^A-Za-z0-9_-]", "_", device))
images = {}
f = Fixture(device)
entry = registry.for_rop(f.rop, native_scene.configuration(f.rop))
entry.session.close()
entry.session = registry.session_from_configuration(native_scene.configuration(f.rop), collect_diagnostics=True)
report = dict(result="running", device=device, preflight=[], checks=[])


def reference(name):
    if name not in images:
        path = references / (name + ".exr")
        reader = oiio.ImageBuf(str(path))
        channels = list(reader.spec().channelnames)
        images[name] = (
            np.flipud(reader.get_pixels()[:, :, [channels.index(c) for c in ("R", "G", "B")]]),
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
    return images[name]


def render(
    label,
    kind,
    steps=3,
    offset=0,
    *,
    codes=(),
    camera_only=False,
    resources=1,
    reference_name=None,
    witness_visible=False,
    dicing_change=False,
):
    configure_render(f.rop, dict(motion_samples=steps, shutteroffset=offset))
    before = entry.session.status()
    frame = entry.session.wait(entry.apply(native_scene.capture(f.rop, camera_only=camera_only), hou.time()))
    status = entry.session.status()
    deadline = time.monotonic() + 3
    while status["state"] != "complete" and time.monotonic() < deadline:
        assert status["state"] != "failed", status
        time.sleep(0.005)
        status = entry.session.status()
    plane = frame["planes"]["combined"]
    actual = np.frombuffer(plane["pixels"], np.float32).reshape(96, 160, plane["channels"])[:, :, :3]
    expected, digest = reference(
        reference_name or kind + "-" + str(steps) + "-" + ("m" if offset < 0 else "p" if offset > 0 else "c")
    )
    error = float(np.abs(actual - expected).mean())
    diagnostics = entry.session.diagnostics()
    report["checks"].append(
        dict(
            label=label,
            steps=steps,
            offset=offset,
            mean_difference=error,
            reference_sha256=digest,
            status=status,
            previous=before,
            diagnostics=diagnostics,
        )
    )
    assert (
        status["state"] == "complete"
        and status["geometry_resources"] == status["object_resources"] == resources
        and not status["motion_warnings"]
    ), status
    assert {d["code"] for d in status["source_diagnostics"]} == set(codes), status
    if not np.isfinite(actual).all() or error >= 0.0012:
        np.savez(dest / "failed-image.npz", actual=actual, expected=expected)
        raise AssertionError((label, steps, offset, error))
    if witness_visible:
        assert float((actual[:, :, 0] - actual[:, :, 1]).max()) > 0.03, (
            "The clipping witness was not demonstrably rendered"
        )
    if camera_only:
        assert all(status[key] == before[key] for key in ("geometry_conversions", "extractions")), status
        if dicing_change:
            assert status["subdivision_tessellations"] > before["subdivision_tessellations"], status
        else:
            assert status["subdivision_tessellations"] == before["subdivision_tessellations"], status
            # No-op submissions retain the last physical render's diagnostics.
            # Only a new engine revision can attribute copies to this edit.
            if status["render_revision"] != before["render_revision"]:
                assert not {"tri_verts", "tri_vindex"} & diagnostics["buffer_copy_requests"].keys(), diagnostics
    return status


try:
    entry.session.wait(entry.apply(native_scene.capture(f.rop), hou.time()))
    errors = []
    for case in CASES:
        f.configure(case)
        try:
            frame = entry.session.wait(entry.apply(native_scene.capture(f.rop, camera_only=True), hou.time()))
            status = entry.session.status()
            plane = frame["planes"]["combined"]
            pixels = np.frombuffer(plane["pixels"], np.float32).reshape(96, 160, plane["channels"])[:, :, :3]
            assert np.isfinite(pixels).all() and pixels.max() > 0.1 and status["state"] != "failed", status
            report["preflight"].append(dict(case=case, result="passed", status=status))
        except Exception as error:
            errors.append(case)
            report["preflight"].append(dict(case=case, result="failed", error=str(error)))
    assert not errors, ("Camera edits rejected usable rendering", errors)
    run = subprocess.run(
        [
            str(runtime_executable("ch_harness.exe")),
            "--resources",
            os.environ["CYCLES_HOUDINI_RESOURCES"],
            "--device",
            device,
            "--camera-lens-reference",
            "--output",
            str(references),
        ],
        capture_output=True,
        text=True,
        timeout=240,
    )
    assert run.returncode == 0, (run.stdout, run.stderr)
    report["reference_contract"] = json.loads(run.stdout.strip().splitlines()[-1])
    cases = (
        ("valid", "pinhole", ()),
        ("dof", "dof", ()),
        ("focus_pull", "focus_pull", ()),
        ("offframe_invalid_focus", "dof", ("camera.shutter_dof",)),
        ("invalid_nominal_focus", "pinhole", ("camera.invalid_dof",)),
        ("zoom", "zoom", ()),
        ("ortho_scale", "ortho", ()),
        ("clipping", "pinhole", ()),
        ("projection_switch", "pinhole", ("camera.shutter_projection",)),
        ("offframe_invalid_focal", "pinhole", ("camera.shutter_projection",)),
        ("held_transform", "held_transform", ("camera.shutter_transform",)),
        ("held_transform_zoom", "held_transform_zoom", ("camera.shutter_transform",)),
        ("zoom_only", "zoom_only", ()),
    )
    for case, kind, codes in cases:
        f.configure(case)
        for steps in (3, 5):
            for offset in (-0.75, 0, 0.75):
                render(case, kind, steps, offset, codes=codes)
    f.configure("quarter_zoom")
    render("Full extra-key field-of-view curve is rendered", "quarter_zoom", 5)
    f.configure("invalid_nominal_focus")
    pass_fixture.parm(f.rop, "dof_mode").set("off")
    render("Disabled DOF ignores broken focus", "pinhole")
    f.configure("ortho_scale")
    pass_fixture.parm(f.camera, "orthowidth").deleteAllKeyframes()
    pass_fixture.parm(f.camera, "orthowidth").set(6)
    pass_fixture.parm(f.camera, "focal").setExpression("-1", hou.exprLanguage.Hscript)
    render("Unused orthographic focal value is ignored", "ortho_static")
    f.configure("valid")
    f.witness()
    render(
        "Near witness is visible with valid clipping", "near_witness", offset=0.75, resources=2, witness_visible=True
    )
    pass_fixture.parm(f.camera, "near").setExpression(".1-$T*4", hou.exprLanguage.Hscript)
    render(
        "Near witness follows animated clipping",
        "animated_near_witness",
        offset=0.75,
        resources=2,
        witness_visible=True,
    )
    f.configure("valid")
    f.witness(far=True)
    render("Far witness is visible with valid clipping", "far_witness", offset=0.75, resources=2, witness_visible=True)
    f.configure("valid")
    pass_fixture.parm(f.camera, "far").setExpression("5.1+$T*2", hou.exprLanguage.Hscript)
    f.witness(far=True)
    render(
        "Far witness follows animated clipping", "animated_far_witness", offset=0.75, resources=2, witness_visible=True
    )
    f.configure("zoom")
    render("Supported zoom before motion toggle", "zoom")
    pass_fixture.parm(f.rop, "allowmotionblur").set(0)
    render("Motion off renders requested lens without blur", "zoom", reference_name="zoom-no-blur")
    pass_fixture.parm(f.rop, "allowmotionblur").set(1)
    render("Motion re-enabled restores zoom", "zoom")
    # Motion vectors use frame-1/frame/frame+1 camera projections independently
    # of shutter blur. The expected raster vectors are analytic at pixel centres.
    f.configure("zoom")
    pass_fixture.parm(f.camera, "focal").setExpression("50/(1+$T*6)", hou.exprLanguage.Hscript)
    configure_render(f.rop, dict(allowmotionblur=0, pass_motion=1))
    frame = entry.session.wait(entry.apply(native_scene.capture(f.rop), hou.time()))
    plane = frame["planes"]["motion"]
    vector = np.frombuffer(plane["pixels"], np.float32).reshape(96, 160, 4)
    y, x = np.indices((96, 160))
    dx = x + 0.5 - 80
    dy = y + 0.5 - 48
    expected = np.stack((dx / 3 + 16 / 3, dy / 3, dx / 5 + 16 / 5, dy / 5), axis=-1)
    delta = np.abs(vector[42:54, 70:90] - expected[42:54, 70:90])
    assert float(delta.mean()) < 0.03 and float(delta.max()) < 0.12, (delta.mean(), delta.max())
    assert not entry.session.status()["source_diagnostics"], entry.session.status()
    report["zoom_vectors"] = dict(
        mean_difference=float(delta.mean()),
        max_difference=float(delta.max()),
        expected="Analytic perspective raster pre/centre/post with camera and geometry translation",
    )
    for case in ("focus_pull", "clipping"):
        f.configure(case)
        configure_render(f.rop, dict(allowmotionblur=0, pass_motion=1))
        frame = entry.session.wait(entry.apply(native_scene.capture(f.rop), hou.time()))
        plane = frame["planes"]["motion"]
        vectors = np.frombuffer(plane["pixels"], np.float32).reshape(96, 160, 4)[42:54, 70:90]
        # Motion is normalized by the samples that hit a surface, independently
        # of beauty coverage (Cycles kernel/film/data_passes.h and film/read.h).
        # Defocus mixes constant emission with black, giving an independent
        # coverage witness while surface displacement remains four pixels.
        beauty = frame["planes"]["combined"]
        rgb = np.frombuffer(beauty["pixels"], np.float32).reshape(96, 160, beauty["channels"])[42:54, 70:90, :3]
        coverage = rgb[:, :, 2] / 0.8
        assert np.max(np.abs(rgb - coverage[:, :, None] * np.array((0.2, 0.4, 0.8)))) < 0.0001
        if case == "focus_pull":
            assert 0.5 < float(coverage.mean()) < 0.99, "Defocus must exercise partial background coverage"
        else:
            assert float(coverage.min()) > 0.99
        error = float(np.abs(vectors - np.array((4, 0, 4, 0))).mean())
        assert error < 0.03, (case, error, vectors.mean(axis=(0, 1)))
        assert not entry.session.status()["source_diagnostics"], entry.session.status()
        report.setdefault("vector_coverage", []).append(
            dict(case=case, mean_difference=error, mean_coverage=float(coverage.mean()))
        )
    f.configure("dof")
    configure_render(f.rop, dict(subdivision=dict(type="linear", space="pixel", dicing_rate=4, max_level=4)))
    render("Before camera-only lens edits with adaptive subdivision", "dof")
    pass_fixture.parm(f.camera, "focus").setExpression("2.5+$T*24", hou.exprLanguage.Hscript)
    render("Camera-only focus pull samples optics while retaining geometry", "focus_pull", camera_only=True)
    pass_fixture.parm(f.camera, "focus").deleteAllKeyframes()
    pass_fixture.parm(f.camera, "focus").set(2.5)
    render("Repair clears only camera warnings", "dof", camera_only=True)
    pass_fixture.parm(f.camera, "focus").set(3.5)
    render("Native focus edit changes optical blur", "gui_dof_focus", camera_only=True)
    pass_fixture.parm(f.camera, "focus").set(2.5)
    pass_fixture.parm(f.camera, "tx").setExpression(".1+$T*6", hou.exprLanguage.Hscript)
    render(
        "Camera movement refines adaptive geometry without SOP capture",
        "gui_dof_shift",
        camera_only=True,
        dicing_change=True,
    )
    f.configure("zoom")
    render("Before shutter-only zoom dependency edit", "zoom")
    signature = native_scene.camera_signature(native_scene.capture(f.rop, camera_only=True), hou.time())
    pass_fixture.parm(f.camera, "focal").setExpression("50/(1+$T*12)", hou.exprLanguage.Hscript)
    assert signature != native_scene.camera_signature(native_scene.capture(f.rop, camera_only=True), hou.time()), (
        "Shutter-only focal edits were invisible to navigation"
    )
    pass_fixture.parm(f.camera, "focal").setExpression("50/(1+$T*24)", hou.exprLanguage.Hscript)
    render("Restoring shutter-only projection keys restores image", "zoom", camera_only=True)
    warm = []
    for index in range(12):
        pass_fixture.parm(f.camera, "tx").setExpression((".1+" if index % 2 else "") + "$T*6", hou.exprLanguage.Hscript)
        status = render("Warm camera edit " + str(index), "gui_zoom_shift" if index % 2 else "zoom", camera_only=True)
        warm.append(
            {key: status[key] for key in ("owned_geometry_bytes", "device_allocation_bytes", "engine_shader_resources")}
        )
    for parity in (0, 1):
        for key in warm[0]:
            assert len({row[key] for row in warm[6 + parity :: 2]}) == 1, (key, warm)
    report["warm_resources"] = warm
    # Known optical failures are source errors; schema/programming errors stay strict.
    optics = dict(cycles_dof=True, focus=2.5, fstop=0.05, focal=50, cycles_aperture_blades=0)

    def sample_optics(t):
        return camera_properties.evaluate(
            lambda name, default: dict(optics, cycles_focus_distance=float("nan")).get(name, default), unit_length=1
        )

    recovered = camera_properties.resolve_lens(sample_optics, 0, [0], f.camera.path())
    assert recovered["aperture_size"] == 0 and recovered["source_diagnostics"][0]["code"] == "camera.invalid_dof"
    for values in (
        dict(cycles_fstop=1e-300),
        dict(cycles_aperture_ratio=1e300),
        dict(cycles_aperture_blades=float("inf")),
    ):

        def bad_optics(t):
            return camera_properties.evaluate(
                lambda name, default: dict(optics, **values).get(name, default), unit_length=1
            )

        recovered = camera_properties.resolve_lens(bad_optics, 0, [0], f.camera.path())
        assert recovered["aperture_size"] == 0 and recovered["source_diagnostics"][0]["code"] == "camera.invalid_dof", (
            recovered
        )
    before = entry.session.status()
    pass_fixture.parm(f.camera, "focal").setExpression("0", hou.exprLanguage.Hscript)
    try:
        entry.apply(native_scene.capture(f.rop), hou.time())
    except (RuntimeError, ValueError):
        pass
    else:
        raise AssertionError("Invalid requested projection was silently accepted")
    after = entry.session.status()
    assert (
        after["state"] != "failed"
        and after["committed_generation"] == before["committed_generation"]
        and after["render_revision"] == before["render_revision"]
    ), after
    pass_fixture.parm(f.camera, "focal").setExpression("50/(1+$T*24)", hou.exprLanguage.Hscript)
    render("Essential camera repair keeps the existing renderer", "gui_zoom_shift", camera_only=True)
    pid = entry.session.status()["renderer_pid"]
    entry.session.cancel()
    deadline = time.monotonic() + 5
    while entry.session.status()["backend_state"] != "cancelled" and time.monotonic() < deadline:
        time.sleep(0.005)
    assert entry.session.status()["backend_state"] == "cancelled", entry.session.status()
    render("An identical camera request after Stop resumes retained rendering", "gui_zoom_shift", camera_only=True)
    assert entry.session.status()["renderer_pid"] == pid, entry.session.status()
    registry.close_all()
    f.configure("dof")
    pass_fixture.parm(f.camera, "cycles_dof").setExpression("rint($FF)!=4", hou.exprLanguage.Hscript)
    pass_fixture.parm(f.camera, "cycles_focus_distance").setExpression(
        "float('nan') if int(hou.frame()+.5)==3 else 2.5+(hou.time()-(int(hou.frame()+.5)-1)/24)*24 if int(hou.frame()+.5)==2 else 2.5",
        hou.exprLanguage.Python,
    )
    pass_fixture.parm(f.camera, "focal").setExpression(
        "if(rint($FF)==4,50/(1+($T-(rint($FF)-1)/24)*24),50)", hou.exprLanguage.Hscript
    )
    configure_render(f.rop, dict(persistent_data=1, picture=str(dest / "frame.$F4.exr")))
    f.rop.render(frame_range=(1, 5, 1))
    summary = f.rop.cachedUserData(final_render._summary_key)
    assert summary["completed_frames"] == 5 and summary["created_sessions"] == 1 and not summary["session_alive"], (
        summary
    )
    batch = []
    for index in range(1, 6):
        path = dest / f"frame.{index:04d}.exr"
        image = oiio.ImageBuf(str(path))
        channels = list(image.spec().channelnames)
        expected, digest = reference("frame-" + str(index))
        actual = image.get_pixels()[:, :, [channels.index(c) for c in ("R", "G", "B")]]
        error = float(np.abs(actual - np.flipud(expected)).mean())
        assert error < 0.0012, (index, error)
        issues = json.loads(image.spec().get_string_attribute("cycles_houdini:source_diagnostics") or "[]")
        assert {d["code"] for d in issues} == ({"camera.invalid_dof"} if index == 3 else set()), (index, issues)
        batch.append(dict(frame=index, mean_difference=error, diagnostics=issues, reference_sha256=digest))
    report.update(batch=batch, final=summary)
    report["result"] = "passed"
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    registry.close_all()
    (dest / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(dict(result=report["result"], preflight=len(report["preflight"]), checks=len(report["checks"]))))
