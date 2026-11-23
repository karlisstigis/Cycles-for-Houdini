# SPDX-License-Identifier: Apache-2.0
"""Actual animated camera controls, local source repair and five written frames."""

from support.paths import artifacts_root
import argparse, json, os, time, traceback
from pathlib import Path
import hou, numpy as np, OpenImageIO as oiio
import cycles_houdini as ch
from cycles_houdini import final_render, native_scene, registry
from camera_intrinsic_motion_fixture import Fixture, plane

p = argparse.ArgumentParser()
p.add_argument("--device", default="CPU")
p.add_argument("--report", type=Path, default=artifacts_root() / "stage2/cameras/intrinsic-motion/report.json")
args = p.parse_args()
args.report.parent.mkdir(parents=True, exist_ok=True)
f = Fixture(args.device)
entry = registry.for_rop(f.rop, native_scene.configuration(f.rop))
entry.session.close()
entry.session = registry.session_from_configuration(native_scene.configuration(f.rop), collect_diagnostics=True)
report = dict(result="running", device=args.device, checks=[], batch=[])


def save():
    args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")


def render(label, camera_only=False):
    previous = entry.session.status()
    cooks = [sop.cookCount() for sop in f.outputs]
    frame = entry.session.wait(entry.apply(f.update(camera_only), hou.time()))
    status = entry.session.status()
    deadline = time.monotonic() + 5
    while status["state"] != "complete" and time.monotonic() < deadline:
        assert status["state"] != "failed", status
        time.sleep(0.005)
        status = entry.session.status()
    event = dict(
        label=label,
        case=f.case,
        offset=f.offset,
        steps=f.steps,
        status=status,
        captured=f.capture_info(entry.session),
        cooks_before=cooks,
        cooks_after=[sop.cookCount() for sop in f.outputs],
    )
    report["attempted"] = event
    save()
    event["image"] = f.inspect(frame)
    assert status["state"] == "complete" and not status["motion_warnings"], event
    assert sorted(status["render_device_ids"]) == sorted(f.target_ids), event
    assert {d["code"] for d in status["source_diagnostics"]} == f.expected_codes(), event
    assert event["captured"]["camera_intrinsic_samples"] == f.expected_samples(), event
    assert event["captured"]["camera_motion_samples"] == f.steps and event["captured"]["mesh_motion_samples"] == 6 * (
        f.steps - 1
    ), event
    assert status["geometry_resources"] == status["object_resources"] == 6, event
    if f.case == "dof_independent":
        assert event["captured"]["camera_dof_samples"] == f.steps, event
    if camera_only:
        assert event["cooks_after"] == cooks, event
        assert all(
            status[k] == previous[k] for k in ("geometry_conversions", "extractions", "subdivision_tessellations")
        ), event
        if status["render_revision"] != previous["render_revision"]:
            assert not {"tri_verts", "tri_vindex"} & entry.session.diagnostics()["buffer_copy_requests"].keys(), event
    if report["checks"]:
        assert status["renderer_pid"] == report["checks"][0]["status"]["renderer_pid"], event
    report["checks"].append(event)
    report.pop("attempted", None)
    save()
    return frame, event


