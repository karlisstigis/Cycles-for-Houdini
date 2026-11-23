# SPDX-License-Identifier: Apache-2.0
"""Headless: gesture moves of objects that own many instances travel as root matrices
(protocol 91, WorkerUpdate.root_poses) and give the same image as the per-instance path.

Every case runs twice from the same scene state: session A with root poses
(CYCLES_HOUDINI_ROOT_POSES unset) and session A again with the per-instance path
(CYCLES_HOUDINI_ROOT_POSES=0). After the gesture both take Entry.narrow_update (the
pose commit), and a third session (ROP B) takes a complete read. All three images
must be identical. The root path must have been used (navigation_stats counters).
Also: the posed-table memo of the per-instance path is hit by a light edit after a move.
Scene in memory, never saved. Usage: hython houdini_ipr_root_pose_smoke.py --device CPU [--blur]
"""

import json
import os
import sys
import time

import hou
import numpy as np
from cycles_houdini import _native, native_scene, registry
from cycles_houdini.core import device_selection
from cycles_houdini.scene import camera_properties, light_properties, object_properties

device = sys.argv[sys.argv.index("--device") + 1] if "--device" in sys.argv else "CPU"
blur = "--blur" in sys.argv


def menu_pick(parm, *words):
    for item, label in zip(parm.parmTemplate().menuItems(), parm.parmTemplate().menuLabels()):
        if any(w in item.lower() or w in label.lower() for w in words):
            return item
    raise ValueError(words)


obj = hou.node("/obj")
cam = obj.createNode("cam", "rp_cam")
cam.setParms(dict(ty=2, tz=10, rx=-10, resx=64, resy=36))
camera_properties.install(cam)
light = obj.createNode("hlight::2.0", "rp_light")
light.parm("light_type").set(menu_pick(light.parm("light_type"), "point"))
light.setParms(dict(tx=-2, ty=4, tz=3, light_intensity=20.0))
light_properties.install(light)


def instancer(name, rows, **parms):
    """A geo object whose render SOP is packed copies on a grid: one root, rows x rows instances."""
    node = obj.createNode("geo", name)
    object_properties.install(node)
    grid = node.createNode("grid")
    grid.setParms(dict(sizex=2, sizey=2, rows=rows, cols=rows))
    box = node.createNode("box")
    box.parmTuple("size").set((0.12, 0.12, 0.12))
    copy = node.createNode("copytopoints::2.0")
    copy.setInput(0, box)
    copy.setInput(1, grid)
    copy.parm("pack").set(1)
    copy.setRenderFlag(True)
    node.setParms(parms)
    return node


def single(name, **parms):
    node = obj.createNode("geo", name)
    object_properties.install(node)
    sphere = node.createNode("sphere")
    sphere.parm("type").set(menu_pick(sphere.parm("type"), "polygon"))
    sphere.setRenderFlag(True)
    node.setParms(parms)
    return node


ground = obj.createNode("geo", "rp_ground")
object_properties.install(ground)
ground.createNode("grid").setRenderFlag(True)
swarm = instancer("rp_swarm", 12, ty=0.5)  # one root, 144 instances
lone = single("rp_lone", tx=2.5, ty=1)  # a root with a single object
group = obj.createNode("null", "rp_group")  # moves several roots
left = instancer("rp_left", 5, tx=-2.5, ty=1.5)
right = instancer("rp_right", 4, tx=2.5, ty=2.5)
left.setInput(0, group)
right.setInput(0, group)
nested = instancer("rp_nested", 3, ty=0.8)  # a root parented to another instancing root
nested.setInput(0, left)
rops = []
for name in ("rp_rop_a", "rp_rop_b"):
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
    device_selection.set_devices(rop, [device])
    rops.append(rop)
rop_a, rop_b = rops
if blur:
    for node in (swarm, group, lone):
        for frame, value in ((1, 0.0), (24, 0.5)):
            key = hou.Keyframe()
            key.setFrame(frame)
            key.setValue(value)
            node.parm("tz").setKeyframe(key)
    hou.setFrame(5)


def wait_complete(entry, generation, timeout=60):
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


