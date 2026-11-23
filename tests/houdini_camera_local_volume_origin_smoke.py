# SPDX-License-Identifier: Apache-2.0
"""Moving camera origins inside a local medium against static-pose quadrature.

The nominal camera is outside; all complete shutter poses are inside. Static
intrinsics and disabled DOF ensure only transform motion initializes the stack.
"""

from support.paths import artifacts_root
import argparse, json, os, time, traceback
from pathlib import Path
import hou, numpy as np
import cycles_houdini as ch
from cycles_houdini import native_scene, registry
from cycles_houdini.device_selection import set_devices

p = argparse.ArgumentParser()
p.add_argument("--device", default="CPU")
p.add_argument("--report", type=Path, default=artifacts_root() / "stage2/cameras/local-volume-origin/report.json")
args = p.parse_args()
args.report.parent.mkdir(parents=True, exist_ok=True)
report = dict(result="running", device=args.device, scope=__doc__, renders=[])
hou.setFps(24)
hou.setFrame(1)
volume_obj = hou.node("/obj").createNode("geo", "probe_local_camera_volume", run_init_scripts=False)
volume = volume_obj.createNode("volume", "density")
volume.setParms(dict(name="density", initialval1=1, uniformsamples=0, divsx=24, divsy=16, divsz=32))
volume.parmTuple("size").set((0.6, 4, 8))
placement = volume_obj.createNode("xform", "local_volume_bounds")
placement.setInput(0, volume)
placement.parm("tx").set(0.5)
placement.setDisplayFlag(True)
placement.setRenderFlag(True)
absorb = ch.create_material(name="probe_absorption", shader="absorption_volume")
absorb.node("volume_shader").parmTuple("color").set((0, 0, 0))
volume_obj.parm("shop_materialpath").set(absorb.path())
target = hou.node("/obj").createNode("geo", "probe_white_target", run_init_scripts=False)
target.parm("tz").set(-3)
grid = target.createNode("grid", "emission_plane")
grid.setParms(dict(type=0, orient=0, rows=2, cols=2))
grid.parmTuple("size").set((8, 8))
grid.setDisplayFlag(True)
grid.setRenderFlag(True)
emission = ch.create_material(name="probe_white_emission", shader="emission")
emission.node("surface_shader").parmTuple("color").set((1, 1, 1))
emission.node("surface_shader").parm("strength").set(1)
target.parm("shop_materialpath").set(emission.path())
camera = hou.node("/obj").createNode("cam", "probe_requested_camera")
camera.setParms(dict(tx=0, ty=0, tz=0))
rop = hou.node("/out").createNode("cycles_ipr", "probe_camera_origin")
requested = json.loads(args.device) if args.device.startswith("[") else args.device
targets = list(set_devices(rop, requested))
rop.setParms(
    dict(
        source_mode="objects",
        vobject=volume_obj.path() + " " + target.path(),
        camera=camera.path(),
        resx=48,
        resy=24,
        samples=1024,
        ipr_samples=1024,
        adaptive_sampling=0,
        ipr_adaptive_sampling=0,
        use_denoise=0,
        ipr_use_denoise=0,
        allowmotionblur=1,
        geometry_motion="transform",
        motion_samples=3,
        use_material_assignments=1,
        volume_step_rate=0.25,
    )
)
rop.parmTuple("background").set((0, 0, 0))
entry = registry.for_rop(rop, native_scene.configuration(rop))
entry.session.close()
entry.session = registry.session_from_configuration(native_scene.configuration(rop), collect_diagnostics=True)
nominal = ch.matrix_from_houdini(camera.worldTransform(), camera=True)


def pose(x):
    value = list(nominal)
    value[3] = x
    return value


def render(label, x=None):
    update = native_scene.capture(rop)
    update.pop("camera_path", None)
    update["camera"] = dict(
        transform=nominal if x is None else pose(x),
        fov=0.08,
        near=0.01,
        far=100,
        orthographic=False,
        ortho_scale=2,
        motion=[pose(t) for t in (0.3, 0.5, 0.7)] if x is None else [],
    )
    update["passes"] = ["combined"]
    frame = entry.session.wait(entry.apply(update, hou.time()))
    status = entry.session.status()
    plane = frame["planes"]["combined"]
    image = np.frombuffer(plane["pixels"], np.float32).reshape(24, 48, plane["channels"]).copy()
    assert (
        status["state"] != "failed"
        and sorted(status["render_device_ids"]) == sorted(targets)
        and not status["source_diagnostics"]
    ), status
    assert status["geometry_resources"] == status["object_resources"] == 2 and np.isfinite(image).all(), status
    captured = ch._native.snapshot_info(entry.session._controller.acknowledged)
    assert (
        captured["camera_motion_samples"] == (3 if x is None else 0)
        and captured["camera_intrinsic_samples"] == captured["camera_dof_samples"] == 0
    ), captured
    if report["renders"]:
        first = report["renders"][0]["status"]
        assert (
            status["renderer_pid"] == first["renderer_pid"]
            and status["geometry_conversions"] == first["geometry_conversions"]
            and status["extractions"] == first["extractions"]
        ), status
    report["renders"].append(
        dict(
            label=label,
            static_x=x,
            central_mean=float(image[10:14, 21:27, :3].mean()),
            status=status,
            captured=captured,
        )
    )
    return image


try:
    moving = render("Requested pose outside, all chronological shutter poses inside local volume")
    rop.parm("allowmotionblur").set(0)
    parts = []
    for i in range(8):
        parts.append(render("Independent static pose " + str(i), 0.3 + 0.4 * (i + 0.5) / 8))
    expected = np.mean(parts, axis=0)
    actual_mean = float(moving[10:14, 21:27, :3].mean())
    expected_mean = float(expected[10:14, 21:27, :3].mean())
    error = float(np.abs(moving[10:14, 21:27, :3] - expected[10:14, 21:27, :3]).mean())
    assert 0.02 < expected_mean < 0.09, ("Static reference did not prove attenuation", expected_mean)
    np.savez(args.report.with_suffix(".npz"), actual=moving, expected=expected)
    report.update(
        result="passed" if error < 0.01 else "failed",
        mean_difference=error,
        tolerance=0.01,
        actual_mean=actual_mean,
        expected_static_mean=expected_mean,
        reference="Eight independently rendered static poses; no sampled interpolation or captured camera array used by reference.",
    )
    assert error < 0.01, dict(mean_difference=error, actual=actual_mean, independent=expected_mean)
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    registry.close_all()
    deadline = time.monotonic() + 20
    while entry.session.status()["state"] != "closed" and time.monotonic() < deadline:
        time.sleep(0.01)
    report["closed"] = entry.session.status()
    assert (
        report["closed"]["state"] == "closed"
        and report["closed"]["owned_geometry_bytes"] == report["closed"]["device_allocation_bytes"] == 0
    ), report["closed"]
    args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")
print(
    json.dumps({key: report.get(key) for key in ("result", "mean_difference", "actual_mean", "expected_static_mean")})
)
