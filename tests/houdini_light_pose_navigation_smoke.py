# SPDX-License-Identifier: Apache-2.0
"""Retained analytic-light transforms match full captures in one live worker."""

from support.paths import artifacts_root

import argparse
import json
import os
from pathlib import Path
import time
import hou
import numpy as np
from cycles_houdini import Session, device_selection, light_properties, native_scene, registry

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
device = parser.parse_args().device
root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
obj = hou.node("/obj")
scene = obj.createNode("subnet", "pose_scene")
geo = scene.createNode("geo", "pose_receiver", run_init_scripts=False)
grid = geo.createNode("grid")
grid.setParms(dict(orient=0, rows=2, cols=2, sizex=6, sizey=6))
camera = obj.createNode("cam", "pose_camera")
camera.setParms(dict(tz=7, projection=1, orthowidth=7))
parent = scene.createNode("null", "pose_light_parent")
light = scene.createNode(light_properties.LIGHT_TYPE, "pose_light")
light.setInput(0, parent)
light.setParms(dict(tx=-1, ty=1, tz=3, light_intensity=80, singlesided=1))
rop = hou.node("/out").createNode("cycles_ipr", "light_pose_reference")
device_selection.set_devices(rop, [device])
rop.setParms(
    dict(
        source_mode="objects",
        objectroot=scene.path(),
        vobject=geo.path(),
        lights=light.path(),
        camera=camera.path(),
        resx=96,
        resy=72,
        samples=64,
        ipr_samples=64,
        resolution_percent=100,
        ipr_resolution_percent=100,
        use_material_assignments=0,
        material_kind="diffuse",
        use_denoise=0,
        ipr_use_denoise=0,
        allowmotionblur=0,
    )
)
rop.parmTuple("background").set((0, 0, 0))
checks = []


def pixels(frame):
    return np.frombuffer(frame["planes"]["combined"]["pixels"], np.float32).copy()


def preview_frame(session, generation):
    began = time.monotonic()
    while time.monotonic() - began < 12:
        status = session.status()
        frame = session.frame()
        assert status["state"] != "failed", status
        if frame and frame["complete"] and frame["generation"] == generation and frame.get("current_scene"):
            assert status["pending_update"], "Preview discharged the authoritative source update"
            return frame
        time.sleep(0.005)
    raise TimeoutError(status)


