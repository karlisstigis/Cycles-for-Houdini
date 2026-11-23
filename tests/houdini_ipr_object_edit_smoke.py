# SPDX-License-Identifier: Apache-2.0
"""Headless: object transform edits end without a complete scene read when nothing
else depends on the moved objects, and with one when something does.

Session A follows the Cycles Render panel / Render View rules (pose previews during
the gesture, Entry.narrow_update after it); session B (identical second ROP) always
takes the complete read. Each case: image A == image B, the path taken is recorded,
the checker (CYCLES_HOUDINI_NARROW_CHECK) is silent. Scene in memory, never saved.
Usage: hython houdini_ipr_object_edit_smoke.py --device CPU [--blur]
"""

import json
import os
import sys
import time

os.environ["CYCLES_HOUDINI_NARROW_CHECK"] = "1"
import hou
import numpy as np
from cycles_houdini import native_scene, registry
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
cam = obj.createNode("cam", "oe_cam")
cam.setParms(dict(ty=2, tz=9, rx=-10, resx=64, resy=36))
camera_properties.install(cam)
light = obj.createNode("hlight::2.0", "oe_light")
light.parm("light_type").set(menu_pick(light.parm("light_type"), "point"))
light.setParms(dict(tx=-2, ty=4, tz=3, light_intensity=20.0))
light_properties.install(light)


def geo(name, kind, **parms):
    node = obj.createNode("geo", name)
    object_properties.install(node)
    sop = node.createNode(kind)
    if kind == "sphere":
        sop.parm("type").set(menu_pick(sop.parm("type"), "polygon"))
    sop.setRenderFlag(True)
    node.setParms(parms)
    return node


ground = geo("oe_ground", "grid")
parent = obj.createNode("null", "oe_parent")
ball = geo("oe_ball", "sphere", ty=1)
ball.setInput(0, parent)
child = geo("oe_child", "box", tx=1.2)
child.setInput(0, ball)
target = geo("oe_target", "box", tx=-2, ty=0.5)
merger = obj.createNode("geo", "oe_merger")
object_properties.install(merger)
merge = merger.createNode("object_merge")
merge.parm("objpath1").set(target.path())
merge.parm("xformtype").set(1)  # into this object: the merged geometry bakes target's transform
merge.setRenderFlag(True)
rops = []
for name in ("oe_rop_a", "oe_rop_b"):
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
    for node in (ball, parent):
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


report = dict(device=device, blur=blur, cases=[])


def case(name, gesture, expected):
    registry.close_all()
    entry_a, entry_b = start(rop_a), start(rop_b)
    for value in gesture:  # a short drag: pose previews while it lasts
        value()
        entry_a.preview_object_poses()
        time.sleep(0.03)
    time.sleep(0.2)  # the gesture settles (defer_source_capture ends)
    now = hou.time()
    motion = native_scene.motion_configuration(rop_a, now)
    narrow = entry_a.narrow_update(now, motion, native_scene.capture(rop_a, at_time=now, camera_only=True))
    if narrow is not None:
        generation = entry_a.apply(narrow, now, reuse_if_unchanged=True, change_serial=entry_a.change_serial)
        path = entry_a.narrow_reason
    else:
        generation = full(entry_a, rop_a)
        path = entry_a.narrow_reason or "full read"
    image_a = wait_complete(entry_a, generation)
    image_b = wait_complete(entry_b, full(entry_b, rop_b))
    check = entry_a.run_narrow_check() if narrow is not None else None
    row = dict(
        case=name,
        path=path,
        equal=bool(np.array_equal(image_a, image_b)),
        max_difference=float(np.abs(image_a - image_b).max()),
        checker=None if check is None else check["differences"],
        captures=len([e for e in entry_a.session._capture_events if not e.get("navigation_only")]),
    )
    report["cases"].append(row)
    print("OBJECT_EDIT " + json.dumps(row), flush=True)
    assert row["equal"], row
    assert (path == "poses") == (expected == "poses"), row
    assert not row["checker"], row


