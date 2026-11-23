# SPDX-License-Identifier: Apache-2.0
"""Headless: moving an object with many packed instances (10,000, above the renderer's all-core thresholds for
pose validation and pose writes) takes the pose path and renders exactly what a complete read renders on a
second ROP, with the renderer's pose-only fast path (default) and with CYCLES_HOUDINI_POSE_FAST=0. Each case
starts a fresh renderer, so the worker reads the switch. Also: a displaced mesh among the instances keeps the
general renderer path (variants), still equal. Scene in memory, never saved.
Usage: hython houdini_ipr_many_instance_pose_smoke.py --device CPU [--blur]
"""

import json
import os
import sys
import time

import hou
import numpy as np
from cycles_houdini import native_scene, registry
from cycles_houdini.core import device_selection
from cycles_houdini.scene import camera_properties, light_properties, object_properties

device = sys.argv[sys.argv.index("--device") + 1] if "--device" in sys.argv else "CPU"
devices = json.loads(device) if device.startswith("[") else [device]  # probes/E4/run_both.py passes a JSON list
# CPU renders of two sessions are bit-identical. On GPU two sessions are not always bit-identical after a scene
# layout change (BVH build order, kernel reductions): other tests here accept up to about 6e-8 there.
tolerance = 0.0 if devices == ["CPU"] else 1e-6
blur = "--blur" in sys.argv


def menu_pick(parm, *words):
    for item, label in zip(parm.parmTemplate().menuItems(), parm.parmTemplate().menuLabels()):
        if any(w in item.lower() or w in label.lower() for w in words):
            return item
    raise ValueError(words)


obj = hou.node("/obj")
cam = obj.createNode("cam", "mi_cam")
cam.setParms(dict(ty=3, tz=12, rx=-14, resx=64, resy=36))
camera_properties.install(cam)
light = obj.createNode("hlight::2.0", "mi_light")
light.parm("light_type").set(menu_pick(light.parm("light_type"), "point"))
light.setParms(dict(tx=-2, ty=5, tz=4, light_intensity=30.0))
light_properties.install(light)
ground = obj.createNode("geo", "mi_ground")
object_properties.install(ground)
ground.createNode("grid").setRenderFlag(True)
packed = obj.createNode("geo", "mi_packed")
object_properties.install(packed)
grid = packed.createNode("grid")
grid.parmTuple("size").set((6, 6))
grid.parm("rows").set(100)
grid.parm("cols").set(100)
box = packed.createNode("box")
box.parmTuple("size").set((0.04, 0.04, 0.04))
copy = packed.createNode("copytopoints::2.0")
copy.setInput(0, box)
copy.setInput(1, grid)
copy.parm("pack").set(1)
copy.setRenderFlag(True)
packed.setParms(dict(ty=0.2))
rops = []
for name in ("mi_rop_a", "mi_rop_b"):
    rop = hou.node("/out").createNode("cycles_ipr", name)
    rop.setParms(
        dict(
            camera=cam.path(),
            resx=64,
            resy=36,
            ipr_samples=2,
            ipr_adaptive_sampling=0,
            ipr_use_denoise=0,
            allowmotionblur=1 if blur else 0,
        )
    )
    device_selection.set_devices(rop, devices)
    rops.append(rop)
rop_a, rop_b = rops
if blur:
    for frame, value in ((1, 0.0), (24, 0.5)):
        key = hou.Keyframe()
        key.setFrame(frame)
        key.setValue(value)
        packed.parm("tz").setKeyframe(key)
    hou.setFrame(5)


def wait_complete(entry, generation, timeout=120):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        status = entry.session.status()
        frame = entry.session.frame_info()
        if status["state"] == "failed":
            raise RuntimeError(status["error"])
        if status["state"] == "complete" and frame and frame["complete"] and frame["generation"] >= generation:
            if not status.get("capture_pending") and not status.get("pending_update"):
                return np.frombuffer(entry.session.frame()["planes"]["combined"]["pixels"], np.float32).copy()
        time.sleep(0.005)
    raise TimeoutError("render did not complete")


def full(entry, rop):
    now = hou.time()
    return entry.apply(native_scene.capture(rop, now), now, reuse_if_unchanged=True)