def gesture_then_commit(steps):
    """One session on ROP A: start, drag (pose previews), settle, narrow update; returns (image, path)."""
    registry.close_all()
    entry = start(rop_a)
    for step in steps:
        step()
        entry.preview_object_poses()
        time.sleep(0.03)
    time.sleep(0.2)
    now = hou.time()
    motion = native_scene.motion_configuration(rop_a, now)
    narrow = entry.narrow_update(now, motion, native_scene.capture(rop_a, at_time=now, camera_only=True))
    if narrow is not None:
        generation = entry.apply(narrow, now, reuse_if_unchanged=True, change_serial=entry.change_serial)
        path = entry.narrow_reason
    else:
        generation, path = full(entry, rop_a), entry.narrow_reason or "full read"
    return wait_complete(entry, generation), path


report = dict(device=device, blur=blur, cases=[])


def case(name, node, parm, values):
    start_value = node.parm(parm).eval()
    steps = [lambda v=v: node.parm(parm).set(v) for v in values]
    images, paths, stats = {}, {}, {}
    for mode in os.environ.get("RP_ORDER", "roots,instances").split(","):
        node.parm(parm).set(start_value)
        if mode == "instances":
            os.environ["CYCLES_HOUDINI_ROOT_POSES"] = "0"
        else:
            os.environ.pop("CYCLES_HOUDINI_ROOT_POSES", None)
        before = _native.navigation_stats()
        images[mode], paths[mode] = gesture_then_commit(steps)
        after = _native.navigation_stats()
        stats[mode] = {
            k: after[k] - before[k] for k in ("root_previews", "root_updates", "origin_sends", "materializations")
        }
    os.environ.pop("CYCLES_HOUDINI_ROOT_POSES", None)
    registry.close_all()
    reference = start(rop_b)  # complete read of the final state
    image_full = wait_complete(reference, full(reference, rop_b))
    row = dict(
        case=name,
        paths=paths,
        stats=stats,
        roots_equal_instances=bool(np.array_equal(images["roots"], images["instances"])),
        roots_equal_full=bool(np.array_equal(images["roots"], image_full)),
        max_difference=float(np.abs(images["roots"] - image_full).max()),
        instances_max_difference=float(np.abs(images["instances"] - image_full).max()),
        rerun_full_equal=bool(np.array_equal(image_full, wait_complete(reference, full(reference, rop_b)))),
    )
    report["cases"].append(row)
    print("ROOT_POSE " + json.dumps(row), flush=True)
    # The per-instance path itself is not bit-identical to the complete read after a rotation (host and renderer
    # compose root x origin in different compilation units; measured 7.5e-9). The root path must be at least as
    # close to the complete read as the per-instance path, and equal to one of them.
    assert row["max_difference"] <= row["instances_max_difference"] and row["max_difference"] < 1e-6, row
    assert paths["roots"] == "poses" and paths["instances"] == "poses", row
    assert stats["roots"]["root_previews"] > 0 and stats["roots"]["root_updates"] > 0, row
    assert stats["instances"]["root_previews"] == 0, row


case("move an object owning many instances", swarm, "tx", (0.2, 0.5, 0.8))
case("rotate an object owning many instances", swarm, "ry", (10, 25))
case("move a root with a single object", lone, "ty", (1.4, 1.8))
case("move a parent null over several roots (and a nested root)", group, "tx", (0.3, 0.7))
case("move a root parented to another root", nested, "tx", (0.4, 0.9))
case("move the outer of two nested roots", left, "ty", (1.8, 2.2))