steps = lambda node, parm, values: [lambda v=v: node.parm(parm).set(v) for v in values]
case("move an object", steps(ball, "tx", (0.2, 0.4, 0.6)), "poses")
case("move a parent null (descendants follow)", steps(parent, "tx", (0.3, 0.6)), "poses")
case("rotate an object with an OBJ child", steps(ball, "ry", (15, 30)), "poses")
linked = obj.createNode("hlight::2.0", "oe_linked_light")
linked.parm("light_type").set(menu_pick(linked.parm("light_type"), "point"))
linked.setParms(dict(tx=2, ty=3, tz=2, light_intensity=8.0))
light_properties.install(linked)
linked.parm("cycles_illuminates").set("* ^" + ball.path())
case("move an object a light's linking excludes", steps(ball, "tx", (0.1, -0.2)), "poses")


def light_values_after_move(name):
    # After a committed move the controller still holds the moved roots; each later light-value read is posed
    # from them (navigation_snapshot). The 2nd read reuses the memoised posed object table: images must match a
    # full read of the same state.
    case(name + ": the move", steps(ball, "tx", (0.4, 0.7)), "poses")
    entry_a, entry_b = (registry._entries["rop:%d" % rop.sessionId()] for rop in (rop_a, rop_b))
    for value in (30.0, 45.0):
        light.parm("light_intensity").set(value)
        now = hou.time()
        motion = native_scene.motion_configuration(rop_a, now)
        narrow = entry_a.narrow_update(now, motion, native_scene.capture(rop_a, at_time=now, camera_only=True))
        assert narrow is not None, entry_a.narrow_reason
        path = entry_a.narrow_reason
        image_a = wait_complete(
            entry_a, entry_a.apply(narrow, now, reuse_if_unchanged=True, change_serial=entry_a.change_serial)
        )
        image_b = wait_complete(entry_b, full(entry_b, rop_b))
        row = dict(
            case=name + ": light intensity %g" % value,
            path=path,
            equal=bool(np.array_equal(image_a, image_b)),
            max_difference=float(np.abs(image_a - image_b).max()),
        )
        report["cases"].append(row)
        print("OBJECT_EDIT " + json.dumps(row), flush=True)
        assert row["equal"], row
        assert path == "lights", row
    light.parm("light_intensity").set(20.0)


light_values_after_move("light values after an object move")

# The reader below makes the ball's subtree a VEX dependency: later ball moves need the full read.
# A dependency Houdini may not report: VEX optransform() of the moved object.
reader = obj.createNode("geo", "oe_reader")
object_properties.install(reader)
box = reader.createNode("box")
box.parmTuple("size").set((0.3, 0.3, 0.3))
wrangle = reader.createNode("attribwrangle", "read_transform")
wrangle.setInput(0, box)
wrangle.parm("snippet").set('@P += cracktransform(0, 0, 0, {0, 0, 0}, optransform("%s")) * 0.5;' % child.path())
wrangle.setRenderFlag(True)
reader.parm("tz").set(-1)
wrangle.geometry()
case("move an object a VEX optransform() reads", steps(child, "tx", (1.6, 2.0)), "full")
os.environ["CYCLES_HOUDINI_POSE_COMMIT"] = "0"
case("move an object with pose commit switched off", steps(ball, "tx", (0.0, 0.3)), "full")
os.environ.pop("CYCLES_HOUDINI_POSE_COMMIT")
case("move an object another object merges into itself", steps(target, "tx", (-1.5, -1.0)), "full")
# "Into this object" bakes the merger's own transform into its geometry: complete read.
case("move the merging object itself", steps(merger, "tx", (0.5, 1.0)), "full")

registry.close_all()
print("OBJECT_EDIT_RESULT " + json.dumps(report), flush=True)
print("PASS", flush=True)
hou.exit(0, suppress_save_prompt=True)