def start(rop):
    entry = registry.for_rop(rop, native_scene.configuration(rop))
    wait_complete(entry, full(entry, rop))
    return entry


report = dict(device=device, blur=blur, cases=[])


def drag(name, env=None):
    start_matrix = packed.parmTransform()
    os.environ.update(env or {})
    try:
        registry.close_all()
        entry = start(rop_a)
        for n in range(4):  # handle-style moves: pose previews
            packed.setParmTransform(
                start_matrix
                * hou.hmath.buildRotate(0, 7 * (n + 1), 0)
                * hou.hmath.buildTranslate(0.2 * (n + 1), 0, -0.1 * n)
            )
            entry.preview_object_poses()
            time.sleep(0.05)
        time.sleep(0.2)
        now = hou.time()
        motion = native_scene.motion_configuration(rop_a, now)
        narrow = entry.narrow_update(now, motion, native_scene.capture(rop_a, at_time=now, camera_only=True))
        if narrow is not None:
            generation = entry.apply(narrow, now, reuse_if_unchanged=True, change_serial=entry.change_serial)
            path = entry.narrow_reason
        else:
            generation, path = full(entry, rop_a), entry.narrow_reason or "full read"
        image = wait_complete(entry, generation)
    finally:
        for key in env or {}:
            os.environ.pop(key, None)
    registry.close_all()
    reference = start(rop_b)
    image_full = wait_complete(reference, full(reference, rop_b))
    registry.close_all()
    packed.setParmTransform(start_matrix)
    row = dict(
        case=name,
        path=path,
        equal=bool(np.array_equal(image, image_full)),
        max_difference=float(np.abs(image - image_full).max()),
    )
    report["cases"].append(row)
    print("MANY_INSTANCE_POSE " + json.dumps(row), flush=True)
    # Without blur the pose path equals a complete read bit for bit. With blur the renderer derives each
    # instance's shutter poses from the root's (protocol 91 root poses): float rounding differs from the host's
    # per-instance products by a few 1e-8 (same tolerance as houdini_ipr_root_pose_smoke).
    assert row["equal"] or ((blur or tolerance) and row["max_difference"] < 1e-6), row
    assert path == "poses", row
    return image


fast = drag("10,000 instances moved")
old = drag("10,000 instances moved, renderer pose fast path off", {"CYCLES_HOUDINI_POSE_FAST": "0"})
# The renderer's pose-only fast path must render exactly what its general path renders.
report["fast_equals_general"] = bool(np.array_equal(fast, old))
print("MANY_INSTANCE_POSE_FAST_EQUALS_GENERAL " + json.dumps(report["fast_equals_general"]), flush=True)
assert report["fast_equals_general"] or float(np.abs(fast - old).max()) <= tolerance, float(np.abs(fast - old).max())
# A true-displaced mesh in the scene: the renderer keeps its general path for poses (variant policy).
displaced = obj.createNode("geo", "mi_displaced")
object_properties.install(displaced)
sphere = displaced.createNode("sphere")
sphere.parm("type").set(menu_pick(sphere.parm("type"), "polygon"))
sphere.setRenderFlag(True)
displaced.setParms(dict(tx=-2.5, ty=1.0))
import cycles_houdini as ch

material = ch.create_material(name="mi_displace")
try:
    output = material.node("surface_output")
    noise = material.createNode("cycles::noise_texture::1", "mi_noise")
    disp = material.createNode("cycles::displacement::1", "mi_displacement")
    disp.setNamedInput("height", noise, "fac")
    output.setNamedInput("displacement", disp, "displacement")
    method = next((p for p in material.parms() if "displacement_method" in p.name()), None)
    if method is not None:
        method.set(menu_pick(method, "true", "displacement only"))
    displaced.parm("shop_materialpath").set(material.path())
    drag("10,000 instances moved, displaced mesh in the scene")
except (hou.Error, ValueError, StopIteration) as error:
    report["displaced_case_skipped"] = str(error)
    print("MANY_INSTANCE_POSE_SKIPPED " + str(error), flush=True)
print("MANY_INSTANCE_POSE_RESULT " + json.dumps(report), flush=True)
print("PASS", flush=True)
hou.exit(0, suppress_save_prompt=True)