def variant(name, env, kill_worker=False):
    """A move of the 144-instance root under one transport variant; image must equal a complete read."""
    swarm.parm("tx").set(0.0)
    os.environ.pop("CYCLES_HOUDINI_ROOT_POSES", None)
    os.environ.update(env)
    try:
        registry.close_all()
        entry = start(rop_a)
        errors = []
        for n, value in enumerate((0.3, 0.6, 0.9, 1.2)):
            if kill_worker and n == 2:
                # The renderer dies mid-drag: the session fails visibly, the next read restarts it
                # with every instance pose (no acknowledged snapshot), and the drag continues.
                entry.session._controller.backend.process.kill()
                end = time.monotonic() + 30
                while entry.session.status()["state"] != "failed" and time.monotonic() < end:
                    time.sleep(0.01)
                errors.append(entry.session.status()["error"])
                wait_complete(entry, full(entry, rop_a))
            swarm.parm("tx").set(value)
            entry.preview_object_poses()
            time.sleep(0.05)
        time.sleep(0.2)
        now = hou.time()
        narrow = entry.narrow_update(
            now,
            native_scene.motion_configuration(rop_a, now),
            native_scene.capture(rop_a, at_time=now, camera_only=True),
        )
        assert narrow is not None, entry.narrow_reason
        image = wait_complete(
            entry, entry.apply(narrow, now, reuse_if_unchanged=True, change_serial=entry.change_serial)
        )
        status = entry.session.status()
        metrics = entry.session._controller.backend.metrics if entry.session._controller.backend else {}
    finally:
        for key in env:
            os.environ.pop(key, None)
    registry.close_all()
    reference = start(rop_b)
    image_full = wait_complete(reference, full(reference, rop_b))
    row = dict(
        case=name,
        equal=bool(np.array_equal(image, image_full)),
        max_difference=float(np.abs(image - image_full).max()),
        errors=errors,
        final_error=status["error"],
        fallbacks=metrics.get("root_pose_fallbacks", 0),
    )
    report["cases"].append(row)
    print("ROOT_POSE " + json.dumps(row), flush=True)
    # Host-side materialization (legacy wire, fallback) composes root x origin in the host's compilation unit:
    # the same float rounding as the per-instance path (measured 3.7e-9), not bit-identical to a complete read.
    assert row["max_difference"] < 1e-6, row
    assert not any("origins" in str(e) for e in errors + [status["error"]]), row
    return row


variant("legacy wire switch (CYCLES_HOUDINI_WIRE_LEGACY=1)", {"CYCLES_HOUDINI_WIRE_LEGACY": "1"})
row = variant(
    "renderer refuses root poses (test hook): per-instance retry", {"CYCLES_HOUDINI_TEST_REFUSE_ROOT_POSES": "1"}
)
assert row["fallbacks"] >= 1 and not row["final_error"], row
variant("renderer killed mid-drag, restarted by the next read", {}, kill_worker=True)

# The per-instance path's posed-table memo: a light value read after a move reuses the posed table.
os.environ["CYCLES_HOUDINI_ROOT_POSES"] = "0"
registry.close_all()
entry = start(rop_a)
for value in (1.0, 1.3):
    swarm.parm("tx").set(value)
    entry.preview_object_poses()
    time.sleep(0.03)
time.sleep(0.2)
now = hou.time()
narrow = entry.narrow_update(
    now, native_scene.motion_configuration(rop_a, now), native_scene.capture(rop_a, at_time=now, camera_only=True)
)
wait_complete(entry, entry.apply(narrow, now, reuse_if_unchanged=True, change_serial=entry.change_serial))
before = _native.navigation_stats()
for value in (30.0, 45.0):
    light.parm("light_intensity").set(value)
    now = hou.time()
    narrow = entry.narrow_update(
        now, native_scene.motion_configuration(rop_a, now), native_scene.capture(rop_a, at_time=now, camera_only=True)
    )
    assert narrow is not None and entry.narrow_reason == "lights", entry.narrow_reason
    wait_complete(entry, entry.apply(narrow, now, reuse_if_unchanged=True, change_serial=entry.change_serial))
hits = _native.navigation_stats()["memo_hits"] - before["memo_hits"]
report["memo_hits_two_light_reads"] = hits
print("ROOT_POSE_MEMO " + json.dumps(dict(memo_hits=hits)), flush=True)
assert hits >= 1, hits
os.environ.pop("CYCLES_HOUDINI_ROOT_POSES", None)
registry.close_all()
print("ROOT_POSE_RESULT " + json.dumps(report), flush=True)
print("PASS", flush=True)
hou.exit(0, suppress_save_prompt=True)
