# SPDX-License-Identifier: Apache-2.0
"""Headless IPR edit latency contracts (no GUI, scene built in memory, never saved).

1. A complete capture of an unchanged small scene reaches the renderer within one
   host poll: consecutive capture stages run in the same poll (queued.py chain).
2. Houdini's own IPR update during a transform-only light gesture takes the pose
   preview route (Entry.gesture_pose_update) instead of a complete capture, and the
   authoritative capture after the gesture gives the same image as a fresh session.
"""

import json
import sys
import time

import hou
import numpy as np
from cycles_houdini import native_scene, registry
from cycles_houdini.core import device_selection
from cycles_houdini.scene import camera_properties, light_properties, object_properties


def menu_pick(parm, *words):
    for item, label in zip(parm.parmTemplate().menuItems(), parm.parmTemplate().menuLabels()):
        if any(w in item.lower() or w in label.lower() for w in words):
            return item
    raise ValueError(words)


device = sys.argv[sys.argv.index("--device") + 1] if "--device" in sys.argv else "CPU"
obj = hou.node("/obj")
cam = obj.createNode("cam", "gesture_cam")
cam.setParms(dict(ty=2, tz=8, rx=-10, resx=160, resy=90))
camera_properties.install(cam)
light = obj.createNode("hlight::2.0", "gesture_light")
light.parm("light_type").set(menu_pick(light.parm("light_type"), "grid", "area"))
light.setParms(dict(tx=-2, ty=4, tz=3, rx=-50, light_intensity=20.0))
light_properties.install(light)
geo = obj.createNode("geo", "gesture_geo")
object_properties.install(geo)
sphere = geo.createNode("sphere")
sphere.parm("type").set(menu_pick(sphere.parm("type"), "polygon"))
sphere.setRenderFlag(True)
rop = hou.node("/out").createNode("cycles_ipr", "gesture_rop")
rop.setParms(dict(camera=cam.path(), resx=160, resy=90, ipr_samples=8, ipr_adaptive_sampling=0, ipr_use_denoise=0))
device_selection.set_devices(rop, [device])
report = dict(device=device)


def wait_complete(entry, generation, timeout=60):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        status = entry.session.status()
        frame = entry.session.frame_info()
        if (
            status["state"] == "complete"
            and frame
            and frame["complete"]
            and frame["generation"] >= generation
            and not status.get("capture_pending")
        ):
            return np.frombuffer(entry.session.frame()["planes"]["combined"]["pixels"], np.float32).copy()
        if status["state"] == "failed":
            raise RuntimeError(status["error"])
        time.sleep(0.01)
    raise TimeoutError("render did not complete")


def full_capture(entry):
    now = hou.time()
    return entry.apply(native_scene.capture(rop, now), now, reuse_if_unchanged=True)


now = hou.time()
entry = registry.for_rop(rop, native_scene.configuration(rop))
entry.channel = "headless-test"  # stands for a bound Render View
generation = full_capture(entry)
wait_complete(entry, generation)

# 1. One poll completes a trivial capture.
light.parm("light_intensity").set(25.0)
entry.apply(native_scene.capture(rop, hou.time()), hou.time(), reuse_if_unchanged=True)
pending_after_apply = entry.session.status()["capture_pending"]
entry.session.status()  # the next host poll
report["capture_pending_after_one_poll"] = entry.session.status()["capture_pending"]
assert not report["capture_pending_after_one_poll"], "capture needed more than one host poll"

# 2. Transform-only gesture: pose route, no complete capture.
wait_complete(entry, entry.session.status()["generation"])
applied = entry.applied_serial
captures_before = len(entry.session._capture_events)
routed = []
for step in range(6):
    light.parm("tx").set(-2 + 0.2 * (step + 1))
    routed.append(entry.gesture_pose_update())
    time.sleep(0.03)
report["gesture_routed"] = [g is not None for g in routed]
assert all(g is not None for g in routed), routed
assert entry.applied_serial == applied, "a gesture step acknowledged the source capture"
entry.session.status()
assert len(entry.session._capture_events) == captures_before, "a gesture step ran a complete capture"
time.sleep(0.2)
assert entry.gesture_pose_update() is None, "settled gesture must take the complete capture"
generation = full_capture(entry)
assert entry.applied_serial == entry.change_serial
after_gesture = wait_complete(entry, generation)

# Same image as a fresh session with the final scene.
registry.close_all()
entry = registry.for_rop(rop, native_scene.configuration(rop))
fresh = wait_complete(entry, full_capture(entry))
report["max_abs_difference"] = float(np.abs(after_gesture - fresh).max())
assert np.array_equal(after_gesture, fresh), report["max_abs_difference"]
registry.close_all()
print("GESTURE_CAPTURE_RESULT " + json.dumps(report), flush=True)
print("PASS", flush=True)
hou.exit(0, suppress_save_prompt=True)