with Session(
    device=device, width=96, height=72, samples=64, interactive=False, execution="worker", collect_diagnostics=True
) as session:
    entry = registry.Entry("light-pose-test", native_scene.configuration(rop), session, rop=rop)
    session.wait(entry.apply(native_scene.capture(rop), hou.time()))
    assert not entry.expression_light_poses, "Native constant scale locks are not animated channels"
    pid = session.status()["renderer_pid"]
    if device != "CPU":
        scheduling = session.diagnostics()["controller"]["gpu_scheduling"]
        assert scheduling["applied"] and scheduling["after"] == 1, scheduling
    conversions = session.status()["geometry_conversions"]
    for index, (kind, shape, two_sided, reverse) in enumerate(
        (
            ("grid", "rectangle", False, False),
            ("grid", "rectangle", True, False),
            ("grid", "ellipse", False, True),
            ("grid", "disk", True, False),
            ("spot", "rectangle", False, False),
            ("point", "rectangle", False, False),
            ("sun", "rectangle", False, False),
        )
    ):
        light.setParms(
            dict(cycles_light_type=kind, cycles_light_area_shape=shape, singlesided=not two_sided, reverse=reverse)
        )
        parent.setParms(dict(tx=0, ry=0, sx=1, sy=1))
        session.wait(entry.apply(native_scene.capture(rop), hou.time()))
        initial = pixels(session.frame())
        light.setParms(dict(tx=0.6, rx=8, ry=-12))
        parent.setParms(dict(tx=0.2, ry=11, sx=-1.3, sy=0.8))
        if index == 0:
            camera.parm("tx").set(0.05)
        entry.changed(object_pose=True)
        assert entry.defer_source_capture(), "Pure transforms did not defer their source scan"
        generation = entry.preview_object_poses()
        assert generation is not None, (index, kind, shape)
        preview = pixels(preview_frame(session, generation))
        assert session.status()["renderer_pid"] == pid
        assert session.status()["geometry_conversions"] == conversions
        if index == 0:
            # A later partial source packet contains neither camera nor lights.
            # It must retain both accepted previews before the full reference.
            partial = pixels(session.wait(entry.apply(dict(background=(0, 0, 0)), hou.time())))
            assert np.abs(partial - preview).mean() < 0.001
        reference = pixels(session.wait(entry.apply(native_scene.capture(rop), hou.time())))
        difference = float(np.abs(preview - reference).mean())
        assert difference < 0.001, (kind, shape, two_sided, reverse, difference)
        if index == 0:
            assert np.abs(initial - preview).mean() > 0.005, "Moving an area light did not change illumination"
        checks.append(
            dict(
                kind=kind,
                shape=shape,
                two_sided=two_sided,
                reverse=reverse,
                preview_reference_mean_error=difference,
                worker_pid=pid,
            )
        )
    entry.changed(object_pose=True)
    assert entry.defer_source_capture()
    entry.changed()  # A SOP/shader edit must break the deferral immediately.
    entry.changed(object_pose=True)
    assert not entry.defer_source_capture(), "A later transform hid a pending source change"
    checks.append("Pending geometry/material edits revoke transform deferral and survive subsequent transforms")
    # Removing and recreating the source cannot resurrect its old cached lights.
    old = light.path()
    light.destroy()
    entry.changed(object_pose=True)
    entry.preview_object_poses()
    session.wait(entry.apply(native_scene.capture(rop), hou.time()))
    assert session.status()["light_resources"] == 0
    light = scene.createNode(light_properties.LIGHT_TYPE, old.rsplit("/", 1)[-1])
    light.setParms(dict(tz=3, light_intensity=80))
    session.wait(entry.apply(native_scene.capture(rop), hou.time()))
    light.parm("tx").set(0.7)
    entry.changed(object_pose=True)
    repaired = pixels(preview_frame(session, entry.preview_object_poses()))
    reference = pixels(session.wait(entry.apply(native_scene.capture(rop), hou.time())))
    assert np.abs(repaired - reference).mean() < 0.001
    checks.append("Deleting/recreating a light repairs its transform preview without restarting the worker")

    # Look At targets can sit outside the ROP's scene root. Move the target
    # through its parent and send the actual observer routes to the live IPR.
    target_parent = obj.createNode("null", "pose_target_parent")
    target = obj.createNode("null", "pose_target")
    target.setInput(0, target_parent)
    light.setParms(dict(cycles_light_type="spot", cycles_light_spot_angle=45, lookatpath=target.path(), tx=0, ty=0))
    before = pixels(session.wait(entry.apply(native_scene.capture(rop), hou.time())))
    source_events = []

    def move_source(node):
        source_events.append(node.sessionId())
        entry.changed(object_pose=True, source_node=node)

    observer = native_scene.SceneObserver(rop, entry.changed, object_pose_source_callback=move_source)
    try:
        target_parent.parm("tx").set(1.7)
        assert target_parent.sessionId() in source_events, source_events
        assert not entry.pose_preview_host_safe()
        generation = entry.preview_object_poses()
        assert generation is not None
        preview = pixels(preview_frame(session, generation))
        reference = pixels(session.wait(entry.apply(native_scene.capture(rop), hou.time())))
    finally:
        observer.close()
    difference = float(np.abs(preview - reference).mean())
    signal = float(np.abs(before - preview).mean())
    assert difference < 0.001 and signal > 0.005, (difference, signal)
    assert session.status()["renderer_pid"] == pid and session.status()["geometry_conversions"] == conversions
    checks.append(
        dict(case="Look At target moved through a parent outside the render root", error=difference, signal=signal)
    )

    environment = scene.createNode("envlight", "pose_environment")
    rop.parm("lights").set(light.path() + " " + environment.path())
    rop.parm("world_source").set("scene")
    session.wait(entry.apply(native_scene.capture(rop), hou.time()))
    assert entry.environment_pose_source is not None
    environment.parm("ry").set(30)
    entry.changed(object_pose=True)
    assert not entry.defer_source_capture(), "World mapping was deferred despite lacking an analytic-pose preview"
    session.wait(entry.apply(native_scene.capture(rop), hou.time()))
    checks.append("Environment orientation retains the authoritative world-shader update path")
report = dict(
    result="passed",
    device=device,
    checks=checks,
    scope="One live persistent worker; parented translate/rotate/scale and analytic light variants compared to full reference captures",
)
destination = artifacts_root() / "stage1/navigation"
destination.mkdir(parents=True, exist_ok=True)
(destination / "houdini-light-pose-navigation-smoke.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(dict(result="passed", checks=len(checks))))