try:
    assert ch.capabilities()["native_animated_camera_intrinsics"]
    render("Full equirectangular exposure with independent geometry, object and camera motion")
    for case in ("perspective", "extra_fov"):
        f.set_case(case, steps=5)
        render("Full key array " + case)
        before = native_scene.camera_signature(f.update(True), hou.time())
        f.camera.parm("test_intrinsic_amplitude").set(0.5)
        assert native_scene.camera_signature(f.update(True), hou.time()) != before, (
            "Shutter-only edit missing from camera signature"
        )
        render("Identical nominal optics with changed off-frame samples", True)
        f.camera.parm("test_intrinsic_amplitude").set(1)
        render("Exact authored repair", True)
    for case in (
        "orthographic",
        "ortho_clip",
        "bad_width",
        "orthographic",
        "clipping",
        "cylinder",
        "cylinder_radius",
        "fisheye_equidistant",
        "fisheye_equisolid",
        "sensor",
        "polynomial",
        "bad_clip",
        "equirectangular",
        "bad_range",
        "equirectangular",
        "bad_lens",
        "fisheye_equisolid",
        "bad_sensor",
        "fisheye_equisolid",
        "fit_hold",
        "fisheye_equisolid",
        "projection_hold",
        "equirectangular",
        "inactive_controls",
        "dof_independent",
        "equirectangular",
    ):
        # Grid remains3 after the first transition; every following edit is
        # purely camera state, including changes of the already supported model.
        retain = f.steps == 3
        f.set_case(case, pixel_aspect=1.7 if case in ("orthographic", "ortho_clip", "bad_width") else 1)
        render("Projection/source break-repair " + case, retain)
    for offset in (-1, 1):
        f.set_case("equirectangular", offset=offset)
        render("Shifted shutter retains separate requested-frame intrinsics")
    f.set_case("equirectangular", steps=129)
    render("Maximum owned chronological projection array")
    f.set_case("motion_off")
    frame, event = render("Vector-only capture uses endpoint optics with nominal beauty")
    vectors = plane(frame, "motion")
    yy, xx = np.indices((f.height, f.width))
    u = (xx + 0.5) / f.width
    v = (yy + 0.5) / f.height
    pcx = (2 * u - 1) * 2 * 0.6 * 3
    pcy = (2 * v - 1) * 0.6 * 3
    uv = np.stack((u, v), axis=-1)
    end = []
    for t in (-1 / 24, 1 / 24):
        tangent = 0.6 + 4.8 * t
        end.append(np.stack((0.5 + (pcx - 3.9 * t) / (12 * tangent), 0.5 + pcy / (6 * tangent)), axis=-1))
    expected = np.concatenate((end[0] - uv, uv - end[1]), axis=-1) * np.array((f.width, f.height, f.width, f.height))
    mask = (xx > 40) & (xx < 88) & (yy > 20) & (yy < 44)
    error = float(np.abs(vectors[mask] - expected[mask]).mean())
    report["vector_reference"] = dict(
        mean_difference=error, reference="Independent projection of translated enclosure at +/- one frame"
    )
    assert np.isfinite(vectors).all() and error < 0.025, report["vector_reference"]
    f.set_case("equirectangular")
    render("Beauty motion restored after vector-only endpoint capture")
    before = entry.session.status()
    f.camera.parm("cycles_latitude_max").setExpression("float('nan')", hou.exprLanguage.Python)
    try:
        entry.apply(f.update(True), hou.time())
    except (RuntimeError, ValueError):
        pass
    else:
        raise AssertionError("Invalid essential nominal camera source was accepted")
    after = entry.session.status()
    assert after["state"] != "failed" and after["committed_generation"] == before["committed_generation"], after
    f.set_case("equirectangular")
    render("Essential requested-camera repair keeps accepted worker", True)
    registry.close_all()
    deadline = time.monotonic() + 20
    while entry.session.status()["state"] != "closed" and time.monotonic() < deadline:
        time.sleep(0.01)
    report["closed"] = closed = entry.session.status()
    assert closed["state"] == "closed" and closed["owned_geometry_bytes"] == closed["device_allocation_bytes"] == 0, (
        closed
    )
    f.set_case("equirectangular")
    f.recenter_final()
    f.camera.parm("cycles_longitude_max").setExpression(
        "float('nan') if int(hou.frame()+.5)==3 and hou.time()-(int(hou.frame()+.5)-1)/24<0 else 180+(hou.time()-(int(hou.frame()+.5)-1)/24)*3600",
        hou.exprLanguage.Python,
    )
    f.rop.setParms(dict(persistent_data=1, picture=str(args.report.parent / "intrinsics.$F4.exr")))
    f.rop.render(frame_range=(1, 5, 1))
    summary = f.rop.cachedUserData(final_render._summary_key)
    assert summary["completed_frames"] == 5 and summary["created_sessions"] == 1 and not summary["session_alive"], (
        summary
    )
    for number in range(1, 6):
        hou.setFrame(number)
        f.case = "bad_range" if number == 3 else "equirectangular"
        path = args.report.parent / f"intrinsics.{number:04d}.exr"
        image = oiio.ImageBuf(str(path))
        names = list(image.spec().channelnames)
        pixels = np.flipud(image.get_pixels()[:, :, [names.index(c) for c in ("R", "G", "B")]])
        error = float(np.abs(pixels - f.reference()).mean())
        issues = json.loads(image.spec().get_string_attribute("cycles_houdini:source_diagnostics") or "[]")
        event = dict(frame=number, path=str(path), mean_difference=error, diagnostics=issues)
        report["batch"].append(event)
        save()
        assert error < 0.003 and {d["code"] for d in issues} == f.expected_codes(), event
        if number == 3:
            assert any("longitude" in d.get("attribute", "") for d in issues), event
    report.update(result="passed", final=summary)
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    registry.close_all()
    save()
print(json.dumps(dict(result=report["result"], checks=len(report["checks"]), frames=len(report["batch"]))))
